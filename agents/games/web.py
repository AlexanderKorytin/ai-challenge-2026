"""Веб игрового агента: мастер первого запуска, разговор с советником, предложения, желаемое,
календарь, библиотека, лента, настройки.

Один человек, `127.0.0.1:8791`. Каталог агента — `GAMES_DATA_DIR`: хранилище, `.mcp.json`,
разрешения; карточка и разговор `myharness` привязаны к нему же. Ключи — в файле агента
(`secrets_store`), настройки `myharness` агент не читает: у другого человека их нет.

Мастер первого запуска (страница решает по `/api/state`): «Войти через Steam» → три ключа с
инструкцией → проверка Ollama → опрос вкуса. Опрос идёт, пока в карточке нет «Критериев».

Оценка — слово человека. Плюс — игра в желаемое. Минус — запрет строкой в «Ограничениях»
карточки с источником «человек» (адрес записи называет кнопка), затем DeepSeek пересматривает
вкус. Писать желаемое, оценки и календарь модели не дано.
"""

from __future__ import annotations

import agent_env

agent_env.задать()

import asyncio
import contextlib
import json
import os
import secrets
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx2
import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from starlette.routing import Route

import games_db
import itad
import secrets_store
import steam_account
import steam_store as ss
from myharness import api, project_card
from myharness.mcp_tools import Соединения

import advisor
import jobs
import judge
import similar
import taste

ЗДЕСЬ = Path(__file__).resolve().parent
ХОСТ = "127.0.0.1"
ПОРТ = int(os.environ.get("GAMES_WEB_PORT") or 8791)
АДРЕС = f"http://{ХОСТ}:{ПОРТ}"
ВОЗВРАТ_ВХОДА = f"{АДРЕС}/auth/steam"
# Свои источники запросов. Чужой сайт, открытый в браузере человека, может послать «простой»
# запрос на 127.0.0.1 (разбор рисков): записи принимаются только своими страницами.
СВОИ = {f"http://127.0.0.1:{ПОРТ}", f"http://localhost:{ПОРТ}"}
ПРИВЕТСТВИЕ_ПЕРЕСБОРКИ = "Хочу пересобрать вкус заново: спроси меня о любимых играх с начала."
# Модели, без которых агент не работает: местный отбор и векторы RAG.
МЕСТНЫЕ_МОДЕЛИ = (judge.МОДЕЛЬ.removeprefix("ollama/"), "bge-m3")


def каталог() -> Path:
    return agent_env.данные()


class Приложение:
    """Один агент разговора на процесс. Обмен и пересмотр идут под одним замком: оба пишут
    карточку и разговор. Задания из кнопок — по одному каждого рода."""

    def __init__(self) -> None:
        self._клиент: api.Clients | None = None
        self._ключ_клиента = ""
        self.соединения = Соединения()
        self.агент = None
        self.пары: list[tuple[str, str]] = []
        self.замок = asyncio.Lock()
        self.метки: list[ss.Метка] = []
        self.задания: dict[str, asyncio.Task] = {}
        self.задачи: set[asyncio.Task] = set()
        # Повтор перехваченного ответа OpenID: однажды принятая метка ответа второй раз не
        # принимается. Живёт в памяти процесса — на 127.0.0.1 этого довольно.
        self.метки_входа: set[str] = set()
        # Ожидающие входы: метка в `return_to` связывает ответ Steam с входом, который начал
        # этот браузер. Без неё чужой подписанный ответ записал бы чужой SteamID (разбор рисков).
        self.ожидающие_входы: set[str] = set()
        # Тайны правят вход и форма ключей — разными запросами, а проверка ключа идёт секундами.
        # Без замка одна запись затирала бы другую.
        self.замок_тайн = asyncio.Lock()

    def клиент(self) -> api.Clients:
        ключ = secrets_store.загрузить().deepseek_ключ
        if self._клиент is None or ключ != self._ключ_клиента:
            self._клиент = api.Clients(ключ)
            self._ключ_клиента = ключ
        return self._клиент

    async def метки_магазина(self) -> list[ss.Метка]:
        """Метки — при первой надобности, в памяти процесса. Берутся ВНЕ замка обмена: повторы
        магазина при 429 тянутся минутами, и чат всё это время отвечал бы «идёт ответ»."""
        if not self.метки:
            async with ss.клиент() as к:
                self.метки = await ss.метки(к)
        return self.метки

    async def поднять(self, *, новый: bool = False) -> None:
        метки = await self.метки_магазина()
        if self.агент is not None:
            await self.соединения.закрыть()
            self.соединения = Соединения()
        self.агент, self.пары = advisor.поднять(каталог(), self.соединения, метки, новый=новый)

    async def закрыть(self) -> None:
        for задача in self.задания.values():
            задача.cancel()
        await asyncio.gather(*self.задачи, *self.задания.values(), return_exceptions=True)
        await self.соединения.закрыть()
        if self._клиент is not None:
            await self._клиент.aclose()


