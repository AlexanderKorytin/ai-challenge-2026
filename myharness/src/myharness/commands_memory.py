"""Команды про память разговора: факты, ветви, `/remember`, `/memory`, `/forget`.

Память у инструмента двух родов, и команды здесь обоих. Разговор — то, что сказано в этой
папке с этим профилем; глобальные факты о человеке — то, что верно в любой папке. Ветвь
(`/branch`) относится к первому роду: она раздваивает разговор, оставляя обоим общее начало.

`/remember` и `/forget` вдобавок сбрасывают запомненную постоянную часть ПОЛОСКИ ЗАНЯТОСТИ.
На правильность запроса это не влияет: факты подставляются вызываемым, которое читает диск на
каждой сборке, и устаревшими они не бывают. Дело именно в полоске: её отпечаток глобальных
фактов не видит, и без сброса она показывала бы прежний вес. `/facts` этого вызова не делает и
делать не должен — он правит разговор, а не глобальные факты.
"""

from __future__ import annotations

from typing import Any

from . import context_strategy, memory, ui
from . import picker as picker_mod
from . import screens as screens_mod
from .config import save as save_config
from .conversation import забыть_счёт_занятости
from .output import append_log, refresh
from .panes import active_input_pane, restore_active_draft, save_active_draft, active_profile, switch_pane
from .state import State
from .strategies import branch_prefill


def cmd_facts(state: State, arg: str) -> None:
    """Показать или вручную изменить факты активного разговора Sticky Facts."""

    pane = active_input_pane(state)
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

    save_active_draft(state)
    branch_panes: list[screens_mod.Pane] = []
    for name in (left, right):
        pane = screens_mod.Pane(
            key=name,
            title=name,
            profile=profile,
            agent=branches[name],
        )
        branch_prefill(state, profile, pane, name)
        branch_panes.append(pane)
    # Корень покидает дерево экранов ровно здесь. Его расход не восстановится в новых
    # агентах и потому переносится в общий сеанс один раз до потери ссылки.
    state.retire([root_agent])
    screen.panes = branch_panes
    screen.active_pane = 0
    screen.zoomed = False
    restore_active_draft(state)
    checkpoint = screen.branch_store.load().checkpoint_id
    append_log(
        state,
        ui.system_fragments(
            f"разговор разделён в точке {checkpoint}: ветви {left}, {right}; активна {left}"
        ),
    )
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
