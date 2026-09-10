"""Полноэкранный REPL: лог-панель сверху (растёт, автопрокрутка), поле ввода снизу,
окаймлённое горизонтальными линиями. Очередь запросов, отмена по Ctrl+C, потоковый ответ,
всплывающее меню команд по «/» и панель выбора значений параметров генерации."""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prompt_toolkit.application import Application, get_app
from prompt_toolkit.completion import Completer, Completion
from prompt_toolkit.data_structures import Point
from prompt_toolkit.filters import Condition
from prompt_toolkit.key_binding import KeyBindings
from prompt_toolkit.layout.containers import (
    ConditionalContainer,
    DynamicContainer,
    Float,
    FloatContainer,
    HSplit,
    VSplit,
    Window,
)
from prompt_toolkit.layout.controls import FormattedTextControl
from prompt_toolkit.layout.dimension import Dimension
from prompt_toolkit.layout.layout import Layout
from prompt_toolkit.layout.menus import CompletionsMenu
from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.widgets import TextArea

from . import api, archivist, background, compact, memory, profiles, team, tokens, ui
from . import methods as methods_mod
from . import params as params_mod
from . import picker as picker_mod
from . import screens as screens_mod
from .agent import Agent
from .api import DeepSeekClient
from .config import Config
from .config import load as load_config
from .config import save as save_config
from .output import Fragments, append_log, deliver, refresh, run_turn
from .profiles import Profile


@dataclass
class Request:
    """Единица очереди. Ведущий задаётся только для разового запуска группы через /team —
    в остальных случаях берётся профиль, активный на момент отправки."""

    content: str
    lead: Profile | None = None
    screen: screens_mod.Screen | None = None  # ввод с рабочего экрана уходит в него же


@dataclass
class State:
    config: Config
    client: DeepSeekClient | None
    model: str
    profile: Profile
    app: Application | None = None
    screens: list[screens_mod.Screen] = field(default_factory=lambda: [screens_mod.main_screen()])
    active: int = 0
    queue: asyncio.Queue[Request] = field(default_factory=asyncio.Queue)
    current_task: asyncio.Task | None = None
    busy: bool = False
    awaiting_key: bool = False
    awaiting_custom: str | None = None  # имя параметра, для которого ждём своё значение
    picker: picker_mod.Picker | None = None
    profile_dirty: bool = False  # параметры меняли, но профиль не сохранён
    known_models: list[str] = field(default_factory=lambda: list(api.FALLBACK_MODELS))
    journal_warned: bool = False
    # Расход агентов, которых в дереве экранов больше нет: собеседника, смещённого сменой
    # профиля, и экспертов закрытой группы. Сеанс — это запуск процесса, а не жизнь одного
    # собеседника: деньги за прежний разговор списаны и никуда не делись оттого, что человек
    # сменил профиль. Без копилки итог падал бы посреди работы почти до нуля — то есть врал.
    retired_usage: dict[str, int] = field(default_factory=dict)
    # Деньги за сеанс копим по ходу, а не считаем задним числом по накопленным токенам:
    # тариф зависит от модели и от часа, а модель меняется на лету. Пересчёт по текущей
    # модели врал втрое — расход, сделанный на `pro`, дешевел от одной лишь команды /model.
    session_cost: float = 0.0
    # Был ли хоть один обмен моделью с известным тарифом. Нужен, чтобы отличить «бесплатно»
    # от «тариф неизвестен»: ноль на экране читается как «денег не потрачено».
    session_cost_known: bool = True
    store_warned: bool = False  # о сбое записи разговора говорим один раз за сеанс
    input_buffer: Any = None  # буфер строки ввода: профиль подставляет в него заготовку
    # Ещё не показанные заготовки профиля. Очередь живёт в состоянии, а не в профиле: профиль
    # — это описание, одинаковое для всех запусков, а очередь расходуется по ходу разговора,
    # и хранить расходуемое в описании значило бы менять описание на лету.
    prefill_queue: list[str] = field(default_factory=list)
    # Мышь включена всегда: клики и выделение текста уживаются, если не просить у терминала
    # отслеживание перетаскивания (см. click_only_mouse). Команда /mouse оставлена аварийным
    # выходом для терминала, который так не умеет.
    mouse_enabled: bool = True
    # Куда пишется главный разговор между запусками. Заводится в `_main`, а НЕ здесь:
    # конструктор состояния зовут проверки напрямую, и любое состояние начало бы читать и
    # писать в каталог состояния пользователя.
    store: memory.SessionStore | None = None
    # Сколько пар легло в главный разговор с прошлого захода архивариуса.
    since_archive: int = 0
    # Фоновая служба архивариуса: идущий заход, счёт отказов и признак «о сбое уже сказали».
    # Одним полем, а не россыпью: скелет у фоновых служб общий и живёт в `background`, и
    # разложить его части по отдельным полям состояния значило бы собирать их заново в каждой
    # службе — со своими именами и своими расхождениями.
    # Имя берём готовое из самого архивариуса, а не пишем строкой второй раз: под ним он уже
    # известен журналу прогонов, и два написания одного имени однажды разъехались бы.
    архивариус: background.Служба = field(default_factory=lambda: background.Служба(archivist.AGENT_NAME))
    # Фоновая служба сжимателя — та же по устройству, что у архивариуса, и заведена тем же
    # скелетом. Имя берём готовое из самого сжимателя: под ним он известен журналу прогонов,
    # и второе написание одного имени однажды разъехалось бы с первым.
    сжиматель: background.Служба = field(default_factory=lambda: background.Служба(compact.ИМЯ_СЛУЖБЫ))
    # Счёт занятости контекста для полоски над строкой ввода. Заводится в `_main` вместе с
    # раскладкой, а не здесь: его незачем иметь состоянию, собранному проверками без окна.
    # Держим его на состоянии, потому что сбрасывать запомненную постоянную часть приходится
    # оттуда, где меняются глобальные факты, — они живут на диске, отпечаток памяти их не
    # видит, а сторожить их опросом значило бы ходить на диск на каждую букву.
    занятость: Any = None
    # Сколько пар файла сессии осталось за спиной К НАЧАЛУ работы с этим файлом: пары до
    # границы прежней выжимки и пары, не поместившиеся в порог при подъёме. Дальше граница
    # разговора считается от него плюс пройденное агентом (`compact.граница_разговора`).
    #
    # Число живёт здесь, а не в агенте: оно про ФАЙЛ сессии, а файл принадлежит состоянию —
    # его меняют `/clear` и смена профиля. Агент про файл не знает и знать не должен.
    база_границы: int = 0

    @property
    def main(self) -> screens_mod.Screen:
        """Экран пользователя: сюда идут команды, вопросы и сводка группы."""
        return self.screens[0]

    @property
    def screen(self) -> screens_mod.Screen:
        """Показываемый сейчас экран — он же и прокручивается."""
        return self.screens[self.active if 0 <= self.active < len(self.screens) else 0]

    @property
    def focus(self) -> screens_mod.Screen:
        """Куда писать вывод команд: на рабочем экране — в него, на экране агента (он только
        для чтения) — в главный, иначе сообщение осталось бы незамеченным."""
        current = self.screen
        return current if current.interactive else self.main

    @property
    def log(self) -> Fragments:
        return self.main.first.log

    @property
    def main_agent(self) -> Agent:
        """Собеседник главного экрана — тот, с кем разговаривает пользователь."""
        return self.main.first.agent

    def agents(self) -> list[Agent]:
        """Все поднятые собеседники сеанса, каждый по одному разу.

        Агентов ищем там, где они живут, — в панелях экранов: собственного списка агентов
        состояние не ведёт, и второй список разошёлся бы с панелями на первом же экране,
        заведённом мимо него. Собеседник главного экрана попадает сюда тем же обходом: его
        панель — первая панель первого экрана.

        Одного и того же агента отдаём один раз: панель может показывать чужого собеседника,
        и сложенный дважды расход выглядел бы ростом вдвое там, где ничего не росло.
        """
        агенты: list[Agent] = []
        for screen in self.screens:
            for pane in screen.panes:
                if pane.agent is not None and not any(pane.agent is уже for уже in агенты):
                    агенты.append(pane.agent)
        return агенты

    def retire(self, агенты: list[Agent]) -> None:
        """Проводить агентов: их расход уходит в копилку выбывших.

        Звать ТОЛЬКО там, где агент действительно покидает дерево экранов, и до того, как
        ссылка на него потеряна. Проводи живого — и его расход посчитался бы дважды: раз в
        копилке, раз при обходе панелей."""
        for агент in агенты:
            self.retired_usage = tokens.add_usage(self.retired_usage, агент.session_usage)

    def session_usage_total(self) -> dict[str, int]:
        """Расход за сеанс по ВСЕМ поднятым агентам, одной суммой.

        Считать по одному главному агенту нельзя: группа экспертов, цепочка способов и
        рабочие экраны ходят к модели своими агентами, и их обмены оплачены из того же
        кошелька. Итог, показывающий один главный разговор, занижал бы расход ровно в тот
        день, когда он вырос, — когда человек запустил четыре способа сразу.
        """
        живые = [agent.session_usage for agent in self.agents()]
        return tokens.total_usage([self.retired_usage, *живые])

    def __post_init__(self) -> None:
        # Панель главного экрана заводит `screens.main_screen()`, профиля у неё нет, а
        # собеседник нужен: главный разговор ведёт он. Заводим здесь, а не в `_main`, чтобы
        # у любого состояния — в том числе собранного проверками — главный агент был на месте.
        self.main.first.agent = Agent(screens_mod.MAIN_KEY, self.profile, facts=user_facts)


