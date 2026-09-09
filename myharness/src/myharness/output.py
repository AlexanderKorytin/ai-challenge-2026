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
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from . import api, tokens, ui
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


def replace_log(
    state: State, pane: screens_mod.Pane, at: int, length: int, fragments: Fragments
) -> None:
    """Заменить кусок ленты на месте: `length` фрагментов, начиная с `at`, становятся новыми.

    Третий и последний способ менять ленту — рядом с `append_log`. Заведён ради строки
    ожидания: она живёт весь обмен и обновляется десять раз в секунду, а печатать её заново
    нельзя — ниже неё уже идёт ответ. Прежний способ (обрезать ленту до отметки и напечатать
    строку заново) на этом и ломался: обрезка сносила всё, что успело лечь ниже, и держалась
    на договорённости «пока строка крутится, ниже никто не пишет» — договорённости, которую
    поток событий нарушал первым же куском текста.

    Счётчики строк ведём здесь же, как и в `append_log`: разница «стало минус было», а не
    предположение «длина не меняется». Сегодня она и правда не меняется — строка ожидания
    занимает ровно одну строку в любом виде, — но счётчик, выведенный из чужого обещания,
    молча разъезжается ровно тогда, когда обещание перестают выполнять, а расплачивается за
    это прокрутка, уехавшая за край экрана.

    Вставка пустым срезом (`length == 0`) — тоже законный случай: так строка ожидания
    печатается в первый раз, и отдельного пути для первой печати не нужно.
    """
    removed = pane.log[at : at + length]

    def строк(items: Fragments, style: str | None = None) -> int:
        return sum(text.count("\n") for стиль, text in items if style is None or стиль == style)

    pane.line_count += строк(fragments) - строк(removed)
    pane.reasoning_lines += строк(fragments, screens_mod.REASONING) - строк(
        removed, screens_mod.REASONING
    )
    pane.log[at : at + length] = fragments
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


# Как часто перерисовывается кадр вращения строки ожидания. Восьмая доля секунды — предел,
# ниже которого глаз перестаёт различать движение, а выше — начинает считать кадры.
SPIN_INTERVAL = 0.08


def _session_before(state: State, pane: screens_mod.Pane, agent_obj: Agent) -> int:
    """Расход, к которому строка ожидания прибавляет живые числа идущего обмена.

    Он РАЗНЫЙ на главном экране и на экране агента, и это не мелочь показа: строки отвечают
    на разные вопросы. У эксперта группы одна задача и никакой переписки — его строка
    отвечает «во что обошёлся он», и общий итог сеанса стоял бы в ней чужим числом, к его
    работе не относящимся: человек сравнивает экспертов между собой, а сравнивать было бы
    нечего — у всех одно и то же большое число. На главном экране человек ведёт разговор и
    платит за окно целиком, поэтому там нужен весь сеанс, вместе с отработавшими экспертами:
    иначе счёт занижен ровно в тот день, когда он вырос, — когда подняли группу.

    Отсюда же исчезает беда, ради которой правило и заведено: складывать своё с чужим больше
    негде, и один и тот же обмен не может попасть сразу в три панели.

    Через `getattr`, а не прямым вызовом: состояние собирают проверки, и метода у их сборки
    может не оказаться. Отсутствие итога — не повод уронить обмен: колонка «Σ» покажет
    расход одного этого запроса, и это честнее, чем упасть на строке ожидания.

    Сам метод живёт в `cli.State`, потому что итог складывается по агентам панелей, а панели
    и экраны — это `cli`. Ввозить `cli` сюда нельзя: вывод про него не знает и знать не
    должен, иначе вернётся круг импортов, ради разрыва которого и заведён этот модуль.
    """
    главный = getattr(state, "main", None)
    if главный is None or pane is not главный.first:
        return int(agent_obj.session_usage.get("total_tokens", 0))
    getter = getattr(state, "session_usage_total", None)
    if getter is None:
        return 0
    try:
        return int(getter().get("total_tokens", 0))
    except Exception:  # noqa: BLE001 — счёт расхода вспомогателен, обмен из-за него не роняем
        return 0


