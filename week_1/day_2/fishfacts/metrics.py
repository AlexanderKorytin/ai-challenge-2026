"""Метрики одного прогона и сводка по серии.

Меряем форму ответа, а не его правдивость: держит ли модель структуру, типы и длину.
Достоверность фактов о рыбе наш валидатор проверить не может и не пытается.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass, field
from typing import Any

from .schema import Parsed, summary_words


@dataclass
class Run:
    """Итог одного запроса."""

    profile: str
    query: str
    index: int
    raw_is_json: bool = False
    is_json: bool = False
    needed_cleanup: bool = False
    schema_ok: bool = False
    shape: str = "—"
    status: str | None = None
    summary_words: int = 0
    finish_reason: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    elapsed_ms: int = 0
    errors: list[str] = field(default_factory=list)
    error: str | None = None
    text: str = ""

    @classmethod
    def from_parsed(
        cls,
        profile: str,
        query: str,
        index: int,
        parsed: Parsed,
        finish_reason: str | None,
        usage: dict[str, Any],
        elapsed_ms: int,
    ) -> "Run":
        return cls(
            profile=profile,
            query=query,
            index=index,
            raw_is_json=parsed.raw_is_json,
            is_json=parsed.is_json,
            needed_cleanup=parsed.needed_cleanup,
            schema_ok=parsed.schema_ok,
            shape=parsed.shape,
            status=(parsed.fact.status if parsed.fact else (parsed.data or {}).get("status")),
            summary_words=summary_words(parsed.fact),
            finish_reason=finish_reason,
            prompt_tokens=usage.get("prompt_tokens"),
            completion_tokens=usage.get("completion_tokens"),
            elapsed_ms=elapsed_ms,
            errors=parsed.errors[:5],
            text=parsed.raw,
        )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _share(count: int, total: int) -> float:
    return round(count / total, 3) if total else 0.0


@dataclass
class Summary:
    profile: str
    query: str
    runs: int
    json_share: float
    clean_json_share: float  # разобрался без снятия обрамления
    schema_share: float
    shapes: dict[str, int]
    distinct_shapes: int
    finish_reasons: dict[str, int]
    statuses: dict[str, int]
    avg_completion_tokens: float
    max_summary_words: int
    avg_elapsed_ms: int
    failures: list[str]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def summarize(profile: str, query: str, runs: list[Run]) -> Summary:
    total = len(runs)
    shapes = Counter(run.shape for run in runs)
    failures: list[str] = []
    for run in runs:
        if run.error:
            failures.append(f"#{run.index}: {run.error}")
        elif not run.schema_ok:
            reason = run.errors[0] if run.errors else "ответ не разобрался как JSON"
            failures.append(f"#{run.index}: {reason}")
    return Summary(
        profile=profile,
        query=query,
        runs=total,
        json_share=_share(sum(run.is_json for run in runs), total),
        clean_json_share=_share(sum(run.raw_is_json for run in runs), total),
        schema_share=_share(sum(run.schema_ok for run in runs), total),
        shapes=dict(shapes.most_common()),
        distinct_shapes=len(shapes),
        finish_reasons=dict(Counter(run.finish_reason or "—" for run in runs)),
        statuses=dict(Counter(run.status or "—" for run in runs)),
        avg_completion_tokens=round(
            sum(run.completion_tokens or 0 for run in runs) / total, 1
        ) if total else 0.0,
        max_summary_words=max((run.summary_words for run in runs), default=0),
        avg_elapsed_ms=int(sum(run.elapsed_ms for run in runs) / total) if total else 0,
        failures=failures[:10],
    )


def table(summaries: list[Summary]) -> str:
    """Сводка ступеней рядом — то самое сравнение «без ограничений и с ограничениями»."""
    headers = [
        ("профиль", lambda s: s.profile),
        ("прогонов", lambda s: str(s.runs)),
        ("JSON", lambda s: f"{s.json_share:.0%}"),
        ("без чистки", lambda s: f"{s.clean_json_share:.0%}"),
        ("схема", lambda s: f"{s.schema_share:.0%}"),
        ("разных форм", lambda s: str(s.distinct_shapes)),
        ("токенов", lambda s: f"{s.avg_completion_tokens:.0f}"),
        ("слов в summary", lambda s: str(s.max_summary_words)),
        ("остановка", lambda s: ", ".join(f"{k}×{v}" for k, v in s.finish_reasons.items())),
    ]
    rows = [[name for name, _ in headers]]
    rows += [[fn(s) for _, fn in headers] for s in summaries]
    widths = [max(len(row[i]) for row in rows) for i in range(len(headers))]
    lines = ["  ".join(cell.ljust(widths[i]) for i, cell in enumerate(rows[0]))]
    lines.append("  ".join("─" * w for w in widths))
    for row in rows[1:]:
        lines.append("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)
