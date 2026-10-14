"""Проверки сервера `support`.

Протокол проверяется настоящим клиентом MCP (`mcp.Client`) поверх сервера, поднятого в
процессе: подделка ответов проверила бы подделку, а не схему доводов и `structuredContent`.
Данные — копия настоящего `данные.json` во временном каталоге (`SUPPORT_DATA`): сценарий дня
опирается на них, а проверки записи меняют файл. Вход задаёт `SUPPORT_USER` в окружении процесса
проверки — сервер читает его при каждом вызове.

Запуск: `uv run python check_server.py` из каталога сервера. Каждая проверка печатает «ок» или
«ПРОВАЛ»; код выхода 1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from mcp import Client

import server
import store

провалов = 0


def проверить(название: str, условие: bool, подробно: str = "") -> None:
    global провалов
    if условие:
        print(f"ок      {название}")
    else:
        провалов += 1
        print(f"ПРОВАЛ  {название}{': ' + подробно if подробно else ''}")


def текст(итог) -> str:
    return " ".join(getattr(ч, "text", "") for ч in итог.content)


async def главная() -> None:
    async with Client(server.srv) as к:
        инструменты = (await к.list_tools()).tools
        имена = {и.name for и in инструменты}
        проверить("шесть инструментов: три чтения и три записи",
                  имена == {"get_ticket", "find_user", "list_tickets", "create_ticket", "add_message", "set_status"},
                  str(имена))

        # Вход задаёт SUPPORT_USER — переменная читается при каждом вызове.
        os.environ["SUPPORT_USER"] = "U-01"
        свой = await к.call_tool("get_ticket", {"ticket_id": "t-1001"})
        д = свой.structured_content or {}
        проверить("Анна (U-01): свой T-1001 (номер строчными) отдан, ошибка ключа DeepSeek",
                  not свой.is_error and д.get("user_id") == "U-01" and "DeepSeek" in д.get("error", ""), текст(свой))
        чужой = await к.call_tool("get_ticket", {"ticket_id": "T-1002"})
        проверить("Анна (U-01): чужой T-1002 — ошибка «тикет T-1002 не ваш», данных Бориса нет",
                  чужой.is_error and "тикет T-1002 не ваш" in текст(чужой) and "Ollama" not in текст(чужой), текст(чужой))
        нет = await к.call_tool("get_ticket", {"ticket_id": "T-9999"})
        проверить("T-9999: ошибка «тикет T-9999 не найден»",
                  нет.is_error and "тикет T-9999 не найден" in текст(нет), текст(нет))

        # Пара к чужому тикету: под Борисом тот же T-1002 отдаётся.
        os.environ["SUPPORT_USER"] = "U-02"
        у_бориса = await к.call_tool("get_ticket", {"ticket_id": "T-1002"})
        проверить("Борис (U-02): свой T-1002 отдан, ошибка службы Ollama",
                  "Ollama не отвечает" in (у_бориса.structured_content or {}).get("error", ""), текст(у_бориса))

        # Контекст пользователя — только вошедший, доводов о личности у инструментов нет.
        os.environ["SUPPORT_USER"] = "u-03"
        вера = (await к.call_tool("find_user", {})).structured_content or {}
        проверить("Вера (u-03 строчными): find_user отдаёт U-03 с незаданным OLLAMA_API_KEY",
                  вера.get("user_id") == "U-03" and вера.get("env", {}).get("OLLAMA_API_KEY") == "не задан", str(вера))
        список = await к.call_tool("list_tickets", {})
        тикеты = (список.structured_content or {}).get("tickets", [])
        проверить("list_tickets Веры: ровно закрытый T-1003",
                  [(т["ticket_id"], т["status"]) for т in тикеты] == [("T-1003", "закрыт")], str(тикеты))
        доводы = {и.name: set((и.input_schema or {}).get("properties", {})) for и in инструменты}
        проверить("у find_user и list_tickets нет доводов, у get_ticket — только ticket_id",
                  {к: доводы[к] for к in ("find_user", "list_tickets", "get_ticket")}
                  == {"find_user": set(), "list_tickets": set(), "get_ticket": {"ticket_id"}}, str(доводы))

        # Входа нет — отказ во всём; неизвестный пользователь — тоже.
        os.environ.pop("SUPPORT_USER", None)
        for имя, доводы_вызова in (("find_user", {}), ("list_tickets", {}), ("get_ticket", {"ticket_id": "T-1001"})):
            итог = await к.call_tool(имя, доводы_вызова)
            проверить(f"без SUPPORT_USER {имя}: ошибка «вход не выполнен»",
                      итог.is_error and "вход не выполнен" in текст(итог), текст(итог))
        os.environ["SUPPORT_USER"] = "U-99"
        итог = await к.call_tool("find_user", {})
        проверить("SUPPORT_USER=U-99: ошибка «пользователь U-99 не найден»",
                  итог.is_error and "пользователь U-99 не найден" in текст(итог), текст(итог))

        await запись(к)

    проверить_store()


async def запись(к: Client) -> None:
    """Инструменты записи — парами: свой тикет проходит, чужой отвергнут."""
    def тикет(номер: str) -> dict:
        return next(т for т in store.прочитать()["tickets"] if т["ticket_id"] == номер)

    os.environ["SUPPORT_USER"] = "U-01"
    свой = await к.call_tool("add_message", {"ticket_id": "T-1001", "text": "Проверьте ключ в кабинете."})
    сообщения = тикет("T-1001")["messages"]
    проверить("add_message в свой T-1001: сообщение ассистента в конце переписки",
              not свой.is_error and сообщения[-1]["author"] == "ассистент"
              and сообщения[-1]["text"] == "Проверьте ключ в кабинете.", текст(свой))
    было = len(тикет("T-1002")["messages"])
    чужой = await к.call_tool("add_message", {"ticket_id": "T-1002", "text": "чужое"})
    проверить("add_message в чужой T-1002: «не ваш», переписка Бориса не тронута",
              чужой.is_error and "не ваш" in текст(чужой) and len(тикет("T-1002")["messages"]) == было, текст(чужой))
    чужой = await к.call_tool("set_status", {"ticket_id": "T-1002", "status": "закрыт", "resolution": "x"})
    проверить("set_status чужого T-1002: «не ваш», статус прежний",
              чужой.is_error and "не ваш" in текст(чужой) and тикет("T-1002")["status"] == "открыт", текст(чужой))

    без = await к.call_tool("set_status", {"ticket_id": "T-1001", "status": "закрыт"})
    проверить("set_status «закрыт» без resolution: ошибка, статус прежний",
              без.is_error and тикет("T-1001")["status"] == "открыт", текст(без))
    с = await к.call_tool("set_status", {"ticket_id": "T-1001", "status": "закрыт", "resolution": "ключ заменён"})
    проверить("set_status «закрыт» с resolution: закрыт, причина записана",
              not с.is_error and (тикет("T-1001")["status"], тикет("T-1001")["resolution"]) == ("закрыт", "ключ заменён"),
              текст(с))
    снова = await к.call_tool("set_status", {"ticket_id": "T-1001", "status": "ждёт специалиста"})
    проверить("set_status «ждёт специалиста»: статус сменён, resolution очищен",
              not снова.is_error and (тикет("T-1001")["status"], тикет("T-1001")["resolution"]) == ("ждёт специалиста", ""),
              текст(снова))
    странный = await к.call_tool("set_status", {"ticket_id": "T-1001", "status": "решён"})
    проверить("set_status «решён»: статус неизвестен", странный.is_error and "неизвестен" in текст(странный), текст(странный))

    os.environ["SUPPORT_USER"] = "U-03"
    первый = await к.call_tool("create_ticket", {"subject": "Нет ответа", "text": "Молчит", "error": "  "})
    второй = await к.call_tool("create_ticket", {"subject": "Ещё", "text": "Снова молчит"})
    п, в = первый.structured_content or {}, второй.structured_content or {}
    проверить("create_ticket под U-03: T-1006, затем T-1007, оба Веры и «открыт»",
              (п.get("ticket_id"), в.get("ticket_id")) == ("T-1006", "T-1007")
              and п.get("user_id") == в.get("user_id") == "U-03" and п.get("status") == "открыт" and п.get("error") == "",
              f"{п} {в}")
    пустой = await к.call_tool("create_ticket", {"subject": "  ", "text": "x"})
    проверить("create_ticket с пустой темой: ошибка, тикета нет",
              пустой.is_error and len(store.прочитать()["tickets"]) == 7, текст(пустой))
    список = (await к.call_tool("list_tickets", {})).structured_content or {}
    проверить("list_tickets Веры видит свои новые тикеты",
              [т["ticket_id"] for т in список.get("tickets", [])][:2] in (["T-1007", "T-1006"], ["T-1006", "T-1007"]),
              str(список))


def проверить_store() -> None:
    анна = store.войти("  анна   смирнова ")
    проверить("войти «анна смирнова»: U-01", анна["user_id"] == "U-01", str(анна))
    ирина = store.войти("Ирина")
    снова = store.войти("ирина")
    проверить("войти «Ирина»: заведена U-06 с пустым окружением; «ирина» — та же U-06",
              ирина["user_id"] == снова["user_id"] == "U-06" and ирина["env"] == {}
              and sum(1 for п in store.прочитать()["users"] if п["name"] == "Ирина") == 1, f"{ирина} {снова}")
    try:
        store.войти("   ")
        проверить("войти с пустым именем: ValueError", False)
    except ValueError:
        проверить("войти с пустым именем: ValueError", True)

    # Одновременность: 20 потоков дописывают по сообщению — ни одно не теряется.
    def дописать(н: int) -> None:
        def правка(д: dict) -> None:
            т = next(т for т in д["tickets"] if т["ticket_id"] == "T-1004")
            т["messages"].append({"author": "ассистент", "at": "", "text": f"н{н}"})
        store.изменить(правка)

    with ThreadPoolExecutor(20) as пул:
        list(пул.map(дописать, range(20)))
    текстов = {с["text"] for с in next(т for т in store.прочитать()["tickets"] if т["ticket_id"] == "T-1004")["messages"]}
    проверить("20 одновременных правок: все 20 сообщений на месте",
              {f"н{н}" for н in range(20)} <= текстов, str(len(текстов)))

    def сломать(д: dict) -> None:
        д["users"].clear()
        raise RuntimeError("обрыв правки")
    try:
        store.изменить(сломать)
    except RuntimeError:
        pass
    проверить("исключение в правке: файл прежний",
              len(store.прочитать()["users"]) == 6 and not list(store.путь().parent.glob("*.tmp")))

    # Сбой посреди записи временного файла: `set` не пишется в JSON, исключение после `mkstemp`.
    def не_записать(д: dict) -> None:
        д["users"].append({"странное": {1, 2}})
    try:
        store.изменить(не_записать)
    except TypeError:
        pass
    проверить("сбой записи временного файла: файл прежний, временного нет",
              len(store.прочитать()["users"]) == 6 and not list(store.путь().parent.glob("*.tmp")))


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as каталог:
        копия = Path(каталог) / "данные.json"
        shutil.copy(store.ДАННЫЕ_ПО_УМОЛЧАНИЮ, копия)
        os.environ[store.ПЕРЕМЕННАЯ_ДАННЫХ] = str(копия)
        # Предусловие проверок записи: пишем в копию, а не в данные сценария.
        assert store.путь() == копия and json.loads(копия.read_text("utf-8"))["tickets"]
        asyncio.run(главная())
    sys.exit(1 if провалов else 0)