ПРИЛОЖЕНИЕ = Приложение()


def _скрыть(текст: str) -> str:
    try:
        return secrets_store.скрыть(текст, secrets_store.загрузить())
    except Exception:  # noqa: BLE001 — файл тайн не читается: ключа в процессе нет, прятать нечего
        return текст


def _ошибка(текст: str, код: int = 400) -> JSONResponse:
    return JSONResponse({"error": _скрыть(текст)}, status_code=код)


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


# ---- состояние ----


def _вкус() -> dict[str, list[str]]:
    карточка, _ = project_card.load(каталог())
    if карточка is None:
        return {}
    return {имя: карточка.пункты(имя) for имя in taste.РАЗДЕЛЫ}


def _запреты() -> list[dict[str, Any]]:
    карточка, _ = project_card.load(каталог())
    if карточка is None:
        return []
    return [
        {"строка": с, "номер": taste.номер_запрета(с)}
        for с in карточка.пункты(project_card.РАЗДЕЛ_ОГРАНИЧЕНИЙ)
    ]


def _с_оценкой(записи: list[dict]) -> list[dict]:
    """Оценка SteamDB числом и порядок по ней: лучшие первыми, без отзывов — в конце."""
    for з in записи:
        о = ss.оценка(ss.Отзывы("", int(з.get("отзывы_положительных") or 0), int(з.get("отзывы_всего") or 0)))
        з["оценка_steamdb"] = None if о is None else round(100 * о)
    return sorted(записи, key=lambda з: -(з["оценка_steamdb"] if з["оценка_steamdb"] is not None else -1))


async def _ollama() -> dict[str, Any]:
    хост = os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    адрес = хост if "://" in хост else f"http://{хост}"
    try:
        async with httpx2.AsyncClient(timeout=5) as к:
            ответ = await к.get(f"{адрес}/api/tags")
        есть = {м.get("name", "") for м in ответ.json().get("models", [])}
    except (httpx2.HTTPError, ValueError):
        return {"служба": False, "модели": {м: False for м in МЕСТНЫЕ_МОДЕЛИ}}
    # `bge-m3` в списке службы называется `bge-m3:latest`.
    return {"служба": True, "модели": {м: м in есть or f"{м}:latest" in есть for м in МЕСТНЫЕ_МОДЕЛИ}}


def _сверка_нужна(с) -> bool:
    последняя = games_db.отметка(с, "последняя_сверка")
    return any(
        з["род"] == "сверка" and (последняя is None or str(з["время"]) > последняя)
        for з in games_db.лента(с)
    )


async def состояние(request: Request) -> Response:
    тайны = secrets_store.загрузить()
    с = games_db.открыть()
    try:
        данные = {
            "настройка": {
                "вошёл": bool(тайны.steamid),
                "steamid": тайны.steamid,
                "ключи": {
                    "steam": bool(тайны.steam_ключ),
                    "deepseek": bool(тайны.deepseek_ключ),
                    "itad": bool(тайны.itad_ключ),
                },
                "ollama": await _ollama(),
            },
            "опрос_пройден": taste.опрос_пройден(каталог()),
            "сверка_нужна": _сверка_нужна(с),
            "вкус": _вкус(),
            "запреты": _запреты(),
            "предложения": _с_оценкой(games_db.предложения(с, только_без_оценки=True)),
            "желаемое": _с_оценкой(games_db.желаемое(с)),
            "календарь": games_db.календарь(с),
            "библиотека": games_db.библиотека(с),
            "лента": games_db.лента(с),
            "задания_идут": sorted(р for р, з in ПРИЛОЖЕНИЕ.задания.items() if not з.done()),
            "разговор": [{"вопрос": в, "ответ": о} for в, о in ПРИЛОЖЕНИЕ.пары],
        }
    finally:
        с.close()
    return JSONResponse(данные)


# ---- вход и ключи ----


def _возврат(метка: str) -> str:
    return f"{ВОЗВРАТ_ВХОДА}?state={метка}"


