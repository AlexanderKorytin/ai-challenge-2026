"""Раскладка окна: из чего оно собрано и какое окно кому принадлежит.

Раньше это жило внутри `build_app` вместе с привязкой клавиш — тремя сотнями строк, в которых
две разные работы держались за одно замыкание. Работы и правда разные: раскладку меняют, когда
на экране должно появиться что-то новое, а клавиши — когда меняется способ этим управлять.
Поэтому здесь собирается окно и отдаётся `Раскладка` — всё, что нужно клавишам и приложению, —
а сами клавиши живут в `keys`.

Одно правило этого модуля стоит назвать вслух: пока панель жива, окно у неё РОВНО ОДНО. В окне
хранится прокрутка, и окно, собранное заново на каждой перерисовке, теряло бы её на каждом
ответе модели.

Обратное неверно, и это известный изъян, а не свойство: окна ключуются по `id(pane)` и не
убираются никогда, то есть окно переживает свою панель. Замена ключа на саму панель записана
отдельной работой (`openspec/changes/конвейер/план.md`); до неё адрес убранной панели может
достаться новой, и та получит чужое окно.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from prompt_toolkit.application import get_app
from prompt_toolkit.buffer import Buffer
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
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

from . import archivist, compact, context_strategy, profiles, ui
from . import params as params_mod
from . import picker as picker_mod
from . import screens as screens_mod
from .agents_panel import agent_index, collect_agents, goto_agent, show_agent_panel
from .output import Fragments
from .panes import active_input_pane, pane_profile, restore_active_draft, apply_prefill
from .state import State
from .strategies import STRATEGY_TITLES


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
        else:
            # Команды памяти подсказывают и вторым уровнем, и третьим: после «/task » — слова
            # команды, после «/task забыть » — разделы, после «/task забыть план » — номера
            # строк вместе с их текстом. Состав считает `ui.подсказки` — чистая функция,
            # которая проверяется без терминала; здесь остаётся только отбор по набранным
            # буквам и обёртка в вид prompt_toolkit.
            for значение, показ, пояснение in ui.подсказки(self.state, text):
                if значение.lower().startswith(word.lower()):
                    yield Completion(
                        значение,
                        start_position=-len(word),
                        display=показ,
                        display_meta=пояснение,
                    )


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
    `1015h` и `1006h` (расширенные ответы). Просить меньше стоит ради `1003h`: пока он поднят,
    приложению идёт каждое движение мыши, а нам движение не нужно — нужны клики по вкладкам,
    панелям и строкам списка. Их дают `1000h` и `1006h`, и своего выделения приложение не ведёт
    ни при каком наборе: протяжка остаётся терминалу.

    Беду дня 8 это, однако, сняло не целиком, и почему — выяснил замер 2026-09-16. Пока запрошены
    хотя бы нажатия, штатный Terminal.app отдаёт приложению и левую кнопку, поэтому выделять
    приходится с клавишей обхода перехвата (в Terminal.app это Fn). Клавиша эта — свойство
    терминала, от запрошенного набора она не зависит; при `1003h` замера с ней никто не делал.
    Своё выделение протяжкой — отложенная задача 1, разбор в `docs/выделение-мышью.md`.
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


@dataclass


class Раскладка:
    """Собранное окно и те его части, без которых нельзя привязать клавиши.

    Поля здесь не для удобства: каждое принадлежит окну, а не сеансу, и потому не может
    прийти к обработчику через состояние. Полей ровно пять — что окну нужно отдать наружу, а
    что оно держит при себе, видно по этому списку. Буфер лежит и в `state.input_buffer`, но
    хозяин у него окно: состояние получает его как раз отсюда."""

    layout: Layout
    buffer: Buffer
    picker_active: Condition
    many_agents: Condition
    pane_window: Callable[[screens_mod.Pane], "LogWindow"]


def собрать_раскладку(state: State) -> Раскладка:
    windows: dict[int, LogWindow] = {}

    def pane_window(pane: screens_mod.Pane) -> LogWindow:
        """Пока панель жива, окно у неё одно: в нём хранится прокрутка. Про обратную сторону —
        окно переживает панель — сказано в докстроке модуля."""
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
                ui.строка_задачи(state),
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
    занятость = ui.СчётЗанятости()
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
    restore_active_draft(state)
    active_pane = active_input_pane(state)
    if active_pane is not None:
        profile = pane_profile(state, active_pane)
        if profile is not None:
            apply_prefill(state, profile, active_pane)

    return Раскладка(
        layout=layout,
        buffer=input_area.buffer,
        picker_active=picker_active,
        many_agents=many_agents,
        pane_window=pane_window,
    )
