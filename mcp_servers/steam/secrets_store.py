"""Тайны игрового агента: ключи Steam Web API, IsThereAnyDeal, DeepSeek и SteamID человека.

Файл — `$GAMES_CONFIG_DIR/config.json`, умолчание `~/.config/games-agent`. Переопределение пути
нужно проверкам: без него они писали бы в настоящий файл и затирали ключи (`CLAUDE.md`, «Грабли»).

Каталог — права 700, файл — 600. Права ставятся ДО записи содержимого: временный файл рождается
сразу с 600 и подменяет прежний через `os.replace`. Иначе был бы миг, когда ключ лежит в файле с
правами по `umask` (обычно 644), и его успел бы прочесть другой пользователь Мака.

Ключ не попадает ни в одно исключение модуля: испорченный файл называется путём, без содержимого.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

# Поля, которые прячет `скрыть`. SteamID тайной не считается: его видно в адресе профиля.
_КЛЮЧИ = ("steam_ключ", "itad_ключ", "deepseek_ключ")
ЗАМЕНА = "***"


class ОшибкаТайн(Exception):
    """Файл тайн есть, но не читается. Текст называет путь и никогда — содержимое."""


@dataclass
class Тайны:
    steam_ключ: str = ""
    steamid: str = ""
    itad_ключ: str = ""
    # Ключ DeepSeek агента живёт здесь, а не в настройках `myharness`: решение ведущего
    # 2026-10-16 — агент отдаётся другому человеку и не должен трогать чужой файл настроек.
    deepseek_ключ: str = ""


def каталог() -> Path:
    свой = os.environ.get("GAMES_CONFIG_DIR")
    return Path(свой).expanduser() if свой else Path.home() / ".config" / "games-agent"


def путь() -> Path:
    return каталог() / "config.json"


def загрузить() -> Тайны:
    """Тайны из файла. Файла нет — пустые тайны: первый запуск, мастер спросит ключи.

    Файл не JSON либо не объект — `ОшибкаТайн`. Молча вернуть пустые тайны нельзя: следующий
    `сохранить` записал бы их поверх и стёр ключи, которые человек ещё мог бы поправить руками."""
    файл = путь()
    испорчен = False
    try:
        сырое = файл.read_bytes()
    except FileNotFoundError:
        return Тайны()
    except OSError as exc:
        # Имя рода ошибки, а не её текст: так короче и не зависит от того, что в нём окажется.
        причина = type(exc).__name__
        raise ОшибкаТайн(f"{файл}: файл тайн не читается ({причина})") from None
    try:
        данные = json.loads(сырое.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        # Исключение не поднимается здесь же: `JSONDecodeError.doc` и `UnicodeDecodeError.object`
        # держат весь файл, а исключение, поднятое внутри `except`, хранит их в `__context__`.
        испорчен = True
        данные = None
    if испорчен or not isinstance(данные, dict):
        raise ОшибкаТайн(f"{файл}: файл тайн испорчен — не объект JSON")
    # Поле не строкой (правка руками) равносильно пустому: так же поступает `myharness`
    # с испорченным полем своих настроек.
    return Тайны(**{
        поле: значение if isinstance(значение := данные.get(поле), str) else ""
        for поле in Тайны.__dataclass_fields__
    })


def сохранить(т: Тайны) -> None:
    """Записать тайны целиком. Читатель видит либо прежний файл, либо новый, но не обрезанный."""
    папка = каталог()
    папка.mkdir(parents=True, exist_ok=True, mode=0o700)
    # `mode` у `mkdir` урезается `umask` и не трогает уже существующий каталог — ставим явно.
    os.chmod(папка, 0o700)
    # Символическая ссылка — пишем в её цель: `os.replace` иначе подменил бы саму ссылку.
    файл = путь().resolve()
    текст = json.dumps(asdict(т), ensure_ascii=False, indent=2)
    # `mkstemp` создаёт файл с `O_EXCL` и правами 600 одним вызовом и даёт неповторимое имя:
    # два одновременных сохранения (веб и задание) не пишут в один временный файл.
    дескриптор, временный = tempfile.mkstemp(dir=файл.parent, prefix=f".{файл.name}.", suffix=".tmp")
    try:
        with os.fdopen(дескриптор, "w", encoding="utf-8") as поток:
            поток.write(текст)
            поток.flush()
            os.fsync(поток.fileno())
        os.chmod(временный, 0o600)
        os.replace(временный, файл)
    except BaseException:
        Path(временный).unlink(missing_ok=True)
        raise


def скрыть(текст: str, т: Тайны) -> str:
    """Каждый непустой ключ в тексте — «***». Длинный ключ прячется первым: ключ, вложенный в
    другой, иначе оставил бы от длинного видимый хвост."""
    for ключ in sorted((getattr(т, поле) for поле in _КЛЮЧИ), key=len, reverse=True):
        if ключ:
            текст = текст.replace(ключ, ЗАМЕНА)
    return текст
