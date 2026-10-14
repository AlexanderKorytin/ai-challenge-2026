"""Проверки сервера `support`.

Протокол проверяется настоящим клиентом MCP (`mcp.Client`) поверх сервера, поднятого в
процессе: подделка ответов проверила бы подделку, а не схему доводов и `structuredContent`.
Данные — настоящий `данные.json`: сценарий дня опирается именно на них.

Запуск: `uv run python check_server.py` из каталога сервера. Каждая проверка печатает «ок» или
«ПРОВАЛ»; код выхода 1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
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

        # Контекст тикета: соседние тикеты отдают разные ошибки — проверка не пустая.
        т1 = await к.call_tool("get_ticket", {"ticket_id": "T-1001"})
        т2 = await к.call_tool("get_ticket", {"ticket_id": "t-1002"})
        д1, д2 = т1.structured_content or {}, т2.structured_content or {}
        проверить("T-1001: автор U-01, ошибка ключа DeepSeek",
                  д1.get("user_id") == "U-01" and "DeepSeek" in д1.get("error", ""), текст(т1))
        проверить("T-1002 (номер строчными): автор U-02, ошибка службы Ollama",
                  д2.get("user_id") == "U-02" and "Ollama не отвечает" in д2.get("error", ""), текст(т2))
        проверить("у T-1001 и T-1002 ошибки разные", д1.get("error") != д2.get("error"))

        нет = await к.call_tool("get_ticket", {"ticket_id": "T-9999"})
        проверить("T-9999: ошибка «тикет T-9999 не найден»",
                  нет.is_error and "тикет T-9999 не найден" in текст(нет), текст(нет))

        # Контекст пользователя: по почте и по номеру — один человек.
        по_почте = await к.call_tool("find_user", {"email": "Vera@Example.com"})
        по_номеру = await к.call_tool("find_user", {"user_id": "U-03"})
        проверить("find_user по почте и по номеру отдаёт одного пользователя U-03",
                  (по_почте.structured_content or {}).get("user_id") == "U-03"
                  and по_почте.structured_content == по_номеру.structured_content, текст(по_почте))
        проверить("у U-03 OLLAMA_API_KEY не задан",
                  (по_номеру.structured_content or {}).get("env", {}).get("OLLAMA_API_KEY") == "не задан")

        пусто = await к.call_tool("find_user", {})
        проверить("find_user без доводов: ошибка «нужен user_id или email»",
                  пусто.is_error and "нужен user_id или email" in текст(пусто), текст(пусто))

        чужой = await к.call_tool("find_user", {"email": "nobody@example.com"})
        проверить("find_user с неизвестной почтой: ошибка «не найден»",
                  чужой.is_error and "не найден" in текст(чужой), текст(чужой))

        список = await к.call_tool("list_tickets", {"user_id": "U-03"})
        тикеты = (список.structured_content or {}).get("tickets", [])
        проверить("list_tickets U-03: ровно закрытый T-1003",
                  [(т["ticket_id"], т["status"]) for т in тикеты] == [("T-1003", "закрыт")], str(тикеты))

    sys.exit(1 if провалов else 0)


if __name__ == "__main__":
    asyncio.run(главная())