def _predict_outgoing(state: State, agent_obj: Agent, content: str) -> int:
    """Сколько примерно весит уходящий запрос — до того, как он собран.

    Число нужно СРАЗУ: строка ожидания показывает «↑» с первой доли секунды, а точный вес
    известен только после сборки запроса. Собрать запрос заранее нельзя — `build_messages`
    меняет состояние агента: внутри неё работает обрезка памяти, и позови мы её здесь, пары
    выбрасывались бы дважды за один обмен, второй раз впустую, а `restored_pairs` и
    `dropped_pairs` описывали бы не тот запрос, что ушёл. Поэтому вес складывается из частей,
    которые агент отдаёт наружу, не трогая память: системная инструкция, вес памяти
    (`history_tokens`, у профиля без истории он нулевой) и текст самого вопроса, плюс
    надбавка обёртки, подстроенная этим агентом под эту модель.

    Системная часть берётся у агента целиком, вместе с блоком глобальных фактов: по этому
    числу решается, предупреждать ли о переполнении окна, и занижение здесь не безобидно.
    Пока фактов не считали, разница доходила до девятисот токенов — а значит, чем больше
    человек рассказал о себе, тем вероятнее он получал отказ сервера вместо предупреждения.

    Приблизительным число остаётся: в нём нет поправки на пары, которые обрезка ещё
    выбросит, — то есть оно скорее завышено, а это безопасная сторона. Точное
    `turn.predicted_prompt` встаёт на его место, когда строка замирает.
    """
    превью: list[dict] = []
    системная = agent_obj.system_text()
    if системная:
        превью.append({"role": "system", "content": системная})
    превью.append({"role": "user", "content": content})
    обёртка = agent_obj.overhead(state.model)
    return tokens.count_messages(превью, overhead=обёртка) + agent_obj.history_tokens()


def _show_wait(
    state: State,
    pane: screens_mod.Pane,
    marks: dict[str, Any],
    *,
    mark: str,
    frozen: bool = False,
) -> None:
    """Показать строку ожидания в её нынешнем виде — заменой на месте, а не новой печатью.

    Единственное место, где строка ожидания попадает в ленту: и первая печать, и каждое
    обновление живых чисел, и замирание в конце обмена. Одно место потому, что вид строки
    обязан быть один: разойдись живой и замерший вид хоть числом фрагментов, замена среза
    сдвинула бы всё, что напечатано ниже.

    Σ — расход вместе с идущим обменом: на главном экране за весь сеанс, на экране агента —
    его собственный (`_session_before` объясняет, почему они разные). Слагаемых текущего
    обмена в счётчиках агента ещё нет — они появляются, когда обмен закончен, — поэтому
    живые числа прибавляются здесь. В замершем виде формула та же: `run_turn` подставляет
    в `outgoing` и `incoming` серверные числа, и сумма сходится с тем, что записано в расход.
    """
    at = marks.get("wait_at")
    if at is None:  # строка ещё не заведена — обновлять нечего
        return
    исходящие = marks.get("outgoing", 0)
    входящие = marks.get("incoming", 0)
    # Обмен не состоялся — прибавлять к итогу нечего: отвергнутый запрос не оплачен.
    итог = marks.get("session", 0) if marks.get("session_only") else marks.get("session", 0) + исходящие + входящие
    фрагменты = ui.waiting_fragments(
        mark,
        time.monotonic() - marks.get("started", time.monotonic()),
        исходящие,
        входящие,
        итог,
        exact=marks.get("exact", True),
        frozen=frozen,
    )
    replace_log(state, pane, at, marks.get("wait_len", 0), фрагменты)
    marks["wait_len"] = len(фрагменты)


async def _spin(state: State, pane: screens_mod.Pane, marks: dict[str, Any]) -> None:
    """Вращение строки ожидания: меняет кадр и просит её перерисоваться.

    Своей строки в ленте у счётчика больше нет — есть общая строка обмена, и печатает её
    `_show_wait`. Отмена никакой уборки не требует: строка остаётся в ленте намеренно, а
    замораживает её `run_turn` в своём `finally` — по любому пути, включая отмену.
    """
    i = 0
    while True:
        await asyncio.sleep(SPIN_INTERVAL)
        i += 1
        marks["frame"] = ui.SPINNER_FRAMES[i % len(ui.SPINNER_FRAMES)]
        _show_wait(state, pane, marks, mark=marks["frame"])


