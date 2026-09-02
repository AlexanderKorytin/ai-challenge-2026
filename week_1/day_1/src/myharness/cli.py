"""Полноэкранный REPL: лог-панель сверху (растёт, автопрокрутка), поле ввода снизу,
окаймлённое горизонтальными линиями. Очередь запросов, отмена по Ctrl+C, потоковый ответ,
всплывающее меню команд по «/» и панель выбора значений параметров генерации."""

from __future__ import annotations

import argparse
import asyncio
import os
import time
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from prompt_toolkit.application import Application
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import ConditionalContainer, Float, FloatContainer, HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import TextArea

from . import api, journal, profiles, team, ui
from . import params as params_mod
from . import picker as picker_mod
from . import screens as screens_mod
from .api import DeepSeekClient
from .config import Config
from .config import load as load_config
from .config import save as save_config
from .profiles import Profile

Fragments = list[tuple[str, str]]


@dataclass
class Request:
    """Единица очереди. Ведущий задаётся только для разового запуска группы через /team —
    в остальных случаях берётся профиль, активный на момент отправки."""

    content: str
    lead: Profile | None = None
    screen: screens_mod.Screen | None = None  # ввод с рабочего экрана уходит в него же


@dataclass
class State:
    config: Config
    client: DeepSeekClient | None
    model: str
    profile: Profile
    app: Application | None = None
    screens: list[screens_mod.Screen] = field(default_factory=lambda: [screens_mod.main_screen()])
    active: int = 0
    queue: asyncio.Queue[Request] = field(default_factory=asyncio.Queue)
    current_task: asyncio.Task | None = None
    busy: bool = False
    awaiting_key: bool = False
    awaiting_custom: str | None = None  # имя параметра, для которого ждём своё значение
    picker: picker_mod.Picker | None = None
    profile_dirty: bool = False  # параметры меняли, но профиль не сохранён
    known_models: list[str] = field(default_factory=lambda: list(api.FALLBACK_MODELS))
    journal_warned: bool = False
    input_buffer: Any = None  # буфер строки ввода: профиль подставляет в него заготовку
    mouse_enabled: bool = True  # False — мышь отдана терминалу, чтобы выделять и копировать

    @property
    def main(self) -> screens_mod.Screen:
        """Экран пользователя: сюда идут команды, вопросы и сводка группы."""
        return self.screens[0]

    @property
    def screen(self) -> screens_mod.Screen:
        """Показываемый сейчас экран — он же и прокручивается."""
        return self.screens[self.active if 0 <= self.active < len(self.screens) else 0]

    @property
    def focus(self) -> screens_mod.Screen:
        """Куда писать вывод команд: на рабочем экране — в него, на экране агента (он только
        для чтения) — в главный, иначе сообщение осталось бы незамеченным."""
        current = self.screen
        return current if current.interactive else self.main

    @property
    def log(self) -> Fragments:
        return self.main.log

    @property
    def messages(self) -> list[dict]:
        return self.main.messages


def append_log(state: State, fragments: Fragments, screen: screens_mod.Screen | None = None) -> None:
    """Без указания экрана вывод идёт на тот, где пользователь работает (см. State.focus)."""
    target = screen or state.focus
    target.log.extend(fragments)
    target.line_count += sum(text.count("\n") for _, text in fragments)
    if state.app is not None:
        state.app.invalidate()


def truncate_log(state: State, mark: int, screen: screens_mod.Screen | None = None) -> None:
    target = screen or state.focus
    removed = target.log[mark:]
    target.line_count -= sum(text.count("\n") for _, text in removed)
    del target.log[mark:]
    if state.app is not None:
        state.app.invalidate()


def switch_screen(state: State, index: int) -> None:
    if not 0 <= index < len(state.screens) or index == state.active:
        return
    state.active = index
    screen = state.screen
    screen.autoscroll = True  # переключились — показываем свежий конец ленты
    if screen.interactive and screen.profile is not None:
        # набранное пользователем не затираем: заготовка подставляется только в пустую строку
        apply_prefill(state, screen.profile, only_if_empty=True)
    refresh(state)


