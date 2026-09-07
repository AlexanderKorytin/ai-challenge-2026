"""Всплывающая панель выбора: стрелки — движение, Enter — применить, Esc — закрыть.

Используется и для выбора параметра, и для выбора его значения. Панель модальна: пока
она открыта, ввод в строку не проходит — иначе выбор стрелками и печать текста мешали бы
друг другу.

Строку можно выбрать и мышью: обработчик висит прямо на фрагменте текста строки — тем же
способом, каким кликаются вкладки. Как и там, мышь работает, только когда она отдана
harness (F2 или `/mouse`): по умолчанию она у терминала, чтобы текст выделялся обычным
образом.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.mouse_events import MouseEvent, MouseEventType

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


def _row_click(picker: Picker, index: int) -> Callable[[MouseEvent], Any]:
    """Клик по строке: подвести выбор к ней и сразу применить.

    Именно применить, а не только подсветить: панель модальна, и клик, который лишь
    переставил бы подсветку, оставлял бы пользователя дожимать Enter — то есть требовал бы
    и мыши, и клавиатуры там, где хватает одного движения."""

    def handler(mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            picker.index = index
            picker.choose()
            return None
        return NotImplemented  # прочие события мыши пусть обрабатывает prompt_toolkit

    return handler


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

    def line(inner: Fragments, click: Callable[[MouseEvent], Any] | None = None) -> Fragments:
        used = sum(len(text) for _, text in inner)
        pad = " " * max(0, content_width - used)
        if click is None:
            return (
                [("class:panel.border", "│ ")]
                + inner
                + [("class:panel", pad), ("class:panel.border", " │\n")]
            )
        # Обработчик вешаем и на отбивку справа: попасть мышью надо в строку, а не в буквы.
        return (
            [("class:panel.border", "│ ")]
            + [(style, text, click) for style, text in inner]
            + [("class:panel", pad, click), ("class:panel.border", " │\n")]
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
        out += line(inner, _row_click(picker, i))
    out += [("class:panel.border", "├─" + "─" * content_width + "─┤\n")]
    out += line([("class:panel.footer", picker.footer)])
    out += [("class:panel.border", "╰─" + "─" * content_width + "─╯")]
    return out
