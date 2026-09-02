"""Экраны: отдельная лента вывода на каждого собеседника.

До появления группы агентов лента была одна на всё приложение. Теперь экранов несколько:
нулевой — главный (диалог пользователя), остальные заводятся под агентов. Экран хранит всё,
что делает ленту самостоятельной: сам вывод, число строк для автопрокрутки, признак «следим
за концом», историю сообщений и состояние занятости — по нему полоса вкладок показывает,
кто ещё думает, а кто уже ответил.

Экраны агентов работают только на просмотр: писать в них пользователь не может, ввод всегда
уходит в главный экран.
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
class Screen:
    key: str  # "main" либо имя профиля агента — по нему экран находят повторно
    title: str
    log: Fragments = field(default_factory=list)
    line_count: int = 0
    autoscroll: bool = True
    messages: list[dict] = field(default_factory=list)
    status: str = IDLE
    profile: Profile | None = None  # у агента — его профиль: постановка задачи и параметры

    @property
    def is_agent(self) -> bool:
        return self.key != MAIN_KEY


def main_screen() -> Screen:
    return Screen(key=MAIN_KEY, title="главный")
