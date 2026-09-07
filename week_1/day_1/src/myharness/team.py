"""Группа агентов: один вопрос — несколько независимых ответов и сводка ведущего.

Агент здесь — не процесс, который надо поднимать: DeepSeek-API не хранит сессий, каждый
запрос самодостаточен. Поэтому агент это профиль (своя системная инструкция и свои
параметры) плюс своя панель, куда идёт его ответ. Запросы уходят одновременно, ответы
пишутся каждый в свою ленту, и только когда ответили все, ведущий получает их разом.

Экранов у группы два: на первом ответы экспертов лежат рядом, панель к панели, — так их и
сравнивают; на втором сводка ведущего. Разделение не косметическое: сводка — уже чужая
интерпретация ответов, и мешать её с самими ответами значит терять исходный материал.

Ведущий — профиль со списком `agents`. Если у него нет собственной инструкции, берётся
запасная (см. `DEFAULT_LEAD_INSTRUCTION`): без неё сводка получилась бы пересказом.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from . import output, profiles, ui
from . import screens as screens_mod
from .agent import Agent
from .profiles import Profile

DEFAULT_LEAD_INSTRUCTION = (
    "Ты ведущий группы экспертов. Ниже задача и независимые ответы экспертов, "
    "которые не видели ответов друг друга. Сопоставь их, укажи, в чём они расходятся, "
    "и дай итоговый ответ на задачу с обоснованием."
)

SUMMARY_SUFFIX = ":summary"


def load_agents(state, lead: Profile) -> list[Profile]:
    """Профили агентов ведущего. Пропавший профиль пропускаем с явным сообщением: молча
    подставленный default сделал бы агента безликим, а результат группы необъяснимым."""
    loaded: list[Profile] = []
    for name in lead.agents:
        profile, warnings = profiles.load(name)
        for warning in warnings:
            output.append_log(state, ui.error_fragments(f"агент «{name}»: {warning}"))
        if profile.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            output.append_log(state, ui.error_fragments(f"агент «{name}» пропущен: профиль не найден"))
            continue
        profile.name = name
        loaded.append(profile)
    return loaded


def ensure_screens(state, lead: Profile, agents: list[Profile]) -> tuple[screens_mod.Screen, screens_mod.Screen]:
    """Экран с панелями экспертов и экран сводки. Заводятся один раз на состав группы:
    повторный вопрос продолжает те же ленты."""
    board = None
    summary = None
    for screen in state.screens:
        if screen.key == lead.name:
            board = screen
        elif screen.key == lead.name + SUMMARY_SUFFIX:
            summary = screen
    if board is None:
        board = screens_mod.Screen(
            key=lead.name,
            title=lead.title or lead.name,
            profile=lead,
            panes=[
                screens_mod.Pane(
                    key=expert.name,
                    title=expert.title or expert.name,
                    profile=expert,
                    agent=Agent(expert.name, expert),
                )
                for expert in agents
            ],
        )
        state.screens.append(board)
    if summary is None:
        summary = screens_mod.Screen(
            key=lead.name + SUMMARY_SUFFIX, title=f"{lead.title or lead.name} · сводка", profile=lead
        )
        # Панель сводки делает `Screen.__post_init__`, поэтому собеседника ведущего кладём
        # следом: сводит ответы экспертов он, значит и память сводки принадлежит ему.
        summary.first.agent = Agent(lead.name, lead)
        state.screens.append(summary)
    return board, summary


def build_summary_request(question: str, answers: list[tuple[str, str]]) -> str:
    parts = [f"Задача:\n{question}", ""]
    for name, text in answers:
        parts.append(f"Ответ эксперта «{name}»:\n{text.strip()}")
        parts.append("")
    return "\n".join(parts).strip()


async def run(state, question: str, lead: Profile, *, announce: bool = True) -> None:
    """Поднять группу ведущего профиля на этом вопросе и свести ответы."""
    from . import cli

    run_id = uuid4().hex[:8]
    agents = load_agents(state, lead)
    if not agents:
        output.append_log(state, ui.error_fragments("группа не поднята: ни один профиль агента не найден"))
        return

    board, summary_screen = ensure_screens(state, lead, agents)
    if announce:
        output.append_log(state, ui.team_start_fragments([expert.name for expert in agents]))

    tasks = []
    for expert in agents:  # здесь `expert` — ПРОФИЛЬ эксперта, а не собеседник `Agent`
        pane = board.pane_by_key(expert.name)
        if pane is None:  # состав группы изменился на ходу — панель заводим на месте
            pane = screens_mod.Pane(
                key=expert.name,
                title=expert.title or expert.name,
                profile=expert,
                agent=Agent(expert.name, expert),
            )
            board.panes.append(pane)
        pane.status = screens_mod.BUSY
        output.append_log(state, ui.agent_task_fragments(expert.name, expert.name, expert.system, question), pane)
        if expert.keep_history:  # историю панели ведёт вызывающий: сборка сообщений её только читает
            pane.messages.append({"role": "user", "content": question})
        tasks.append(
            cli.generate_response(
                state,
                screens_mod.build_messages(expert, pane, question),
                question,
                pane=pane,
                profile=expert,
                agent=expert.name,
                run_id=run_id,
            )
        )
    turns = await asyncio.gather(*tasks)

    answers = [(expert.name, turn.text) for expert, turn in zip(agents, turns, strict=True) if turn.ok and turn.text]
    failed = [expert.name for expert, turn in zip(agents, turns, strict=True) if not (turn.ok and turn.text)]
    for name in failed:
        output.append_log(state, ui.error_fragments(f"агент «{name}» ответа не дал — в сводку не попал"), summary_screen)
    if not answers:
        output.append_log(state, ui.error_fragments("сводить нечего: ни один агент не ответил"), summary_screen)
        return

    output.append_log(state, ui.team_summary_label_fragments(len(answers)), summary_screen)
    instruction = lead.system or DEFAULT_LEAD_INSTRUCTION
    if not lead.system:
        output.append_log(
            state, ui.system_fragments("у ведущего нет своей инструкции — свожу по общему правилу"), summary_screen
        )
    await cli.generate_response(
        state,
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": build_summary_request(question, answers)},
        ],
        question,
        pane=summary_screen.first,
        profile=lead,
        agent="lead",
        run_id=run_id,
    )
