"""Сводка по задачам."""

import json
from pathlib import Path

from tasks.storage import load_tasks


def summary() -> str:
    tasks = load_tasks()
    done = sum(1 for task in tasks if task.done)
    return f"всего {len(tasks)}, сделано {done}, осталось {len(tasks) - done}"


def raw_count() -> int:
    """Число записей в файле без разбора в задачи."""
    return len(json.loads(Path("tasks.json").read_text(encoding="utf-8")))
