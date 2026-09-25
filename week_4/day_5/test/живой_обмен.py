"""Живой обмен: одна просьба человека — модель сама проводит цепочку через четыре сервера MCP.

Без терминального интерфейса — тот же `Agent.exchange` и тот же набор инструментов MCP
(`mcp_tools.Соединения`), что у собеседника главного экрана. Серверы и разрешения берутся из
`.mcp.json` и `.claude/settings.json` текущего каталога, профиль `справочник` — из `profiles/`.

Своей сверки нет: след обмена ложится в `myharness-journal.jsonl`, и сверяет его `след.py` —
одинаково для прогона этим скриптом и прогона в окне `myharness`.

Запуск из этого каталога (токен сервера ЦБ — с VPS, на Маке не хранится):
    export CBR_MCP_TOKEN=$(ssh challenge-mcp "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")
    bash подготовить.sh
    uv run --project ~/challenge/main/myharness python живой_обмен.py "просьба"
    python3 след.py
"""

import asyncio
import sys
from pathlib import Path

from myharness import api, config, mcp_tools, profiles
from myharness.agent import Agent


async def main(просьба: str) -> int:
    профиль, предупреждения = profiles.load("справочник")
    for п in предупреждения:
        print(f"! профиль: {п}")
    cfg = config.load()
    клиент = api.DeepSeekClient(cfg.api_key)
    соединения = mcp_tools.Соединения()
    агент = Agent("справочник", профиль, инструменты=lambda: соединения.набор(Path.cwd()))

    def событие(e: api.StreamEvent) -> None:
        if e.kind == "tool_calls":
            for вызов in e.calls:
                функция = вызов["function"]
                print(f"→ {функция['name']} {функция['arguments']}")
        elif e.kind == "tool_result":
            print(f"← {e.text[:300]}{'…' if len(e.text) > 300 else ''}")

    try:
        print(f"ПРОСЬБА: {просьба}")
        ход = await агент.exchange(клиент, cfg.model, просьба, on_event=событие)
        if агент.store_error:
            print(f"! жалоба: {агент.store_error}")
        print("ОТВЕТ МОДЕЛИ:\n" + (ход.text if ход.ok else f"ошибка: {ход.error}"))
    finally:
        await соединения.закрыть()
        await клиент.aclose()
    return 0 if ход.ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]))))
