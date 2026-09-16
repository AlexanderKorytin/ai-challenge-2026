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

Консилиум (`созвать`) — та же одновременная рассылка без сводки: ответы возвращаются модели,
которая созвала консилиум инструментом автомата задачи, и сводит их она сама. Поэтому у
консилиума один экран — доска с панелями участников, — а экрана сводки нет.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace
from uuid import uuid4

from . import output, profiles, ui
from . import screens as screens_mod
from .profiles import Profile

DEFAULT_LEAD_INSTRUCTION = (
    "Ты ведущий группы экспертов. Ниже задача и независимые ответы экспертов, "
    "которые не видели ответов друг друга. Сопоставь их, укажи, в чём они расходятся, "
    "и дай итоговый ответ на задачу с обоснованием."
)

SUMMARY_SUFFIX = ":summary"
COUNCIL_SUFFIX = ":council"


def summary_profile(lead: Profile) -> Profile:
    """Профиль, с которым ведущий сводит ответы: тот же самый, но разовый и с инструкцией.

    Собеседник собирает запрос сам, из своего профиля. Значит и запасное правило сведения
    обязано лежать в профиле, а не подставляться мимо него при ручной сборке сообщений: иначе
    у ведущего снова два разных входа — один для сводки, другой для всего остального.

    `keep_history=False` — здесь главное. Ведущему на вход приходит готовый текст: задача и
    ответы экспертов этого прогона. Прошлые заседания ему не нужны, а помнил бы он их — второй
    прогон группы тащил бы в САМЫЙ дорогой запрос инструмента весь первый: и все ответы
    экспертов, и прежнюю сводку. Цена росла бы с каждым прогоном без всякой пользы.

    Копия ВСЕГДА, в обоих случаях: `lead` — это ещё и профиль, выбранный пользователем
    командой `/profile`, и правка на месте утекла бы и в главный экран, и в файл профиля при
    сохранении. `replace` копирует неглубоко — `params` и `vars` остаются общими словарями с
    профилем пользователя; для двух правящихся здесь полей это безразлично, но добавлять сюда
    правку словаря нельзя, не сделав его копию."""
    return replace(lead, system=lead.system or DEFAULT_LEAD_INSTRUCTION, keep_history=False)


def load_agents(state, lead: Profile) -> list[Profile]:
    """Профили агентов ведущего. Пропавший профиль пропускаем с явным сообщением: молча
    подставленный default сделал бы агента безликим, а результат группы необъяснимым."""
    return _загрузить(state, lead.agents)


def _загрузить(state, имена: list[str], куда: screens_mod.Screen | None = None) -> list[Profile]:
    """Профили по именам; не найденный назван вслух (в `куда`, по умолчанию — в экран вывода
    команд) и пропущен. Общая часть группы и консилиума: у консилиума это и есть проверка
    имён карты стадий, которую разбор профиля намеренно отложил до созыва."""
    loaded: list[Profile] = []
    for name in имена:
        profile, warnings = profiles.load(name)
        for warning in warnings:
            output.append_log(state, ui.error_fragments(f"агент «{name}»: {warning}"), куда)
        if profile.name == profiles.DEFAULT_PROFILE_NAME and name != profiles.DEFAULT_PROFILE_NAME:
            output.append_log(state, ui.error_fragments(f"агент «{name}» пропущен: профиль не найден"), куда)
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
                screens_mod.Pane(key=expert.name, title=expert.title or expert.name, profile=expert)
                for expert in agents
            ],
        )
        state.screens.append(board)
    if summary is None:
        # Панель задаём сами, а не отдаём `Screen.__post_init__`: её профиль — не сам `lead`,
        # а `summary_profile(lead)`, с запасной инструкцией сведения. Собеседника по этому
        # профилю панель заводит уже без нас.
        summary = screens_mod.Screen(
            key=lead.name + SUMMARY_SUFFIX,
            title=f"{lead.title or lead.name} · сводка",
            profile=lead,
            panes=[screens_mod.Pane(key=lead.name + SUMMARY_SUFFIX, profile=summary_profile(lead))],
        )
        state.screens.append(summary)
    return board, summary


async def _опросить(
    state, вопрос: str, участники: list[Profile], доска: screens_mod.Screen, run_id: str
) -> tuple[list[tuple[str, str]], list[str]]:
    """Один вопрос всем участникам разом, каждому в его панель на доске.

    Общая часть группы (`run`) и консилиума (`созвать`): до сводки они не различаются ничем.
    Возвращает ответивших парами «имя, текст» и не ответивших парами «имя, причина» (причина
    пуста, если обмен просто не дал текста) — назвать их вслух обязан вызывающий: куда и
    какими словами, у группы и консилиума разное.

    `return_exceptions=True` — чтобы исключение одного участника не бросило остальных
    сиротами: без него `gather` отдаёт исключение сразу, а запросы соседей продолжают идти
    и тратить без хозяина. Внешняя отмена `gather` доходит до всех запросов и при этом
    флаге. Отменённый участник считается не ответившим только при сбое, не при отмене
    всего опроса: та пробрасывается."""
    tasks = []
    for expert in участники:  # здесь `expert` — ПРОФИЛЬ участника, а не собеседник `Agent`
        pane = доска.pane_by_key(expert.name)
        if pane is None:  # состав изменился на ходу — панель заводим на месте
            pane = screens_mod.Pane(key=expert.name, title=expert.title or expert.name, profile=expert)
            доска.panes.append(pane)
        pane.status = screens_mod.BUSY
        output.append_log(state, ui.agent_task_fragments(expert.name, expert.name, expert.system, вопрос), pane)
        tasks.append(output.run_turn(state, pane.agent, вопрос, pane=pane, agent_name=expert.name, run_id=run_id))
    turns = await asyncio.gather(*tasks, return_exceptions=True)

    answers: list[tuple[str, str]] = []
    failed: list[tuple[str, str]] = []
    for expert, turn in zip(участники, turns, strict=True):
        if isinstance(turn, BaseException):
            if not isinstance(turn, Exception):
                raise turn  # отмена и прочее системное не превращаются в «не ответил»
            failed.append((expert.name, f"{type(turn).__name__}: {turn}"))
        elif turn.ok and turn.text:
            answers.append((expert.name, turn.text))
        else:
            failed.append((expert.name, ""))
    return answers, failed


