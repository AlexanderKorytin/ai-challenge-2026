"""Пакетный наряд: список заданий из файла выполняется без человека и без терминала.

Зачем режим нужен. Во-первых, по прямому требованию: инструмент должен уметь не ждать
нажатия клавиши, а сразу исполнять то, что записано в наряде. Во-вторых — и это важнее —
режим служит проверкой изоляции делом. Про модуль `agent` сказано, что он отделён от
интерфейса, но декларацию проверить нечем; а этот режим либо запускается в процессе, где
`prompt_toolkit` не загружен вовсе, либо не запускается. Поэтому модулю разрешены только
`agent`, `api`, `profiles`, `config`, `journal` и стандартная библиотека, а `cli`,
`screens`, `team`, `methods`, `output`, `ui` и сам `prompt_toolkit` — запрещены.

Ограничение здесь не число заданий, а число ОДНОВРЕМЕННЫХ запросов: сотня заданий разом
упрётся в предел частоты у поставщика, и половина наряда вернётся отказами вместо ответов.
Поэтому одновременность ограничена всегда — значением наряда, ключом `--concurrency` или
умолчанием `DEFAULT_CONCURRENCY`.

Файл наряда — JSON такого вида:

    { "model": "deepseek-v4-flash", "concurrency": 8,
      "tasks": [ { "agent": "физик", "profile": "expert",
                   "vars": {"область": "физика"}, "ask": "вопрос" } ] }

Один профиль-заготовка с `$переменными` плюс свои `vars` у каждого задания дают сто разных
собеседников двадцатью строками наряда. Механизм подстановки — тот же самый, что у профилей
(`profiles.Substitution`), третьего заводить нельзя: два разных правила подстановки в одном
инструменте расходятся при первой же правке.
"""

from __future__ import annotations

import asyncio
import json
import sys
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from uuid import uuid4

from . import api, config, context_strategy, profiles
from .agent import Agent, Turn, usage_tokens
from .profiles import Profile, Substitution

# Восемь одновременных запросов — компромисс, а не круглое число: у DeepSeek предел частоты
# считается по запросам в минуту, и восьми хватает, чтобы наряд из сотни заданий не тянулся
# по одному, но не хватает, чтобы упереться в предел с первой же секунды.
DEFAULT_CONCURRENCY = 8


class OrderError(Exception):
    """Наряд нельзя прочитать как наряд — отказ всего файла, а не пропуск одного задания.

    Своё исключение, а не `ValueError`: тот пришлось бы ловить в `main` вместе с любым
    `ValueError`, случайно вылетевшим из нашего же кода, и настоящая ошибка разбора уехала бы
    в вывод под видом жалобы на файл пользователя."""


@dataclass
class Task:
    """Одно задание наряда: кто спрашивает, по какой заготовке и о чём."""

    agent: str  # имя задания: попадает в журнал и в сводку
    profile: str  # имя профиля-заготовки
    vars: dict  # подстановки поверх профиля
    ask: str  # вопрос
    # Готовый профиль задания — заготовка с уже подставленными `vars`. Кладётся при разборе
    # наряда, там же, где профиль проверяется на существование. Грузить его повторно в
    # `run_order` значило бы читать те же файлы второй раз и рисковать разойтись с тем, что
    # уже проверено разбором.
    prepared: Profile | None = field(default=None, repr=False)


@dataclass
class Order:
    """Разобранный наряд целиком."""

    model: str
    concurrency: int
    tasks: list[Task]


def _concurrency(raw: Any, warnings: list[str]) -> int:
    """Число одновременных запросов: целое положительное.

    Мусор отбрасываем с предупреждением, а не подставляем умолчание молча — иначе наряд
    работает не так, как написано, и нигде об этом не говорит. `bool` отсеиваем отдельно:
    в Python он подкласс `int`, и `true` иначе прошло бы как «один запрос за раз»."""
    if raw is None:
        return DEFAULT_CONCURRENCY
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 1:
        warnings.append(
            f"поле «concurrency»: {raw!r} — ожидалось целое положительное число, "
            f"взято умолчание {DEFAULT_CONCURRENCY}"
        )
        return DEFAULT_CONCURRENCY
    return raw


