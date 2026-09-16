"""Режимы управления контекстом, ветви разговора и смена профиля — всё, что заводит экраны.

Режим (`context_strategy`) — это отдельный разговор со своим файлом на диске и своим агентом,
показанный отдельной вкладкой. Главный экран всегда остаётся `standard`: режим, записанный в
профиле, открывает свой экран при запуске, а не превращает главный разговор в другой.

Ветвь — тот же приём на уровне одного режима: разговор раздваивается, и у каждой ветви свой
файл. Отсюда и соседство со сменой профиля: профиль решает, какие экраны вообще существуют,
поэтому его смена сносит старое дерево экранов и поднимает разговор нового профиля.
"""

from __future__ import annotations

from pathlib import Path

from . import context_strategy, memory, profiles, ui
from . import methods as methods_mod
from . import screens as screens_mod
from .agent import Agent
from .output import append_log
from .config import save as save_config
from .conversation import restore_conversation
from .panes import active_profile, apply_prefill, drop_agent_screens, switch_screen
from .profiles import Profile
from .state import State, profile_for_strategy, главный_агент

# Человеческие названия режимов — ими подписаны вкладки, панель выбора и подсказки команд.
STRATEGY_TITLES = {
    context_strategy.CONTEXT_STANDARD: "Standard",
    context_strategy.CONTEXT_SLIDING: "Sliding Window",
    context_strategy.CONTEXT_FACTS: "Sticky Facts",
    context_strategy.CONTEXT_BRANCHING: "Branching",
}


def _linear_strategy_agent(
    profile: Profile,
    pane_key: str,
) -> tuple[Agent, memory.SessionStore, memory.Restored | None, list[str]]:
    """Завести линейного собеседника и поднять полную запись его стратегии."""

    path = memory.latest_session(Path.cwd(), profile.name, profile.context_strategy)
    store = memory.SessionStore(
        path or memory.new_session(Path.cwd(), profile.name, profile.context_strategy)
    )
    facts_store = (
        memory.FactsStore(store.path)
        if profile.context_strategy == context_strategy.CONTEXT_FACTS
        else None
    )
    agent = Agent(
        pane_key,
        profile,
        store=store,
        facts_store=facts_store,
    )
    if path is None:
        return agent, store, None, [agent.store_error] if agent.store_error else []

    restored = memory.read_session(
        path,
        window=0,
        system_fp=memory.fingerprint(profile.system),
    )
    agent.restore(restored.pairs, restored.приложения)
    warnings = list(restored.warnings)
    if restored.fingerprint_changed:
        warnings.append(
            "инструкция профиля изменилась с прошлого запуска — "
            "прежние ответы получены под другой"
        )
    if agent.store_error:
        warnings.append(agent.store_error)
    return agent, store, restored, warnings


def branch_prefill(
    state: State,
    profile: Profile,
    pane: screens_mod.Pane,
    branch: str,
) -> None:
    """Назначить панели только очередь её ветви, не задевая общий буфер ввода."""

    pane.prefill_initialized = True
    pane.prefill_queue = list(profile.branch_prefills.get(branch, []))
    if not pane.prefill_queue:
        return
    count = len(pane.prefill_queue)
    pane.draft = pane.prefill_queue.pop(0)
    message = (
        "заготовка ветви подставлена в строку ввода — Enter отправит её"
        if count == 1
        else f"вопросы ветви ({count}) пойдут по очереди — Enter отправляет и подставляет следующий"
    )
    append_log(state, ui.system_fragments(message), pane)


def create_strategy_screen(
    state: State,
    source: Profile,
    strategy: str,
    *,
    key: str,
    title: str,
) -> screens_mod.Screen:
    """Создать сохраняемый экран стратегии и восстановить только его собственный ключ."""

    profile = profile_for_strategy(source, strategy)
    if strategy != context_strategy.CONTEXT_BRANCHING:
        agent, store, restored, warnings = _linear_strategy_agent(profile, key)
        pane = screens_mod.Pane(
            key=key,
            title=title,
            profile=profile,
            agent=agent,
        )
        screen = screens_mod.Screen(
            key=key,
            title=title,
            panes=[pane],
            profile=profile,
            strategy_identity=(source.name, strategy),
            interactive=True,
            session_store=store,
        )
        for warning in warnings:
            append_log(state, ui.error_fragments(warning), pane)
        if restored is not None and restored.saved_pairs:
            append_log(
                state,
                ui.restored_fragments(
                    len(restored.pairs),
                    restored.saved_pairs,
                    restored.last_ts,
                ),
                pane,
            )
        apply_prefill(state, profile, pane)
        return screen

    path = memory.latest_session(Path.cwd(), profile.name, strategy)
    store = memory.BranchStore(
        path or memory.new_session(Path.cwd(), profile.name, strategy)
    )
    graph = store.load()
    panes: list[screens_mod.Pane] = []
    if graph.checkpoint_id is None:
        root = Agent(key, profile, branch_store=store)
        panes.append(
            screens_mod.Pane(
                key=key,
                title=title,
                profile=profile,
                agent=root,
            )
        )
    else:
        for branch in graph.branches:
            agent = Agent(
                f"{key}:{branch}",
                profile,
                branch_store=store,
                branch=branch,
            )
            pane = screens_mod.Pane(
                key=branch,
                title=branch,
                profile=profile,
                agent=agent,
            )
            branch_prefill(state, profile, pane, branch)
            panes.append(pane)
    screen = screens_mod.Screen(
        key=key,
        title=title,
        panes=panes,
        profile=profile,
        strategy_identity=(source.name, strategy),
        interactive=True,
        branch_store=store,
    )
    for warning in graph.warnings:
        append_log(state, ui.error_fragments(warning), screen.first)
    if graph.turns:
        last_ts = max(turn.ts for turn in graph.turns.values())
        for pane in screen.panes:
            path_pairs = pane.agent.history() if pane.agent is not None else []
            if path_pairs:
                append_log(
                    state,
                    ui.restored_fragments(
                        len(path_pairs) // 2,
                        len(graph.turns),
                        last_ts,
                    ),
                    pane,
                )
    if graph.checkpoint_id is None:
        apply_prefill(state, profile, screen.first)
    return screen


