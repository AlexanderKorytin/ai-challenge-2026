"""Полноэкранный REPL: лог-панель сверху (растёт, автопрокрутка), поле ввода снизу,
окаймлённое горизонтальными линиями. Очередь запросов, отмена по Ctrl+C, потоковый ответ,
всплывающее меню команд по «/» и панель выбора значений параметров генерации."""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress
from typing import Any

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import TextArea

from . import archivist, background, compact, context_strategy, profiles, ui
from . import params as params_mod
from . import picker as picker_mod
from . import screens as screens_mod
from .agents_panel import agent_index, collect_agents, goto_agent, show_agent_panel, step_agent
from .api import DeepSeekClient
from .commands import handle_submit
from .commands_params import toggle_mouse
from .config import load as load_config
from .conversation import restore_conversation
from .output import Fragments, append_log, refresh
from .panes import (
    _active_input_pane,
    _pane_profile,
    _restore_active_draft,
    apply_prefill,
    next_prefill,
    switch_pane,
    switch_screen,
    toggle_zoom,
)
from .state import State, user_facts
from .strategies import STRATEGY_TITLES, open_profile_surfaces
from .workers import _track_submission, close_pane_workers, worker




# ─────────────────────────────── список агентов ───────────────────────────────


# ─────────────────────────────── меню команд ───────────────────────────────


class HarnessCompleter(Completer):
    """Список команд появляется сразу по вводу «/», без Enter. Состав зависит от того,
    авторизован ли пользователь: пока ключа нет, всё остальное всё равно не сработает."""

    def __init__(self, state: State) -> None:
        self.state = state

    def get_completions(self, document, complete_event):  # noqa: ANN001, ANN201
        if self.state.awaiting_key or self.state.awaiting_custom or self.state.picker is not None:
            return
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text.split(" ")
        word = parts[-1]
        if len(parts) == 1:
            for name, arg, description in ui.visible_commands(self.state.config.is_authorized):
                if name.startswith(word):
                    display = f"{name} {arg}".strip()
                    yield Completion(name, start_position=-len(word), display=display, display_meta=description)
            return
        command = parts[0].lower()
        if command == "/set" and len(parts) == 2:
            for name in params_mod.ORDER:
                if name.startswith(word):
                    yield Completion(
                        name, start_position=-len(word), display=name, display_meta=params_mod.SPECS[name].description
                    )
        elif command == "/profile" and len(parts) == 2:
            for name, source in profiles.available():
                if name.startswith(word):
                    meta = str(source) if source else "встроенный"
                    yield Completion(name, start_position=-len(word), display=name, display_meta=meta)
            if "save".startswith(word):
                yield Completion(
                    "save", start_position=-len(word), display="save", display_meta="сохранить текущие параметры"
                )
        elif len(parts) == 2 and parts[0] == "/strategy":
            for strategy in context_strategy.CONTEXT_STRATEGIES:
                command = f"use {strategy}"
                if not word or command.startswith(word) or strategy.startswith(word):
                    yield Completion(
                        command,
                        start_position=-len(word),
                        display=strategy,
                        display_meta=STRATEGY_TITLES[strategy],
                    )
        elif len(parts) == 3 and parts[:2] == ["/strategy", "use"]:
            for strategy in context_strategy.CONTEXT_STRATEGIES:
                if strategy.startswith(word):
                    yield Completion(
                        strategy,
                        start_position=-len(word),
                        display=strategy,
                        display_meta=STRATEGY_TITLES[strategy],
                    )
        elif command == "/model" and len(parts) == 2:
            for name in self.state.known_models:
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word), display=name, display_meta="модель DeepSeek")


class LogWindow(Window):
    """Window с логом: колесо мыши отключает автопрокрутку (даём пролистать историю)."""

    def __init__(self, *args, on_manual_scroll, **kwargs) -> None:  # noqa: ANN001
        super().__init__(*args, **kwargs)
        self._on_manual_scroll = on_manual_scroll

    def _mouse_handler(self, mouse_event: MouseEvent):
        if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self._on_manual_scroll()
        return super()._mouse_handler(mouse_event)


