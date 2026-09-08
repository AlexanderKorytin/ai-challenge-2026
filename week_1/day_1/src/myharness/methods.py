"""Набор способов: один вопрос — несколько разных подходов к нему, каждый на своей вкладке.

Профиль со списком `methods` перечисляет другие профили; каждый из них становится вкладкой и
получает тот же самый вопрос. Способ определяется по самому профилю, а не по названию:

* обычный профиль — один запрос, одна лента;
* профиль со `screens` — цепочка: ответ первого шага автоматически уходит во второй.
  Как разложить шаги, говорит поле `layout` профиля цепочки: `panes` (умолчание) делит
  вкладку способа на панели по шагам — слева, скажем, составленный промпт, справа решение
  по нему; `tabs` отводит каждому шагу свою вкладку, во всю ширину окна;
* профиль с `agents` — группа: панели по экспертам, сводка ведущего на соседней вкладке.

Запросы всех способов уходят одновременно: ждать четыре очереди подряд бессмысленно, а
сравнивать ответы всё равно можно только когда они собраны.
"""

from __future__ import annotations

import asyncio

from . import output, profiles, team, ui
from . import screens as screens_mod
from .profiles import Profile


def load_methods(state, holder: Profile) -> list[Profile]:
    loaded: list[Profile] = []
    for name in holder.methods:
        profile, warnings = profiles.load(name)
        for warning in warnings:
            output.append_log(state, ui.error_fragments(f"способ «{name}»: {warning}"))
        if profile.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            output.append_log(state, ui.error_fragments(f"способ «{name}» пропущен: профиль не найден"))
            continue
        profile.name = name
        loaded.append(profile)
    return loaded


def load_step(name: str) -> Profile:
    """Профиль шага цепочки. Имя выставляем по запрошенному: под ним шаг записан в цепочке,
    под ним его и ищут — поле `name` внутри файла может с ним не совпадать."""
    step, _ = profiles.load(name)
    step.name = name
    return step


def chain_panes(profile: Profile) -> list[screens_mod.Pane]:
    """Панели цепочки — по шагу на панель, в порядке их выполнения."""
    panes: list[screens_mod.Pane] = []
    for name in profile.screens:
        step = load_step(name)
        panes.append(screens_mod.Pane(key=name, title=step.title or name, profile=step))
    return panes


def step_screen_key(chain: Profile, step_name: str) -> str:
    """Ключ вкладки шага при раскладке `tabs`.

    Имя одного шага говорит слишком мало: один и тот же профиль шага может стоять в двух
    цепочках набора, и вкладки столкнулись бы ключами — вторая цепочка дописывала бы ответы
    в ленты первой. Имя цепочки впереди делает ключ своим у каждой, как `:summary` у
    сводки ведущего."""
    return f"{chain.name}:{step_name}"


def ensure_chain_tabs(state, profile: Profile) -> None:
    """Вкладка на каждый шаг цепочки. Экраны только на просмотр: вопрос задаётся в главном,
    оттуда он и раздаётся всем способам набора."""
    for name in profile.screens:
        key = step_screen_key(profile, name)
        if any(screen.key == key for screen in state.screens):
            continue
        step = load_step(name)
        # Панель заводит `Screen.__post_init__` по профилю шага, а собеседника — сама панель:
        # у каждого шага своя инструкция, значит и своя память.
        state.screens.append(screens_mod.Screen(key=key, title=step.title or name, profile=step))


def chain_step_panes(state, profile: Profile) -> list[screens_mod.Pane]:
    """Панели шагов цепочки в порядке выполнения — независимо от того, как они разложены.

    Раскладка кончается здесь: дальше цепочка видит просто список панелей и не знает, лежат
    они на одной вкладке или на разных."""
    if profile.layout == profiles.LAYOUT_TABS:
        panes: list[screens_mod.Pane] = []
        for name in profile.screens:
            screen = find_screen(state, step_screen_key(profile, name))
            if screen is not None:
                panes.append(screen.first)
        return panes
    screen = find_screen(state, profile.name)
    return list(screen.panes) if screen is not None else []


def find_screen(state, key: str) -> screens_mod.Screen | None:
    return next((screen for screen in state.screens if screen.key == key), None)


