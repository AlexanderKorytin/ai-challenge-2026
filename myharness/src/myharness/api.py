"""Тонкая обёртка над API моделей в формате OpenAI: DeepSeek и местная служба Ollama.

Поставщика называет имя модели (`providers`): распределитель `Clients` держит по клиенту на
поставщика и отдаёт запрос нужному. Вызывающий код знает только распределитель."""

from __future__ import annotations

import asyncio
import ipaddress
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import urllib.request
from urllib.parse import urlsplit

import httpx2
from openai import (
    DEFAULT_CONNECTION_LIMITS,
    DEFAULT_MAX_RETRIES,
    APIConnectionError,
    APITimeoutError,
    AsyncOpenAI,
    AuthenticationError,
    DefaultAsyncHttpxClient,
    Timeout,
)

from . import params as params_mod
from . import providers, tokens

BASE_URL = "https://api.deepseek.com"

# SDK по умолчанию даёт всего 5с на установление соединения — на неидеальной сети
# (VPN и т.п.) этого мало и вылезает ложный "Request timed out" при живом сервисе.
#
# Чтение — десять минут, и это не запас про запас. Окно моделей v4 — миллион токенов, и
# запрос почти во всё окно модель обдумывает минутами; со ста двадцатью секундами harness
# обрывал бы такой запрос сообщением «истекло время ожидания» при полностью живом сервисе,
# то есть отправлял бы человека чинить сеть вместо того, чтобы уменьшить запрос. Прочие
# пределы оставлены прежними: они про установление связи, а не про размышление модели.
REQUEST_TIMEOUT = Timeout(connect=20.0, read=600.0, write=20.0, pool=20.0)
# Сколько раз повторять НЕУДАВШУЮСЯ УСТАНОВКУ СОЕДИНЕНИЯ: запрос при этом ещё не ушёл, так что
# повтор ничего не оплачивает дважды. Число — как у Claude Code, решение пользователя 2026-10-02
# по замеру (8 из 20 новых соединений с DeepSeek не вставали). Пауза между попытками — у
# библиотеки: 0, 0,5, 1, 2, 4 с и далее вдвое.
#
# ЦЕНА, названная целиком: библиотека OpenAI сама повторяет запрос при ошибке связи дважды
# (её умолчание), и каждый её заход заново проходит все попытки транспорта — до 3 × 11 = 33
# попыток соединения. При сети, где соединение не встаёт вовсе, один запрос ждёт до ~24 минут
# (11 × 20 с и паузы 255,5 с на заход), прежде чем сообщить о сбое; прежде было 3 × 20 с.
CONNECT_RETRIES = 10

# Резервный список — используется, если GET /models недоступен.
FALLBACK_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]

# Параметры, которые уходят прямыми аргументами метода, и те, что кладутся в extra_body.
DIRECT_PARAMS = ("temperature", "top_p", "max_tokens", "stop", "response_format")
EXTRA_BODY_PARAMS = ("thinking", "reasoning_effort")


@dataclass
class StreamEvent:
    kind: str  # "reasoning" | "content" | "tool_calls" | "tool_result" | "meta"
    text: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    # Только у события "tool_calls": вызовы инструментов круга, склеенные из кусков, по
    # порядку номера. Каждый — `{"id", "type": "function", "function": {"name",
    # "arguments"}}`, доводы — строка JSON как пришла: разбирает их исполнитель, не поток.
    calls: list[dict] = field(default_factory=list)


class AuthError(Exception):
    """Ключ отсутствует или не принят DeepSeek API."""


def split_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    direct = {k: v for k, v in params.items() if k in DIRECT_PARAMS and v is not None}
    extra = {k: v for k, v in params.items() if k in EXTRA_BODY_PARAMS and v is not None}
    return direct, extra


def _копить_вызов(вызовы: dict[int, dict[str, str]], кусок: Any) -> None:
    """Добавить один кусок `delta.tool_calls` к накопленному вызову с тем же номером.

    `id` и имя берутся первые непустые: сервер присылает их в первом куске, а в следующих
    оставляет пустыми, и перезапись пустым стёрла бы имя. Доводы склеиваются по порядку."""
    номер = getattr(кусок, "index", None)
    if isinstance(номер, bool) or not isinstance(номер, int):
        # Кусок без номера — продолжение последнего вызова, а не новый вызов: иначе его
        # части доводов стали бы отдельным вызовом без имени.
        номер = max(вызовы) if вызовы else 0
    вызов = вызовы.setdefault(номер, {"id": "", "name": "", "arguments": ""})
    if not вызов["id"] and getattr(кусок, "id", None):
        вызов["id"] = кусок.id
    функция = getattr(кусок, "function", None)
    if функция is None:
        return
    if not вызов["name"] and getattr(функция, "name", None):
        вызов["name"] = функция.name
    части = getattr(функция, "arguments", None)
    if части:
        вызов["arguments"] += части


