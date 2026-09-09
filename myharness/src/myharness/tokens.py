"""Счёт токенов и денег: сколько запрос весит до отправки и во что обошёлся после.

Зачем: до отправки harness обязан сказать, влезает ли разговор в окно модели и сколько
примерно будет стоить, — иначе о переполнении контекста узнаёшь по отказу сервера, а о
цене по счёту в конце месяца. После ответа тот же модуль превращает `usage` от DeepSeek
в деньги и складывает расход за сеанс.

Счёт до отправки — предсказание, а не истина: точный итог знает только сервер. Поэтому
здесь два пути. Точный — настоящим словарём DeepSeek через библиотеку `tokenizers`.
Запасной — по числу знаков, если словаря или библиотеки нет. Отсутствие словаря не ошибка:
инструмент обязан работать и без него, просто честно сообщая, что счёт приблизительный
(`exact()`).
"""

from __future__ import annotations

import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

# Замерено 2026-09-09 на четырёх запросах к deepseek-v4-pro: пустой запрос из одной реплики
# сервер оценил в 83 токена — это обёртка разговора, которую модель добавляет сама.
BASE_OVERHEAD = 83
# Там же: четыре реплики дали 87 вместо 83, то есть примерно полтора токена на каждую
# реплику сверх первой (разделители ролей).
PER_MESSAGE_OVERHEAD = 1.5
# Запасной путь: сколько знаков приходится на токен. Замеры настоящим словарём по видам
# текста: русский разговорный 3.50, русский технический 3.52, русская строка документации
# 3.21, код Python 3.28–3.85, английский 5.39–6.08. Взято 3.0 — ниже всех замеров, а не
# среднее по ним. Причина: по запасному счёту принимается решение «влезет ли запрос в окно»,
# и ошибки тут несимметричны. Занизить счёт значит отправить запрос, который сервер отвергнет
# по переполнению, — ответа не будет вовсе. Завысить значит обрезать разговор чуть раньше,
# чем было необходимо, — дёшево. Поэтому делитель занижен намеренно.
FALLBACK_CHARS_PER_TOKEN = 3.0

DEFAULT_VOCABULARY = "deepseek_v4_tokenizer.json"

# Загруженный словарь держим в модульной переменной вместе с путём, из которого он взят:
# разбор шестимегабайтного файла стоит заметного времени, а путь может смениться прямо по
# ходу работы ($MYHARNESS_TOKENIZER). Пара «путь → словарь» позволяет и не грузить дважды,
# и не отдать словарь от прежнего пути.
_vocabulary_cache: tuple[Path, Any] | None = None


def vocabulary_path() -> Path:
    """Где искать словарь: переопределение из окружения либо файл рядом с модулем.

    Переменную читаем при каждом обращении, а не один раз при загрузке модуля: проверки
    подменяют путь на ходу, да и пользователю удобнее задать свой словарь без перезапуска.

    Путь приводим к абсолютному (`resolve`) именно здесь, потому что он служит ключом
    хранения загруженного словаря. Относительный путь при смене рабочего каталога указывает
    на другой файл, оставаясь той же строкой, — и `exact()` уверял бы, что счёт точный,
    когда файла по новому месту уже нет."""
    override = os.environ.get("MYHARNESS_TOKENIZER")
    if override:
        return Path(override).expanduser().resolve()
    return (Path(__file__).parent / "data" / DEFAULT_VOCABULARY).resolve()


def _vocabulary() -> Any | None:
    """Словарь для точного счёта либо None, если точного пути нет.

    Ни отсутствие библиотеки, ни отсутствие файла, ни испорченный файл ошибкой не считаются:
    счёт токенов — удобство, а не условие работы, и ронять из-за него весь harness нельзя."""
    global _vocabulary_cache
    target = vocabulary_path()
    if _vocabulary_cache is not None and _vocabulary_cache[0] == target:
        return _vocabulary_cache[1]
    loaded: Any | None = None
    try:
        from tokenizers import Tokenizer  # локальный ввоз: без библиотеки модуль всё равно жив

        loaded = Tokenizer.from_file(str(target))
    except Exception:  # noqa: BLE001 — годится любая причина: нет библиотеки, нет файла, битый файл
        loaded = None
    # Неудачу запоминаем тоже: иначе каждый вызов заново ищет отсутствующий файл.
    _vocabulary_cache = (target, loaded)
    return loaded


def exact() -> bool:
    """Точен ли счёт: словарь и библиотека на месте. Показу нужно, чтобы поставить «≈»."""
    return _vocabulary() is not None


