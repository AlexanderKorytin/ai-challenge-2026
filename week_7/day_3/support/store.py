"""Файл данных поддержки: чтение, запись под блокировкой, заведение пользователя.

В файл пишут несколько процессов сразу: сервер `support` каждого веб-сеанса и веб-приложение при
входе. Поэтому запись идёт так: блокировка `fcntl.flock` на соседнем файле `<данные>.lock`, чтение
свежих данных, правка, запись во временный файл рядом и `os.replace`. Обрыв посреди записи не
оставит половину JSON, а две одновременные правки не затрут друг друга.

Путь файла задаёт `SUPPORT_DATA`, умолчание — `данные.json` рядом с модулем.
"""

from __future__ import annotations

import fcntl
import json
import os
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Callable, TypeVar

from mcp.server.mcpserver.exceptions import ToolError

ПЕРЕМЕННАЯ_ДАННЫХ = "SUPPORT_DATA"
ДАННЫЕ_ПО_УМОЛЧАНИЮ = Path(__file__).parent / "данные.json"

T = TypeVar("T")


def путь() -> Path:
    return Path(os.environ.get(ПЕРЕМЕННАЯ_ДАННЫХ) or ДАННЫЕ_ПО_УМОЛЧАНИЮ)


def _прочитать(файл: Path) -> dict[str, Any]:
    try:
        return json.loads(файл.read_text("utf-8"))
    except FileNotFoundError:
        raise ToolError(f"файла данных {файл} нет") from None
    except (OSError, ValueError) as exc:
        raise ToolError(f"файл данных {файл} не читается: {type(exc).__name__}") from None


def прочитать() -> dict[str, Any]:
    """Данные без блокировки: `os.replace` подменяет файл целиком, полузаписанного не бывает."""
    return _прочитать(путь())


def изменить(правка: Callable[[dict[str, Any]], T]) -> T:
    """Прочитать, поправить и записать данные под блокировкой. Возвращает итог `правка`.

    Исключение из `правка` отменяет запись: файл остаётся прежним."""
    файл = путь()
    with open(файл.with_name(файл.name + ".lock"), "a") as замок:
        fcntl.flock(замок, fcntl.LOCK_EX)
        try:
            данные = _прочитать(файл)
            итог = правка(данные)
            дескриптор, временный = tempfile.mkstemp(dir=файл.parent, prefix=файл.name, suffix=".tmp")
            try:
                with os.fdopen(дескриптор, "w", encoding="utf-8") as поток:
                    json.dump(данные, поток, ensure_ascii=False, indent=2)
                    поток.write("\n")
                    поток.flush()
                    os.fsync(поток.fileno())
                os.replace(временный, файл)
            except BaseException:
                Path(временный).unlink(missing_ok=True)
                raise
            return итог
        finally:
            fcntl.flock(замок, fcntl.LOCK_UN)


def следующий_номер(записи: list[dict[str, Any]], поле: str, приставка: str, ширина: int) -> str:
    """Наибольший номер вида `<приставка>NN` плюс один, с нулями до `ширина` цифр."""
    числа = [
        int(з[поле][len(приставка):])
        for з in записи
        if з[поле].startswith(приставка) and з[поле][len(приставка):].isdecimal()
    ]
    return f"{приставка}{(max(числа, default=0) + 1):0{ширина}d}"


def войти(имя: str) -> dict[str, Any]:
    """Пользователь по имени без учёта регистра; нет такого — заводится с пустым окружением."""
    имя = " ".join(имя.split())
    if not имя:
        raise ValueError("имя пустое")

    def правка(данные: dict[str, Any]) -> dict[str, Any]:
        for п in данные["users"]:
            if п["name"].casefold() == имя.casefold():
                return п
        новый = {
            "user_id": следующий_номер(данные["users"], "user_id", "U-", 2),
            "name": имя,
            "email": "",
            "registered": date.today().isoformat(),
            "os": "",
            "provider": "",
            "model": "",
            "env": {},
            "mcp_servers": [],
        }
        данные["users"].append(новый)
        return новый

    return изменить(правка)
