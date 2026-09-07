"""Вывод в ленту, отделённый от интерфейса.

Раньше эти функции жили в `cli.py` вместе с разметкой окна, состоянием и обработкой клавиш.
Из-за этого оркестраторы `team` и `methods` — которым от `cli` нужна была одна лишь
`append_log` — импортировали его лениво, прямо в телах функций: обычный импорт замкнул бы
круг, ведь сам `cli` импортирует и `team`, и `methods`.

Здесь этого круга нет: модуль знает только про экраны (`screens`) и оформление (`ui`), а про
`cli`, `prompt_toolkit` и оркестраторы — ничего. Поэтому импортировать его можно обычным
образом, из любого места пакета.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from . import screens as screens_mod
from . import ui

if TYPE_CHECKING:  # только для подсказок типов — на импорт cli вывод не завязан
    from .cli import State

Fragments = list[tuple[str, str]]


def target_pane(state: State, target: screens_mod.Screen | screens_mod.Pane | None) -> screens_mod.Pane:
    """Куда писать. Без указания — первая панель экрана, где пользователь работает; экран
    вместо панели тоже принимается: у большинства экранов панель одна."""
    if isinstance(target, screens_mod.Pane):
        return target
    if isinstance(target, screens_mod.Screen):
        return target.first
    return state.focus.first


def append_log(
    state: State, fragments: Fragments, target: screens_mod.Screen | screens_mod.Pane | None = None
) -> None:
    pane = target_pane(state, target)
    pane.log.extend(fragments)
    pane.line_count += sum(text.count("\n") for _, text in fragments)
    if state.app is not None:
        state.app.invalidate()


def truncate_log(
    state: State, mark: int, target: screens_mod.Screen | screens_mod.Pane | None = None
) -> None:
    pane = target_pane(state, target)
    removed = pane.log[mark:]
    pane.line_count -= sum(text.count("\n") for _, text in removed)
    del pane.log[mark:]
    if state.app is not None:
        state.app.invalidate()


def refresh(state: State) -> None:
    if state.app is not None:
        state.app.invalidate()


def warn_journal(state: State, error: str | None) -> None:
    """Сказать пользователю, что журнал прогонов не пишется, — не больше одного раза за сеанс.

    Сбой журнала harness не роняет (требование `openspec/specs/journal/spec.md`), а причина у
    него обычно постоянная: нет прав на файл, кончилось место. Без флага `journal_warned` то же
    самое сообщение повторялось бы после каждого следующего запроса и вытеснило бы из ленты
    сами ответы.

    Записью в журнал модуль не занимается: сюда приходит уже готовая ошибка или её отсутствие.
    """
    if error and not state.journal_warned:
        state.journal_warned = True
        append_log(state, ui.error_fragments(error))