def _prepare(profile: Profile, task_vars: dict[str, Any]) -> Profile:
    """Заготовка с подставленными `vars` задания — копией.

    Копия, а не правка на месте: заготовку читают все задания наряда, и дописанная в неё
    подстановка одного задания утекла бы во все остальные.

    Подстановка та же, что у профилей: `safe_substitute` по `$имя`. Неразрешённое имя
    остаётся в тексте как есть и загрузку не роняет — значит наряд волен подставлять только
    часть переменных заготовки, а фигурные скобки примера JSON внутри инструкции не
    трогаются вовсе."""
    if not task_vars:
        return profile
    merged = {**profile.vars, **task_vars}  # своё у задания перекрывает общее у заготовки
    system = profile.system
    if system:
        system = Substitution(system).safe_substitute(task_vars)
    return replace(profile, system=system, vars=merged)


def _task_from_dict(raw: Any, position: int, warnings: list[str]) -> Task | None:
    """Одно задание из наряда. `None` — задание непригодно, причина уже в предупреждениях.

    Мусор не роняет разбор целиком: непригодное задание пропускается, остальные читаются.
    Наряд из сотни заданий не должен пропадать из-за одной опечатки в пятидесятом."""
    if not isinstance(raw, dict):
        warnings.append(f"задание №{position}: ожидался объект JSON — пропущено")
        return None

    # Имя разбираем первым и молча: оно нужно как метка во всех остальных предупреждениях —
    # «задание №2 «немой»» пользователь найдёт в наряде глазами, а «задание №2» ему придётся
    # отсчитывать пальцем. Об отсутствии имени говорим ниже, когда задание уже признано
    # пригодным: у выброшенного задания имя роли не играет.
    raw_name = raw.get("agent")
    name = raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else ""
    метка = f"задание №{position}" + (f" «{name}»" if name else "")

    ask = raw.get("ask")
    if not isinstance(ask, str) or not ask.strip():
        warnings.append(f"{метка}: нет поля «ask» с вопросом — пропущено")
        return None

    if not name:
        # Имя — только метка в журнале и в сводке, ради неё работу не выбрасываем. Но и
        # молчать нельзя: без предупреждения пользователь не поймёт, откуда в сводке взялось
        # имя, которого он не писал.
        name = f"задание {position}"
        метка = f"задание №{position}"
        warnings.append(f"{метка}: нет имени в поле «agent» — названо «{name}»")

    profile_name = raw.get("profile")
    if not isinstance(profile_name, str) or not profile_name.strip():
        profile_name = profiles.DEFAULT_PROFILE_NAME
    profile_name = profile_name.strip()

    profile, profile_warnings = profiles.load(profile_name)
    for warning in profile_warnings:
        warnings.append(f"{метка}: {warning}")
    # Ровно та же проверка, что у группы агентов (`team.load_agents`): молча подставленный
    # `default` сделал бы собеседника безликим, а результат наряда необъяснимым — вместо
    # эксперта по физике ответил бы кто угодно, и по ответу этого не видно.
    if profile.name == profiles.DEFAULT_PROFILE_NAME and profile_name != profiles.DEFAULT_PROFILE_NAME:
        warnings.append(f"{метка} пропущено: профиль «{profile_name}» не найден")
        return None

    task_vars = raw.get("vars") or {}
    if not isinstance(task_vars, dict):
        warnings.append(f"{метка}: поле «vars» — не объект подстановок, пропущено")
        task_vars = {}

    return Task(
        agent=name,
        profile=profile_name,
        vars=dict(task_vars),
        ask=ask.strip(),
        prepared=_prepare(profile, task_vars),
    )


