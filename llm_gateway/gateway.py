"""Шлюз перед службой Ollama: вход из сети для местной модели (день 30, docs/местная-служба.md).

Ollama токенов не проверяет и отдаёт любому удаление и загрузку моделей, поэтому наружу её порт
не выходит. Шлюз стоит между Caddy и службой и на каждом запросе делает проверки по порядку:

    токен → путь → модель → известные поля → частота → вид полей → окно → передача службе → журнал

Отказы — в виде OpenAI и Anthropic: 401, 404, 429 с `Retry-After`, 400 с кодом
`context_length_exceeded`, 503 при молчащей службе. Клиент, написанный под них, поймёт отказ.

Своих пределов ожидания, своей очереди и повторов у шлюза нет: очередь ведёт сама Ollama, предел
ожидания держит клиент. Текст запросов и ответов в журнал не пишется.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import hmac
import json
import math
import re
import sys
import time
from collections.abc import AsyncIterator, Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from myharness import api, providers, tokens
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse
from starlette.routing import Route

TOKEN_PREFIX = "LLM_TOKEN_"
CHAT = ("POST", "/v1/chat/completions")
SHOW = ("POST", "/api/show")
# Всё, чего здесь нет, снаружи не существует: `/api/pull`, `/api/delete`, `/api/create`,
# `/api/generate` и прочее. Список разрешающий — новый путь службы сам наружу не выйдет.
ALLOWED = frozenset({CHAT, SHOW, ("GET", "/v1/models"), ("GET", "/api/tags"), ("GET", "/api/ps")})
# Ответы со списком моделей: путь → (ключ списка, ключ имени). Шлюз сужает их до разрешённых.
LISTS = {"/v1/models": ("data", "id"), "/api/tags": ("models", "name"), "/api/ps": ("models", "name")}
# Поля чата, которые шлюз знает и передаёт службе. Запрос с любым другим ключом — отказ 400.
# Служба разбирает JSON без учёта регистра ключей (Go, `encoding/json`), а шлюз — с учётом:
# ключ `Model` рядом с `model` шлюз бы не заметил, а служба взяла бы его. Поэтому службе уходят
# не байты клиента, а тело, собранное заново из проверенных полей. Полей `options`,
# `keep_alive` и `num_ctx` здесь нет намеренно: ими клиент менял бы окно и время жизни модели.
CHAT_FIELDS = frozenset({
    "model", "messages", "stream", "stream_options", "max_tokens", "max_completion_tokens",
    "temperature", "top_p", "stop", "seed", "frequency_penalty", "presence_penalty",
    "response_format", "tools", "tool_choice", "reasoning_effort", "user", "parallel_tool_calls",
})  # fmt: skip
MESSAGE_FIELDS = frozenset(
    {"role", "content", "name", "tool_calls", "tool_call_id", "reasoning_content", "reasoning", "refusal"}
)
# Заголовки запроса, которые уходят службе. Токен клиента (`Authorization`) в их число не входит.
FORWARD_HEADERS = ("content-type", "accept")
# Хвост потока, в котором ищется поле `usage`: оно приходит последним куском перед `[DONE]`.
USAGE_TAIL = 4096
USAGE = re.compile(rb'"usage"\s*:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})')


@dataclass(frozen=True)
class Settings:
    upstream: str
    models: frozenset[str]
    rate_per_minute: int
    tokens: Mapping[str, str]  # токен → имя клиента
    log_path: Path

    @classmethod
    def from_env(cls, environ: Mapping[str, str]) -> Settings:
        """Настройки из окружения. Адрес службы — `OLLAMA_HOST`, как у самой Ollama и у
        `myharness`: своей переменной не заводим. Без токенов и без списка моделей шлюз не
        стартует — открытый вход хуже неработающего."""
        клиенты = {
            значение.strip(): имя.removeprefix(TOKEN_PREFIX)
            for имя, значение in environ.items()
            if имя.startswith(TOKEN_PREFIX) and значение.strip()
        }
        if not клиенты:
            raise SystemExit(f"шлюз не стартует: нет ни одного токена {TOKEN_PREFIX}<имя>")
        модели = frozenset(имя.strip() for имя in environ.get("LLM_MODELS", "").split(",") if имя.strip())
        if not модели:
            raise SystemExit("шлюз не стартует: пуст список моделей LLM_MODELS")
        try:
            частота = int(environ.get("LLM_RATE_PER_MINUTE", ""))
        except ValueError:
            частота = 0
        if частота <= 0:
            raise SystemExit("шлюз не стартует: LLM_RATE_PER_MINUTE — целое число больше нуля")
        журнал = environ.get("LLM_GATEWAY_LOG", "").strip()
        if not журнал:
            raise SystemExit("шлюз не стартует: не назван файл журнала LLM_GATEWAY_LOG")
        return cls(providers.ollama_root(), модели, частота, клиенты, Path(журнал))


class Buckets:
    """Ведро жетонов на клиента: вместимость — запросов в минуту, пополнение равномерное.

    Живёт в памяти процесса: после перезапуска вёдра полны."""

    def __init__(self, per_minute: int, clock: Callable[[], float]) -> None:
        self._вместимость = float(per_minute)
        self._в_секунду = per_minute / 60
        self._часы = clock
        self._вёдра: dict[str, tuple[float, float]] = {}  # клиент → (жетоны, время счёта)

    def take(self, client: str) -> int:
        """Взять жетон. Ноль — взят; иначе число секунд до ближайшего жетона."""
        сейчас = self._часы()
        жетоны, когда = self._вёдра.get(client, (self._вместимость, сейчас))
        жетоны = min(self._вместимость, жетоны + (сейчас - когда) * self._в_секунду)
        if жетоны >= 1:
            self._вёдра[client] = (жетоны - 1, сейчас)
            return 0
        self._вёдра[client] = (жетоны, сейчас)
        return max(1, math.ceil((1 - жетоны) / self._в_секунду))


def _отказ(код: int, сообщение: str, вид: str, признак: str, **заголовки: str) -> JSONResponse:
    return JSONResponse(
        {"error": {"message": сообщение, "type": вид, "code": признак}}, status_code=код, headers=заголовки
    )


class Gateway:
    def __init__(
        self,
        settings: Settings,
        transport: httpx2.AsyncBaseTransport | None,
        clock: Callable[[], float],
    ) -> None:
        self.settings = settings
        # Предела ожидания нет намеренно: модель думает минуты, и ждать решает клиент шлюза.
        # Посредника из окружения клиент не читает: служба стоит на петле.
        self._служба = httpx2.AsyncClient(
            base_url=settings.upstream, timeout=None, transport=transport, trust_env=False
        )
        # Окно и словарь модели спрашивает тот же клиент, что у `myharness`: счёт совпадает.
        self._ollama = api.OllamaClient(transport)
        self._вёдра = Buckets(settings.rate_per_minute, clock)
        self._окна: dict[str, int] = {}
        self._подготовка = asyncio.Lock()
        self._счёт = asyncio.Lock()

    async def aclose(self) -> None:
        await self._служба.aclose()
        await self._ollama.aclose()

    def _клиент(self, request: Request) -> str | None:
        """Имя клиента по токену. Сравнение — за постоянное время и со всеми токенами подряд:
        по времени ответа токен не подобрать."""
        схема, _, присланный = request.headers.get("authorization", "").partition(" ")
        if схема.lower() != "bearer" or not присланный.strip():
            return None
        # Заголовки Starlette читает как latin-1: той же кодировкой возвращаем исходные байты.
        присланный_байтами = присланный.strip().encode("latin-1")
        найден = None
        for токен, имя in self.settings.tokens.items():
            if hmac.compare_digest(токен.encode(), присланный_байтами):
                найден = имя
        return найден

    async def _окно(self, name: str) -> int | None:
        """Окно модели; попутно запоминается её словарь. `None` — служба окна или словаря не
        дала: отказывать по догадке шлюз не должен, проверка окна пропускается."""
        if name in self._окна:
            return self._окна[name]
        async with self._подготовка:
            if name in self._окна:
                return self._окна[name]
            try:
                окно = await self._ollama.window(name)
                сведения = await self._ollama.vocabulary(name)
            except Exception:  # noqa: BLE001 — служба молчит: сам запрос получит 503 ниже
                return None
            словарь = await asyncio.to_thread(tokens.build_vocabulary, сведения) if сведения else None
            if not окно or словарь is None:
                return None
            tokens.remember_vocabulary(providers.OLLAMA_PREFIX + name, словарь)
            self._окна[name] = окно
            return окно

    def _журнал(self, **запись: Any) -> None:
        строка = {"время": datetime.now(timezone.utc).isoformat(timespec="seconds"), **запись}
        try:
            with self.settings.log_path.open("a", encoding="utf-8") as файл:
                файл.write(json.dumps(строка, ensure_ascii=False) + "\n")
        except OSError as сбой:
            # Сбой журнала не повод отказать клиенту; причина уходит в журнал systemd.
            print(f"журнал шлюза не записан: {сбой}", file=sys.stderr)

    async def handle(self, request: Request) -> Response:
        try:
            return await self._handle(request)
        except Exception as сбой:  # noqa: BLE001 — сбой шлюза: клиенту 500 без подробностей
            # Строка журнала есть и у сбоя; в журнал systemd идёт только вид исключения —
            # его текст может нести кусок запроса.
            print(f"сбой шлюза: {type(сбой).__name__}", file=sys.stderr)
            self._журнал(клиент=None, метод=request.method, путь=request.url.path, модель=None, код=500)
            return _отказ(500, "сбой шлюза", "api_error", "gateway_error")

    async def _handle(self, request: Request) -> Response:
        начало = time.monotonic()
        путь = request.url.path
        запись: dict[str, Any] = {"клиент": None, "метод": request.method, "путь": путь, "модель": None}

        def итог(ответ: Response, **поля: Any) -> Response:
            self._журнал(**запись, код=ответ.status_code, мс=round((time.monotonic() - начало) * 1000), **поля)
            return ответ

        клиент = self._клиент(request)
        if клиент is None:
            return итог(_отказ(401, "нет токена либо токен неверен", "authentication_error", "invalid_api_key"))
        запись["клиент"] = клиент
        ход = (request.method, путь)
        if ход not in ALLOWED:
            return итог(_отказ(404, "такого пути у службы нет", "not_found_error", "not_found"))

        сырое = await request.body()
        отправить = сырое
        if ход in (CHAT, SHOW):
            try:
                тело = json.loads(сырое)
            except ValueError:
                тело = None
            if not isinstance(тело, dict):
                return итог(_отказ(400, "тело запроса — не объект JSON", "invalid_request_error", "invalid_json"))
            имя = тело.get("model")
            if имя is None and ход == SHOW:
                имя = тело.get("name")
            if not isinstance(имя, str) or имя not in self.settings.models:
                return итог(_отказ(404, "такой модели у службы нет", "not_found_error", "model_not_found"))
            # В журнал идёт только имя из списка: чужой текст из поля `model` туда не попадает.
            запись["модель"] = имя
            if ход == SHOW:
                тело = {"model": имя, "verbose": тело.get("verbose") is True}
            else:
                лишние = sorted(set(тело) - CHAT_FIELDS)
                if лишние:
                    return итог(
                        _отказ(
                            400,
                            f"шлюз не знает полей запроса: {', '.join(лишние)[:200]}",
                            "invalid_request_error",
                            "unknown_field",
                        )
                    )
            # Службе уходит то самое тело, которое проверено, а не байты клиента.
            # Запись ASCII: одиночный суррогат (`"\ud800"`) в UTF-8 не кодируется вовсе.
            отправить = json.dumps(тело).encode()

        if ход == CHAT:
            ждать = self._вёдра.take(клиент)
            if ждать:
                return итог(
                    _отказ(
                        429,
                        f"запросов больше {self.settings.rate_per_minute} в минуту, повторите через {ждать} с",
                        "rate_limit_error",
                        "rate_limit_exceeded",
                        **{"Retry-After": str(ждать)},
                    )
                )
            # Вид полей проверяется всегда, а не только при известном окне: реплику
            # неожиданного вида служба не получает.
            # Счёт идёт в потоке и по одному: запомненные подсчёты `myharness` общие и без замка.
            async with self._счёт:
                вес = await asyncio.to_thread(self._вес, тело, имя)
            if вес is None:
                return итог(_отказ(400, "поля запроса не того вида", "invalid_request_error", "invalid_request"))
            окно = await self._окно(имя)
            запись["окно"] = окно
            if окно:
                вход, выход = вес
                запись["вес"] = вход
                if вход + выход > окно:
                    return итог(
                        _отказ(
                            400,
                            f"запрос не помещается в окно модели: вход {вход} + выход {выход} > {окно} токенов",
                            "invalid_request_error",
                            "context_length_exceeded",
                        )
                    )

        запрос = self._служба.build_request(
            request.method,
            путь,
            content=отправить,
            headers={имя: request.headers[имя] for имя in FORWARD_HEADERS if имя in request.headers},
        )
        try:
            ответ = await self._служба.send(запрос, stream=True)
        except httpx2.TransportError:
            return итог(_отказ(503, "узел модели недоступен", "api_error", "upstream_unavailable"))

        заголовки = {имя: ответ.headers[имя] for имя in ("content-type",) if имя in ответ.headers}
        if путь in LISTS:
            try:
                содержимое = await ответ.aread()
            except httpx2.TransportError:
                return итог(_отказ(503, "узел модели недоступен", "api_error", "upstream_unavailable"))
            finally:
                await ответ.aclose()
            return итог(Response(self._сузить(путь, содержимое, ответ.status_code), ответ.status_code, заголовки))

        хвост = bytearray()
        дочитан = False
        закрыт = False

        async def закрыть() -> None:
            """Закрыть соединение со службой и записать строку журнала — ровно один раз и при
            любом исходе: поток дочитан, клиент бросил чтение, служба оборвала ответ."""
            nonlocal закрыт
            if закрыт:
                return
            закрыт = True
            try:
                await ответ.aclose()
            finally:
                итог(Response(status_code=ответ.status_code), оборван=not дочитан, **_расход(bytes(хвост)))

        async def поток() -> AsyncIterator[bytes]:
            nonlocal дочитан
            try:
                async for кусок in ответ.aiter_raw():
                    хвост.extend(кусок)
                    del хвост[:-USAGE_TAIL]
                    yield кусок
                дочитан = True
            except httpx2.TransportError:
                # Служба оборвала ответ посреди потока: клиент получает оборванный поток,
                # журнал — признак `оборван`.
                pass

        # Сжатие службы уходит клиенту как есть: шлюз кусков не разворачивает и не копит.
        if "content-encoding" in ответ.headers:
            заголовки["content-encoding"] = ответ.headers["content-encoding"]
        return _Передача(поток(), ответ.status_code, заголовки, закрыть=закрыть)

    def _вес(self, тело: dict[str, Any], name: str) -> tuple[int, int] | None:
        """Вход и запрошенный выход в токенах; `None` — поля запроса не того вида.

        Вид проверяется строго: всё, что шлюз не умеет взвесить, служба не получает. Иначе
        текст в непосчитанном поле прошёл бы проверку окна, и служба молча обрезала бы запрос."""
        реплики = тело.get("messages")
        инструменты = тело.get("tools") or []
        if not isinstance(реплики, list) or not isinstance(инструменты, list):
            return None
        выход = 0
        for поле in ("max_tokens", "max_completion_tokens"):
            значение = тело.get(поле)
            if значение is None:
                continue
            # `True` — тоже `int`: без отдельной ветки оно прошло бы как единица.
            if isinstance(значение, bool) or not isinstance(значение, int) or значение < 0:
                return None
            выход = max(выход, значение)
        модель = providers.OLLAMA_PREFIX + name
        прочее = []
        для_счёта = []
        for реплика in реплики:
            if not isinstance(реплика, dict) or set(реплика) - MESSAGE_FIELDS:
                return None
            содержимое = реплика.get("content")
            if isinstance(содержимое, list):
                # Текст списком частей шлют клиенты OpenAI. Счёт `myharness` весит такой список
                # нулём, поэтому части склеиваются для счёта; не текст (картинка) — отказ.
                if not all(
                    isinstance(часть, dict) and set(часть) == {"type", "text"}
                    and часть["type"] == "text" and isinstance(часть["text"], str)
                    for часть in содержимое
                ):  # fmt: skip
                    return None
                # Обёртка каждой части весит как её JSON без текста.
                содержимое = "".join(часть["text"] for часть in содержимое) + '{"type":"text","text":""},' * len(содержимое)
            elif not isinstance(содержимое, (str, type(None))):
                return None
            if not isinstance(реплика.get("reasoning_content"), (str, type(None))):
                return None
            для_счёта.append({**реплика, "content": содержимое})
            прочее.append(
                {поле: реплика[поле] for поле in ("role", "name", "tool_call_id", "reasoning", "refusal") if поле in реплика}
            )
        try:
            вход = (
                tokens.count_messages(для_счёта, модель)
                + tokens.count_tools(инструменты, модель)
                # Поля, которых счёт реплики не знает, весят как их JSON.
                + tokens.count_text(json.dumps(прочее, ensure_ascii=False), модель)
                + sum(
                    tokens.count_text(json.dumps(тело[поле], ensure_ascii=False), модель)
                    for поле in ("response_format", "tool_choice", "stop")
                    if тело.get(поле) is not None
                )
            )
        except Exception:  # noqa: BLE001 — поле неожиданного вида: службе его не отдаём
            return None
        return вход, выход

    def _сузить(self, путь: str, содержимое: bytes, код: int) -> bytes:
        """Список моделей без тех, что не разрешены: клиенту незачем знать, что ещё стоит у службы."""
        if код != 200:
            return содержимое
        список, поле = LISTS[путь]
        try:
            ответ = json.loads(содержимое)
            ответ[список] = [запись for запись in ответ.get(список) or [] if запись.get(поле) in self.settings.models]
        except (ValueError, AttributeError, TypeError):
            # Ответ незнакомого вида наружу не идёт: в нём могут быть чужие имена.
            return b"{}"
        return json.dumps(ответ, ensure_ascii=False).encode()


class _Передача(StreamingResponse):
    """Потоковый ответ, который закрывает соединение со службой при любом исходе.

    Одного `finally` в генераторе мало: клиент, бросивший чтение до первого куска, отменяет
    задачу раньше, чем генератор начат, и его `finally` не выполняется вовсе."""

    def __init__(self, *доводы: Any, закрыть: Callable[[], Any], **именованные: Any) -> None:
        super().__init__(*доводы, **именованные)
        self._закрыть = закрыть

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        try:
            await super().__call__(scope, receive, send)
        finally:
            # Отмена задачи не должна оборвать само закрытие.
            await asyncio.shield(self._закрыть())


def _расход(хвост: bytes) -> dict[str, Any]:
    """Токены входа и выхода из поля `usage` ответа службы; поля нет — `None`."""
    найдено = USAGE.findall(хвост)
    try:
        расход = json.loads(найдено[-1]) if найдено else {}
    except ValueError:
        расход = {}
    return {"вход": расход.get("prompt_tokens"), "выход": расход.get("completion_tokens")}


def build_app(
    settings: Settings,
    transport: httpx2.AsyncBaseTransport | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> Starlette:
    шлюз = Gateway(settings, transport, clock)

    @contextlib.asynccontextmanager
    async def жизнь(_: Starlette) -> AsyncIterator[None]:
        yield
        await шлюз.aclose()

    return Starlette(
        routes=[Route("/{path:path}", шлюз.handle, methods=["GET", "POST", "PUT", "DELETE", "PATCH", "HEAD"])],
        lifespan=жизнь,
    )


def main() -> None:
    import os

    доводы = argparse.ArgumentParser(description="Шлюз перед службой Ollama")
    доводы.add_argument("--host", default="127.0.0.1")
    доводы.add_argument("--port", type=int, required=True)
    выбор = доводы.parse_args()
    # Журнал доступа uvicorn выключен: журнал запросов ведёт сам шлюз, второй рядом не нужен.
    uvicorn.run(build_app(Settings.from_env(os.environ)), host=выбор.host, port=выбор.port, access_log=False)


if __name__ == "__main__":
    main()