def pane_columns(count: int, width: int) -> int:
    """Сколько панелей ставить в ряд. Пять экспертов в пять колонок на обычном окне дают по
    двадцать знаков на колонку — читать невозможно, поэтому решает ширина, а не число."""
    if count <= 1 or width < 100:
        return 1
    return 2 if width < 170 else 3


def app_output():  # noqa: ANN201 — тип вывода приходит из prompt_toolkit
    from prompt_toolkit.application.current import get_app_session

    return get_app_session().output


def click_only_mouse(output) -> None:  # noqa: ANN001
    """Просить у терминала только нажатия мыши, без отслеживания перетаскивания.

    prompt_toolkit включает мышь одним куском: `1000h` (нажатия), `1003h` (любое движение),
    `1015h` и `1006h` (расширенные ответы). Губителен здесь `1003h` — пока он поднят, терминал
    отдаёт приложению и протяжку тоже, а значит выделить текст мышью нельзя. Отсюда и родился
    прежний переключатель «или клики, или копирование».

    Выбор ложный. Оставив только `1000h` и `1006h`, приложение получает клики по вкладкам,
    панелям и строкам списка, а протяжка остаётся терминалу — выделение и копирование работают
    как обычно, без единой команды.
    """
    if getattr(output, "_click_only", False):
        return
    output._click_only = True

    def enable() -> None:
        output.write_raw("\x1b[?1000h\x1b[?1006h")

    def disable() -> None:
        output.write_raw("\x1b[?1006l\x1b[?1000l")

    output.enable_mouse_support = enable
    output.disable_mouse_support = disable


