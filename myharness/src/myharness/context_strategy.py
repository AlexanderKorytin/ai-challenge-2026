"""Чистая политика выбора истории для стратегий контекста.

Модуль не хранит разговор и не знает об агенте: он получает уже завершённый путь,
проверяет границы пар и возвращает копии сообщений, которые должны уйти в запрос.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

CONTEXT_STANDARD = "standard"
CONTEXT_SLIDING = "sliding"
CONTEXT_FACTS = "facts"
CONTEXT_BRANCHING = "branching"
CONTEXT_STRATEGIES = (
    CONTEXT_STANDARD,
    CONTEXT_SLIDING,
    CONTEXT_FACTS,
    CONTEXT_BRANCHING,
)
DEFAULT_CONTEXT_STRATEGY = CONTEXT_STANDARD
DEFAULT_STRATEGY_WINDOW = 4


@dataclass(frozen=True)
class ContextSelection:
    """Результат выбора завершённых пар для одного запроса."""

    messages: list[dict[str, Any]]
    selected_pairs: int
    omitted_pairs: int


def _pair_count(history: list[dict[str, Any]]) -> int:
    if len(history) % 2:
        raise ValueError("история должна состоять из целых пар user/assistant")

    for index in range(0, len(history), 2):
        user = history[index]
        assistant = history[index + 1]
        if (
            not isinstance(user, dict)
            or user.get("role") != "user"
            or not isinstance(assistant, dict)
            or assistant.get("role") != "assistant"
        ):
            raise ValueError(
                f"сообщения {index + 1} и {index + 2} не образуют пару user/assistant"
            )
    return len(history) // 2


def select_history(
    history: list[dict[str, Any]], strategy: str, window_pairs: int
) -> ContextSelection:
    """Выбирает целые пары, не меняя полную запись разговора.

    `sliding` и `facts` берут строгий хвост без запаса. `standard` и `branching`
    получают весь переданный линейный путь; окно для них остаётся настройкой профиля,
    но на выбор сообщений не влияет.
    """
    if strategy not in CONTEXT_STRATEGIES:
        raise ValueError(f"неизвестная стратегия контекста: {strategy!r}")
    if isinstance(window_pairs, bool) or not isinstance(window_pairs, int) or window_pairs <= 0:
        raise ValueError("размер окна стратегии должен быть положительным целым числом")

    total_pairs = _pair_count(history)
    if strategy in (CONTEXT_SLIDING, CONTEXT_FACTS):
        selected_pairs = min(total_pairs, window_pairs)
        start = (total_pairs - selected_pairs) * 2
    else:
        selected_pairs = total_pairs
        start = 0

    return ContextSelection(
        messages=[dict(message) for message in history[start:]],
        selected_pairs=selected_pairs,
        omitted_pairs=total_pairs - selected_pairs,
    )


def conversation_facts_block(values: dict[str, str]) -> str:
    """Собирает подписанный блок фактов текущего разговора с устойчивым порядком ключей."""
    if not values:
        return ""
    lines = ["Факты текущего разговора (Sticky Facts):"]
    lines.extend(f"- {key}: {values[key]}" for key in sorted(values))
    return "\n".join(lines)