def _о_молчании(текст: str, причина: str) -> str:
    return f"{текст} ({причина})" if причина else текст


def доска_консилиума(state, владелец: Profile, участники: list[Profile]) -> screens_mod.Screen:
    """Экран с панелями участников консилиума профиля `владелец`; экрана сводки нет.

    Ключ — имя владельца с `COUNCIL_SUFFIX`, а не само имя: у профиля с картой стадий может
    быть и своя группа (`agents`), и её доска с ключом `lead.name` не должна смешаться с
    консилиумом. Доска одна на владельца: консилиум другой стадии дописывает свои панели на
    месте (`_опросить`), повторный созыв продолжает те же ленты."""
    ключ = владелец.name + COUNCIL_SUFFIX
    for screen in state.screens:
        if screen.key == ключ:
            return screen
    доска = screens_mod.Screen(
        key=ключ,
        title=f"{владелец.title or владелец.name} · консилиум",
        profile=владелец,
        panes=[
            screens_mod.Pane(key=участник.name, title=участник.title or участник.name, profile=участник)
            for участник in участники
        ],
    )
    state.screens.append(доска)
    return доска


async def созвать(state, вопрос: str, имена: list[str], *, владелец: Profile) -> list[tuple[str, str]] | None:
    """Созвать консилиум: вопрос уходит профилям `имена` одновременно, ответы — вызвавшему.

    Устройство доски: `доска_консилиума(state, владелец, …)` — отдельный экран владельца с
    панелями участников, без экрана сводки: сводит сама модель, созвавшая консилиум. Строки о
    созыве, не найденных и не ответивших идут в главный экран: созывает его собеседник, и
    человек смотрит туда.

    Возвращает пары «имя профиля, текст ответа» только ответивших; пустой список — не ответил
    никто; `None` — не найден ни один профиль, запросов не было (это тоже сказано вслух).
    Сводного запроса нет.
    """
    главный = state.main
    участники = _загрузить(state, имена, главный)
    if not участники:
        output.append_log(state, ui.error_fragments("консилиум не созван: ни один профиль не найден"), главный)
        return None
    доска = доска_консилиума(state, владелец, участники)
    output.append_log(
        state,
        ui.system_fragments(
            f"консилиум созван: {', '.join(у.name for у in участники)} — ответы на вкладке «{доска.title}»"
        ),
        главный,
    )
    ответы, молчавшие = await _опросить(state, вопрос, участники, доска, uuid4().hex[:8])
    for имя, причина in молчавшие:
        output.append_log(
            state, ui.error_fragments(_о_молчании(f"участник консилиума «{имя}» ответа не дал", причина)), главный
        )
    return ответы


def build_summary_request(question: str, answers: list[tuple[str, str]]) -> str:
    parts = [f"Задача:\n{question}", ""]
    for name, text in answers:
        parts.append(f"Ответ эксперта «{name}»:\n{text.strip()}")
        parts.append("")
    return "\n".join(parts).strip()


async def run(state, question: str, lead: Profile, *, announce: bool = True) -> output.Outcome | None:
    """Поднять группу ведущего профиля на этом вопросе и свести ответы.

    Сводку возвращаем вызывающему, а в главный экран не пишем: куда девать итог, решает тот,
    кто группу поднял. Поднял пользователь — итог кладёт очередь запросов; поднял набор
    способов — он же, вместе с итогами остальных способов и в порядке набора."""
    run_id = uuid4().hex[:8]
    agents = load_agents(state, lead)
    if not agents:
        output.append_log(state, ui.error_fragments("группа не поднята: ни один профиль агента не найден"))
        return None

    board, summary_screen = ensure_screens(state, lead, agents)
    if announce:
        output.append_log(state, ui.team_start_fragments([expert.name for expert in agents]))

    answers, failed = await _опросить(state, question, agents, board, run_id)
    for name, причина in failed:
        output.append_log(
            state,
            ui.error_fragments(_о_молчании(f"агент «{name}» ответа не дал — в сводку не попал", причина)),
            summary_screen,
        )
    if not answers:
        output.append_log(state, ui.error_fragments("сводить нечего: ни один агент не ответил"), summary_screen)
        return None

    output.append_log(state, ui.team_summary_label_fragments(len(answers)), summary_screen)
    if not lead.system:
        # Запасную инструкцию уже несёт профиль собеседника сводки (`summary_profile`) —
        # здесь только говорим об этом вслух, чтобы подмена не была молчаливой.
        output.append_log(
            state, ui.system_fragments("у ведущего нет своей инструкции — свожу по общему правилу"), summary_screen
        )
    summary = await output.run_turn(
        state,
        summary_screen.first.agent,
        build_summary_request(question, answers),
        pane=summary_screen.first,
        agent_name="lead",
        run_id=run_id,
    )
    if not (summary.ok and summary.text.strip()):
        return None
    return output.Outcome(kind="сводка группы", source=lead.name, text=summary.text)
