"""Веб игрового агента: разговор с советником, предложения, желаемое, календарь, лента.

Один человек, `127.0.0.1:8791`, входа нет. Каталог агента — `GAMES_DATA_DIR`: там хранилище,
`.mcp.json` и разрешения; карточка и разговор `myharness` привязаны к нему же.

Опрос идёт, пока в карточке нет «Критериев» (`taste.опрос_пройден`): страница сама решает, что
показать, по `/api/state`. Перезапуск службы опрос не начинает — разговор и карточка на диске.

Оценка — слово человека. Плюс — игра в желаемое. Минус — запрет строкой в «Ограничениях»
карточки с источником «человек» (адрес записи называет кнопка), затем DeepSeek пересматривает
вкус. Писать желаемое, оценки и календарь модели не дано.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response, StreamingResponse
from starlette.routing import Route

import games_db
import steam_store as ss
from myharness import api, config, project_card
from myharness.mcp_tools import Соединения

import advisor
import jobs
import taste

ЗДЕСЬ = Path(__file__).resolve().parent
АДРЕС = "127.0.0.1"
ПОРТ = int(os.environ.get("GAMES_WEB_PORT") or 8791)
ПРИВЕТСТВИЕ_ПЕРЕСБОРКИ = "Хочу пересобрать вкус заново: спроси меня о любимых играх с начала."


def каталог() -> Path:
    return games_db.путь().parent


class Приложение:
    """Один агент разговора на процесс. Обмен идёт под замком: два ответа разом писали бы в
    один файл разговора. Задания из кнопок — тоже по одному каждого рода."""

    def __init__(self) -> None:
        self._клиент: api.Clients | None = None
        self.соединения = Соединения()
        self.агент = None
        self.пары: list[tuple[str, str]] = []
        self.замок = asyncio.Lock()
        self.метки: list[ss.Метка] = []
        self.задания: dict[str, asyncio.Task] = {}
        self.задачи: set[asyncio.Task] = set()

    def клиент(self) -> api.Clients:
        if self._клиент is None:
            self._клиент = api.Clients(config.load().api_key)
        return self._клиент

    async def метки_магазина(self) -> list[ss.Метка]:
        """Метки берутся при первой надобности и держатся в памяти процесса. Магазин не
        ответил — исключение наверх: без меток `save_taste` отверг бы любой вкус."""
        if not self.метки:
            async with ss.клиент() as к:
                self.метки = await ss.метки(к)
        return self.метки

    async def поднять(self, *, новый: bool = False) -> None:
        if self.агент is not None:
            await self.соединения.закрыть()
            self.соединения = Соединения()
        self.агент, self.пары = advisor.поднять(
            каталог(), self.соединения, await self.метки_магазина(), новый=новый
        )

    async def закрыть(self) -> None:
        for задача in self.задания.values():
            задача.cancel()
        await asyncio.gather(*self.задачи, *self.задания.values(), return_exceptions=True)
        await self.соединения.закрыть()
        if self._клиент is not None:
            await self._клиент.aclose()


ПРИЛОЖЕНИЕ = Приложение()


def _ошибка(текст: str, код: int = 400) -> JSONResponse:
    return JSONResponse({"error": текст}, status_code=код)


async def _тело(request: Request) -> dict[str, Any]:
    try:
        тело = await request.json()
    except ValueError:
        raise ValueError("тело — не JSON") from None
    if not isinstance(тело, dict):
        raise ValueError("тело — не объект JSON")
    return тело


async def главная(request: Request) -> Response:
    return FileResponse(ЗДЕСЬ / "index.html")


def _вкус() -> dict[str, list[str]]:
    карточка, _ = project_card.load(каталог())
    if карточка is None:
        return {}
    return {имя: карточка.пункты(имя) for имя in taste.РАЗДЕЛЫ}


def _запреты() -> list[dict[str, Any]]:
    карточка, _ = project_card.load(каталог())
    if карточка is None:
        return []
    итог = []
    for строка in карточка.пункты(project_card.РАЗДЕЛ_ОГРАНИЧЕНИЙ):
        номер = taste.номер_запрета(строка)
        итог.append({"строка": строка, "номер": номер})
    return итог


async def состояние(request: Request) -> Response:
    с = games_db.открыть()
    try:
        данные = {
            "опрос_пройден": taste.опрос_пройден(каталог()),
            "вкус": _вкус(),
            "запреты": _запреты(),
            "предложения": games_db.предложения(с, только_без_оценки=True),
            "желаемое": games_db.желаемое(с),
            "календарь": games_db.календарь(с),
            "лента": games_db.лента(с),
            "задания_идут": sorted(р for р, з in ПРИЛОЖЕНИЕ.задания.items() if not з.done()),
            "разговор": [{"вопрос": в, "ответ": о} for в, о in ПРИЛОЖЕНИЕ.пары],
        }
    finally:
        с.close()
    return JSONResponse(данные)


def _событие(событие: api.StreamEvent) -> list[dict[str, Any]]:
    """Событие потока агента → строки NDJSON для страницы (как в веб-чате дня 33)."""
    if событие.kind == "content" and событие.text:
        return [{"kind": "content", "text": событие.text}]
    if событие.kind == "tool_calls":
        return [
            {"kind": "tool", "name": (в.get("function") or {}).get("name", "")}
            for в in событие.calls
        ]
    if событие.kind == "tool_result":
        имя = ((событие.calls or [{}])[0].get("function") or {}).get("name", "")
        return [{"kind": "tool_result", "name": имя, "ok": not событие.text.startswith("ошибка инструмента")}]
    return []


async def вопрос(request: Request) -> Response:
    try:
        тело = await _тело(request)
    except ValueError as exc:
        return _ошибка(str(exc))
    текст = str(тело.get("text", "")).strip()
    if not текст:
        return _ошибка("пустая реплика")
    if ПРИЛОЖЕНИЕ.замок.locked():
        return _ошибка("идёт ответ", 409)

    очередь: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    async def обмен() -> None:
        async with ПРИЛОЖЕНИЕ.замок:
            try:
                if ПРИЛОЖЕНИЕ.агент is None:
                    await ПРИЛОЖЕНИЕ.поднять()
                ход = await ПРИЛОЖЕНИЕ.агент.exchange(
                    ПРИЛОЖЕНИЕ.клиент(),
                    advisor.модель(),
                    текст,
                    on_event=lambda с: [очередь.put_nowait(с2) for с2 in _событие(с)],
                    agent="веб",
                )
                if ход.ok:
                    ПРИЛОЖЕНИЕ.пары.append((текст, ход.text))
                ошибка = ход.error or ПРИЛОЖЕНИЕ.агент.store_error
                очередь.put_nowait({"kind": "done", "status": ход.status, "error": ошибка})
            except Exception as exc:  # noqa: BLE001 — причина уходит на страницу, а не в никуда
                очередь.put_nowait({"kind": "done", "status": "error", "error": f"{type(exc).__name__}: {exc}"})
            finally:
                очередь.put_nowait(None)

    # Обмен живёт отдельной задачей: закрытая вкладка не обрывает его посреди записи вкуса.
    задача = asyncio.create_task(обмен())
    ПРИЛОЖЕНИЕ.задачи.add(задача)
    задача.add_done_callback(ПРИЛОЖЕНИЕ.задачи.discard)

    async def поток():
        while (строка := await очередь.get()) is not None:
            yield json.dumps(строка, ensure_ascii=False) + "\n"

    return StreamingResponse(поток(), media_type="application/x-ndjson")


def _номер(тело: dict[str, Any]) -> int:
    try:
        return int(тело["номер"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("нужен номер игры") from None


async def оценка(request: Request) -> Response:
    """Плюс — в желаемое. Минус — запрет человека, затем пересмотр вкуса DeepSeek."""
    try:
        тело = await _тело(request)
        номер = _номер(тело)
    except ValueError as exc:
        return _ошибка(str(exc))
    плюс = bool(тело.get("плюс"))
    причина = str(тело.get("причина") or "").strip()
    с = games_db.открыть()
    try:
        запись = games_db.игра(с, номер)
        if запись is None:
            return _ошибка(f"игры {номер} нет в хранилище", 404)
        games_db.оценить(с, номер, плюс, причина)
    finally:
        с.close()
    if плюс:
        return JSONResponse({"итог": f"«{запись['имя']}» — в желаемом, слежу за скидками"})
    игра = ss.Игра(номер=номер, имя=запись["имя"], жанры=tuple(запись.get("жанры") or ()))
    записан, текст = taste.запретить(каталог(), игра)
    if not записан:
        return _ошибка(f"запрет не записан: {текст}", 500)
    async with ПРИЛОЖЕНИЕ.замок:
        вкус_записан, ответ = await advisor.пересмотреть(
            ПРИЛОЖЕНИЕ.клиент(), каталог(), игра, причина, await ПРИЛОЖЕНИЕ.метки_магазина()
        )
    return JSONResponse({
        "итог": f"«{запись['имя']}» больше не предлагается",
        "вкус_записан": вкус_записан,
        "пересмотр": ответ,
    })


async def снять_запрет(request: Request) -> Response:
    try:
        номер = _номер(await _тело(request))
    except ValueError as exc:
        return _ошибка(str(exc))
    снят, текст = taste.снять_запрет(каталог(), номер)
    if not снят:
        return _ошибка(текст, 404)
    с = games_db.открыть()
    try:
        games_db.снять_оценку(с, номер)
    finally:
        с.close()
    return JSONResponse({"итог": f"запрет снят: {текст}"})


async def запустить(request: Request) -> Response:
    род = request.path_params["род"]
    if род not in ("week", "day"):
        return _ошибка("задание — week или day")
    идущее = ПРИЛОЖЕНИЕ.задания.get(род)
    if идущее is not None and not идущее.done():
        return _ошибка("уже идёт", 409)

    async def задание() -> None:
        # Сбой задание уже записало в ленту и показало уведомлением.
        with contextlib.suppress(Exception):
            await jobs.запустить(род, каталог())

    ПРИЛОЖЕНИЕ.задания[род] = asyncio.create_task(задание())
    return JSONResponse({"итог": "запущено"}, status_code=202)


async def пересобрать(request: Request) -> Response:
    """Новый разговор опроса. Карточка не стирается: модель перепишет её `save_taste`, а
    запреты человека `create` сохранит."""
    if ПРИЛОЖЕНИЕ.замок.locked():
        return _ошибка("идёт ответ", 409)
    async with ПРИЛОЖЕНИЕ.замок:
        await ПРИЛОЖЕНИЕ.поднять(новый=True)
    return JSONResponse({"приветствие": ПРИВЕТСТВИЕ_ПЕРЕСБОРКИ})


def _ics_текст(текст: str) -> str:
    return текст.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def календарь_ics(записи: list[dict[str, Any]], сейчас: datetime | None = None) -> str:
    """Файл `.ics`: событие на весь день выхода. Без точной даты события нет — подписаться на
    «I кв. 2027» календарь не умеет."""
    штамп = (сейчас or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    строки = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//games-agent//RU",
        "CALSCALE:GREGORIAN", "X-WR-CALNAME:Выход игр",
    ]
    for з in записи:
        if not з.get("дата"):
            continue
        день = date.fromisoformat(з["дата"])
        строки += [
            "BEGIN:VEVENT",
            f"UID:steam-{з['номер']}@games-agent",
            f"DTSTAMP:{штамп}",
            f"DTSTART;VALUE=DATE:{день:%Y%m%d}",
            f"DTEND;VALUE=DATE:{день + timedelta(days=1):%Y%m%d}",
            f"SUMMARY:{_ics_текст('Выход: ' + з['имя'])}",
            f"DESCRIPTION:{_ics_текст((з.get('причина') or '') + ' ' + з['ссылка'])}",
            f"URL:{з['ссылка']}",
            "END:VEVENT",
        ]
    строки.append("END:VCALENDAR")
    return "\r\n".join(строки) + "\r\n"


async def ics(request: Request) -> Response:
    с = games_db.открыть()
    try:
        текст = календарь_ics(games_db.календарь(с))
    finally:
        с.close()
    return Response(текст, media_type="text/calendar; charset=utf-8")


@contextlib.asynccontextmanager
async def срок_жизни(app):
    каталог().mkdir(parents=True, exist_ok=True)
    advisor.подготовить_каталог(каталог())
    games_db.открыть().close()
    yield
    await ПРИЛОЖЕНИЕ.закрыть()


app = Starlette(
    routes=[
        Route("/", главная),
        Route("/api/state", состояние),
        Route("/api/ask", вопрос, methods=["POST"]),
        Route("/api/rate", оценка, methods=["POST"]),
        Route("/api/unban", снять_запрет, methods=["POST"]),
        Route("/api/run/{род}", запустить, methods=["POST"]),
        Route("/api/retaste", пересобрать, methods=["POST"]),
        Route("/calendar.ics", ics),
    ],
    lifespan=срок_жизни,
)


if __name__ == "__main__":
    uvicorn.run(app, host=АДРЕС, port=ПОРТ)
