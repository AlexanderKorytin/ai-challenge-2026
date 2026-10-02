"""Тонкая обёртка над DeepSeek API (OpenAI-совместимый формат)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import urllib.request
from urllib.parse import urlsplit

import httpx2
from openai import (
    DEFAULT_CONNECTION_LIMITS,
    AsyncOpenAI,
    AuthenticationError,
    DefaultAsyncHttpxClient,
    Timeout,
)

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


def environment_proxy(url: str) -> str | None:
    """Посредник из окружения (и настроек системы) для этого адреса; `None` — идти напрямую.

    Клиент со СВОИМ транспортом посредника из окружения сам не читает: библиотека делает это
    только пока строит транспорт сама. Без этой функции запросы с ключом пошли бы мимо
    `HTTPS_PROXY` молча."""
    host = urlsplit(url).hostname or ""
    if urllib.request.proxy_bypass(host):
        return None
    proxies = urllib.request.getproxies()
    proxy = proxies.get(urlsplit(url).scheme) or proxies.get("all")
    if not proxy:
        return None
    return proxy if "://" in proxy else f"http://{proxy}"


def _transport(url: str) -> httpx2.AsyncHTTPTransport:
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
        retries=CONNECT_RETRIES,
        limits=DEFAULT_CONNECTION_LIMITS,
        proxy=environment_proxy(url),
    )


class DeepSeekClient:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(
            api_key=api_key,
            base_url=BASE_URL,
            timeout=REQUEST_TIMEOUT,
            http_client=DefaultAsyncHttpxClient(timeout=REQUEST_TIMEOUT, transport=_transport(BASE_URL)),
        )

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
        direct, extra_body = split_params(params or {})
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
                reasoning = getattr(delta, "reasoning_content", None)
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
