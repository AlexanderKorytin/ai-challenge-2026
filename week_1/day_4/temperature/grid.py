"""Прогон одной клетки сетки «вопрос × температура × повтор» и запись результата.

Клетка — это один запрос к API: заданный вопрос, заданная температура, заданный номер
повтора. Модуль умеет ровно две вещи: сходить в API за ответом на клетку и дописать
итог в журнал прогонов, откуда его потом читают страница сравнения и разбор.

Ключ доступа сюда не попадает: клиент приходит готовым от вызывающего, а тот берёт
ключ из настроек инструмента (`myharness.config.load()`). В журнал ключ попасть не может,
потому что писать в него нечего — модуль его не видит.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import subprocess
import time
from collections.abc import Callable
from contextlib import aclosing
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from temperature.questions import TEMPERATURES, Question, extract_value

# --- настройки прогона --------------------------------------------------------

#: Модель по умолчанию — то же значение, что у инструмента (`myharness/config.py`).
MODEL = "deepseek-v4-flash"

#: Потолок длины ответа. Общий на все клетки: иначе длинные ответы при высокой
#: температуре обрывались бы, а при низкой нет, и сравнивать было бы нечего.
MAX_TOKENS = 1024

#: Основа параметров запроса. Рассуждения выключены намеренно: при включённых
#: рассуждениях сервер игнорирует `temperature`, и весь замер теряет смысл.
#: Наружу раздаётся только копиями (`cell_params`): вложенный `thinking` — общий по ссылке
#: словарь, и попади он в каждую запись журнала как есть, одна случайная правка на любой
#: клетке задним числом переписала бы параметры всего прогона.
PARAMS_BASE: dict[str, Any] = {"max_tokens": MAX_TOKENS, "thinking": {"type": "disabled"}}

#: Журнал прогонов: одна строка JSON на клетку. Путь абсолютный, считается от расположения
#: модуля, а не от места запуска: живой прогон ведётся из песочницы `test/`, а журнал должен
#: лечь туда же, где его ищут разбор и страница сравнения, — в `day_4/data/`.
DATA_PATH = Path(__file__).resolve().parent.parent / "data" / "grid.jsonl"

#: Сколько запросов к API держим в воздухе одновременно.
CONCURRENCY = 4

#: Паузы перед второй и последующими попытками, секунды.
ПАУЗЫ_ПОПЫТОК = (1, 2)

#: Сколько раз пробуем клетку, прежде чем признать её проваленной. Выведено из числа пауз:
#: первая попытка идёт без паузы, каждая следующая — со своей. Связывать эти две величины
#: вручную нельзя: правка одной дала бы `IndexError` на последней попытке, и вылезло бы это
#: только на живом прогоне, посреди оплаченных запросов.
ATTEMPTS = len(ПАУЗЫ_ПОПЫТОК) + 1

#: Текст ошибки для клетки, в которую вопреки настройке пришли рассуждения.
ОШИБКА_РАССУЖДЕНИЙ = "пришли рассуждения при выключенном thinking"

#: Сюда уходят жалобы на сбой записи и показа: молча терять их нельзя, а поднимать выше —
#: значит ронять прогон из-за того, что кто-то закрыл браузер.
ЖУРНАЛ_СООБЩЕНИЙ = logging.getLogger(__name__)


# --- итог одной клетки --------------------------------------------------------


@dataclass
class CellResult:
    """Что вышло из одной клетки сетки.

    Изменяемый (в отличие от `Question`) намеренно: значение копится по ходу потока
    событий, и заводить новый объект на каждый кусок ответа было бы расточительно.
    """

    question_id: str
    temperature: float
    repetition: int
    answer: str = ""
    value: str | None = None
    finish_reason: str | None = None
    usage: dict = field(default_factory=dict)
    elapsed_ms: int = 0
    status: str = "ok"  # "ok" | "error"
    error: str | None = None


# --- имена и служебные сведения -----------------------------------------------


def run_key(question_id: str, temperature: float, repetition: int) -> str:
    """Имя клетки вида `poet_t0.7_r2`.

    Температура печатается с одним знаком после точки всегда, даже нулевая: иначе
    `0.0` и `0` дали бы два разных имени одной и той же клетке.
    """
    return f"{question_id}_t{temperature:.1f}_r{repetition}"


@lru_cache(maxsize=1)
def code_version() -> str:
    """Короткая фиксация git, на которой сделан прогон, или `"unknown"`.

    Записи живут дольше памяти о них: через месяц по журналу надо понять, каким кодом
    он собран. Ответ запоминается: за один прогон код не меняется, а вызывать git на
    каждую из полутора сотен записей незачем.

    Любой сбой (нет git, каталог не репозиторий, пустой репозиторий) — это `"unknown"`,
    а не исключение: отсутствие приписки не повод терять собранные данные.
    """
    try:
        готово = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=Path(__file__).resolve().parent,
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return "unknown"
    if готово.returncode != 0:
        return "unknown"
    return готово.stdout.strip() or "unknown"


# --- запись в журнал ----------------------------------------------------------


def build_record(result: CellResult, question: Question, model: str, params: dict) -> dict:
    """Словарь одной строки журнала.

    Запись самодостаточна: в ней лежит полный текст вопроса, модель, параметры запроса и
    фиксация кода. Читать её можно, не имея под рукой ни этого модуля, ни набора вопросов
    той версии, на которой прогон делался.
    """
    return {
        "run_key": run_key(result.question_id, result.temperature, result.repetition),
        "question_id": result.question_id,
        "question_text": question.text,
        "temperature": result.temperature,
        "repetition": result.repetition,
        "timestamp": datetime.now(UTC).isoformat(),
        "model": model,
        "params": params,
        "answer": result.answer,
        "value": result.value,
        "finish_reason": result.finish_reason,
        "usage": result.usage,
        "elapsed_ms": result.elapsed_ms,
        "status": result.status,
        "error": result.error,
        "code_version": code_version(),
    }


def append_record(record: dict, path: Path = DATA_PATH) -> None:
    """Дописывает одну строку JSON в журнал, заводя недостающие каталоги.

    Русский текст сохраняется как есть (`ensure_ascii=False`): журнал читают глазами, а
    экранированные последовательности вида `\\u043e` читать невозможно.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as файл:
        файл.write(json.dumps(record, ensure_ascii=False) + "\n")


