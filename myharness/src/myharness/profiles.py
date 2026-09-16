"""Профили генерации: системная инструкция + параметры запроса одним объектом.

Профиль хранится JSON-файлом, длинный текст системной инструкции выносится в соседний
`.md` (в JSON он превратился бы в одну строку с экранированными переносами — нечитаемо
ни в редакторе, ни в различиях между версиями).

Кроме инструкции профиль может нести заготовку ввода (`prefill` / `prefill_file`): при выборе
профиля она подставляется в строку ввода. Нужна там, где вопрос к модели — часть самого
приёма и всегда один и тот же: например «составь промпт для решения такой-то задачи».
Заготовку видно до отправки, её можно поправить или стереть.

Профиль со списком `agents` — ведущий группы: перечисленные в нём профили поднимаются
как отдельные агенты, каждый со своей системной инструкцией и своим экраном, а сам ведущий
сводит их ответы. Профиль со списком `screens` раскладывает по вкладкам приём в несколько
шагов: каждый экран — своя инструкция, своя заготовка ввода и своя ветка разговора.

Профили ищутся в нескольких местах, ближний перекрывает дальний:
  1. `$MYHARNESS_PROFILES`, если переменная задана;
  2. `profiles/` в каталоге запуска и выше по дереву (до домашней папки) — профили проекта:
     harness, запущенный из песочницы `day_2/test/`, подхватит `day_2/profiles/`;
  3. `~/.config/myharness/profiles` — личные;
  4. встроенный `default` — на случай, когда файлов нет вообще.
"""

from __future__ import annotations

import contextlib
import json
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from string import Template
from typing import Any

from .context_strategy import (
    CONTEXT_BRANCHING,
    CONTEXT_FACTS,
    CONTEXT_STANDARD,
    CONTEXT_STRATEGIES,
    CONTEXT_SLIDING,
    DEFAULT_CONTEXT_STRATEGY,
    DEFAULT_STRATEGY_WINDOW,
)
from . import machine
from . import params as params_mod
from . import tokens
from .agent import DEFAULT_WINDOW_PAIRS
from .config import config_dir

DEFAULT_PROFILE_NAME = "default"

# Доля окна модели, при близости к которой память ужимается. Восемь десятых выведены из
# тарифа, а не выбраны на глаз: вход из живого кэша поставщика дешевле промаха в тридцать
# раз, а каждое сжатие меняет начало запроса и тем обнуляет кэш всего запроса целиком.
# Значит, сжимать надо как можно ПОЗЖЕ: восемьсот тысяч токенов из кэша стоят полтора
# цента, одно преждевременное сжатие — дороже. От окна в 1 048 576 остаётся за порогом
# около 210 000 — на новый вопрос, ответ, блок выжимки и рассуждения.
DEFAULT_COMPACT_AT = 0.8
# Выше этой доли порог не пускаем: за ним обязано остаться место на вопрос и на ответ.
MAX_COMPACT_AT = 0.9


# Раскладка цепочки шагов: панелями рядом на одной вкладке либо вкладкой на шаг.
LAYOUT_PANES = "panes"
LAYOUT_TABS = "tabs"
LAYOUTS = (LAYOUT_PANES, LAYOUT_TABS)
DEFAULT_LAYOUT = LAYOUT_PANES


class Substitution(Template):
    """Подстановка $переменных с именами на любом языке.

    Стандартный Template распознаёт только латиницу, поэтому `$слов` он оставлял в тексте
    как есть — инструкция уходила в модель с долларом вместо числа, и заметить это можно
    было только по ответу. Тихая неподстановка хуже явной ошибки, поэтому шаблон расширен
    до любых буквенных имён.
    """

    idpattern = r"(?:[^\W\d]\w*)"


class _JSONObject(dict[str, Any]):
    """Обычный JSON-объект, который дополнительно помнит исходные пары ключей.

    Как словарь он сохраняет привычное поведение `json.loads`: при точном повторе ключа
    доступно последнее значение. Исходные пары нужны только разбору `branch_prefills`,
    где повтор имени обязан быть виден и отброшен с предупреждением.
    """

    __slots__ = ("pairs",)

    def __init__(self, pairs: list[tuple[str, Any]]) -> None:
        super().__init__(pairs)
        self.pairs = pairs


def user_profiles_dir() -> Path:
    return config_dir() / "profiles"


