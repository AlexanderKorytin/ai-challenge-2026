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

from .agent import Agent

if TYPE_CHECKING:  # только для подсказок типов — на импорт профилей экраны не завязаны
    from .profiles import Profile

Fragments = list[tuple[str, str]]

MAIN_KEY = "main"

# Имя стиля — договор между тем, кто пишет в ленту (`output`), и тем, кто её показывает
# (`Pane.visible_log`). Написанное словом в трёх местах, оно ломается опечаткой в одном:
# фрагмент перестанет попадать под отбор и размышления вылезут в свёрнутом виде.
REASONING = "class:reasoning"  # сам черновик модели — прячется
REASONING_HEAD = "class:reasoning.head"  # заголовок области — виден всегда

IDLE = "idle"
BUSY = "busy"
DONE = "done"
ERROR = "error"


@dataclass
class Pane:
    """Лента вывода: то, что раньше было экраном целиком.

    У панели есть собеседник (`agent`), потому что панель — это место отрисовки, а разговор
    ей не принадлежит: память и правила сборки запроса — дело агента, панель лишь показывает
    то, что из этого вышло."""

    key: str
    title: str = ""
    log: Fragments = field(default_factory=list)
    line_count: int = 0
    reasoning_lines: int = 0  # сколько строк ленты занято размышлениями
    show_reasoning: bool = False  # черновик модели длиннее ответа в разы — по умолчанию свёрнут
    autoscroll: bool = True
    status: str = IDLE
    profile: Profile | None = None  # чей это вывод: инструкция исполнителя и его параметры
    agent: Agent | None = None  # собеседник этой панели: его память, его инструкция

    def __post_init__(self) -> None:
        """Собеседника заводит сама панель, а не тот, кто её создаёт.

        Пока агентов расставляли вызывающие, панель, заведённая где-то ещё, оставалась без
        собеседника — и вскрывалось это не при создании, а при первом обращении, посреди
        разговора. Здесь забыть нельзя: есть профиль — есть и агент.

        Единственная панель без профиля — главный экран: там профиль пользовательский, он
        меняется командой `/profile`, и собеседника кладёт `cli.State`."""
        if self.profile is not None and self.agent is None:
            self.agent = Agent(self.key, self.profile)

    def visible_log(self) -> Fragments:
        """Лента такой, какой её видит пользователь: со свёрнутыми размышлениями или без.

        Прячем отбором, а не удалением: размышления остаются в `log` целиком, поэтому
        развернуть их можно в любой момент, в том числе спустя десяток обменов. Хранить
        два списка вместо одного не выйдет — они разъедутся на первой же обрезке ленты.

        Заголовок области (`class:reasoning.head`) виден всегда: свёрнутое, о котором
        нигде не сказано, неотличимо от несуществующего — человек решит, что модель
        не размышляла, хотя он за эти токены заплатил."""
        if self.show_reasoning:
            return self.log
        return [fragment for fragment in self.log if fragment[0] != REASONING]

    def visible_lines(self) -> int:
        """Сколько строк на самом деле показано. Нужно для положения курсора: по нему
        окно доматывает ленту вниз, и со скрытыми размышлениями `line_count` увёл бы
        прокрутку ниже последней видимой строки — конец ответа ушёл бы за край."""
        if self.show_reasoning:
            return self.line_count
        # `max` не про аккуратность, а про живучесть: курсор стоит ровно на последней
        # допустимой строке, запаса нет, и завышение хоть на единицу роняет отрисовку
        # обращением за край экрана.
        return max(0, self.line_count - self.reasoning_lines)


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
