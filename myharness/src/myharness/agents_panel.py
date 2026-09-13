"""Список агентов под строкой ввода и команда `/team`, с которой группа начинается.

Список — не отдельное хранилище, а обход дерева экранов: агенты живут в панелях, и второй
список рядом с ними разошёлся бы с ними на первом же экране, заведённом мимо него. Отсюда и
соседство с `/team`: команда поднимает группу исполнителей, а список — то место, где человек
эту группу видит и по ней ходит.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import profiles, ui
from . import screens as screens_mod
from .agent import Agent
from .output import append_log, refresh
from .panes import switch_pane, switch_screen
from .state import Request, State
def cmd_team(state: State, arg: str) -> None:
    """Разовый запуск группы. Без аргумента — показывает состав; с вопросом — задаёт его
    группе. Первым словом можно назвать профиль-ведущего: так группу поднимают, не уходя
    с обычного профиля."""
    text = arg.strip()
    lead = state.profile
    if text:
        parts = text.split(maxsplit=1)
        known = {name for name, _ in profiles.available()}
        if len(parts) > 1 and parts[0] in known:
            candidate, warnings = profiles.load(parts[0])
            for warning in warnings:
                append_log(state, ui.error_fragments(warning))
            if candidate.agents:
                lead, text = candidate, parts[1]
    if not lead.agents:
        append_log(state, ui.error_fragments(f"в профиле «{lead.name}» группа не задана"))
        append_log(state, ui.hint_fragments("группу задаёт поле agents профиля: /profile <имя-ведущего>"))
        return
    if not text:
        append_log(state, ui.team_list_fragments(lead.name, lead.agents))
        return
    if not state.config.is_authorized:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return
    append_log(state, ui.user_fragments(text), state.main.first)
    was_busy = state.busy or not state.queue.empty()
    state.queue.put_nowait(Request(content=text, pane=state.main.first, lead=lead))
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()), state.main.first)


@dataclass
class AgentRow:
    """Один агент в списке под строкой ввода: сам собеседник и адрес его панели.

    Адрес храним номерами экрана и панели, а не ссылкой на них: переход делают
    `switch_screen` и `switch_pane`, а они работают именно по номерам."""

    agent: Agent
    pane: screens_mod.Pane
    screen_index: int
    pane_index: int
    status: str
    occupation: str  # чем занят: начало вопроса, пока работает, иначе имя профиля


def collect_agents(state: State) -> list[AgentRow]:
    """Все поднятые агенты — в том же порядке, в каком идут экраны и панели на них.

    Порядок не косметика: строка списка и есть адрес агента, и переставь список агентов
    по-своему — номер строки перестал бы что-либо значить, а ↑/↓ водили бы не туда.

    Панель без собеседника пропускаем. Сейчас такой нет — панель заводит агента сама, как
    только у неё есть профиль, — но список не то место, где стоит падать: его открывают,
    когда с агентами уже что-то не так."""
    rows: list[AgentRow] = []
    for screen_index, screen in enumerate(state.screens):
        for pane_index, pane in enumerate(screen.panes):
            if pane.agent is None:
                continue
            rows.append(
                AgentRow(
                    agent=pane.agent,
                    pane=pane,
                    screen_index=screen_index,
                    pane_index=pane_index,
                    status=pane.status,
                    occupation=ui.agent_occupation(pane.status, pane.agent.task, pane.agent.profile.name),
                )
            )
    return rows


def agent_index(state: State, rows: list[AgentRow]) -> int:
    """Строка, на которой стоит пользователь: экран, который открыт, и панель, которая
    выбрана на нём. Не нашли — считаем первой: строка «вы здесь» в списке обязана быть."""
    current = state.screen.pane
    for index, row in enumerate(rows):
        if row.pane is current:
            return index
    return 0


def goto_agent(state: State, index: int) -> None:
    """Перейти на экран и панель агента по номеру строки списка."""
    rows = collect_agents(state)
    if not 0 <= index < len(rows):
        return
    row = rows[index]
    switch_screen(state, row.screen_index)
    switch_pane(state, row.pane_index)
    refresh(state)


def show_agent_panel(state: State) -> bool:
    """Показывать ли список агентов. Пока агент один, списка нет: строка «main» в одиночестве
    ничего не сообщает и только съедает высоту экрана."""
    return len(collect_agents(state)) > 1


def step_agent(state: State, delta: int) -> None:
    """Соседний агент по списку, по кругу. Клавиши ↑ и ↓ ведут ровно туда же, куда щелчок
    мышью по строке: два пути к одному месту, а не два разных поведения."""
    rows = collect_agents(state)
    if len(rows) < 2:
        return
    goto_agent(state, (agent_index(state, rows) + delta) % len(rows))