def switch_screen(state: State, index: int) -> None:
    if not 0 <= index < len(state.screens) or index == state.active:
        return
    state.active = index
    screen = state.screen
    for pane in screen.panes:
        pane.autoscroll = True  # переключились — показываем свежий конец ленты
    if screen.interactive and screen.profile is not None:
        # набранное пользователем не затираем: заготовка подставляется только в пустую строку
        apply_prefill(state, screen.profile, only_if_empty=True)
    refresh(state)


def switch_pane(state: State, index: int) -> None:
    """Панели внутри экрана перебираются по кругу: их немного, и так не надо целиться."""
    screen = state.screen
    if len(screen.panes) < 2:
        return
    screen.active_pane = index % len(screen.panes)
    screen.pane.autoscroll = True
    refresh(state)


def toggle_zoom(state: State) -> None:
    """Развернуть активную панель на весь экран и обратно: в сетке ответ читается по
    диагонали, а вчитаться иногда нужно."""
    screen = state.screen
    if len(screen.panes) < 2:
        return
    screen.zoomed = not screen.zoomed
    refresh(state)


def drop_agent_screens(state: State) -> None:
    """Экраны агентов и рабочие экраны живут ровно столько, сколько профиль, который их
    завёл: сменился профиль — прежние ленты уже не о чем.

    Расход экспертов провожаем в копилку прежде, чем закрыть их экраны: ленты не о чем, а
    деньги за их ответы заплачены."""
    выбывают = [pane.agent for screen in state.screens[1:] for pane in screen.panes if pane.agent is not None]
    state.retire(выбывают)
    del state.screens[1:]
    state.active = 0


# ─────────────────────────────── память разговора ───────────────────────────────


def user_facts() -> list[str]:
    """Глобальные факты о человеке — для системной инструкции ГЛАВНОГО разговора.

    Отдаётся вызываемым, а не готовым списком: факты меняются по ходу сеанса — их кладёт
    `/remember` и выписывает архивариус, — и список, снятый при создании агента, отстал бы
    от них к первому же запросу.

    Получают их только собеседник главного экрана. Исполнителям группы, шагам цепочки и
    пакетному наряду нужна их роль, а не любимый цвет пользователя: наряд ещё и обязан быть
    воспроизводимым, а факты о человеке меняются между прогонами.

    Предупреждения о попорченных файлах здесь отбрасываются намеренно: сборка запроса — не
    то место, где о них говорить (она молчалива и идёт на каждый вопрос), их показывает
    `/memory`, куда человек за памятью и приходит."""
    факты, _ = memory.load_facts()
    return факты


def open_new_session(state: State) -> None:
    """Начать писать в новый файл разговора — прежний остаётся на диске нетронутым.

    Именно так устроено «забудь»: файл не стирается, а закрывается. Стирай мы файл, любая
    ошибка человека («не то очистил») была бы необратимой, а так прошлый разговор лежит
    в каталоге состояния и читается глазами.

    Файл заводится СРАЗУ, пустым, а не при первой записи. Иначе «забудь» переживало бы
    перезапуск только вместе с обменом: очистил, вышел не сказав ни слова — и следующий
    запуск нашёл бы самым свежим прежний файл и поднял ровно то, что человек стёр. Пустой
    файл в каталоге состояния стоит ничего, а необъяснимое воскрешение стёртого разговора
    стоит доверия ко всей памяти."""
    state.store = memory.SessionStore(memory.new_session(Path.cwd(), state.profile.name))
    ошибка = state.store.touch()
    if ошибка:
        append_log(state, ui.error_fragments(ошибка))
    state.main_agent.set_store(state.store)
    # Файл новый и пустой: за спиной нет ни одной пары. Не обнули мы границу — выжимка,
    # собранная в новом разговоре, унесла бы в файл число пройденных пар от разговора,
    # который человек только что стёр, и следующий запуск начал бы новый разговор с середины.
    state.база_границы = 0


def забыть_счёт_занятости(state: State) -> None:
    """Сбросить запомненную постоянную часть полоски занятости.

    Зовётся там и только там, где изменились ГЛОБАЛЬНЫЕ ФАКТЫ: они живут на диске, уходят в
    системную часть каждого запроса и потому меняют вес — а отпечаток, по которому полоска
    решает пересчитывать ли себя, их не видит и видеть не может. Сторожить факты опросом
    значило бы ходить на диск ровно затем, чтобы узнать, что ходить было незачем.

    Очистку разговора сюда не заводим: она меняет длину памяти, а длина в отпечатке есть, и
    полоска пересчитается сама. Лишний вызов был бы вторым местом одного решения."""
    if state.занятость is not None:
        state.занятость.сбросить()