def build_app(state: State) -> Application:
    windows: dict[int, LogWindow] = {}

    def pane_window(pane: screens_mod.Pane) -> LogWindow:
        """Окно панели живёт столько же, сколько сама панель: в нём хранится прокрутка."""
        existing = windows.get(id(pane))
        if existing is not None:
            return existing
        control = FormattedTextControl(text=lambda: pane.visible_log(), show_cursor=False)
        window = LogWindow(
            content=control,
            wrap_lines=True,
            always_hide_cursor=True,
            height=Dimension(weight=1),
            on_manual_scroll=lambda: setattr(pane, "autoscroll", False),
        )
        control.get_cursor_position = lambda: (
            Point(x=0, y=pane.visible_lines()) if pane.autoscroll else Point(x=0, y=window.vertical_scroll)
        )
        windows[id(pane)] = window
        return window

    def pane_header(pane: screens_mod.Pane) -> Window:
        return Window(
            content=FormattedTextControl(
                text=lambda: ui.pane_title_fragments(pane.title or pane.key, pane.status, state.screen.pane is pane)
            ),
            height=1,
            style="class:pane.title",
        )

    def pane_block(pane: screens_mod.Pane, titled: bool):  # noqa: ANN202 — контейнер prompt_toolkit
        window = pane_window(pane)
        return HSplit([pane_header(pane), window]) if titled else window

    def screen_container():  # noqa: ANN202 — контейнер prompt_toolkit
        """Раскладка активного экрана: одна лента, развёрнутая панель или сетка панелей."""
        screen = state.screen
        panes = screen.panes
        if len(panes) == 1:
            return pane_block(panes[0], titled=False)
        if screen.zoomed:
            return pane_block(screen.pane, titled=True)
        columns = pane_columns(len(panes), get_app().output.get_size().columns)
        rows: list[Any] = []
        for start in range(0, len(panes), columns):
            blocks: list[Any] = []
            for index, pane in enumerate(panes[start : start + columns]):
                if index:
                    blocks.append(Window(width=1, char="│", style="class:sep"))
                blocks.append(pane_block(pane, titled=True))
            if rows:
                rows.append(Window(height=1, char="─", style="class:sep"))
            rows.append(VSplit(blocks))
        return HSplit(rows)

    output_window = DynamicContainer(screen_container)

    input_area = TextArea(
        height=1,
        prompt="› ",
        multiline=False,
        wrap_lines=False,
        password=Condition(lambda: state.awaiting_key),
        completer=HarnessCompleter(state),
        complete_while_typing=True,
    )

    def sep() -> Window:
        return Window(height=1, char="─", style="class:sep")

    status_window = Window(
        content=FormattedTextControl(
            text=lambda: ui.status_fragments(
                state.model,
                state.config.is_authorized,
                state.profile.name,
                state.profile_dirty,
                state.mouse_enabled,
                archivist.running(state),
                compact.идёт(state),
            )
        ),
        height=1,
        style="class:status",
    )

    def agent_panel() -> Fragments:
        rows = collect_agents(state)
        return ui.agent_panel_fragments(
            [
                (row.status, row.agent.name, row.occupation, row.agent.total_ms, row.agent.total_tokens, row.agent.runs)
                for row in rows
            ],
            agent_index(state, rows),
            get_app().output.get_size().columns,
            lambda index: goto_agent(state, index),
        )

    agents_window = Window(
        content=FormattedTextControl(text=agent_panel, focusable=False),
        dont_extend_height=True,
        wrap_lines=False,
        style="class:agents",
    )
    # Пока агент один, списка нет: строка «main» в одиночестве ничего не сообщает и только
    # съедает высоту экрана. Отбивку прячем вместе со списком — иначе под строкой ввода
    # оставались бы две черты подряд.
    many_agents = Condition(lambda: show_agent_panel(state))
    agents_area = ConditionalContainer(HSplit([sep(), agents_window]), filter=many_agents)

    picker_active = Condition(lambda: state.picker is not None)
    picker_window = Window(
        content=FormattedTextControl(text=lambda: picker_mod.fragments(state.picker) if state.picker else []),
        style="class:panel",
        dont_extend_height=True,
        dont_extend_width=True,
    )

    # Полоска занятости контекста — над строкой ввода. Считает по ВЕСУ СЛЕДУЮЩЕГО ЗАПРОСА, а
    # не по расходу за сеанс: расход только растёт, а полоска обязана падать при сжатии — в
    # этом весь её смысл. Постоянная часть веса запоминается и пересчитывается при изменении
    # памяти, на нажатие клавиши прибавляется только вес набранного: системная часть ходит на
    # диск за глобальными фактами, и звать её на каждую букву нельзя.
    занятость = ui.СчётЗанятости(user_facts)
    state.занятость = занятость
    context_bar = Window(
        content=FormattedTextControl(
            text=lambda: ui.context_bar_fragments(
                занятость.ближайший(state.main_agent, state.input_buffer.text, model=state.model),
                get_app().output.get_size().columns,
            )
        ),
        dont_extend_height=True,
        style="class:gauge",
    )

    root = FloatContainer(
        content=HSplit([output_window, sep(), context_bar, input_area, agents_area, sep(), status_window]),
        floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12, scroll_offset=1)),
            Float(left=2, bottom=4, content=ConditionalContainer(picker_window, filter=picker_active)),
        ],
    )
    layout = Layout(root, focused_element=input_area)
    state.input_buffer = input_area.buffer
    _restore_active_draft(state)
    active_pane = _active_input_pane(state)
    if active_pane is not None:
        profile = _pane_profile(state, active_pane)
        if profile is not None:
            apply_prefill(state, profile, active_pane)

    kb = KeyBindings()
    buffer = input_area.buffer

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
        _track_submission(state, submission)

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

    click_only_mouse(app_output())
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=ui.STYLE,
        full_screen=True,
        mouse_support=Condition(lambda: state.mouse_enabled),
        erase_when_done=True,
    )
    # Esc — начало alt-комбинаций и escape-последовательностей, поэтому prompt_toolkit
    # ждёт продолжения, прежде чем счесть клавишу самостоятельной. При стандартных
    # 0.5 и 1.0 с закрытие панели по Esc ощущается как залипание; сочетаний с Alt у нас
    # нет, поэтому ждать долго незачем.
    app.ttimeoutlen = 0.15
    app.timeoutlen = 0.3
    return app


