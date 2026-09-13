"""Клавиши: что происходит по каждому нажатию.

Отделены от раскладки намеренно. Раскладку меняют, когда на экране должно появиться что-то
новое; клавиши — когда меняется способ этим управлять. Пока и то и другое жило в одном
замыкании `build_app`, любая правка одного заставляла читать триста строк другого.

Обработчик берёт из `Раскладка` то, что принадлежит окну: буфер ввода, признак открытой панели
выбора, окно панели и признак «агентов много». Всё остальное — из `state`.
"""

from __future__ import annotations

import asyncio

from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings

from . import ui
from .agents_panel import step_agent
from .commands import handle_submit
from .commands_params import toggle_mouse
from .layout import Раскладка
from .output import append_log, refresh
from .panes import next_prefill, switch_pane, switch_screen, toggle_zoom
from .state import State
from .workers import track_submission


def привязки(state: State, раскладка: Раскладка) -> KeyBindings:
    """Собрать привязки клавиш для этого сеанса и этой раскладки."""

    kb = KeyBindings()
    # Всё, чего нет в состоянии: буфер ввода, признак открытой панели выбора, окно панели
    # (в нём живёт прокрутка) и признак «агентов много». Достаём разом, чтобы ниже читались
    # сами клавиши, а не дорога до них.
    buffer = раскладка.buffer
    picker_active = раскладка.picker_active
    many_agents = раскладка.many_agents
    pane_window = раскладка.pane_window

    @kb.add("up", filter=picker_active)
    def _picker_up(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.move(-1)

    @kb.add("down", filter=picker_active)
    def _picker_down(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.move(1)

    @kb.add("enter", filter=picker_active)
    def _picker_choose(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.choose()

    @kb.add("escape", filter=picker_active)
    @kb.add("c-c", filter=picker_active)
    def _picker_close(event) -> None:  # noqa: ANN001
        state.picker = None
        append_log(state, ui.system_fragments("выбор отменён"))

    @kb.add("<any>", filter=picker_active)
    def _picker_swallow(event) -> None:  # noqa: ANN001
        """Панель модальна: печать в строку ввода при открытом меню только мешала бы."""

    @kb.add("enter", filter=~picker_active)
    def _submit(event) -> None:  # noqa: ANN001
        completion_state = buffer.complete_state
        if completion_state is not None and completion_state.current_completion is not None:
            buffer.apply_completion(completion_state.current_completion)
            return
        destination_screen = state.screen if state.screen.interactive else state.main
        destination_pane = destination_screen.pane
        text = buffer.text
        buffer.reset()
        destination_pane.draft = ""
        # Следующую заготовку показываем в том же обработчике клавиши. Асинхронная задача
        # отправки может начать выполняться уже после перерисовки либо перехода на другой
        # экран — тогда строка оставалась пустой, хотя вопрос был принят.
        stripped = text.strip()
        if (
            stripped
            and not stripped.startswith("/")
            and state.config.is_authorized
            and not state.switching_profile
        ):
            next_prefill(state, destination_pane)
        if not state.screen.interactive:
            switch_screen(state, 0)  # экран агента только для чтения — ответ придёт в главный
        for pane in destination_screen.panes:
            pane.autoscroll = True  # новое сообщение — вернуться к живому выводу
        submission = asyncio.get_running_loop().create_task(
            handle_submit(
                text,
                state,
                destination_screen=destination_screen,
                destination_pane=destination_pane,
            )
        )
        track_submission(state, submission)

    @kb.add("c-c", filter=~picker_active)
    def _cancel(event) -> None:  # noqa: ANN001
        addressed_screen = state.screen if state.screen.interactive else state.main
        addressed_pane = addressed_screen.pane
        if addressed_screen is state.main:
            if state.busy and state.current_task is not None:
                state.current_task.cancel()
            return
        existing = state.pane_workers.get(id(addressed_pane))
        if existing is not None and existing.pane is addressed_pane:
            existing.cancel_active()

    @kb.add("c-d")
    def _quit(event) -> None:  # noqa: ANN001
        event.app.exit()

    @kb.add("f2")
    def _toggle_mouse(event) -> None:  # noqa: ANN001
        toggle_mouse(state)

    @kb.add("pageup")
    def _scroll_up(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.autoscroll = False
        window = pane_window(pane)
        window.vertical_scroll = max(0, window.vertical_scroll - 10)

    @kb.add("pagedown")
    def _scroll_down(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.autoscroll = False
        pane_window(pane).vertical_scroll += 10

    @kb.add("c-end")
    def _resume_autoscroll(event) -> None:  # noqa: ANN001
        state.screen.pane.autoscroll = True

    # Меню команд тоже ходит стрелками, и отнимать их у него нельзя: «/» открывает список
    # прямо под строкой ввода, и там ↑/↓ выбирают команду.
    menu_open = Condition(lambda: buffer.complete_state is not None)

    @kb.add("up", filter=~picker_active & ~menu_open & many_agents)
    def _prev_agent(event) -> None:  # noqa: ANN001
        step_agent(state, -1)

    @kb.add("down", filter=~picker_active & ~menu_open & many_agents)
    def _next_agent(event) -> None:  # noqa: ANN001
        step_agent(state, 1)

    @kb.add("escape", "left", filter=~picker_active)
    def _prev_pane(event) -> None:  # noqa: ANN001
        switch_pane(state, state.screen.active_pane - 1)

    @kb.add("escape", "right", filter=~picker_active)
    def _next_pane(event) -> None:  # noqa: ANN001
        switch_pane(state, state.screen.active_pane + 1)

    @kb.add("f3", filter=~picker_active)
    def _zoom_pane(event) -> None:  # noqa: ANN001
        toggle_zoom(state)

    @kb.add("c-r", filter=~picker_active)
    def _toggle_reasoning(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.show_reasoning = not pane.show_reasoning
        # Переключение меняет объём ленты выше точки просмотра сразу на все размышления,
        # а сама точка остаётся на прежнем номере строки — прокрученная вверх панель
        # прыгнула бы на чужое место. Возвращаемся к живому выводу: там место известно.
        pane.autoscroll = True
        refresh(state)

    @kb.add("s-right", filter=~picker_active)
    def _next_screen(event) -> None:  # noqa: ANN001
        switch_screen(state, (state.active + 1) % len(state.screens))

    @kb.add("s-left", filter=~picker_active)
    def _prev_screen(event) -> None:  # noqa: ANN001
        switch_screen(state, (state.active - 1) % len(state.screens))

    # Alt+N — прямо на экран с этим номером: номер написан на самой вкладке.
    for number in range(1, 10):
        @kb.add("escape", str(number), filter=~picker_active)
        def _goto_screen(event, index=number - 1) -> None:  # noqa: ANN001
            switch_screen(state, index)

    return kb