def draw_event(state: State, pane: screens_mod.Pane, event: api.StreamEvent, marks: dict) -> None:
    """Нарисовать одно событие потока. Единственный обработчик на все режимы.

    `marks` — память между событиями одного обмена: где стоит строка ожидания и что в ней
    показано, напечатан ли заголовок размышлений, напечатан ли ярлык ответа. Без неё ярлыки
    печатались бы перед каждым куском текста.

    Строку ожидания событие больше не гасит: она живёт весь обмен, а свой кусок текста
    событие печатает ниже неё.
    """
    if event.kind in ("reasoning", "content"):
        # Кусок потока считаем за токен: живой замер 2026-09-09 дал 180 кусков размышления
        # при `reasoning_tokens` = 180. Число приблизительное и живёт до конца обмена —
        # точное придёт в `usage`, и оно же встанет в замершую строку.
        marks["incoming"] = marks.get("incoming", 0) + 1
    if event.kind == "meta":
        return
    if event.kind == "reasoning":
        if not marks.get("reasoning"):
            # Заголовок области — единственное, что остаётся видимым в свёрнутом виде,
            # поэтому его стиль отличается от стиля самих размышлений: по стилю их и
            # отбирает `Pane.visible_log`. Чисел ещё нет — обмен только начался; место
            # заголовка запоминаем, чтобы в конце подставить их на то же место.
            marks["reasoning"] = True
            marks["reasoning_started"] = time.monotonic()
            marks["head_at"] = len(pane.log)
            заголовок = ui.reasoning_head_fragments(None, None)
            marks["head_len"] = len(заголовок)
            append_log(state, заголовок, pane)
        append_log(state, [(screens_mod.REASONING, event.text)], pane)
        _show_wait(state, pane, marks, mark=marks.get("frame", ui.SPINNER_FRAMES[0]))
        return
    if not marks.get("answer"):
        if marks.get("reasoning"):
            # Время размышлений снимаем здесь: первый кусок ответа и есть тот миг, когда
            # модель перестала думать и начала отвечать. Замерь мы его в конце обмена — в
            # заголовке оказалась бы длительность всего обмена, то есть неправда.
            marks.setdefault("reasoning_seconds", time.monotonic() - marks["reasoning_started"])
            # Разделитель между черновиком и ответом принадлежит черновику и прячется
            # вместе с ним: иначе свёрнутая область из одной строки занимает две, и
            # под заголовком остаётся необъяснимая пустая строка.
            append_log(state, [(screens_mod.REASONING, "\n")], pane)
        append_log(state, ui.answer_label_fragments(), pane)
        marks["answer"] = True
    append_log(state, [("", event.text)], pane)
    _show_wait(state, pane, marks, mark=marks.get("frame", ui.SPINNER_FRAMES[0]))


def _freeze_wait(
    state: State,
    pane: screens_mod.Pane,
    marks: dict[str, Any],
    turn: Turn | None,
    *,
    cancelled: bool,
) -> None:
    """Остановить строку ожидания навсегда: знак исхода вместо кадра, серверные числа вместо
    живых.

    Строка НЕ стирается ни на одном пути — в этом весь смысл затеи. Она остаётся в ленте, а
    следующий вопрос заводит свою, и лента сама становится таблицей роста расхода за диалог:
    видно, как «↑» и «Σ» росли от вопроса к вопросу, и для этого не нужно ни отдельного
    экрана, ни отчёта.

    Серверным числам верим больше своих: предсказание входа приблизительно по устройству, а
    выход мы считали кусками потока. Нет серверных — оставляем последние живые, но помечаем
    строку неточной: показать приблизительное число как точное значит соврать там, где
    человек по нему считает деньги.
    """
    if marks.get("wait_at") is None:
        return
    if turn is not None and turn.usage:
        счёт = tokens.normalize(turn.usage)
        marks["outgoing"] = счёт["prompt_tokens"]
        marks["incoming"] = счёт["completion_tokens"]
        marks["exact"] = True
    else:
        # Серверных чисел нет: обмен оборвался ошибкой либо его отменили. Вход уточняем
        # только на первом из двух путей: при отмене `exchange` пробрасывает `CancelledError`,
        # присваивание результата не выполняется, и `turn` здесь всегда `None` — уточнять
        # нечем, остаётся живая прикидка. `turn.predicted_prompt` считан по РЕАЛЬНОМУ составу
        # запроса, живое число — по прикидке до сборки; оба предсказания, поэтому строка
        # честно остаётся помеченной неточной.
        if turn is not None and turn.predicted_prompt:
            marks["outgoing"] = turn.predicted_prompt
        marks["exact"] = False
        # Итог сеанса за несостоявшийся обмен не растёт. Живой Σ складывался из расхода до
        # обмена и того, что мы насчитали сами, — но отвергнутый сервером запрос не оплачен
        # ни на токен, и оставить в замершей строке прибавку значило бы показать трату,
        # которой не было. Замечено на живом прогоне: разговор с документом под потолок
        # упёрся в окно, и в строке отказа стояло Σ 3.1M вместо настоящих 2.1M.
        marks["session_only"] = True
    if cancelled:
        исход = "cancelled"
    elif turn is not None and turn.ok:
        исход = "ok"
    else:
        исход = "error"
    _show_wait(state, pane, marks, mark=ui.WAIT_MARKS[исход], frozen=True)


