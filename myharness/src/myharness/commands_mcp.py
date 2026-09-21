"""Команда `/mcp`: серверы MCP из `.mcp.json` рабочего каталога — показать, добавить, убрать.

Соединение и файл — забота `mcp_client`; здесь разбор доводов, правила о тайнах и показ.
Синтаксис взят у `claude mcp add` / `claude mcp remove`, чтобы один `.mcp.json` читали оба
инструмента и сравнение недели шло на одних и тех же серверах.
"""

from __future__ import annotations

import logging
import re
import shlex
from pathlib import Path

from . import mcp_client, ui
from .mcp_client import НегоднаяЗапись, ОшибкаФайла, Сервер
from .output import append_log, replace_log, target_pane
from .state import State

# Библиотека MCP пишет предупреждения через `logging`. Без единого обработчика Python печатает
# их прямо в поток ошибок — поверх полноэкранного интерфейса. Пустой обработчик на её логгере
# это выключает, ничего не меняя для того, кто однажды заведёт журнал всерьёз.
logging.getLogger("mcp").addHandler(logging.NullHandler())

# Имя сервера входит в `mcp__<сервер>__<инструмент>`, а оно станет именем функции для модели;
# API принимает имена функций только из этих знаков.
_ИМЯ = re.compile(r"^[A-Za-z0-9_-]+$")

СЛОВА = (
    ("add", "добавить сервер: /mcp add <имя> <адрес> | /mcp add <имя> -- <команда>"),
    ("remove", "убрать сервер: /mcp remove <имя>"),
)

ПОДСКАЗКА_ДОБАВЛЕНИЯ = (
    "/mcp add <имя> <адрес> [--header \"Имя: значение\"] — сервер HTTP; "
    "/mcp add <имя> [--env КЛЮЧ=значение] -- <команда> [доводы] — местный сервер"
)


def путь_файла() -> Path:
    return Path.cwd() / mcp_client.ИМЯ_ФАЙЛА


def _буквальное(значение: str) -> bool:
    return "${" not in значение


# Что в Authorization может стоять буквально рядом с подстановкой: только слово схемы.
# Всё прочее — кусок токена, и «ghp_abc${E}» обходил бы отказ одной подстановкой в хвосте.
_СХЕМА_ВХОДА = re.compile(r"^\s*(?:[A-Za-z]+\s+)?$")


def _токен_открыт(значение: str) -> bool:
    без_подстановок = mcp_client._ПЕРЕМЕННАЯ.sub("", значение)
    return _буквальное(значение) or not _СХЕМА_ВХОДА.match(без_подстановок)


def разобрать_добавление(доводы: list[str]) -> tuple[Сервер, list[str]]:
    """Доводы после `add` — в запись сервера и список предупреждений.

    `ValueError` с текстом отказа, если запись не складывается. Способ передачи определяет сам
    довод: адрес `http(s)://` — HTTP, `-- <команда>` — stdio. Флаг `--transport`, как у Claude
    Code, не нужен: у него он выбирает между HTTP и SSE, а SSE снят протоколом.
    """
    if not доводы:
        raise ValueError(f"нужно имя сервера — {ПОДСКАЗКА_ДОБАВЛЕНИЯ}")
    имя, *остаток = доводы
    if not _ИМЯ.match(имя):
        raise ValueError(
            f"имя «{имя}» не годится: только латиница, цифры, «_» и «-» — оно войдёт в имя "
            "инструмента mcp__<сервер>__<инструмент>"
        )
    команда: list[str] = []
    if "--" in остаток:
        граница = остаток.index("--")
        остаток, команда = остаток[:граница], остаток[граница + 1 :]
        if not команда:
            raise ValueError("после «--» нужна команда запуска сервера")

    заголовки: dict[str, str] = {}
    окружение: dict[str, str] = {}
    адреса: list[str] = []
    i = 0
    while i < len(остаток):
        флаг = остаток[i]
        if флаг in ("--header", "-H", "--env", "-e"):
            if i + 1 >= len(остаток):
                raise ValueError(f"после {флаг} нужно значение")
            значение = остаток[i + 1]
            if флаг in ("--header", "-H"):
                ключ, двоеточие, текст = значение.partition(":")
                # Сам довод не повторяем: самая частая опечатка — забытое двоеточие в
                # «Authorization Bearer <токен>», и повтор вывел бы токен на экран.
                if not двоеточие or not ключ.strip():
                    raise ValueError(f"после {флаг} нужно \"Имя: значение\" — двоеточия нет")
                заголовки[ключ.strip()] = текст.strip()
            else:
                ключ, равно, текст = значение.partition("=")
                if not равно or not ключ:
                    raise ValueError(f"после {флаг} нужно КЛЮЧ=значение — нет имени или знака «=»")
                окружение[ключ] = текст
            i += 2
        elif флаг.startswith("-"):
            raise ValueError(f"незнакомый флаг {флаг} — {ПОДСКАЗКА_ДОБАВЛЕНИЯ}")
        else:
            адреса.append(флаг)
            i += 1

    if команда:
        if адреса:
            raise ValueError(f"лишнее перед «--»: {' '.join(адреса)} — адрес и команда вместе не бывают")
        if заголовки:
            raise ValueError("--header — для сервера HTTP; местному серверу нужен --env")
        сервер = Сервер(имя=имя, способ="stdio", команда=команда[0], доводы=tuple(команда[1:]), окружение=окружение)
    else:
        if len(адреса) != 1 or not адреса[0].startswith(("http://", "https://")):
            raise ValueError(f"нужен адрес http(s):// или «-- <команда>» — {ПОДСКАЗКА_ДОБАВЛЕНИЯ}")
        if окружение:
            raise ValueError("--env — для местного сервера; серверу HTTP нужен --header")
        сервер = Сервер(имя=имя, способ="http", адрес=адреса[0], заголовки=заголовки)

    # Репозиторий челленджа публичный, и `.mcp.json` песочницы уходит в него вместе с днём.
    # Токен в `Authorization` — тайна всегда, поэтому буквальный отклоняется; прочие значения
    # бывают и безобидными, их записываем, но говорим вслух.
    предупреждения: list[str] = []
    for ключ, значение in заголовки.items():
        if ключ.lower() == "authorization" and _токен_открыт(значение):
            raise ValueError(
                "токен в Authorization открытым текстом не записываю: .mcp.json уйдёт в "
                "репозиторий — пишите \"Authorization: Bearer ${ПЕРЕМЕННАЯ}\""
            )
        if ключ.lower() == "authorization" or not _буквальное(значение):
            continue
        предупреждения.append(f"заголовок {ключ} записан в .mcp.json открытым текстом")
    for ключ, значение in окружение.items():
        if _буквальное(значение):
            предупреждения.append(f"переменная {ключ} записана в .mcp.json открытым текстом")
    return сервер, предупреждения