@dataclass
class Profile:
    name: str
    description: str = ""
    title: str = ""  # короткое имя для вкладки и заголовка панели; пусто — берётся name
    system: str | None = None  # итоговый текст инструкции (подстановки уже выполнены)
    system_file: str | None = None
    prefill: str | None = None  # заготовка ввода: подставляется в строку ввода при выборе профиля
    prefill_file: str | None = None
    # Очередь заготовок: следующий вопрос подставляется сам, как только отправлен предыдущий.
    # Заведена для повторяемых прогонов — показа и разбора: набранный руками вопрос всякий раз
    # чуть другой, и сравнивать два прогона между собой становится нечем.
    prefills: list[str] = field(default_factory=list)
    keep_history: bool = True
    # сколько пар «вопрос — ответ» держать в памяти; 0 — окно выключено, память не обрезается
    history_window: int = DEFAULT_WINDOW_PAIRS
    # предел веса запроса в токенах; 0 — предела нет. Окно по парам меряет не то, чем считает
    # контекст и деньги поставщик: пять пар со вставленными файлами весят больше сотни коротких.
    budget_tokens: int = 0
    # доля окна модели, при близости к которой память ужимается; 0 — сжатие выключено
    compact_at: float = DEFAULT_COMPACT_AT
    # Политика выбора истории. Отдельное окно стратегии не заменяет прежнее history_window:
    # оно действует только в новых режимах, а standard сохраняет прежнее поведение.
    context_strategy: str = DEFAULT_CONTEXT_STRATEGY
    strategy_window: int = DEFAULT_STRATEGY_WINDOW
    # Очереди заготовок ветвей хранятся и при другом выбранном режиме: пользователь может
    # вернуться к branching, не потеряв настройки профиля.
    branch_prefills: dict[str, list[str]] = field(default_factory=dict)
    agents: list[str] = field(default_factory=list)  # непусто — профиль ведущего группы
    screens: list[str] = field(default_factory=list)  # непусто — набор рабочих экранов
    # как разложены шаги цепочки: "panes" — панелями рядом, "tabs" — вкладкой на шаг
    layout: str = DEFAULT_LAYOUT
    methods: list[str] = field(default_factory=list)  # непусто — набор способов решения
    # какие шаги цепочки составляют её итог; пусто — все шаги в порядке исполнения
    result: list[str] = field(default_factory=list)
    # Записи о человеке, с которыми это занятие расходится: дословно, как они лежат в
    # глобальной памяти. Не выбрасывают запись из запроса, а помечают её — см. `memory.facts_block`.
    overrides: list[str] = field(default_factory=list)
    # Карта стадий автомата задачи; `None` — профиль задачу стадиями не ведёт.
    стадии: machine.Карта | None = None
    vars: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        """Слепок профиля для журнала — по нему прогон можно воспроизвести."""
        snapshot: dict[str, Any] = {
            "name": self.name,
            "system": self.system,
            "keep_history": self.keep_history,
            "history_window": self.history_window,
            "budget_tokens": self.budget_tokens,
            "compact_at": self.compact_at,
            "context_strategy": self.context_strategy,
            "strategy_window": self.strategy_window,
            "params": dict(self.params),
        }
        if self.branch_prefills:
            snapshot["branch_prefills"] = {
                name: list(prefills) for name, prefills in self.branch_prefills.items()
            }
        if self.agents:
            snapshot["agents"] = list(self.agents)
        if self.screens:
            snapshot["screens"] = list(self.screens)
            snapshot["layout"] = self.layout
            if self.result:
                snapshot["result"] = list(self.result)
        if self.methods:
            snapshot["methods"] = list(self.methods)
        if self.overrides:
            snapshot["overrides"] = list(self.overrides)
        if self.стадии is not None:
            snapshot["stages"] = machine.в_список(self.стадии)
        return snapshot

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.title:
            data["title"] = self.title
        if self.description:
            data["description"] = self.description
        if self.system_file:
            data["system_file"] = self.system_file
        elif self.system is not None:
            data["system"] = self.system
        if self.prefill_file:
            data["prefill_file"] = self.prefill_file
        elif self.prefill is not None:
            data["prefill"] = self.prefill
        if self.prefills:
            data["prefills"] = list(self.prefills)
        data["keep_history"] = self.keep_history
        data["history_window"] = self.history_window
        data["budget_tokens"] = self.budget_tokens
        data["compact_at"] = self.compact_at
        data["context_strategy"] = self.context_strategy
        data["strategy_window"] = self.strategy_window
        if self.branch_prefills:
            data["branch_prefills"] = {
                name: list(prefills) for name, prefills in self.branch_prefills.items()
            }
        if self.agents:
            data["agents"] = list(self.agents)
        if self.screens:
            data["screens"] = list(self.screens)
            data["layout"] = self.layout
            if self.result:
                data["result"] = list(self.result)
        if self.methods:
            data["methods"] = list(self.methods)
        if self.overrides:
            data["overrides"] = list(self.overrides)
        if self.стадии is not None:
            data["stages"] = machine.в_список(self.стадии)
        if self.vars:
            data["vars"] = self.vars
        data.update(self.params)
        return data