def restore_conversation(state: State) -> None:
    """Поднять последний разговор пары «рабочий каталог, профиль» и продолжить писать в него.

    Зовётся при запуске и при смене профиля — то есть каждый раз, когда у главного экрана
    меняется собеседник. Ключ — пара, а не один каталог: в инструменте уже действует правило
    «сменился профиль, значит другой собеседник, память не переносится», и ключ по одному
    каталогу молча отменил бы его.

    Продолжаем НАЙДЕННУЮ сессию, а не заводим новую: иначе каждый запуск оставлял бы на
    диске по файлу, а «продолжить последний разговор» находило бы вчерашний огрызок.

    Реплики в ленту не печатаются — только строка отчёта: экран остаётся чистым, память при
    этом полная. Чистый запуск не сообщает ничего: разговора не было, и говорить не о чем.

    Сколько пар поднять, решает ТОТ ЖЕ порог, что режет память посреди сеанса: пары берутся с
    конца, пока вес не упёрся в него, но не больше окна по числу пар. Правило одно на два
    случая намеренно — разведи их, и объяснить человеку, почему после перезапуска модель
    помнит больше или меньше, чем помнила минуту назад, было бы нечем.

    Пары, лежащие ДО границы выжимки, не поднимаются вовсе — ни пересказанные, ни забытые
    дословно. Иначе пара, забытая посреди вчерашнего сеанса, вернулась бы в разговор после
    перезапуска, и модель отвечала бы на реплику, которую сама же успела забыть.

    Собирать выжимку здесь, при запуске, инструмент не имеет права: разговоры, накопленные до
    появления сжатия, выжимки не имеют, и первый же запуск в такой папке начался бы с платного
    запроса к дорогой модели, которого человек не заказывал. Вместо этого — честная строка и
    предложение команды."""
    state.база_границы = 0
    if not state.profile.keep_history:
        # Профиль без истории не копит разговор и не пишет его на диск (`Agent.remember`).
        # Поднимать ему что-то значило бы отчитаться о памяти, которой не будет ни в одном
        # запросе. Хранилище всё же открываем: профиль сменят, а место для записи должно
        # быть на своём месте.
        open_new_session(state)
        return
    каталог = Path.cwd()
    путь = memory.latest_session(каталог, state.profile.name)
    if путь is None:
        open_new_session(state)
        return
    агент = state.main_agent
    # Окно при чтении НЕ накладываем: сколько пар поднять, решают граница выжимки и порог
    # сжатия вместе с окном, а для этого нужны все пары файла. Обрежь чтение окном — и пары,
    # отсечённые границей, съели бы место поднимаемых: разговор начался бы с середины.
    восстановленное = memory.read_session(
        путь, window=0, system_fp=memory.fingerprint(state.profile.system)
    )
    выжимка, жалобы = memory.load_summary(путь)
    for предупреждение in [*восстановленное.warnings, *жалобы]:
        append_log(state, ui.error_fragments(предупреждение))
    # Сверка границы с числом пар в файле стоит ИМЕННО ЗДЕСЬ: чтение выжимки о разговоре не
    # знает, чтение разговора не знает о выжимке, и оба числа сходятся только в этой точке.
    # Это единственная порча выжимки, которая разбирается без ошибки и при этом опасна: она
    # молча съела бы поднимаемые пары, и разговор начался бы с середины без предупреждения.
    if выжимка is not None and выжимка.граница > восстановленное.saved_pairs:
        append_log(
            state,
            ui.error_fragments(
                f"выжимка испорчена: в ней записана граница {выжимка.граница} пар, "
                f"а в разговоре их {восстановленное.saved_pairs} — разговор поднят без пересказа"
            ),
        )
        выжимка = None
    граница = выжимка.граница if выжимка is not None else 0
    остаток = восстановленное.pairs[граница:]
    # Выжимку ставим ДО подбора пар: её блок уезжает в системную часть запроса и весит вместе
    # с ним. Подбери мы пары раньше — в память попало бы больше, чем влезает в порог на самом
    # деле. Отвергнутая по отпечатку выжимка в вес не входит, и это тот же `_блок_выжимки`,
    # что решает подстановку, — второго места принятия решения нет.
    if выжимка is not None:
        агент.запомнить_выжимку(выжимка)
    сколько = агент.подобрать_с_конца(остаток, facts=агент.блок_фактов(), model=state.model)
    окно = state.profile.history_window
    if окно > 0:
        сколько = min(сколько, окно)
    поднимаемые = остаток[len(остаток) - сколько :] if сколько else []
    агент.restore(поднимаемые)
    # Граница разговора = пар в файле минус пар в памяти. Инвариант держится потому, что
    # память всегда хранит ХВОСТ файла: пары ложатся в файл и в память одним вызовом, а
    # уходят из памяти только со старого края. Отсюда и число: всё, что не поднято, — за
    # спиной, включая пары, не поместившиеся в порог.
    state.база_границы = восстановленное.saved_pairs - len(поднимаемые)
    state.store = memory.SessionStore(путь)
    агент.set_store(state.store)
    if поднимаемые:
        # Числа выжимки уходят в ТУ ЖЕ строку, а не в соседнюю. Отчёт о восстановлении читают
        # один раз, в первую секунду запуска, и разбитый надвое он читается как два разных
        # события, хотя событие одно: вот столько разговора у модели дословно, вот столько —
        # пересказом, вот столько нет нигде.
        append_log(
            state,
            ui.restored_fragments(
                len(поднимаемые),
                восстановленное.saved_pairs,
                восстановленное.last_ts,
                за_выжимкой=выжимка.граница if выжимка is not None else 0,
                забыто_дословно=выжимка.забыто_дословно if выжимка is not None else 0,
                снято_пунктов=выжимка.снято_пунктов if выжимка is not None else 0,
            ),
        )
    if агент.выжимка_отвергнута():
        # Молча пропавший кусок памяти человек отлаживает как «модель отвечает не на то».
        # Сама выжимка при этом сохранена: собрана она за деньги, а инструкцию могут вернуть.
        append_log(
            state,
            ui.system_fragments(
                "выжимка собрана под другой системной инструкцией — в запрос она не пойдёт"
            ),
        )
    # Сколько пар не поднято В ПАМЯТЬ и не покрыто пересказом. Считается ВСЕГДА, а не только
    # при отсутствующей выжимке: с выжимкой этот случай не только возможен, но и обычен —
    # сжатие выключилось на трёх отказах, обрезка резала дословно, а файл выжимки остался со
    # старыми числами. Прежнее условие молчало ровно тогда, и двадцать пар исчезали из отчёта
    # без единого слова: в ленте стояло «поднято 10 из 40, за выжимкой 10 пар», и куда делись
    # остальные двадцать, узнать было неоткуда.
    #
    # Собирать пересказ молча нельзя — это списанные деньги за запрос, которого человек не
    # заказывал. Поэтому называем команду и оставляем решение за ним.
    за_выжимкой = выжимка.граница if выжимка is not None else 0
    непересказанных = восстановленное.saved_pairs - за_выжимкой - len(поднимаемые)
    if непересказанных > 0:
        append_log(
            state,
            ui.system_fragments(
                f"{непересказанных} пар не поднято и не пересказано "
                "— /compact соберёт по ним выжимку"
            ),
        )
    if восстановленное.fingerprint_changed:
        # Молча продолжать разговор под изменённой инструкцией нельзя: он выглядел бы
        # цельным, не будучи им, — прежние ответы рождены другой инструкцией.
        append_log(
            state,
            ui.system_fragments(
                "инструкция профиля изменилась с прошлого запуска — прежние ответы получены под другой"
            ),
        )


# ─────────────────────────────── профиль и запрос ───────────────────────────────


def switch_profile(state: State, name: str) -> None:
    profile, warnings = profiles.load(name)
    state.profile = profile
    state.profile_dirty = False
    state.config.profile = profile.name
    save_config(state.config)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    # История, набранная под прежней инструкцией, исказила бы следующий ответ. Забыть её
    # мало: у собеседника главного экрана сменились и инструкция, и параметры — значит это
    # уже другой собеседник. Смотрим на прежнюю память ДО замены, иначе сообщение об очистке
    # появилось бы и при первой смене профиля на пустом разговоре.
    had_history = bool(state.main_agent.history())
    # Прежнего собеседника прямо объявляем забывшим разговор, а не просто выбрасываем ссылку
    # на него: его обмен мог ещё идти, и в своём `finally` он дописал бы пару в память,
    # которой мы уже не пользуемся. Смена поколения делает это дописывание невозможным —
    # брошенный агент не оставит после себя ни строчки.
    state.main.first.agent.forget()
    # Расход уходящего собеседника провожаем в копилку до замены: сеанс продолжается, и
    # потраченное им не должно исчезнуть с экрана вместе с ним.
    state.retire([state.main.first.agent])
    state.main.first.agent = Agent(screens_mod.MAIN_KEY, profile, facts=user_facts)
    if had_history:
        append_log(state, ui.system_fragments("история диалога очищена — профиль сменился"))
    if len(state.screens) > 1:
        drop_agent_screens(state)
        append_log(state, ui.system_fragments("экраны прежней группы закрыты"))
    source = str(profile.source) if profile.source else "встроенный"
    append_log(state, ui.system_fragments(f"профиль: {profile.name} ({source})"))
    # Разговор принадлежит паре «каталог, профиль», значит смена профиля — это переход в
    # другой разговор, а не только смена инструкции. Прежняя сессия остаётся на диске и
    # поднимется обратно, когда человек вернётся к тому профилю.
    restore_conversation(state)
    if not profile.keep_history:
        append_log(state, ui.system_fragments("в этом профиле каждый запрос уходит без истории"))
    if profile.agents:
        append_log(state, ui.team_list_fragments(profile.name, profile.agents))
    if profile.screens:
        open_work_screens(state, profile)
        return
    if profile.methods:
        open_method_screens(state, profile)
    apply_prefill(state, profile)


def apply_prefill(state: State, profile: Profile, *, only_if_empty: bool = False) -> None:
    """Заготовки профиля кладём в строку ввода, а не отправляем сами: человек видит текст,
    может его поправить и отправляет сам — Enter'ом.

    Заготовок может быть несколько (`prefills`): тогда следующая встаёт в строку ввода, как
    только отправлена предыдущая, — прогон повторяется в точности, и на показе не приходится
    ничего набирать. Одиночная `prefill` — та же очередь длиной в один вопрос, отдельного
    пути для неё не заводим."""
    if state.input_buffer is None:
        return
    if only_if_empty and state.input_buffer.text.strip():
        return
    очередь = list(profile.prefills) or ([profile.prefill.strip()] if profile.prefill else [])
    if not очередь:
        return
    state.prefill_queue = очередь
    сказать = (
        "заготовка вопроса подставлена в строку ввода — Enter отправит её"
        if len(очередь) == 1
        else f"вопросы профиля ({len(очередь)}) пойдут по очереди — Enter отправляет и подставляет следующий"
    )
    append_log(state, ui.system_fragments(сказать))
    next_prefill(state)


def next_prefill(state: State) -> None:
    """Поставить в строку ввода следующий вопрос очереди, если он есть.

    Текст, набранный человеком, не затираем: он мог начать печатать своё, пока шёл ответ, и
    подстановка поверх стёрла бы работу. Очередь тогда просто ждёт — она про удобство, а не
    про власть над строкой ввода."""
    if not state.prefill_queue or state.input_buffer is None:
        return
    if state.input_buffer.text.strip():
        return
    текст = state.prefill_queue.pop(0)
    state.input_buffer.text = текст
    state.input_buffer.cursor_position = len(текст)
    if state.app is not None:
        state.app.invalidate()


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
        # Панель рабочего экрана делает `Screen.__post_init__`, а собеседника — сама панель
        # по своему профилю: у каждого шага приёма своя ветка разговора, значит и своя память.
        screen = screens_mod.Screen(key=name, title=name, profile=step, interactive=True)
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