def count_text(text: str) -> int:
    """Сколько токенов в тексте — точно словарём либо на глазок по числу знаков."""
    if not text:
        return 0
    vocabulary = _vocabulary()
    if vocabulary is not None:
        # Служебные метки не добавляем: обёртку разговора считает count_messages,
        # иначе она была бы учтена дважды.
        return len(vocabulary.encode(text, add_special_tokens=False).ids)
    # Непустой текст не может стоить ноль токенов: «да» короче делителя, но в запрос
    # попадёт и место займёт. Ноль здесь поехал бы и в предсказание переполнения, и в цену.
    return max(1, int(len(text) / FALLBACK_CHARS_PER_TOKEN))


def count_messages(messages: list[dict], *, overhead: int = BASE_OVERHEAD) -> int:
    """Вес запроса целиком: текст всех реплик плюс обёртка разговора.

    Пустой список даёт ровно базовую надбавку, а не «минус одну реплику»: отрицательная
    поправка занизила бы предсказание там, где оно и так приблизительное.

    Дробную сумму отсекаем вниз (`int`), а не округляем вверх: живой замер дал ровно 87 на
    четырёх репликах, а `math.ceil` дал бы 88. Число обязано сходиться с замером — не менять
    на «более правильное» округление.

    Ждём список (или иную последовательность) словарей. Генератор передавать НЕЛЬЗЯ: те же
    реплики уходят потом в модель, а генератор одноразовый — счёт опустошил бы его, и запрос
    ушёл бы пустым. Поэтому последовательность здесь принципиально не материализуется, а всё
    прочее (генератор, None, число) считается пустым списком. Это сеть, а не поддерживаемый
    путь: получить в ответ базовую надбавку вместо настоящего веса — уже ошибка, просто не
    портящая данные вызывающего."""
    # Строку отсекаем наравне с не-последовательностями: строка — тоже Sequence, и "абвгд"
    # дал бы 89 — правдоподобное число из ничего, худший вид неверного ответа.
    items: Sequence[Any]
    if isinstance(messages, Sequence) and not isinstance(messages, (str, bytes, bytearray)):
        items = messages
    else:
        # Счёт — удобство, ронять из-за него harness нельзя.
        items = []
    total = float(overhead) + PER_MESSAGE_OVERHEAD * max(0, len(items) - 1)
    for message in items:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        # Содержимое бывает списком частей (модели с картинками) или вовсе отсутствует —
        # такую реплику считаем пустой, но реплику как таковую учитываем.
        if isinstance(content, str):
            total += count_text(content)
    return int(total)


@dataclass(frozen=True)
class Tariff:
    """Цена в долларах за миллион токенов. Значения — для льготных часов, вне пика."""

    cache_hit: float
    cache_miss: float
    output: float


# Снято 2026-09-09 с https://api-docs.deepseek.com/quick_start/pricing
TARIFFS: dict[str, Tariff] = {
    "deepseek-v4-flash": Tariff(0.007, 0.22, 0.66),
    "deepseek-v4-pro": Tariff(0.022, 0.66, 1.98),
    "deepseek-v4-flash-vision-exp": Tariff(0.007, 0.22, 0.66),
}

# Часы UTC по будням, в которые тариф вдвое дороже. Границы — полуинтервал [начало, конец):
# час 4 уже вне окна (1, 4). Оттуда же, со страницы цен.
PEAK_WINDOWS = ((1, 4), (6, 10))
PEAK_MULTIPLIER = 2.0

CONTEXT_WINDOW = 1_000_000  # окно моделей v4, оттуда же
MAX_OUTPUT = 393_216  # предел max_tokens, подтверждён отказом сервера

UNKNOWN_PRICE = "тариф неизвестен"


def peak(moment: datetime) -> bool:
    """Попадает ли момент в дорогие часы: будний день и час внутри одного из окон.

    Окна заданы в UTC, поэтому момент с поясом сперва переводим. Без перевода 05:00 по
    Москве (это 02:00 UTC) прочиталось бы как час 5 — вне окна, — и цена вышла бы вдвое
    меньше настоящей. Наивный момент считаем уже заданным в UTC: иначе пришлось бы гадать
    о поясе, а гадание в счёте денег хуже уговора."""
    if moment.tzinfo is not None:
        moment = moment.astimezone(UTC)
    if moment.weekday() >= 5:  # суббота и воскресенье целиком льготные
        return False
    return any(start <= moment.hour < end for start, end in PEAK_WINDOWS)


