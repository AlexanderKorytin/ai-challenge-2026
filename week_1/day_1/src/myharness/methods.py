"""Набор способов: один вопрос — несколько разных подходов к нему, каждый на своей вкладке.

Профиль со списком `methods` перечисляет другие профили; каждый из них становится вкладкой и
получает тот же самый вопрос. Способ определяется по самому профилю, а не по названию:

* обычный профиль — один запрос, одна лента;
* профиль со `screens` — цепочка: ответ первого шага автоматически уходит во второй, и
  вкладка делится на панели по шагам (слева, скажем, составленный промпт, справа решение
  по нему);
* профиль с `agents` — группа: панели по экспертам, сводка ведущего на соседней вкладке.

Запросы всех способов уходят одновременно: ждать четыре очереди подряд бессмысленно, а
сравнивать ответы всё равно можно только когда они собраны.
"""

from __future__ import annotations

import asyncio

from . import profiles, team, ui
from . import screens as screens_mod
from .profiles import Profile


def load_methods(state, holder: Profile) -> list[Profile]:
    from . import cli

    loaded: list[Profile] = []
    for name in holder.methods:
        profile, warnings = profiles.load(name)
        for warning in warnings:
            cli.append_log(state, ui.error_fragments(f"способ «{name}»: {warning}"))
        if profile.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            cli.append_log(state, ui.error_fragments(f"способ «{name}» пропущен: профиль не найден"))
            continue
        profile.name = name
        loaded.append(profile)
    return loaded


def chain_panes(profile: Profile) -> list[screens_mod.Pane]:
    """Панели цепочки — по шагу на панель, в порядке их выполнения."""
    panes: list[screens_mod.Pane] = []
    for name in profile.screens:
        step, _ = profiles.load(name)
        step.name = name
        panes.append(screens_mod.Pane(key=name, title=step.description or name, profile=step))
    return panes


def ensure_screens(state, methods: list[Profile]) -> None:
    """Вкладки под способы. Заводятся один раз: повторный вопрос продолжает те же ленты."""
    for profile in methods:
        if profile.agents:
            team.ensure_screens(state, profile, team.load_agents(state, profile))
            continue
        if any(screen.key == profile.name for screen in state.screens):
            continue
        panes = chain_panes(profile) if profile.screens else []
        state.screens.append(
            screens_mod.Screen(key=profile.name, title=profile.name, profile=profile, panes=panes)
        )


async def run_chain(state, screen: screens_mod.Screen, profile: Profile, question: str) -> None:
    """Цепочка шагов: то, что ответил предыдущий шаг, становится началом запроса следующего.

    Человеку тут копировать нечего — промпт переносится сам, и обе половины видны рядом.
    """
    from . import cli

    carried = ""
    for index, pane in enumerate(screen.panes):
        step = pane.profile
        if step is None:
            continue
        content = question if index == 0 else f"{carried.strip()}\n\n{question}"
        pane.status = screens_mod.BUSY
        cli.append_log(state, ui.agent_task_fragments(pane.key, step.name, step.system, content), pane)
        turn = await cli.generate_response(
            state,
            screens_mod.build_messages(step, pane, content),
            content,
            pane=pane,
            profile=step,
        )
        if not turn.ok:
            cli.append_log(state, ui.error_fragments("шаг не дал ответа — цепочка прервана"), pane)
            return
        carried = turn.text


async def run_single(state, screen: screens_mod.Screen, profile: Profile, question: str) -> None:
    from . import cli

    pane = screen.first
    pane.status = screens_mod.BUSY
    cli.append_log(state, ui.user_fragments(question), pane)
    await cli.generate_response(
        state,
        screens_mod.build_messages(profile, pane, question),
        question,
        pane=pane,
        profile=profile,
    )


async def run_all(state, question: str, holder: Profile) -> None:
    """Задать вопрос всем способам набора разом."""
    from . import cli

    methods = load_methods(state, holder)
    if not methods:
        cli.append_log(state, ui.error_fragments("ни один способ не найден — набор пуст"))
        return
    ensure_screens(state, methods)

    tasks = []
    for profile in methods:
        screen = next((item for item in state.screens if item.key == profile.name), None)
        if screen is None:
            continue
        if profile.agents:
            tasks.append(team.run(state, question, profile, announce=False))
        elif profile.screens:
            tasks.append(run_chain(state, screen, profile, question))
        else:
            tasks.append(run_single(state, screen, profile, question))
    await asyncio.gather(*tasks)
