"""Живой обмен: одна просьба человека — модель сама проводит цепочку search → summarize → saveToFile.

Без терминального интерфейса — тот же `Agent.exchange` и тот же набор инструментов MCP
(`mcp_tools.Соединения`), что у собеседника главного экрана. Сервер и разрешения берутся из
`.mcp.json` и `.claude/settings.json` текущего каталога.

После ответа сверяется цепочка: вызовы шли по порядку, `search_id` на входе `summarize` — из
выхода `search`, `summary_id` на входе `saveToFile` — из выхода `summarize`, контрольная сумма
файла на диске равна `sha256` из ответа `saveToFile`, источники файла — статьи из `search`.

Сервер не перезаписывает файлы: перед повтором удалите `результаты/<имя>.md` или назовите в
просьбе новое имя, иначе лишний вызов даст «ПРОВАЛ» сверки порядка.

Запуск из этого каталога:
    uv run --project ~/challenge/main/myharness python живой_обмен.py "просьба"
"""

import asyncio
import hashlib
import json
import sys
from pathlib import Path

from myharness import api, config, mcp_tools, profiles
from myharness.agent import Agent


async def main(просьба: str) -> int:
    cfg = config.load()
    клиент = api.DeepSeekClient(cfg.api_key)
    соединения = mcp_tools.Соединения()
    агент = Agent(
        "живой-обмен",
        profiles.Profile(name="живой-обмен", keep_history=True),
        инструменты=lambda: соединения.набор(Path.cwd()),
    )
    вызовы: list[tuple[str, dict]] = []
    результаты: list[str] = []

    def событие(e: api.StreamEvent) -> None:
        if e.kind == "tool_calls":
            for вызов in e.calls:
                функция = вызов["function"]
                вызовы.append((функция["name"], json.loads(функция["arguments"] or "{}")))
                print(f"→ вызов {функция['name']} {функция['arguments']}")
        elif e.kind == "tool_result":
            результаты.append(e.text)
            print(f"← результат {e.text[:300]}{'…' if len(e.text) > 300 else ''}")

    try:
        print(f"ПРОСЬБА: {просьба}")
        ход = await агент.exchange(клиент, cfg.model, просьба, on_event=событие)
        if агент.store_error:
            print(f"! жалоба: {агент.store_error}")
        print("ОТВЕТ МОДЕЛИ:\n" + (ход.text if ход.ok else f"ошибка: {ход.error}"))
    finally:
        await соединения.закрыть()
        await клиент.aclose()
    return сверить(вызовы, результаты)


def сверить(вызовы: list[tuple[str, dict]], результаты: list[str]) -> int:
    print("\nСВЕРКА ЦЕПОЧКИ")
    провалы = 0

    def да(название: str, условие: bool) -> None:
        nonlocal провалы
        провалы += not условие
        print(f"{'ок    ' if условие else 'ПРОВАЛ'}  {название}")

    имена = [имя.removeprefix("mcp__pipeline__") for имя, _ in вызовы]
    да(f"вызовы по порядку: {' → '.join(имена)}", имена == ["search", "summarize", "saveToFile"])
    if len(вызовы) != 3 or len(результаты) != 3:
        return 1
    try:
        найдено, выжимка, сохранено = (json.loads(р) for р in результаты)
    except json.JSONDecodeError:
        да("результаты инструментов — JSON", False)
        return 1
    да("search_id: выход search = вход summarize", вызовы[1][1].get("search_id") == найдено["search_id"])
    да("summary_id: выход summarize = вход saveToFile", вызовы[2][1].get("summary_id") == выжимка["summary_id"])
    путь = Path(сохранено["path"])
    байты = путь.read_bytes() if путь.exists() else b""
    да(f"файл {путь} есть, sha256 совпадает", hashlib.sha256(байты).hexdigest() == сохранено["sha256"])
    текст = байты.decode("utf-8")
    да("текст выжимки в файле дословно", выжимка["text"] in текст)
    да("источники файла — статьи из search", all(f"[{р['title']}]({р['url']})" in текст for р in найдено["results"]))
    return 1 if провалы else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main(" ".join(sys.argv[1:]))))