# ─────────────────────────────── очередь запросов ───────────────────────────────


async def worker(state: State) -> None:
    while True:
        request = await state.queue.get()
        lead = request.lead or state.profile
        state.busy = True
        # Чем занят запрос, помним отдельно: по одному лишь возвращённому значению не
        # отличить итог оркестратора от обмена, который сам себя уже показал.
        kind = "обмен"
        if request.screen is not None and request.screen.profile is not None:
            pane = request.screen.first
            task = asyncio.create_task(run_turn(state, pane.agent, request.content, pane=pane))
        elif lead.methods:
            kind = "набор"
            task = asyncio.create_task(methods_mod.run_all(state, request.content, lead))
        elif lead.agents:
            kind = "группа"
            task = asyncio.create_task(team.run(state, request.content, lead))
        else:
            # Вопрос в память не дописываем: памятью владеет агент, и кладёт он туда только
            # отвеченную пару — иначе после сетевого сбоя в истории остался бы вопрос,
            # на который никто не отвечал.
            task = asyncio.create_task(run_turn(state, state.main_agent, request.content, pane=state.main.first))
        state.current_task = task
        # Сколько пар лежало в главном разговоре ДО обмена. Считать обмены по их исходу
        # нельзя: обмен главного экрана, итог группы и итог набора способов кладут пару
        # каждый по-своему, а отменённый и упавший — не кладут вовсе. Рост памяти отвечает
        # на нужный вопрос прямо: разговору, который читает архивариус, прибыло.
        было_пар = len(state.main_agent.history())
        try:
            result = await task
            # Единственное место, где итог оркестратора попадает в главный экран. Сами
            # оркестраторы решают, ЧТО считать итогом, но не куда его девать: набор способов
            # состоит из тех же оркестраторов, и пиши каждый из них наверх сам — четыре
            # способа писали бы вперемешку и в случайном порядке.
            if kind == "набор":
                deliver(state, request.content, result)
            elif kind == "группа" and result is not None:
                deliver(state, request.content, [result])
            if len(state.main_agent.history()) > было_пар:
                state.since_archive += 1
            # Заход заводится ОТДЕЛЬНОЙ задачей и никем не ожидается: архивариус не имеет
            # права задержать ни следующий запрос из очереди, ни ввод человека.
            archivist.start(state)
            # Сжиматель — там же и по той же причине. Заводится он не по счёту обменов, а по
            # подходу к ближайшему ограничителю обрезки: своей мерки «пора» у сжатия нет, оно
            # подбирает то, что решила выбросить обрезка.
            compact.start(state)
        except asyncio.CancelledError:
            append_log(state, ui.system_fragments("запрос отменён"))
        finally:
            state.current_task = None
            state.busy = False
            state.queue.task_done()


# ─────────────────────────────── команды ───────────────────────────────


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
        items.append(picker_mod.Item(label=name, hint="текущая" if name == state.model else "", payload=name))

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


def open_profile_picker(state: State) -> None:
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for name, source in profiles.available():
        if name == state.profile.name:
            marked = len(items)
        hint = str(source.parent) if source else "встроенный"
        items.append(picker_mod.Item(label=name, hint=hint, payload=name))

    def choose(payload: Any) -> None:
        state.picker = None
        switch_profile(state, str(payload))

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


def cmd_profile(state: State, arg: str) -> None:
    if not arg:
        open_profile_picker(state)
        return
    parts = arg.split(maxsplit=1)
    if parts[0] == "save":
        name = parts[1].strip() if len(parts) > 1 else state.profile.name
        state.profile.name = name
        try:
            path = profiles.save(state.profile)
        except OSError as exc:
            append_log(state, ui.error_fragments(f"не удалось сохранить профиль: {exc}"))
            return
        state.profile_dirty = False
        state.config.profile = name
        save_config(state.config)
        append_log(state, ui.system_fragments(f"профиль сохранён: {path}"))
        return
    switch_profile(state, parts[0])


def cmd_params(state: State) -> None:
    """Показываем значения и сразу даём их менять: список в логе выбирать нечем."""
    append_log(state, ui.params_fragments(state.profile.name, state.profile.params, state.profile.system))
    if state.profile_dirty:
        append_log(state, ui.hint_fragments("изменения не сохранены — /profile save <имя>"))
    open_param_picker(state)


def set_param(state: State, name: str, value: Any) -> None:
    spec = params_mod.SPECS[name]
    if value is params_mod.UNSET:
        state.profile.params.pop(name, None)
        append_log(state, ui.system_fragments(f"{spec.title}: параметр снят (умолчание API)"))
    else:
        state.profile.params[name] = value
        append_log(state, ui.system_fragments(f"{spec.title} = {params_mod.format_value(value)}"))
    state.profile_dirty = True
    reason = params_mod.inapplicable_reason(name, state.profile.params)
    if reason and value is not params_mod.UNSET:
        append_log(state, ui.hint_fragments(f"{spec.title} сейчас {reason}"))
    append_log(state, ui.hint_fragments("сохранить в профиль: /profile save <имя>"))


def open_value_picker(state: State, name: str) -> None:
    spec = params_mod.SPECS[name]
    current = state.profile.params.get(name)
    items: list[picker_mod.Item] = []
    marked: int | None = None
    for choice in spec.choices:
        payload = choice.value
        if (payload is params_mod.UNSET and current is None) or (payload is not params_mod.UNSET and payload == current):
            marked = len(items)
        items.append(picker_mod.Item(label=choice.label, hint=choice.hint, payload=payload))
    if spec.custom_hint:
        items.append(picker_mod.Item(label="ввести своё значение…", hint=spec.custom_hint, payload="__custom__"))

    description = spec.description
    reason = params_mod.inapplicable_reason(name, state.profile.params)
    if reason:
        description += f" — {reason}"

    def choose(payload: Any) -> None:
        state.picker = None
        if payload == "__custom__":
            state.awaiting_custom = name
            append_log(state, ui.system_fragments(f"{spec.title}: введите значение ({spec.custom_hint}), Enter — применить"))
            return
        set_param(state, name, payload)

    state.picker = picker_mod.Picker(
        title=f"/set {spec.title}",
        description=description,
        items=items,
        on_choose=choose,
        index=marked or 0,
        marked=marked,
    )
    refresh(state)


def open_param_picker(state: State) -> None:
    items: list[picker_mod.Item] = []
    for name in params_mod.ORDER:
        spec = params_mod.SPECS[name]
        value = params_mod.format_value(state.profile.params.get(name))
        items.append(picker_mod.Item(label=spec.title, hint=f"сейчас: {value}", payload=name))

    def choose(payload: Any) -> None:
        state.picker = None
        open_value_picker(state, str(payload))

    state.picker = picker_mod.Picker(
        title="/set — параметры генерации",
        description="какой параметр меняем",
        items=items,
        on_choose=choose,
    )
    refresh(state)


def cmd_set(state: State, arg: str) -> None:
    name = arg.strip()
    if not name:
        open_param_picker(state)
        return
    if name not in params_mod.SPECS:
        append_log(state, ui.error_fragments(f"нет такого параметра: {name}"))
        append_log(state, ui.hint_fragments("доступны: " + ", ".join(params_mod.ORDER)))
        return
    open_value_picker(state, name)


def apply_custom_value(state: State, raw: str) -> None:
    name = state.awaiting_custom or ""
    state.awaiting_custom = None
    spec = params_mod.SPECS.get(name)
    if spec is None:
        return
    text = raw.strip()
    if not text:
        append_log(state, ui.hint_fragments("значение не введено, отменено"))
        return
    if spec.parse is None:
        append_log(state, ui.error_fragments(f"{spec.title}: своё значение не поддерживается"))
        return
    try:
        value = spec.parse(text)
    except ValueError as exc:
        append_log(state, ui.error_fragments(f"{spec.title}: {exc}"))
        return
    set_param(state, name, value)


def toggle_mouse(state: State) -> None:
    """Аварийный выход: вернуть мышь терминалу целиком.

    Обычно этого не требуется — клики и выделение текста уживаются (см. `click_only_mouse`).
    Команда оставлена на случай терминала, который отслеживание нажатий понимает, а выделение
    при нём всё равно отдаёт приложению.
    """
    state.mouse_enabled = not state.mouse_enabled
    if state.mouse_enabled:
        append_log(state, ui.system_fragments("мышь у harness: клики по вкладкам, панелям и строкам списка"))
    else:
        append_log(state, ui.system_fragments("мышь целиком у терминала: клики в harness не действуют"))
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