def _собрать_вызовы(вызовы: dict[int, dict[str, str]]) -> list[dict]:
    """Накопленные вызовы в виде сообщения API, по порядку номера."""
    return [
        {
            "id": вызов["id"],
            "type": "function",
            "function": {"name": вызов["name"], "arguments": вызов["arguments"]},
        }
        for _, вызов in sorted(вызовы.items())
    ]


def _петлевой(host: str) -> bool:
    """Адрес этой же машины. К нему посредник не нужен никогда: при `ALL_PROXY` без `NO_PROXY`
    запрос к местной службе ушёл бы посреднику, а тот своей `127.0.0.1` не знает."""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def environment_proxy(url: str) -> str | None:
    """Посредник из окружения (и настроек системы) для этого адреса; `None` — идти напрямую.

    Клиент со СВОИМ транспортом посредника из окружения сам не читает: библиотека делает это
    только пока строит транспорт сама. Без этой функции запросы с ключом пошли бы мимо
    `HTTPS_PROXY` молча."""
    host = urlsplit(url).hostname or ""
    if _петлевой(host) or urllib.request.proxy_bypass(host):
        return None
    proxies = urllib.request.getproxies()
    proxy = proxies.get(urlsplit(url).scheme) or proxies.get("all")
    if not proxy:
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def _transport(url: str, retries: int = CONNECT_RETRIES) -> httpx2.AsyncHTTPTransport:
    """Транспорт клиента: повтор неудавшейся установки соединения, остальное — как было.

    Транспорт — из `httpx2`: на нём работает библиотека OpenAI, и транспорт другой библиотеки
    (`httpx`) она принимает молча, а падает на первом же запросе. `retries` повторяет только
    установку соединения, а не ушедший запрос; повторы самой библиотеки (ошибки связи, 429,
    5xx) остаются её умолчанием. Пределы пула и посредник заданы явно: со своим транспортом
    клиент их уже не подставляет.

    Оговорка: при посреднике повтор установки соединения НЕ действует — библиотека `httpx2`
    число повторов в пул посредника не передаёт. Запросы идут через посредника, как и
    положено, но без повтора: остаются только повторы самой библиотеки OpenAI."""
    return httpx2.AsyncHTTPTransport(
        retries=retries,
        limits=DEFAULT_CONNECTION_LIMITS,
        proxy=environment_proxy(url),
    )


