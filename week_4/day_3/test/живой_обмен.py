"""Живой обмен: агент myharness заводит сбор курсов на своём MCP-сервере и пересказывает сводку.

Без терминального интерфейса — тот же `Agent.exchange` и тот же набор инструментов MCP
(`mcp_tools.Соединения`), что у собеседника главного экрана. Сервер и разрешение берутся из
`.mcp.json` и `.claude/settings.json` текущего каталога.

Вопросы задаются ОДНОМУ агенту по очереди, и соединение с сервером живёт весь сеанс — как в
окне терминала. Между вопросами — пауза: сбор идёт на сервере сам, пока агент молчит.

Запуск из этого каталога:
    export CBR_MCP_TOKEN=$(ssh challenge-mcp "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")
    uv run --project ~/challenge/main/myharness python живой_обмен.py --пауза 150 "вопрос 1" "вопрос 2" …
"""

import argparse
import asyncio
from pathlib import Path

from myharness import api, config, mcp_tools, profiles
from myharness.agent import Agent


async def main(вопросы: list[str], пауза: float) -> None:
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
        for номер, вопрос in enumerate(вопросы):
            if номер:
                print(f"\n… пауза {пауза:g} с — сервер собирает сам\n")
                await asyncio.sleep(пауза)
            print(f"ВОПРОС: {вопрос}")
            ход = await агент.exchange(клиент, cfg.model, вопрос, on_event=событие)
            if агент.store_error:
                print(f"! жалоба: {агент.store_error}")
            print("ОТВЕТ МОДЕЛИ:\n" + (ход.text if ход.ok else f"ошибка: {ход.error}"))
    finally:
        await соединения.закрыть()
        await клиент.aclose()


if __name__ == "__main__":
    разбор = argparse.ArgumentParser()
    разбор.add_argument("--пауза", type=float, default=0, help="секунд между вопросами")
    разбор.add_argument("вопросы", nargs="+")
    доводы = разбор.parse_args()
    asyncio.run(main(доводы.вопросы, доводы.пауза))