def cmd_tokens(state: State) -> None:
    """`/tokens` — снимок расхода: чем нагружен следующий запрос и во что обошёлся сеанс.

    Итог берём по всем агентам сеанса, а вес разговора — у собеседника главного экрана.
    Это не непоследовательность: платит человек за всех, кого поднял, а решает «пора ли
    звать /clear» по разговору, который ведёт сам. Возьми вес истории тоже по всем — и
    сумма памяти четырёх исполнителей, живущих ровно один вопрос, выдавала бы главный
    разговор за неподъёмный.

    Цену считает `tokens` по накопленному расходу, а не сложением цен обменов: у DeepSeek
    цена зависит от часа, и обмен, сделанный в дорогой час, дорог именно тогда. Сумма по
    накопленному — приближение, зато одно и то же число не пересчитывается двумя способами.
    """
    итог = state.session_usage_total()
    агент = state.main_agent
    append_log(
        state,
        ui.tokens_report_fragments(
            state.model,
            history=агент.history_tokens(),
            pairs=len(агент.history()) // 2,
            # Вес системной инструкции — часть каждого запроса, и в снимке без него занятое
            # окно выходило смехотворно малым: профиль дня кладёт в инструкцию документ на
            # миллион токенов, а снимок показывал «занято 1 354».
            system=tokens.count_text(агент.profile.system or ""),
            overhead=агент.overhead(state.model),
            restored=агент.restored_pairs,
            runs=sum(другой.runs for другой in state.agents()),
            usage=итог,
            budget=state.profile.budget_tokens,
            cost=tokens.format_price(state.session_cost if state.session_cost_known else None),
        ),
    )


def cmd_budget(state: State, arg: str) -> None:
    """`/budget [токены]` — предел веса запроса; 0 снимает предел.

    Почему это отдельная команда, а НЕ параметр `/set`. В параметрах генерации живут только
    ручки самого DeepSeek — правило проекта, заведённое затем, чтобы `/params` можно было
    сверять со страницей документации поставщика построчно. Предел веса запроса в запрос не
    уходит вовсе: это наша политика обрезки памяти перед отправкой. Положи его к параметрам —
    и человек искал бы его в документации DeepSeek, где такого поля нет и не будет.

    Значение кладётся в профиль, но на диск не пишется: `/budget` — прикидка на сеанс, а
    насовсем предел закрепляет `/profile save`. Поэтому здесь же поднимается признак
    «профиль изменён» — тот самый, по которому строка состояния предупреждает о
    несохранённых правках.
    """
    текст = arg.strip()
    if not текст:
        if state.profile.budget_tokens > 0:
            append_log(
                state,
                ui.system_fragments(
                    f"предел веса запроса профиля «{state.profile.name}»: "
                    f"{ui.format_exact(state.profile.budget_tokens)} токенов"
                ),
            )
        else:
            append_log(state, ui.system_fragments("предел веса запроса не задан — память режет только окно по парам"))
        append_log(state, ui.hint_fragments("задать: /budget 3000; снять: /budget 0"))
        return
    try:
        предел = int(текст)
    except ValueError:
        append_log(state, ui.error_fragments(f"«{текст}» — не число токенов; например: /budget 3000"))
        return
    if предел < 0:
        append_log(state, ui.error_fragments("предел не бывает отрицательным; 0 — без предела"))
        return
    # Признак «профиль изменён» поднимаем только на настоящей смене значения: повторный
    # `/budget 3000` ничего не менял, а строка состояния уверяла бы, что есть несохранённое.
    if предел != state.profile.budget_tokens:
        state.profile.budget_tokens = предел
        state.profile_dirty = True
    if предел == 0:
        append_log(state, ui.system_fragments("предел веса запроса снят — память режет только окно по парам"))
        return
    append_log(state, ui.system_fragments(f"предел веса запроса: {ui.format_exact(предел)} токенов"))
    if предел < tokens.BASE_OVERHEAD:
        # Ровно то же предупреждение, что даёт разбор профиля: в такой предел не влезает даже
        # пустой запрос — одна обёртка разговора весит больше. Узнавать об этом по поведению
        # («почему модель ничего не помнит?») человек не должен.
        append_log(
            state,
            ui.hint_fragments(
                f"это меньше веса пустого запроса ({tokens.BASE_OVERHEAD}) — "
                f"память будет обрезана до последней пары"
            ),
        )
    append_log(state, ui.hint_fragments("сохранить в профиль: /profile save <имя>"))


def сбросить_счёт_отказов(state: State) -> None:
    """Обнулить счёт отказов подряд у фоновых служб — при очистке разговора.

    Отказы считаются ПОДРЯД и означают «служба не работает прямо сейчас». Новый разговор —
    новая почва: тащить в него два отказа, случившиеся до очистки, значило бы выключить
    сжатие на третьей неудаче, две из которых относятся к разговору, которого больше нет.
    Архивариус сбрасывается заодно: его счёт `/clear` переживал с самого начала, и это была
    та же шероховатость, просто незамеченная.

    Признак «служба выключена» и признак «о сбое уже сказали» НЕ трогаем: и то и другое
    сказано человеку про СЕАНС, а не про разговор, — «выключено до конца сеанса» обязано
    значить именно это, иначе обещание в ленте перестаёт быть правдой.
    """
    for служба in (state.сжиматель, state.архивариус):
        служба.отказов_подряд = 0


def cmd_context(state: State) -> None:
    """Показать выжимку разговора ДОСЛОВНО, целиком, вместе с тремя числами и поколением.

    Это не удобство, а условие, при котором пересказ вообще допущен в запрос: обещание «что
    модель видела, человек может прочитать» держится на этой команде одной. Поэтому показ
    дословный — сокращённый пересказ пересказа отвечал бы на вопрос «примерно о чём выжимка»,
    а спрашивают ровно то, что ушло в модель.

    Три числа, а не одно: пройденное разговором не равно пересказанному, и теряется оно
    двумя разными способами — сжатие не успело (пары ушли дословно) либо выжимка переполнилась
    (сняты её старейшие пункты). Снятые считаются ПУНКТАМИ: пункт парам не сопоставлен — он
    мог родиться из одной пары, из пяти или из половины разговора, — и пересчитать их в пары
    значило бы выдумать число.
    """
    агент = state.main_agent
    выжимка = агент.выжимка()
    if выжимка is None:
        if агент.порог_сжатия() <= 0:
            append_log(state, ui.system_fragments("выжимки нет: сжатие выключено полем профиля compact_at"))
        else:
            append_log(state, ui.system_fragments("выжимки нет — разговор ещё не сжимали"))
        return
    заголовок = (
        f"выжимка разговора, сжатие {выжимка.поколение}-е: "
        f"за нею {выжимка.граница} пар, забыто дословно {выжимка.забыто_дословно}, "
        f"снято пунктов при переполнении {выжимка.снято_пунктов}"
    )
    append_log(state, ui.system_fragments(заголовок))
    for номер, пункт in enumerate(выжимка.пункты, 1):
        append_log(state, [("", f"  {номер}. {пункт}\n")])
    if агент.выжимка_отвергнута():
        append_log(
            state,
            ui.system_fragments("в запрос она сейчас не идёт: собрана под другой системной инструкцией"),
        )


def cmd_compact(state: State) -> None:
    """Сжать память по требованию человека — все пары, кроме последней.

    Долю от памяти команда не берёт: человек, отдавший её, просит минимальную память, а не
    часть от неё — доля потребовала бы от него знать, какую именно, и назначать её на глаз.
    Последняя пара остаётся по сквозному правилу: иначе агент забудет то, о чём его только
    что спросили.

    Заход идёт в фоне и ввод не блокирует — то же правило, что и у самостоятельного сжатия:
    задерживать человека ради вспомогательного механизма нельзя.

    Отказы называются вслух, а не проглатываются: команда, которая молча ничего не делает,
    отлаживается как поломка инструмента.
    """
    агент = state.main_agent
    if not агент.profile.keep_history:
        # Память такого профиля в запрос не идёт вовсе: пересказывать нечего, а запрос
        # сжимателя стоит денег на той же дорогой модели, что ведёт разговор.
        append_log(
            state,
            ui.error_fragments("этот профиль не хранит историю — сжимать нечего"),
        )
        return
    if агент.порог_сжатия() <= 0:
        append_log(
            state,
            ui.error_fragments("сжатие выключено полем профиля compact_at — включить: /set compact_at"),
        )
        return
    if state.сжиматель.выключена:
        append_log(
            state,
            ui.error_fragments("сжатие выключено до конца сеанса: подряд не удались три попытки"),
        )
        return
    if state.client is None:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return
    if compact.идёт(state):
        append_log(state, ui.system_fragments("сжатие уже идёт — второе не заводим"))
        return
    # Считаем и память, и хвост за границей выжимки. Смотри мы на одну память — команда
    # отказывала бы ровно в том случае, ради которого дочитывание и заведено: при запуске на
    # старой сессии в порог поднимается одна пара, лента обещает «остальное не пересказано —
    # /compact соберёт выжимку», а команда отвечает «сжимать нечего». Обещание, нарушенное
    # инструментом в следующую же секунду, хуже несделанного.
    if len(агент.history()) // 2 - 1 <= 0 and not compact.дочитать(state):
        append_log(
            state,
            ui.system_fragments("сжимать нечего: последняя пара не выбрасывается никогда"),
        )
        return
    append_log(state, ui.system_fragments("сжимаю разговор — ввод не заблокирован"))
    background.завести(state.сжиматель, compact.run(state, вручную=True))