def price(usage: dict, model: str, moment: datetime | None = None) -> float | None:
    """Во что обошёлся обмен, в долларах. None — тариф модели неизвестен.

    Именно None, а не ноль: ноль на экране читается как «бесплатно», и пользователь
    узнает правду только из счёта. Показ обязан сказать «тариф неизвестен»."""
    tariff = TARIFFS.get(model)
    if tariff is None:
        return None
    counts = normalize(usage)
    hit = counts["prompt_cache_hit_tokens"]
    miss = counts["prompt_cache_miss_tokens"]
    # Разбивка входа годна, только если её слагаемые дают весь вход. Проверять наличие
    # ключей мало: поле может прийти строкой, прийти одно из двух или оказаться нулевым
    # при непустом запросе — и тогда часть входа (а то и весь) выпала бы из счёта.
    # Расхождение любого рода трактуем в одну сторону — весь вход промах: завысить оценку
    # безопаснее, чем занизить. За величину входа берём большее из двух: испортиться может и
    # само `prompt_tokens` (прийти строкой или не прийти вовсе), и тогда верная разбивка
    # 400 + 600 обнулилась бы в счёте — ровно то занижение, от которого правило и заведено.
    if hit + miss != counts["prompt_tokens"]:
        hit, miss = 0, max(counts["prompt_tokens"], hit + miss)
    # Выход берём целиком по completion_tokens: у DeepSeek рассуждения уже входят в него
    # и оплачиваются по той же цене — складывать их отдельно значило бы посчитать дважды.
    dollars = (
        hit * tariff.cache_hit + miss * tariff.cache_miss + counts["completion_tokens"] * tariff.output
    ) / 1_000_000
    if peak(moment if moment is not None else datetime.now(UTC)):
        dollars *= PEAK_MULTIPLIER
    return dollars


def format_price(dollars: float | None) -> str:
    """Цена для экрана. Принимает и None — ровно то, что отдаёт `price` у незнакомой модели:
    пара функций задумана вместе, и разбирать этот случай на каждой стороне ни к чему.

    Мелочь показываем центами: «$0.0004» глаз читает как ноль. Но и в центах есть дно —
    ниже сотой цента печатаем «< 0.01 ¢», а не «0.00 ¢»: иначе беда, от которой заведена
    функция, возвращается, лишь сдвинувшись на два разряда."""
    if dollars is None:
        return UNKNOWN_PRICE
    if dollars >= 0.01:
        return f"${dollars:.2f}"
    if 0 < dollars < 0.0001:  # меньше сотой цента — до второго знака в центах не видно
        return "< 0.01 ¢"
    return f"{dollars * 100:.2f} ¢"


FIELDS = (
    "prompt_tokens",
    "completion_tokens",
    "total_tokens",
    "prompt_cache_hit_tokens",
    "prompt_cache_miss_tokens",
    "reasoning_tokens",
)


def _as_int(value: Any) -> int:
    """Число или ноль. bool отсеиваем отдельно: в Python True — это единица."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def normalize(usage: dict) -> dict[str, int]:
    """Плоский словарь из `usage` DeepSeek — единственное место, где разбирается вложенность.

    Возвращает все поля разом, недостающие нулями: у накопителя расхода должна быть одна
    и та же форма, иначе показ обрастает проверками «а есть ли такой ключ».
    Нечисловые значения отбрасываются — `usage` приходит от сервера, и падать на нём нельзя."""
    result = {field: 0 for field in FIELDS}
    if not isinstance(usage, dict):
        return result
    for field in FIELDS:
        if field in usage:
            result[field] = _as_int(usage[field])
    details = usage.get("completion_tokens_details")
    if isinstance(details, dict):
        result["reasoning_tokens"] = _as_int(details.get("reasoning_tokens"))
    # Итог сервер присылает не всегда (обрыв потока, отказ на середине). Собираем его сами:
    # иначе расход за сеанс покажет ноль при непустых слагаемых, и человек решит, что
    # обмена не было.
    if result["total_tokens"] == 0:
        result["total_tokens"] = result["prompt_tokens"] + result["completion_tokens"]
    return result


def add_usage(acc: dict[str, int], usage: dict) -> dict[str, int]:
    """Накопитель плюс один обмен. Возвращает НОВЫЙ словарь: накопитель мог уже попасть
    в журнал или на экран, и правка на месте изменила бы там числа задним числом.

    Собирается строго по `FIELDS`: посторонние ключи накопителя в результат не переходят.
    Это намеренно — у накопителя одна и та же форма, и подмешивать в него что-то своё,
    рассчитывая, что оно доживёт до конца сеанса, не выйдет."""
    addition = normalize(usage)
    return {field: _as_int(acc.get(field)) + addition[field] for field in FIELDS}


def total_usage(items: Iterable[dict]) -> dict[str, int]:
    """Расход за несколько обменов одной суммой."""
    result = {field: 0 for field in FIELDS}
    for item in items:
        result = add_usage(result, item)
    return result