def drop_agent_screens(state: State) -> None:
    """Экраны агентов и рабочие экраны живут ровно столько, сколько профиль, который их
    завёл: сменился профиль — прежние ленты уже не о чем."""
    del state.screens[1:]
    state.active = 0


def refresh(state: State) -> None:
    if state.app is not None:
        state.app.invalidate()


# ─────────────────────────────── профиль и запрос ───────────────────────────────


def build_request_messages(state: State, content: str) -> list[dict]:
    """Системная инструкция всегда первая: так её видно отдельно от ввода пользователя,
    и так же работает кэширование общего начала запроса на стороне DeepSeek."""
    messages: list[dict] = []
    if state.profile.system:
        messages.append({"role": "system", "content": state.profile.system})
    if state.profile.keep_history:
        messages.extend(state.messages)
    else:
        messages.append({"role": "user", "content": content})
    return messages


def switch_profile(state: State, name: str) -> None:
    profile, warnings = profiles.load(name)
    state.profile = profile
    state.profile_dirty = False
    state.config.profile = profile.name
    save_config(state.config)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    # история, набранная под прежней инструкцией, исказила бы следующий ответ
    if state.main.messages:
        state.main.messages.clear()
        append_log(state, ui.system_fragments("история диалога очищена — профиль сменился"))
    if len(state.screens) > 1:
        drop_agent_screens(state)
        append_log(state, ui.system_fragments("экраны прежней группы закрыты"))
    source = str(profile.source) if profile.source else "встроенный"
    append_log(state, ui.system_fragments(f"профиль: {profile.name} ({source})"))
    if not profile.keep_history:
        append_log(state, ui.system_fragments("в этом профиле каждый запрос уходит без истории"))
    if profile.agents:
        append_log(state, ui.team_list_fragments(profile.name, profile.agents))
    if profile.screens:
        open_work_screens(state, profile)
        return
    apply_prefill(state, profile)


def apply_prefill(state: State, profile: Profile, *, only_if_empty: bool = False) -> None:
    """Заготовку ввода кладём в строку ввода, а не отправляем сами: пользователь видит текст,
    может его поправить и отправляет сам — Enter'ом."""
    if not profile.prefill or state.input_buffer is None:
        return
    if only_if_empty and state.input_buffer.text.strip():
        return
    text = profile.prefill.strip()
    state.input_buffer.text = text
    state.input_buffer.cursor_position = len(text)
    append_log(state, ui.system_fragments("заготовка вопроса подставлена в строку ввода — Enter отправит её"))


def open_work_screens(state: State, profile: Profile) -> None:
    """Профиль со списком screens раскладывает приём по вкладкам: каждый экран — свой шаг со
    своей инструкцией и своей заготовкой ввода. Ввод уходит в тот экран, который открыт."""
    opened: list[str] = []
    for name in profile.screens:
        step, warnings = profiles.load(name)
        for warning in warnings:
            append_log(state, ui.error_fragments(f"экран «{name}»: {warning}"))
        if step.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            append_log(state, ui.error_fragments(f"экран «{name}» пропущен: профиль не найден"))
            continue
        screen = screens_mod.Screen(key=name, title=name, profile=step, interactive=True)
        state.screens.append(screen)
        # описание профиля — вводная для шага («вставьте промпт с первого экрана»). Держим её
        # в ленте, а не в строке ввода: заготовка ввода ушла бы в модель вместе с вопросом.
        if step.description:
            append_log(state, ui.system_fragments(step.description), screen)
        opened.append(name)
    if not opened:
        append_log(state, ui.error_fragments("рабочие экраны не открыты: профили не найдены"))
        return
    append_log(state, ui.work_screens_fragments(opened))
    switch_screen(state, 1)


# ─────────────────────────────── генерация ответа ───────────────────────────────


async def _spin(state: State, screen: screens_mod.Screen) -> None:
    mark = len(screen.log)
    i = 0
    try:
        while True:
            frame = ui.SPINNER_FRAMES[i % len(ui.SPINNER_FRAMES)]
            truncate_log(state, mark, screen)
            append_log(state, [("class:dim", f"{frame} думаю…")], screen)
            i += 1
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        truncate_log(state, mark, screen)
        raise


