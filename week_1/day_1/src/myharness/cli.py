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

from . import api, journal, profiles, ui
from . import params as params_mod
from . import picker as picker_mod
from .api import DeepSeekClient
from .config import Config
from .config import load as load_config
from .config import save as save_config
from .profiles import Profile

Fragments = list[tuple[str, str]]


@dataclass
class State:
    config: Config
    client: DeepSeekClient | None
    model: str
    profile: Profile
    app: Application | None = None
    messages: list[dict] = field(default_factory=list)
    queue: asyncio.Queue[str] = field(default_factory=asyncio.Queue)
    current_task: asyncio.Task | None = None
    busy: bool = False
    awaiting_key: bool = False
    awaiting_custom: str | None = None  # имя параметра, для которого ждём своё значение
    picker: picker_mod.Picker | None = None
    profile_dirty: bool = False  # параметры меняли, но профиль не сохранён
    known_models: list[str] = field(default_factory=lambda: list(api.FALLBACK_MODELS))
    journal_warned: bool = False
    log: Fragments = field(default_factory=list)
    line_count: int = 0
    autoscroll: bool = True


def append_log(state: State, fragments: Fragments) -> None:
    state.log.extend(fragments)
    state.line_count += sum(text.count("\n") for _, text in fragments)
    if state.app is not None:
        state.app.invalidate()


def truncate_log(state: State, mark: int) -> None:
    removed = state.log[mark:]
    state.line_count -= sum(text.count("\n") for _, text in removed)
    del state.log[mark:]
    if state.app is not None:
        state.app.invalidate()


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
    if state.messages:
        state.messages.clear()
        append_log(state, ui.system_fragments("история диалога очищена — профиль сменился"))
    source = str(profile.source) if profile.source else "встроенный"
    append_log(state, ui.system_fragments(f"профиль: {profile.name} ({source})"))
    if profile.description:
        append_log(state, ui.hint_fragments(profile.description))
    if not profile.keep_history:
        append_log(state, ui.system_fragments("в этом профиле каждый запрос уходит без истории"))


# ─────────────────────────────── генерация ответа ───────────────────────────────


async def _spin(state: State) -> None:
    mark = len(state.log)
    i = 0
    try:
        while True:
            frame = ui.SPINNER_FRAMES[i % len(ui.SPINNER_FRAMES)]
            truncate_log(state, mark)
            append_log(state, [("class:dim", f"{frame} думаю…")])
            i += 1
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        truncate_log(state, mark)
        raise


def record(state: State, entry: dict[str, Any]) -> None:
    error = journal.append(entry)
    if error and not state.journal_warned:
        state.journal_warned = True
        append_log(state, ui.error_fragments(error))


async def generate_response(state: State, request_messages: list[dict], user_text: str) -> None:
    assert state.client is not None
    spinner_task = asyncio.create_task(_spin(state))

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
        async for event in state.client.stream_chat(state.model, request_messages, state.profile.params):
            await clear_spinner()
            if event.kind == "meta":
                finish_reason = event.finish_reason
                usage = event.usage
                continue
            if event.kind == "reasoning":
                if not reasoning_started:
                    append_log(state, ui.reasoning_label_fragments())
                    reasoning_started = True
                reasoning_text += event.text
                append_log(state, [("class:dim", event.text)])
            else:
                if not answer_started:
                    if reasoning_started:
                        append_log(state, [("", "\n")])
                    append_log(state, ui.answer_label_fragments())
                    answer_started = True
                append_log(state, [("", event.text)])
                answer_text += event.text
        status = "ok"
    except asyncio.CancelledError:
        await clear_spinner()
        status = "cancelled"
        raise
    except Exception as exc:  # сеть, лимиты, ошибки API — не роняем harness
        await clear_spinner()
        error_text = str(exc)
        append_log(state, ui.error_fragments(f"ошибка запроса к DeepSeek: {exc}"))
    finally:
        await clear_spinner()
        elapsed = time.monotonic() - started
        if reasoning_started or answer_started:
            append_log(state, [("", "\n")])
        if status == "ok":
            append_log(state, ui.meta_fragments(finish_reason, usage, elapsed, state.profile.name))
            if finish_reason == "length":
                append_log(state, ui.hint_fragments("ответ упёрся в max_tokens — увеличьте лимит: /set max_tokens"))
        if status == "ok" and answer_text and state.profile.keep_history:
            state.messages.append({"role": "assistant", "content": answer_text})
        elif state.profile.keep_history and state.messages and state.messages[-1]["role"] == "user":
            # ответ не получен (ошибка/отмена) — не оставляем в истории вопрос без ответа
            state.messages.pop()
        record(
            state,
            {
                "status": status,
                "model": state.model,
                "profile": state.profile.snapshot(),
                "query": user_text,
                "messages": request_messages,
                "response": answer_text or None,
                "reasoning": reasoning_text or None,
                "finish_reason": finish_reason,
                "usage": usage or None,
                "elapsed_ms": int(elapsed * 1000),
                "error": error_text,
            },
        )


