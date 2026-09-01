"""Схема факта о рыбе и разбор ответа модели.

Проверка нарочно строгая: числа обязаны быть числами, лишние поля запрещены. Смысл дня
второго — измерить, насколько модель держит форму, а мягкий разбор («40» сойдёт за 40)
как раз и скрыл бы то, что мы измеряем.

DeepSeek в chat/completions поддерживает только `response_format: json_object` — гарантию
валидного JSON, но не схемы. Поэтому схему проверяем у себя.
"""

from __future__ import annotations

import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, StrictFloat, StrictInt, StrictStr, ValidationError

Number = StrictFloat | StrictInt


class Range(BaseModel):
    """Диапазон значения: числами и только числами."""

    model_config = ConfigDict(extra="forbid")

    min: Number | None = None
    max: Number | None = None


class FishFact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok", "not_a_fish"]
    reason: StrictStr | None = None
    name: StrictStr | None = None
    scientific_name: StrictStr | None = None
    length_cm: Range | None = None
    weight_kg: Range | None = None
    habitat: list[StrictStr] = []
    diet: list[StrictStr] = []
    summary: StrictStr | None = None


FENCE_MARKERS = ("```json", "```JSON", "```")


def strip_fence(text: str) -> str:
    """Снимает обрамление блоком кода, если модель всё же его добавила."""
    cleaned = text.strip()
    for marker in FENCE_MARKERS:
        if cleaned.startswith(marker):
            cleaned = cleaned[len(marker) :]
            break
    if cleaned.endswith("```"):
        cleaned = cleaned[:-3]
    return cleaned.strip()


class Parsed(BaseModel):
    """Итог разбора одного ответа — из этого потом складываются метрики."""

    model_config = ConfigDict(arbitrary_types_allowed=True)

    raw: str
    raw_is_json: bool  # сырой ответ разобрался без чистки
    is_json: bool  # разобрался хотя бы после снятия обрамления
    needed_cleanup: bool
    schema_ok: bool
    errors: list[str] = []
    data: dict | None = None
    fact: FishFact | None = None

    @property
    def shape(self) -> str:
        """Подпись формы ответа: набор путей до полей. Одинаковая подпись — одинаковая структура."""
        if self.data is None:
            return "—"
        return ",".join(sorted(_paths(self.data)))


def _paths(value: object, prefix: str = "") -> list[str]:
    if isinstance(value, dict):
        out: list[str] = []
        for key, item in value.items():
            path = f"{prefix}.{key}" if prefix else str(key)
            nested = _paths(item, path)
            out.extend(nested or [path])
        return out
    if isinstance(value, list):
        return [f"{prefix}[]"]
    return [prefix]


def parse(text: str) -> Parsed:
    raw = text or ""
    raw_is_json = False
    data: dict | None = None
    errors: list[str] = []

    try:
        candidate = json.loads(raw)
        raw_is_json = isinstance(candidate, dict)
        if raw_is_json:
            data = candidate
    except json.JSONDecodeError:
        pass

    needed_cleanup = False
    if data is None:
        cleaned = strip_fence(raw)
        if cleaned != raw.strip():
            needed_cleanup = True
        try:
            candidate = json.loads(cleaned)
            if isinstance(candidate, dict):
                data = candidate
        except json.JSONDecodeError as exc:
            errors.append(f"не разобрался как JSON: {exc.msg} (позиция {exc.pos})")

    if data is None:
        return Parsed(
            raw=raw, raw_is_json=False, is_json=False, needed_cleanup=needed_cleanup, schema_ok=False, errors=errors
        )

    fact: FishFact | None = None
    try:
        fact = FishFact.model_validate(data)
    except ValidationError as exc:
        for error in exc.errors():
            location = ".".join(str(part) for part in error["loc"]) or "<корень>"
            errors.append(f"{location}: {error['msg']}")

    return Parsed(
        raw=raw,
        raw_is_json=raw_is_json,
        is_json=True,
        needed_cleanup=needed_cleanup,
        schema_ok=fact is not None,
        errors=errors,
        data=data,
        fact=fact,
    )


def summary_words(fact: FishFact | None) -> int:
    return len((fact.summary or "").split()) if fact else 0
