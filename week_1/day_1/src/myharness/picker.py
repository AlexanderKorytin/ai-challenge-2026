"""Всплывающая панель выбора: стрелки — движение, Enter — применить, Esc — закрыть.

Используется и для выбора параметра, и для выбора его значения. Панель модальна: пока
она открыта, ввод в строку не проходит — иначе выбор стрелками и печать текста мешали бы
друг другу.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

Fragments = list[tuple[str, str]]


@dataclass
class Item:
    label: str
    hint: str = ""
    payload: Any = None


@dataclass
class Picker:
    title: str
    description: str
    items: list[Item]
    on_choose: Callable[[Any], None]
    index: int = 0
    footer: str = "↑↓ — выбор · Enter — применить · Esc — отмена"
    marked: int | None = None  # текущее значение параметра — помечаем точкой
    _width: int = field(default=0, init=False)

    def move(self, delta: int) -> None:
        if not self.items:
            return
        self.index = (self.index + delta) % len(self.items)

    def choose(self) -> None:
        if self.items:
            self.on_choose(self.items[self.index].payload)


def fragments(picker: Picker) -> Fragments:
    rows: list[tuple[str, str]] = []  # (метка, подсказка)
    for i, item in enumerate(picker.items):
        mark = "●" if picker.marked == i else " "
        rows.append((f"{mark} {item.label}", item.hint))

    label_width = max((len(label) for label, _ in rows), default=0)
    content_width = max(
        [len(picker.title), len(picker.description), len(picker.footer)]
        + [label_width + (len(hint) + 3 if hint else 0) for label, hint in rows]
    )

    def line(inner: Fragments) -> Fragments:
        used = sum(len(text) for _, text in inner)
        return (
            [("class:panel.border", "│ ")]
            + inner
            + [("class:panel", " " * max(0, content_width - used)), ("class:panel.border", " │\n")]
        )

    out: Fragments = [("class:panel.border", "╭─" + "─" * content_width + "─╮\n")]
    out += line([("class:panel.title", picker.title)])
    if picker.description:
        out += line([("class:panel.hint", picker.description)])
    out += [("class:panel.border", "├─" + "─" * content_width + "─┤\n")]
    for i, (label, hint) in enumerate(rows):
        selected = i == picker.index
        item_style = "class:panel.item.selected" if selected else "class:panel.item"
        hint_style = "class:panel.hint.selected" if selected else "class:panel.hint"
        inner: Fragments = [(item_style, label.ljust(label_width))]
        if hint:
            inner.append((hint_style, f"   {hint}"))
        out += line(inner)
    out += [("class:panel.border", "├─" + "─" * content_width + "─┤\n")]
    out += line([("class:panel.footer", picker.footer)])
    out += [("class:panel.border", "╰─" + "─" * content_width + "─╯")]
    return out