def _показ(итог: mcp_client.Итог) -> ui.Fragments:
    сервер = итог.сервер
    if isinstance(сервер, НегоднаяЗапись):
        return ui.error_fragments(f"{сервер.имя} — {итог.сбой}")
    if итог.сбой is not None:
        return ui.error_fragments(f"{сервер.имя} · {сервер.способ} — {итог.сбой}")
    части = ui.system_fragments(
        f"{сервер.имя} · {сервер.способ} · {итог.имя_на_сервере} · протокол {итог.протокол}"
        f" · инструментов {len(итог.инструменты)}"
    )
    for инструмент in итог.инструменты:
        строки = инструмент.описание.strip().splitlines()
        первая = строки[0].strip() if строки else ""
        хвост = f" — {первая}" if первая else ""
        части += [("class:dim", f"    {инструмент.полное_имя}{хвост}"), ("", "\n")]
    return части


async def _показать_все(state: State) -> None:
    путь = путь_файла()
    try:
        записи = mcp_client.прочитать(путь)
    except ОшибкаФайла as exc:
        append_log(state, ui.error_fragments(str(exc)))
        return
    if not записи:
        append_log(state, ui.system_fragments(f"серверов MCP нет — {путь} пуст или не заведён"))
        append_log(state, ui.hint_fragments(ПОДСКАЗКА_ДОБАВЛЕНИЯ))
        return
    # Опрос идёт до предела запуска на сервер, и секунды тишины после Enter читались бы как
    # «команда не принята». Строка ожидания гаснет на месте пустыми фрагментами той же длины,
    # а итог дописывается в конец: за время опроса ниже могли лечь чужие строки, и идущий
    # обмен помнит номер своей строки ожидания — сдвиг ленты заставил бы его затирать чужое
    # (тот же приём, что `_снять_ожидание` в commands.py).
    панель = target_pane(state, None)
    ожидание = ui.system_fragments(f"подключаюсь к серверам MCP: {len(записи)}…")
    место = len(панель.log)
    append_log(state, ожидание, панель)
    try:
        итоги = await mcp_client.список_всех(путь)
    except ОшибкаФайла as exc:
        # Файл могли испортить между чтением и опросом.
        показ = ui.error_fragments(str(exc))
    else:
        показ = []
        for итог in итоги:
            показ += _показ(итог)
    finally:
        replace_log(state, панель, место, len(ожидание), [("", "")] * len(ожидание))
    append_log(state, показ, панель)


def _добавить(state: State, доводы: list[str]) -> None:
    try:
        сервер, предупреждения = разобрать_добавление(доводы)
        mcp_client.добавить(путь_файла(), сервер)
    except (ValueError, ОшибкаФайла) as exc:
        append_log(state, ui.error_fragments(str(exc)))
        return
    for текст in предупреждения:
        append_log(state, ui.hint_fragments(f"{текст}; для тайн — ${{ПЕРЕМЕННАЯ}}"))
    append_log(state, ui.system_fragments(f"сервер {сервер.имя} добавлен в {путь_файла()}; проверить — /mcp"))


def _убрать(state: State, доводы: list[str]) -> None:
    if len(доводы) != 1:
        append_log(state, ui.error_fragments("нужно одно имя: /mcp remove <имя>"))
        return
    try:
        mcp_client.убрать(путь_файла(), доводы[0])
    except ОшибкаФайла as exc:
        append_log(state, ui.error_fragments(str(exc)))
        return
    append_log(state, ui.system_fragments(f"сервер {доводы[0]} убран из {путь_файла()}"))


async def cmd_mcp(state: State, arg: str) -> None:
    try:
        доводы = shlex.split(arg)
    except ValueError as exc:
        append_log(state, ui.error_fragments(f"не разобрать доводы: {exc}"))
        return
    if not доводы:
        await _показать_все(state)
    elif доводы[0] == "add":
        _добавить(state, доводы[1:])
    elif доводы[0] == "remove":
        _убрать(state, доводы[1:])
    else:
        append_log(state, ui.error_fragments(f"незнакомое слово «{доводы[0]}»: /mcp, /mcp add, /mcp remove"))