def record(state: State, entry: dict[str, Any]) -> None:
    error = journal.append(entry)
    if error and not state.journal_warned:
        state.journal_warned = True
        append_log(state, ui.error_fragments(error))


@dataclass
class Turn:
    """Итог одного обмена с моделью — тем, кто позвал: тексту ответа и цене."""

    status: str
    text: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


async def generate_response(
    state: State,
    request_messages: list[dict],
    user_text: str,
    *,
    screen: screens_mod.Screen | None = None,
    profile: Profile | None = None,
    agent: str | None = None,
    run_id: str | None = None,
) -> Turn:
    """Один запрос к модели с потоковым выводом в свой экран.

    Экран и профиль задаются явно, потому что агенты группы отвечают одновременно: у каждого
    своя лента, своя системная инструкция и свои параметры, а State у них общий.
    """
    assert state.client is not None
    target = screen or state.main
    active_profile = profile or state.profile
    target.status = screens_mod.BUSY
    spinner_task = asyncio.create_task(_spin(state, target))

    async def clear_spinner() -> None:
        if not spinner_task.done():
            spinner_task.cancel()
            with suppress(asyncio.CancelledError):
                await spinner_task

    reasoning_started = False
    answer_started = False
    answer_text = ""
    reasoning_text = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = {}
    status = "error"
    error_text: str | None = None
    started = time.monotonic()
    try:
        async for event in state.client.stream_chat(state.model, request_messages, active_profile.params):
            await clear_spinner()
            if event.kind == "meta":
                finish_reason = event.finish_reason
                usage = event.usage
                continue
            if event.kind == "reasoning":
                if not reasoning_started:
                    append_log(state, ui.reasoning_label_fragments(), target)
                    reasoning_started = True
                reasoning_text += event.text
                append_log(state, [("class:dim", event.text)], target)
            else:
                if not answer_started:
                    if reasoning_started:
                        append_log(state, [("", "\n")], target)
                    append_log(state, ui.answer_label_fragments(), target)
                    answer_started = True
                append_log(state, [("", event.text)], target)
                answer_text += event.text
        status = "ok"
    except asyncio.CancelledError:
        await clear_spinner()
        status = "cancelled"
        raise
    except Exception as exc:  # сеть, лимиты, ошибки API — не роняем harness
        await clear_spinner()
        error_text = str(exc)
        append_log(state, ui.error_fragments(f"ошибка запроса к DeepSeek: {exc}"), target)
    finally:
        await clear_spinner()
        elapsed = time.monotonic() - started
        target.status = screens_mod.DONE if status == "ok" else screens_mod.ERROR
        if reasoning_started or answer_started:
            append_log(state, [("", "\n")], target)
        if status == "ok":
            append_log(state, ui.meta_fragments(finish_reason, usage, elapsed, active_profile.name), target)
            if finish_reason == "length":
                append_log(
                    state,
                    ui.hint_fragments("ответ упёрся в max_tokens — увеличьте лимит: /set max_tokens"),
                    target,
                )
        if status == "ok" and answer_text and active_profile.keep_history:
            target.messages.append({"role": "assistant", "content": answer_text})
        elif active_profile.keep_history and target.messages and target.messages[-1]["role"] == "user":
            # ответ не получен (ошибка/отмена) — не оставляем в истории вопрос без ответа
            target.messages.pop()
        entry: dict[str, Any] = {
            "status": status,
            "model": state.model,
            "profile": active_profile.snapshot(),
            "query": user_text,
            "messages": request_messages,
            "response": answer_text or None,
            "reasoning": reasoning_text or None,
            "finish_reason": finish_reason,
            "usage": usage or None,
            "elapsed_ms": int(elapsed * 1000),
            "error": error_text,
        }
        if agent:
            entry["agent"] = agent
        if run_id:
            entry["run_id"] = run_id
        record(state, entry)
    return Turn(
        status=status,
        text=answer_text,
        reasoning=reasoning_text,
        finish_reason=finish_reason,
        usage=usage,
        elapsed_ms=int((time.monotonic() - started) * 1000),
        error=error_text,
    )