async def вход_начать(request: Request) -> Response:
    метка = secrets.token_urlsafe(24)
    ПРИЛОЖЕНИЕ.ожидающие_входы.add(метка)
    return RedirectResponse(steam_account.адрес_входа(_возврат(метка)), status_code=302)


async def вход_вернуться(request: Request) -> Response:
    доводы = dict(request.query_params)
    вход = доводы.pop("state", "")
    if вход not in ПРИЛОЖЕНИЕ.ожидающие_входы:
        return Response("Вход отклонён: этот браузер вход не начинал. Нажмите «Войти через Steam» ещё раз.", status_code=403)
    ПРИЛОЖЕНИЕ.ожидающие_входы.discard(вход)
    метка = доводы.get("openid.response_nonce", "")
    if not метка or метка in ПРИЛОЖЕНИЕ.метки_входа:
        return Response("Вход отклонён: ответ Steam уже использован или без метки.", status_code=400)
    try:
        async with httpx2.AsyncClient(timeout=30) as к:
            steamid = await steam_account.проверить_вход(к, доводы, _возврат(вход))
    except Exception as exc:  # noqa: BLE001 — Steam не ответил: человеку — причина, а не трасса
        return Response(f"Steam не ответил на проверку входа: {type(exc).__name__}. Попробуйте ещё раз.", status_code=502)
    if steamid is None:
        return Response("Вход отклонён: Steam не подтвердил ответ.", status_code=403)
    ПРИЛОЖЕНИЕ.метки_входа.add(метка)
    async with ПРИЛОЖЕНИЕ.замок_тайн:
        тайны = secrets_store.загрузить()
        тайны.steamid = steamid
        secrets_store.сохранить(тайны)
    await _синхронизировать_сейчас()
    return RedirectResponse("/", status_code=302)


async def _синхронизировать_сейчас() -> None:
    """Библиотека и список Steam — сразу, как есть вход и ключ: опрос начинается с библиотеки,
    а задание дня до опроса не идёт (находка просмотра). Сбой — строкой в ленте, как у задания."""
    тайны = secrets_store.загрузить()
    if not (тайны.steam_ключ and тайны.steamid):
        return
    async with httpx2.AsyncClient(timeout=30) as к:
        учётка = jobs.Учётка(
            библиотека=lambda: steam_account.библиотека(к, тайны.steam_ключ, тайны.steamid),
            список_желаемого=lambda: steam_account.список_желаемого(к, тайны.steamid, тайны.steam_ключ),
        )
        с = games_db.открыть()
        try:
            await jobs._синхронизировать(с, учётка)
        finally:
            с.close()


async def _проверить_steam(ключ: str, steamid: str) -> str | None:
    """Ключ проверяется настоящим вызовом библиотеки. Закрытая библиотека — ключ годен, но
    человеку нужна подсказка: её несёт текст исключения."""
    try:
        async with httpx2.AsyncClient(timeout=30) as к:
            await steam_account.библиотека(к, ключ, steamid)
    except steam_account.БиблиотекаЗакрыта as exc:
        return f"ключ принят, но {exc}"
    except steam_account.КлючНеПринят:
        return "ключ Steam не принят"
    except Exception as exc:  # noqa: BLE001
        return f"Steam не ответил: {type(exc).__name__}"
    return None


async def _проверить_deepseek(ключ: str) -> str | None:
    клиент = api.Clients(ключ)
    try:
        return None if await клиент.validate() else "ключ DeepSeek не принят"
    except Exception as exc:  # noqa: BLE001
        return f"DeepSeek не ответил: {type(exc).__name__}"
    finally:
        await клиент.aclose()


async def _проверить_itad(ключ: str) -> str | None:
    try:
        async with httpx2.AsyncClient(timeout=30) as к:
            await itad.минимум(к, ключ, 1086940)
    except itad.КлючНеПринят:
        return "ключ IsThereAnyDeal не принят"
    except Exception as exc:  # noqa: BLE001
        return f"IsThereAnyDeal не ответил: {type(exc).__name__}"
    return None


