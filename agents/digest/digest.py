"""Агент сводок: один запуск — одна сводка по курсам, которые собрал сервер `cbr`.

Работает 24/7 на VPS: его запускает таймер systemd `cbr-digest.timer` (как `cron` в Hermes —
задание по расписанию выполняет агент с инструментами). Каждый запуск:

1. агент DeepSeek (`myharness.Agent`) получает задание и сам вызывает инструменты чтения
   сервера — какие именно, решают правила `permissions.allow` в `.claude/settings.json` этого
   каталога; заводить и снимать задания сбора ему нельзя;
2. готовый текст сохраняет ПРОГРАММА вызовом `save_digest` — модель могла бы забыть это
   сделать, а сводка обязана лечь в базу. Сервер тот же, что в `.mcp.json`: второго адреса и
   второго способа читать токен нет.

Сбой обмена или сохранения — код выхода 1: systemd помечает запуск неудачным, и это видно в
`systemctl status cbr-digest`. Сводка без данных не сохраняется вовсе: если набор инструментов
не собрался (сервер ещё не поднялся после перезагрузки) или модель не вызвала ни одного
инструмента, её текст — не сводка, а догадка, и следующий запуск прочёл бы его как «прошлую
сводку».

Окружение: `DEEPSEEK_API_KEY`, `CBR_MCP_TOKEN` (служба берёт их из `/opt/challenge/.env`),
`DIGEST_MODEL` — модель, по умолчанию умолчание `myharness`.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from myharness import api, config, mcp_client, mcp_tools, profiles
from myharness.agent import Agent

КАТАЛОГ = Path(__file__).resolve().parent

ИНСТРУКЦИЯ = """Ты — агент, который по расписанию пишет сводку по официальным курсам валют ЦБ РФ.
Курсы собирает сервер cbr сам, по своим заданиям сбора. Ты видишь только то, что отдают
инструменты, и опираешься только на их ответы — ничего не додумываешь.

Порядок работы:
1. list_schedules — какие валюты собираются и нет ли у заданий ошибок;
2. get_summary по каждой собираемой валюте — изменение, минимум, максимум, среднее;
3. list_digests с limit=1 — твоя прошлая сводка, чтобы сказать, что изменилось с неё.

Ответ — только текст сводки по-русски, без вступлений: по каждой валюте последний курс с датой
установления и изменение за собранный период; затем что нового по сравнению с прошлой
сводкой; затем сбои сбора, если есть. Числа — как в ответах инструментов, округляя до копеек."""

ЗАДАНИЕ = "Составь очередную сводку по собранным курсам."


async def сохранить(текст: str) -> str:
    """Сохраняет сводку на сервере `cbr` из `.mcp.json` каталога агента; отдаёт ответ сервера."""
    записи = [з for з in mcp_client.прочитать(КАТАЛОГ / ".mcp.json") if isinstance(з, mcp_client.Сервер)]
    сервер = next((з for з in записи if з.имя == "cbr"), None)
    if сервер is None:
        raise RuntimeError(f"в {КАТАЛОГ / '.mcp.json'} нет годной записи сервера cbr")
    async with mcp_client.соединение(сервер) as (сессия, _):
        итог = await сессия.call_tool("save_digest", {"text": текст})
    ответ = " ".join(getattr(ч, "text", "") for ч in итог.content)
    if итог.is_error:
        raise RuntimeError(f"сервер не сохранил сводку: {ответ}")
    return ответ


async def main() -> int:
    ключ = os.environ.get("DEEPSEEK_API_KEY", "").strip()
    if not ключ:
        print("не задан DEEPSEEK_API_KEY", file=sys.stderr)
        return 1
    клиент = api.DeepSeekClient(ключ)
    соединения = mcp_tools.Соединения()
    агент = Agent(
        "сводка",
        profiles.Profile(name="сводка", system=ИНСТРУКЦИЯ, keep_history=False),
        инструменты=lambda: соединения.набор(КАТАЛОГ),
    )

    вызовы = 0

    def событие(e: api.StreamEvent) -> None:
        nonlocal вызовы
        if e.kind == "tool_calls":
            вызовы += len(e.calls)
            for вызов in e.calls:
                функция = вызов["function"]
                print(f"→ {функция['name']} {функция['arguments']}", flush=True)

    try:
        ход = await агент.exchange(
            клиент, os.environ.get("DIGEST_MODEL") or config.DEFAULT_MODEL, ЗАДАНИЕ, on_event=событие
        )
        if ход.journal_error:
            print(f"журнал прогонов не записан: {ход.journal_error}", file=sys.stderr)
        # Хранилища, фактов и карточки у этого агента нет — жалоба может прийти только от
        # набора инструментов.
        if агент.store_error:
            print(f"инструменты недоступны, сводка не сохранена: {агент.store_error}", file=sys.stderr)
            return 1
        if not ход.ok or not ход.text.strip():
            print(f"обмен не удался: {ход.error or 'пустой ответ'}", file=sys.stderr)
            return 1
        if not вызовы:
            print("модель не вызвала ни одного инструмента — сводка не сохранена", file=sys.stderr)
            return 1
        print(ход.text, flush=True)
        print(f"сохранено: {await сохранить(ход.text)}", flush=True)
        return 0
    finally:
        await соединения.закрыть()
        await клиент.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