async def worker(state: State) -> None:
    while True:
        request = await state.queue.get()
        lead = request.lead or state.profile
        state.busy = True
        if request.screen is not None and request.screen.profile is not None:
            screen = request.screen
            messages = screens_mod.build_messages(screen.profile, screen, request.content)
            task = asyncio.create_task(
                generate_response(state, messages, request.content, screen=screen, profile=screen.profile)
            )
        elif lead.agents:
            task = asyncio.create_task(team.run(state, request.content, lead))
        else:
            if lead.keep_history:
                state.main.messages.append({"role": "user", "content": request.content})
            request_messages = build_request_messages(state, request.content)
            task = asyncio.create_task(generate_response(state, request_messages, request.content))
        state.current_task = task
        try:
            await task
        except asyncio.CancelledError:
            append_log(state, ui.system_fragments("запрос отменён"))
        finally:
            state.current_task = None
            state.busy = False
            state.queue.task_done()


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
        items.append(picker_mod.Item(label=name, hint="текущая" if name == state.model else "", payload=name))

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
        items.append(picker_mod.Item(label=name, hint=hint, payload=name))

    def choose(payload: Any) -> None:
        state.picker = None
        switch_profile(state, str(payload))

    description = "какой профиль генерации применить"
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


def cmd_profile(state: State, arg: str) -> None:
    if not arg:
        open_profile_picker(state)
        return
    parts = arg.split(maxsplit=1)
    if parts[0] == "save":
        name = parts[1].strip() if len(parts) > 1 else state.profile.name
        state.profile.name = name
        try:
            path = profiles.save(state.profile)
        except OSError as exc:
            append_log(state, ui.error_fragments(f"не удалось сохранить профиль: {exc}"))
            return
        state.profile_dirty = False
        state.config.profile = name
        save_config(state.config)
        append_log(state, ui.system_fragments(f"профиль сохранён: {path}"))
        return
    switch_profile(state, parts[0])


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
    """Полноэкранный режим забирает мышь себе, и выделить текст мышью становится нельзя —
    терминал не видит ни нажатий, ни протяжек. Здесь мышь можно вернуть терминалу: тогда
    выделение и копирование работают как обычно, а клики по вкладкам и прокрутка колесом
    временно отключаются."""
    state.mouse_enabled = not state.mouse_enabled
    if state.mouse_enabled:
        append_log(state, ui.system_fragments("мышь снова у harness: клики по вкладкам и прокрутка колесом"))
    else:
        append_log(state, ui.system_fragments("мышь отдана терминалу: выделяйте и копируйте текст обычным образом"))
        append_log(state, ui.hint_fragments("вернуть мышь harness — F2 или /mouse"))
    refresh(state)


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
    append_log(state, ui.user_fragments(text))
    was_busy = state.busy
    state.queue.put_nowait(Request(content=text, lead=lead))
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()))


async def handle_command(text: str, state: State) -> bool:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit"):
        return True
    if cmd == "/help":
        append_log(state, ui.help_fragments())
    elif cmd == "/auth":
        cmd_auth(state)
    elif cmd == "/model":
        await cmd_model(state, arg)
    elif cmd == "/profile":
        cmd_profile(state, arg)
    elif cmd == "/params":
        cmd_params(state)
    elif cmd == "/set":
        cmd_set(state, arg)
    elif cmd == "/system":
        append_log(state, ui.system_prompt_fragments(state.profile.name, state.profile.system))
    elif cmd == "/team":
        cmd_team(state, arg)
    elif cmd == "/mouse":
        toggle_mouse(state)
    elif cmd == "/clear":
        state.main.messages.clear()
        append_log(state, ui.system_fragments("история диалога очищена"))
    else:
        append_log(state, ui.error_fragments(f"неизвестная команда: {cmd} (см. /help)"))
    return False


async def handle_submit(raw_text: str, state: State) -> None:
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

    screen = state.screen if state.screen.interactive and state.screen is not state.main else None
    append_log(state, ui.user_fragments(text), screen)
    was_busy = state.busy
    state.queue.put_nowait(Request(content=text, screen=screen))
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()), screen)


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


