"""Цветовая схема и билдеры фрагментов текста для лог-панели полноэкранного интерфейса."""

from __future__ import annotations

from typing import Any

from prompt_toolkit.styles import Style

from . import params as params_mod

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
        "meta": "#5c6370",
        "meta.warn": "#e5c07b",
        # всплывающие панели: меню команд и выбор значения параметра
        "panel": "bg:#21252b #abb2bf",
        "panel.border": "bg:#21252b #5c6370",
        "panel.title": "bg:#21252b #61afef bold",
        "panel.item": "bg:#21252b #abb2bf",
        "panel.item.selected": "bg:#3e4451 #ffffff bold",
        "panel.hint": "bg:#21252b #5c6370",
        "panel.hint.selected": "bg:#3e4451 #c8ccd4",
        "panel.footer": "bg:#21252b #5c6370 italic",
        "completion-menu.completion": "bg:#21252b #abb2bf",
        "completion-menu.completion.current": "bg:#3e4451 #ffffff bold",
        "completion-menu.meta.completion": "bg:#21252b #5c6370",
        "completion-menu.meta.completion.current": "bg:#3e4451 #c8ccd4",
    }
)

Fragments = list[tuple[str, str]]

# (команда, аргумент, описание, показывать только авторизованным)
COMMANDS: tuple[tuple[str, str, str, bool], ...] = (
    ("/auth", "", "авторизация по API-ключу DeepSeek", False),
    ("/help", "", "справка по командам и горячим клавишам", False),
    ("/model", "[имя]", "выбрать модель — список со стрелками", True),
    ("/profile", "[имя]", "выбрать профиль генерации — список со стрелками", True),
    ("/params", "", "параметры профиля: показать и изменить", True),
    ("/set", "<параметр>", "изменить параметр — меню выбора значения", True),
    ("/system", "", "показать текущую системную инструкцию", True),
    ("/clear", "", "очистить историю диалога", True),
    ("/exit", "", "выход", False),
)

FINISH_REASONS = {
    "stop": "модель закончила сама",
    "length": "упёрлось в max_tokens — ответ обрезан",
    "content_filter": "ответ отфильтрован",
    "tool_calls": "модель запросила вызов инструмента",
    "insufficient_system_resource": "прервано сервером из-за нехватки ресурсов",
}


def visible_commands(authorized: bool) -> list[tuple[str, str, str]]:
    """Не авторизован — показываем только то, чем можно воспользоваться сейчас."""
    out: list[tuple[str, str, str]] = []
    for name, arg, description, needs_auth in COMMANDS:
        if needs_auth and not authorized:
            continue
        if name == "/auth" and authorized:
            description = "сменить API-ключ DeepSeek"
        out.append((name, arg, description))
    return out


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


# Ветка day2-fish-facts: harness работает как агент-справочник по рыбам. Строка стоит в
# шапке, чтобы назначение было видно сразу, а не угадывалось по имени профиля.
AGENT_TITLE = "агент поиска информации о рыбах"


def banner_fragments(model: str, authorized: bool, profile: str, description: str = "") -> Fragments:
    auth_line: Fragments = (
        [("class:ok", "● авторизован")] if authorized else [("class:bad", "○ не авторизован — /auth")]
    )
    lines = [
        [("class:title", "myharnessfish"), ("", "  ·  DeepSeek API")],
        [("class:answer", AGENT_TITLE)],
        [("", "модель: "), ("class:model", model), ("", "   ")] + auth_line,
        [("", "профиль: "), ("class:model", profile)],
    ]
    if description:
        lines.append([("class:hint", description)])
    lines.append([("class:dim", "введите название рыбы · / — список команд")])
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