def collected_repeats(path: Path = DATA_PATH) -> dict[str, int]:
    """По каждому вопросу — наибольший номер повтора среди **всех** его записей.

    Это ответ на вопрос «с какого номера продолжать»: следующий прогон начинается с
    `значение + 1`. Статус записи здесь не смотрится намеренно: номер, по которому в
    журнал уже писалось, занят навсегда. Считай мы занятыми только успешные номера,
    целиком упавший последний прогон — обрыв связи под конец, обычное дело — освободил бы
    свой номер, следующее нажатие кнопки выдало бы тот же, и в журнале появились бы две
    записи с одним `run_key`. Ключ перестал бы быть именем клетки, а разбор молча терял бы
    одну из двух записей, причём какую — как повезёт с порядком обхода.

    Размен принят сознательно: целиком провалившийся прогон держит свой номер навсегда, и
    строка таблицы останется неполной. Дырка в таблице видна человеку и объясняется
    записями об ошибке, а две записи с одним ключом не видны никому.

    Отбор идёт по пригодности записи к разбору, а не по её статусу: пропускается всё, из
    чего нельзя достать пару «вопрос — номер», — не JSON, не словарь, нестроковый
    `question_id`, нецелый или отсутствующий `repetition`. Испорченная строка не роняет
    разбор файла: журнал дописывается по ходу живого прогона, и оборванная на полуслове
    последняя строка — обычное дело.

    Вопрос, о котором в журнале нет ни одной пригодной записи, в словаре отсутствует.
    """
    path = Path(path)
    if not path.exists():
        return {}

    собрано: dict[str, int] = {}
    with path.open(encoding="utf-8") as файл:
        for строка in файл:
            строка = строка.strip()
            if not строка:
                continue
            try:
                запись = json.loads(строка)
            except json.JSONDecodeError:
                continue
            if not isinstance(запись, dict):
                continue
            идентификатор = запись.get("question_id")
            повтор = запись.get("repetition")
            # `bool` — подкласс `int`, а `True` в роли номера повтора это мусор, а не единица.
            if not isinstance(идентификатор, str) or not isinstance(повтор, int):
                continue
            if isinstance(повтор, bool):
                continue
            if повтор > собрано.get(идентификатор, 0):
                собрано[идентификатор] = повтор
    return собрано


# --- прогон -------------------------------------------------------------------


def cell_params(temperature: float) -> dict[str, Any]:
    """Своя копия параметров запроса для клетки: общая основа плюс своя температура.

    Копия глубокая: `|` копирует только верхний уровень, и вложенный `thinking` остался бы
    общим для всех клеток и всех записей журнала. Клетка не должна иметь возможности
    испортить прогон соседке.
    """
    return copy.deepcopy(PARAMS_BASE) | {"temperature": temperature}