def greet(state: State) -> None:
    """Шапка и подсказка про инструкцию. Вынесены из `repl`, потому что печатать их надо
    ДО восстановления разговора: строка «восстановлен разговор…», вылезшая выше приветствия,
    читается так, будто разговор подняли ещё до запуска инструмента."""
    append_log(state, ui.banner_fragments(state.model, state.config.is_authorized, state.profile.name))
    if state.profile.system:
        append_log(state, ui.system_fragments("профиль задаёт системную инструкцию — показать: /system"))


async def repl(state: State) -> None:
    app = build_app(state)
    state.app = app
    worker_task = asyncio.create_task(worker(state))
    try:
        await app.run_async()
    finally:
        # Сначала запрещаем новым обработчикам Enter пополнять очереди, затем одновременно
        # снимаем общий и панельные исполнители. Каждый из них дожидается уборки дочернего
        # `run_turn`, поэтому вращатели строк ожидания не переживают закрытие приложения.
        submissions = tuple(state.submission_tasks)
        for task in submissions:
            task.cancel()
        if submissions:
            await asyncio.gather(*submissions, return_exceptions=True)
        worker_task.cancel()
        await asyncio.gather(worker_task, close_pane_workers(state), return_exceptions=True)
        # Последний заход архивариуса — до закрытия клиента: без него всё, о чём говорили
        # после прошлого захода (до пяти обменов), в глобальную память не попало бы вовсе.
        await archivist.finish(state)
        # Идущий заход сжимателя, наоборот, снимаем и не дожидаемся: его итог — заготовка на
        # следующий обмен, а следующего обмена не будет. Ждать выхода ради неё значило бы
        # держать человека, уже сказавшего «выхожу», ради работы, которая никому не достанется.
        # Снять при этом обязательно: брошенная задача при закрытии цикла даёт предупреждение
        # «задача уничтожена, а она ещё работала» поверх прощального экрана.
        background.отменить(state.сжиматель)
        if state.client:
            await state.client.aclose()


def silence_transport_noise(loop: asyncio.AbstractEventLoop) -> None:
    """Глушит одно конкретное сообщение httpcore2 2.12: при обрыве ответа по max_tokens
    тело остаётся недочитанным, и закрытие потока печатает «generator didn't stop after
    athrow()». Это шум чужой библиотеки, но в полноэкранном режиме он рвёт разметку экрана.
    Все прочие ошибки цикла обрабатываются как обычно."""
    default_handler = loop.get_exception_handler()

    def handler(target_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get("exception")
        message = context.get("message", "")
        if isinstance(exception, RuntimeError) and "athrow" in str(exception):
            return
        if "closing of asynchronous generator" in message:
            return
        if default_handler is not None:
            default_handler(target_loop, context)
        else:
            target_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _main(args: argparse.Namespace) -> None:
    """Поднять интерфейс на уже разобранных ключах.

    Ключи сюда приходят готовыми (их разбирает точка входа пакета) и здесь не разбираются
    заново: второй разбор — это второе место, где живут имена и значения по умолчанию, и
    расходятся такие места молча."""
    silence_transport_noise(asyncio.get_running_loop())
    cfg = load_config()
    profile_name = args.profile or os.environ.get("MYHARNESS_PROFILE") or cfg.profile
    profile, warnings = profiles.load(profile_name)
    if args.model:
        cfg.model = args.model
    client = DeepSeekClient(cfg.api_key) if cfg.is_authorized else None
    state = State(config=cfg, client=client, model=cfg.model, profile=profile)
    greet(state)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    # Прежний разговор поднимается ЗДЕСЬ: настройки и профиль уже прочитаны, агент уже есть,
    # ни один запрос ещё невозможен. В конструкторе состояния этому места нет — его зовут
    # проверки напрямую, и любое состояние начало бы читать и писать в каталог состояния.
    restore_conversation(state)
    open_profile_surfaces(state, state.initial_strategy)
    await repl(state)


def main(args: argparse.Namespace) -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(_main(args))
