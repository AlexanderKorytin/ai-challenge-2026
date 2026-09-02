"""Группа агентов: один вопрос — несколько независимых ответов и сводка ведущего.

Агент здесь — не процесс, который надо поднимать: DeepSeek-API не хранит сессий, каждый
запрос самодостаточен. Поэтому агент это профиль (своя системная инструкция и свои
параметры) плюс свой экран, куда идёт его ответ. Запросы уходят одновременно, ответы
пишутся каждый в свою ленту, и только когда ответили все, ведущий получает их разом и
сводит воедино.

Ведущий — профиль со списком `agents`. Если у него нет собственной инструкции, берётся
запасная (см. `DEFAULT_LEAD_INSTRUCTION`): без неё сводка получилась бы пересказом.
"""

from __future__ import annotations

import asyncio
from uuid import uuid4

from . import profiles, ui
from . import screens as screens_mod
from .profiles import Profile

DEFAULT_LEAD_INSTRUCTION = (
    "Ты ведущий группы экспертов. Ниже задача и независимые ответы экспертов, "
    "которые не видели ответов друг друга. Сопоставь их, укажи, в чём они расходятся, "
    "и дай итоговый ответ на задачу с обоснованием."
)


def ensure_screen(state, name: str, profile: Profile) -> screens_mod.Screen:
    """Экран агента заводится один раз на состав группы: повторный вопрос продолжает ту же
    ленту, чтобы было видно, как агент отвечал раньше."""
    for screen in state.screens:
        if screen.key == name:
            screen.profile = profile
            return screen
    screen = screens_mod.Screen(key=name, title=name, profile=profile)
    state.screens.append(screen)
    return screen


def build_summary_request(question: str, answers: list[tuple[str, str]]) -> str:
    parts = [f"Задача:\n{question}", ""]
    for name, text in answers:
        parts.append(f"Ответ эксперта «{name}»:\n{text.strip()}")
        parts.append("")
    return "\n".join(parts).strip()


async def run(state, question: str, lead: Profile) -> None:
    """Поднять группу ведущего профиля на этом вопросе и свести ответы."""
    from . import cli  # ленивый импорт: cli зовёт team из worker, а team — генерацию из cli

    run_id = uuid4().hex[:8]
    loaded: list[tuple[str, Profile, screens_mod.Screen]] = []
    for name in lead.agents:
        profile, warnings = profiles.load(name)
        for warning in warnings:
            cli.append_log(state, ui.error_fragments(f"агент «{name}»: {warning}"))
        if profile.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            # профиля с таким именем нет — молча подставленный default сделал бы агента
            # безликим, а результат группы необъяснимым
            cli.append_log(state, ui.error_fragments(f"агент «{name}» пропущен: профиль не найден"))
            continue
        loaded.append((name, profile, ensure_screen(state, name, profile)))

    if not loaded:
        cli.append_log(state, ui.error_fragments("группа не поднята: ни один профиль агента не найден"))
        return

    cli.append_log(state, ui.team_start_fragments([name for name, _, _ in loaded]))

    tasks = []
    for name, profile, screen in loaded:
        screen.status = screens_mod.BUSY
        cli.append_log(state, ui.agent_task_fragments(name, profile.name, profile.system, question), screen)
        messages = screens_mod.build_messages(profile, screen, question)
        tasks.append(
            cli.generate_response(
                state,
                messages,
                question,
                screen=screen,
                profile=profile,
                agent=name,
                run_id=run_id,
            )
        )
    turns = await asyncio.gather(*tasks)

    answers = [(name, turn.text) for (name, _, _), turn in zip(loaded, turns, strict=True) if turn.ok and turn.text]
    failed = [name for (name, _, _), turn in zip(loaded, turns, strict=True) if not (turn.ok and turn.text)]
    for name in failed:
        cli.append_log(state, ui.error_fragments(f"агент «{name}» ответа не дал — в сводку не попал"))
    if not answers:
        cli.append_log(state, ui.error_fragments("сводить нечего: ни один агент не ответил"))
        return

    cli.append_log(state, ui.team_summary_label_fragments(len(answers)))
    instruction = lead.system or DEFAULT_LEAD_INSTRUCTION
    if not lead.system:
        cli.append_log(state, ui.system_fragments("у ведущего нет своей инструкции — свожу по общему правилу"))
    summary_messages = [
        {"role": "system", "content": instruction},
        {"role": "user", "content": build_summary_request(question, answers)},
    ]
    if lead.keep_history:
        state.main.messages.append({"role": "user", "content": question})
    await cli.generate_response(
        state,
        summary_messages,
        question,
        screen=state.main,
        profile=lead,
        agent="lead",
        run_id=run_id,
    )