def builtin_default() -> Profile:
    return Profile(name=DEFAULT_PROFILE_NAME, description="без ограничений: ничего не задаём, историю храним")


MAX_UPWARD_LEVELS = 5


def _upward_dirs(start: Path) -> list[Path]:
    """`profiles/` в каталоге запуска и у ближайших родителей — не выше домашней папки."""
    found: list[Path] = []
    current = start
    home = Path.home()
    for _ in range(MAX_UPWARD_LEVELS):
        found.append(current / "profiles")
        if current == home or current.parent == current:
            break
        current = current.parent
    return found


def search_dirs(cwd: Path | None = None) -> list[Path]:
    """Каталоги поиска по убыванию близости. `cwd` — откуда считать «рядом»; умолчание —
    каталог запуска.

    Довод заведён затем, чтобы порядок жил в ОДНОМ месте: `target_dir` пишет новый профиль по
    тому же порядку, и второй его список неизбежно разошёлся бы с этим — молча и в ту сторону,
    где записанный профиль перекрыт одноимённым из ближнего каталога."""
    dirs: list[Path] = []
    env_dir = os.environ.get("MYHARNESS_PROFILES")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    try:
        dirs.extend(_upward_dirs((cwd or Path.cwd()).resolve()))
    except OSError:  # каталог запуска удалён из-под нас
        pass
    dirs.append(user_profiles_dir())
    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        resolved = d.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def available() -> list[tuple[str, Path | None]]:
    """Имена профилей с источником; ближний каталог перекрывает дальний."""
    found: dict[str, Path] = {}
    for directory in reversed(search_dirs()):  # дальние первыми, ближние затирают
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            found[path.stem] = path
    items: list[tuple[str, Path | None]] = sorted(found.items())
    if DEFAULT_PROFILE_NAME not in found:
        items.insert(0, (DEFAULT_PROFILE_NAME, None))
    return items


def _prefills(value: Any, warnings: list[str]) -> list[str]:
    """Очередь заготовок из файла профиля.

    Пустые строки выбрасываем, нестроковое — называем вслух и пропускаем: молча съеденный
    элемент очереди означал бы, что на показе вопрос не появится, а почему — неизвестно."""
    if value is None:
        return []
    if not isinstance(value, list):
        warnings.append("поле prefills — список вопросов; значение другого вида пропущено")
        return []
    очередь: list[str] = []
    for элемент in value:
        if isinstance(элемент, str) and элемент.strip():
            очередь.append(элемент.strip())
        else:
            warnings.append(f"в prefills пропущен элемент, который не текст: {элемент!r}")
    return очередь


def _overrides(raw: Any, warnings: list[str]) -> list[str]:
    """Записи о человеке, которые это занятие перекрывает. Мусор отбрасываем вслух.

    Строки хранятся ДОСЛОВНО, со срезанными лишь краевыми пробелами: файл профиля должен
    показывать запись такой, какая она есть. Сравнение — дело не хранения: его ведёт общий ключ
    опознания `memory.ключ_записи`, и внутренний вид строки ему не мешает.

    Повтор отбрасываем по той же причине, что и в списках имён: записи уникальны, значит
    повтор — опечатка, и молчание о ней скрывает её от человека."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append("поле «overrides» — список записей о человеке, значение другого вида пропущено")
        return []
    записи: list[str] = []
    for элемент in raw:
        if not isinstance(элемент, str) or not элемент.strip():
            warnings.append(f"поле «overrides»: {элемент!r} — не текст записи, пропущено")
            continue
        запись = элемент.strip()
        if any(запись.casefold() == прежняя.casefold() for прежняя in записи):
            warnings.append(f"поле «overrides»: «{запись}» указана повторно — второе упоминание пропущено")
            continue
        записи.append(запись)
    return записи


def _profile_names(raw: Any, field_name: str, warnings: list[str]) -> list[str]:
    """Список имён профилей (состав группы, набор рабочих экранов или набор способов). Мусор в
    поле не должен ронять профиль — отбрасываем его с предупреждением, как и неизвестные
    параметры.

    Повторы имён отбрасываем там же и по тому же правилу. Оставить их нельзя: одно имя дважды
    означает два запроса ОДНОМУ собеседнику — две одинаковые пары в его памяти, две ленты в
    одной панели и двойная цена за тот же ответ. Схлопнуть молча тоже нельзя: повтор — это
    почти всегда опечатка в профиле, и человек должен о ней услышать. Порядок первого появления
    сохраняется: по нему расставлены вкладки и панели."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(f"поле «{field_name}» — не список имён профилей, пропущено")
        return []
    names: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not item.strip():
            warnings.append(f"поле «{field_name}»: {item!r} — не имя профиля, пропущено")
            continue
        name = item.strip()
        if name in names:
            warnings.append(f"поле «{field_name}»: «{name}» указан повторно — второе упоминание пропущено")
            continue
        names.append(name)
    return names