def _finish_reasoning_head(
    state: State, pane: screens_mod.Pane, marks: dict[str, Any], turn: Turn | None
) -> None:
    """Дописать в заголовок размышлений то, чего в начале обмена ещё не знали: цену черновика
    в токенах и время, которое модель на него потратила.

    Заголовок подменяется на месте по тем же правилам, что и строка ожидания: число
    фрагментов у него одинаково во всех видах, поэтому напечатанное ниже не сдвигается.

    Ноль токенов размышлений показываем как «неизвестно», а не как ноль: сервер присылает
    это поле не всегда, и «▸ размышления · 0 токенов» под непустым черновиком — прямая ложь
    о том, за что человек заплатил.
    """
    at = marks.get("head_at")
    if at is None:
        return
    рассуждения = tokens.normalize(turn.usage)["reasoning_tokens"] if turn is not None else 0
    секунды = marks.get("reasoning_seconds")
    if секунды is None and marks.get("reasoning_started") is not None:
        # Ответа так и не было — одни размышления. Тогда время черновика и есть время обмена.
        секунды = time.monotonic() - marks["reasoning_started"]
    заголовок = ui.reasoning_head_fragments(рассуждения or None, секунды)
    replace_log(state, pane, at, marks.get("head_len", 0), заголовок)
    marks["head_len"] = len(заголовок)


