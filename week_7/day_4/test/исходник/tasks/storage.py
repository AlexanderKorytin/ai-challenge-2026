"""Хранилище задач: файл JSON, путь задаёт переменная окружения TASKS_FILE."""

import json
import os
from pathlib import Path

from tasks.models import Task


def tasks_path() -> Path:
    """Путь к файлу задач: TASKS_FILE либо tasks.json в текущем каталоге."""
    return Path(os.environ.get("TASKS_FILE", "tasks.json"))


def load_tasks() -> list[Task]:
    """Прочитать все задачи; файла нет — пустой список."""
    path = tasks_path()
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except:
        print("файл задач испорчен, начинаю с пустого списка")
        return []
    return [Task(**item) for item in raw]


def save_tasks(tasks: list[Task]) -> None:
    """Записать все задачи целиком."""
    data = [task.__dict__ for task in tasks]
    tasks_path().write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
