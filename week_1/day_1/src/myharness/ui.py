"""Цветовая схема и билдеры фрагментов текста для лог-панели полноэкранного интерфейса."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
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
        # список агентов под строкой ввода: строка на агента, главный разговор первым
        "agents": "#5c6370",
        "agents.name": "#abb2bf",
        "agents.name.active": "#ffffff bold",
        "agents.busy": "#61afef",
        "agents.done": "#98c379",
        "agents.error": "#e06c75",
        "agents.hint": "#5c6370 italic",
        "agents.meta": "#5c6370",
        # заголовки панелей внутри экрана
        "pane.title": "bg:#2c313a #7f8896",
        "pane.title.active": "bg:#3e4451 #ffffff bold",
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
    ("/mouse", "", "вернуть мышь терминалу и обратно (F2)", False),
    ("/clear", "", "очистить историю диалога и начать новый разговор", True),
    ("/remember", "<текст>", "запомнить факт о себе — он будет известен в любой папке", True),
    ("/memory", "[on|off]", "глобальная память: список фактов, сбор фактов вкл/выкл", True),
    ("/forget", "<номер>", "убрать факт из глобальной памяти", True),
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
    model: str,
    authorized: bool,
    profile: str,
    profile_dirty: bool,
    mouse_enabled: bool = True,
    archiving: bool = False,
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
        out.append(("class:status.bad", "  ·  мышь отдана терминалу — клики не действуют (F2)"))
    if archiving:
        # Архивариус работает фоном и ввод не блокирует, но и молчать о лишнем запросе
        # нельзя: человек платит за него и вправе видеть, что инструмент сейчас чем-то занят.
        out.append(("class:status", "  ·  запоминаю…"))
    out.append(("class:status", " "))
    return out


STATUS_MARKS: dict[str, tuple[str, str]] = {
    screens_mod.IDLE: ("·", "class:agents"),
    screens_mod.BUSY: ("…", "class:agents.busy"),
    screens_mod.DONE: ("✓", "class:agents.done"),
    screens_mod.ERROR: ("✕", "class:agents.error"),
}


def _row_click(on_click: Callable[[int], None], index: int) -> Callable[[MouseEvent], Any]:
    def handler(mouse_event: MouseEvent) -> Any:
        if mouse_event.event_type == MouseEventType.MOUSE_UP:
            on_click(index)
            return None
        return NotImplemented  # прочие события мыши пусть обрабатывает prompt_toolkit

    return handler


# Сколько строк списка показываем разом. Набор из двух групп поднимает полтора десятка
# агентов, и список во весь их рост съел бы пол-экрана — того самого, ради которого агентов и
# поднимали. Двенадцать строк умещаются даже в невысокое окно, а остальные показывает окно
# прокрутки: строка, на которой стоит пользователь, видна всегда.
PANEL_ROWS_MAX = 12

PANEL_HINT = "Alt+N · Shift+←/→ — экран · Alt+←/→ — панель · ↑/↓ — агент · клик — перейти"


def panel_slice(count: int, active: int, limit: int = PANEL_ROWS_MAX) -> tuple[int, int]:
    """Какой отрезок списка показать. Окно ведём за выбранной строкой, а не за началом списка:
    закрашенный кружок означает «вы здесь», и спрятать его значило бы соврать."""
    if count <= limit:
        return 0, count
    start = max(0, min(active - limit // 2, count - limit))
    return start, start + limit


def _clip(text: str, width: int) -> str:
    """Обрезать до ширины колонки. Многоточие ставим вместо последнего знака, а не после него:
    иначе строка вылезает за ширину и переносится, ломая сетку списка."""
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    return text[: width - 1] + "…" if width > 1 else "…"


def agent_panel_fragments(
    rows: list[tuple[str, str, str, int, int, int]],
    active: int,
    width: int,
    on_click: Callable[[int], None],
) -> Fragments:
    """Список агентов под строкой ввода: строка на агента, главный разговор первой строкой.

    На вход — (состояние, имя, занятие, миллисекунды, токены, число обменов) по строке на
    агента, номер строки, на которой стоит пользователь, ширина окна и переход по номеру.

    Кружок слева говорит, на кого мы смотрим сейчас, а не что с агентом происходит:
    состояние несут цвет имени и колонка занятия. Двух разных смыслов на один значок не
    вешаем — список читают мельком.

    Время и расход прижаты к правому краю: сравнивать их глазом можно только когда числа
    стоят друг под другом. Каждая строка добивается пробелами до полной ширины, потому что
    обработчик щелчка висит на самой строке — попасть мышью надо в строку, а не в буквы.
    """
    start, end = panel_slice(len(rows), active)
    shown = rows[start:end]
    name_width = min(max((len(name) for _, name, _, _, _, _ in shown), default=0), 22)
    meta_width = 9 + 2 + 8  # время, отбивка, токены
    task_width = max(8, width - 3 - name_width - 2 - meta_width - 1)

    out: Fragments = [("class:agents.hint", " " + PANEL_HINT), ("", "\n")]
    if start:
        out += [("class:agents", f" ↑ выше ещё {start}"), ("", "\n")]
    for offset, (status, name, task, total_ms, total_tokens, runs) in enumerate(shown):
        index = start + offset
        current = index == active
        handler = _row_click(on_click, index)
        _, mark_style = STATUS_MARKS.get(status, STATUS_MARKS[screens_mod.IDLE])
        elapsed = format_duration(total_ms) if runs else "—"
        tokens = f"↓ {format_tokens(total_tokens)}" if runs else "—"
        line: Fragments = [
            (mark_style, " ● " if current else " ○ "),
            ("class:agents.name.active" if current else "class:agents.name", _clip(name, name_width).ljust(name_width)),
            ("class:agents", "  "),
            (mark_style if status in (screens_mod.BUSY, screens_mod.ERROR) else "class:agents",
             _clip(task, task_width).ljust(task_width)),
            ("class:agents.meta", f"{elapsed:>9}  {tokens:>8}"),
        ]
        used = sum(len(text) for _, text in line)
        line.append(("class:agents", " " * max(0, width - used)))
        out += [(style, text, handler) for style, text in line]
        out.append(("", "\n"))
    if end < len(rows):
        out += [("class:agents", f" ↓ ниже ещё {len(rows) - end}"), ("", "\n")]
    return out


def pane_title_fragments(title: str, status: str, active: bool) -> Fragments:
    """Заголовок панели: чьи это ответы и что с ними сейчас. Активная панель подсвечена —
    её прокручивают и разворачивают."""
    mark, _ = STATUS_MARKS.get(status, STATUS_MARKS[screens_mod.IDLE])
    style = "class:pane.title.active" if active else "class:pane.title"
    return [(style, f" {'▸ ' if active else ''}{title} {mark}")]


# Слово к значку состояния. Значок один в один тот же, что на вкладках (`STATUS_MARKS`), —
# иначе в двух местах экрана одно и то же состояние выглядело бы по-разному. Слово рядом
# нужно потому, что список читают, когда что-то пошло не так, и гадать по одному значку в
# такой момент — лишняя работа.
AGENT_STATE_WORDS: dict[str, str] = {
    screens_mod.IDLE: "ждёт",
    screens_mod.BUSY: "занят",
    screens_mod.DONE: "готов",
    screens_mod.ERROR: "ошибка",
}


def agent_occupation(status: str, task: str, profile_name: str) -> str:
    """Чем агент занят — вторая колонка списка агентов.

    Пока агент работает, показываем начало его вопроса: список читают именно затем, чтобы
    понять, кто над чем сидит. Освободился — показываем имя профиля: вопрос уже отвечен, и
    держать его в строке значит выдавать прошлое за настоящее. Оборвался — говорим об этом
    словом, а не одним значком: искать глазами цвет в такой момент лишняя работа.

    Вопрос сворачиваем до первой строки: многострочный вопрос разорвал бы сетку списка.
    """
    if status == screens_mod.BUSY:
        first = next((line for line in task.strip().splitlines() if line.strip()), "")
        return first.strip() or profile_name
    if status == screens_mod.ERROR:
        return f"{AGENT_STATE_WORDS[screens_mod.ERROR]} · {profile_name}"
    return profile_name


def format_duration(ms: int) -> str:
    """Время работы по-человечески: до минуты — секундами, дальше — минутами и секундами.

    «382.4 с» формально верно и нечитаемо: чтобы понять, много это или мало, приходится
    делить в уме. Доли секунды после минуты не нужны — на таком масштабе они ничего не решают."""
    seconds = ms / 1000
    if seconds < 60:
        return f"{seconds:.1f} с"
    return f"{int(seconds) // 60} м {int(seconds) % 60} с"


def format_tokens(count: int) -> str:
    """Расход токенов: до тысячи — как есть, дальше — сокращением вида «92.4k».

    Считать нули в «92417» глазом невозможно, а в списке важен порядок величины: кто из
    агентов съел заметно больше остальных."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k"


