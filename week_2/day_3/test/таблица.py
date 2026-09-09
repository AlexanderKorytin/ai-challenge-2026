"""Кривая роста расхода — из журнала прогонов, а не из самого инструмента.

Журнал заведён точкой развязки: сторонний наблюдатель читает его и строит что угодно, а
harness про этого наблюдателя ничего не знает. Эта таблица — и есть такой наблюдатель.

Деньги считаются ЗДЕСЬ, действующей таблицей тарифов, а не берутся из записей: в записях
денег нет намеренно — тариф меняется на стороне поставщика, а записи остаются навсегда.

Запуск: uv run таблица.py [файл журнала]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from myharness import tokens

ЖУРНАЛ = Path(sys.argv[1] if len(sys.argv) > 1 else "myharness-journal.jsonl")


def записи() -> list[dict]:
    if not ЖУРНАЛ.exists():
        sys.exit(f"журнала нет: {ЖУРНАЛ}")
    строки = []
    for строка in ЖУРНАЛ.read_text(encoding="utf-8").splitlines():
        if строка.strip():
            строки.append(json.loads(строка))
    return строки


def таблица(имя: str, прогон: list[dict]) -> None:
    print(f"\n{'═' * 96}\n{имя}: {len(прогон)} обменов\n")
    print(
        f"{'#':>3} {'вход':>7} {'из кэша':>8} {'выход':>7} {'рассужд.':>9} "
        f"{'память':>7} {'Σ сеанса':>9} {'деньги':>10} {'с':>6}"
    )
    первый_вход = None
    деньги = 0.0
    for запись in прогон:
        расход = tokens.normalize(запись.get("usage") or {})
        цена = tokens.price(запись.get("usage") or {}, запись.get("model", "")) or 0.0
        деньги += цена
        первый_вход = первый_вход or расход["prompt_tokens"]
        пометки = []
        if запись.get("over_budget"):
            пометки.append("сверх предела")
        if запись.get("restored_pairs"):
            пометки.append(f"поднято {запись['restored_pairs']} пар")
        if запись.get("finish_reason") == "length":
            пометки.append("оборван по max_tokens")
        print(
            f"{запись.get('index', 0):>3} {расход['prompt_tokens']:>7} "
            f"{расход['prompt_cache_hit_tokens']:>8} {расход['completion_tokens']:>7} "
            f"{расход['reasoning_tokens']:>9} {запись.get('history_tokens', 0):>7} "
            f"{запись.get('session_tokens', 0):>9} {tokens.format_price(цена):>10} "
            f"{запись.get('elapsed_ms', 0) / 1000:>6.1f}"
            + ("   ← " + ", ".join(пометки) if пометки else "")
        )
    последний_вход = tokens.normalize(прогон[-1].get("usage") or {})["prompt_tokens"]
    итог = tokens.normalize(прогон[-1].get("usage") or {})
    print(
        f"\n  вход вырос с {первый_вход} до {последний_вход} — в {последний_вход / max(1, первый_вход):.1f} раза"
        f"\n  всего за прогон: {прогон[-1].get('session_tokens', 0)} токенов, {tokens.format_price(деньги)}"
    )
    if итог["prompt_cache_hit_tokens"]:
        доля = итог["prompt_cache_hit_tokens"] / max(1, итог["prompt_tokens"]) * 100
        print(f"  к концу разговора {доля:.0f}% входа шло из кэша — за это платят в тридцать раз меньше")


прогоны: dict[str, list[dict]] = {}
for запись in записи():
    прогоны.setdefault(запись.get("agent") or "без имени", []).append(запись)

for имя, прогон in прогоны.items():
    таблица(имя, прогон)