class _ChatClient:
    """Клиент одного поставщика в формате OpenAI. Чем поставщики различаются — адрес, ключ,
    повторы и перевод ручек, — задают наследники; поток у всех один."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        transport: httpx2.AsyncBaseTransport,
        max_retries: int = DEFAULT_MAX_RETRIES,
    ) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=base_url,
            timeout=REQUEST_TIMEOUT,
            max_retries=max_retries,
            http_client=DefaultAsyncHttpxClient(timeout=REQUEST_TIMEOUT, transport=transport),
        )

    def _split(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return split_params(params)

    async def validate(self) -> bool:
        """False — ключ отклонён сервером (401). Прочие сбои (сеть и т.п.) пробрасываются вызывающему."""
        try:
            await self._client.models.list()
        except AuthenticationError:
            return False
        return True

    async def list_models(self) -> list[str]:
        response = await self._client.models.list()
        ids = sorted({m.id for m in response.data})
        return ids or FALLBACK_MODELS

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        params: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Потоковый ответ. Последним отдаёт событие "meta" с причиной остановки и расходом токенов.

        `tools` уходит в запрос прямым доводом и только непустым: без инструментов запрос
        остаётся байт в байт прежним. Вызовы модели приходят кусками `delta.tool_calls`:
        первый кусок вызова несёт `index`, `id`, `type` и имя с пустыми доводами, следующие с
        тем же `index` — части строки доводов (живой замер 2026-09-16). Куски копятся
        словарём по номеру и отдаются ОДНИМ событием "tool_calls" после всего текста и перед
        "meta": вызов по половине доводов исполнять нельзя, а номер, а не порядок прихода,
        держит части каждого вызова вместе."""
        direct, extra_body = self._split(params or {})
        if tools:
            direct["tools"] = tools
        response = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
            **direct,
        )
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        вызовы: dict[int, dict[str, str]] = {}
        # Поток закрываем явно: иначе HTTP-соединение остаётся подвешенным до сборки мусора,
        # и при выходе сыплются ошибки закрытия асинхронных генераторов — особенно заметно,
        # когда ответ оборван по max_tokens или запрос отменён на полуслове.
        async with response as stream:
            async for chunk in stream:
                if chunk.usage is not None:
                    usage = chunk.usage.model_dump(exclude_none=True)
                if not chunk.choices:
                    continue
                choice = chunk.choices[0]
                if choice.finish_reason:
                    finish_reason = choice.finish_reason
                delta = choice.delta
                if delta is None:
                    continue
                # DeepSeek называет поле рассуждений `reasoning_content`, Ollama — `reasoning`
                # (живой замер 2026-10-05). Читаем оба: с одним именем рассуждения местной
                # модели терялись бы молча.
                reasoning = getattr(delta, "reasoning_content", None) or getattr(delta, "reasoning", None)
                if reasoning:
                    yield StreamEvent("reasoning", reasoning)
                if delta.content:
                    yield StreamEvent("content", delta.content)
                for кусок in getattr(delta, "tool_calls", None) or ():
                    _копить_вызов(вызовы, кусок)
        if вызовы:
            yield StreamEvent("tool_calls", calls=_собрать_вызовы(вызовы))
        yield StreamEvent("meta", finish_reason=finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
        # Обход дефекта httpcore2 2.12: если ответ оборван по max_tokens, тело остаётся
        # недочитанным, и закрытие пула сыплет в stderr «generator didn't stop after
        # athrow()». Короткий оборот цикла даёт транспорту закрыться до этой проверки.
        await asyncio.sleep(0.05)


class DeepSeekClient(_ChatClient):
    def __init__(self, api_key: str) -> None:
        super().__init__(BASE_URL, api_key, transport=_transport(BASE_URL))


class OllamaUnavailable(Exception):
    """Местная служба не приняла соединение."""


def split_local_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """Те же семь ручек в виде, который понимает Ollama.

    Поле `thinking` Ollama молча пропускает, а рассуждения выключает значением
    `reasoning_effort: "none"` (живой замер 2026-10-05). Без перевода выключатель рассуждений
    у местной модели не действовал бы, и человек об этом не узнал бы."""
    direct, extra = split_params(params)
    extra.pop("thinking", None)
    if not params_mod.thinking_enabled(params):
        extra["reasoning_effort"] = "none"
    elif extra.get("reasoning_effort") == "max":
        # Градации `max` у Ollama нет: на неё служба отвечает ошибкой 500 (живой замер
        # 2026-10-05). Высшая её градация — `high`.
        extra["reasoning_effort"] = "high"
    return direct, extra


def _num_ctx(show: dict[str, Any]) -> int | None:
    """Окно из ответа `/api/show`: строка `num_ctx <число>` текстового поля `parameters`."""
    найдено = re.search(r"^num_ctx\s+(\d+)", str(show.get("parameters") or ""), re.MULTILINE)
    return int(найдено.group(1)) if найдено else None


class OllamaClient(_ChatClient):
    """Местная служба Ollama. Ключа у неё нет: в заголовок уходит строка-заглушка, и ключ
    DeepSeek сюда не попадает ни при каком пути.

    Соединение не повторяется: служба на этой же машине либо слушает, либо нет, и повторы
    DeepSeek дали бы минуты ожидания вместо сообщения, что её надо запустить.

    Обмен идёт по `/v1` — тот же формат, что у DeepSeek, и тот же разбор потока. Список моделей
    и окно `/v1` не отдаёт, за ними клиент ходит в родные `/api/tags`, `/api/show`, `/api/ps`."""

    def __init__(self, transport: httpx2.AsyncBaseTransport | None = None) -> None:
        self.root = providers.ollama_root()
        super().__init__(
            f"{self.root}/v1", "ollama", transport=transport or _transport(self.root, retries=0), max_retries=0
        )
        self._native = httpx2.AsyncClient(
            base_url=self.root,
            timeout=REQUEST_TIMEOUT,
            transport=transport or _transport(self.root, retries=0),
        )

    def _split(self, params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
        return split_local_params(params)

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        params: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        try:
            async for event in super().stream_chat(model, messages, params, tools):
                yield event
        except APITimeoutError:
            # Истёкшее ожидание — служба жива, модель думает: это не «запустите службу».
            raise
        except APIConnectionError as exc:
            raise OllamaUnavailable(
                f"Ollama не отвечает по адресу {self.root} — проверьте, что служба запущена"
            ) from exc

    async def _show(self, name: str) -> dict[str, Any]:
        ответ = await self._native.post("/api/show", json={"model": name})
        ответ.raise_for_status()
        return ответ.json()

    async def models(self) -> list[tuple[str, int | None]]:
        """Модели, умеющие отвечать текстом, и окно каждой, если его задал файл модели.

        Модели без способности `completion` (векторные представления) в список не входят:
        выбрать такую для разговора нельзя."""
        ответ = await self._native.get("/api/tags")
        ответ.raise_for_status()
        итог: list[tuple[str, int | None]] = []
        for запись in ответ.json().get("models") or []:
            имя = запись.get("name")
            if not имя:
                continue
            try:
                show = await self._show(имя)
            except httpx2.HTTPError:
                # Сбой показа одной модели не прячет остальные.
                continue
            if "completion" in (show.get("capabilities") or []):
                итог.append((имя, _num_ctx(show)))
        return sorted(итог)

    async def window(self, name: str, *, load: bool = True) -> int | None:
        """Окно модели в токенах; `None` — служба молчит или модели нет.

        `load=False` — только быстрый путь, без загрузки модели: так спрашивает запуск, которому
        нельзя ждать минуту с пустым экраном.

        Окно задаёт файл модели (`num_ctx`). Не задал — действует умолчание службы, а оно
        зависит от памяти машины и нигде не названо, пока модель не загружена. Поэтому клиент
        загружает модель пустым запросом и читает окно у загруженной: число точное, а не
        догадка. Загрузка всё равно случилась бы на первом вопросе."""
        try:
            окно = _num_ctx(await self._show(name))
            if окно or not load:
                return окно
            загрузка = await self._native.post("/api/generate", json={"model": name})
            загрузка.raise_for_status()
            ответ = await self._native.get("/api/ps")
            ответ.raise_for_status()
            # Служба имён по регистру не различает и сама дописывает метку `:latest`.
            искомые = {name.lower(), f"{name.lower()}:latest"}
            for запись in ответ.json().get("models") or []:
                if {str(запись.get("name")).lower(), str(запись.get("model")).lower()} & искомые:
                    окно = запись.get("context_length")
                    return окно if isinstance(окно, int) and окно > 0 else None
        except Exception:  # noqa: BLE001 — ответ чужой службы: любой его вид не повод ронять обмен
            return None
        return None

    async def aclose(self) -> None:
        await self._native.aclose()
        await super().aclose()


class Clients:
    """Распределитель: один вход для вызывающего кода, поставщика выбирает имя модели.

    Договор тот же, что был у клиента DeepSeek, — `stream_chat`, `list_models`, `validate`,
    `aclose`. Архивариус, распознаватель и судья названы моделью DeepSeek и потому идут в
    DeepSeek, даже когда разговор ведёт местная модель. Сжиматель, извлекатель фактов и опросы
    идут моделью разговора — значит, и местной.

    Клиент Ollama заводится при первой надобности, а не в конструкторе: негодный `OLLAMA_HOST`
    не должен ронять запуск того, кто работает с DeepSeek."""

    def __init__(
        self,
        api_key: str,
        *,
        deepseek: DeepSeekClient | None = None,
        ollama: OllamaClient | None = None,
    ) -> None:
        self._deepseek = deepseek or DeepSeekClient(api_key)
        self._местный = ollama
        self._окно_узнано: set[str] = set()

    @property
    def _ollama(self) -> OllamaClient:
        if self._местный is None:
            self._местный = OllamaClient()
        return self._местный

    async def validate(self) -> bool:
        return await self._deepseek.validate()

    async def list_models(self) -> list[str]:
        """Модели DeepSeek. Сбой пробрасывается, как прежде: его показывает `/model`."""
        return await self._deepseek.list_models()

    async def local_models(self) -> list[str]:
        """Местные модели с приставкой. Служба молчит — список пуст: это не сбой `/model`,
        местных моделей в таком положении просто нет."""
        try:
            модели = await self._ollama.models()
        except Exception:  # noqa: BLE001 — ответ чужой службы и негодный адрес: списка просто нет
            return []
        имена = []
        for имя, окно in модели:
            полное = providers.OLLAMA_PREFIX + имя
            if окно:
                tokens.remember_window(полное, окно)
                self._окно_узнано.add(полное)
            имена.append(полное)
        return имена

    async def prepare(self, model: str, *, load: bool = True) -> None:
        """Узнать окно местной модели до того, как оно понадобится счёту. Сбой не выпускается:
        окно остаётся неизвестным, а причину назовёт сам запрос к модели.

        `load=False` не загружает модель: годится, только если окно задал файл модели."""
        if not providers.is_local(model) or model in self._окно_узнано:
            return
        try:
            окно = await self._ollama.window(providers.local_name(model), load=load)
        except Exception:  # noqa: BLE001 — негодный адрес службы: окно неизвестно, запуск жив
            return
        if окно:
            tokens.remember_window(model, окно)
            self._окно_узнано.add(model)

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        params: dict[str, Any] | None = None,
        tools: list[dict] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        if providers.is_local(model):
            await self.prepare(model)
            поток = self._ollama.stream_chat(providers.local_name(model), messages, params, tools)
        else:
            поток = self._deepseek.stream_chat(model, messages, params, tools)
        async for event in поток:
            yield event

    async def aclose(self) -> None:
        await self._deepseek.aclose()
        if self._местный is not None:
            await self._местный.aclose()