def _warn_if_over_window(state: State, pane: screens_mod.Pane, agent_obj: Agent, предсказание: int) -> None:
    """Сказать заранее, что запрос не влезет в окно модели, — до отправки, а не после отказа.

    Ради этого и заведён собственный счёт токенов. Проверено живьём 2026-09-09: на третьем
    вопросе разговора с документом под потолок наше предсказание дало 1 049 323 токена, сервер
    в отказе насчитал 1 049 324 — расхождение в один токен на миллион. Значит предупредить
    можно честно, не гадая.

    Место под ответ входит в окно, а не идёт сверх него: в том же отказе сервер сложил
    1 047 324 токена сообщений и 2000 запрошенного ответа и сравнил сумму с окном.

    Запрос при этом всё равно уходит. Отказать самим значило бы поставить свою оценку выше
    ответа сервера: ошибись мы в большую сторону — и человек не смог бы отправить запрос,
    который на самом деле проходит. Наше дело — назвать причину заранее, а решает сервер.
    """
    место_под_ответ = agent_obj.profile.params.get("max_tokens") or 0
    всего = предсказание + (место_под_ответ if isinstance(место_под_ответ, int) else 0)
    if всего <= tokens.CONTEXT_WINDOW:
        return
    append_log(
        state,
        ui.hint_fragments(
            f"запрос не влезет в окно модели: {ui.format_exact(всего)} из "
            f"{ui.format_exact(tokens.CONTEXT_WINDOW)} (включая {ui.format_exact(место_под_ответ)} "
            "на ответ) — очистите историю командой /clear или задайте вопрос короче"
        ),
        pane,
    )


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
    в журнал), показ целиком — здесь (строка ожидания, ярлыки, строка итога, сообщение
    об ошибке). Панель задаётся явно, потому что исполнители отвечают одновременно: у
    каждого своя лента, а `State` у них общий.

    Своя строка ожидания у каждого обмена, и заводится она до первого события: «↑» видно с
    первой доли секунды, потому что вес запроса считается офлайн, а не ждёт ответа сервера.
    """
    assert state.client is not None
    pane.status = screens_mod.BUSY
    marks: dict[str, Any] = {
        "started": time.monotonic(),
        # Предсказанный вес запроса кладём ДО обмена: он и есть «↑» первой доли секунды.
        "outgoing": _predict_outgoing(state, agent_obj, content),
        "incoming": 0,
        # Расход ДО этого обмена: на главном экране — всего сеанса, на экране агента — его
        # собственный (см. `_session_before`). Живые числа прибавляются к нему на показе.
        "session": _session_before(state, pane, agent_obj),
        # Точен ли счёт входа: со словарём токенизатора — да, по знакам — нет, и тогда
        # строка честно ставит «~».
        "exact": tokens.exact(),
        "frame": ui.SPINNER_FRAMES[0],
        "wait_at": len(pane.log),
        "wait_len": 0,
    }
    # Предупреждение печатаем ДО того, как заведена строка ожидания, и отметку строки берём
    # после него: иначе оно встаёт в ленте ниже строки, к которой относится, и читается как
    # сказанное после отправки.
    _warn_if_over_window(state, pane, agent_obj, marks["outgoing"])
    marks["wait_at"] = len(pane.log)
    _show_wait(state, pane, marks, mark=marks["frame"])
    spinner_task = asyncio.create_task(_spin(state, pane, marks))

    def on_event(event: api.StreamEvent) -> None:
        draw_event(state, pane, event, marks)

    turn: Turn | None = None
    cancelled = False
    try:
        turn = await agent_obj.exchange(
            state.client,
            state.model,
            content,
            on_event=on_event,
            agent=agent_name,
            run_id=run_id,
        )
    except asyncio.CancelledError:
        # Отмену надо не только пробросить, но и НАЗВАТЬ: у оборванного запроса свой знак
        # исхода, и по замершей строке должно быть видно, что обмен прервал человек, а не
        # сеть. Отличить одно от другого по `turn` нельзя — при отмене его просто нет.
        cancelled = True
        raise
    finally:
        # Вращение гасим первым делом: дождаться его отмены можно только отсюда (`draw_event`
        # синхронный), а пока задача жива, она вправе перерисовать строку поверх замершей.
        spinner_task.cancel()
        with suppress(asyncio.CancelledError):
            await spinner_task
        _freeze_wait(state, pane, marks, turn, cancelled=cancelled)
        _finish_reasoning_head(state, pane, marks, turn)
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
                ui.meta_fragments(
                    turn.finish_reason,
                    turn.usage,
                    agent_obj.profile.name,
                    tokens.format_price(tokens.price(turn.usage, turn.model)),
                ),
                pane,
            )
            if turn.finish_reason == "length":
                append_log(
                    state,
                    ui.hint_fragments("ответ упёрся в max_tokens — увеличьте лимит: /set max_tokens"),
                    pane,
                )
        if turn is not None and turn.over_budget:
            # Предел, который тихо не сработал, хуже отсутствующего: на отсутствующий человек
            # не рассчитывает. Обрезка выбросила всё, что могла, а системная часть с вопросом
            # уже перевесили предел — сказать об этом обязаны.
            append_log(
                state,
                ui.hint_fragments(
                    "запрос ушёл тяжелее заданного предела — уменьшить нечего: "
                    "поднимите предел (/budget) или сократите вопрос"
                ),
                pane,
            )
        if turn is not None:
            # Деньги копим тем тарифом, что действовал на этот обмен: модель меняется на
            # лету, и пересчёт итога по текущей модели врал бы втрое.
            цена = tokens.price(turn.usage, turn.model) if turn.usage else 0.0
            if цена is None:
                state.session_cost_known = False
            else:
                state.session_cost += цена
            warn_journal(state, turn.journal_error)
            warn_store(state, turn.store_error)
    return turn