async def ключи(request: Request) -> Response:
    """Сохранить ключи. Каждый поданный ключ проверяется настоящим вызовом ДО записи: негодный
    ключ не ложится в файл. Пустая строка — ключ удаляется. Ключи в ответ не возвращаются."""
    try:
        тело = await _тело(request)
    except ValueError as exc:
        return _ошибка(str(exc))
    steamid = secrets_store.загрузить().steamid
    итоги: dict[str, str] = {}
    принятые: dict[str, str] = {}
    проверки = {
        "steam": ("steam_ключ", lambda к: _проверить_steam(к, steamid)),
        "deepseek": ("deepseek_ключ", _проверить_deepseek),
        "itad": ("itad_ключ", _проверить_itad),
    }
    for имя, (поле, проверить) in проверки.items():
        if имя not in тело:
            continue
        ключ = str(тело[имя] or "").strip()
        if not ключ:
            принятые[поле] = ""
            итоги[имя] = "удалён"
            continue
        if имя == "steam" and not steamid:
            итоги[имя] = "сначала войдите через Steam"
            continue
        жалоба = await проверить(ключ)
        if жалоба is None or жалоба.startswith("ключ принят"):
            принятые[поле] = ключ
        итоги[имя] = жалоба or "принят"
    # Тайны перечитываются под замком прямо перед записью, и меняются только поданные поля:
    # вход Steam или вторая форма, пришедшие за секунды проверки, не затираются.
    async with ПРИЛОЖЕНИЕ.замок_тайн:
        тайны = secrets_store.загрузить()
        for поле, ключ in принятые.items():
            setattr(тайны, поле, ключ)
        secrets_store.сохранить(тайны)
    if принятые.get("steam_ключ"):
        await _синхронизировать_сейчас()
    return JSONResponse({"итоги": {к: secrets_store.скрыть(т, тайны) for к, т in итоги.items()}})


async def выйти_из_steam(request: Request) -> Response:
    async with ПРИЛОЖЕНИЕ.замок_тайн:
        тайны = secrets_store.загрузить()
        тайны.steamid = ""
        тайны.steam_ключ = ""
        secrets_store.сохранить(тайны)
    return JSONResponse({"итог": "вход в Steam и ключ Steam удалены"})


# ---- разговор ----


def _событие(событие: api.StreamEvent) -> list[dict[str, Any]]:
    """Событие потока агента → строки NDJSON для страницы (как в веб-чате дня 33)."""
    if событие.kind == "content" and событие.text:
        return [{"kind": "content", "text": событие.text}]
    if событие.kind == "tool_calls":
        return [{"kind": "tool", "name": (в.get("function") or {}).get("name", "")} for в in событие.calls]
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
    if not secrets_store.загрузить().deepseek_ключ:
        return _ошибка("нет ключа DeepSeek: введите его в настройках", 412)
    try:
        await ПРИЛОЖЕНИЕ.метки_магазина()
    except ss.ОшибкаМагазина as exc:
        return _ошибка(f"магазин Steam не отдал метки: {exc}", 502)

    очередь: asyncio.Queue[dict[str, Any] | None] = asyncio.Queue()

    if not taste.опрос_пройден(каталог()):
        с = games_db.открыть()
        try:
            пусто = not games_db.библиотека(с)
        finally:
            с.close()
        if пусто:
            await _синхронизировать_сейчас()

    async def обмен() -> None:
        async with ПРИЛОЖЕНИЕ.замок:
            try:
                if ПРИЛОЖЕНИЕ.агент is None:
                    await ПРИЛОЖЕНИЕ.поднять()
                ход = await ПРИЛОЖЕНИЕ.агент.exchange(
                    ПРИЛОЖЕНИЕ.клиент(), advisor.модель(), текст,
                    on_event=lambda с: [очередь.put_nowait(с2) for с2 in _событие(с)],
                    agent="веб",
                )
                if ход.ok:
                    ПРИЛОЖЕНИЕ.пары.append((текст, ход.text))
                ошибка = ход.error or ПРИЛОЖЕНИЕ.агент.store_error
                очередь.put_nowait({"kind": "done", "status": ход.status, "error": _скрыть(ошибка) if ошибка else None})
            except Exception as exc:  # noqa: BLE001 — причина уходит на страницу, а не в никуда
                очередь.put_nowait({"kind": "done", "status": "error", "error": _скрыть(f"{type(exc).__name__}: {exc}")})
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


# ---- оценки ----


def _номер(тело: dict[str, Any]) -> int:
    try:
        return int(тело["номер"])
    except (KeyError, TypeError, ValueError):
        raise ValueError("нужен номер игры") from None


def _игра(запись: dict) -> ss.Игра:
    return ss.Игра(
        номер=int(запись["номер"]), имя=запись.get("имя") or str(запись["номер"]),
        описание=запись.get("описание") or "", жанры=tuple(запись.get("жанры") or ()),
        отзывы=ss.Отзывы(запись.get("отзывы_сводка") or "", int(запись.get("отзывы_положительных") or 0),
                         int(запись.get("отзывы_всего") or 0)),
    )


