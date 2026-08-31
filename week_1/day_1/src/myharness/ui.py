"""Цветовая схема и билдеры фрагментов текста для лог-панели полноэкранного интерфейса."""

from __future__ import annotations

from prompt_toolkit.styles import Style

SPINNER_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

STYLE = Style.from_dict(
    {
        "border": "#5c6370",
        "title": "bold",
        "model": "#56b6c2",
        "ok": "#98c379 bold",
        "bad": "#e5c07b",
        "dim": "#5c6370",
        "hint": "#e5c07b",
        "error": "#e06c75 bold",
        "system": "#5c6370 italic",
        "user": "#61afef bold",
        "answer": "#56b6c2 bold",
        "sep": "#3b3f4a",
    }
)

Fragments = list[tuple[str, str]]


def _plain_len(fragments: Fragments) -> int:
    return sum(len(text) for _, text in fragments)


def _box(lines: list[Fragments]) -> Fragments:
    width = max(_plain_len(line) for line in lines)
    out: Fragments = [("class:border", "╭─" + "─" * width + "─╮\n")]
    for line in lines:
        pad = width - _plain_len(line)
        out.append(("class:border", "│ "))
        out.extend(line)
        out.append(("", " " * pad))
        out.append(("class:border", " │\n"))
    out.append(("class:border", "╰─" + "─" * width + "─╯\n"))
    return out


def banner_fragments(model: str, authorized: bool) -> Fragments:
    auth_line: Fragments = (
        [("class:ok", "● авторизован")] if authorized else [("class:bad", "○ не авторизован — /auth")]
    )
    lines = [
        [("class:title", "myharness"), ("", "  ·  DeepSeek API")],
        [("", "модель: "), ("class:model", model), ("", "   ")] + auth_line,
        [("class:dim", "/model · /auth · /help · /exit")],
    ]
    return _box(lines) + [("", "\n")]


def hint_fragments(text: str) -> Fragments:
    return [("class:hint", f"› {text}"), ("", "\n")]


def error_fragments(text: str) -> Fragments:
    return [("class:error", f"✕ {text}"), ("", "\n")]


def system_fragments(text: str) -> Fragments:
    return [("class:system", f"· {text}"), ("", "\n")]


def queued_fragments(position: int) -> Fragments:
    return [("class:dim", f"· добавлено в очередь (позиция {position})"), ("", "\n")]


def user_fragments(text: str) -> Fragments:
    return [("class:user", "› "), ("", text), ("", "\n")]


def reasoning_label_fragments() -> Fragments:
    return [("class:system", "· размышляю…"), ("", "\n")]


def answer_label_fragments() -> Fragments:
    return [("class:answer", "myharness › ")]


def help_fragments() -> Fragments:
    text = (
        "Команды\n"
        "  /model            — список доступных моделей\n"
        "  /model <имя>      — выбрать модель\n"
        "  /auth             — авторизация по API-ключу DeepSeek\n"
        "  /clear            — очистить историю диалога\n"
        "  /exit, /quit      — выход\n"
        "\n"
        "Ctrl+C во время ответа — отменить текущий запрос.\n"
        "Пока модель отвечает, можно вводить следующие сообщения — они встанут в очередь\n"
        "и уйдут в LLM сразу после ответа на предыдущее.\n"
        "Колесо мыши / PageUp, PageDown — прокрутка истории вверх-вниз (обычная прокрутка\n"
        "терминала тут не работает — полноэкранный режим). Ctrl+End или отправка нового\n"
        "сообщения — вернуться к живому выводу.\n"
    )
    return [("", text)]
