"""Что мы считаем результатом прогона и как сводим прогоны в таблицу.

Меряем три вещи, и все три нужны вместе: доля верных ответов (иначе способ не сравнить),
разброс ответов (одна и та же формулировка может выигрывать через раз — у API нет `seed`)
и цена — сколько запросов, токенов и секунд стоил один ответ. Способ, который точнее вчетверо
дороже, — это результат, а не победа.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

VERDICT_UNKNOWN = None


@dataclass
class Step:
    """Промежуточный запрос внутри одного прогона: агент группы или шаг цепочки."""

    name: str
    text: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    elapsed_ms: int = 0
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "text": self.text,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
        }


@dataclass
class Run:
    """Один прогон способа: итоговый ответ плюс всё, из чего он получился."""

    method: str
    index: int
    answer: str = ""
    reasoning: str = ""
    steps: list[Step] = field(default_factory=list)
    finish_reason: str | None = None
    elapsed_ms: int = 0
    error: str | None = None
    correct: bool | None = VERDICT_UNKNOWN  # проставляет судья, см. judge.py
    verdict_why: str = ""
    usage: dict[str, Any] = field(default_factory=dict)  # расход последнего, итогового запроса

    @property
    def requests(self) -> int:
        """Сколько обращений к API стоил ответ: шаги плюс итоговый запрос."""
        return len(self.steps) + 1

    @property
    def prompt_tokens(self) -> int:
        return self._own("prompt_tokens") + sum(step.prompt_tokens for step in self.steps)

    @property
    def completion_tokens(self) -> int:
        return self._own("completion_tokens") + sum(step.completion_tokens for step in self.steps)

    @property
    def reasoning_tokens(self) -> int:
        return self._own("reasoning_tokens") + sum(step.reasoning_tokens for step in self.steps)

    def _own(self, key: str) -> int:
        return int(self.usage.get(key) or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "index": self.index,
            "answer": self.answer,
            "reasoning": self.reasoning,
            "steps": [step.to_dict() for step in self.steps],
            "finish_reason": self.finish_reason,
            "elapsed_ms": self.elapsed_ms,
            "error": self.error,
            "correct": self.correct,
            "verdict_why": self.verdict_why,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Run:
        run = cls(
            method=data["method"],
            index=data["index"],
            answer=data.get("answer", ""),
            reasoning=data.get("reasoning", ""),
            finish_reason=data.get("finish_reason"),
            elapsed_ms=data.get("elapsed_ms", 0),
            error=data.get("error"),
            correct=data.get("correct"),
            verdict_why=data.get("verdict_why", ""),
        )
        run.steps = [Step(**step) for step in data.get("steps", [])]
        # токены в файле уже просуммированы по шагам — раскладывать обратно незачем,
        # достаточно вернуть их как собственные
        run.usage = {
            "prompt_tokens": data.get("prompt_tokens", 0) - sum(s.prompt_tokens for s in run.steps),
            "completion_tokens": data.get("completion_tokens", 0) - sum(s.completion_tokens for s in run.steps),
            "reasoning_tokens": data.get("reasoning_tokens", 0) - sum(s.reasoning_tokens for s in run.steps),
        }
        return run


@dataclass
class Summary:
    method: str
    runs: int
    answered: int
    correct: int
    unjudged: int
    requests: int
    prompt_tokens: int
    completion_tokens: int
    reasoning_tokens: int
    elapsed_ms: int
    failures: list[str] = field(default_factory=list)

    @property
    def accuracy(self) -> float | None:
        judged = self.answered - self.unjudged
        return self.correct / judged if judged else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "runs": self.runs,
            "answered": self.answered,
            "correct": self.correct,
            "unjudged": self.unjudged,
            "accuracy": self.accuracy,
            "requests": self.requests,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "elapsed_ms": self.elapsed_ms,
            "failures": self.failures,
        }


def summarize(method: str, runs: list[Run]) -> Summary:
    answered = [run for run in runs if run.answer and not run.error]
    return Summary(
        method=method,
        runs=len(runs),
        answered=len(answered),
        correct=sum(1 for run in answered if run.correct is True),
        unjudged=sum(1 for run in answered if run.correct is VERDICT_UNKNOWN),
        requests=sum(run.requests for run in runs),
        prompt_tokens=sum(run.prompt_tokens for run in runs),
        completion_tokens=sum(run.completion_tokens for run in runs),
        reasoning_tokens=sum(run.reasoning_tokens for run in runs),
        elapsed_ms=sum(run.elapsed_ms for run in runs),
        failures=[f"прогон {run.index}: {run.error}" for run in runs if run.error],
    )


def table(summaries: list[Summary]) -> str:
    """Сводка способов: точность рядом с ценой, иначе выводы получаются половинчатыми."""
    head = f"{'способ':<30}{'верных':>10}{'запросов':>10}{'токенов':>12}{'сек/ответ':>11}"
    lines = [head, "─" * len(head)]
    for item in summaries:
        judged = item.answered - item.unjudged
        share = "—" if item.accuracy is None else f"{item.correct}/{judged} ({item.accuracy * 100:.0f}%)"
        per_answer = item.elapsed_ms / item.answered / 1000 if item.answered else 0.0
        tokens = item.prompt_tokens + item.completion_tokens
        lines.append(f"{item.method:<30}{share:>10}{item.requests:>10}{tokens:>12}{per_answer:>11.1f}")
    return "\n".join(lines)