async def _в_индекс(игра: ss.Игра, отметка: str) -> None:
    """Новая отметка человека — сразу в RAG, отдельным потоком: служба Ollama грузит модель
    секундами, и цикл событий веба не должен замирать. Не ответила — недельная пересборка
    подхватит; оценка человека от этого не теряется."""
    документ = similar.Документ(игра.номер, игра.имя, similar.текст_игры(игра), отметка)
    with contextlib.suppress(Exception):
        await asyncio.to_thread(lambda: similar.Индекс().дописать(документ))


async def _пересмотр(игра: ss.Игра, причина: str, род: str) -> dict[str, Any]:
    if not secrets_store.загрузить().deepseek_ключ:
        return {"вкус_записан": False, "пересмотр": "нет ключа DeepSeek: вкус не пересмотрен"}
    метки = await ПРИЛОЖЕНИЕ.метки_магазина()
    async with ПРИЛОЖЕНИЕ.замок:
        записан, ответ = await advisor.пересмотреть(ПРИЛОЖЕНИЕ.клиент(), каталог(), игра, причина, метки, род)
    return {"вкус_записан": записан, "пересмотр": ответ}


async def оценка(request: Request) -> Response:
    """Плюс — в желаемое. Минус — сначала запрет человека, затем оценка, затем пересмотр: отказ
    записи запрета не должен оставить игру убранной из выдачи без запрета, который можно снять."""
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
        игра = _игра(запись)
        if плюс:
            games_db.оценить(с, номер, True, причина)
            await _в_индекс(игра, "плюс")
            return JSONResponse({"итог": f"«{игра.имя}» — в желаемом, слежу за скидками"})
        записан, текст = taste.запретить(каталог(), игра)
        if not записан:
            return _ошибка(f"запрет не записан: {текст}", 500)
        games_db.оценить(с, номер, False, причина)
        games_db.убрать_из_календаря(с, номер)
    finally:
        с.close()
    await _в_индекс(игра, f"минус: {причина}" if причина else "минус")
    return JSONResponse({"итог": f"«{игра.имя}» больше не предлагается", **await _пересмотр(игра, причина, "минус")})


async def уже_играл(request: Request) -> Response:
    """Игра пройдена вне Steam: уходит из выдачи; «не понравилась» — ещё и запрет человека."""
    try:
        тело = await _тело(request)
        номер = _номер(тело)
    except ValueError as exc:
        return _ошибка(str(exc))
    понравилась = bool(тело.get("понравилась"))
    причина = str(тело.get("причина") or "").strip()
    с = games_db.открыть()
    try:
        запись = games_db.игра(с, номер)
        if запись is None:
            return _ошибка(f"игры {номер} нет в хранилище", 404)
        игра = _игра(запись)
        if not понравилась:
            записан, текст = taste.запретить(каталог(), игра)
            if not записан:
                return _ошибка(f"запрет не записан: {текст}", 500)
        games_db.уже_играл(с, номер, понравилась)
        games_db.убрать_из_календаря(с, номер)
    finally:
        с.close()
    род = "уже играл: понравилась" if понравилась else "уже играл: не понравилась"
    await _в_индекс(игра, род)
    return JSONResponse({"итог": f"«{игра.имя}» — отмечена как пройденная", **await _пересмотр(игра, причина, род)})


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


# ---- задания, пересборка, сверка ----


async def запустить(request: Request) -> Response:
    род = request.path_params["kind"]
    if род not in ("week", "day"):
        return _ошибка("задание — week или day")
    идущее = ПРИЛОЖЕНИЕ.задания.get(род)
    if идущее is not None and not идущее.done():
        return _ошибка("уже идёт", 409)

    async def задание() -> None:
        # Сбой и отмену задание само пишет в ленту (`jobs.запустить`).
        try:
            await jobs.запустить(род, каталог())
        except jobs.УжеИдёт:
            jobs.в_ленту_итог(f"Задание «{'неделя' if род == 'week' else 'день'}» уже идёт в службе — дождитесь его итога")
        except Exception:  # noqa: BLE001 — уже в ленте
            pass

    ПРИЛОЖЕНИЕ.задания[род] = asyncio.create_task(задание())
    return JSONResponse({"итог": "запущено"}, status_code=202)


