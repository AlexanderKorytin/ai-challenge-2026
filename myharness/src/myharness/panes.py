"""Куда сейчас смотрит человек: активный экран, активная панель и черновик её ввода.

Экраны и панели как устройство описаны в `screens`; здесь — переходы между ними и то, что
обязано случиться при каждом переходе. Черновик ввода один на окно (буфер `prompt_toolkit`
один), а панелей много: поэтому при уходе черновик покидаемой панели откладывается в неё, а
при приходе — достаётся из новой. Пропусти одно из двух — и набранный вопрос молча уедет в
чужую вкладку.

Про раскладку окна модуль не знает: он двигает состояние и просит перерисовку — через
`output.refresh`, а где нужна одна лишь перерисовка окна, напрямую у приложения.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from . import ui
from . import screens as screens_mod
from .output import append_log, refresh
from .profiles import Profile
from .state import State
from .workers import PaneWorker

if TYPE_CHECKING:  # только подсказка типа: агентов модуль не заводит, а лишь провожает
    from .agent import Agent
def active_profile(state: State) -> Profile:
    """Профиль панели, с которой человек сейчас работает."""

    pane = _active_input_pane(state)
    return pane.profile or state.profile


def _active_input_pane(state: State) -> screens_mod.Pane:
    """Панель, чей черновик сейчас показан в общей строке ввода.

    Экран агента служит только для чтения, поэтому строка под ним по-прежнему принадлежит
    главной панели: набранное там не должно исчезать при возврате.
    """
    return state.screen.pane if state.screen.interactive else state.main.first


def _save_active_draft(state: State) -> None:
    pane = _active_input_pane(state)
    if state.input_buffer is not None:
        pane.draft = state.input_buffer.text


def _restore_active_draft(state: State) -> None:
    if state.input_buffer is None:
        return
    pane = _active_input_pane(state)
    state.input_buffer.text = pane.draft
    state.input_buffer.cursor_position = len(pane.draft)


def _pane_profile(state: State, pane: screens_mod.Pane) -> Profile | None:
    if pane is state.main.first:
        return state.profile
    return pane.profile or state.screen.profile


def switch_screen(state: State, index: int) -> None:
    if not 0 <= index < len(state.screens) or index == state.active:
        return
    _save_active_draft(state)
    state.active = index
    screen = state.screen
    for pane in screen.panes:
        pane.autoscroll = True  # переключились — показываем свежий конец ленты
    pane = _active_input_pane(state)
    _restore_active_draft(state)
    profile = _pane_profile(state, pane)
    if profile is not None:
        apply_prefill(state, profile, pane)
    refresh(state)


def switch_pane(state: State, index: int) -> None:
    """Панели внутри экрана перебираются по кругу: их немного, и так не надо целиться."""
    screen = state.screen
    if len(screen.panes) < 2:
        return
    new_index = index % len(screen.panes)
    if new_index == screen.active_pane:
        return
    _save_active_draft(state)
    screen.active_pane = new_index
    screen.pane.autoscroll = True
    _restore_active_draft(state)
    pane = _active_input_pane(state)
    profile = _pane_profile(state, pane)
    if profile is not None:
        apply_prefill(state, profile, pane)
    refresh(state)


def toggle_zoom(state: State) -> None:
    """Развернуть активную панель на весь экран и обратно: в сетке ответ читается по
    диагонали, а вчитаться иногда нужно."""
    screen = state.screen
    if len(screen.panes) < 2:
        return
    screen.zoomed = not screen.zoomed
    refresh(state)


async def drop_agent_screens(state: State) -> None:
    """Закончить работу закрываемых экранов, учесть расход и только затем удалить их.

    Смена профиля не отменяет уже оплаченный вопрос и не переносит его поздний ответ в
    новый профиль. Сначала каждый исполнитель перестаёт принимать новые единицы, затем
    дорабатывает текущую и всю свою очередь. После этого его окончательный расход можно
    перенести в копилку, а ссылки на панели — удалить без риска повторного `id`.
    """

    closing_screens = list(state.screens[1:])
    closing_panes = [pane for screen in closing_screens for pane in screen.panes]
    closing_workers: list[PaneWorker] = []
    for pane in closing_panes:
        executor = state.pane_workers.get(id(pane))
        if executor is not None and executor.pane is pane:
            executor.accepting = False
            closing_workers.append(executor)
    await asyncio.gather(*(executor.drain_and_close() for executor in closing_workers))

    closing_agents: list[Agent] = []
    for pane in closing_panes:
        if pane.agent is not None and not any(pane.agent is item for item in closing_agents):
            closing_agents.append(pane.agent)
    state.retire(closing_agents)

    for pane in closing_panes:
        key = id(pane)
        executor = state.pane_workers.get(key)
        if executor is not None and executor.pane is pane:
            del state.pane_workers[key]
    if state.active != 0:
        switch_screen(state, 0)
    del state.screens[1:]


def apply_prefill(state: State, profile: Profile, pane: screens_mod.Pane) -> None:
    """Один раз завести очередь заготовок конкретной панели.

    Заготовки кладём в строку ввода, а не отправляем сами: человек видит текст, может его
    поправить и отправляет сам — Enter'ом. Одиночная `prefill` проходит тем же путём, что
    список `prefills`, как очередь длиной один.
    """
    if pane.prefill_initialized:
        return
    pane.prefill_initialized = True
    очередь = list(profile.prefills) or ([profile.prefill.strip()] if profile.prefill else [])
    if not очередь:
        return
    pane.prefill_queue = очередь
    сказать = (
        "заготовка вопроса подставлена в строку ввода — Enter отправит её"
        if len(очередь) == 1
        else f"вопросы профиля ({len(очередь)}) пойдут по очереди — Enter отправляет и подставляет следующий"
    )
    append_log(state, ui.system_fragments(сказать), pane)
    next_prefill(state, pane)


def next_prefill(state: State, pane: screens_mod.Pane) -> None:
    """Поставить следующий вопрос очереди в черновик только указанной панели."""
    if _active_input_pane(state) is pane:
        _save_active_draft(state)
    if not pane.prefill_queue or pane.draft.strip():
        return
    pane.draft = pane.prefill_queue.pop(0)
    if _active_input_pane(state) is pane:
        _restore_active_draft(state)
        if state.app is not None:
            state.app.invalidate()
