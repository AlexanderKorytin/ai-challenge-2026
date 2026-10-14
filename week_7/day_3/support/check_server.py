"""Проверки сервера `support`.

Протокол проверяется настоящим клиентом MCP (`mcp.Client`) поверх сервера, поднятого в
процессе: подделка ответов проверила бы подделку, а не схему доводов и `structuredContent`.
Данные — настоящий `данные.json`: сценарий дня опирается именно на них. Вход задаёт
`SUPPORT_USER` в окружении процесса проверки — сервер читает его при каждом вызове.

Запуск: `uv run python check_server.py` из каталога сервера. Каждая проверка печатает «ок» или
«ПРОВАЛ»; код выхода 1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
import os
import sys

from mcp import Client

import server

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
        проверить("три инструмента: get_ticket, find_user, list_tickets",
                  имена == {"get_ticket", "find_user", "list_tickets"}, str(имена))

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
                  доводы == {"find_user": set(), "list_tickets": set(), "get_ticket": {"ticket_id"}}, str(доводы))

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

    sys.exit(1 if провалов else 0)


if __name__ == "__main__":
    asyncio.run(главная())