def _history_window(raw: Any, warnings: list[str]) -> int:
    """Размер окна памяти в парах: целое неотрицательное, 0 — окно выключено.

    Мусор в поле отбрасываем с предупреждением, а не подставляем умолчание молча: молча
    подставленное значение сделало бы поведение необъяснимым — пользователь написал одно,
    harness работает по-другому и нигде об этом не говорит. `bool` отсеиваем отдельно, он
    в Python подкласс `int`, и `true` иначе прошло бы как окно в одну пару."""
    if raw is None:
        return DEFAULT_WINDOW_PAIRS
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        warnings.append(
            f"поле «history_window»: {raw!r} — ожидалось целое неотрицательное число, "
            f"взято умолчание {DEFAULT_WINDOW_PAIRS}"
        )
        return DEFAULT_WINDOW_PAIRS
    return raw


def _budget_tokens(raw: Any, warnings: list[str]) -> int:
    """Предел веса запроса в токенах: целое неотрицательное, 0 — предела нет.

    Умолчание — ноль, а не какое-нибудь разумное число: профиль, написанный до появления
    поля, обязан работать ровно как прежде. Предел — вещь, о которой просят вслух.

    Мусор отбрасываем с предупреждением по той же причине, что и в `_history_window`: молча
    подставленное умолчание сделало бы поведение необъяснимым — человек написал предел,
    harness режет память по-своему и нигде об этом не говорит. `bool` отсеиваем отдельно,
    он в Python подкласс `int`, и `true` прошло бы как бюджет в один токен, то есть как
    приказ выбросить всю память до последней пары."""
    if raw is None:
        return 0
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        warnings.append(
            f"поле «budget_tokens»: {raw!r} — ожидалось целое неотрицательное число, предел не задан"
        )
        return 0
    if 0 < raw < tokens.BASE_OVERHEAD:
        # Значение оставляем как есть: человек вправе задать любой предел, и подменять его
        # своим — то же молчаливое умолчание, от которого разбор и защищается. Но сказать
        # обязаны: в такой предел не влезает даже пустой запрос — одна обёртка разговора
        # весит больше, — а значит память будет обрезана до последней пары при каждом
        # обмене. Узнавать об этом по поведению («почему модель ничего не помнит?») человек
        # не должен: поведение объяснится не сразу, а строка предупреждения — сразу.
        warnings.append(
            f"поле «budget_tokens»: {raw} меньше веса пустого запроса ({tokens.BASE_OVERHEAD}) — "
            f"память будет обрезана до последней пары"
        )
    return raw


def _compact_at(raw: Any, warnings: list[str]) -> float:
    """Порог сжатия памяти: доля окна модели от нуля до девяти десятых включительно.

    Ноль здесь не край диапазона, а рабочий случай: им сжатие и выключают, поэтому он
    пригоден и предупреждения не даёт. Верхняя граница — девять десятых: за порогом обязано
    остаться место на новый вопрос и на `max_tokens` ответа, а порог в единицу означал бы
    сжатие в тот момент, когда запрос уже равен окну, — то есть после отказа сервера, а не
    до него: успеть по такому порогу нельзя никогда.

    Целое допускаем наравне с дробным — ради того же нуля, который в JSON пишется без точки.
    `bool` отсеиваем отдельно, он в Python подкласс `int`. `true` поймала бы и верхняя граница,
    а вот `false` лежит ВНУТРИ диапазона: без отдельной проверки он прошёл бы как ноль, то есть
    молча выключил бы сжатие у того, кто просто описался. Та же беда однажды была поймана на
    `history_window`.

    Мусор отбрасываем с предупреждением по той же причине, что и в `_history_window`: молча
    подставленное умолчание сделало бы поведение необъяснимым — человек написал один порог,
    harness сжимает по другому и нигде об этом не говорит.

    Отдельного текста для `false` сознательно НЕ заводим, хотя он и подсказал бы точнее:
    написавший `"compact_at": false` прочтёт про разрешённый ноль и решит, что был прав.
    Цена расхождения выше цены неточности — `_history_window` и `_budget_tokens` устроены
    ровно так же, одним условием на все роды мусора, и третья сестра с собственной веткой
    разошлась бы с ними при первой же общей правке. Само значение в предупреждении печатается
    как `False`, так что увидеть написанное человек всё равно может."""
    if raw is None:
        return DEFAULT_COMPACT_AT
    if isinstance(raw, bool) or not isinstance(raw, int | float) or not 0 <= raw <= MAX_COMPACT_AT:
        warnings.append(
            f"поле «compact_at»: {raw!r} — ожидалась доля окна от 0 до {MAX_COMPACT_AT} "
            f"(0 — сжатие выключено), взято умолчание {DEFAULT_COMPACT_AT}"
        )
        return DEFAULT_COMPACT_AT
    return float(raw)


