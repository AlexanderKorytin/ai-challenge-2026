"""Проверки сервера `cbr` без сети.

Обращение к ЦБ подменяется образцами из `образцы/`, снятыми с `www.cbr.ru` 2026-09-22. Протокол
проверяется настоящим клиентом MCP поверх приложения, поднятого в процессе: подделка ответов
проверила бы подделку, а не схему доводов, `structuredContent` и замок.

Запуск: `uv run python check_server.py`. Каждая проверка печатает «ок» или «ПРОВАЛ»; код выхода
1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import server
from mcp.server.mcpserver.exceptions import ToolError

ОБРАЗЦЫ = Path(__file__).parent / "образцы"
ТОКЕН = "check-token-0123"
АДРЕС = "http://127.0.0.1:8770/mcp"
провалы: list[str] = []


def check(имя: str, условие: bool, подробно: object = "") -> None:
    print(("ок      " if условие else "ПРОВАЛ  ") + имя + (f" — {подробно}" if not условие else ""))
    if not условие:
        провалы.append(имя)


# Ответы ЦБ по доводам запроса; чего нет в словаре — пустой ответ, как у ЦБ на будущую дату.
ОТВЕТЫ = {
    ("XML_daily.asp", "01/09/2026"): "daily_2026-09-01.xml",
    ("XML_daily.asp", "20/09/2026"): "daily_2026-09-20.xml",
    ("XML_daily.asp", "05/09/2026"): "daily_2026-09-01.xml",
    ("XML_dynamic.asp", "01/09/2026"): "dynamic_usd_2026-09-01_05.xml",
}
ПУСТОЙ = b'<?xml version="1.0" encoding="windows-1251"?><ValCurs Date="01.12.2099" name="x"></ValCurs>'
запросы: list[tuple[str, dict]] = []


async def поддельный_цб(путь: str, доводы: dict[str, str]) -> bytes:
    запросы.append((путь, доводы))
    ключ = доводы.get("date_req") or доводы.get("date_req1")
    имя = ОТВЕТЫ.get((путь, ключ))
    return (ОБРАЗЦЫ / имя).read_bytes() if имя else ПУСТОЙ


async def ошибка_ожидаемая(сопрограмма) -> str:
    try:
        await сопрограмма
    except ToolError as exc:
        return str(exc)
    return ""


async def инструменты() -> None:
    server._получить = поддельный_цб

    usd = await server.get_rate("usd", "2026-09-01")
    check("курс USD на 01.09 разобран", usd.value == 86.3793 and usd.nominal == 1, usd)
    check("код приведён к верхнему регистру", usd.code == "USD" and usd.name == "Доллар США", usd)
    check("дата установления = запрошенной в рабочий день", usd.rate_date == usd.requested_date == "2026-09-01", usd)
    eur = await server.get_rate("EUR", "2026-09-01")
    check("парная: EUR в том же ответе — другой курс", eur.code == "EUR" and eur.value != usd.value, eur)

    вс = await server.get_rate("USD", "2026-09-20")
    check(
        "воскресенье: rate_date — дата, названная ЦБ, а не запрошенная",
        вс.requested_date == "2026-09-20" and вс.rate_date == "2026-09-19",
        вс,
    )
    dzd = await server.get_rate("DZD", "2026-09-01")  # номинал 100 у алжирского динара
    check("номинал 100: курс за единицу в сто раз меньше", abs(dzd.value / 100 - dzd.unit_rate) < 1e-6 and dzd.nominal == 100, dzd)

    текст = await ошибка_ожидаемая(server.get_rate("XXX", "2026-09-01"))
    check("неизвестный код — ошибка со списком известных", "нет курса XXX" in текст and "USD" in текст, текст)
    текст = await ошибка_ожидаемая(server.get_rate("USD", "2099-12-01"))
    check("дальняя дата, ЦБ дал пустой список — ошибка", "не дал курсов" in текст, текст)

    # Близкая будущая дата: ЦБ отдаёт ПОСЛЕДНИЙ курс под его датой (образец на 20.09 несёт
    # Date=19.09). «Сегодня» подменяется: 19.09 — запрос на 20.09 в будущем.
    настоящее_сегодня = server._сегодня
    server._сегодня = lambda: server.dt.date(2026, 9, 19)
    текст = await ошибка_ожидаемая(server.get_rate("USD", "2026-09-20"))
    check("близкая будущая дата — ошибка с последним курсом", "ещё не установлен" in текст and "2026-09-19" in текст, текст)
    server._сегодня = lambda: server.dt.date(2026, 9, 22)
    вс_прошлое = await server.get_rate("USD", "2026-09-20")
    check("парная: та же дата в прошлом (выходной) — курс пятницы", вс_прошлое.rate_date == "2026-09-19", вс_прошлое)
    server._сегодня = настоящее_сегодня
    текст = await ошибка_ожидаемая(server.get_rate("USD", "01.09.2026"))
    check("дата не в ГГГГ-ММ-ДД — ошибка с именем довода", текст.startswith("date:"), текст)

    запросы.clear()
    дин = await server.get_rate_dynamics("USD", "2026-09-01", "2026-09-05")
    check("динамика: точки по дням", [т.date for т in дин.points][:2] == ["2026-09-01", "2026-09-02"] and дин.points[0].value == 86.3793, дин)
    check(
        "динамика запрошена по коду ЦБ R01235, а не по ISO",
        any(п == "XML_dynamic.asp" and д.get("VAL_NM_RQ") == "R01235" for п, д in запросы),
        запросы,
    )
    настоящее_сегодня = server._сегодня
    server._сегодня = lambda: server.dt.date(2026, 9, 5)
    запросы.clear()
    будущий_конец = await server.get_rate_dynamics("USD", "2026-09-01", "2026-10-30")
    check(
        "динамика с концом в будущем: код валюты берётся на сегодня, точки есть",
        будущий_конец.points and any(д.get("date_req") == "05/09/2026" for _, д in запросы),
        запросы,
    )
    server._сегодня = настоящее_сегодня
    текст = await ошибка_ожидаемая(server.get_rate_dynamics("USD", "2026-09-05", "2026-09-01"))
    check("начало позже конца — ошибка", "позже" in текст, текст)


async def протокол() -> None:
    server._получить = поддельный_цб
    приложение = server.приложение(ТОКЕН, ["127.0.0.1:8770"])
    async with AsyncExitStack() as стек:
        # Транспорт ASGI не проводит жизненный цикл приложения — менеджер сеансов поднимается
        # здесь, как его поднял бы uvicorn.
        await стек.enter_async_context(server.srv.session_manager.run())
        транспорт = httpx2.ASGITransport(app=приложение)

        async def post(заголовки: dict[str, str]) -> int:
            async with httpx2.AsyncClient(transport=транспорт) as клиент:
                ответ = await клиент.post(
                    АДРЕС,
                    headers={"accept": "application/json, text/event-stream", **заголовки},
                    json={"jsonrpc": "2.0", "id": 1, "method": "ping"},
                )
                return ответ.status_code

        check("без токена — 401", await post({}) == 401)
        check("чужой токен — 401", await post({"authorization": "Bearer other-token"}) == 401)
        check("парная: верный токен — не 401", await post({"authorization": f"Bearer {ТОКЕН}"}) != 401)
        check("схема bearer без учёта регистра", await post({"authorization": f"bearer {ТОКЕН}"}) != 401)
        check("токен без схемы — 401", await post({"authorization": ТОКЕН}) == 401)

        async with httpx2.AsyncClient(
            transport=транспорт, headers={"authorization": f"Bearer {ТОКЕН}"}
        ) as клиент:
            потоки = await стек.enter_async_context(streamable_http_client(АДРЕС, http_client=клиент))
            сессия = await стек.enter_async_context(ClientSession(потоки[0], потоки[1]))
            ответ = await сессия.initialize()
            check("согласование: сервер назвался cbr", ответ.server_info.name == "cbr", ответ.server_info)
            список = {t.name: t for t in (await сессия.list_tools()).tools}
            check("зарегистрированы два инструмента", sorted(список) == ["get_rate", "get_rate_dynamics"], sorted(список))
            доводы = список["get_rate"].input_schema.get("properties", {})
            check(
                "у каждого довода get_rate есть описание",
                set(доводы) == {"currency", "date"} and all(д.get("description") for д in доводы.values()),
                доводы,
            )
            check(
                "обязателен только currency",
                список["get_rate"].input_schema.get("required") == ["currency"],
                список["get_rate"].input_schema.get("required"),
            )
            check("у get_rate есть схема выхода с rate_date", "rate_date" in (список["get_rate"].output_schema or {}).get("properties", {}))
            итог = await сессия.call_tool("get_rate", {"currency": "USD", "date": "2026-09-20"})
            check(
                "вызов: structuredContent несёт rate_date",
                not итог.is_error and (итог.structured_content or {}).get("rate_date") == "2026-09-19",
                итог,
            )
            check("вызов: есть и текстовая часть", any(getattr(ч, "text", "") for ч in итог.content), итог.content)
            плохой = await сессия.call_tool("get_rate", {"currency": "XXX", "date": "2026-09-01"})
            check(
                "ошибка инструмента доходит как isError с причиной",
                плохой.is_error and "нет курса XXX" in " ".join(getattr(ч, "text", "") for ч in плохой.content),
                плохой,
            )


def без_токена() -> None:
    try:
        server.приложение("", ["x"])
    except ValueError:
        check("пустой токен — приложение не собирается", True)
    else:
        check("пустой токен — приложение не собирается", False)


async def main() -> None:
    await инструменты()
    await протокол()
    без_токена()


asyncio.run(main())
print(f"\nпровалов: {len(провалы)}")
sys.exit(1 if провалы else 0)
