"""Полноэкранный REPL: лог-панель сверху (растёт, автопрокрутка), поле ввода снизу,
окаймлённое горизонтальными линиями. Очередь запросов, отмена по Ctrl+C, потоковый ответ,
всплывающее меню команд по «/» и панель выбора значений параметров генерации."""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass
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

from . import archivist, background, compact, context_strategy, memory, profiles, tokens, ui
from . import params as params_mod
from . import picker as picker_mod
from . import screens as screens_mod
from .agent import Agent
from .api import DeepSeekClient
from .config import load as load_config
from .config import save as save_config
from .conversation import open_new_session, restore_conversation, забыть_счёт_занятости
from .output import Fragments, append_log, refresh
from .panes import (
    _active_input_pane,
    _pane_profile,
    _restore_active_draft,
    _save_active_draft,
    active_profile,
    apply_prefill,
    next_prefill,
    switch_pane,
    switch_screen,
    toggle_zoom,
)
from .state import Request, State, user_facts
from .strategies import (
    STRATEGY_TITLES,
    _branch_prefill,
    open_profile_surfaces,
    switch_profile,
    use_strategy,
)
from .workers import _track_submission, close_pane_workers, pane_worker, worker


# ─────────────────────────────── команды ───────────────────────────────


def cmd_auth(state: State) -> None:
    state.awaiting_key = True
    append_log(state, ui.system_fragments("введите API-ключ DeepSeek (ввод скрыт звёздочками), затем Enter:"))


async def do_auth(raw_key: str, state: State) -> None:
    key = raw_key.strip()
    if not key:
        append_log(state, ui.hint_fragments("ключ не введён, отменено"))
        return
    append_log(state, ui.system_fragments("проверяю ключ…"))
    candidate = DeepSeekClient(key)
    try:
        ok = await candidate.validate()
    except Exception as exc:
        append_log(state, ui.error_fragments(f"не удалось проверить ключ: {exc}"))
        await candidate.aclose()
        return
    if not ok:
        append_log(state, ui.error_fragments("ключ не принят DeepSeek API (проверьте правильность)"))
        await candidate.aclose()
        return
    if state.client:
        await state.client.aclose()
    state.client = candidate
    state.config.api_key = key
    save_config(state.config)
    append_log(state, ui.system_fragments("авторизация сохранена — вводить ключ заново не потребуется"))


def set_model(state: State, name: str) -> None:
    state.model = name
    state.config.model = name
    save_config(state.config)
    append_log(state, ui.system_fragments(f"модель установлена: {name}"))


