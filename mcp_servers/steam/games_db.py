"""Хранилище игрового агента: один файл SQLite на три процесса.

С файлом работают сразу сервер MCP (только чтение), задания по расписанию и веб (оба пишут).
Поэтому режим `WAL` (читатель не ждёт писателя), ожидание замка 5000 мс и короткие транзакции:
каждая функция записи открывает свою `BEGIN IMMEDIATE` и закрывает её сразу.

`BEGIN IMMEDIATE`, а не отложенная транзакция: при отложенной два процесса могут оба прочитать,
затем оба попросить запись — и один получит `database is locked` без ожидания. Немедленная
берёт замок записи первой же командой и ждёт его в пределах `busy_timeout`.

Файл живёт месяцами и переживает обновления кода. Пересоздавать его нельзя никогда; файл с
версией схемы новее известной код не трогает и падает с понятным текстом.

Сети модуль не знает, карточку myharness не читает.
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

from steam_store import МАГАЗИН, Игра, Отзывы

ВЕРСИЯ_СХЕМЫ = 1
ИМЯ_ФАЙЛА = "games.sqlite"
# Правило проекта: всё, что пишет в домашний каталог, переопределяется окружением.
ПЕРЕМЕННАЯ_КАТАЛОГА = "GAMES_DATA_DIR"
КАТАЛОГ_ПО_УМОЛЧАНИЮ = Path("~/.local/share/games-agent")
ОЖИДАНИЕ_ЗАМКА_МС = 5000
РОДЫ_ЛЕНТЫ = ("скидка", "сбой", "итог")

СХЕМА = """
CREATE TABLE IF NOT EXISTS meta (
    ключ TEXT PRIMARY KEY,
    значение TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    номер INTEGER PRIMARY KEY,
    имя TEXT NOT NULL,
    описание TEXT NOT NULL DEFAULT '',
    жанры TEXT NOT NULL DEFAULT '[]',
    дата_текст TEXT NOT NULL DEFAULT '',
    дата TEXT,
    вышла INTEGER NOT NULL DEFAULT 1,
    цена INTEGER,
    цена_без_скидки INTEGER,
    скидка INTEGER NOT NULL DEFAULT 0,
    валюта TEXT NOT NULL DEFAULT '',
    бесплатна INTEGER NOT NULL DEFAULT 0,
    отзывы_сводка TEXT NOT NULL DEFAULT '',
    отзывы_положительных INTEGER NOT NULL DEFAULT 0,
    отзывы_всего INTEGER NOT NULL DEFAULT 0,
    обновлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    предложено TEXT NOT NULL,
    оценка INTEGER,
    причина_оценки TEXT,
    оценено TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    добавлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    номер INTEGER PRIMARY KEY,
    цена INTEGER NOT NULL,
    скидка INTEGER NOT NULL,
    отмечено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed (
    номер INTEGER PRIMARY KEY AUTOINCREMENT,
    время TEXT NOT NULL,
    род TEXT NOT NULL,
    текст TEXT NOT NULL
);
"""


class НоваяСхема(RuntimeError):
    """Файл записан кодом новее этого: читать и писать его старым кодом нельзя."""


def путь() -> Path:
    """`$GAMES_DATA_DIR/games.sqlite`; без переменной — `~/.local/share/games-agent/`."""
    каталог = os.environ.get(ПЕРЕМЕННАЯ_КАТАЛОГА)
    return (Path(каталог) if каталог else КАТАЛОГ_ПО_УМОЛЧАНИЮ).expanduser() / ИМЯ_ФАЙЛА


def _сейчас() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def открыть(только_чтение: bool = False) -> sqlite3.Connection:
    """Соединение с файлом. На запись — каталог и схема создаются, если их нет.

    `isolation_level=None`: модуль сам открывает транзакции (`BEGIN IMMEDIATE`), неявная
    отложенная транзакция модуля `sqlite3` дала бы мгновенный отказ замка, описанный выше.
    """
    файл = путь()
    if только_чтение:
        if not файл.exists():
            raise FileNotFoundError(f"хранилища игр нет: {файл}")
        с = sqlite3.connect(
            f"{файл.as_uri()}?mode=ro", uri=True, isolation_level=None,
            timeout=ОЖИДАНИЕ_ЗАМКА_МС / 1000,
        )
    else:
        файл.parent.mkdir(parents=True, exist_ok=True)
        с = sqlite3.connect(файл, isolation_level=None, timeout=ОЖИДАНИЕ_ЗАМКА_МС / 1000)
    try:
        с.row_factory = sqlite3.Row
        с.execute(f"PRAGMA busy_timeout = {ОЖИДАНИЕ_ЗАМКА_МС}")
        if not только_чтение:
            # Режим журнала записан в самом файле: достаточно поставить его пишущему.
            _включить_wal(с)
        _сверить_схему(с, файл, только_чтение)
    except BaseException:
        с.close()
        raise
    return с


def _включить_wal(с: sqlite3.Connection) -> None:
    """`PRAGMA journal_mode` не ждёт замка по `busy_timeout`: два процесса, разом открывшие
    новый файл, получали отказ сразу (замер просмотра — 2–3 из 4). Ждём сами, тот же срок."""
    срок = time.monotonic() + ОЖИДАНИЕ_ЗАМКА_МС / 1000
    while True:
        try:
            с.execute("PRAGMA journal_mode = WAL")
            return
        except sqlite3.OperationalError as exc:
            if "locked" not in str(exc) or time.monotonic() >= срок:
                raise
            time.sleep(0.05)


def _версия(с: sqlite3.Connection) -> int | None:
    есть = с.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'meta'"
    ).fetchone()
    if not есть:
        return None
    строка = с.execute("SELECT значение FROM meta WHERE ключ = 'версия_схемы'").fetchone()
    return int(строка[0]) if строка else None


def _сверить_схему(с: sqlite3.Connection, файл: Path, только_чтение: bool) -> None:
    версия = _версия(с)
    if версия is not None and версия > ВЕРСИЯ_СХЕМЫ:
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, этот код знает только {ВЕРСИЯ_СХЕМЫ}. "
            "Обнови код; файл не тронут."
        )
    if версия == ВЕРСИЯ_СХЕМЫ:
        return
    if версия is not None:
        # Перехода со старой версии пока нет: версия одна. Отметить старый файл новой версией
        # без перестройки таблиц значило бы сломать запросы молча.
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, код ждёт {ВЕРСИЯ_СХЕМЫ}, а перехода нет. Файл не тронут."
        )
    if только_чтение:
        raise НоваяСхема(f"{файл}: схема не создана — файл ещё ни разу не открывали на запись")
    with _запись(с):
        # Внутри замка версия читается заново: другой процесс мог создать схему раньше.
        версия = _версия(с)
        if версия is not None and версия > ВЕРСИЯ_СХЕМЫ:
            raise НоваяСхема(f"{файл}: версия схемы {версия}, этот код знает только {ВЕРСИЯ_СХЕМЫ}")
        # `executescript` сам завершает открытую транзакцию, поэтому таблицы создаются
        # по одной командой `execute` — внутри той же транзакции, что и запись версии.
        for команда in СХЕМА.split(";"):
            if команда.strip():
                с.execute(команда)
        с.execute(
            "INSERT INTO meta (ключ, значение) VALUES ('версия_схемы', ?) "
            "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
            (str(ВЕРСИЯ_СХЕМЫ),),
        )


@contextmanager
def _запись(с: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """Одна короткая транзакция записи: фиксация на выходе, откат при исключении."""
    with с:
        с.execute("BEGIN IMMEDIATE")
        yield с


# ---- запись ----


def запомнить_игру(с: sqlite3.Connection, игра: Игра) -> None:
    """Строка игры по номеру. Повтор обновляет все поля: магазин — источник правды."""
    with _запись(с):
        с.execute(
            """
            INSERT INTO games (номер, имя, описание, жанры, дата_текст, дата, вышла, цена,
                цена_без_скидки, скидка, валюта, бесплатна, отзывы_сводка,
                отзывы_положительных, отзывы_всего, обновлено)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                имя = excluded.имя, описание = excluded.описание, жанры = excluded.жанры,
                дата_текст = excluded.дата_текст, дата = excluded.дата, вышла = excluded.вышла,
                цена = excluded.цена, цена_без_скидки = excluded.цена_без_скидки,
                скидка = excluded.скидка, валюта = excluded.валюта,
                бесплатна = excluded.бесплатна, отзывы_сводка = excluded.отзывы_сводка,
                отзывы_положительных = excluded.отзывы_положительных,
                отзывы_всего = excluded.отзывы_всего, обновлено = excluded.обновлено
            """,
            (
                игра.номер, игра.имя, игра.описание,
                json.dumps(list(игра.жанры), ensure_ascii=False),
                игра.дата_текст, игра.дата.isoformat() if игра.дата else None,
                int(игра.вышла), игра.цена, игра.цена_без_скидки, игра.скидка, игра.валюта,
                int(игра.бесплатна), игра.отзывы.сводка, игра.отзывы.положительных,
                игра.отзывы.всего, _сейчас(),
            ),
        )


def предложить(с: sqlite3.Connection, номер: int, причина: str) -> bool:
    """Заводит предложение. `False` — игра уже предлагалась, с любой оценкой или без неё."""
    with _запись(с):
        курсор = с.execute(
            "INSERT OR IGNORE INTO suggestions (номер, причина, предложено) VALUES (?, ?, ?)",
            (номер, причина, _сейчас()),
        )
        return курсор.rowcount == 1


def оценить(с: sqlite3.Connection, номер: int, плюс: bool, причина: str) -> None:
    """Оценка человека. Игре календаря строки предложения может не быть — она заводится.
    Плюс кладёт игру в желаемое: желаемое — это предложения с плюсом."""
    сейчас = _сейчас()
    with _запись(с):
        с.execute(
            """
            INSERT INTO suggestions (номер, причина, предложено, оценка, причина_оценки, оценено)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                оценка = excluded.оценка, причина_оценки = excluded.причина_оценки,
                оценено = excluded.оценено
            """,
            (номер, причина, сейчас, int(плюс), причина, сейчас),
        )


def снять_оценку(с: sqlite3.Connection, номер: int) -> None:
    """Удаляет строку предложения целиком: игру можно предложить снова."""
    with _запись(с):
        с.execute("DELETE FROM suggestions WHERE номер = ?", (номер,))


def в_календарь(с: sqlite3.Connection, номер: int, причина: str) -> bool:
    """`False` — игра уже стоит в календаре."""
    with _запись(с):
        курсор = с.execute(
            "INSERT OR IGNORE INTO calendar (номер, причина, добавлено) VALUES (?, ?, ?)",
            (номер, причина, _сейчас()),
        )
        return курсор.rowcount == 1


def отметить_цену(
    с: sqlite3.Connection, номер: int, цена: int, скидка: int, текст_ленты: str | None = None
) -> bool:
    """Записывает последнюю известную цену. `True` — новая скидка: ровно один раз на пару
    «игра, цена со скидкой». Новая — если скидка есть и прежней записи нет, либо прежняя цена
    другая, либо прежде скидки не было.

    `текст_ленты` новой скидки ложится в ленту В ТОЙ ЖЕ транзакции: иначе обрыв между двумя
    записями оставил бы скидку отмеченной, но не сообщённой — и назавтра она пропала бы молча."""
    with _запись(с):
        прежняя = с.execute(
            "SELECT цена, скидка FROM prices WHERE номер = ?", (номер,)
        ).fetchone()
        новая = скидка > 0 and (
            прежняя is None or прежняя["цена"] != цена or прежняя["скидка"] == 0
        )
        с.execute(
            """
            INSERT INTO prices (номер, цена, скидка, отмечено) VALUES (?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                цена = excluded.цена, скидка = excluded.скидка, отмечено = excluded.отмечено
            """,
            (номер, цена, скидка, _сейчас()),
        )
        if новая and текст_ленты:
            с.execute(
                "INSERT INTO feed (время, род, текст) VALUES (?, ?, ?)",
                (_сейчас(), "скидка", текст_ленты),
            )
        return новая


def в_ленту(с: sqlite3.Connection, род: str, текст: str) -> None:
    """Строка ленты. Род — `скидка`, `сбой` либо `итог`; чужой род — ошибка вызова."""
    if род not in РОДЫ_ЛЕНТЫ:
        raise ValueError(f"род ленты {род!r}: допустимы {', '.join(РОДЫ_ЛЕНТЫ)}")
    with _запись(с):
        с.execute(
            "INSERT INTO feed (время, род, текст) VALUES (?, ?, ?)", (_сейчас(), род, текст)
        )


# ---- чтение ----

# Поля игры для соединения с другими таблицами. Игры может не быть в `games`
# (предложение завели раньше, чем запомнили игру), поэтому везде `LEFT JOIN`.
_ПОЛЯ_ИГРЫ = """
    g.имя, g.описание, g.жанры, g.дата_текст, g.дата, g.вышла, g.цена, g.цена_без_скидки,
    g.скидка, g.валюта, g.бесплатна, g.отзывы_сводка, g.отзывы_положительных,
    g.отзывы_всего, g.обновлено
"""


def _словарь(строка: sqlite3.Row) -> dict:
    """Строка в словарь: жанры — список, флаги — `bool`, ссылка и текст отзывов добавлены."""
    д = dict(строка)
    if "жанры" in д:
        д["жанры"] = json.loads(д["жанры"]) if д["жанры"] else []
    for флаг in ("вышла", "бесплатна"):
        if д.get(флаг) is not None:
            д[флаг] = bool(д[флаг])
    if "оценка" in д and д["оценка"] is not None:
        д["оценка"] = bool(д["оценка"])
    if д.get("отзывы_всего") is not None:
        д["отзывы"] = Отзывы(
            д["отзывы_сводка"] or "", д["отзывы_положительных"] or 0, д["отзывы_всего"]
        ).текст()
    д["ссылка"] = f"{МАГАЗИН}/app/{д['номер']}/"
    return д


def игра(с: sqlite3.Connection, номер: int) -> dict | None:
    строка = с.execute(
        f"SELECT g.номер, {_ПОЛЯ_ИГРЫ} FROM games g WHERE g.номер = ?", (номер,)
    ).fetchone()
    return _словарь(строка) if строка else None


def предложения(с: sqlite3.Connection, только_без_оценки: bool = False) -> list[dict]:
    """Предложения с полями игры, новые первыми."""
    условие = "WHERE s.оценка IS NULL" if только_без_оценки else ""
    строки = с.execute(
        f"""
        SELECT s.номер, s.причина, s.предложено, s.оценка, s.причина_оценки, s.оценено,
            {_ПОЛЯ_ИГРЫ}
        FROM suggestions s LEFT JOIN games g ON g.номер = s.номер
        {условие}
        ORDER BY s.предложено DESC, s.rowid DESC
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def календарь(с: sqlite3.Connection) -> list[dict]:
    """Игры календаря по дате выхода; без точной даты — в конце."""
    строки = с.execute(
        f"""
        SELECT c.номер, c.причина, c.добавлено, {_ПОЛЯ_ИГРЫ}
        FROM calendar c LEFT JOIN games g ON g.номер = c.номер
        ORDER BY g.дата IS NULL, g.дата, c.добавлено, c.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def желаемое(с: sqlite3.Connection) -> list[dict]:
    """Игры с плюсом: поля игры и последняя известная цена из `отметить_цену`.
    Поля `цена` и `скидка` — из карточки игры; `последняя_цена`, `последняя_скидка` и
    `цена_отмечена` — из отметок цены, `None`, если цену ещё не отмечали."""
    строки = с.execute(
        f"""
        SELECT s.номер, s.причина_оценки, s.оценено, {_ПОЛЯ_ИГРЫ},
            p.цена AS последняя_цена, p.скидка AS последняя_скидка, p.отмечено AS цена_отмечена
        FROM suggestions s
        LEFT JOIN games g ON g.номер = s.номер
        LEFT JOIN prices p ON p.номер = s.номер
        WHERE s.оценка = 1
        ORDER BY s.оценено DESC, s.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def показанные(с: sqlite3.Connection) -> set[int]:
    """Номера, которые повторно не судить: предложения и календарь."""
    строки = с.execute(
        "SELECT номер FROM suggestions UNION SELECT номер FROM calendar"
    ).fetchall()
    return {с_[0] for с_ in строки}


def лента(с: sqlite3.Connection, сколько: int | None = None) -> list[dict]:
    """Строки ленты, новые первыми. `None` — все."""
    запрос = "SELECT номер, время, род, текст FROM feed ORDER BY номер DESC"
    if сколько is None:
        строки = с.execute(запрос).fetchall()
    else:
        строки = с.execute(запрос + " LIMIT ?", (сколько,)).fetchall()
    return [dict(с_) for с_ in строки]
