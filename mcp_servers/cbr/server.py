"""MCP-сервер официальных курсов валют Центрального банка РФ.

Обёртка над голым XML-API ЦБ (`www.cbr.ru/scripts/`): курс на дату и курс по дням за период, а
также сбор курсов по расписанию — задания cron, которые выполняет сам сервер (`scheduler.py`),
хранилище SQLite (`store.py`) и сводка по собранному. Регистрирует их декоратор `@srv.tool()`, описание доводов берётся из подписи
функции (`Annotated[..., Field(description=...)]`), а возврат — модель данных, из которой
библиотека строит и текст ответа, и `structuredContent` со схемой выхода.

Работает по Streamable HTTP за проверкой токена `Authorization: Bearer …`. Сеансов на сервере
нет (`stateless_http`): перезапуск службы не рвёт клиента, у которого соединение живёт весь
сеанс.

Запуск: `CBR_MCP_TOKEN=… uv run python server.py --port 8770 --allowed-host 38.180.117.69`.
Хранилище — файл по пути `CBR_MCP_DB` (не задан — `cbr.sqlite3` рядом с сервером).
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import datetime as dt
import hmac
import os
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

import scheduler
from store import Job, Observed, Store

ИСТОЧНИК = "https://www.cbr.ru/scripts/"
ПЕРЕМЕННАЯ_ТОКЕНА = "CBR_MCP_TOKEN"
ПЕРЕМЕННАЯ_БАЗЫ = "CBR_MCP_DB"
# «Сегодня» — по Москве: ЦБ устанавливает курс по московскому календарю, и на сервере в другом
# поясе около полуночи «сегодня» было бы соседним днём.
МОСКВА = ZoneInfo("Europe/Moscow")


# --- модели выхода -------------------------------------------------------------------------
# Имена полей латиницей: схема уходит модели как JSON Schema, а описания полей — по-русски.


class Rate(BaseModel):
    code: str = Field(description="Код валюты ISO 4217")
    name: str = Field(description="Название валюты по ЦБ")
    nominal: int = Field(description="Номинал: за сколько единиц валюты указан курс")
    value: float = Field(description="Курс в рублях за номинал")
    unit_rate: float = Field(description="Курс в рублях за одну единицу валюты")
    rate_date: str = Field(
        description="Дата ГГГГ-ММ-ДД, на которую ЦБ установил этот курс. На выходной и "
        "праздник ЦБ отдаёт курс, установленный на последний рабочий день, — эта дата "
        "может отличаться от запрошенной"
    )
    requested_date: str = Field(description="Запрошенная дата ГГГГ-ММ-ДД")


class Point(BaseModel):
    date: str = Field(description="Дата ГГГГ-ММ-ДД, на которую установлен курс")
    nominal: int = Field(description="Номинал")
    value: float = Field(description="Курс в рублях за номинал")
    unit_rate: float = Field(description="Курс в рублях за одну единицу")


class Dynamics(BaseModel):
    code: str = Field(description="Код валюты ISO 4217")
    name: str = Field(description="Название валюты по ЦБ")
    date_from: str = Field(description="Начало периода ГГГГ-ММ-ДД")
    date_to: str = Field(description="Конец периода ГГГГ-ММ-ДД")
    points: list[Point] = Field(
        description="Курсы по дням установления; дней без нового курса (выходных) в списке нет"
    )


class ScheduledJob(BaseModel):
    id: int = Field(description="Номер задания сбора")
    currencies: list[str] = Field(description="Коды валют, которые собирает задание")
    cron: str = Field(description="Расписание выражением cron, время московское")
    next_run: str = Field(description="Ближайший срок запуска, ISO 8601 с поясом")
    runs_ok: int = Field(description="Удачных запусков")
    runs_failed: int = Field(description="Запусков с ошибкой")
    last_run_at: str | None = Field(description="Последний запуск, ISO 8601; не было — null")
    last_error: str | None = Field(
        description="Ошибка последнего запуска; последний прошёл удачно или не было — null"
    )


class Schedules(BaseModel):
    jobs: list[ScheduledJob] = Field(description="Задания сбора по номеру")


class Deleted(BaseModel):
    job_id: int = Field(description="Номер снятого задания; собранные им курсы остались")


class RateSummary(BaseModel):
    code: str = Field(description="Код валюты ISO 4217")
    name: str = Field(description="Название валюты по ЦБ")
    date_from: str | None = Field(description="Начало отбора ГГГГ-ММ-ДД; null — с первого курса")
    date_to: str | None = Field(description="Конец отбора ГГГГ-ММ-ДД; null — по последний курс")
    count: int = Field(description="Сколько курсов (дат установления) собрано в отборе")
    first: Point = Field(description="Самый ранний собранный курс")
    last: Point = Field(description="Самый поздний собранный курс")
    change: float = Field(description="Изменение курса за единицу от первого к последнему, руб.")
    change_pct: float = Field(description="То же изменение в процентах от первого курса")
    min: Point = Field(description="Наименьший курс за единицу; при равных — более ранний")
    max: Point = Field(description="Наибольший курс за единицу; при равных — более ранний")
    mean: float = Field(description="Средний курс за единицу, руб.")
    last_seen_at: str = Field(description="Когда сервер собрал самый свежий курс, ISO 8601")


# --- обращение к ЦБ ------------------------------------------------------------------------


async def _получить(путь: str, доводы: dict[str, str]) -> bytes:
    """Тело ответа ЦБ как есть (XML в windows-1251).

    Ожидаемые сбои инструментов — `ToolError`: только его текст библиотека передаёт клиенту,
    любое другое исключение она считает падением и прячет за «Error executing tool …», и
    модель не узнала бы, что поправить в доводах.

    Предел ожидания — умолчание клиента (5 с); своего числа нет. Истечение и сетевые сбои —
    ошибка инструмента с понятной причиной, а не падение сервера.
    """
    try:
        async with httpx2.AsyncClient() as клиент:
            ответ = await клиент.get(ИСТОЧНИК + путь, params=доводы)
            ответ.raise_for_status()
            return ответ.content
    except httpx2.TimeoutException as exc:
        raise ToolError("ЦБ не ответил вовремя") from exc
    except httpx2.HTTPStatusError as exc:
        raise ToolError(f"ЦБ ответил HTTP {exc.response.status_code}") from exc
    except httpx2.HTTPError as exc:
        raise ToolError(f"нет связи с ЦБ: {type(exc).__name__}") from exc


def _дата(текст: str, довод: str) -> dt.date:
    try:
        return dt.date.fromisoformat(текст)
    except ValueError:
        raise ToolError(f"{довод}: ожидается дата ГГГГ-ММ-ДД, получено {текст!r}") from None


def _для_цб(дата: dt.date) -> str:
    return дата.strftime("%d/%m/%Y")


def _из_цб(текст: str) -> dt.date:
    return dt.datetime.strptime(текст, "%d.%m.%Y").date()


def _число(текст: str | None) -> float:
    # У ЦБ десятичная запятая: «86,3793».
    return float((текст or "").replace(",", "."))


def _сейчас() -> dt.datetime:
    return dt.datetime.now(МОСКВА)


def _сегодня() -> dt.date:
    return _сейчас().date()


def _валюты(корень: ET.Element) -> dict[str, ET.Element]:
    return {(в.findtext("CharCode") or "").upper(): в for в in корень.iter("Valute")}


def _разобрать(тело: bytes) -> ET.Element:
    try:
        return ET.fromstring(тело)
    except ET.ParseError:
        # При сбое ЦБ отдаёт страницу HTML, а не XML; без своей ошибки библиотека спрятала бы
        # причину за «Error executing tool».
        raise ToolError("ЦБ вернул не XML — сбой на стороне ЦБ") from None


def _дата_ответа(корень: ET.Element, атрибут: str) -> dt.date:
    try:
        return _из_цб(корень.get(атрибут, ""))
    except ValueError:
        raise ToolError(f"в ответе ЦБ нет даты {атрибут}") from None


async def _ежедневные(дата: dt.date) -> ET.Element:
    корень = _разобрать(await _получить("XML_daily.asp", {"date_req": _для_цб(дата)}))
    if not корень.findall("Valute"):
        # Пустой список с чужой датой ЦБ отдаёт на дальнюю будущую дату и на даты до начала
        # своих записей — выдавать его за курс нельзя.
        raise ToolError(f"ЦБ не дал курсов на {дата.isoformat()}")
    return корень


def _не_найдена(код: str, дата: dt.date, валюты: dict[str, ET.Element]) -> ToolError:
    return ToolError(
        f"нет курса {код} на {дата.isoformat()}; есть: {', '.join(sorted(валюты))}"
    )


async def _собрать(дата: dt.date) -> list[Observed]:
    """Все валюты ответа ЦБ на дату — для планировщика. Дата курса — та, что назвал ЦБ."""
    корень = await _ежедневные(дата)
    установлен = _дата_ответа(корень, "Date")
    return [
        Observed(
            code=код,
            name=в.findtext("Name") or "",
            rate_date=установлен,
            nominal=int(в.findtext("Nominal") or "1"),
            value=_число(в.findtext("Value")),
            unit_rate=_число(в.findtext("VunitRate")),
        )
        for код, в in _валюты(корень).items()
    ]


# --- сервер и инструменты ------------------------------------------------------------------

srv = MCPServer(
    "cbr",
    instructions=(
        "Официальные курсы иностранных валют к рублю, установленные ЦБ РФ. Сервер умеет сам "
        "собирать курсы по расписанию (schedule_collect) и отдавать сводку по собранному "
        "(get_summary)."
    ),
)

# Хранилище и планировщик подставляет `main()` при запуске службы, а проверки — свои.
_хранилище: Store | None = None
_планировщик: scheduler.Scheduler | None = None


def _задания() -> tuple[Store, scheduler.Scheduler]:
    if _хранилище is None or _планировщик is None:
        raise ToolError("хранилище сервера не поднято")
    return _хранилище, _планировщик


def _задание(з: Job) -> ScheduledJob:
    return ScheduledJob(
        id=з.id,
        currencies=з.currencies,
        cron=з.cron,
        next_run=з.next_run.isoformat(),
        runs_ok=з.runs_ok,
        runs_failed=з.runs_failed,
        last_run_at=з.last_run_at.isoformat() if з.last_run_at else None,
        last_error=з.last_error,
    )


def _точка(к: Observed) -> Point:
    return Point(date=к.rate_date.isoformat(), nominal=к.nominal, value=к.value, unit_rate=к.unit_rate)


@srv.tool()
async def get_rate(
    currency: Annotated[str, Field(description="Код валюты ISO 4217: USD, EUR, CNY…")],
    date: Annotated[
        str | None,
        Field(description="Дата ГГГГ-ММ-ДД; не задана — сегодня по Москве"),
    ] = None,
) -> Rate:
    """Официальный курс валюты к рублю, установленный ЦБ РФ на дату."""
    запрошена = _дата(date, "date") if date else _сегодня()
    код = currency.strip().upper()
    корень = await _ежедневные(запрошена)
    валюты = _валюты(корень)
    if код not in валюты:
        raise _не_найдена(код, запрошена, валюты)
    в = валюты[код]
    установлен = _дата_ответа(корень, "Date")
    if запрошена > _сегодня() and установлен < запрошена:
        # На близкую будущую дату ЦБ отдаёт ПОСЛЕДНИЙ установленный курс под его датой.
        # Отличить это от выходного можно только по «сегодня»: выходной в прошлом — законный
        # курс пятницы, а будущая дата без нового курса — курса ещё нет. Курс на завтра ЦБ
        # публикует днём, и тогда даты совпадут — такой ответ проходит.
        raise ToolError(
            f"курс {код} на {запрошена.isoformat()} ещё не установлен; последний — на "
            f"{установлен.isoformat()}: {_число(в.findtext('Value'))} руб. "
            f"за {в.findtext('Nominal') or '1'}"
        )
    return Rate(
        code=код,
        name=в.findtext("Name") or "",
        nominal=int(в.findtext("Nominal") or "1"),
        value=_число(в.findtext("Value")),
        unit_rate=_число(в.findtext("VunitRate")),
        rate_date=установлен.isoformat(),
        requested_date=запрошена.isoformat(),
    )


@srv.tool()
async def get_rate_dynamics(
    currency: Annotated[str, Field(description="Код валюты ISO 4217: USD, EUR, CNY…")],
    date_from: Annotated[str, Field(description="Начало периода ГГГГ-ММ-ДД")],
    date_to: Annotated[str, Field(description="Конец периода ГГГГ-ММ-ДД, не раньше начала")],
) -> Dynamics:
    """Официальные курсы валюты к рублю по дням за период (ЦБ РФ)."""
    начало = _дата(date_from, "date_from")
    конец = _дата(date_to, "date_to")
    if начало > конец:
        raise ToolError(f"date_from {начало.isoformat()} позже date_to {конец.isoformat()}")
    код = currency.strip().upper()
    # Динамику ЦБ отдаёт по своему коду валюты (R01235), а не по ISO; код берётся из списка
    # на конец периода, но не позже сегодняшнего дня — на будущую дату списка может не быть,
    # а данные за начало периода есть.
    опорная = min(конец, _сегодня())
    валюты = _валюты(await _ежедневные(опорная))
    if код not in валюты:
        raise _не_найдена(код, опорная, валюты)
    в = валюты[код]
    корень = _разобрать(
        await _получить(
            "XML_dynamic.asp",
            {
                "date_req1": _для_цб(начало),
                "date_req2": _для_цб(конец),
                "VAL_NM_RQ": в.get("ID", ""),
            },
        )
    )
    точки = [
        Point(
            date=_дата_ответа(з, "Date").isoformat(),
            nominal=int(з.findtext("Nominal") or "1"),
            value=_число(з.findtext("Value")),
            unit_rate=_число(з.findtext("VunitRate")),
        )
        for з in корень.iter("Record")
    ]
    return Dynamics(
        code=код,
        name=в.findtext("Name") or "",
        date_from=начало.isoformat(),
        date_to=конец.isoformat(),
        points=точки,
    )


@srv.tool()
async def schedule_collect(
    currencies: Annotated[
        list[str], Field(description="Коды валют ISO 4217 для сбора: [\"USD\", \"EUR\"]")
    ],
    cron: Annotated[
        str,
        Field(
            description="Расписание выражением cron из пяти полей, время московское: "
            "\"*/30 * * * *\" — каждые 30 минут, \"0 16 * * 1-5\" — по будням в 16:00 "
            "(ЦБ публикует курс на завтра около 15:30)"
        ),
    ],
) -> ScheduledJob:
    """Завести задание: сервер сам по расписанию собирает официальные курсы ЦБ и хранит их.

    Сбор идёт на сервере без участия клиента; собранное отдаёт get_summary, ход заданий —
    list_schedules. Каждый запуск берёт самый свежий установленный курс; одна и та же дата
    установления хранится один раз.
    """
    хранилище, планировщик = _задания()
    коды = list(dict.fromkeys(к.strip().upper() for к in currencies if к.strip()))
    if not коды:
        raise ToolError("currencies: нужен хотя бы один код валюты")
    сейчас = _сейчас()
    try:
        срок = scheduler.next_run(cron, сейчас)
    except ValueError as exc:
        raise ToolError(f"cron: {exc}") from None
    сегодня = сейчас.date()
    валюты = _валюты(await _ежедневные(сегодня))
    for код in коды:
        if код not in валюты:
            raise _не_найдена(код, сегодня, валюты)
    задание = хранилище.add_job(коды, cron, срок, сейчас)
    планировщик.wake()
    return _задание(задание)


@srv.tool()
async def list_schedules() -> Schedules:
    """Задания сбора курсов: расписание, ближайший срок, удачные и неудачные запуски."""
    хранилище, _ = _задания()
    return Schedules(jobs=[_задание(з) for з in хранилище.jobs()])


@srv.tool()
async def delete_schedule(
    job_id: Annotated[int, Field(description="Номер задания из list_schedules")],
) -> Deleted:
    """Снять задание сбора. Собранные им курсы остаются и входят в get_summary."""
    хранилище, планировщик = _задания()
    if not хранилище.delete_job(job_id):
        raise ToolError(f"нет задания {job_id}; list_schedules покажет, какие есть")
    планировщик.wake()
    return Deleted(job_id=job_id)


@srv.tool()
async def get_summary(
    currency: Annotated[str, Field(description="Код валюты ISO 4217: USD, EUR, CNY…")],
    date_from: Annotated[
        str | None, Field(description="Начало отбора ГГГГ-ММ-ДД; не задано — с первого курса")
    ] = None,
    date_to: Annotated[
        str | None, Field(description="Конец отбора ГГГГ-ММ-ДД; не задано — по последний")
    ] = None,
) -> RateSummary:
    """Сводка по курсам, которые сервер собрал по расписанию: изменение, минимум, максимум, среднее.

    Считается только по собранному заданиями schedule_collect; к ЦБ сводка не обращается.
    """
    хранилище, _ = _задания()
    начало = _дата(date_from, "date_from") if date_from else None
    конец = _дата(date_to, "date_to") if date_to else None
    if начало and конец and начало > конец:
        raise ToolError(f"date_from {начало.isoformat()} позже date_to {конец.isoformat()}")
    код = currency.strip().upper()
    с = хранилище.summary(код, начало, конец)
    if с is None:
        raise ToolError(
            f"нет собранных курсов {код}"
            + (f" с {начало.isoformat()}" if начало else "")
            + (f" по {конец.isoformat()}" if конец else "")
            + "; заведите сбор schedule_collect"
        )
    return RateSummary(
        code=с.code,
        name=с.name,
        date_from=начало.isoformat() if начало else None,
        date_to=конец.isoformat() if конец else None,
        count=с.count,
        first=_точка(с.first),
        last=_точка(с.last),
        change=с.change,
        change_pct=с.change_pct,
        min=_точка(с.min),
        max=_точка(с.max),
        mean=с.mean,
        last_seen_at=с.last_seen_at.isoformat(),
    )


# --- приложение HTTP с замком --------------------------------------------------------------


class _Замок:
    """Прослойка ASGI: HTTP-запрос без `Authorization: Bearer <токен>` получает 401.

    Проверка в самой службе, а не во входном прокси (Caddy): тайна живёт в одном месте — в
    `.env` службы, а не ещё и в конфигурации прокси. Сравнение `hmac.compare_digest` — время ответа не выдаёт, сколько
    знаков токена совпало. Запросы жизненного цикла (`lifespan`) идут насквозь: без них не
    поднимется менеджер сеансов библиотеки.
    """

    def __init__(self, приложение: Any, токен: str) -> None:
        self._приложение = приложение
        self._токен = токен.encode()

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            заголовки = dict(scope.get("headers") or [])
            схема, _, пришёл = заголовки.get(b"authorization", b"").partition(b" ")
            # Имя схемы по RFC 9110 без учёта регистра; сам токен — побайтно.
            if схема.lower() != b"bearer" or not hmac.compare_digest(пришёл.strip(), self._токен):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [
                            (b"www-authenticate", b"Bearer"),
                            (b"content-type", b"text/plain; charset=utf-8"),
                        ],
                    }
                )
                await send({"type": "http.response.body", "body": "нужен токен".encode()})
                return
        await self._приложение(scope, receive, send)


def приложение(токен: str, имена: list[str]) -> _Замок:
    """Приложение ASGI сервера: MCP по пути `/mcp` за замком.

    `имена` — допустимые значения заголовка `Host` (защита от подмены имени узла). Через Caddy
    приходит адрес сервера, при местном запуске — `127.0.0.1:<порт>`; всё прочее библиотека
    отклоняет.
    """
    if not токен:
        raise ValueError(f"пустой токен: задайте {ПЕРЕМЕННАЯ_ТОКЕНА}")
    основа = srv.streamable_http_app(
        stateless_http=True,
        json_response=True,
        transport_security=TransportSecuritySettings(allowed_hosts=имена, allowed_origins=[]),
    )
    return _Замок(основа, токен)


def main() -> None:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--host", default="127.0.0.1")
    разбор.add_argument("--port", type=int, default=8770)
    разбор.add_argument(
        "--allowed-host",
        action="append",
        default=[],
        help="допустимое значение заголовка Host; повторяемый",
    )
    доводы = разбор.parse_args()
    токен = os.environ.get(ПЕРЕМЕННАЯ_ТОКЕНА, "").strip()
    if not токен:
        # Служба без замка хуже, чем служба, которая не поднялась.
        sys.exit(f"не задан {ПЕРЕМЕННАЯ_ТОКЕНА}: без токена сервер не запускается")
    имена = [*доводы.allowed_host, f"{доводы.host}:{доводы.port}"]
    база = Path(os.environ.get(ПЕРЕМЕННАЯ_БАЗЫ) or Path(__file__).with_name("cbr.sqlite3"))
    asyncio.run(_служба(приложение(токен, имена), доводы.host, доводы.port, база))


async def _служба(прил: _Замок, host: str, port: int, база: Path) -> None:
    """Сервер MCP и планировщик в одном процессе.

    Процесс ровно один: второй рабочий процесс uvicorn запустил бы планировщик дважды. Упал
    планировщик — останавливается и сервер, и процесс выходит с ошибкой: служба без сбора,
    которая молча отвечает на вызовы, хуже перезапуска systemd.
    """
    global _хранилище, _планировщик
    _хранилище = Store(база)
    _планировщик = scheduler.Scheduler(_хранилище, _сейчас, _собрать)
    сервер = uvicorn.Server(uvicorn.Config(прил, host=host, port=port))
    сбор = asyncio.create_task(_планировщик.run_forever())
    сбор.add_done_callback(lambda _: setattr(сервер, "should_exit", True))
    try:
        await сервер.serve()
    finally:
        сбор.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await сбор
        _хранилище.close()


if __name__ == "__main__":
    main()