def build_app(state: State) -> Application:
    output_control = FormattedTextControl(text=lambda: state.screen.log, show_cursor=False)
    output_window = LogWindow(
        content=output_control,
        wrap_lines=True,
        always_hide_cursor=True,
        height=Dimension(weight=1),
        on_manual_scroll=lambda: setattr(state.screen, "autoscroll", False),
    )
    def cursor_position() -> Point:
        """Курсор держим в конце ленты активного экрана — на нём и стоит автопрокрутка."""
        screen = state.screen
        return Point(x=0, y=screen.line_count) if screen.autoscroll else Point(x=0, y=output_window.vertical_scroll)

    output_control.get_cursor_position = cursor_position

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
            )
        ),
        height=1,
        style="class:status",
    )

    tabs_window = Window(
        content=FormattedTextControl(
            text=lambda: ui.tabs_fragments(
                [(screen.title, screen.status) for screen in state.screens],
                state.active,
                lambda index: switch_screen(state, index),
            ),
            focusable=False,
        ),
        height=1,
        style="class:tabs",
    )
    # Пока агентов нет, полоса не нужна — она только отнимала бы строку экрана.
    tabs_area = ConditionalContainer(tabs_window, filter=Condition(lambda: len(state.screens) > 1))

    picker_active = Condition(lambda: state.picker is not None)
    picker_window = Window(
        content=FormattedTextControl(text=lambda: picker_mod.fragments(state.picker) if state.picker else []),
        style="class:panel",
        dont_extend_height=True,
        dont_extend_width=True,
    )

    root = FloatContainer(
        content=HSplit([output_window, sep(), input_area, tabs_area, sep(), status_window]),
        floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12, scroll_offset=1)),
            Float(left=2, bottom=4, content=ConditionalContainer(picker_window, filter=picker_active)),
        ],
    )
    layout = Layout(root, focused_element=input_area)
    state.input_buffer = input_area.buffer

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
        text = buffer.text
        buffer.reset()
        if not state.screen.interactive:
            state.active = 0  # экран агента только для чтения — ответ придёт в главный
        state.screen.autoscroll = True  # новое сообщение — вернуться к живому выводу
        asyncio.get_running_loop().create_task(handle_submit(text, state))

    @kb.add("c-c", filter=~picker_active)
    def _cancel(event) -> None:  # noqa: ANN001
        if state.busy and state.current_task is not None:
            state.current_task.cancel()

    @kb.add("c-d")
    def _quit(event) -> None:  # noqa: ANN001
        event.app.exit()

    @kb.add("f2")
    def _toggle_mouse(event) -> None:  # noqa: ANN001
        toggle_mouse(state)

    @kb.add("pageup")
    def _scroll_up(event) -> None:  # noqa: ANN001
        state.screen.autoscroll = False
        output_window.vertical_scroll = max(0, output_window.vertical_scroll - 10)

    @kb.add("pagedown")
    def _scroll_down(event) -> None:  # noqa: ANN001
        state.screen.autoscroll = False
        output_window.vertical_scroll += 10

    @kb.add("c-end")
    def _resume_autoscroll(event) -> None:  # noqa: ANN001
        state.screen.autoscroll = True

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


async def repl(state: State) -> None:
    app = build_app(state)
    state.app = app
    append_log(state, ui.banner_fragments(state.model, state.config.is_authorized, state.profile.name))
    if state.profile.system:
        append_log(state, ui.system_fragments("профиль задаёт системную инструкцию — показать: /system"))
    worker_task = asyncio.create_task(worker(state))
    try:
        await app.run_async()
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        if state.client:
            await state.client.aclose()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="myharness", description="Терминальный harness для DeepSeek API")
    parser.add_argument("--profile", help="профиль генерации, применяемый при старте")
    parser.add_argument("--model", help="модель DeepSeek")
    return parser.parse_args(argv)


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


async def _main(argv: list[str] | None = None) -> None:
    silence_transport_noise(asyncio.get_running_loop())
    args = parse_args(argv)
    cfg = load_config()
    profile_name = args.profile or os.environ.get("MYHARNESS_PROFILE") or cfg.profile
    profile, warnings = profiles.load(profile_name)
    if args.model:
        cfg.model = args.model
    client = DeepSeekClient(cfg.api_key) if cfg.is_authorized else None
    state = State(config=cfg, client=client, model=cfg.model, profile=profile)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    await repl(state)


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(_main())
