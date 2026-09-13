"""Команды про то, ЧЕМ отвечает модель: ключ, модель, профиль, режим управления контекстом.

Команды собраны вместе не по алфавиту: все они меняют не содержимое разговора, а условия, в
которых он идёт. Три из четырёх — `/model`, `/profile`, `/strategy` — работают двумя способами:
доводом в строке (`/model deepseek-v4-pro`) и панелью выбора, когда довода нет; панель у них
общая (`picker`), и правится этот способ в одном месте. `/auth` стоит особняком: довода он не
берёт вовсе и панели не открывает, а ждёт следующую строку — ключ не показывают списком.
"""

from __future__ import annotations

import asyncio
from typing import Any

from . import context_strategy, profiles, ui
from . import picker as picker_mod
from .api import DeepSeekClient
from .config import save as save_config
from .output import append_log, refresh
from .panes import active_profile
from .state import State
from .strategies import STRATEGY_TITLES, switch_profile, use_strategy
from .workers import _track_submission
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
