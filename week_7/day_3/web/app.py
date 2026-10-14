"""Учебный веб-чат поддержки `myharness` (продолжение дня 33).

Человек входит по имени без пароля, спрашивает, ассистент отвечает и ведёт его тикеты. Отвечает
тот же `Agent` из `myharness`, что в окне терминала, с профилем `поддержка` и тем же мостом MCP
`mcp_tools.Соединения`; своего круга вызовов модели здесь нет.

Пользователь сеанса задаётся файлом, а не окружением процесса: на каждого пользователя заводится
каталог `сеансы/<U-NN>/` со своим `.mcp.json`, где `SUPPORT_USER` записан значением. Мост читает
этот каталог и запускает сервер `support` под этим пользователем — два человека в браузере
получают два процесса сервера. Менять `os.environ` перед запуском сервера нельзя: два
одновременных входа подменили бы пользователя друг другу.

Пароля нет, адрес — только `127.0.0.1`. Дизайн — `docs/веб-чат-поддержки.md` в `main`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import secrets
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

ВЕБ = Path(__file__).resolve().parent
ДЕНЬ = ВЕБ.parent

# Пути процесса — до импорта myharness: каталог состояния и журнал читаются из окружения.
os.environ.setdefault("MYHARNESS_PROFILES", str(ДЕНЬ / "profiles"))
os.environ.setdefault("MYHARNESS_STATE_DIR", str(ВЕБ / "state"))
os.environ.setdefault("MYHARNESS_JOURNAL", str(ВЕБ / "журнал.jsonl"))
os.environ.setdefault("SUPPORT_DATA", str(ВЕБ / "данные.json"))

# `store` — модуль сервера `support`: вход и заведение пользователя пишут тот же файл данных под той
# же блокировкой, что и инструменты записи. Пакетом сервер не собирается, поэтому — путём.
sys.path.insert(0, str(ДЕНЬ / "support"))
import store  # noqa: E402

from myharness import api, config, memory, profiles  # noqa: E402
from myharness.agent import Agent  # noqa: E402
from myharness.mcp_tools import Соединения  # noqa: E402

ПРОФИЛЬ = "поддержка"
МОДЕЛИ = ("deepseek-v4-flash", "ollama/qwen3.5:9b-q4_K_M-32k")
КУКА = "sid"
ШАБЛОН_MCP = ДЕНЬ / "test" / ".mcp.json"
ШАБЛОН_РАЗРЕШЕНИЙ = ДЕНЬ / "test" / ".claude" / "settings.json"
ЗАПИСЬ = ("mcp__support__create_ticket", "mcp__support__add_message", "mcp__support__set_status")


def каталог_сеансов() -> Path:
    return Path(os.environ.get("SUPPORT_WEB_SESSIONS") or ВЕБ / "сеансы")


@dataclass
class Сеанс:
    пользователь: dict[str, Any]
    каталог: Path
    агент: Agent
    соединения: Соединения
    замок: asyncio.Lock = field(default_factory=asyncio.Lock)


class Приложение:
    """Сеансы в памяти процесса и один клиент моделей на всё приложение.

    Сеанс закрывается только под его замком обмена: замок держит обмен целиком, поэтому выход
    или повторный вход ждёт конца ответа, а не рвёт его посреди записи тикета. Так же ни один
    обмен не откроет новых процессов MCP в уже закрытых соединениях."""

    def __init__(self) -> None:
        self.сеансы: dict[str, Сеанс] = {}
        self._клиент: api.Clients | None = None
        # Вход одного пользователя — по одному: два одновременных входа иначе оба прошли бы мимо
        # замены и оставили бы два агента на один файл разговора. Замок свой у каждого
        # пользователя: вход ждёт конца ответа прежнего сеанса, и общий замок задержал бы всех.
        self._входы: dict[str, asyncio.Lock] = {}
        # Задачи обменов: без ссылки задачу может собрать сборщик мусора, а остановка ждёт их.
        self.задачи: set[asyncio.Task] = set()

    def клиент(self) -> api.Clients:
        if self._клиент is None:
            self._клиент = api.Clients(config.load().api_key)
        return self._клиент

    async def закрыть(self) -> None:
        # Сначала дождаться идущих обменов: соединения и клиент нужны им до конца.
        await asyncio.gather(*self.задачи, return_exceptions=True)
        сеансы = list(self.сеансы.values())
        self.сеансы.clear()
        await asyncio.gather(*(закрыть_сеанс(с) for с in сеансы), return_exceptions=True)
        if self._клиент is not None:
            await self._клиент.aclose()

    async def выйти(self, sid: str) -> None:
        сеанс = self.сеансы.pop(sid, None)
        if сеанс is not None:
            await закрыть_сеанс(сеанс)

    async def войти(self, имя: str) -> tuple[str, Сеанс, list[tuple[str, str]]]:
        пользователь = await asyncio.to_thread(store.войти, имя)
        async with self._входы.setdefault(пользователь["user_id"], asyncio.Lock()):
            # Прежний сеанс того же пользователя заменяется: два агента писали бы в один разговор.
            # Его обмен доживает до конца — пара ляжет в файл раньше, чем новый агент его прочтёт.
            прежние = [
                sid for sid, с in self.сеансы.items()
                if с.пользователь["user_id"] == пользователь["user_id"]
            ]
            for sid in прежние:
                await self.выйти(sid)
            каталог = подготовить_каталог(пользователь["user_id"])
            соединения = Соединения()
            агент, история = поднять_агента(каталог, соединения, новый=False)
            сеанс = Сеанс(пользователь, каталог, агент, соединения)
            sid = secrets.token_urlsafe(24)
            self.сеансы[sid] = сеанс
            return sid, сеанс, история

    def сеанс(self, request: Request) -> Сеанс | None:
        return self.сеансы.get(request.cookies.get(КУКА, ""))


async def закрыть_сеанс(сеанс: Сеанс) -> None:
    """Закрыть процессы серверов MCP сеанса — после конца идущего обмена."""
    async with сеанс.замок:
        await сеанс.соединения.закрыть()


def подготовить_каталог(user_id: str) -> Path:
    """Каталог сеанса пользователя: `.mcp.json` и разрешения пишутся при каждом входе заново."""
    каталог = каталог_сеансов() / user_id
    (каталог / ".claude").mkdir(parents=True, exist_ok=True)
    mcp = json.loads(ШАБЛОН_MCP.read_text("utf-8"))
    mcp["mcpServers"]["support"]["env"] = {
        "SUPPORT_USER": user_id,
        "SUPPORT_DATA": str(store.путь()),
    }
    (каталог / ".mcp.json").write_text(json.dumps(mcp, ensure_ascii=False, indent=1), "utf-8")
    разрешения = json.loads(ШАБЛОН_РАЗРЕШЕНИЙ.read_text("utf-8"))
    разрешения["permissions"]["allow"] = [*разрешения["permissions"]["allow"], *ЗАПИСЬ]
    (каталог / ".claude" / "settings.json").write_text(
        json.dumps(разрешения, ensure_ascii=False, indent=1), "utf-8"
    )
    return каталог


def поднять_агента(
    каталог: Path, соединения: Соединения, *, новый: bool
) -> tuple[Agent, list[tuple[str, str]]]:
    """Агент профиля `поддержка` и прежний разговор пользователя (пусто при `новый`).

    Инструменты — серверы MCP из `.mcp.json` каталога сеанса, через соединения сеанса."""
    профиль, предупреждения = profiles.load(ПРОФИЛЬ)
    if профиль.source is None:
        raise RuntimeError(f"профиль {ПРОФИЛЬ} не загружен: {'; '.join(предупреждения)}")

    async def набор():
        return await соединения.набор(каталог)

    агент = Agent(ПРОФИЛЬ, профиль, инструменты=набор)
    путь = None if новый else memory.latest_session(каталог, ПРОФИЛЬ)
    пары: list[tuple[str, str]] = []
    if путь is not None:
        прочитано = memory.read_session(путь, window=0, system_fp=memory.fingerprint(профиль.system))
        пары = прочитано.pairs
        агент.restore(пары, прочитано.приложения)
    else:
        путь = memory.new_session(каталог, ПРОФИЛЬ)
    хранилище = memory.SessionStore(путь)
    if not путь.exists():
        # Пустой файл сразу: иначе «Новый разговор» без вопросов не пережил бы повторный вход —
        # `latest_session` нашёл бы прежний файл. Так же делает окно (`conversation.py`).
        if ошибка := хранилище.touch():
            raise RuntimeError(ошибка)
    агент.set_store(хранилище)
    return агент, пары


ПРИЛОЖЕНИЕ = Приложение()


def _без_входа() -> JSONResponse:
    return JSONResponse({"error": "вход не выполнен"}, status_code=401)


async def главная(request: Request) -> Response:
    return FileResponse(ВЕБ / "index.html")


async def _тело(request: Request) -> dict[str, Any]:
    """Тело запроса — объект JSON; иначе `ValueError`."""
    тело = await request.json()
    if not isinstance(тело, dict):
        raise ValueError("тело — не объект JSON")
    return тело


async def вход(request: Request) -> Response:
    try:
        имя = str((await _тело(request)).get("name", ""))
        sid, сеанс, история = await ПРИЛОЖЕНИЕ.войти(имя)
    except ValueError:
        return JSONResponse({"error": "введите имя"}, status_code=400)
    ответ = JSONResponse(
        {"user": сеанс.пользователь, "history": история, "models": list(МОДЕЛИ)}
    )
    ответ.set_cookie(КУКА, sid, httponly=True, samesite="strict")
    return ответ


async def выход(request: Request) -> Response:
    await ПРИЛОЖЕНИЕ.выйти(request.cookies.get(КУКА, ""))
    ответ = JSONResponse({"ok": True})
    ответ.delete_cookie(КУКА)
    return ответ


async def новый_разговор(request: Request) -> Response:
    сеанс = ПРИЛОЖЕНИЕ.сеанс(request)
    if сеанс is None:
        return _без_входа()
    if сеанс.замок.locked():
        return JSONResponse({"error": "идёт ответ"}, status_code=409)
    сеанс.агент, _ = поднять_агента(сеанс.каталог, сеанс.соединения, новый=True)
    return JSONResponse({"ok": True})


async def тикеты(request: Request) -> Response:
    сеанс = ПРИЛОЖЕНИЕ.сеанс(request)
    if сеанс is None:
        return _без_входа()
    данные = await asyncio.to_thread(store.прочитать)
    свои = [т for т in данные["tickets"] if т["user_id"] == сеанс.пользователь["user_id"]]
    свои.sort(key=lambda т: т["created"], reverse=True)
    return JSONResponse({"tickets": свои})


def _событие(событие: api.StreamEvent) -> list[dict[str, Any]]:
    """Событие потока агента → строки NDJSON для страницы."""
    if событие.kind == "content" and событие.text:
        return [{"kind": "content", "text": событие.text}]
    if событие.kind == "tool_calls":
        return [
            {
                "kind": "tool",
                "name": (в.get("function") or {}).get("name", ""),
                "args": (в.get("function") or {}).get("arguments", ""),
            }
            for в in событие.calls
        ]
    if событие.kind == "tool_result":
        имя = ((событие.calls or [{}])[0].get("function") or {}).get("name", "")
        return [{"kind": "tool_result", "name": имя, "ok": not событие.text.startswith("ошибка инструмента")}]
    return []


async def вопрос(request: Request) -> Response:
    сеанс = ПРИЛОЖЕНИЕ.сеанс(request)
    if сеанс is None:
        return _без_входа()
    try:
        тело = await _тело(request)
    except ValueError:
        return JSONResponse({"error": "тело — не объект JSON"}, status_code=400)
    текст = str(тело.get("text", "")).strip()
    модель = str(тело.get("model") or МОДЕЛИ[0])
    if not текст:
        return JSONResponse({"error": "пустой вопрос"}, status_code=400)
    if модель not in МОДЕЛИ:
        return JSONResponse({"error": f"модель {модель} не из списка"}, status_code=400)
    if сеанс.замок.locked():
        return JSONResponse({"error": "идёт ответ"}, status_code=409)

    очередь: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def обмен() -> None:
        async with сеанс.замок:
            try:
                if сеанс not in ПРИЛОЖЕНИЕ.сеансы.values():
                    # Выход успел раньше первого шага обмена: соединения сеанса уже закрыты, и
                    # процессы MCP, открытые сейчас, остались бы без хозяина.
                    raise RuntimeError("сеанс закрыт, войдите снова")
                клиент = ПРИЛОЖЕНИЕ.клиент()
                await клиент.prepare(модель)
                ход = await сеанс.агент.exchange(
                    клиент,
                    модель,
                    текст,
                    on_event=lambda с: [очередь.put_nowait(с2) for с2 in _событие(с)],
                    agent=f"веб {сеанс.пользователь['user_id']}",
                )
                очередь.put_nowait({"kind": "done", "status": ход.status, "error": ход.error})
            except Exception as exc:  # noqa: BLE001 — причина уходит на страницу, а не в никуда
                очередь.put_nowait({"kind": "done", "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                очередь.put_nowait(None)

    # Обмен живёт отдельной задачей: закрытая вкладка, выход и повторный вход не обрывают его
    # посреди записи тикета — они ждут замка сеанса.
    задача = asyncio.create_task(обмен())
    ПРИЛОЖЕНИЕ.задачи.add(задача)
    задача.add_done_callback(ПРИЛОЖЕНИЕ.задачи.discard)

    async def поток():
        while (строка := await очередь.get()) is not None:
            yield json.dumps(строка, ensure_ascii=False) + "\n"

    return StreamingResponse(поток(), media_type="application/x-ndjson")


@contextlib.asynccontextmanager
async def срок_жизни(app):
    данные = store.путь()
    if not данные.exists():
        # Рабочая копия: записи чата не трогают данные сценария дня в git.
        данные.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(store.ДАННЫЕ_ПО_УМОЛЧАНИЮ, данные)
    yield
    await ПРИЛОЖЕНИЕ.закрыть()


app = Starlette(
    routes=[
        Route("/", главная),
        Route("/api/login", вход, methods=["POST"]),
        Route("/api/logout", выход, methods=["POST"]),
        Route("/api/new", новый_разговор, methods=["POST"]),
        Route("/api/tickets", тикеты),
        Route("/api/ask", вопрос, methods=["POST"]),
    ],
    lifespan=срок_жизни,
)
