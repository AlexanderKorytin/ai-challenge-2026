"""Что делать с тем, что человек ввёл: команда это или вопрос модели.

Одна развилка на весь инструмент. Строка, начинающаяся с «/», уходит в свою команду; всё
остальное становится единицей очереди и отправляется тому агенту, чья панель сейчас активна.
Сюда же сходятся две особые строки: ответ на вопрос о ключе и своё значение параметра —
инструмент их ждёт, и обычным вопросом они быть не могут.

Команды ввозятся по имени модуля, а не поимённо: в раздаче видно, к какой семье принадлежит
каждая, и семья читается вместе со строкой, которая её зовёт.
"""

from __future__ import annotations

from . import background, ui
from . import agents_panel, commands_context, commands_memory, commands_model, commands_params
from . import commands_project, commands_task
from . import screens as screens_mod
from .conversation import open_new_session
from .output import append_log
from . import state as state_mod
from .state import Request, State
from .workers import pane_worker


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
        commands_model.cmd_auth(state)
    elif cmd == "/model":
        await commands_model.cmd_model(state, arg)
    elif cmd == "/profile":
        await commands_model.cmd_profile(state, arg)
    elif cmd == "/params":
        commands_params.cmd_params(state)
    elif cmd == "/set":
        commands_params.cmd_set(state, arg)
    elif cmd == "/system":
        # На панели исполнителя показываем его инструкцию: именно её там свернули до строки.
        # На главном экране — ТОЧНЫЙ текст, уходящий в запрос: системную часть со всеми
        # слоями и эфемерный хвост. Это единственная мера человека против незаметного роста
        # памяти, и показывать вместо неё одну инструкцию профиля значило бы её отменить.
        source = state.screen.pane.profile
        if source is None:
            # Хвост берём у поставщика напрямую, а не через агента: агент жалобу глотает —
            # ему нельзя ронять взвешивание, — а `/system` заведена ровно затем, чтобы
            # человек видел правду о своём запросе, включая причину, по которой слоя нет.
            try:
                хвост = state_mod.work_block(state)
            except Exception as exc:  # noqa: BLE001 — поставщик рабочей памяти ходит на диск
                хвост = ""
                append_log(state, ui.error_fragments(f"рабочее состояние не прочитано: {exc}"))
            append_log(
                state,
                ui.request_text_fragments(state.main_agent.system_text(), хвост),
            )
        else:
            append_log(
                state,
                ui.system_prompt_fragments(source.name, source.system),
            )
    elif cmd == "/strategy":
        commands_model.cmd_strategy(state, arg)
    elif cmd == "/facts":
        commands_memory.cmd_facts(state, arg)
    elif cmd == "/branch":
        await commands_memory.cmd_branch(state, arg)
    elif cmd == "/team":
        agents_panel.cmd_team(state, arg)
    elif cmd == "/mouse":
        commands_params.toggle_mouse(state)
    elif cmd == "/clear":
        # Идущий заход сжимателя снимаем ПЕРВЫМ делом. Своего итога он после этого не
        # применит и без отмены — применение сверяется с поколением памяти, — но платить за
        # запрос, чей итог заведомо выброшен, незачем.
        background.отменить(state.сжиматель)
        # Выжимку и заготовку убирает сам агент (`forget`): оставь их — и человек, стёрший
        # разговор, продолжил бы говорить с моделью, которая помнит его пересказ.
        state.main_agent.forget()
        commands_context.сбросить_счёт_отказов(state)
        # Мало забыть разговор в памяти: не открой мы новую сессию, следующий запуск поднял
        # бы очищенное обратно с диска — издевательство, а не очистка.
        open_new_session(state)
        append_log(
            state,
            ui.system_fragments("главный разговор очищен — начат новый разговор"),
        )
    elif cmd == "/context":
        commands_context.cmd_context(state)
    elif cmd == "/compact":
        commands_context.cmd_compact(state)
    elif cmd == "/project":
        commands_project.cmd_project(state, arg)
    elif cmd == "/task":
        commands_task.cmd_task(state, arg)
    elif cmd == "/remember":
        commands_memory.cmd_remember(state, arg)
    elif cmd == "/memory":
        commands_memory.cmd_memory(state, arg)
    elif cmd == "/forget":
        commands_memory.cmd_forget(state, arg)
    elif cmd == "/tokens":
        commands_context.cmd_tokens(state)
    elif cmd == "/budget":
        commands_context.cmd_budget(state, arg)
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
        await commands_model.do_auth(raw_text, state)
        return

    if state.awaiting_custom:
        commands_params.apply_custom_value(state, raw_text)
        return

    text = raw_text.strip()
    if not text:
        return

    # Идёт интервью о проекте: строка человека — это ответ на заданный вопрос, а не вопрос
    # модели.
    #
    # Командой считаем только ИЗВЕСТНОЕ имя, а не всякую строку с косой чертой впереди. На
    # вопрос «где что лежит?» человек отвечает `/opt/challenge`, и съеденный как «неизвестная
    # команда» ответ ломал бы обряд на самом естественном месте: пара не записана, вопрос не
    # переспрошен, интервью молча ждёт.
    if not ui.известная_команда(text):
        from . import interview

        if interview.идёт(state):
            # Ответ принимаем только с ГЛАВНОГО экрана: обряд идёт там, и строка, набранная
            # на экране способов или стратегии, адресована их собеседнику. Съешь мы её —
            # соседний экран онемел бы, а человек увидел бы не ошибку, а ничего.
            адресат = destination_screen or (
                state.screen if state.screen.interactive else state.main
            )
            if адресат is state.main:
                await interview.принять_ответ(state, text)
                return
            append_log(
                state,
                ui.hint_fragments("идёт интервью о проекте — ответьте на главном экране"),
                destination_pane or адресат.pane,
            )
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
