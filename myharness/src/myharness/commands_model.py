"""Команды про то, ЧЕМ отвечает модель: ключ, модель, профиль, режим управления контекстом.

Команды собраны вместе не по алфавиту: все они меняют не содержимое разговора, а условия, в
которых он идёт. Три из четырёх — `/model`, `/profile`, `/strategy` — работают двумя способами:
доводом в строке (`/model deepseek-v4-pro`) и панелью выбора, когда довода нет; панель у них
общая (`picker`), и правится этот способ в одном месте. `/auth` стоит особняком: довода он не
берёт вовсе и панели не открывает, а ждёт следующую строку — ключ не показывают списком.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from . import context_strategy, helpdoc, profiles, ui
from . import picker as picker_mod
from .api import DeepSeekClient
from . import interview, memory, profile_maker
from .config import save as save_config
from .output import append_log, refresh
from .panes import active_profile
from .state import State
from .strategies import STRATEGY_TITLES, switch_profile, use_strategy
from .workers import track_submission


def cmd_auth(state: State) -> None:
    state.awaiting_key = True
    append_log(state, ui.system_fragments("введите API-ключ DeepSeek (ввод скрыт звёздочками), затем Enter:"))


async def do_auth(raw_key: str, state: State) -> None:
    key = raw_key.strip()
    if not key:
        append_log(state, ui.hint_fragments("ключ не введён, отменено"))
        return
    append_log(state, ui.system_fragments("проверяю ключ…"))
    candidate = DeepSeekClient(key)
    try:
        ok = await candidate.validate()
    except Exception as exc:
        append_log(state, ui.error_fragments(f"не удалось проверить ключ: {exc}"))
        await candidate.aclose()
        return
    if not ok:
        append_log(state, ui.error_fragments("ключ не принят DeepSeek API (проверьте правильность)"))
        await candidate.aclose()
        return
    if state.client:
        await state.client.aclose()
    state.client = candidate
    state.config.api_key = key
    save_config(state.config)
    append_log(state, ui.system_fragments("авторизация сохранена — вводить ключ заново не потребуется"))


def set_model(state: State, name: str) -> None:
    state.model = name
    state.config.model = name
    save_config(state.config)
    append_log(state, ui.system_fragments(f"модель установлена: {name}"))


async def cmd_model(state: State, arg: str) -> None:
    if arg:
        set_model(state, arg)
        return
    models = state.known_models
    if state.client:
        try:
            models = await state.client.list_models()
            state.known_models = models
        except Exception as exc:
            append_log(state, ui.error_fragments(f"не удалось получить список моделей: {exc}"))
            append_log(state, ui.system_fragments("показан статический список"))
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for name in models:
        if name == state.model:
            marked = len(items)
        items.append(
            picker_mod.Item(
                label=name,
                hint="текущая" if name == state.model else "",
                payload=name,
            )
        )

    def choose(payload: Any) -> None:
        state.picker = None
        set_model(state, str(payload))

    state.picker = picker_mod.Picker(
        title="/model — модель DeepSeek",
        description="какой моделью отвечать",
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


def open_help_picker(state: State) -> None:
    """Меню разделов справки. Точки «текущего значения» нет: у справки его нет, а раздел
    читается в ленте, потому что панель выбора прокручивать текст не умеет."""
    items = [
        picker_mod.Item(label=раздел.заголовок, hint=раздел.подсказка, payload=раздел)
        for раздел in helpdoc.РАЗДЕЛЫ
    ]

    def choose(payload: Any) -> None:
        state.picker = None
        append_log(state, helpdoc.фрагменты(payload))

    state.picker = picker_mod.Picker(
        title="/help — справка",
        description="какой раздел показать",
        items=items,
        on_choose=choose,
    )
    refresh(state)


def open_profile_picker(state: State) -> None:
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for name, source in profiles.available():
        if name == state.profile.name:
            marked = len(items)
        hint = str(source.parent) if source else "встроенный"
        items.append(
            picker_mod.Item(label=name, hint=hint, payload=name)
        )

    def choose(payload: Any) -> None:
        state.picker = None
        task = asyncio.create_task(switch_profile(state, str(payload)))
        track_submission(state, task)

    description = "какой профиль генерации применить"
    if len(items) <= 1:
        # Один встроенный профиль в списке почти всегда значит не «профилей нет», а
        # «harness запущен не из той папки»: профили ищутся рядом с каталогом запуска.
        # Человек в этот момент смотрит именно сюда, поэтому и сказать надо здесь.
        description += f" · профили ищутся в {profiles.search_hint()}"
    if state.profile_dirty:
        description += " (текущие изменения не сохранены — /profile save <имя>)"
    state.picker = picker_mod.Picker(
        title="/profile — профиль генерации",
        description=description,
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


# Слова команды `/profile`. Держим их здесь, рядом с разбором: копия в подсказке разошлась бы
# с командой при первом же переименовании, а проверка осталась бы зелёной.
СЛОВО_NEW = "new"
# Уточнение первым словом описания: профиль для этого проекта, а не глобальный.
СЛОВО_ПРОЕКТ = "--проект"
СЛОВО_ОТМЕНА = "отмена"


def _записи_о_человеке(state: State) -> tuple[list[str], list[str]]:
    """Записи о человеке для запросов мастера. Сбой чтения — пустой список, а не отказ.

    Обряд без записей беднее (он не спросит про язык занятия), но он возможен; отказаться
    заводить профиль из-за попорченного файла памяти значило бы запереть человека там, где
    работа ещё вся впереди.

    Вторым значением — тексты записей-инвариантов из ТОГО ЖЕ снимка: перекрытия сверяются с тем,
    что видел мастер, и одним чтением диска, под той же защитой от сбоя."""
    try:
        снимок, жалобы = memory.load_records()
    except Exception as exc:  # noqa: BLE001 — хранилище живёт на диске и правится руками
        append_log(
            state,
            ui.error_fragments(f"записи о вас не прочитаны ({exc}) — обряд пойдёт без них"),
        )
        return [], []
    for жалоба in жалобы:
        # Молчать нельзя: с обеднённым списком модель не задаст вопроса, который должна была
        # задать, и человек не поймёт, почему профиль вышел не таким.
        append_log(state, ui.error_fragments(жалоба))
    return [текст for текст, _ in снимок], [текст for текст, инвариант in снимок if инвариант]


def _подготовить_профиль(state: State, интервью: Any) -> bool:
    """Что прочитать и что сказать до первого платного запроса мастера.

    Записи о человеке и список заведённых профилей читаются ЗДЕСЬ, а не в команде, и это не
    перестановка ради красоты: подготовка зовётся уже после всех заслонов обряда. Читай мы
    раньше — диск читался бы и тогда, когда обряд не начнётся вовсе, а жалобы на попорченную
    память печатались бы в ответ на команду, которая ничего не начала. Второе условие с теми же
    тремя заслонами в команде разошлось бы с настоящими молча: обряд пошёл бы с пустыми
    записями, и модель не увидела бы о человеке ничего.

    Заслона на занятое имя здесь нет намеренно: имени до сборки не существует, его придумает
    модель. Цена названа в требовании к команде.
    """
    интервью.записи, интервью.инварианты = _записи_о_человеке(state)
    интервью.доступные = [имя for имя, _ in profiles.available()]
    append_log(
        state,
        ui.system_fragments(
            "завожу профиль занятия по вашему описанию — спрашиваю у модели, чего в нём не хватает"
        ),
    )
    return True


def _шаг_запрос_профиля(интервью: Any) -> tuple[Any, str]:
    """Запрос очередного шага: сперва разметка областей, потом вопрос про область из очереди.

    Очередь ведёт ПРОГРАММА: полнота, отданная на самосуд модели, держалась ровно до первого
    описания, где модель сочла область закрытой домыслом, — и обряд кончался одним вопросом.
    """
    состояние = интервью.состояние_рода
    if not состояние.get("размечено"):
        return profile_maker.разметка_profile(), profile_maker.questions_request(
            интервью.описание, интервью.записи, интервью.доступные, интервью.пары
        )
    очередь = состояние.get("очередь") or []
    if not очередь:
        # Сюда не попасть: пустую очередь ловит `пора_собирать` до всякого запроса. Но послать
        # модели запрос «спроси про область» без области значило бы оплатить обмен, в котором
        # инструкция и вход противоречат друг другу, — лучше громкая неудача с названной причиной.
        raise RuntimeError("очередь областей пуста — спрашивать нечего, а запрос собирается")
    # Область не снимается с очереди здесь: снимает её разбор, когда вопрос УЖЕ задан. Сними мы
    # её сейчас, оборванный запрос уносил бы область с собой.
    return profile_maker.questions_profile(), profile_maker.questions_request(
        интервью.описание, интервью.записи, интервью.доступные, интервью.пары, очередь[0]
    )


def _шаг_разбор_профиля(текст: str, интервью: Any) -> str | None:
    """Ответ модели — в вопрос человеку, а разметка — в очередь областей.

    `None` означает «спрашивать нечего» и возвращается ТОЛЬКО с пустой очередью: пока в ней
    есть область, вопрос находится всегда — модельный или запасной.
    """
    состояние = интервью.состояние_рода
    if not состояние.get("размечено"):
        вопрос, область, очередь = profile_maker.parse_разметку(текст)
        состояние["размечено"] = True
        состояние["очередь"] = очередь
        if not очередь:
            return None
        # Снимаем ту область, про которую модель СПРОСИЛА, а не первую по счёту: спросив про
        # «стиль» и умолчав про «роль», она вычеркнула бы роль, и её не спросили бы никогда.
        снимаемая = область if область in очередь else очередь[0]
        очередь.remove(снимаемая)
        return вопрос or profile_maker.вопрос_по_умолчанию(снимаемая)
    очередь = состояние.get("очередь") or []
    if not очередь:
        return None
    область = очередь.pop(0)
    return profile_maker.parse_вопрос(текст, область)


def _пора_собирать_профиль(интервью: Any) -> bool:
    """Разметка есть, очередь пуста — спрашивать нечего, и узнаётся это БЕЗ обмена с моделью."""
    состояние = интервью.состояние_рода
    return bool(состояние.get("размечено")) and not состояние.get("очередь")


def _итог_запрос_профиля(интервью: Any) -> tuple[Any, str]:
    return profile_maker.draft_profile(), profile_maker.draft_request(
        интервью.описание, интервью.записи, интервью.доступные, интервью.пары
    )


def _записать_профиль(state: State, интервью: Any, текст: str) -> None:
    """Ответ модели — в профиль, профиль — парой файлов на диск, инструмент — в него.

    Порядок здесь не случаен. Сперва отбор (помощники, перекрытые записи) и проверка имени,
    потом запись, и только потом переход: перейти в профиль, которого нет на диске, значило бы
    получить его при следующем запуске из воздуха — то есть не получить вовсе.
    """
    черновик = profile_maker.parse_draft(текст)  # ValueError поднимается наверх, там его ждут
    профиль, жалобы = profile_maker.собрать(
        черновик,
        доступные=интервью.доступные,
        записи=интервью.записи,
        инварианты=интервью.инварианты,
    )
    for жалоба in жалобы:
        # Отброшенное называем вслух: за строку заплачен обмен, и человек должен знать, чего в
        # профиле нет и почему.
        append_log(state, ui.hint_fragments(жалоба))

    беда_имени = profiles.проверить_имя(профиль.name)
    if беда_имени:
        raise ValueError(f"модель предложила негодное имя профиля: {беда_имени}")

    # Сверяем имя со ВСЕМИ заведёнными профилями, а не только с каталогом записи. `save_pair`
    # видит один каталог — тот, что выбрал `target_dir`, — и профиль с тем же именем из
    # другого каталога отказа бы не вызвал: один из двух молча перекрыл бы другой, и тот стал
    # бы недостижим по имени, оставшись лежать на диске.
    занято, источник = next(
        (
            (имя, путь)
            for имя, путь in profiles.available()
            if имя.casefold() == профиль.name.casefold()
        ),
        (None, None),
    )
    if занято is not None:
        interview.прибрать(state, интервью)
        где = f": {источник}" if источник is not None else " (встроенный)"
        append_log(state, ui.error_fragments(f"профиль с именем «{занято}» уже есть{где}"))
        append_log(
            state,
            ui.hint_fragments(
                "профиль не записан; повторите, назвав другое имя прямо в описании: "
                + ("/profile new --проект <описание>" if интервью.состояние_рода.get("проект") else "/profile new <описание>")
            ),
        )
        return

    каталог = profiles.target_dir(
        Path.cwd(), проект=bool(интервью.состояние_рода.get("проект"))
    )
    try:
        путь_json, путь_md = profiles.save_pair(профиль, каталог)
    except FileExistsError as exc:
        # Лежащий профиль мог быть написан человеком. Своё имя модель придумала сама, поэтому
        # и просим человека назвать другое — переименовывать за него мы не вправе.
        interview.прибрать(state, интервью)
        append_log(
            state,
            ui.error_fragments(f"профиль с именем «{профиль.name}» уже есть: {exc}"),
        )
        append_log(
            state,
            ui.hint_fragments(
                "профиль не записан; повторите с другим именем: "
                + ("/profile new --проект <описание>" if интервью.состояние_рода.get("проект") else "/profile new <описание>")
                + ", назвав имя прямо в описании"
            ),
        )
        return
    except OSError as exc:
        interview.прибрать(state, интервью)
        append_log(state, ui.error_fragments(f"профиль не записан: {exc}"))
        return

    interview.прибрать(state, интервью)
    append_log(state, ui.system_fragments(f"профиль «{профиль.name}» записан: {путь_json}"))
    if путь_md is not None:
        append_log(
            state,
            ui.hint_fragments(f"инструкция лежит рядом и правится руками: {путь_md}"),
        )
    append_log(state, ui.system_prompt_fragments(профиль.name, профиль.system or ""))
    append_log(state, ui.hint_fragments(profile_maker.словами_о_запросе(профиль.params)))
    if профиль.стадии is not None:
        # Карту называем отдельной строкой: она и есть то, что превращает профиль в автомат
        # задачи, и человек должен видеть её до первой `/task new`, а не узнать из файла.
        append_log(state, ui.hint_fragments(profile_maker.словами_о_карте(профиль.стадии)))
    if профиль.overrides:
        append_log(
            state,
            ui.hint_fragments(
                "в этом занятии не действуют записи о вас: "
                + "; ".join(профиль.overrides)
                + " — они остаются в памяти и помечаются в запросе"
            ),
        )
    if state.switching_profile:
        # Человек набрал `/profile <другой>`, пока шла сборка: `switch_profile` откажет, и
        # инструмент остался бы в прежнем профиле молча — при том, что новый уже на диске.
        append_log(
            state,
            ui.hint_fragments(
                f"идёт смена профиля — перейти в новый: /profile {профиль.name}"
            ),
        )
        return
    # Переход отдельной задачей: `switch_profile` дожидается очереди прежнего собеседника, а
    # обряд сейчас сам исполняется внутри задачи — ждать себя же он не вправе.
    track_submission(state, asyncio.create_task(switch_profile(state, профиль.name)))


РОД_ПРОФИЛЯ = interview.Род(
    имя="мастер профиля",
    итог_не_записан="профиль не записан",
    прерван="мастер профиля прерван",
    не_удался="мастер профиля не справился",
    итог_вот_вот="профиль вот-вот будет записан",
    команда_заново="/profile new",
    команда_заново_с_доводом="/profile new <описание>",
    команда_отмены="/profile отмена",
    агент=profile_maker.ИМЯ_МАСТЕРА,
    подсказка_после_неудачи=(
        "запрос к модели уже оплачен, профиль не записан; "
        "повторить — /profile new <описание>, завести руками — файл profiles/<имя>.json"
    ),
    сборка_объявление="описание и ответы собраны — прошу модель собрать профиль",
    подготовить=_подготовить_профиль,
    шаг_запрос=_шаг_запрос_профиля,
    шаг_разбор=_шаг_разбор_профиля,
    пора_собирать=_пора_собирать_профиль,
    итог_запрос=_итог_запрос_профиля,
    итог_записать=_записать_профиль,
)


async def cmd_profile(state: State, arg: str) -> None:
    if not arg:
        open_profile_picker(state)
        return
    parts = arg.split(maxsplit=1)
    слово = parts[0].lower()
    if слово == СЛОВО_ОТМЕНА:
        if not interview.отменить(state):
            append_log(state, ui.hint_fragments("прерывать нечего — обряд не идёт"))
        return
    if слово == СЛОВО_NEW:
        описание = parts[1].strip() if len(parts) > 1 else ""
        проект = False
        if описание.split(maxsplit=1)[:1] == [СЛОВО_ПРОЕКТ]:
            проект = True
            описание = описание[len(СЛОВО_ПРОЕКТ):].strip()
        if not описание:
            # Без описания мастеру не с чего начинать: он спросил бы «а чего вы хотите?» —
            # тот же вопрос, но за деньги.
            append_log(
                state,
                ui.hint_fragments(
                    "опишите занятие одной репликой: роль, стиль, формат ответа, чего не делать, "
                    "кого звать в помощь — например: /profile new профиль математика, отвечает "
                    "определением и формулой, без лирики; профиль ляжет в личный каталог, "
                    "для этого проекта — /profile new --проект <описание>"
                ),
            )
            return
        # Записи и список профилей читает подготовка обряда — она зовётся уже после заслонов.
        прежний = state.интервью
        interview.начать(state, род=РОД_ПРОФИЛЯ, описание=описание)
        # Выбор каталога живёт на обряде: запись случится через несколько ходов. Помечаем только
        # обряд, заведённый ЭТИМ вызовом: `начать` отказывает молча, если обряд уже идёт, и
        # пометка иначе легла бы на чужой опрос — и его профиль ушёл бы не в тот каталог.
        if проект and state.интервью is not None and state.интервью is not прежний:
            state.интервью.состояние_рода["проект"] = True
        return
    if слово == "save":
        target = active_profile(state)
        name = parts[1].strip() if len(parts) > 1 else target.name
        # Имя проверяем ДО того, как оно попало в живой профиль и на диск. Негодное имя здесь
        # не опечатка, а путь: `/profile save ../../ключи` означал бы запись мимо каталога
        # профилей. И живому профилю такое имя оставлять нельзя — следующий `/profile save`
        # повторил бы попытку уже без довода.
        беда_имени = profiles.проверить_имя(name)
        if беда_имени:
            append_log(state, ui.error_fragments(f"профиль не сохранён: {беда_имени}"))
            return
        прежнее_имя = target.name
        target.name = name
        try:
            path = profiles.save(target)
        except (OSError, ValueError) as exc:
            # `ValueError` ловим наравне с `OSError`: без него отказ уходил бы в задачу
            # `handle_submit`, где исключение снимается ради чистоты вывода, — и человек
            # получил бы на свою команду ПУСТУЮ ленту, ни строки о том, что ничего не вышло.
            target.name = прежнее_имя
            append_log(
                state,
                ui.error_fragments(f"не удалось сохранить профиль: {exc}"),
            )
            return
        state.profile_dirty = False
        state.config.profile = name
        save_config(state.config)
        append_log(state, ui.system_fragments(f"профиль сохранён: {path}"))
        соседняя = path.with_suffix(".md")
        if соседняя.exists():
            append_log(
                state,
                ui.hint_fragments(f"инструкция лежит рядом и правится руками: {соседняя}"),
            )
        return
    await switch_profile(state, parts[0])


def open_strategy_picker(state: State) -> None:
    source = active_profile(state)
    current = source.context_strategy
    items = [
        picker_mod.Item(
            label=STRATEGY_TITLES[strategy],
            hint="текущая" if strategy == current else "",
            payload=strategy,
        )
        for strategy in context_strategy.CONTEXT_STRATEGIES
    ]

    def choose(payload: Any) -> None:
        state.picker = None
        use_strategy(state, str(payload), source=source)

    state.picker = picker_mod.Picker(
        title="/strategy — стратегия контекста",
        description="какой независимый разговор открыть",
        items=items,
        on_choose=choose,
        index=context_strategy.CONTEXT_STRATEGIES.index(current),
        marked=context_strategy.CONTEXT_STRATEGIES.index(current),
    )
    refresh(state)


def cmd_strategy(state: State, arg: str) -> None:
    """Показать, открыть либо настроить независимый экран стратегии."""

    if not arg:
        profile = active_profile(state)
        append_log(
            state,
            ui.system_fragments(
                f"стратегия: {profile.context_strategy}; "
                f"строгое окно: {profile.strategy_window} пар"
            ),
        )
        open_strategy_picker(state)
        return

    parts = arg.split()
    action = parts[0].lower()
    if action == "use":
        if len(parts) != 2 or parts[1] not in context_strategy.CONTEXT_STRATEGIES:
            append_log(
                state,
                ui.error_fragments(
                    "нужен режим: /strategy use <standard|sliding|facts|branching>"
                ),
            )
            return
        use_strategy(state, parts[1], source=active_profile(state))
        return

    if action == "window":
        if len(parts) != 2:
            append_log(
                state,
                ui.error_fragments(
                    "нужно одно положительное целое число: /strategy window <N>"
                ),
            )
            return
        profile = active_profile(state)
        if profile.context_strategy not in (
            context_strategy.CONTEXT_SLIDING,
            context_strategy.CONTEXT_FACTS,
        ):
            append_log(
                state,
                ui.error_fragments(
                    "строгое окно применяется только в sliding и facts"
                ),
            )
            return
        raw_window = parts[1]
        window = int(raw_window) if raw_window.isdecimal() else 0
        if window <= 0:
            append_log(
                state,
                ui.error_fragments(
                    "размер окна должен быть положительным целым числом"
                ),
            )
            return
        profile.strategy_window = window
        state.profile_dirty = True
        append_log(
            state,
            ui.system_fragments(
                f"строгое окно стратегии {profile.context_strategy}: {window} пар"
            ),
        )
        append_log(
            state,
            ui.hint_fragments("сохранить в профиль: /profile save <имя>"),
        )
        return

    append_log(
        state,
        ui.error_fragments(
            "форма команды: /strategy [use <режим>|window <N>]"
        ),
    )
