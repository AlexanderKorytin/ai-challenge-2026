"""Журнал прогонов: одна строка JSON на каждый обмен с моделью.

Зачем: во-первых, потом видно, каким профилем и с какими параметрами получен любой ответ;
во-вторых, это точка развязки — сторонние наблюдатели (например, web-страница сравнения
ответов) читают журнал и не требуют ни строчки правок в самом harness.

Файл: `$MYHARNESS_JOURNAL` либо `./myharness-journal.jsonl` рядом с местом запуска.
API-ключ в журнал не попадает никогда.
"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

DEFAULT_FILENAME = "myharness-journal.jsonl"


def path() -> Path:
    env_path = os.environ.get("MYHARNESS_JOURNAL")
    return Path(env_path).expanduser() if env_path else Path.cwd() / DEFAULT_FILENAME


def append(entry: dict[str, Any]) -> str | None:
    """Дописывает запись. Возвращает текст ошибки — журнал никогда не роняет harness."""
    record = {"ts": datetime.now(UTC).isoformat(timespec="seconds"), **entry}
    target = path()
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    except OSError as exc:
        return f"не удалось записать журнал ({target}): {exc}"
    return None