async def run_cell(client, question: Question, temperature: float, repetition: int) -> CellResult:
    """Один запрос к API: ответ, сравнимое значение, расход и время.

    Устройство: до `ATTEMPTS` попыток с паузами `ПАУЗЫ_ПОПЫТОК`. Каждая попытка заводит
    свой пустой итог — куски прошлой, оборванной попытки не должны склеиваться с новым
    ответом. Время меряется от отправки до последнего события каждой попытки отдельно:
    в записи стоит длительность той попытки, чей ответ и сохранён.

    Исключение наружу не выходит: исчерпав попытки, клетка возвращается со `status="error"`
    и текстом исключения. Одна упавшая клетка не должна ронять прогон всей сетки — иначе
    единственный сбой сети стоил бы всех уже оплаченных запросов.
    """
    последняя_ошибка = ""
    for попытка in range(ATTEMPTS):
        if попытка:
            await asyncio.sleep(ПАУЗЫ_ПОПЫТОК[попытка - 1])

        итог = CellResult(
            question_id=question.id, temperature=temperature, repetition=repetition
        )
        куски: list[str] = []
        рассуждения = False
        начало = time.perf_counter()
        try:
            поток = client.stream_chat(
                MODEL,
                [{"role": "user", "content": question.text}],
                cell_params(temperature),
            )
            # `aclosing` закрывает поток явно, если попытка оборвалась исключением. Иначе
            # закрытие достаётся сборщику мусора, и в stderr сыплются жалобы на незакрытые
            # асинхронные генераторы — этот шум описан прямо в `myharness/api.py`.
            async with aclosing(поток) as события:
                async for событие in события:
                    if событие.kind == "content":
                        куски.append(событие.text)
                    elif событие.kind == "reasoning":
                        рассуждения = True
                    elif событие.kind == "meta":
                        итог.finish_reason = событие.finish_reason
                        итог.usage = событие.usage
        except Exception as исключение:  # noqa: BLE001 — сбой клетки не роняет прогон
            последняя_ошибка = f"{type(исключение).__name__}: {исключение}"
            continue

        итог.elapsed_ms = int((time.perf_counter() - начало) * 1000)
        итог.answer = "".join(куски)
        итог.value = extract_value(question, итог.answer)
        if рассуждения:
            # Рассуждения при выключенном `thinking` означают, что настройка не
            # применилась, а без неё сервер игнорирует температуру: замер бессмыслен.
            # Ответ всё же сохраняем — по нему видно, что именно пришло.
            итог.status = "error"
            итог.error = ОШИБКА_РАССУЖДЕНИЙ
        return итог

    return CellResult(
        question_id=question.id,
        temperature=temperature,
        repetition=repetition,
        status="error",
        error=последняя_ошибка,
    )


async def run_question(
    client,
    question: Question,
    start_repetition: int,
    count: int,
    on_result: Callable[[CellResult], None],
    path: Path = DATA_PATH,
) -> None:
    """Прогон вопроса: `count` повторов на каждую из температур, все сразу.

    Одновременность ограничена `asyncio.Semaphore(CONCURRENCY)`: клеток в сетке заметно
    больше, чем разумно держать запросов в воздухе. Готовый результат сначала дописывается
    в журнал, потом уходит в `on_result` (обычная функция, не сопрограмма) — так прогон
    можно продолжить с места обрыва, а не начинать заново.

    Порядок «сначала диск, потом показ» не случаен: клетка, показанная страницей, обязана
    уже лежать в журнале. Обратный порядок при отказе записи оставил бы на экране клетку,
    которой в данных нет, — и разбор расходился бы с тем, что человек видел своими глазами.

    Ни запись, ни показ не могут уронить прогон: обе беды записываются в
    `ЖУРНАЛ_СООБЩЕНИЙ` и остаются внутри своей клетки. `on_result` в шаге 6 — это отправка
    в поток событий страницы, и она падает от простого закрытия браузера; уронив на этом
    прогон, мы бы выбросили уже оплаченные ответы и продолжили платить за оставшиеся
    клетки, которые `asyncio.gather` без `return_exceptions` не отменяет, а бросает
    доигрывать в одиночестве. `return_exceptions=True` стоит вторым рубежом — на случай
    беды, о которой здесь не подумали.
    """
    ограничитель = asyncio.Semaphore(CONCURRENCY)

    async def клетка(temperature: float, repetition: int) -> None:
        async with ограничитель:
            итог = await run_cell(client, question, temperature, repetition)

        имя = run_key(question.id, temperature, repetition)
        try:
            append_record(build_record(итог, question, MODEL, cell_params(temperature)), path)
        except Exception:  # noqa: BLE001 — отказ записи не отменяет оплаченную работу
            ЖУРНАЛ_СООБЩЕНИЙ.exception("клетка %s не записана в журнал", имя)
        try:
            on_result(итог)
        except Exception:  # noqa: BLE001 — отказ показа не отменяет оплаченную работу
            ЖУРНАЛ_СООБЩЕНИЙ.exception("обработчик клетки %s отказал", имя)

    await asyncio.gather(
        *(
            клетка(температура, повтор)
            for повтор in range(start_repetition, start_repetition + count)
            for температура in TEMPERATURES
        ),
        return_exceptions=True,
    )
