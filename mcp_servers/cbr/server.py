"""MCP-сервер официальных курсов валют Центрального банка РФ.

Обёртка над голым XML-API ЦБ (`www.cbr.ru/scripts/`): два инструмента — курс на дату и курс по
дням за период. Регистрирует их декоратор `@srv.tool()`, описание доводов берётся из подписи
функции (`Annotated[..., Field(description=...)]`), а возврат — модель данных, из которой
библиотека строит и текст ответа, и `structuredContent` со схемой выхода.

Работает по Streamable HTTP за проверкой токена `Authorization: Bearer …`. Сеансов на сервере
нет (`stateless_http`): перезапуск службы не рвёт клиента, у которого соединение живёт весь
сеанс.

Запуск: `CBR_MCP_TOKEN=… uv run python server.py --port 8770 --allowed-host koritin84.fvds.ru`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import hmac
import os
import sys
import xml.etree.ElementTree as ET
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import httpx2
import uvicorn
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, Field

ИСТОЧНИК = "https://www.cbr.ru/scripts/"
ПЕРЕМЕННАЯ_ТОКЕНА = "CBR_MCP_TOKEN"
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


def _сегодня() -> dt.date:
    return dt.datetime.now(МОСКВА).date()


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


# --- сервер и инструменты ------------------------------------------------------------------

srv = MCPServer(
    "cbr",
    instructions="Официальные курсы иностранных валют к рублю, установленные ЦБ РФ.",
)


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


# --- приложение HTTP с замком --------------------------------------------------------------


class _Замок:
    """Прослойка ASGI: HTTP-запрос без `Authorization: Bearer <токен>` получает 401.

    Проверка в самой службе, а не в nginx: тайна не должна лежать в конфигурации, которую
    ведёт панель хостинга. Сравнение `hmac.compare_digest` — время ответа не выдаёт, сколько
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

    `имена` — допустимые значения заголовка `Host` (защита от подмены имени узла). Через nginx
    приходит имя сайта, при местном запуске — `127.0.0.1:<порт>`; всё прочее библиотека
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
    uvicorn.run(приложение(токен, имена), host=доводы.host, port=доводы.port)


if __name__ == "__main__":
    main()