def load_order(path: Path) -> tuple[Order, list[str]]:
    """Наряд из файла плюс список предупреждений о пропущенном.

    Отсутствие файла и испорченный JSON наверх ПРОБРАСЫВАЮТСЯ (`OSError`,
    `json.JSONDecodeError`, `OrderError`): это отказ всего наряда, а не пропуск задания, и делать
    вид, что наряд прочитан и просто пуст, — значит врать вызывающему. Сообщение человеку
    складывает `main`, которому и положено разговаривать с оболочкой."""
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    warnings: list[str] = []
    if not isinstance(data, dict):
        raise OrderError("ожидался объект JSON с полями «model», «concurrency», «tasks»")

    model = data.get("model")
    if not isinstance(model, str) or not model.strip():
        model = config.DEFAULT_MODEL
    model = model.strip()

    raw_tasks = data.get("tasks")
    if raw_tasks is None:
        raw_tasks = []
    if not isinstance(raw_tasks, list):
        warnings.append("поле «tasks» — не список заданий, наряд пуст")
        raw_tasks = []

    tasks: list[Task] = []
    for position, raw in enumerate(raw_tasks, start=1):
        task = _task_from_dict(raw, position, warnings)
        if task is not None:
            tasks.append(task)

    return Order(model=model, concurrency=_concurrency(data.get("concurrency"), warnings), tasks=tasks), warnings


async def run_order(order: Order, client, *, on_line) -> list[Turn]:
    """Выполнить наряд. Возвращает по записи `Turn` на каждое задание, в порядке наряда.

    Ход работы уходит строками в `on_line`, а не печатается напрямую: печать привязала бы
    выполнение к стандартному выводу, и проверить `run_order` можно было бы только перехватом
    вывода процесса.

    Собеседник на каждое задание — свой: у него своя память и свой профиль. Клиент API при
    этом один на весь наряд, он приходит аргументом, — поэтому сто собеседников не означают
    ста сетевых соединений.

    `run_id` один на весь наряд: по нему в журнале потом видно, что эти записи — один прогон,
    а поле `agent` в каждой записи говорит, какое именно задание её оставило. Журнал пишет
    сам собеседник, здесь для этого делать нечего."""
    run_id = uuid4().hex[:8]
    # Ограничитель одновременности. Без него сотня заданий ушла бы в сеть разом и вернулась
    # отказами по превышению частоты — то есть наряд «выполнился» бы вхолостую.
    gate = asyncio.Semaphore(max(1, order.concurrency))
    total = len(order.tasks)

    async def one(position: int, task: Task) -> Turn:
        async with gate:
            on_line(f"[{position}/{total}] {task.agent} → {order.model}: {task.ask}")
            profile = task.prepared if task.prepared is not None else profiles.builtin_default()
            agent = Agent(task.agent, profile)
            turn = await agent.exchange(client, order.model, task.ask, agent=task.agent, run_id=run_id)
            if turn.ok:
                on_line(f"[{position}/{total}] {task.agent}: готово за {turn.elapsed_ms / 1000:.1f} с")
            else:
                on_line(f"[{position}/{total}] {task.agent}: СБОЙ — {turn.error or turn.status}")
            if turn.journal_error:
                on_line(f"[{position}/{total}] {task.agent}: {turn.journal_error}")
            return turn

    # `return_exceptions=True` — не украшение: без него первое же исключение отменило бы все
    # остальные задания наряда, и сбой одного собеседника стоил бы всей работы. Сам обмен
    # сетевые ошибки уже ловит и возвращает `Turn` со статусом «error»; сюда исключение
    # долетит только из нашего собственного кода, и тогда мы всё равно обязаны досчитать
    # наряд, а не оборвать его.
    results = await asyncio.gather(
        *(one(position, task) for position, task in enumerate(order.tasks, start=1)),
        return_exceptions=True,
    )
    turns: list[Turn] = []
    for result in results:
        if isinstance(result, BaseException):
            # Сюда попадает только сбой нашего собственного кода вокруг обмена — например
            # сорвавшийся `on_line` самого вызывающего. Докладывать о нём через `on_line`
            # нельзя: он-то и сорвался, и второй вызов сорвётся так же, но уже вне `gather`,
            # то есть уронит весь наряд на последнем шаге. Сбой уходит наверх записью `Turn`
            # и попадает в сводку — там его и видно.
            turns.append(Turn(status="error", error=str(result)))
        else:
            turns.append(result)
    return turns


