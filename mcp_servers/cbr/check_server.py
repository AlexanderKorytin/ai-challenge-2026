"""Проверки сервера `cbr` без сети.

Обращение к ЦБ подменяется образцами из `образцы/`, снятыми с `www.cbr.ru` 2026-09-22. Протокол
проверяется настоящим клиентом MCP поверх приложения, поднятого в процессе: подделка ответов
проверила бы подделку, а не схему доводов, `structuredContent` и замок.

Запуск: `uv run python check_server.py`. Каждая проверка печатает «ок» или «ПРОВАЛ»; код выхода
1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import sys
import tempfile
from contextlib import AsyncExitStack
from pathlib import Path

import httpx2
from mcp.client.session import ClientSession
from mcp.client.streamable_http import streamable_http_client

import scheduler
import server
from mcp.server.mcpserver.exceptions import ToolError
from store import Observed, Store

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
    # Сбор по расписанию запрашивает завтрашнюю дату; ответ на 02.09 — образец на 01.09, как
    # ЦБ отдаёт последний установленный курс, пока нового нет.
    ("XML_daily.asp", "02/09/2026"): "daily_2026-09-01.xml",
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


МОСКВА = server.МОСКВА


def в(час: int, минута: int, секунда: int = 0) -> dt.datetime:
    return dt.datetime(2026, 9, 1, час, минута, секунда, tzinfo=МОСКВА)


class Часы:
    def __init__(self, сейчас: dt.datetime) -> None:
        self.сейчас = сейчас

    def __call__(self) -> dt.datetime:
        return self.сейчас


def поднять(каталог: str, часы: Часы) -> tuple[Store, scheduler.Scheduler]:
    """Хранилище во временном каталоге и планировщик на поддельных часах — как их ставит main()."""
    хранилище = Store(Path(каталог) / "вложенный" / "cbr.sqlite3")
    планировщик = scheduler.Scheduler(хранилище, часы, server._собрать)
    server._хранилище, server._планировщик = хранилище, планировщик
    server._сейчас = часы
    return хранилище, планировщик


async def задания() -> None:
    server._получить = поддельный_цб
    настоящее_сейчас = server._сейчас
    with tempfile.TemporaryDirectory() as каталог:
        часы = Часы(в(10, 1))
        хранилище, планировщик = поднять(каталог, часы)

        з = await server.schedule_collect(["usd", "EUR", "usd"], "*/2 * * * *")
        check("задание: коды в верхнем регистре без повторов", з.currencies == ["USD", "EUR"], з)
        check("задание: ближайший срок по cron — 10:02 по Москве", з.next_run == в(10, 2).isoformat(), з)
        текст = await ошибка_ожидаемая(server.schedule_collect(["USD"], "каждые 2 минуты"))
        check("негодный cron — ошибка с именем довода", текст.startswith("cron:"), текст)
        for выражение in ("* * * * * *", "@hourly"):
            текст = await ошибка_ожидаемая(server.schedule_collect(["USD"], выражение))
            check(f"cron {выражение!r} не из пяти полей — ошибка", текст.startswith("cron:"), текст)
        текст = await ошибка_ожидаемая(server.schedule_collect(["XXX"], "*/2 * * * *"))
        check("неизвестная валюта — ошибка со списком известных", "нет курса XXX" in текст and "USD" in текст, текст)
        текст = await ошибка_ожидаемая(server.schedule_collect([" "], "*/2 * * * *"))
        check("пустой список валют — ошибка", текст.startswith("currencies:"), текст)
        check("парная: негодные задания не записаны", len(хранилище.jobs()) == 1, хранилище.jobs())

        часы.сейчас = в(10, 1, 59)
        await планировщик.tick()
        check("до срока задание не выполняется", хранилище.jobs()[0].runs_ok == 0, хранилище.jobs())
        часы.сейчас = в(10, 2)
        запросы.clear()
        await планировщик.tick()
        [з] = хранилище.jobs()
        check("в срок: запуск удачен", з.runs_ok == 1 and з.last_error is None, з)
        check("в срок: один запрос к ЦБ на завтра для всех валют", [д.get("date_req") for _, д in запросы] == ["02/09/2026"], запросы)
        check("в срок: следующий срок — 10:04", з.next_run == в(10, 4), з)
        usd = хранилище.summary("USD", None, None)
        check("курс USD сохранён с датой, названной ЦБ", usd is not None and usd.count == 1 and usd.last.rate_date == dt.date(2026, 9, 1) and usd.last.unit_rate == 86.3793, usd)
        check("курс EUR сохранён", хранилище.summary("EUR", None, None) is not None)
        check("не заказанная валюта не сохраняется", хранилище.summary("CNY", None, None) is None)

        часы.сейчас = в(10, 4)
        await планировщик.tick()
        [з] = хранилище.jobs()
        check("повторный сбор того же курса: запуск удачен", з.runs_ok == 2, з)
        check("повторный сбор того же курса: новых строк нет", хранилище.summary("USD", None, None).count == 1)

        # Служба лежала: пропущены сроки 10:06, 10:08 и 10:10.
        часы.сейчас = в(10, 11)
        check("предусловие: пропущено больше одного срока", хранилище.jobs()[0].next_run <= в(10, 6))
        await планировщик.tick()
        [з] = хранилище.jobs()
        check("пропущенные сроки — один запуск", з.runs_ok == 3, з)
        check("пропущенные сроки — следующий срок после «сейчас»", з.next_run == в(10, 12), з)

        async def цб_лежит(путь: str, доводы: dict[str, str]) -> bytes:
            raise ToolError("нет связи с ЦБ: ConnectError")

        server._получить = цб_лежит
        часы.сейчас = в(10, 12)
        await планировщик.tick()
        [з] = хранилище.jobs()
        check("сбой ЦБ — запуск с ошибкой, текст сохранён", з.runs_failed == 1 and "нет связи" in (з.last_error or ""), з)
        check("сбой ЦБ — задание живо, срок сдвинут", з.next_run == в(10, 14), з)
        server._получить = поддельный_цб
        часы.сейчас = в(10, 14)
        await планировщик.tick()
        check("после удачного запуска последняя ошибка снята", хранилище.jobs()[0].last_error is None, хранилище.jobs())

        чужое = хранилище.add_job(["USD", "ZZZ"], "*/2 * * * *", в(10, 16), в(10, 15))
        часы.сейчас = в(10, 16)
        await планировщик.tick()
        з = next(з for з in хранилище.jobs() if з.id == чужое.id)
        check("валюты нет в ответе ЦБ — ошибка с её кодом", з.runs_failed == 1 and "ZZZ" in (з.last_error or ""), з)
        хранилище.delete_job(чужое.id)

        список = await server.list_schedules()
        check("list_schedules отдаёт задание со счётом запусков", len(список.jobs) == 1 and список.jobs[0].runs_ok == 5 and список.jobs[0].runs_failed == 1, список)
        снято = await server.delete_schedule(з_id := список.jobs[0].id)
        check("delete_schedule снимает задание", снято.job_id == з_id and хранилище.jobs() == [], хранилище.jobs())
        текст = await ошибка_ожидаемая(server.delete_schedule(з_id))
        check("снять несуществующее — ошибка", f"нет задания {з_id}" in текст, текст)
        часы.сейчас = в(12, 0)
        запросы.clear()
        await планировщик.tick()
        check("снятое задание не выполняется", запросы == [], запросы)
        check("курсы снятого задания остались", хранилище.summary("USD", None, None) is not None)

        # Долгий цикл спит без срока, пока нет заданий, и просыпается по wake().
        цикл = asyncio.create_task(планировщик.run_forever())
        await asyncio.sleep(0.05)
        check("предусловие: без заданий цикл не ходил к ЦБ", запросы == [], запросы)
        новое = await server.schedule_collect(["USD"], "0 16 * * *")
        check("номер снятого задания не достаётся новому", новое.id > з_id and новое.runs_ok == 0, новое)
        хранилище.set_next_run(новое.id, в(11, 0))  # срок уже прошёл
        планировщик.wake()
        await asyncio.sleep(0.05)
        цикл.cancel()
        check("wake() будит цикл, и он выполняет задание в срок", хранилище.jobs()[0].runs_ok == 1, хранилище.jobs())
        хранилище.close()
    server._сейчас = настоящее_сейчас


def курс(день: int, за_единицу: float) -> Observed:
    return Observed("USD", "Доллар США", dt.date(2026, 9, день), 1, за_единицу, за_единицу)


async def сводка() -> None:
    настоящее_сейчас = server._сейчас
    with tempfile.TemporaryDirectory() as каталог:
        хранилище, _ = поднять(каталог, Часы(в(10, 0)))
        курсы = [курс(1, 80.0), курс(2, 90.0), курс(3, 85.0)]
        check("три курса записаны", хранилище.add_rates(курсы, в(10, 0), 1) == 3)
        check("повтор тех же курсов — ноль новых", хранилище.add_rates(курсы, в(11, 0), 1) == 0)
        с = await server.get_summary("usd")
        check(
            "сводка: число, края, изменение",
            (с.count, с.first.date, с.last.date, с.change) == (3, "2026-09-01", "2026-09-03", 5.0),
            с,
        )
        check("сводка: изменение в процентах от первого", abs(с.change_pct - 6.25) < 1e-9, с.change_pct)
        check(
            "сводка: минимум, максимум с датами, среднее",
            (с.min.date, с.min.unit_rate, с.max.date, с.max.unit_rate, с.mean) == ("2026-09-01", 80.0, "2026-09-02", 90.0, 85.0),
            с,
        )
        check("сводка: время сбора — первое, повтор его не сдвинул", с.last_seen_at == в(10, 0).isoformat(), с.last_seen_at)
        отбор = await server.get_summary("USD", "2026-09-02")
        check("отбор с 02.09 отсекает первый курс", отбор.count == 2 and отбор.first.date == "2026-09-02" and отбор.date_from == "2026-09-02", отбор)
        отбор = await server.get_summary("USD", None, "2026-09-02")
        check("отбор по 02.09 отсекает последний курс", отбор.count == 2 and отбор.last.date == "2026-09-02", отбор)
        текст = await ошибка_ожидаемая(server.get_summary("USD", "2026-10-01"))
        check("пустой отбор — ошибка с подсказкой завести сбор", "нет собранных курсов USD с 2026-10-01" in текст and "schedule_collect" in текст, текст)
        текст = await ошибка_ожидаемая(server.get_summary("USD", "2026-09-03", "2026-09-01"))
        check("начало отбора позже конца — ошибка", "позже" in текст, текст)
        хранилище.close()
    server._сейчас = настоящее_сейчас


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
            check(
                "зарегистрированы шесть инструментов",
                sorted(список) == ["delete_schedule", "get_rate", "get_rate_dynamics", "get_summary", "list_schedules", "schedule_collect"],
                sorted(список),
            )
            check(
                "у каждого довода новых инструментов есть описание",
                all(
                    д.get("description")
                    for имя in ("schedule_collect", "delete_schedule", "get_summary")
                    for д in список[имя].input_schema.get("properties", {}).values()
                ),
            )
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
            with tempfile.TemporaryDirectory() as каталог:
                настоящее_сейчас = server._сейчас
                хранилище, _ = поднять(каталог, Часы(в(10, 1)))
                итог = await сессия.call_tool("schedule_collect", {"currencies": ["USD"], "cron": "*/2 * * * *"})
                check(
                    "вызов schedule_collect: structuredContent несёт срок",
                    not итог.is_error and (итог.structured_content or {}).get("next_run") == в(10, 2).isoformat(),
                    итог,
                )
                хранилище.close()
                server._сейчас = настоящее_сейчас
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
    await задания()
    await сводка()
    await протокол()
    без_токена()


asyncio.run(main())
print(f"\nпровалов: {len(провалы)}")
sys.exit(1 if провалы else 0)