def ensure_strategy_screen(
    state: State,
    strategy: str,
    *,
    source: Profile | None = None,
) -> int:
    """Вернуть прежний экран режима исходного профиля либо создать новый."""

    origin = source or active_profile(state)
    if (
        strategy == context_strategy.CONTEXT_STANDARD
        and origin.name == state.profile.name
    ):
        return 0
    identity = (origin.name, strategy)
    for index, screen in enumerate(state.screens):
        if screen.strategy_identity == identity:
            return index
    key = screens_mod.strategy_screen_key(origin.name, strategy)
    screen = create_strategy_screen(
        state,
        origin,
        strategy,
        key=key,
        title=STRATEGY_TITLES[strategy],
    )
    state.screens.append(screen)
    return len(state.screens) - 1


def use_strategy(
    state: State,
    strategy: str,
    *,
    source: Profile | None = None,
) -> None:
    """Открыть сохранённый экран режима; разговоры соседей остаются на месте."""

    switch_screen(
        state,
        ensure_strategy_screen(state, strategy, source=source),
    )


def open_profile_surfaces(state: State, initial_strategy: str) -> None:
    """Развернуть профиль до первого вопроса, не начиная ни одного обмена."""

    profile = state.profile
    if profile.screens:
        open_work_screens(state, profile)
        return
    if profile.methods:
        open_method_screens(state, profile)
    apply_prefill(state, profile, state.main.first)
    if initial_strategy != context_strategy.CONTEXT_STANDARD:
        use_strategy(state, initial_strategy)


async def switch_profile(state: State, name: str) -> None:
    """Дождаться старого дерева разговоров и целиком заменить профиль."""

    if state.switching_profile:
        append_log(state, ui.error_fragments("смена профиля уже выполняется"))
        return
    state.switching_profile = True
    try:
        loaded, warnings = profiles.load(name)
        initial_strategy = loaded.context_strategy
        profile = profile_for_strategy(loaded, context_strategy.CONTEXT_STANDARD)

        # Общий исполнитель берёт `state.main_agent` в момент начала единицы. Пока в его
        # очереди остаётся старый вопрос, заменять ссылку нельзя: иначе тот вопрос получил
        # бы уже новый собеседник. Новые единицы на время ожидания отклоняет handle_submit.
        await state.queue.join()
        if len(state.screens) > 1:
            await drop_agent_screens(state)
            append_log(state, ui.system_fragments("экраны прежней группы закрыты"))

        # История, набранная под прежней инструкцией, исказила бы следующий ответ. К этому
        # месту старые очереди завершены, поэтому поздний ответ уже не сможет попасть в
        # новый профиль, а расход всех удалённых экранов снят окончательным.
        had_history = bool(state.main_agent.history())
        state.main.first.agent.forget()
        state.retire([state.main.first.agent])

        state.profile = profile
        state.initial_strategy = initial_strategy
        state.main.first.agent = главный_агент(state, profile)
        state.main.first.prefill_queue.clear()
        state.main.first.prefill_initialized = False
        state.profile_dirty = False
        state.config.profile = profile.name
        save_config(state.config)
        for warning in warnings:
            append_log(state, ui.error_fragments(warning))
        if had_history:
            append_log(
                state,
                ui.system_fragments("история диалога очищена — профиль сменился"),
            )
        source = str(profile.source) if profile.source else "встроенный"
        append_log(
            state,
            ui.system_fragments(f"профиль: {profile.name} ({source})"),
        )
        # Разговор принадлежит паре «каталог, профиль», значит смена профиля — это переход
        # в другой разговор. Прежняя сессия остаётся на диске и поднимется при возврате.
        restore_conversation(state)
        if not profile.keep_history:
            append_log(
                state,
                ui.system_fragments(
                    "в этом профиле каждый запрос уходит без истории"
                ),
            )
        if profile.agents:
            append_log(
                state,
                ui.team_list_fragments(profile.name, profile.agents),
            )
        open_profile_surfaces(state, initial_strategy)
    finally:
        state.switching_profile = False


def open_method_screens(state: State, profile: Profile) -> None:
    """Профиль-набор разворачивает вкладки способов заранее, до первого вопроса: так видно,
    что именно будет сравниваться, ещё до того, как что-то отправлено."""
    loaded = methods_mod.load_methods(state, profile)
    if not loaded:
        append_log(state, ui.error_fragments("набор способов пуст — вкладки не открыты"))
        return
    methods_mod.ensure_screens(state, loaded)
    append_log(state, ui.methods_fragments([item.name for item in loaded]))


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
        # Любой рабочий экран получает тот же сохраняемый договор стратегии, что экран,
        # открытый `/strategy use`: отдельного несохраняемого пути для `screens` нет.
        screen = create_strategy_screen(
            state,
            step,
            step.context_strategy,
            key=name,
            title=step.title or name,
        )
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