def summary_lines(order: Order, turns: list[Turn]) -> list[str]:
    """Сводка по наряду — списком строк, готовых к печати.

    Строки, а не готовый текст: сводку одинаково надо и напечатать, и проверить построчно,
    и при желании положить в файл, — склеивать её умеет вызывающий."""
    lines = [f"Наряд: модель {order.model}, заданий {len(order.tasks)}, одновременно {order.concurrency}"]
    width = max((len(task.agent) for task in order.tasks), default=0)
    tokens_total = 0
    tokens_total_known = True
    ok_count = 0
    for task, turn in zip(order.tasks, turns, strict=True):
        # Sticky Facts оплачивает два запроса, поэтому его расход точен только при наличии
        # обоих серверных `usage`. Накопитель Agent содержит известную часть даже при обрыве,
        # но пакетная сводка не умеет подписывать частичную сумму и не вправе выдавать её за
        # полный расход. У остальных стратегий сохраняем прежнее правило по серверному
        # `usage`: оно важно и для результатов, созданных при внутреннем сбое до запуска
        # агента.
        facts = turn.context_strategy == context_strategy.CONTEXT_FACTS
        tokens_known = not facts or (
            bool(turn.usage) and turn.facts_usage is not None
        )
        tokens = turn.agent_session_tokens if facts else usage_tokens(turn.usage)
        tokens_total += tokens
        tokens_total_known = tokens_total_known and tokens_known
        tokens_text = (
            f"{tokens:6d} токенов" if tokens_known else "расход неизвестен"
        )
        if turn.ok:
            ok_count += 1
            state = "ок"
            tail = f"{turn.elapsed_ms / 1000:6.1f} с  {tokens_text}"
        else:
            state = "сбой"
            tail = (
                f"{turn.elapsed_ms / 1000:6.1f} с  {tokens_text}"
                f"  — {turn.error or turn.status}"
            )
        lines.append(f"  {task.agent.ljust(width)}  {state:4}  {tail}")
    total = (
        f"израсходовано {tokens_total} токенов"
        if tokens_total_known
        else "расход неизвестен"
    )
    lines.append(f"Итог: ответили {ok_count} из {len(order.tasks)}, {total}")
    return lines


async def main(args) -> int:
    """Точка входа режима. Код возврата: 0 — ответили все, 1 — хоть одно задание не ответило.

    Наряд читается ДО настроек намеренно: путь к файлу — единственное, что пользователь
    только что напечатал руками, и опечатка в нём должна называться опечаткой в пути, а не
    жалобой на отсутствующий ключ. Спрашивать ключ ради наряда, который всё равно нельзя
    выполнить, тоже незачем."""
    path = Path(args.batch).expanduser()
    try:
        order, warnings = load_order(path)
    except FileNotFoundError:
        print(f"наряд не найден: {path}", file=sys.stderr)
        return 1
    except OSError as exc:
        print(f"не удалось прочитать наряд {path}: {exc}", file=sys.stderr)
        return 1
    except json.JSONDecodeError as exc:
        print(f"наряд {path} — испорченный JSON: {exc}", file=sys.stderr)
        return 1
    except OrderError as exc:
        print(f"наряд {path}: {exc}", file=sys.stderr)
        return 1

    for warning in warnings:
        print(f"! {warning}", file=sys.stderr)
    if not order.tasks:
        print(f"в наряде {path} нет ни одного пригодного задания", file=sys.stderr)
        return 1

    if args.model:
        order.model = args.model
    if args.concurrency is not None:
        if args.concurrency < 1:
            print(f"--concurrency {args.concurrency}: ожидалось целое положительное число", file=sys.stderr)
            return 1
        order.concurrency = args.concurrency

    settings = config.load()
    if not settings.is_authorized:
        print(
            "нет ключа DeepSeek: пакетный режим не спрашивает его сам. "
            "Задайте ключ командой /auth в обычном режиме — он ляжет в "
            f"{config.config_path()}",
            file=sys.stderr,
        )
        return 1

    client = api.DeepSeekClient(settings.api_key)
    try:
        turns = await run_order(order, client, on_line=lambda line: print(line, flush=True))
    finally:
        await client.aclose()

    for line in summary_lines(order, turns):
        print(line)
    return 0 if all(turn.ok for turn in turns) else 1
