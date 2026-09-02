"""Цветовая схема и билдеры фрагментов текста для лог-панели полноэкранного интерфейса."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from prompt_toolkit.mouse_events import MouseEvent, MouseEventType
from prompt_toolkit.styles import Style

from . import params as params_mod
from . import screens as screens_mod

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
        "status": "bg:#21252b #7f8896",
        "status.ok": "bg:#21252b #98c379",
        "status.bad": "bg:#21252b #e5c07b",
        "status.value": "bg:#21252b #56b6c2",
        # полоса вкладок под строкой ввода: главный экран и экраны агентов
        "tabs": "bg:#2c313a #7f8896",
        "tabs.active": "bg:#3e4451 #ffffff bold",
        "tabs.busy": "bg:#2c313a #61afef",
        "tabs.done": "bg:#2c313a #98c379",
        "tabs.error": "bg:#2c313a #e06c75",
        "tabs.hint": "bg:#2c313a #5c6370 italic",
        "agent": "#c678dd bold",
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
    ("/team", "[вопрос]", "поднять группу агентов из профиля-ведущего", True),
    ("/mouse", "", "отдать мышь терминалу и обратно (F2) — чтобы выделять текст", False),
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
    """Пока ключа нет, в меню только /auth: остальное всё равно не сработает. После
    авторизации /auth из меню уходит — он больше не нужен на каждый день. Сама команда
    остаётся рабочей (сменить ключ можно, набрав её целиком), и в /help она есть."""
    if not authorized:
        return [(name, arg, description) for name, arg, description, needs_auth in COMMANDS if name == "/auth"]
    return [(name, arg, description) for name, arg, description, _ in COMMANDS if name != "/auth"]


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


def banner_fragments(model: str, authorized: bool, profile: str) -> Fragments:
    auth_line: Fragments = (
        [("class:ok", "● авторизован")] if authorized else [("class:bad", "○ не авторизован — /auth")]
    )
    lines = [
        [("class:title", "myharness"), ("", "  ·  DeepSeek API")],
        [("", "модель: "), ("class:model", model), ("", "   ")] + auth_line,
        [("", "профиль: "), ("class:model", profile)],
        [("class:dim", "введите / — появится список команд")],
    ]
    return _box(lines) + [("", "\n")]


def status_fragments(
    model: str, authorized: bool, profile: str, profile_dirty: bool, mouse_enabled: bool = True
) -> Fragments:
    """Живая строка состояния внизу экрана. Шапка печатается один раз и остаётся историей,
    а здесь всегда актуальное: после /auth статус меняется сразу, без перезапуска."""
    out: Fragments = [("class:status", " ")]
    if authorized:
        out.append(("class:status.ok", "● авторизован"))
    else:
        out.append(("class:status.bad", "○ не авторизован — /auth"))
    out.append(("class:status", "  ·  "))
    out.append(("class:status.value", model))
    out.append(("class:status", "  ·  профиль: "))
    out.append(("class:status.value", profile))
    if profile_dirty:
        out.append(("class:status.bad", " (изменён)"))
    if not mouse_enabled:
        out.append(("class:status.bad", "  ·  мышь у терминала (F2)"))
    out.append(("class:status", " "))
    return out


STATUS_MARKS: dict[str, tuple[str, str]] = {
    screens_mod.IDLE: ("·", "class:tabs"),
    screens_mod.BUSY: ("…", "class:tabs.busy"),
    screens_mod.DONE: ("✓", "class:tabs.done"),
    screens_mod.ERROR: ("✕", "class:tabs.error"),
}


def _tab_click(on_click: Callable[[int], None], index: int) -> Callable[[MouseEvent], Any]:
    def handler(mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_click(index)
            return None
        return NotImplemented  # прочие события мыши пусть обрабатывает prompt_toolkit

    return handler


def tabs_fragments(items: list[tuple[str, str]], active: int, on_click: Callable[[int], None]) -> Fragments:
    """Полоса вкладок под строкой ввода: главный экран и экраны агентов.

    Вкладка кликабельна (обработчик висит прямо на фрагменте текста) и пронумерована —
    номер совпадает с Alt+N, чтобы клавиша и клик вели в одно и то же место. Значок
    показывает, что с агентом происходит: думает, ответил или упал.
    """
    out: Fragments = []
    for index, (title, status) in enumerate(items):
        mark, mark_style = STATUS_MARKS.get(status, STATUS_MARKS[screens_mod.IDLE])
        handler = _tab_click(on_click, index)
        style = "class:tabs.active" if index == active else "class:tabs"
        out.append((style, f" {index + 1} {title} ", handler))
        out.append((style if index == active else mark_style, f"{mark} ", handler))
        out.append(("class:tabs", "│"))
    out.append(("class:tabs.hint", "  Alt+N или Shift+←/→ — переключить экран"))
    out.append(("class:tabs", " "))
    return out


def agent_task_fragments(agent: str, profile_name: str, system: str | None, question: str) -> Fragments:
    """Шапка экрана агента: кто он, с какой инструкцией поднят и что ему поручено."""
    out: Fragments = [("class:agent", f"● агент «{agent}»"), ("class:dim", f"   профиль: {profile_name}"), ("", "\n")]
    if system:
        out.append(("class:system", "· системная инструкция:"))
        out.append(("", "\n"))
        for line in system.strip().splitlines():
            out.append(("class:dim", f"  {line}\n"))
    else:
        out.extend(system_fragments("системная инструкция не задана"))
    out.append(("class:user", "› "))
    out.append(("", question))
    out.append(("", "\n"))
    return out


def team_start_fragments(names: list[str]) -> Fragments:
    """Сообщение в главном экране: группа поднята, ответы смотреть на соседних вкладках."""
    listing = ", ".join(names)
    return [
        ("class:agent", f"● группа поднята: {listing}"),
        ("", "\n"),
        ("class:dim", "  ответ каждого — на своём экране (Alt+2…), сводка появится здесь"),
        ("", "\n"),
    ]


def work_screens_fragments(names: list[str]) -> Fragments:
    """Сообщение о том, что профиль разложил приём по вкладкам."""
    out: Fragments = [("class:agent", f"● рабочие экраны: {', '.join(names)}"), ("", "\n")]
    out.append(("class:dim", "  ввод уходит в тот экран, который открыт (Alt+N, Shift+←/→ или клик)"))
    out.append(("", "\n"))
    return out


def team_summary_label_fragments(count: int) -> Fragments:
    return [("class:system", f"· свожу ответы агентов ({count})"), ("", "\n")]


def team_list_fragments(lead: str, names: list[str]) -> Fragments:
    out: Fragments = [("class:system", f"· группа профиля «{lead}»"), ("", "\n")]
    for name in names:
        out.append(("", f"  ● {name}\n"))
    out.append(("class:dim", "  вопрос уходит всей группе; разово: /team <вопрос>\n"))
    return out


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
    """В справке перечислены все команды, включая /auth: меню его после авторизации прячет,
    а узнать, чем сменить ключ, пользователь должен где-то одном месте."""
    lines = ["Команды\n"]
    for name, arg, description, _ in COMMANDS:
        signature = f"{name} {arg}".strip()
        lines.append(f"  {signature:<20} — {description}\n")
    text = (
        "\n"
        "Ввод «/» открывает список команд прямо под строкой ввода: стрелки — выбор,\n"
        "Tab или Enter — подставить, Esc — закрыть.\n"
        "/set <параметр> открывает меню значений: стрелки — выбор, Enter — применить,\n"
        "Esc — выйти без изменений.\n"
        "\n"
        "Группа агентов\n"
        "Профиль со списком agents поднимает агентов: вопрос уходит каждому со своей\n"
        "системной инструкцией, ответы приходят на отдельные экраны, а профиль-ведущий\n"
        "сводит их в общий вывод. Экраны агентов — только для чтения: постановка задачи,\n"
        "рассуждения и ответ. Переключение — клик по вкладке, Alt+N или Shift+←/→.\n"
        "\n"
        "Профиль со списком screens раскладывает приём по вкладкам: у каждого экрана своя\n"
        "инструкция и своя заготовка ввода, и ввод уходит в тот экран, который открыт.\n"
        "\n"
        "F2 или /mouse — отдать мышь терминалу и обратно. Полноэкранный режим забирает мышь\n"
        "себе, поэтому выделить текст для копирования можно только отдав её терминалу;\n"
        "клики по вкладкам и прокрутка колесом на это время отключаются.\n"
        "\n"
        "Ctrl+C во время ответа — отменить текущий запрос (всю группу разом).\n"
        "Пока модель отвечает, можно вводить следующие сообщения — они встанут в очередь\n"
        "и уйдут в LLM сразу после ответа на предыдущее.\n"
        "Колесо мыши / PageUp, PageDown — прокрутка истории вверх-вниз (обычная прокрутка\n"
        "терминала тут не работает — полноэкранный режим). Ctrl+End или отправка нового\n"
        "сообщения — вернуться к живому выводу.\n"
    )
    return [("", "".join(lines) + text)]