def cmd_team(state: State, arg: str) -> None:
    """Разовый запуск группы. Без аргумента — показывает состав; с вопросом — задаёт его
    группе. Первым словом можно назвать профиль-ведущего: так группу поднимают, не уходя
    с обычного профиля."""
    text = arg.strip()
    lead = state.profile
    if text:
        parts = text.split(maxsplit=1)
        known = {name for name, _ in profiles.available()}
        if len(parts) > 1 and parts[0] in known:
            candidate, warnings = profiles.load(parts[0])
            for warning in warnings:
                append_log(state, ui.error_fragments(warning))
            if candidate.agents:
                lead, text = candidate, parts[1]
    if not lead.agents:
        append_log(state, ui.error_fragments(f"в профиле «{lead.name}» группа не задана"))
        append_log(state, ui.hint_fragments("группу задаёт поле agents профиля: /profile <имя-ведущего>"))
        return
    if not text:
        append_log(state, ui.team_list_fragments(lead.name, lead.agents))
        return
    if not state.config.is_authorized:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return
    append_log(state, ui.user_fragments(text))
    was_busy = state.busy
    state.queue.put_nowait(Request(content=text, lead=lead))
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()))


# ─────────────────────────────── список агентов ───────────────────────────────


@dataclass
class AgentRow:
    """Один агент в списке под строкой ввода: сам собеседник и адрес его панели.

    Адрес храним номерами экрана и панели, а не ссылкой на них: переход делают
    `switch_screen` и `switch_pane`, а они работают именно по номерам."""

    agent: Agent
    pane: screens_mod.Pane
    screen_index: int
    pane_index: int
    status: str
    occupation: str  # чем занят: начало вопроса, пока работает, иначе имя профиля


def collect_agents(state: State) -> list[AgentRow]:
    """Все поднятые агенты — в том же порядке, в каком идут экраны и панели на них.

    Порядок не косметика: строка списка и есть адрес агента, и переставь список агентов
    по-своему — номер строки перестал бы что-либо значить, а ↑/↓ водили бы не туда.

    Панель без собеседника пропускаем. Сейчас такой нет — панель заводит агента сама, как
    только у неё есть профиль, — но список не то место, где стоит падать: его открывают,
    когда с агентами уже что-то не так."""
    rows: list[AgentRow] = []
    for screen_index, screen in enumerate(state.screens):
        for pane_index, pane in enumerate(screen.panes):
            if pane.agent is None:
                continue
            rows.append(
                AgentRow(
                    agent=pane.agent,
                    pane=pane,
                    screen_index=screen_index,
                    pane_index=pane_index,
                    status=pane.status,
                    occupation=ui.agent_occupation(pane.status, pane.agent.task, pane.agent.profile.name),
                )
            )
    return rows


def agent_index(state: State, rows: list[AgentRow]) -> int:
    """Строка, на которой стоит пользователь: экран, который открыт, и панель, которая
    выбрана на нём. Не нашли — считаем первой: строка «вы здесь» в списке обязана быть."""
    current = state.screen.pane
    for index, row in enumerate(rows):
        if row.screen_index == state.active and row.pane is current:
            return index
    return 0


def goto_agent(state: State, index: int) -> None:
    """Перейти на экран и панель агента по номеру строки списка."""
    rows = collect_agents(state)
    if not 0 <= index < len(rows):
        return
    row = rows[index]
    switch_screen(state, row.screen_index)
    switch_pane(state, row.pane_index)
    refresh(state)


def show_agent_panel(state: State) -> bool:
    """Показывать ли список агентов. Пока агент один, списка нет: строка «main» в одиночестве
    ничего не сообщает и только съедает высоту экрана."""
    return len(collect_agents(state)) > 1


def step_agent(state: State, delta: int) -> None:
    """Соседний агент по списку, по кругу. Клавиши ↑ и ↓ ведут ровно туда же, куда щелчок
    мышью по строке: два пути к одному месту, а не два разных поведения."""
    rows = collect_agents(state)
    if len(rows) < 2:
        return
    goto_agent(state, (agent_index(state, rows) + delta) % len(rows))


async def handle_command(text: str, state: State) -> bool:
    parts = text.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""
    if cmd in ("/exit", "/quit"):
        return True
    if cmd == "/help":
        append_log(state, ui.help_fragments())
    elif cmd == "/auth":
        cmd_auth(state)
    elif cmd == "/model":
        await cmd_model(state, arg)
    elif cmd == "/profile":
        cmd_profile(state, arg)
    elif cmd == "/params":
        cmd_params(state)
    elif cmd == "/set":
        cmd_set(state, arg)
    elif cmd == "/system":
        # на панели исполнителя показываем его инструкцию: именно её там свернули до строки
        source = state.screen.pane.profile or state.profile
        append_log(state, ui.system_prompt_fragments(source.name, source.system))
    elif cmd == "/team":
        cmd_team(state, arg)
    elif cmd == "/mouse":
        toggle_mouse(state)
    elif cmd == "/clear":
        # Идущий заход сжимателя снимаем ПЕРВЫМ делом. Своего итога он после этого не
        # применит и без отмены — применение сверяется с поколением памяти, — но платить за
        # запрос, чей итог заведомо выброшен, незачем.
        background.отменить(state.сжиматель)
        # Выжимку и заготовку убирает сам агент (`forget`): оставь их — и человек, стёрший
        # разговор, продолжил бы говорить с моделью, которая помнит его пересказ.
        state.main_agent.forget()
        сбросить_счёт_отказов(state)
        # Мало забыть разговор в памяти: не открой мы новую сессию, следующий запуск поднял
        # бы очищенное обратно с диска — издевательство, а не очистка.
        open_new_session(state)
        append_log(state, ui.system_fragments("история очищена — начат новый разговор"))
    elif cmd == "/context":
        cmd_context(state)
    elif cmd == "/compact":
        cmd_compact(state)
    elif cmd == "/remember":
        cmd_remember(state, arg)
    elif cmd == "/memory":
        cmd_memory(state, arg)
    elif cmd == "/forget":
        cmd_forget(state, arg)
    elif cmd == "/tokens":
        cmd_tokens(state)
    elif cmd == "/budget":
        cmd_budget(state, arg)
    else:
        append_log(state, ui.error_fragments(f"неизвестная команда: {cmd} (см. /help)"))
    return False


async def handle_submit(raw_text: str, state: State) -> None:
    if state.awaiting_key:
        state.awaiting_key = False
        await do_auth(raw_text, state)
        return

    if state.awaiting_custom:
        apply_custom_value(state, raw_text)
        return

    text = raw_text.strip()
    if not text:
        return

    if text.startswith("/"):
        if await handle_command(text, state):
            if state.app is not None:
                state.app.exit()
        return

    if not state.config.is_authorized:
        append_log(state, ui.hint_fragments("сначала авторизуйтесь: /auth"))
        return

    screen = state.screen if state.screen.interactive and state.screen is not state.main else None
    append_log(state, ui.user_fragments(text), screen)
    was_busy = state.busy
    state.queue.put_nowait(Request(content=text, screen=screen))
    if was_busy:
        append_log(state, ui.queued_fragments(state.queue.qsize()), screen)
    # Следующий вопрос очереди встаёт в строку ввода сразу, не дожидаясь ответа: человек
    # видит, что будет спрошено, и волен это поправить или стереть, пока модель думает.
    next_prefill(state)


# ─────────────────────────────── меню команд ───────────────────────────────