def methods_fragments(names: list[str]) -> Fragments:
    """Сообщение о том, что профиль развернул набор способов по вкладкам."""
    out: Fragments = [("class:agent", f"● способы: {', '.join(names)}"), ("", "\n")]
    out.append(("class:dim", "  задайте задачу здесь — она уйдёт во все способы сразу"))
    out.append(("", "\n"))
    out.append(("class:dim", "  вкладки: Alt+N или Shift+←/→ · панели внутри вкладки: Alt+←/→ · F3 — развернуть"))
    out.append(("", "\n"))
    return out


def agent_task_fragments(agent: str, profile_name: str, system: str | None, question: str) -> Fragments:
    """Шапка панели: кто отвечает, с какой инструкцией и что ему поручено.

    Инструкция сворачивается до первой строки: в сетке из пяти панелей развёрнутый текст
    вытеснил бы сам ответ, ради которого панель и открыта. Целиком инструкцию показывает
    /system на этой панели.
    """
    out: Fragments = [("class:agent", f"● {agent}"), ("class:dim", f"   {profile_name}"), ("", "\n")]
    if system:
        first_line = system.strip().splitlines()[0]
        preview = first_line[:70] + ("…" if len(first_line) > 70 else "")
        out.append(("class:dim", f"· {preview}"))
        out.append(("class:dim", "  (/system — целиком)"))
        out.append(("", "\n"))
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