def meta_fragments(finish_reason: str | None, usage: dict[str, Any], elapsed: float, profile: str) -> Fragments:
    """Строка под ответом: чем закончилось, сколько токенов, сколько времени, каким профилем."""
    parts: list[str] = []
    if finish_reason:
        parts.append(FINISH_REASONS.get(finish_reason, finish_reason))
    if usage:
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        details = usage.get("completion_tokens_details") or {}
        reasoning_tokens = details.get("reasoning_tokens")
        chunk = f"токены: вход {prompt_tokens}, выход {completion_tokens}"
        if reasoning_tokens:
            chunk += f" (из них рассуждения {reasoning_tokens})"
        parts.append(chunk)
    parts.append(f"{elapsed:.1f} с")
    parts.append(f"профиль: {profile}")
    style = "class:meta.warn" if finish_reason == "length" else "class:meta"
    return [(style, "· " + "  ·  ".join(parts)), ("", "\n")]


def params_fragments(profile_name: str, values: dict[str, Any], system: str | None) -> Fragments:
    out: Fragments = [("class:system", f"· параметры профиля «{profile_name}»"), ("", "\n")]
    for name in params_mod.ORDER:
        spec = params_mod.SPECS[name]
        value = values.get(name)
        shown = params_mod.format_value(value)
        out.append(("", f"  {spec.title:<22} "))
        out.append(("class:model" if value is not None else "class:dim", shown))
        reason = params_mod.inapplicable_reason(name, values)
        if reason and value is not None:
            out.append(("class:hint", f"   ← {reason}"))
        out.append(("", "\n"))
    if system:
        first_line = system.strip().splitlines()[0]
        preview = first_line[:60] + ("…" if len(first_line) > 60 else "")
        out.append(("", "  системная инструкция  "))
        out.append(("class:model", preview))
        out.append(("", "\n"))
    else:
        out.append(("", "  системная инструкция  "))
        out.append(("class:dim", "не задана"))
        out.append(("", "\n"))
    out.append(("class:dim", "  изменить: /set <параметр>\n"))
    return out


def profile_list_fragments(items: list[tuple[str, Any]], active: str) -> Fragments:
    out: Fragments = [("class:system", "· профили генерации"), ("", "\n")]
    for name, source in items:
        mark = "●" if name == active else "○"
        out.append(("", f"  {mark} {name}"))
        if source is not None:
            out.append(("class:dim", f"   {source}"))
        else:
            out.append(("class:dim", "   встроенный"))
        out.append(("", "\n"))
    out.append(("class:dim", "  переключить: /profile <имя> · сохранить текущий: /profile save <имя>\n"))
    return out


def system_prompt_fragments(profile_name: str, system: str | None) -> Fragments:
    if not system:
        return system_fragments(f"в профиле «{profile_name}» системная инструкция не задана")
    out: Fragments = [("class:system", f"· системная инструкция профиля «{profile_name}»"), ("", "\n")]
    for line in system.splitlines():
        out.append(("class:dim", f"  {line}\n"))
    return out


def help_fragments() -> Fragments:
    lines = ["Команды\n"]
    for name, arg, description in visible_commands(authorized=True):
        signature = f"{name} {arg}".strip()
        lines.append(f"  {signature:<20} — {description}\n")
    text = (
        "\n"
        "Ввод «/» открывает список команд прямо под строкой ввода: стрелки — выбор,\n"
        "Tab или Enter — подставить, Esc — закрыть.\n"
        "/set <параметр> открывает меню значений: стрелки — выбор, Enter — применить,\n"
        "Esc — выйти без изменений.\n"
        "\n"
        "Ctrl+C во время ответа — отменить текущий запрос.\n"
        "Пока модель отвечает, можно вводить следующие сообщения — они встанут в очередь\n"
        "и уйдут в LLM сразу после ответа на предыдущее.\n"
        "Колесо мыши / PageUp, PageDown — прокрутка истории вверх-вниз (обычная прокрутка\n"
        "терминала тут не работает — полноэкранный режим). Ctrl+End или отправка нового\n"
        "сообщения — вернуться к живому выводу.\n"
    )
    return [("", "".join(lines) + text)]
