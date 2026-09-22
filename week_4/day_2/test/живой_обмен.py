"""Живой обмен: агент myharness вызывает инструмент своего MCP-сервера курсов ЦБ на VPS.

Без терминального интерфейса — тот же `Agent.exchange` и тот же набор инструментов MCP
(`mcp_tools.Соединения`), что у собеседника главного экрана. Сервер и разрешение берутся из
`.mcp.json` и `.claude/settings.json` текущего каталога.

Запуск из этого каталога:
    export CBR_MCP_TOKEN=$(ssh challenge "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")
    uv run --project ~/challenge/main/myharness python живой_обмен.py "вопрос"
"""

import asyncio
import sys
from pathlib import Path

from myharness import api, config, mcp_tools, profiles
from myharness.agent import Agent


async def main(вопрос: str) -> None:
    cfg = config.load()
    клиент = api.DeepSeekClient(cfg.api_key)
    соединения = mcp_tools.Соединения()
    агент = Agent(
        "живой-обмен",
        profiles.Profile(name="живой-обмен", keep_history=True),
        инструменты=lambda: соединения.набор(Path.cwd()),
    )

    def событие(e: api.StreamEvent) -> None:
        if e.kind == "tool_calls":
            for вызов in e.calls:
                функция = вызов["function"]
                print(f"→ вызов {функция['name']} {функция['arguments']}")
        elif e.kind == "tool_result":
            print(f"← результат {e.text}")

    try:
        ход = await агент.exchange(клиент, cfg.model, вопрос, on_event=событие)
        if агент.store_error:
            print(f"! жалоба: {агент.store_error}")
        print("\nОТВЕТ МОДЕЛИ:\n" + (ход.text if ход.ok else f"ошибка: {ход.error}"))
    finally:
        await соединения.закрыть()
        await клиент.aclose()


if __name__ == "__main__":
    asyncio.run(main(" ".join(sys.argv[1:]) or "Какой сегодня курс доллара?"))