def outcome_fragments(kind: str, source: str, text: str) -> Fragments:
    """Итог оркестратора в ленте главного экрана: чей он и что в нём.

    Ответ переносим целиком, а не ссылкой «смотрите вкладку такую-то». Человек, ведущий
    разговор на главном экране, иначе обязан сам пойти и посмотреть, чем всё кончилось, —
    а оркестратор, не вернувший итог наверх, не годится и в звено цикла: сравнивать
    следующему шагу будет нечего.
    """
    return [
        ("class:agent", f"● {kind} «{source}»"),
        ("", "\n"),
        ("", text.strip()),
        ("", "\n"),
    ]


def team_summary_label_fragments(count: int) -> Fragments:
    return [("class:system", f"· свожу ответы агентов ({count})"), ("", "\n")]


def team_list_fragments(lead: str, names: list[str]) -> Fragments:
    out: Fragments = [("class:system", f"· группа профиля «{lead}»"), ("", "\n")]
    for name in names:
        out.append(("", f"  ● {name}\n"))
    out.append(("class:dim", "  вопрос уходит всей группе; разово: /team <вопрос>\n"))
    return out


def _plural(count: int, one: str, few: str, many: str) -> str:
    """Русское склонение по числу: 1 пара, 2 пары, 5 пар.

    Строка о восстановлении разговора встречает человека при каждом запуске, и «3 пар»
    в ней читается как недоделка инструмента."""
    if count % 10 == 1 and count % 100 != 11:
        return one
    if 2 <= count % 10 <= 4 and not 12 <= count % 100 <= 14:
        return few
    return many


def _clock(ts: str) -> str:
    """Часы и минуты отметки времени по местным часам человека — пустая строка, если
    отметки нет или она нечитаема.

    В файле время лежит в UTC (иначе переезд между поясами перепутал бы порядок записей),
    а человек сверяет его со своими часами на стене."""
    try:
        return datetime.fromisoformat(ts).astimezone().strftime("%H:%M")
    except (TypeError, ValueError):
        return ""


def restored_fragments(loaded: int, saved: int, last_ts: str) -> Fragments:
    """Строка о поднятом разговоре: сколько пар в памяти и сколько их лежит в файле.

    Оба числа названы по той же причине, по которой называется число выброшенных обрезкой:
    молча подставленный прошлый разговор человек отлаживает как «модель отвечает не на то»,
    а разницу между «помню три пары» и «в файле их двенадцать» иначе взять неоткуда.

    Сами реплики в ленту не печатаются — решение пользователя: экран остаётся чистым,
    память при этом полная."""
    текст = f"восстановлен разговор: {loaded} {_plural(loaded, 'пара', 'пары', 'пар')} из {saved} сохранённых"
    часы = _clock(last_ts)
    if часы:
        текст += f", последняя {часы}"
    return system_fragments(текст)