async def cmd_model(state: State, arg: str) -> None:
    if arg:
        set_model(state, arg)
        return
    models = state.known_models
    if state.client:
        try:
            models = await state.client.list_models()
            state.known_models = models
        except Exception as exc:
            append_log(state, ui.error_fragments(f"не удалось получить список моделей: {exc}"))
            append_log(state, ui.system_fragments("показан статический список"))
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for name in models:
        if name == state.model:
            marked = len(items)
        items.append(
            picker_mod.Item(
                label=name,
                hint="текущая" if name == state.model else "",
                payload=name,
            )
        )

    def choose(payload: Any) -> None:
        state.picker = None
        set_model(state, str(payload))

    state.picker = picker_mod.Picker(
        title="/model — модель DeepSeek",
        description="какой моделью отвечать",
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


def open_profile_picker(state: State) -> None:
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for name, source in profiles.available():
        if name == state.profile.name:
            marked = len(items)
        hint = str(source.parent) if source else "встроенный"
        items.append(
            picker_mod.Item(label=name, hint=hint, payload=name)
        )

    def choose(payload: Any) -> None:
        state.picker = None
        task = asyncio.create_task(switch_profile(state, str(payload)))
        _track_submission(state, task)

    description = "какой профиль генерации применить"
    if len(items) <= 1:
        # Один встроенный профиль в списке почти всегда значит не «профилей нет», а
        # «harness запущен не из той папки»: профили ищутся рядом с каталогом запуска.
        # Человек в этот момент смотрит именно сюда, поэтому и сказать надо здесь.
        description += f" · профили ищутся в {profiles.search_hint()}"
    if state.profile_dirty:
        description += " (текущие изменения не сохранены — /profile save <имя>)"
    state.picker = picker_mod.Picker(
        title="/profile — профиль генерации",
        description=description,
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


async def cmd_profile(state: State, arg: str) -> None:
    if not arg:
        open_profile_picker(state)
        return
    parts = arg.split(maxsplit=1)
    if parts[0] == "save":
        target = active_profile(state)
        name = parts[1].strip() if len(parts) > 1 else target.name
        target.name = name
        try:
            path = profiles.save(target)
        except OSError as exc:
            append_log(
                state,
                ui.error_fragments(f"не удалось сохранить профиль: {exc}"),
            )
            return
        state.profile_dirty = False
        state.config.profile = name
        save_config(state.config)
        append_log(state, ui.system_fragments(f"профиль сохранён: {path}"))
        return
    await switch_profile(state, parts[0])


def open_strategy_picker(state: State) -> None:
    source = active_profile(state)
    current = source.context_strategy
    items = [
        picker_mod.Item(
            label=STRATEGY_TITLES[strategy],
            hint="текущая" if strategy == current else "",
            payload=strategy,
        )
        for strategy in context_strategy.CONTEXT_STRATEGIES
    ]

    def choose(payload: Any) -> None:
        state.picker = None
        use_strategy(state, str(payload), source=source)

    state.picker = picker_mod.Picker(
        title="/strategy — стратегия контекста",
        description="какой независимый разговор открыть",
        items=items,
        on_choose=choose,
        index=context_strategy.CONTEXT_STRATEGIES.index(current),
        marked=context_strategy.CONTEXT_STRATEGIES.index(current),
    )
    refresh(state)


def cmd_strategy(state: State, arg: str) -> None:
    """Показать, открыть либо настроить независимый экран стратегии."""

    if not arg:
        profile = active_profile(state)
        append_log(
            state,
            ui.system_fragments(
                f"стратегия: {profile.context_strategy}; "
                f"строгое окно: {profile.strategy_window} пар"
            ),
        )
        open_strategy_picker(state)
        return

    parts = arg.split()
    action = parts[0].lower()
    if action == "use":
        if len(parts) != 2 or parts[1] not in context_strategy.CONTEXT_STRATEGIES:
            append_log(
                state,
                ui.error_fragments(
                    "нужен режим: /strategy use <standard|sliding|facts|branching>"
                ),
            )
            return
        use_strategy(state, parts[1], source=active_profile(state))
        return

    if action == "window":
        if len(parts) != 2:
            append_log(
                state,
                ui.error_fragments(
                    "нужно одно положительное целое число: /strategy window <N>"
                ),
            )
            return
        profile = active_profile(state)
        if profile.context_strategy not in (
            context_strategy.CONTEXT_SLIDING,
            context_strategy.CONTEXT_FACTS,
        ):
            append_log(
                state,
                ui.error_fragments(
                    "строгое окно применяется только в sliding и facts"
                ),
            )
            return
        raw_window = parts[1]
        window = int(raw_window) if raw_window.isdecimal() else 0
        if window <= 0:
            append_log(
                state,
                ui.error_fragments(
                    "размер окна должен быть положительным целым числом"
                ),
            )
            return
        profile.strategy_window = window
        state.profile_dirty = True
        append_log(
            state,
            ui.system_fragments(
                f"строгое окно стратегии {profile.context_strategy}: {window} пар"
            ),
        )
        append_log(
            state,
            ui.hint_fragments("сохранить в профиль: /profile save <имя>"),
        )
        return

    append_log(
        state,
        ui.error_fragments(
            "форма команды: /strategy [use <режим>|window <N>]"
        ),
    )


def cmd_facts(state: State, arg: str) -> None:
    """Показать или вручную изменить факты активного разговора Sticky Facts."""

    pane = _active_input_pane(state)
    profile = active_profile(state)
    if (
        profile.context_strategy != context_strategy.CONTEXT_FACTS
        or pane.agent is None
    ):
        append_log(
            state,
            ui.error_fragments("команда /facts работает только в Sticky Facts"),
        )
        return

    values, revision = pane.agent.conversation_facts()
    if not arg:
        append_log(
            state,
            ui.system_fragments(f"факты разговора, редакция {revision}"),
        )
        if not values:
            append_log(
                state,
                ui.system_fragments("факты текущего разговора пусты"),
            )
            return
        for key in sorted(values):
            append_log(state, [("", f"  {key} → {values[key]}\n")])
        return

    def ensure_session_record() -> bool:
        store = state.screen.session_store
        if store is None or store.path.exists():
            return True
        error = store.touch()
        if error:
            append_log(state, ui.error_fragments(error))
            return False
        return True

    parts = arg.split(maxsplit=2)
    action = parts[0].lower()
    if action in ("set", "forget"):
        executor = state.pane_workers.get(id(pane))
        if (
            executor is not None
            and executor.pane is pane
            and (executor.busy or not executor.queue.empty())
        ):
            append_log(
                state,
                ui.error_fragments(
                    "нельзя изменить факты: панель выполняет или ожидает обмен"
                ),
            )
            return

    if action == "set":
        if len(parts) < 2 or not parts[1].strip():
            append_log(state, ui.error_fragments("нужен непустой ключ факта"))
            return
        if len(parts) < 3 or not parts[2].strip():
            append_log(state, ui.error_fragments("нужно непустое значение факта"))
            return
        key, value = parts[1].strip(), parts[2].strip()
        if not ensure_session_record():
            return
        error = pane.agent.set_conversation_fact(key, value)
        if error:
            append_log(state, ui.error_fragments(error))
            return
        _, revision = pane.agent.conversation_facts()
        append_log(
            state,
            ui.system_fragments(f"факт «{key}» установлен, редакция {revision}"),
        )
        return

    if action == "forget":
        key = arg[len(parts[0]) :].strip()
        if not key:
            append_log(state, ui.error_fragments("нужен непустой ключ факта"))
            return
        # Agent намеренно делает отсутствие безопасным, но команда обязана назвать ошибку
        # до вызова: иначе успешный ответ уверял бы, что несуществующий ключ был удалён.
        if key not in values:
            append_log(
                state,
                ui.error_fragments(f"в фактах разговора нет ключа «{key}»"),
            )
            return
        if not ensure_session_record():
            return
        error = pane.agent.forget_conversation_fact(key)
        if error:
            append_log(state, ui.error_fragments(error))
            return
        _, revision = pane.agent.conversation_facts()
        append_log(
            state,
            ui.system_fragments(f"факт «{key}» удалён, редакция {revision}"),
        )
        return

    append_log(
        state,
        ui.error_fragments(
            "форма команды: /facts [set <ключ> <значение>|forget <ключ>]"
        ),
    )


def _activate_branch(state: State, name: str) -> None:
    screen = state.screen
    store = screen.branch_store
    if store is None:
        append_log(state, ui.error_fragments("у экрана нет хранилища ветвей"))
        return
    graph = store.load()
    if name not in graph.branches:
        append_log(state, ui.error_fragments(f"неизвестная ветвь «{name}»"))
        return
    if name in graph.unavailable:
        append_log(
            state,
            ui.error_fragments(f"ветвь «{name}» недоступна из-за повреждения графа"),
        )
        return
    for index, pane in enumerate(screen.panes):
        if pane.key == name:
            switch_pane(state, index)
            return
    append_log(
        state,
        ui.error_fragments(f"ветвь «{name}» недоступна на этом экране"),
    )


def _open_branch_picker(state: State, names: tuple[str, ...]) -> None:
    screen = state.screen
    available = [
        name for name in names if screen.pane_by_key(name) is not None
    ]
    if not available:
        return
    active = screen.pane.key
    items = [
        picker_mod.Item(
            label=name,
            hint="активная" if name == active else "",
            payload=name,
        )
        for name in available
    ]

    def choose(payload: Any) -> None:
        state.picker = None
        _activate_branch(state, str(payload))

    marked = available.index(active) if active in available else None
    state.picker = picker_mod.Picker(
        title="/branch — ветвь разговора",
        description="какую ветвь открыть",
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


async def cmd_branch(state: State, arg: str) -> None:
    """Показать, разделить или активировать ветвь текущего Branching."""

    screen = state.screen
    profile = active_profile(state)
    if (
        not screen.interactive
        or profile.context_strategy != context_strategy.CONTEXT_BRANCHING
        or screen.branch_store is None
    ):
        append_log(
            state,
            ui.error_fragments("команда /branch работает только в Branching"),
        )
        return

    graph = screen.branch_store.load()
    if not arg:
        checkpoint = graph.checkpoint_id or graph.root_head or "нет"
        branches = ", ".join(graph.branches) if graph.branches else "ещё не созданы"
        active = screen.pane.key if graph.checkpoint_id is not None else "родитель"
        append_log(
            state,
            ui.system_fragments(
                f"контрольная точка: {checkpoint}; ветви: {branches}; активная: {active}"
            ),
        )
        if graph.branches:
            _open_branch_picker(state, graph.branches)
        return

    parts = arg.split()
    if parts[0].lower() != "split":
        _activate_branch(state, arg.strip())
        return
    if len(parts) != 3:
        append_log(
            state,
            ui.error_fragments("нужны два имени: /branch split <A> <B>"),
        )
        return
    left, right = parts[1].strip(), parts[2].strip()
    if not left or not right:
        append_log(
            state,
            ui.error_fragments("имена двух ветвей должны быть непустыми"),
        )
        return
    if left == right:
        append_log(
            state,
            ui.error_fragments("имена двух ветвей должны различаться"),
        )
        return
    if graph.checkpoint_id is not None:
        append_log(state, ui.error_fragments("разговор уже разделён"))
        return
    root_pane = screen.first
    root_agent = root_pane.agent
    worker_for_root = state.pane_workers.get(id(root_pane))
    if worker_for_root is not None and (
        worker_for_root.busy or not worker_for_root.queue.empty()
    ):
        append_log(
            state,
            ui.error_fragments(
                "разделение возможно только после завершения текущих запросов родителя"
            ),
        )
        return
    if root_agent is None or len(root_agent.history()) < 2:
        append_log(
            state,
            ui.error_fragments("разделение возможно только после завершённой пары"),
        )
        return

    branches, error = root_agent.split_branches(left, right)
    if error or branches is None:
        append_log(
            state,
            ui.error_fragments(error or "не удалось разделить разговор"),
        )
        return
    if worker_for_root is not None and worker_for_root.pane is root_pane:
        await worker_for_root.drain_and_close()
        executor = state.pane_workers.get(id(root_pane))
        if executor is worker_for_root:
            del state.pane_workers[id(root_pane)]

    _save_active_draft(state)
    branch_panes: list[screens_mod.Pane] = []
    for name in (left, right):
        pane = screens_mod.Pane(
            key=name,
            title=name,
            profile=profile,
            agent=branches[name],
        )
        _branch_prefill(state, profile, pane, name)
        branch_panes.append(pane)
    # Корень покидает дерево экранов ровно здесь. Его расход не восстановится в новых
    # агентах и потому переносится в общий сеанс один раз до потери ссылки.
    state.retire([root_agent])
    screen.panes = branch_panes
    screen.active_pane = 0
    screen.zoomed = False
    _restore_active_draft(state)
    checkpoint = screen.branch_store.load().checkpoint_id
    append_log(
        state,
        ui.system_fragments(
            f"разговор разделён в точке {checkpoint}: ветви {left}, {right}; активна {left}"
        ),
    )
    refresh(state)


def cmd_params(state: State) -> None:
    """Показываем значения и сразу даём их менять: список в логе выбирать нечем."""
    append_log(state, ui.params_fragments(state.profile.name, state.profile.params, state.profile.system))
    if state.profile_dirty:
        append_log(state, ui.hint_fragments("изменения не сохранены — /profile save <имя>"))
    open_param_picker(state)


def set_param(state: State, name: str, value: Any) -> None:
    spec = params_mod.SPECS[name]
    if value is params_mod.UNSET:
        state.profile.params.pop(name, None)
        append_log(state, ui.system_fragments(f"{spec.title}: параметр снят (умолчание API)"))
    else:
        state.profile.params[name] = value
        append_log(state, ui.system_fragments(f"{spec.title} = {params_mod.format_value(value)}"))
    state.profile_dirty = True
    reason = params_mod.inapplicable_reason(name, state.profile.params)
    if reason and value is not params_mod.UNSET:
        append_log(state, ui.hint_fragments(f"{spec.title} сейчас {reason}"))
    append_log(state, ui.hint_fragments("сохранить в профиль: /profile save <имя>"))


def open_value_picker(state: State, name: str) -> None:
    spec = params_mod.SPECS[name]
    current = state.profile.params.get(name)
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for choice in spec.choices:
        payload = choice.value
        if (payload is params_mod.UNSET and current is None) or (payload is not params_mod.UNSET and payload == current):
            marked = len(items)
        items.append(picker_mod.Item(label=choice.label, hint=choice.hint, payload=payload))
    if spec.custom_hint:
        items.append(picker_mod.Item(label="ввести своё значение…", hint=spec.custom_hint, payload="__custom__"))

    description = spec.description
    reason = params_mod.inapplicable_reason(name, state.profile.params)
    if reason:
        description += f" — {reason}"

    def choose(payload: Any) -> None:
        state.picker = None
        if payload == "__custom__":
            state.awaiting_custom = name
            append_log(state, ui.system_fragments(f"{spec.title}: введите значение ({spec.custom_hint}), Enter — применить"))
            return
        set_param(state, name, payload)

    state.picker = picker_mod.Picker(
        title=f"/set {spec.title}",
        description=description,
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


def open_param_picker(state: State) -> None:
    items: list[picker_mod.Item] = []
    for name in params_mod.ORDER:
        spec = params_mod.SPECS[name]
        value = params_mod.format_value(state.profile.params.get(name))
        items.append(picker_mod.Item(label=spec.title, hint=f"сейчас: {value}", payload=name))

    def choose(payload: Any) -> None:
        state.picker = None
        open_value_picker(state, str(payload))

    state.picker = picker_mod.Picker(
        title="/set — параметры генерации",
        description="какой параметр меняем",
        items=items,
        on_choose=choose,
    )
    refresh(state)


def cmd_set(state: State, arg: str) -> None:
    name = arg.strip()
    if not name:
        open_param_picker(state)
        return
    if name not in params_mod.SPECS:
        append_log(state, ui.error_fragments(f"нет такого параметра: {name}"))
        append_log(state, ui.hint_fragments("доступны: " + ", ".join(params_mod.ORDER)))
        return
    open_value_picker(state, name)


def apply_custom_value(state: State, raw: str) -> None:
    name = state.awaiting_custom or ""
    state.awaiting_custom = None
    spec = params_mod.SPECS.get(name)
    if spec is None:
        return
    text = raw.strip()
    if not text:
        append_log(state, ui.hint_fragments("значение не введено, отменено"))
        return
    if spec.parse is None:
        append_log(state, ui.error_fragments(f"{spec.title}: своё значение не поддерживается"))
        return
    try:
        value = spec.parse(text)
    except ValueError as exc:
        append_log(state, ui.error_fragments(f"{spec.title}: {exc}"))
        return
    set_param(state, name, value)


def toggle_mouse(state: State) -> None:
    """Аварийный выход: вернуть мышь терминалу целиком.

    Обычно этого не требуется — клики и выделение текста уживаются (см. `click_only_mouse`).
    Команда оставлена на случай терминала, который отслеживание нажатий понимает, а выделение
    при нём всё равно отдаёт приложению.
    """
    state.mouse_enabled = not state.mouse_enabled
    if state.mouse_enabled:
        append_log(state, ui.system_fragments("мышь у harness: клики по вкладкам, панелям и строкам списка"))
    else:
        append_log(state, ui.system_fragments("мышь целиком у терминала: клики в harness не действуют"))
    refresh(state)


def cmd_remember(state: State, arg: str) -> None:
    """`/remember <текст>` — положить факт о человеке в глобальную память.

    Без аргумента панель выбора НЕ открывается, в отличие от `/model` и `/profile`: факт
    пишется словами, и списка вариантов, из которого его можно выбрать, не существует."""
    текст = arg.strip()
    if not текст:
        append_log(state, ui.hint_fragments("нужен текст факта: /remember зовут Александр"))
        return
    добавлен, сообщение = memory.add_fact(текст)
    if добавлен:
        забыть_счёт_занятости(state)
        append_log(state, ui.fact_added_fragments(сообщение))
        return
    append_log(state, ui.error_fragments(сообщение))


def cmd_memory(state: State, arg: str) -> None:
    """`/memory` — что известно о человеке; `/memory on` и `/memory off` — сбор фактов.

    Выключатель сохраняется в настройках инструмента, а не в профиле: память про самого
    человека и его папку, а профиль отвечает лишь на вопрос «кем сейчас работает модель».
    Выключение не стирает уже записанного: `/remember` и `/forget` работают по-прежнему,
    молчит только архивариус."""
    ключ = arg.strip().lower()
    if ключ in ("on", "off"):
        state.config.remember = ключ == "on"
        save_config(state.config)
        append_log(
            state,
            ui.system_fragments(
                "сбор фактов включён — архивариус выписывает их из разговора"
                if state.config.remember
                else "сбор фактов выключен — факты записываются только командой /remember"
            ),
        )
        return
    if ключ:
        append_log(state, ui.error_fragments(f"не понимаю «{ключ}» — /memory, /memory on или /memory off"))
        return
    факты, предупреждения = memory.load_facts()
    for предупреждение in предупреждения:
        append_log(state, ui.error_fragments(предупреждение))
    # Состояние выключателя — строкой над списком: без неё непонятно, почему память не
    # пополняется сама, и человек ищет поломку там, где стоит его же выбор.
    append_log(
        state,
        ui.system_fragments(
            "сбор фактов включён (/memory off — выключить)"
            if state.config.remember
            else "сбор фактов выключен (/memory on — включить)"
        ),
    )
    append_log(state, ui.facts_fragments(факты))


def cmd_forget(state: State, arg: str) -> None:
    """`/forget <номер>` — убрать факт по номеру, каким его показал `/memory`."""
    текст = arg.strip()
    if not текст:
        append_log(state, ui.hint_fragments("нужен номер: /forget 2 (номера показывает /memory)"))
        return
    try:
        номер = int(текст)
    except ValueError:
        append_log(state, ui.error_fragments(f"«{текст}» — не номер; номера фактов показывает /memory"))
        return
    убран, сообщение = memory.remove_fact(номер)
    if убран:
        забыть_счёт_занятости(state)
        append_log(state, ui.system_fragments(f"забыто: {сообщение}"))
        return
    append_log(state, ui.error_fragments(сообщение))


def cmd_tokens(state: State) -> None:
    """`/tokens` — следующий запрос активной панели, её расход и общий расход сеанса.

    Активный Agent и сеанс показаны раздельно. Первый отвечает на вопрос о выбранной
    стратегии или ветви, второй включает также группы, способы, извлекатели и уже закрытых
    агентов. Полную системную часть берём у самого Agent: в Sticky Facts туда входят факты
    именно этой панели, а не главного разговора.
    """
    итог = state.session_usage_total()
    pane = _active_input_pane(state)
    агент = pane.agent or state.main_agent
    profile = _pane_profile(state, pane) or state.profile
    strategy = profile.context_strategy
    full_pairs = len(агент.history()) // 2
    pairs = (
        min(full_pairs, profile.strategy_window)
        if strategy
        in (
            context_strategy.CONTEXT_SLIDING,
            context_strategy.CONTEXT_FACTS,
        )
        else full_pairs
    )
    session_runs = state.retired_runs + sum(
        другой.runs for другой in state.agents()
    )
    append_log(
        state,
        ui.tokens_report_fragments(
            state.model,
            history=агент.history_tokens(),
            pairs=pairs,
            # Взвешивается ровно системная часть следующего запроса активного Agent:
            # инструкция, глобальные сведения, Sticky Facts либо действующая выжимка.
            system=tokens.count_text(агент.system_text()),
            overhead=агент.overhead(state.model),
            restored=агент.restored_pairs,
            runs=session_runs,
            usage=итог,
            budget=profile.budget_tokens,
            cost=tokens.format_price(
                state.session_cost if state.session_cost_known else None
            ),
            active_name=агент.name,
            active_strategy=strategy,
            active_runs=агент.runs,
            active_usage=агент.session_usage,
            active_usage_known=id(агент)
            not in state.unknown_usage_agent_ids,
            session_usage_known=state.session_usage_known,
        ),
    )


def cmd_budget(state: State, arg: str) -> None:
    """`/budget [токены]` — предел веса запроса; 0 снимает предел.

    Почему это отдельная команда, а НЕ параметр `/set`. В параметрах генерации живут только
    ручки самого DeepSeek — правило проекта, заведённое затем, чтобы `/params` можно было
    сверять со страницей документации поставщика построчно. Предел веса запроса в запрос не
    уходит вовсе: это наша политика обрезки памяти перед отправкой. Положи его к параметрам —
    и человек искал бы его в документации DeepSeek, где такого поля нет и не будет.

    Значение кладётся в профиль, но на диск не пишется: `/budget` — прикидка на сеанс, а
    насовсем предел закрепляет `/profile save`. Поэтому здесь же поднимается признак
    «профиль изменён» — тот самый, по которому строка состояния предупреждает о
    несохранённых правках.
    """
    текст = arg.strip()
    if not текст:
        if state.profile.budget_tokens > 0:
            append_log(
                state,
                ui.system_fragments(
                    f"предел веса запроса профиля «{state.profile.name}»: "
                    f"{ui.format_exact(state.profile.budget_tokens)} токенов"
                ),
            )
        else:
            append_log(state, ui.system_fragments("предел веса запроса не задан — память режет только окно по парам"))
        append_log(state, ui.hint_fragments("задать: /budget 3000; снять: /budget 0"))
        return
    try:
        предел = int(текст)
    except ValueError:
        append_log(state, ui.error_fragments(f"«{текст}» — не число токенов; например: /budget 3000"))
        return
    if предел < 0:
        append_log(state, ui.error_fragments("предел не бывает отрицательным; 0 — без предела"))
        return
    # Признак «профиль изменён» поднимаем только на настоящей смене значения: повторный
    # `/budget 3000` ничего не менял, а строка состояния уверяла бы, что есть несохранённое.
    if предел != state.profile.budget_tokens:
        state.profile.budget_tokens = предел
        state.profile_dirty = True
    if предел == 0:
        append_log(state, ui.system_fragments("предел веса запроса снят — память режет только окно по парам"))
        return
    append_log(state, ui.system_fragments(f"предел веса запроса: {ui.format_exact(предел)} токенов"))
    if предел < tokens.BASE_OVERHEAD:
        # Ровно то же предупреждение, что даёт разбор профиля: в такой предел не влезает даже
        # пустой запрос — одна обёртка разговора весит больше. Узнавать об этом по поведению
        # («почему модель ничего не помнит?») человек не должен.
        append_log(
            state,
            ui.hint_fragments(
                f"это меньше веса пустого запроса ({tokens.BASE_OVERHEAD}) — "
                f"память будет обрезана до последней пары"
            ),
        )
    append_log(state, ui.hint_fragments("сохранить в профиль: /profile save <имя>"))


def сбросить_счёт_отказов(state: State) -> None:
    """Обнулить счёт отказов подряд у фоновых служб — при очистке разговора.

    Отказы считаются ПОДРЯД и означают «служба не работает прямо сейчас». Новый разговор —
    новая почва: тащить в него два отказа, случившиеся до очистки, значило бы выключить
    сжатие на третьей неудаче, две из которых относятся к разговору, которого больше нет.
    Архивариус сбрасывается заодно: его счёт `/clear` переживал с самого начала, и это была
    та же шероховатость, просто незамеченная.

    Признак «служба выключена» и признак «о сбое уже сказали» НЕ трогаем: и то и другое
    сказано человеку про СЕАНС, а не про разговор, — «выключено до конца сеанса» обязано
    значить именно это, иначе обещание в ленте перестаёт быть правдой.
    """
    for служба in (state.сжиматель, state.архивариус):
        служба.отказов_подряд = 0


def cmd_context(state: State) -> None:
    """Показать выжимку разговора ДОСЛОВНО, целиком, вместе с тремя числами и поколением.

    Это не удобство, а условие, при котором пересказ вообще допущен в запрос: обещание «что
    модель видела, человек может прочитать» держится на этой команде одной. Поэтому показ
    дословный — сокращённый пересказ пересказа отвечал бы на вопрос «примерно о чём выжимка»,
    а спрашивают ровно то, что ушло в модель.

    Три числа, а не одно: пройденное разговором не равно пересказанному, и теряется оно
    двумя разными способами — сжатие не успело (пары ушли дословно) либо выжимка переполнилась
    (сняты её старейшие пункты). Снятые считаются ПУНКТАМИ: пункт парам не сопоставлен — он
    мог родиться из одной пары, из пяти или из половины разговора, — и пересчитать их в пары
    значило бы выдумать число.
    """
    агент = state.main_agent
    выжимка = агент.выжимка()
    if выжимка is None:
        if агент.порог_сжатия() <= 0:
            append_log(state, ui.system_fragments("выжимки нет: сжатие выключено полем профиля compact_at"))
        else:
            append_log(state, ui.system_fragments("выжимки нет — разговор ещё не сжимали"))
        return
    заголовок = (
        f"выжимка разговора, сжатие {выжимка.поколение}-е: "
        f"за нею {выжимка.граница} пар, забыто дословно {выжимка.забыто_дословно}"
    )
    append_log(state, ui.system_fragments(заголовок))
    for номер, пункт in enumerate(выжимка.пункты, 1):
        append_log(state, [("", f"  {номер}. {пункт}\n")])
    if агент.выжимка_отвергнута():
        append_log(
            state,
            ui.system_fragments("в запрос она сейчас не идёт: собрана под другой системной инструкцией"),
        )


def cmd_compact(state: State) -> None:
    """Сжать память по требованию человека — все пары, кроме последней.

    Долю от памяти команда не берёт: человек, отдавший её, просит минимальную память, а не
    часть от неё — доля потребовала бы от него знать, какую именно, и назначать её на глаз.
    Последняя пара остаётся по сквозному правилу: иначе агент забудет то, о чём его только
    что спросили.

    Заход идёт в фоне и ввод не блокирует — то же правило, что и у самостоятельного сжатия:
    задерживать человека ради вспомогательного механизма нельзя.

    Отказы называются вслух, а не проглатываются: команда, которая молча ничего не делает,
    отлаживается как поломка инструмента.
    """
    агент = state.main_agent
    if not агент.profile.keep_history:
        # Память такого профиля в запрос не идёт вовсе: пересказывать нечего, а запрос
        # сжимателя стоит денег на той же дорогой модели, что ведёт разговор.
        append_log(
            state,
            ui.error_fragments("этот профиль не хранит историю — сжимать нечего"),
        )
        return
    if агент.порог_сжатия() <= 0:
        append_log(
            state,
            ui.error_fragments("сжатие выключено полем профиля compact_at — включить: /set compact_at"),
        )
        return
    if state.сжиматель.выключена:
        append_log(
            state,
            ui.error_fragments("сжатие выключено до конца сеанса: подряд не удались три попытки"),
        )
        return
    if state.client is None:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return
    if compact.идёт(state):
        append_log(state, ui.system_fragments("сжатие уже идёт — второе не заводим"))
        return
    # Считаем и память, и хвост за границей выжимки. Смотри мы на одну память — команда
    # отказывала бы ровно в том случае, ради которого дочитывание и заведено: при запуске на
    # старой сессии в порог поднимается одна пара, лента обещает «остальное не пересказано —
    # /compact соберёт выжимку», а команда отвечает «сжимать нечего». Обещание, нарушенное
    # инструментом в следующую же секунду, хуже несделанного.
    if len(агент.history()) // 2 - 1 <= 0 and not compact.дочитать(state):
        append_log(
            state,
            ui.system_fragments("сжимать нечего: последняя пара не выбрасывается никогда"),
        )
        return
    append_log(state, ui.system_fragments("сжимаю разговор — ввод не заблокирован"))
    background.завести(state.сжиматель, compact.run(state, вручную=True))


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


# ─────────────────────────────── список агентов ───────────────────────────────


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


async def handle_command(text: str, state: State) -> bool:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit"):
        return True
    if state.switching_profile:
        append_log(
            state,
            ui.error_fragments("сначала дождитесь завершения смены профиля"),
        )
        return False
    if cmd == "/help":
        append_log(state, ui.help_fragments())
    elif cmd == "/auth":
        cmd_auth(state)
    elif cmd == "/model":
        await cmd_model(state, arg)
    elif cmd == "/profile":
        await cmd_profile(state, arg)
    elif cmd == "/params":
        cmd_params(state)
    elif cmd == "/set":
        cmd_set(state, arg)
    elif cmd == "/system":
        # на панели исполнителя показываем его инструкцию: именно её там свернули до строки
        source = state.screen.pane.profile or state.profile
        append_log(
            state,
            ui.system_prompt_fragments(source.name, source.system),
        )
    elif cmd == "/strategy":
        cmd_strategy(state, arg)
    elif cmd == "/facts":
        cmd_facts(state, arg)
    elif cmd == "/branch":
        await cmd_branch(state, arg)
    elif cmd == "/team":
        cmd_team(state, arg)
    elif cmd == "/mouse":
        toggle_mouse(state)
    elif cmd == "/clear":
        # Идущий заход сжимателя снимаем ПЕРВЫМ делом. Своего итога он после этого не
        # применит и без отмены — применение сверяется с поколением памяти, — но платить за
        # запрос, чей итог заведомо выброшен, незачем.
        background.отменить(state.сжиматель)
        # Выжимку и заготовку убирает сам агент (`forget`): оставь их — и человек, стёрший
        # разговор, продолжил бы говорить с моделью, которая помнит его пересказ.
        state.main_agent.forget()
        сбросить_счёт_отказов(state)
        # Мало забыть разговор в памяти: не открой мы новую сессию, следующий запуск поднял
        # бы очищенное обратно с диска — издевательство, а не очистка.
        open_new_session(state)
        append_log(
            state,
            ui.system_fragments("главный разговор очищен — начат новый разговор"),
        )
    elif cmd == "/context":
        cmd_context(state)
    elif cmd == "/compact":
        cmd_compact(state)
    elif cmd == "/remember":
        cmd_remember(state, arg)
    elif cmd == "/memory":
        cmd_memory(state, arg)
    elif cmd == "/forget":
        cmd_forget(state, arg)
    elif cmd == "/tokens":
        cmd_tokens(state)
    elif cmd == "/budget":
        cmd_budget(state, arg)
    else:
        append_log(
            state,
            ui.error_fragments(f"неизвестная команда: {cmd} (см. /help)"),
        )
    return False


async def handle_submit(
    raw_text: str,
    state: State,
    *,
    destination_screen: screens_mod.Screen | None = None,
    destination_pane: screens_mod.Pane | None = None,
) -> None:
    if state.awaiting_key:
        state.awaiting_key = False
        await do_auth(raw_text, state)
        return

    if state.awaiting_custom:
        apply_custom_value(state, raw_text)
        return

    text = raw_text.strip()
    if not text:
        return

    if text.startswith("/"):
        if await handle_command(text, state):
            if state.app is not None:
                state.app.exit()
        return

    if not state.config.is_authorized:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return

    addressed_screen = destination_screen or (
        state.screen if state.screen.interactive else state.main
    )
    addressed_pane = destination_pane or addressed_screen.pane
    if state.switching_profile:
        append_log(
            state,
            ui.error_fragments("нельзя отправить вопрос: выполняется смена профиля"),
            addressed_pane,
        )
        return
    request = Request(content=text, pane=addressed_pane)
    if addressed_screen is state.main:
        append_log(state, ui.user_fragments(text), addressed_pane)
        was_busy = state.busy or not state.queue.empty()
        state.queue.put_nowait(request)
        if was_busy:
            append_log(
                state,
                ui.queued_fragments(state.queue.qsize()),
                addressed_pane,
            )
    else:
        executor = pane_worker(state, addressed_pane)
        was_busy = executor.busy or not executor.queue.empty()
        if not executor.enqueue(request):
            append_log(
                state,
                ui.error_fragments("экран закрывается — вопрос не принят"),
                addressed_pane,
            )
            return
        append_log(state, ui.user_fragments(text), addressed_pane)
        if was_busy:
            append_log(
                state,
                ui.queued_fragments(executor.queue.qsize()),
                addressed_pane,
            )


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
