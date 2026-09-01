"""Описание параметров генерации DeepSeek: градации для меню выбора и правила применимости.

В меню попадают только параметры, которые API действительно принимает. `top_k`, `seed`,
`frequency_penalty` и `presence_penalty` у DeepSeek отсутствуют (последние два помечены в
документации как более не поддерживаемые) — ручек для них здесь нет намеренно: параметр,
который сервер молча выбрасывает, вводит в заблуждение.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

UNSET = object()  # «не задавать параметр» — сервер применит своё умолчание


@dataclass(frozen=True)
class Choice:
    value: Any
    label: str
    hint: str = ""


@dataclass(frozen=True)
class ParamSpec:
    name: str
    title: str
    description: str
    choices: tuple[Choice, ...]
    custom_hint: str = ""  # пусто — своё значение вводить нельзя
    parse: Callable[[str], Any] | None = None


def _parse_float(raw: str) -> float:
    return float(raw.replace(",", "."))


def _parse_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise ValueError("нужно положительное число")
    return value


def _parse_stop(raw: str) -> list[str]:
    parts = [p for p in (s.strip() for s in raw.split(",")) if p]
    if not parts:
        raise ValueError("пустой список")
    if len(parts) > 16:
        raise ValueError("DeepSeek принимает не больше 16 последовательностей")
    # \n в введённой строке — это два символа, превращаем в настоящий перевод строки
    return [p.replace("\\n", "\n") for p in parts]


SPECS: dict[str, ParamSpec] = {
    "temperature": ParamSpec(
        name="temperature",
        title="temperature",
        description="разброс выбора следующего слова: ниже — предсказуемее, выше — разнообразнее",
        choices=(
            Choice(UNSET, "не задавать", "умолчание API — 1.0"),
            Choice(0.0, "0.0 — детерминированный", "для строгих форматов и повторяемости"),
            Choice(0.2, "0.2 — точный", "рабочее значение для JSON-ответов"),
            Choice(0.7, "0.7 — сбалансированный", ""),
            Choice(1.3, "1.3 — творческий", "заметный разброс формулировок"),
        ),
        custom_hint="число от 0 до 2",
        parse=_parse_float,
    ),
    "top_p": ParamSpec(
        name="top_p",
        title="top_p",
        description="доля наиболее вероятных вариантов, из которых модель выбирает",
        choices=(
            Choice(UNSET, "не задавать", "умолчание API — 1.0"),
            Choice(0.1, "0.1 — только самые вероятные", ""),
            Choice(0.5, "0.5 — половина распределения", ""),
            Choice(0.9, "0.9 — почти всё распределение", ""),
            Choice(1.0, "1.0 — без отсечения", ""),
        ),
        custom_hint="число от 0 до 1",
        parse=_parse_float,
    ),
    "max_tokens": ParamSpec(
        name="max_tokens",
        title="max_tokens",
        description="жёсткий потолок длины ответа; обрывает генерацию на полуслове",
        choices=(
            Choice(UNSET, "не задавать", "модель заканчивает сама"),
            Choice(128, "128 — очень коротко", "для русского это примерно 40–60 слов"),
            Choice(400, "400 — короткий ответ", ""),
            Choice(800, "800 — средний ответ", "хватает на JSON из десятка полей"),
            Choice(2048, "2048 — развёрнутый ответ", ""),
        ),
        custom_hint="целое число токенов",
        parse=_parse_int,
    ),
    "stop": ParamSpec(
        name="stop",
        title="stop",
        description="последовательности, на которых генерация обрывается (до 16 штук); сама строка в ответ не попадает",
        choices=(
            Choice(UNSET, "не задавать", "модель заканчивает сама"),
            Choice(["###"], "###", "классический маркер конца"),
            Choice(["\n\n###"], "\\n\\n###", "маркер с отступом — меньше ложных срабатываний"),
            Choice(["</конец>"], "</конец>", "явный тег завершения"),
        ),
        custom_hint="через запятую; \\n — перевод строки",
        parse=_parse_stop,
    ),
    "response_format": ParamSpec(
        name="response_format",
        title="response_format",
        description="формат ответа; json_object гарантирует синтаксически валидный JSON",
        choices=(
            Choice(UNSET, "не задавать", "то же, что text"),
            Choice({"type": "text"}, "text — обычный текст", ""),
            Choice(
                {"type": "json_object"},
                "json_object — только JSON",
                "требует слова «json» и примера структуры в инструкции",
            ),
        ),
    ),
    "thinking": ParamSpec(
        name="thinking",
        title="thinking (рассуждения)",
        description="режим рассуждений; включён у DeepSeek по умолчанию и отключает влияние temperature и top_p",
        choices=(
            Choice(UNSET, "не задавать", "умолчание API — включены"),
            Choice({"type": "enabled"}, "enabled — показывать рассуждения", ""),
            Choice({"type": "disabled"}, "disabled — без рассуждений", "только так работают temperature и top_p"),
        ),
    ),
    "reasoning_effort": ParamSpec(
        name="reasoning_effort",
        title="reasoning_effort",
        description="сколько сил модель тратит на рассуждения (имеет смысл только при включённом thinking)",
        choices=(
            Choice(UNSET, "не задавать", "умолчание API — high"),
            Choice("low", "low — быстро и дёшево", ""),
            Choice("high", "high — обычный режим", ""),
            Choice("max", "max — максимально тщательно", "дороже и дольше"),
        ),
    ),
}

ORDER = ("temperature", "top_p", "max_tokens", "stop", "response_format", "thinking", "reasoning_effort")


def thinking_enabled(params: dict[str, Any]) -> bool:
    """Рассуждения включены, если явно не выключены: у DeepSeek это умолчание для всех моделей."""
    thinking = params.get("thinking")
    if isinstance(thinking, dict):
        return thinking.get("type") != "disabled"
    return True


def inapplicable_reason(name: str, params: dict[str, Any]) -> str | None:
    """Почему параметр сейчас ни на что не влияет (None — влияет)."""
    if name in ("temperature", "top_p") and thinking_enabled(params):
        return "игнорируется API, пока включены рассуждения (thinking)"
    if name == "reasoning_effort" and not thinking_enabled(params):
        return "имеет смысл только при включённых рассуждениях (thinking)"
    return None


def format_value(value: Any) -> str:
    if value is None:
        return "не задан"
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)
