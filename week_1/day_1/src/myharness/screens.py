"""Экраны и панели: как вывод раскладывается по вкладкам и внутри вкладки.

До появления группы агентов лента вывода была одна на всё приложение. Теперь их несколько, и
устроены они двумя уровнями:

* **экран** — вкладка внизу окна. Нулевой экран главный (диалог пользователя), остальные
  заводит профиль: под агента, под шаг приёма или под способ решения;
* **панель** — лента внутри экрана. Обычно панель одна и экран выглядит как раньше. Но
  способу, у которого несколько исполнителей, панелей нужно столько же: цепочка показывает
  слева составленный промпт, справа решение по нему, а группа экспертов — по панели на
  эксперта, чтобы ответы читались рядом, а не подряд.

Экраны бывают двух родов. Экран агента — только на просмотр: писать в него пользователь не
может, ввод уходит в главный. Рабочий экран (профиль со списком `screens`) ввод принимает:
на нём ведётся своя ветка разговора со своей инструкцией.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # только для подсказок типов — на импорт профилей экраны не завязаны
    from .profiles import Profile

Fragments = list[tuple[str, str]]

MAIN_KEY = "main"

IDLE = "idle"
BUSY = "busy"
DONE = "done"
ERROR = "error"


@dataclass
class Pane:
    """Лента вывода: то, что раньше было экраном целиком."""

    key: str
    title: str = ""
    log: Fragments = field(default_factory=list)
    line_count: int = 0
    autoscroll: bool = True
    status: str = IDLE
    profile: Profile | None = None  # чей это вывод: инструкция исполнителя и его параметры
    messages: list[dict] = field(default_factory=list)


@dataclass
class Screen:
    key: str  # "main" либо имя профиля — по нему экран находят повторно
    title: str
    panes: list[Pane] = field(default_factory=list)
    profile: Profile | None = None
    interactive: bool = False  # рабочий экран принимает ввод; экран агента — только чтение
    active_pane: int = 0
    zoomed: bool = False  # одна панель развёрнута на весь экран

    def __post_init__(self) -> None:
        if not self.panes:
            self.panes = [Pane(key=self.key, profile=self.profile)]

    @property
    def pane(self) -> Pane:
        """Панель, на которую сейчас смотрит пользователь: её прокручивают и разворачивают."""
        index = self.active_pane if 0 <= self.active_pane < len(self.panes) else 0
        return self.panes[index]

    @property
    def first(self) -> Pane:
        return self.panes[0]

    @property
    def is_agent(self) -> bool:
        return self.key != MAIN_KEY

    @property
    def status(self) -> str:
        """Состояние экрана — худшее из состояний его панелей: пока думает хоть одна,
        вкладка показывает «занят», а один сбой не должен теряться за успехами соседей."""
        states = {pane.status for pane in self.panes}
        for state in (BUSY, ERROR, IDLE):
            if state in states:
                return state
        return DONE

    def pane_by_key(self, key: str) -> Pane | None:
        for pane in self.panes:
            if pane.key == key:
                return pane
        return None


def main_screen() -> Screen:
    return Screen(key=MAIN_KEY, title="главный", interactive=True)


def build_messages(profile: Profile, pane: Pane, content: str) -> list[dict]:
    """Сообщения одного запроса: инструкция исполнителя, его история (если профиль её держит)
    и новый ввод. Одинаково для агента группы и для рабочего экрана — разница только в том,
    кто набирает текст."""
    messages: list[dict] = []
    if profile.system:
        messages.append({"role": "system", "content": profile.system})
    if profile.keep_history:
        pane.messages.append({"role": "user", "content": content})
        messages.extend(pane.messages)
    else:
        messages.append({"role": "user", "content": content})
    return messages
