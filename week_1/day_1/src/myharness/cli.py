"""Полноэкранный REPL: лог-панель сверху (растёт, автопрокрутка), поле ввода снизу,
окаймлённое горизонтальными линиями. Очередь запросов, отмена по Ctrl+C, стрим ответа."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field

from prompt_toolkit.application import Application
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import HSplit, Window
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import TextArea

from . import api, ui
from .api import DeepSeekClient
from .config import Config, load as load_config, save as save_config

Fragments = list[tuple[str, str]]


@dataclass
class State:
    config: Config
    client: DeepSeekClient | None
    model: str
    app: Application | None = None
    messages: list[dict] = field(default_factory=list)
    queue: "asyncio.Queue[str]" = field(default_factory=asyncio.Queue)
    current_task: asyncio.Task | None = None
    busy: bool = False
    awaiting_key: bool = False
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


async def generate_response(state: State) -> None:
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
    ok = False
    try:
        async for event in state.client.stream_chat(state.model, state.messages):
            await clear_spinner()
            if event.kind == "reasoning":
                if not reasoning_started:
                    append_log(state, ui.reasoning_label_fragments())
                    reasoning_started = True
                append_log(state, [("class:dim", event.text)])
            else:
                if not answer_started:
                    if reasoning_started:
                        append_log(state, [("", "\n")])
                    append_log(state, ui.answer_label_fragments())
                    answer_started = True
                append_log(state, [("", event.text)])
                answer_text += event.text
        ok = True
    except asyncio.CancelledError:
        await clear_spinner()
        raise
    except Exception as exc:  # сеть, лимиты, ошибки API — не роняем harness
        await clear_spinner()
        append_log(state, ui.error_fragments(f"ошибка запроса к DeepSeek: {exc}"))
    finally:
        await clear_spinner()
        if reasoning_started or answer_started:
            append_log(state, [("", "\n")])
        if ok and answer_text:
            state.messages.append({"role": "assistant", "content": answer_text})
        elif state.messages and state.messages[-1]["role"] == "user":
            # ответ не получен (ошибка/отмена) — не оставляем в истории вопрос без ответа
            state.messages.pop()


async def worker(state: State) -> None:
    while True:
        content = await state.queue.get()
        state.messages.append({"role": "user", "content": content})
        state.busy = True
        task = asyncio.create_task(generate_response(state))
        state.current_task = task
        try:
            await task
        except asyncio.CancelledError:
            append_log(state, ui.system_fragments("запрос отменён"))
        finally:
            state.current_task = None
            state.busy = False
            state.queue.task_done()


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
    models = api.FALLBACK_MODELS
    if state.client:
        try:
            models = await state.client.list_models()
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


def on_submit(buf, state: State) -> bool:  # noqa: ANN001 — тип Buffer, не импортируем ради аннотации
    state.autoscroll = True  # новое сообщение — вернуться к живому выводу
    asyncio.get_event_loop().create_task(handle_submit(buf.text, state))
    return False


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
    )
    input_area.buffer.accept_handler = lambda buf: on_submit(buf, state)

    def sep() -> Window:
        return Window(height=1, char="─", style="class:sep")

    root = HSplit([output_window, sep(), input_area, sep()])
    layout = Layout(root, focused_element=input_area)

    kb = KeyBindings()

    @kb.add("c-c")
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

    return Application(
        layout=layout,
        key_bindings=kb,
        style=ui.STYLE,
        full_screen=True,
        mouse_support=True,
        erase_when_done=True,
    )


async def repl(state: State) -> None:
    app = build_app(state)
    state.app = app
    append_log(state, ui.banner_fragments(state.model, state.config.is_authorized))
    worker_task = asyncio.create_task(worker(state))
    try:
        await app.run_async()
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        if state.client:
            await state.client.aclose()


async def _main() -> None:
    cfg = load_config()
    client = DeepSeekClient(cfg.api_key) if cfg.is_authorized else None
    state = State(config=cfg, client=client, model=cfg.model)
    await repl(state)


def main() -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(_main())
