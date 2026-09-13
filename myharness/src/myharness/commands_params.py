"""Команды про ручки генерации: `/params`, `/set`, своё значение и `/mouse`.

Какие ручки вообще существуют и что для них допустимо — знает `params`; здесь только показ,
выбор и запись выбранного в профиль. `/mouse` стоит рядом не по смыслу, а по устройству: это
тоже переключатель сеанса, живущий одной строкой в состоянии.
"""

from __future__ import annotations

from typing import Any

from . import ui
from . import params as params_mod
from . import picker as picker_mod
from .output import append_log, refresh
from .state import State


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