def facts_fragments(facts: list[str]) -> Fragments:
    """Глобальная память списком с номерами — теми же, какие принимает `/forget`."""
    if not facts:
        return system_fragments("глобальная память пуста — /remember <текст> запомнит факт")
    out: Fragments = [("class:system", f"· глобальная память ({len(facts)})"), ("", "\n")]
    for номер, факт in enumerate(facts, 1):
        out.append(("", f"  {номер}. {факт}\n"))
    out.append(("class:dim", "  убрать: /forget <номер>\n"))
    return out


def fact_added_fragments(text: str) -> Fragments:
    """Объявление нового факта. Печатается и на `/remember`, и на находку архивариуса:
    молчаливого роста памяти не бывает — иначе выдумку архивариуса нечем заметить."""
    return [("class:system", "· запомнил: "), ("", text), ("", "\n")]


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
        "Список агентов\n"
        "Как только поднят хоть один агент кроме главного разговора, под строкой ввода\n"
        "появляется список: строка на агента, главный разговор первой строкой. Закрашенный\n"
        "кружок — тот, на кого вы смотрите сейчас; справа время работы и расход токенов.\n"
        "↑ и ↓ переводят на соседнего агента, клик по строке — на выбранного.\n"
        "\n"
        "Группа агентов\n"
        "Профиль со списком agents поднимает агентов: вопрос уходит каждому со своей\n"
        "системной инструкцией, ответы приходят на отдельные экраны, а профиль-ведущий\n"
        "сводит их в общий вывод. Экраны агентов — только для чтения: постановка задачи,\n"
        "рассуждения и ответ. Переключение — Alt+N, Shift+←/→ или строка списка агентов.\n"
        "Итог работы оркестратора — сводка ведущего у группы, ответ последнего шага у\n"
        "цепочки — возвращается в главный экран целиком, с пометкой, кто его дал.\n"
        "\n"
        "Профиль со списком screens раскладывает приём по вкладкам: у каждого экрана своя\n"
        "инструкция и своя заготовка ввода, и ввод уходит в тот экран, который открыт.\n"
        "Профиль со списком methods — набор способов: заданный в главном экране вопрос уходит\n"
        "во все способы сразу, каждый отвечает на своей вкладке.\n"
        "\n"
        "Память\n"
        "Разговор главного экрана сохраняется сам и поднимается при следующем запуске в той\n"
        "же папке с тем же профилем: включать нечего, это свойство инструмента. В ленту при\n"
        "этом печатается одна строка отчёта, а не весь прошлый разговор. /clear начинает\n"
        "новый разговор — прежний остаётся лежать на диске.\n"
        "Второй слой — глобальная память: короткие факты о вас, известные в ЛЮБОЙ папке.\n"
        "/remember <текст> кладёт факт, /memory показывает список с номерами, /forget <номер>\n"
        "убирает. Раз в несколько обменов их выписывает из разговора агент-архивариус —\n"
        "каждый найденный факт он объявляет в ленте; /memory off выключает его совсем.\n"
        "\n"
        "Внутри вкладки может быть несколько панелей — шаги приёма или ответы экспертов рядом.\n"
        "Alt+←/→ переходят между панелями, F3 разворачивает панель на весь экран и обратно.\n"
        "\n"
        "Мышь работает сразу: клики по строкам списка агентов и по строкам меню, прокрутка\n"
        "колесом. Выделение текста при этом остаётся за терминалом — harness просит у него\n"
        "только нажатия, без отслеживания протяжки. F2 или /mouse возвращают мышь терминалу\n"
        "целиком: это аварийный выход на случай терминала, который так не умеет.\n"
        "\n"
        "Ctrl+C во время ответа — отменить текущий запрос (всю группу разом).\n"
        "Пока модель отвечает, можно вводить следующие сообщения — они встанут в очередь\n"
        "и уйдут в LLM сразу после ответа на предыдущее.\n"
        "Колесо мыши / PageUp, PageDown — прокрутка истории вверх-вниз (обычная прокрутка\n"
        "терминала тут не работает — полноэкранный режим). Ctrl+End или отправка нового\n"
        "сообщения — вернуться к живому выводу.\n"
    )
    return [("", "".join(lines) + text)]