def _context_strategy(raw: Any, warnings: list[str]) -> str:
    """Разбирает закрытый набор стратегий, сохраняя прежний режим для старых профилей."""
    if raw is None:
        return DEFAULT_CONTEXT_STRATEGY
    if not isinstance(raw, str) or raw not in CONTEXT_STRATEGIES:
        allowed = ", ".join(f"«{strategy}»" for strategy in CONTEXT_STRATEGIES)
        warnings.append(
            f"поле «context_strategy»: {raw!r} — ожидалось одно из значений {allowed}, "
            f"взято умолчание «{DEFAULT_CONTEXT_STRATEGY}»"
        )
        return DEFAULT_CONTEXT_STRATEGY
    return raw


def _strategy_window(raw: Any, warnings: list[str]) -> int:
    """Строгое окно новых стратегий в завершённых парах."""
    if raw is None:
        return DEFAULT_STRATEGY_WINDOW
    if isinstance(raw, bool) or not isinstance(raw, int) or raw <= 0:
        warnings.append(
            f"поле «strategy_window»: {raw!r} — ожидалось положительное целое число, "
            f"взято умолчание {DEFAULT_STRATEGY_WINDOW}"
        )
        return DEFAULT_STRATEGY_WINDOW
    return raw


def _branch_prefills(raw: Any, warnings: list[str]) -> dict[str, list[str]]:
    """Очереди заготовок по именам ветвей с порядком из файла профиля."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        warnings.append("поле «branch_prefills» — не объект очередей ветвей, пропущено")
        return {}

    branches: dict[str, list[str]] = {}
    seen: set[str] = set()
    pairs = raw.pairs if isinstance(raw, _JSONObject) else raw.items()
    for raw_name, raw_prefills in pairs:
        if not isinstance(raw_name, str) or not raw_name.strip():
            warnings.append(
                f"поле «branch_prefills»: {raw_name!r} — не имя ветви, пропущено"
            )
            continue
        name = raw_name.strip()
        if name in seen:
            warnings.append(
                f"поле «branch_prefills»: ветвь «{name}» указана повторно — "
                "второе упоминание пропущено"
            )
            continue
        # Имя занято первым упоминанием независимо от пригодности его очереди: иначе
        # повтор смог бы молча заменить ошибочное первое значение.
        seen.add(name)
        if not isinstance(raw_prefills, list):
            warnings.append(
                f"поле «branch_prefills»: очередь ветви «{name}» — не список, пропущена"
            )
            continue

        prefills: list[str] = []
        for item in raw_prefills:
            if isinstance(item, str) and item.strip():
                prefills.append(item.strip())
            else:
                warnings.append(
                    f"поле «branch_prefills», ветвь «{name}»: "
                    f"непригодная заготовка {item!r} пропущена"
                )
        branches[name] = prefills
    return branches


def _layout(raw: Any, screens: list[str], warnings: list[str]) -> str:
    """Раскладка шагов цепочки: «panes» — панелями рядом на одной вкладке, «tabs» — вкладкой
    на шаг. Умолчание — панели: так поведение прежних профилей не меняется от появления поля.

    Мусор отбрасываем с предупреждением, как и в `_history_window`: молча подставленное
    умолчание сделало бы поведение необъяснимым. Логическое значение сюда не проходит само —
    оно не строка, — но названо в сообщении наравне с прочим мусором.

    Раскладывать нечего, если шагов нет: `layout` у профиля без `screens` — почти всегда
    поле, положенное не в тот профиль, и промолчать об этом значит оставить человека с
    настройкой, которая ничего не делает.
    """
    if raw is None:
        return DEFAULT_LAYOUT
    if not screens:
        warnings.append(
            "поле «layout» имеет смысл только у профиля-цепочки со списком «screens» — не применено"
        )
        return DEFAULT_LAYOUT
    if not isinstance(raw, str) or raw not in LAYOUTS:
        warnings.append(
            f"поле «layout»: {raw!r} — ожидалось «{LAYOUT_PANES}» или «{LAYOUT_TABS}», "
            f"взято умолчание «{DEFAULT_LAYOUT}»"
        )
        return DEFAULT_LAYOUT
    return raw


def _chain_result(raw: Any, screens: list[str], warnings: list[str]) -> list[str]:
    """Шаги, составляющие итог цепочки. Пусто — итогом будут ответы всех шагов.

    Умолчание именно «все шаги», а не «последний»: цепочка, кончающаяся проверяющим, отдала бы
    наверх один вердикт «ГОДЕН / НЕ ГОДЕН» без предмета вердикта — код остался бы на своей
    вкладке. Потерять предмет работы хуже, чем показать лишний шаг, поэтому сужает список автор
    профиля, а не умолчание за него.

    Шаг, которого нет в `screens`, отбрасываем с предупреждением: молча пропущенное имя дало бы
    итог, в котором чего-то не хватает, и объяснить это было бы нечем."""
    names = _profile_names(raw, "result", warnings)
    if names and not screens:
        warnings.append("поле «result» без «screens» — сужать нечего, пропущено")
        return []
    kept = [name for name in names if name in screens]
    for name in names:
        if name not in screens:
            warnings.append(f"шаг «{name}» из «result» не входит в «screens» — пропущен")
    return kept


def _from_dict(data: dict[str, Any], name: str, base_dir: Path, source: Path | None) -> tuple[Profile, list[str]]:
    warnings: list[str] = []
    known_meta = {
        "name",
        "title",
        "description",
        "system",
        "system_file",
        "prefill",
        "prefill_file",
        "prefills",
        "keep_history",
        "history_window",
        "budget_tokens",
        "compact_at",
        "context_strategy",
        "strategy_window",
        "branch_prefills",
        "agents",
        "screens",
        "methods",
        "layout",
        "result",
        "overrides",
        "stages",
        "vars",
    }

    def _text(inline_key: str, file_key: str) -> str | None:
        value = data.get(inline_key)
        file_name = data.get(file_key)
        if not file_name:
            return value
        path = (base_dir / file_name).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"не удалось прочитать {path.name}: {exc}")
            return None

    system_text = _text("system", "system_file")
    system_file = data.get("system_file")
    prefill_text = _text("prefill", "prefill_file")
    prefills = _prefills(data.get("prefills"), warnings)

    variables = data.get("vars") or {}
    if variables:
        # safe_substitute по $имя: фигурные скобки примера JSON внутри инструкции не трогаются
        if system_text:
            system_text = Substitution(system_text).safe_substitute(variables)
        if prefill_text:
            prefill_text = Substitution(prefill_text).safe_substitute(variables)
        # Очередь заготовок проходит подстановку наравне с одиночной: требование объявляет
        # одиночную «той же очередью длиной в один вопрос», и два поля, объявленные одним,
        # не имеют права вести себя по-разному. Иначе `$переменная` уехала бы в модель
        # дословно — и увидели бы это все, кто смотрит показ.
        prefills = [Substitution(вопрос).safe_substitute(variables) for вопрос in prefills]

    collected: dict[str, Any] = {}
    for key, value in data.items():
        if key in known_meta:
            continue
        if key in params_mod.SPECS:
            collected[key] = value
        else:
            warnings.append(f"неизвестный параметр «{key}» — пропущен")

    screen_names = _profile_names(data.get("screens"), "screens", warnings)
    result_names = _chain_result(data.get("result"), screen_names, warnings)
    context_strategy = _context_strategy(data.get("context_strategy"), warnings)
    strategy_window = _strategy_window(data.get("strategy_window"), warnings)
    branch_prefills = _branch_prefills(data.get("branch_prefills"), warnings)
    compact_at = _compact_at(data.get("compact_at"), warnings)
    if context_strategy != CONTEXT_STANDARD and compact_at != 0:
        warnings.append(
            f"поле «compact_at» сохранено, но в режиме «{context_strategy}» сжатие не применяется"
        )
    if "branch_prefills" in data and context_strategy != CONTEXT_BRANCHING:
        warnings.append(
            f"поле «branch_prefills» сохранено, но применяется только в режиме "
            f"«{CONTEXT_BRANCHING}»"
        )

    имя_профиля = data.get("name") or name
    стадии: machine.Карта | None = None
    if "stages" in data:
        # Имена консилиума сверяются с тем, что видит `/profile`, только ради предупреждения:
        # перечень зависит от каталога запуска, окончательная проверка — при созыве.
        известные = {имя for имя, _ in available()}
        свои = tuple(
            имя for имя in (name, data.get("name")) if isinstance(имя, str) and имя
        )
        стадии, жалобы_карты = machine.разобрать_карту(
            data.get("stages"), известные.__contains__, свой_профиль=свои
        )
        warnings.extend(жалобы_карты)

    profile = Profile(
        name=имя_профиля,
        title=data.get("title", ""),
        description=data.get("description", ""),
        system=system_text.strip() if isinstance(system_text, str) else None,
        system_file=system_file,
        prefill=prefill_text.strip() if isinstance(prefill_text, str) else None,
        prefill_file=data.get("prefill_file"),
        prefills=prefills,
        keep_history=bool(data.get("keep_history", True)),
        history_window=_history_window(data.get("history_window"), warnings),
        budget_tokens=_budget_tokens(data.get("budget_tokens"), warnings),
        compact_at=compact_at,
        context_strategy=context_strategy,
        strategy_window=strategy_window,
        branch_prefills=branch_prefills,
        agents=_profile_names(data.get("agents"), "agents", warnings),
        screens=screen_names,
        layout=_layout(data.get("layout"), screen_names, warnings),
        methods=_profile_names(data.get("methods"), "methods", warnings),
        result=result_names,
        overrides=_overrides(data.get("overrides"), warnings),
        стадии=стадии,
        vars=dict(variables),
        params=collected,
        source=source,
    )
    return profile, warnings


def _короткий_путь(каталог: Path) -> str:
    """Путь для человека: домашний каталог сокращаем до «~», остальное как есть."""
    дом = Path.home()
    try:
        return "~/" + str(каталог.relative_to(дом))
    except ValueError:
        return str(каталог)


def search_hint() -> str:
    """Где искать профили — одной строкой, для сообщений человеку."""
    return " · ".join(_короткий_путь(каталог) for каталог in search_dirs())


def load(name: str) -> tuple[Profile, list[str]]:
    """Профиль по имени. Если файла нет, а имя — default, отдаём встроенный."""
    for directory in search_dirs():
        path = directory / f"{name}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(
                path.read_text(encoding="utf-8"),
                object_pairs_hook=_JSONObject,
            )
        except (json.JSONDecodeError, OSError) as exc:
            return builtin_default(), [f"профиль «{name}» испорчен ({exc}) — взят default"]
        if not isinstance(data, dict):
            return builtin_default(), [f"профиль «{name}»: ожидался объект JSON — взят default"]
        return _from_dict(data, name, path.parent, path)
    if name == DEFAULT_PROFILE_NAME:
        return builtin_default(), []
    # Где искали — обязательная часть жалобы, а не любезность. Профили ищутся рядом с
    # КАТАЛОГОМ ЗАПУСКА, и человек, запустивший harness не оттуда, видит короткий список без
    # своих профилей и не понимает почему. Названные каталоги отвечают на это сразу.
    return builtin_default(), [f"профиль «{name}» не найден — взят default. Искали: {search_hint()}"]


# Знаки, которых не бывает в имени профиля: имя приходит из ответа модели и становится
# ИМЕНЕМ ФАЙЛА. Без проверки текст ответа решал бы, куда пишет программа, — «../../ключи» для
# модели такая же строка, как «математик».
ЗАПРЕЩЕНО_В_ИМЕНИ = ("/", "\\")


def проверить_имя(name: str) -> str | None:
    """Жалоба словами, если имя не годится файлу; `None` — имя годится.

    Длину не ограничиваем: предел имени файла ставит сама система, и её отказ придёт
    `OSError` с настоящей причиной. Своего потолка здесь быть не должно — он отказал бы там,
    где человек назвал профиль верно.
    """
    имя = name or ""
    if not имя.strip():
        return "имя профиля пустое"
    if имя != имя.strip():
        # Имя с краевым пробелом создало бы файл «матем .json»: в перечне профилей он виден,
        # а `/profile матем` его не находит — человек смотрит на профиль, который не
        # выбирается. Обрезать за человека нельзя: `profile.name` ушёл бы в файл одним, а имя
        # файла стало бы другим.
        return "имя профиля начинается или кончается пробелом — уберите его"
    if any(знак < " " for знак in имя):
        return "в имени профиля есть управляющий знак — имя становится именем файла"
    for знак in ЗАПРЕЩЕНО_В_ИМЕНИ:
        if знак in имя:
            return f"в имени профиля недопустим знак «{знак}»: имя становится именем файла"
    if ".." in имя:
        return "в имени профиля недопустимы две точки подряд: имя становится именем файла"
    if имя.startswith("."):
        return "имя профиля начинается с точки — такой файл не виден в перечне профилей"
    return None


def target_dir(cwd: Path) -> Path:
    """Куда лечь НОВОМУ профилю: первый существующий каталог из порядка поиска.

    Порядок тот же, по которому профили ищутся, и это не удобство, а условие работоспособности:
    запиши мы профиль в дальний каталог, одноимённый из ближнего перекрыл бы его — и человек
    правил бы файл, который в запрос не уходит.

    Не существует ни одного каталога — личный: он единственный, чьё появление не зависит от
    того, откуда запущен инструмент.
    """
    личный = user_profiles_dir()
    return next((каталог for каталог in search_dirs(cwd) if каталог.is_dir()), личный)


def save_pair(profile: Profile, directory: Path) -> tuple[Path, Path | None]:
    """Кладёт НОВЫЙ профиль парой файлов: `<имя>.json` и, при непустой инструкции, `<имя>.md`.

    Пара — не украшение: в JSON длинный текст превращается в одну строку с экранированными
    переносами, и профиль, записанный программой, нельзя ни прочитать в редакторе, ни сравнить
    с прежней версией.

    Только для НОВОГО профиля: лежащие файлы дают `FileExistsError`, перезаписи здесь нет вовсе.
    Это решение оплачено тремя кругами правок. Стоило разрешить перезапись — и появились три
    разных способа потерять чужой текст: затереть рукописный `.md` подставленными значениями
    `$переменных`, оставить профиль вовсе без инструкции и сослаться на `.md` соседнего профиля.
    Ни один из трёх не относился к делу, ради которого пара заведена: профиль, рождённый
    разговором, пишется под НОВЫМ именем, и перезаписывать ему нечего.

    Оба файла или ни одного. Текст JSON собирается ДО первой записи на диск: соберись он после,
    непригодное к записи значение в `vars` оставило бы рядом `.md`-сироту, а сирота занимает
    имя — повторная попытка получила бы «имя занято» для профиля, которого нет.
    """
    жалоба = проверить_имя(profile.name)
    if жалоба:
        raise ValueError(жалоба)
    directory.mkdir(parents=True, exist_ok=True)
    путь_json = directory / f"{profile.name}.json"
    инструкция = (profile.system or "").strip()
    путь_md = directory / f"{profile.name}.md" if инструкция else None

    данные = replace(profile, system_file=путь_md.name) if путь_md else profile
    текст_json = json.dumps(данные.to_dict(), ensure_ascii=False, indent=2) + "\n"

    написан_md = False
    try:
        if путь_md is not None:
            # Режим «x» вместо проверки существования: между `exists()` и записью успевает
            # вклиниться вторая запущенная копия инструмента, а на висящей ссылке `exists()`
            # вернул бы «нет» и запись ушла бы по ссылке наружу каталога.
            with open(путь_md, "x", encoding="utf-8") as файл:
                файл.write(инструкция + "\n")
            написан_md = True
        with open(путь_json, "x", encoding="utf-8") as файл:
            файл.write(текст_json)
    except BaseException:
        # Любой сбой, а не только `OSError`: сирота `.md` занял бы имя профиля, которого нет.
        if написан_md and путь_md is not None:
            путь_md.unlink(missing_ok=True)
        raise
    return путь_json, путь_md


def save(profile: Profile) -> Path:
    """`/profile save`: записать текущий профиль под своим именем в личный каталог.

    Пишет ОДИН файл `<имя>.json` — как писала всегда, и это сознательный отказ от пары ЗДЕСЬ.
    Команда перезаписывает профиль своим именем, а перезапись пары означала бы трогать соседний
    `<имя>.md`, которого человек не называл: в нём может лежать его собственный текст с
    `$переменными`, дошедший до нас уже подставленным. Три беды подряд выросли ровно из попытки
    записать здесь пару, и ни одна из них не относилась к способности, ради которой пара заведена.

    Имя проверяется здесь же: `/profile save ../../ключи` иначе означал бы запись мимо каталога
    профилей — файл лёг бы туда, куда указывает ответ на команду, а не туда, где живут профили.
    """
    жалоба = проверить_имя(profile.name)
    if жалоба:
        raise ValueError(жалоба)
    directory = user_profiles_dir()
    directory.mkdir(parents=True, exist_ok=True)
    путь = directory / f"{profile.name}.json"
    путь.write_text(
        json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    return путь