async def worker(state: State) -> None:
    while True:
        content = await state.queue.get()
        if state.profile.keep_history:
            state.messages.append({"role": "user", "content": content})
        request_messages = build_request_messages(state, content)
        state.busy = True
        task = asyncio.create_task(generate_response(state, request_messages, content))
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


async def cmd_model(state: State, arg: str) -> None:
    if arg:
        state.model = arg
        state.config.model = arg
        save_config(state.config)
        append_log(state, ui.system_fragments(f"модель установлена: {arg}"))
        return
    models = state.known_models
    if state.client:
        try:
            models = await state.client.list_models()
            state.known_models = models
        except Exception as exc:
            append_log(state, ui.error_fragments(f"не удалось получить список моделей: {exc}"))
            append_log(state, ui.system_fragments("показан статический список"))
    append_log(state, ui.system_fragments("доступные модели:"))
    lines: Fragments = []
    for m in models:
        mark = "●" if m == state.model else "○"
        lines.append(("", f"  {mark} {m}\n"))
    append_log(state, lines)
    append_log(state, ui.hint_fragments("выбрать: /model <имя>"))


def cmd_profile(state: State, arg: str) -> None:
    if not arg:
        append_log(state, ui.profile_list_fragments(profiles.available(), state.profile.name))
        if state.profile_dirty:
            append_log(state, ui.hint_fragments("параметры изменены и не сохранены — /profile save <имя>"))
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
    append_log(state, ui.params_fragments(state.profile.name, state.profile.params, state.profile.system))
    if state.profile_dirty:
        append_log(state, ui.hint_fragments("изменения не сохранены — /profile save <имя>"))


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
    elif cmd == "/clear":
        state.messages.clear()
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

    append_log(state, ui.user_fragments(text))
    was_busy = state.busy
    state.queue.put_nowait(text)
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()))


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
    output_control = FormattedTextControl(text=lambda: state.log, show_cursor=False)
    output_window = LogWindow(
        content=output_control,
        wrap_lines=True,
        always_hide_cursor=True,
        height=Dimension(weight=1),
        on_manual_scroll=lambda: setattr(state, "autoscroll", False),
    )
    output_control.get_cursor_position = lambda: (
        Point(x=0, y=state.line_count) if state.autoscroll else Point(x=0, y=output_window.vertical_scroll)
    )

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

    picker_active = Condition(lambda: state.picker is not None)
    picker_window = Window(
        content=FormattedTextControl(text=lambda: picker_mod.fragments(state.picker) if state.picker else []),
        style="class:panel",
        dont_extend_height=True,
        dont_extend_width=True,
    )

    root = FloatContainer(
        content=HSplit([output_window, sep(), input_area, sep()]),
        floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12, scroll_offset=1)),
            Float(left=2, bottom=3, content=ConditionalContainer(picker_window, filter=picker_active)),
        ],
    )
    layout = Layout(root, focused_element=input_area)

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
        state.autoscroll = True  # новое сообщение — вернуться к живому выводу
        asyncio.get_running_loop().create_task(handle_submit(text, state))

    @kb.add("c-c", filter=~picker_active)
    def _cancel(event) -> None:  # noqa: ANN001
        if state.busy and state.current_task is not None:
            state.current_task.cancel()

    @kb.add("c-d")
    def _quit(event) -> None:  # noqa: ANN001
        event.app.exit()

    @kb.add("pageup")
    def _scroll_up(event) -> None:  # noqa: ANN001
        state.autoscroll = False
        output_window.vertical_scroll = max(0, output_window.vertical_scroll - 10)

    @kb.add("pagedown")
    def _scroll_down(event) -> None:  # noqa: ANN001
        state.autoscroll = False
        output_window.vertical_scroll += 10

    @kb.add("c-end")
    def _resume_autoscroll(event) -> None:  # noqa: ANN001
        state.autoscroll = True

    app = Application(
        layout=layout,
        key_bindings=kb,
        style=ui.STYLE,
        full_screen=True,
        mouse_support=True,
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
    append_log(
        state,
        ui.banner_fragments(
            state.model, state.config.is_authorized, state.profile.name, state.profile.description
        ),
    )
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


async def _main(argv: list[str] | None = None) -> None:
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
