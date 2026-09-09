"""Вывод в ленту и отрисовка обмена с моделью — отдельно от интерфейса.

Раньше это жило в `cli.py` вместе с разметкой окна, состоянием и обработкой клавиш. Из-за
этого оркестраторы `team` и `methods` — которым от `cli` нужна была только отрисовка обмена —
импортировали его лениво, прямо в телах функций: обычный импорт замкнул бы круг, ведь сам
`cli` импортирует и `team`, и `methods`. Ленивый импорт круг не разрывает, а прячет.

Здесь круга нет: модуль знает про экраны (`screens`), оформление (`ui`), поток событий (`api`)
и собеседника (`agent`), а про `cli` и оркестраторы — ничего. Ни один из этих модулей про
`output` не знает, поэтому импортировать его можно обычным образом, из любого места пакета.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import api, ui
from . import screens as screens_mod
from .agent import Agent, Turn

if TYPE_CHECKING:  # только для подсказок типов — на импорт cli вывод не завязан
    from .cli import State

Fragments = list[tuple[str, str]]


@dataclass
class Outcome:
    """Итог работы оркестратора — то, ради чего его поднимали.

    Оркестратор (группа, цепочка, набор способов) сам итог не показывает и не решает, куда
    его девать: он возвращает эту запись, а кладёт её в главный экран `deliver`. Разделение
    не формальность — набор способов состоит из тех же оркестраторов, и решай каждый из них
    судьбу своего итога сам, четыре способа писали бы в главный экран одновременно, вперемешку
    и в случайном порядке.
    """

    kind: str  # что это за итог: «сводка группы», «итог цепочки», «ответ способа»
    source: str  # кто его дал: имя ведущего, шага или способа
    text: str


def join_outcomes(outcomes: list[Outcome]) -> str:
    """Итоги одним текстом — для памяти главного агента.

    Один итог кладём как есть: пометка «кто сказал» в памяти лишняя, отвечал ровно один.
    Несколько — с заголовками, иначе в памяти окажется склейка из четырёх ответов, про
    которую нельзя сказать, где кончается один и начинается другой.
    """
    if len(outcomes) == 1:
        return outcomes[0].text.strip()
    return "\n\n".join(f"[{item.kind} «{item.source}»]\n{item.text.strip()}" for item in outcomes)


def deliver(state: State, question: str, outcomes: list[Outcome]) -> None:
    """Положить итоги оркестратора в главный экран — в ленту и в память главного агента.

    Единственное место, где это делается. До сих пор цепочка и группа оставляли результат на
    своих вкладках, а главный экран сообщал лишь «способы: …» — человек, ведущий разговор,
    обязан был сам пойти и посмотреть, чем всё кончилось.

    В память кладём тоже: разговор на главном экране продолжается после того, как отработала
    группа, и следующий вопрос («а покороче?») без итога в памяти повисает в пустоте. Пару
    пишем одну на весь прогон — вопрос был один, сколько бы способов на него ни отвечало.

    Модель называем ту, которой работает приложение: оркестратор ходил к ней же, а пометки
    в записи разговора должны говорить, чем ответ получен. Условие `keep_history` здесь не
    проверяется намеренно — оно живёт внутри `remember`, единственной точки пополнения, и
    вторая его копия тут разошлась бы с первой при первой же правке.
    """
    if not outcomes:
        return
    for item in outcomes:
        append_log(state, ui.outcome_fragments(item.kind, item.source, item.text), state.main)
    state.main_agent.remember(question, join_outcomes(outcomes), model=state.model)
    # Итог оркестратора кладётся на диск здесь же, и сбой этой записи обязан быть назван:
    # `Turn` тут не собирается, и без этой строки ошибка ушла бы в тишину.
    warn_store(state, state.main_agent.store_error)


def target_pane(state: State, target: screens_mod.Screen | screens_mod.Pane | None) -> screens_mod.Pane:
    """Куда писать. Без указания — первая панель экрана, где пользователь работает; экран
    вместо панели тоже принимается: у большинства экранов панель одна."""
    if isinstance(target, screens_mod.Pane):
        return target
    if isinstance(target, screens_mod.Screen):
        return target.first
    return state.focus.first


def append_log(
    state: State, fragments: Fragments, target: screens_mod.Screen | screens_mod.Pane | None = None
) -> None:
    pane = target_pane(state, target)
    pane.log.extend(fragments)
    pane.line_count += sum(text.count("\n") for _, text in fragments)
    # Строки размышлений считаем здесь же, рядом с общим счётчиком: лента пополняется
    # только отсюда, и два счётчика, растущие в соседних строках, разойтись не могут.
    pane.reasoning_lines += sum(
        text.count("\n") for style, text in fragments if style == screens_mod.REASONING
    )
    if state.app is not None:
        state.app.invalidate()


def truncate_log(
    state: State, mark: int, target: screens_mod.Screen | screens_mod.Pane | None = None
) -> None:
    pane = target_pane(state, target)
    removed = pane.log[mark:]
    pane.line_count -= sum(text.count("\n") for _, text in removed)
    pane.reasoning_lines -= sum(
        text.count("\n") for style, text in removed if style == screens_mod.REASONING
    )
    del pane.log[mark:]
    if state.app is not None:
        state.app.invalidate()


def refresh(state: State) -> None:
    if state.app is not None:
        state.app.invalidate()


def warn_journal(state: State, error: str | None) -> None:
    """Сказать пользователю, что журнал прогонов не пишется, — не больше одного раза за сеанс.

    Сбой журнала harness не роняет (требование `openspec/specs/journal/spec.md`), а причина у
    него обычно постоянная: нет прав на файл, кончилось место. Без флага `journal_warned` то же
    самое сообщение повторялось бы после каждого следующего запроса и вытеснило бы из ленты
    сами ответы.

    Записью в журнал модуль не занимается: сюда приходит уже готовая ошибка или её отсутствие.
    """
    if error and not state.journal_warned:
        state.journal_warned = True
        append_log(state, ui.error_fragments(error))


def warn_store(state: State, error: str | None) -> None:
    """Сказать, что разговор не сохраняется, — не больше одного раза за сеанс.

    Тот же приём и та же причина, что у предупреждения о журнале: сбой хранилища обмен не
    роняет, но причина у него постоянная (нет прав, кончилось место), и повторяться после
    каждого ответа сообщение не должно.

    Молчать нельзя тем более: человек уверен, что разговор переживёт перезапуск, а он не
    переживёт. Узнать об этом на следующем запуске по пустой ленте — худший из возможных
    способов.
    """
    if error and not state.store_warned:
        state.store_warned = True
        append_log(state, ui.error_fragments(error))


# ─────────────────────────────── генерация ответа ───────────────────────────────


async def _spin(state: State, pane: screens_mod.Pane, marks: dict[str, Any] | None = None) -> None:
    """Счётчик ожидания: крутится в своей строке, пока не пришло первое событие потока.

    Место, откуда счётчик начал писать, кладём в общую с отрисовкой памятку `marks`: гасит
    счётчик синхронный `draw_event`, дождаться отмены задачи он не может, поэтому стирает
    строку сам — и ему нужно знать, докуда обрезать ленту."""
    mark = len(pane.log)
    if marks is not None:
        marks["spin_mark"] = mark
    i = 0
    try:
        while True:
            frame = ui.SPINNER_FRAMES[i % len(ui.SPINNER_FRAMES)]
            truncate_log(state, mark, pane)
            append_log(state, [("class:dim", f"{frame} думаю…")], pane)
            i += 1
            await asyncio.sleep(0.08)
    except asyncio.CancelledError:
        # Обрезаем ленту только если её ещё не обрезал тот, кто нас погасил. Обрезать второй
        # раз нельзя: к этому моменту под тем же отступом уже лежит напечатанный ответ, и
        # `truncate_log` снёс бы его.
        if marks is None or not marks.get("spin_cleared"):
            truncate_log(state, mark, pane)
        raise


def _stop_spinner(state: State, pane: screens_mod.Pane, marks: dict[str, Any]) -> None:
    """Погасить счётчик ожидания и стереть его строку — ровно один раз за обмен.

    Отменяем задачу без ожидания: зовут отсюда и из синхронного `draw_event`, а дождаться
    отмены можно только из корутины — это делает `run_turn` в своём `finally`."""
    if marks.get("spin_cleared"):
        return
    marks["spin_cleared"] = True
    task = marks.get("spin_task")
    if task is not None and not task.done():
        task.cancel()
    mark = marks.get("spin_mark")
    if mark is not None:
        truncate_log(state, mark, pane)


def draw_event(state: State, pane: screens_mod.Pane, event: api.StreamEvent, marks: dict) -> None:
    """Нарисовать одно событие потока. Единственный обработчик на все режимы.

    `marks` — память между событиями одного обмена: погашен ли счётчик ожидания, напечатан
    ли ярлык рассуждений, напечатан ли ярлык ответа. Без неё ярлыки печатались бы перед
    каждым куском текста.
    """
    _stop_spinner(state, pane, marks)
    if event.kind == "meta":
        return
    if event.kind == "reasoning":
        if not marks.get("reasoning"):
            # Заголовок области — единственное, что остаётся видимым в свёрнутом виде,
            # поэтому его стиль отличается от стиля самих размышлений: по стилю их и
            # отбирает `Pane.visible_log`. Чисел ещё нет — обмен только начался.
            append_log(state, ui.reasoning_head_fragments(None, None), pane)
            marks["reasoning"] = True
        append_log(state, [(screens_mod.REASONING, event.text)], pane)
        return
    if not marks.get("answer"):
        if marks.get("reasoning"):
            # Разделитель между черновиком и ответом принадлежит черновику и прячется
            # вместе с ним: иначе свёрнутая область из одной строки занимает две, и
            # под заголовком остаётся необъяснимая пустая строка.
            append_log(state, [(screens_mod.REASONING, "\n")], pane)
        append_log(state, ui.answer_label_fragments(), pane)
        marks["answer"] = True
    append_log(state, [("", event.text)], pane)


async def run_turn(
    state: State,
    agent_obj: Agent,
    content: str,
    *,
    pane: screens_mod.Pane,
    agent_name: str | None = None,
    run_id: str | None = None,
) -> Turn:
    """Обмен агента с моделью, показанный в панели.

    Разделение обязанностей: разговор целиком — за агентом (память, сборка запроса, запись
    в журнал), показ целиком — здесь (счётчик ожидания, ярлыки, строка расхода, сообщение
    об ошибке). Панель задаётся явно, потому что исполнители отвечают одновременно: у
    каждого своя лента, а `State` у них общий.
    """
    assert state.client is not None
    pane.status = screens_mod.BUSY
    marks: dict[str, Any] = {}
    marks["spin_task"] = spinner_task = asyncio.create_task(_spin(state, pane, marks))

    def on_event(event: api.StreamEvent) -> None:
        draw_event(state, pane, event, marks)

    turn: Turn | None = None
    try:
        turn = await agent_obj.exchange(
            state.client,
            state.model,
            content,
            on_event=on_event,
            agent=agent_name,
            run_id=run_id,
        )
    finally:
        # Счётчик мог не получить ни одного события (мгновенная ошибка, отмена) — гасим и
        # здесь, а дождаться его отмены можно только отсюда: `draw_event` синхронный.
        _stop_spinner(state, pane, marks)
        with suppress(asyncio.CancelledError):
            await spinner_task
        pane.status = screens_mod.DONE if (turn is not None and turn.ok) else screens_mod.ERROR
        if turn is not None and not turn.ok and turn.error:
            # `Turn.error` бывает и не сетевой: агент кладёт сюда сбой отрисовки, но только
            # при удавшемся обмене. Поэтому про DeepSeek говорим лишь когда обмен не удался —
            # иначе пользователь пойдёт чинить связь, которая исправна.
            append_log(state, ui.error_fragments(f"ошибка запроса к DeepSeek: {turn.error}"), pane)
        if marks.get("reasoning") or marks.get("answer"):
            append_log(state, [("", "\n")], pane)
        if turn is not None and turn.dropped_pairs > 0:
            # Молчаливая обрезка недопустима. Первая же потерянная отсылка — «сделай короче»,
            # а того, что сокращать, в памяти уже нет — будет отлажена пользователем как
            # «модель поглупела», и час уйдёт на поиск поломки, которой нет.
            append_log(
                state,
                ui.system_fragments(
                    f"память обрезана: выброшено {turn.dropped_pairs} пар «вопрос — ответ»"
                ),
                pane,
            )
        if turn is not None and turn.ok:
            append_log(
                state,
                ui.meta_fragments(turn.finish_reason, turn.usage, turn.elapsed_ms / 1000, agent_obj.profile.name),
                pane,
            )
            if turn.finish_reason == "length":
                append_log(
                    state,
                    ui.hint_fragments("ответ упёрся в max_tokens — увеличьте лимит: /set max_tokens"),
                    pane,
                )
        if turn is not None:
            warn_journal(state, turn.journal_error)
            warn_store(state, turn.store_error)
    return turn
