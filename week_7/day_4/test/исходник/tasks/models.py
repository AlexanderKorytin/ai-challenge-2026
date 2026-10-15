"""Задача списка дел."""

from dataclasses import dataclass


@dataclass
class Task:
    """Одна задача: номер, текст и отметка о выполнении."""

    id: int
    title: str
    done: bool = False
