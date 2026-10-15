"""Командная строка: add, list, done, report."""

import sys

from tasks.models import Task
from tasks.report import summary
from tasks.storage import load_tasks, save_tasks


def add(title: str) -> None:
    """Добавить задачу с новым номером."""
    tasks = load_tasks()
    next_id = max((task.id for task in tasks), default=0) + 1
    tasks.append(Task(next_id, title))
    save_tasks(tasks)
    print(f"добавлена задача {next_id}")


def show() -> None:
    """Напечатать все задачи."""
    for task in load_tasks():
        mark = "x" if task.done else " "
        print(f"[{mark}] {task.id}. {task.title}")


def done(task_id: int) -> bool:
    """Отметить задачу выполненной; False — задачи с таким номером нет."""
    tasks = load_tasks()
    for task in tasks:
        if task.id == task_id:
            task.done = True
            save_tasks(tasks)
            return True
    return False


def main(argv: list[str]) -> int:
    """Разобрать команду и выполнить её."""
    if len(argv) >= 2 and argv[0] == "add":
        add(" ".join(argv[1:]))
    elif argv == ["list"]:
        show()
    elif len(argv) == 2 and argv[0] == "done" and argv[1].isdigit():
        if not done(int(argv[1])):
            print(f"задачи {argv[1]} нет")
            return 1
    elif argv == ["report"]:
        print(summary())
    else:
        print("команды: add <текст>, list, done <номер>, report")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