async def пересобрать(request: Request) -> Response:
    """Новый разговор опроса. Карточка не стирается: модель перепишет её `save_taste`, а
    запреты человека `create` сохранит."""
    if ПРИЛОЖЕНИЕ.замок.locked():
        return _ошибка("идёт ответ", 409)
    await ПРИЛОЖЕНИЕ.метки_магазина()
    async with ПРИЛОЖЕНИЕ.замок:
        await ПРИЛОЖЕНИЕ.поднять(новый=True)
    return JSONResponse({"приветствие": ПРИВЕТСТВИЕ_ПЕРЕСБОРКИ})


async def сверка_начать(request: Request) -> Response:
    return JSONResponse({"приветствие": advisor.ПРИВЕТСТВИЕ_СВЕРКИ})


async def сверка_готова(request: Request) -> Response:
    с = games_db.открыть()
    try:
        games_db.поставить_отметку(с, "последняя_сверка", datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    finally:
        с.close()
    return JSONResponse({"итог": "сверка отмечена, следующая — через полгода"})


# ---- календарь ----


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
            f"SUMMARY:{_ics_текст('Выход: ' + (з.get('имя') or str(з['номер'])))}",
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


# ---- жизнь процесса ----


async def _поднять_при_старте() -> None:
    """Агент поднимается при старте службы: страница сразу показывает прежний разговор. Нет
    ключа или магазин молчит — поднимется при первой реплике."""
    with contextlib.suppress(Exception):
        await ПРИЛОЖЕНИЕ.метки_магазина()
        async with ПРИЛОЖЕНИЕ.замок:
            if ПРИЛОЖЕНИЕ.агент is None:
                await ПРИЛОЖЕНИЕ.поднять()


class ЗащитаЗаписи:
    """Запросы с записью принимаются только своими страницами (разбор рисков).

    - `Content-Type: application/json` — браузер тогда обязан спросить разрешения
      предварительным запросом, а заголовков CORS у веба нет: чужая страница не дойдёт.
    - `Origin`, если он есть, — свой. Браузеры шлют его на каждый POST; значок ходит только GET.
    Подмену DNS на 127.0.0.1 закрывает `TrustedHostMiddleware` рядом."""

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http" and scope["method"] not in ("GET", "HEAD"):
            заголовки = {к.decode("latin-1").lower(): з.decode("latin-1") for к, з in scope["headers"]}
            источник = заголовки.get("origin")
            вид = заголовки.get("content-type", "").split(";")[0].strip().lower()
            if (источник is not None and источник not in СВОИ) or вид != "application/json":
                await JSONResponse({"error": "запрос не со страницы агента"}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


@contextlib.asynccontextmanager
async def срок_жизни(app):
    каталог().mkdir(parents=True, exist_ok=True)
    # Каталог данных — только владельцу: в нём SteamID, библиотека, вкус и разговор.
    os.chmod(каталог(), 0o700)
    advisor.подготовить_каталог(каталог())
    games_db.открыть().close()
    задача = asyncio.create_task(_поднять_при_старте())
    ПРИЛОЖЕНИЕ.задачи.add(задача)
    задача.add_done_callback(ПРИЛОЖЕНИЕ.задачи.discard)
    yield
    await ПРИЛОЖЕНИЕ.закрыть()


app = Starlette(
    routes=[
        Route("/", главная),
        Route("/api/state", состояние),
        Route("/auth/steam/start", вход_начать),
        Route("/auth/steam", вход_вернуться),
        Route("/api/keys", ключи, methods=["POST"]),
        Route("/api/logout", выйти_из_steam, methods=["POST"]),
        Route("/api/ask", вопрос, methods=["POST"]),
        Route("/api/rate", оценка, methods=["POST"]),
        Route("/api/played", уже_играл, methods=["POST"]),
        Route("/api/unban", снять_запрет, methods=["POST"]),
        # Имя довода пути — латиницей: Starlette иного не принимает и маршрут молча отдавал 404.
        Route("/api/run/{kind}", запустить, methods=["POST"]),
        Route("/api/retaste", пересобрать, methods=["POST"]),
        Route("/api/review/start", сверка_начать, methods=["POST"]),
        Route("/api/review/done", сверка_готова, methods=["POST"]),
        Route("/calendar.ics", ics),
    ],
    lifespan=срок_жизни,
    middleware=[
        Middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"]),
        Middleware(ЗащитаЗаписи),
    ],
)


if __name__ == "__main__":
    uvicorn.run(app, host=ХОСТ, port=ПОРТ)