class HarnessCompleter(Completer):
    """Список команд появляется сразу по вводу «/», без Enter. Состав зависит от того,
    авторизован ли пользователь: пока ключа нет, всё остальное всё равно не сработает."""

    def __init__(self, state: State) -> None:
        self.state = state

    def get_completions(self, document, complete_event):  # noqa: ANN001, ANN201
        if self.state.awaiting_key or self.state.awaiting_custom or self.state.picker is not None:
            return
        text = document.text_before_cursor
        if not text.startswith("/"):
            return
        parts = text.split(" ")
        word = parts[-1]
        if len(parts) == 1:
            for name, arg, description in ui.visible_commands(self.state.config.is_authorized):
                if name.startswith(word):
                    display = f"{name} {arg}".strip()
                    yield Completion(name, start_position=-len(word), display=display, display_meta=description)
            return
        command = parts[0].lower()
        if command == "/set" and len(parts) == 2:
            for name in params_mod.ORDER:
                if name.startswith(word):
                    yield Completion(
                        name, start_position=-len(word), display=name, display_meta=params_mod.SPECS[name].description
                    )
        elif command == "/profile" and len(parts) == 2:
            for name, source in profiles.available():
                if name.startswith(word):
                    meta = str(source) if source else "встроенный"
                    yield Completion(name, start_position=-len(word), display=name, display_meta=meta)
            if "save".startswith(word):
                yield Completion(
                    "save", start_position=-len(word), display="save", display_meta="сохранить текущие параметры"
                )
        elif command == "/model" and len(parts) == 2:
            for name in self.state.known_models:
                if name.startswith(word):
                    yield Completion(name, start_position=-len(word), display=name, display_meta="модель DeepSeek")


class LogWindow(Window):
    """Window с логом: колесо мыши отключает автопрокрутку (даём пролистать историю)."""

    def __init__(self, *args, on_manual_scroll, **kwargs) -> None:  # noqa: ANN001
        super().__init__(*args, **kwargs)
        self._on_manual_scroll = on_manual_scroll

    def _mouse_handler(self, mouse_event: MouseEvent):
        if mouse_event.event_type in (MouseEventType.SCROLL_UP, MouseEventType.SCROLL_DOWN):
            self._on_manual_scroll()
        return super()._mouse_handler(mouse_event)


def pane_columns(count: int, width: int) -> int:
    """Сколько панелей ставить в ряд. Пять экспертов в пять колонок на обычном окне дают по
    двадцать знаков на колонку — читать невозможно, поэтому решает ширина, а не число."""
    if count <= 1 or width < 100:
        return 1
    return 2 if width < 170 else 3


def app_output():  # noqa: ANN201 — тип вывода приходит из prompt_toolkit
    from prompt_toolkit.application.current import get_app_session

    return get_app_session().output


def click_only_mouse(output) -> None:  # noqa: ANN001
    """Просить у терминала только нажатия мыши, без отслеживания перетаскивания.

    prompt_toolkit включает мышь одним куском: `1000h` (нажатия), `1003h` (любое движение),
    `1015h` и `1006h` (расширенные ответы). Губителен здесь `1003h` — пока он поднят, терминал
    отдаёт приложению и протяжку тоже, а значит выделить текст мышью нельзя. Отсюда и родился
    прежний переключатель «или клики, или копирование».

    Выбор ложный. Оставив только `1000h` и `1006h`, приложение получает клики по вкладкам,
    панелям и строкам списка, а протяжка остаётся терминалу — выделение и копирование работают
    как обычно, без единой команды.
    """
    if getattr(output, "_click_only", False):
        return
    output._click_only = True

    def enable() -> None:
        output.write_raw("\x1b[?1000h\x1b[?1006h")

    def disable() -> None:
        output.write_raw("\x1b[?1006l\x1b[?1000l")

    output.enable_mouse_support = enable
    output.disable_mouse_support = disable