def ensure_screens(state, methods: list[Profile]) -> None:
    """Вкладки под способы. Заводятся один раз: повторный вопрос продолжает те же ленты."""
    for profile in methods:
        if profile.agents:
            team.ensure_screens(state, profile, team.load_agents(state, profile))
            continue
        if profile.screens and profile.layout == profiles.LAYOUT_TABS:
            ensure_chain_tabs(state, profile)
            continue
        if any(screen.key == profile.name for screen in state.screens):
            continue
        panes = chain_panes(profile) if profile.screens else []
        # Собеседников не расставляем: у обычного способа панель делает `Screen.__post_init__`
        # по профилю способа, у цепочки — `chain_panes` по профилю шага, и в обоих случаях
        # агента заводит сама панель.
        screen = screens_mod.Screen(
            key=profile.name, title=profile.title or profile.name, profile=profile, panes=panes
        )
        state.screens.append(screen)


async def run_chain(state, name: str, panes: list[screens_mod.Pane], question: str) -> output.Outcome | None:
    """Цепочка шагов: то, что ответил предыдущий шаг, становится началом запроса следующего.

    Человеку тут копировать нечего — промпт переносится сам.

    На вход идёт список панелей в порядке шагов, а не экран: собраны они с одной вкладки
    или с разных, цепочку не касается. Знание о раскладке живёт в `chain_step_panes`, и
    передача работы между шагами от смены раскладки не меняется ни на строчку.

    Профиля способа здесь нет намеренно: инструкцию и собеседника каждого шага несёт его
    собственная панель (`pane.profile`, `pane.agent`). Второй источник той же правды в
    аргументах только вводил бы в заблуждение — на выполнение он не влиял никак. Имя цепочки
    приходит отдельной строкой, потому что панели его не несут: при раскладке `panes` ключ
    панели — имя шага, при `tabs` — пара «цепочка:шаг», и собрать из них подпись итога нельзя.

    Ответ последнего шага возвращается вызывающему: это и есть итог цепочки. Оборвалась
    цепочка на полпути — итога нет, и возвращается пустое, а не ответ середины."""
    carried = ""
    last = ""
    for index, pane in enumerate(panes):
        step = pane.profile
        if step is None:
            continue
        content = question if index == 0 else f"{carried.strip()}\n\n{question}"
        pane.status = screens_mod.BUSY
        output.append_log(state, ui.agent_task_fragments(pane.key, step.name, step.system, content), pane)
        turn = await output.run_turn(state, pane.agent, content, pane=pane)
        if not turn.ok:
            output.append_log(state, ui.error_fragments("шаг не дал ответа — цепочка прервана"), pane)
            return None
        carried = turn.text
        last = step.name
    if not carried.strip():
        return None
    return output.Outcome(kind="итог цепочки", source=f"{name} → {last}", text=carried)


async def run_single(state, screen: screens_mod.Screen, question: str) -> output.Outcome | None:
    """Обычный способ: один запрос, одна лента. Профиль, как и у цепочки, берётся у панели."""
    pane = screen.first
    pane.status = screens_mod.BUSY
    output.append_log(state, ui.user_fragments(question), pane)
    turn = await output.run_turn(state, pane.agent, question, pane=pane)
    if not (turn.ok and turn.text.strip()):
        return None
    return output.Outcome(kind="ответ способа", source=screen.key, text=turn.text)


async def run_all(state, question: str, holder: Profile) -> list[output.Outcome]:
    """Задать вопрос всем способам набора разом и собрать их итоги.

    Итоги возвращаются в порядке списка `methods`, а не в порядке, в каком способы управились:
    сравнивают их по столбцам набора, и список, переставляющий способы от прогона к прогону,
    сравнивать нечем. `asyncio.gather` порядок задач сохраняет, поэтому достаточно не терять
    соответствия между задачей и способом."""
    methods = load_methods(state, holder)
    if not methods:
        output.append_log(state, ui.error_fragments("ни один способ не найден — набор пуст"))
        return []
    ensure_screens(state, methods)

    tasks = []
    for profile in methods:
        if profile.agents:
            tasks.append(team.run(state, question, profile, announce=False))
            continue
        if profile.screens:
            panes = chain_step_panes(state, profile)
            if panes:
                tasks.append(run_chain(state, profile.name, panes, question))
            continue
        screen = find_screen(state, profile.name)
        if screen is not None:
            tasks.append(run_single(state, screen, question))
    results = await asyncio.gather(*tasks)
    return [item for item in results if item is not None]