def build_app(state: State) -> Application:
    windows: dict[int, LogWindow] = {}

    def pane_window(pane: screens_mod.Pane) -> LogWindow:
        """Окно панели живёт столько же, сколько сама панель: в нём хранится прокрутка."""
        existing = windows.get(id(pane))
        if existing is not None:
            return existing
        control = FormattedTextControl(text=lambda: pane.visible_log(), show_cursor=False)
        window = LogWindow(
            content=control,
            wrap_lines=True,
            always_hide_cursor=True,
            height=Dimension(weight=1),
            on_manual_scroll=lambda: setattr(pane, "autoscroll", False),
        )
        control.get_cursor_position = lambda: (
            Point(x=0, y=pane.visible_lines()) if pane.autoscroll else Point(x=0, y=window.vertical_scroll)
        )
        windows[id(pane)] = window
        return window

    def pane_header(pane: screens_mod.Pane) -> Window:
        return Window(
            content=FormattedTextControl(
                text=lambda: ui.pane_title_fragments(pane.title or pane.key, pane.status, state.screen.pane is pane)
            ),
            height=1,
            style="class:pane.title",
        )

    def pane_block(pane: screens_mod.Pane, titled: bool):  # noqa: ANN202 — контейнер prompt_toolkit
        window = pane_window(pane)
        return HSplit([pane_header(pane), window]) if titled else window

    def screen_container():  # noqa: ANN202 — контейнер prompt_toolkit
        """Раскладка активного экрана: одна лента, развёрнутая панель или сетка панелей."""
        screen = state.screen
        panes = screen.panes
        if len(panes) == 1:
            return pane_block(panes[0], titled=False)
        if screen.zoomed:
            return pane_block(screen.pane, titled=True)
        columns = pane_columns(len(panes), get_app().output.get_size().columns)
        rows: list[Any] = []
        for start in range(0, len(panes), columns):
            blocks: list[Any] = []
            for index, pane in enumerate(panes[start : start + columns]):
                if index:
                    blocks.append(Window(width=1, char="│", style="class:sep"))
                blocks.append(pane_block(pane, titled=True))
            if rows:
                rows.append(Window(height=1, char="─", style="class:sep"))
            rows.append(VSplit(blocks))
        return HSplit(rows)

    output_window = DynamicContainer(screen_container)

    input_area = TextArea(
        height=1,
        prompt="› ",
        multiline=False,
        wrap_lines=False,
        password=Condition(lambda: state.awaiting_key),
        completer=HarnessCompleter(state),
        complete_while_typing=True,
    )

    def sep() -> Window:
        return Window(height=1, char="─", style="class:sep")

    status_window = Window(
        content=FormattedTextControl(
            text=lambda: ui.status_fragments(
                state.model,
                state.config.is_authorized,
                state.profile.name,
                state.profile_dirty,
                state.mouse_enabled,
                archivist.running(state),
                compact.идёт(state),
            )
        ),
        height=1,
        style="class:status",
    )

    def agent_panel() -> Fragments:
        rows = collect_agents(state)
        return ui.agent_panel_fragments(
            [
                (row.status, row.agent.name, row.occupation, row.agent.total_ms, row.agent.total_tokens, row.agent.runs)
                for row in rows
            ],
            agent_index(state, rows),
            get_app().output.get_size().columns,
            lambda index: goto_agent(state, index),
        )

    agents_window = Window(
        content=FormattedTextControl(text=agent_panel, focusable=False),
        dont_extend_height=True,
        wrap_lines=False,
        style="class:agents",
    )
    # Пока агент один, списка нет: строка «main» в одиночестве ничего не сообщает и только
    # съедает высоту экрана. Отбивку прячем вместе со списком — иначе под строкой ввода
    # оставались бы две черты подряд.
    many_agents = Condition(lambda: show_agent_panel(state))
    agents_area = ConditionalContainer(HSplit([sep(), agents_window]), filter=many_agents)

    picker_active = Condition(lambda: state.picker is not None)
    picker_window = Window(
        content=FormattedTextControl(text=lambda: picker_mod.fragments(state.picker) if state.picker else []),
        style="class:panel",
        dont_extend_height=True,
        dont_extend_width=True,
    )

    # Полоска занятости контекста — над строкой ввода. Считает по ВЕСУ СЛЕДУЮЩЕГО ЗАПРОСА, а
    # не по расходу за сеанс: расход только растёт, а полоска обязана падать при сжатии — в
    # этом весь её смысл. Постоянная часть веса запоминается и пересчитывается при изменении
    # памяти, на нажатие клавиши прибавляется только вес набранного: системная часть ходит на
    # диск за глобальными фактами, и звать её на каждую букву нельзя.
    занятость = ui.СчётЗанятости(user_facts)
    state.занятость = занятость
    context_bar = Window(
        content=FormattedTextControl(
            text=lambda: ui.context_bar_fragments(
                занятость.ближайший(state.main_agent, state.input_buffer.text, model=state.model),
                get_app().output.get_size().columns,
            )
        ),
        dont_extend_height=True,
        style="class:gauge",
    )

    root = FloatContainer(
        content=HSplit([output_window, sep(), context_bar, input_area, agents_area, sep(), status_window]),
        floats=[
            Float(xcursor=True, ycursor=True, content=CompletionsMenu(max_height=12, scroll_offset=1)),
            Float(left=2, bottom=4, content=ConditionalContainer(picker_window, filter=picker_active)),
        ],
    )
    layout = Layout(root, focused_element=input_area)
    state.input_buffer = input_area.buffer

    kb = KeyBindings()
    buffer = input_area.buffer

    @kb.add("up", filter=picker_active)
    def _picker_up(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.move(-1)

    @kb.add("down", filter=picker_active)
    def _picker_down(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.move(1)

    @kb.add("enter", filter=picker_active)
    def _picker_choose(event) -> None:  # noqa: ANN001
        if state.picker:
            state.picker.choose()

    @kb.add("escape", filter=picker_active)
    @kb.add("c-c", filter=picker_active)
    def _picker_close(event) -> None:  # noqa: ANN001
        state.picker = None
        append_log(state, ui.system_fragments("выбор отменён"))

    @kb.add("<any>", filter=picker_active)
    def _picker_swallow(event) -> None:  # noqa: ANN001
        """Панель модальна: печать в строку ввода при открытом меню только мешала бы."""

    @kb.add("enter", filter=~picker_active)
    def _submit(event) -> None:  # noqa: ANN001
        completion_state = buffer.complete_state
        if completion_state is not None and completion_state.current_completion is not None:
            buffer.apply_completion(completion_state.current_completion)
            return
        text = buffer.text
        buffer.reset()
        if not state.screen.interactive:
            state.active = 0  # экран агента только для чтения — ответ придёт в главный
        for pane in state.screen.panes:
            pane.autoscroll = True  # новое сообщение — вернуться к живому выводу
        asyncio.get_running_loop().create_task(handle_submit(text, state))

    @kb.add("c-c", filter=~picker_active)
    def _cancel(event) -> None:  # noqa: ANN001
        if state.busy and state.current_task is not None:
            state.current_task.cancel()

    @kb.add("c-d")
    def _quit(event) -> None:  # noqa: ANN001
        event.app.exit()

    @kb.add("f2")
    def _toggle_mouse(event) -> None:  # noqa: ANN001
        toggle_mouse(state)

    @kb.add("pageup")
    def _scroll_up(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.autoscroll = False
        window = pane_window(pane)
        window.vertical_scroll = max(0, window.vertical_scroll - 10)

    @kb.add("pagedown")
    def _scroll_down(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.autoscroll = False
        pane_window(pane).vertical_scroll += 10

    @kb.add("c-end")
    def _resume_autoscroll(event) -> None:  # noqa: ANN001
        state.screen.pane.autoscroll = True

    # Меню команд тоже ходит стрелками, и отнимать их у него нельзя: «/» открывает список
    # прямо под строкой ввода, и там ↑/↓ выбирают команду.
    menu_open = Condition(lambda: buffer.complete_state is not None)

    @kb.add("up", filter=~picker_active & ~menu_open & many_agents)
    def _prev_agent(event) -> None:  # noqa: ANN001
        step_agent(state, -1)

    @kb.add("down", filter=~picker_active & ~menu_open & many_agents)
    def _next_agent(event) -> None:  # noqa: ANN001
        step_agent(state, 1)

    @kb.add("escape", "left", filter=~picker_active)
    def _prev_pane(event) -> None:  # noqa: ANN001
        switch_pane(state, state.screen.active_pane - 1)

    @kb.add("escape", "right", filter=~picker_active)
    def _next_pane(event) -> None:  # noqa: ANN001
        switch_pane(state, state.screen.active_pane + 1)

    @kb.add("f3", filter=~picker_active)
    def _zoom_pane(event) -> None:  # noqa: ANN001
        toggle_zoom(state)

    @kb.add("c-r", filter=~picker_active)
    def _toggle_reasoning(event) -> None:  # noqa: ANN001
        pane = state.screen.pane
        pane.show_reasoning = not pane.show_reasoning
        # Переключение меняет объём ленты выше точки просмотра сразу на все размышления,
        # а сама точка остаётся на прежнем номере строки — прокрученная вверх панель
        # прыгнула бы на чужое место. Возвращаемся к живому выводу: там место известно.
        pane.autoscroll = True
        refresh(state)

    @kb.add("s-right", filter=~picker_active)
    def _next_screen(event) -> None:  # noqa: ANN001
        switch_screen(state, (state.active + 1) % len(state.screens))

    @kb.add("s-left", filter=~picker_active)
    def _prev_screen(event) -> None:  # noqa: ANN001
        switch_screen(state, (state.active - 1) % len(state.screens))

    # Alt+N — прямо на экран с этим номером: номер написан на самой вкладке.
    for number in range(1, 10):
        @kb.add("escape", str(number), filter=~picker_active)
        def _goto_screen(event, index=number - 1) -> None:  # noqa: ANN001
            switch_screen(state, index)

    click_only_mouse(app_output())
    app = Application(
        layout=layout,
        key_bindings=kb,
        style=ui.STYLE,
        full_screen=True,
        mouse_support=Condition(lambda: state.mouse_enabled),
        erase_when_done=True,
    )
    # Esc — начало alt-комбинаций и escape-последовательностей, поэтому prompt_toolkit
    # ждёт продолжения, прежде чем счесть клавишу самостоятельной. При стандартных
    # 0.5 и 1.0 с закрытие панели по Esc ощущается как залипание; сочетаний с Alt у нас
    # нет, поэтому ждать долго незачем.
    app.ttimeoutlen = 0.15
    app.timeoutlen = 0.3
    return app


def greet(state: State) -> None:
    """Шапка и подсказка про инструкцию. Вынесены из `repl`, потому что печатать их надо
    ДО восстановления разговора: строка «восстановлен разговор…», вылезшая выше приветствия,
    читается так, будто разговор подняли ещё до запуска инструмента."""
    append_log(state, ui.banner_fragments(state.model, state.config.is_authorized, state.profile.name))
    if state.profile.system:
        append_log(state, ui.system_fragments("профиль задаёт системную инструкцию — показать: /system"))


async def repl(state: State) -> None:
    app = build_app(state)
    state.app = app
    worker_task = asyncio.create_task(worker(state))
    try:
        await app.run_async()
    finally:
        worker_task.cancel()
        with suppress(asyncio.CancelledError):
            await worker_task
        # Последний заход архивариуса — до закрытия клиента: без него всё, о чём говорили
        # после прошлого захода (до пяти обменов), в глобальную память не попало бы вовсе.
        await archivist.finish(state)
        # Идущий заход сжимателя, наоборот, снимаем и не дожидаемся: его итог — заготовка на
        # следующий обмен, а следующего обмена не будет. Ждать выхода ради неё значило бы
        # держать человека, уже сказавшего «выхожу», ради работы, которая никому не достанется.
        # Снять при этом обязательно: брошенная задача при закрытии цикла даёт предупреждение
        # «задача уничтожена, а она ещё работала» поверх прощального экрана.
        background.отменить(state.сжиматель)
        if state.client:
            await state.client.aclose()


def silence_transport_noise(loop: asyncio.AbstractEventLoop) -> None:
    """Глушит одно конкретное сообщение httpcore2 2.12: при обрыве ответа по max_tokens
    тело остаётся недочитанным, и закрытие потока печатает «generator didn't stop after
    athrow()». Это шум чужой библиотеки, но в полноэкранном режиме он рвёт разметку экрана.
    Все прочие ошибки цикла обрабатываются как обычно."""
    default_handler = loop.get_exception_handler()

    def handler(target_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get("exception")
        message = context.get("message", "")
        if isinstance(exception, RuntimeError) and "athrow" in str(exception):
            return
        if "closing of asynchronous generator" in message:
            return
        if default_handler is not None:
            default_handler(target_loop, context)
        else:
            target_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _main(args: argparse.Namespace) -> None:
    """Поднять интерфейс на уже разобранных ключах.

    Ключи сюда приходят готовыми (их разбирает точка входа пакета) и здесь не разбираются
    заново: второй разбор — это второе место, где живут имена и значения по умолчанию, и
    расходятся такие места молча."""
    silence_transport_noise(asyncio.get_running_loop())
    cfg = load_config()
    profile_name = args.profile or os.environ.get("MYHARNESS_PROFILE") or cfg.profile
    profile, warnings = profiles.load(profile_name)
    if args.model:
        cfg.model = args.model
    client = DeepSeekClient(cfg.api_key) if cfg.is_authorized else None
    state = State(config=cfg, client=client, model=cfg.model, profile=profile)
    greet(state)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    # Прежний разговор поднимается ЗДЕСЬ: настройки и профиль уже прочитаны, агент уже есть,
    # ни один запрос ещё невозможен. В конструкторе состояния этому места нет — его зовут
    # проверки напрямую, и любое состояние начало бы читать и писать в каталог состояния.
    restore_conversation(state)
    await repl(state)


def main(args: argparse.Namespace) -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(_main(args))
