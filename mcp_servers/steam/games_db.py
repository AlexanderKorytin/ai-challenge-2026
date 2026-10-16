"""Хранилище игрового агента: один файл SQLite на три процесса.

С файлом работают сразу сервер MCP (только чтение), задания по расписанию и веб (оба пишут).
Поэтому режим `WAL` (читатель не ждёт писателя), ожидание замка 5000 мс и короткие транзакции:
каждая функция записи открывает свою `BEGIN IMMEDIATE` и закрывает её сразу.

`BEGIN IMMEDIATE`, а не отложенная транзакция: при отложенной два процесса могут оба прочитать,
затем оба попросить запись — и один получит `database is locked` без ожидания. Немедленная
берёт замок записи первой же командой и ждёт его в пределах `busy_timeout`.

Файл живёт месяцами и переживает обновления кода. Пересоздавать его нельзя никогда; файл с
версией схемы новее известной код не трогает и падает с понятным текстом. Файл версии 1 первое
открытие на запись переводит на версию 2 одной транзакцией: добавляет таблицы, прежние не трогает.

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

ВЕРСИЯ_СХЕМЫ = 3
ИМЯ_ФАЙЛА = "games.sqlite"
# Правило проекта: всё, что пишет в домашний каталог, переопределяется окружением.
ПЕРЕМЕННАЯ_КАТАЛОГА = "GAMES_DATA_DIR"
КАТАЛОГ_ПО_УМОЛЧАНИЮ = Path("~/.local/share/games-agent")
ОЖИДАНИЕ_ЗАМКА_МС = 5000
# «выход» — игра календаря вышла сегодня; «сверка» — пора сверить вкус (раз в полгода).
РОДЫ_ЛЕНТЫ = ("скидка", "сбой", "итог", "выход", "сверка")

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

# Таблицы, которые добавила версия 2. Переход 1 → 2 — только добавление: таблицы версии 1 не
# меняются, и их данные целы. Новый файл получает обе части схемы сразу.
# Версия 3: адрес обложки из магазина. У новых игр Steam кладёт её по пути с хешем, и по номеру
# игры его не угадать (живой сценарий: у Sengoku Rance обложки не было).
СХЕМА_3 = """
ALTER TABLE games ADD COLUMN обложка TEXT
"""

СХЕМА_2 = """
CREATE TABLE IF NOT EXISTS refusals (
    номер INTEGER NOT NULL,
    отпечаток TEXT NOT NULL,
    отказано TEXT NOT NULL,
    PRIMARY KEY (номер, отпечаток)
);
CREATE TABLE IF NOT EXISTS library (
    номер INTEGER PRIMARY KEY,
    имя TEXT NOT NULL,
    часы REAL NOT NULL DEFAULT 0,
    часы_2нед REAL NOT NULL DEFAULT 0,
    последний_запуск INTEGER
);
CREATE TABLE IF NOT EXISTS steam_wishlist (
    номер INTEGER PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS played (
    номер INTEGER PRIMARY KEY,
    понравилась INTEGER NOT NULL,
    отмечено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS marks (
    ключ TEXT PRIMARY KEY,
    значение TEXT NOT NULL
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
    _отвергнуть_версию(версия, файл)
    if версия == ВЕРСИЯ_СХЕМЫ:
        return
    if только_чтение:
        # Читатель — сервер MCP — переходить не вправе: переход пишет в файл.
        if версия is None:
            raise НоваяСхема(f"{файл}: схема не создана — файл ещё ни разу не открывали на запись")
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, код ждёт {ВЕРСИЯ_СХЕМЫ}. Переход сделает первое "
            "открытие на запись — задание или веб."
        )
    with _запись(с):
        # Внутри замка версия читается заново: другой процесс мог создать схему или перейти
        # на новую версию раньше.
        версия = _версия(с)
        _отвергнуть_версию(версия, файл)
        if версия == ВЕРСИЯ_СХЕМЫ:
            return
        # `executescript` сам завершает открытую транзакцию, поэтому таблицы создаются
        # по одной командой `execute` — внутри той же транзакции, что и запись версии.
        # Обрыв посреди перехода откатывает всё: файл остаётся версии 1 целиком.
        части = {None: (СХЕМА, СХЕМА_2, СХЕМА_3), 1: (СХЕМА_2, СХЕМА_3), 2: (СХЕМА_3,)}[версия]
        for часть in части:
            for команда in часть.split(";"):
                if команда.strip():
                    с.execute(команда)
        с.execute(
            "INSERT INTO meta (ключ, значение) VALUES ('версия_схемы', ?) "
            "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
            (str(ВЕРСИЯ_СХЕМЫ),),
        )


def _отвергнуть_версию(версия: int | None, файл: Path) -> None:
    """Код знает пустой файл, версии 1 и 2 (переходы есть) и свою. Остальное — отказ, файл не тронут."""
    if версия is not None and версия > ВЕРСИЯ_СХЕМЫ:
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, этот код знает только {ВЕРСИЯ_СХЕМЫ}. "
            "Обнови код; файл не тронут."
        )
    if версия is not None and версия not in (1, 2, ВЕРСИЯ_СХЕМЫ):
        # Отметить незнакомый файл новой версией без перестройки таблиц значило бы сломать
        # запросы молча.
        raise НоваяСхема(
            f"{файл}: версия схемы {версия}, код ждёт {ВЕРСИЯ_СХЕМЫ}, а перехода нет. Файл не тронут."
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
                отзывы_положительных, отзывы_всего, обновлено, обложка)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                имя = excluded.имя, описание = excluded.описание, жанры = excluded.жанры,
                дата_текст = excluded.дата_текст, дата = excluded.дата, вышла = excluded.вышла,
                цена = excluded.цена, цена_без_скидки = excluded.цена_без_скидки,
                скидка = excluded.скидка, валюта = excluded.валюта,
                бесплатна = excluded.бесплатна, отзывы_сводка = excluded.отзывы_сводка,
                отзывы_положительных = excluded.отзывы_положительных,
                отзывы_всего = excluded.отзывы_всего, обновлено = excluded.обновлено,
                -- Игра без адреса обложки (собрана не из appdetails) не стирает прежний адрес.
                обложка = COALESCE(excluded.обложка, games.обложка)
            """,
            (
                игра.номер, игра.имя, игра.описание,
                json.dumps(list(игра.жанры), ensure_ascii=False),
                игра.дата_текст, игра.дата.isoformat() if игра.дата else None,
                int(игра.вышла), игра.цена, игра.цена_без_скидки, игра.скидка, игра.валюта,
                int(игра.бесплатна), игра.отзывы.сводка, игра.отзывы.положительных,
                игра.отзывы.всего, _сейчас(), игра.обложка or None,
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


def запомнить_отказ(с: sqlite3.Connection, номер: int, отпечаток: str) -> None:
    """Модель отказала игре при этом вкусе. Повтор при том же отпечатке ничего не меняет."""
    with _запись(с):
        с.execute(
            "INSERT OR IGNORE INTO refusals (номер, отпечаток, отказано) VALUES (?, ?, ?)",
            (номер, отпечаток, _сейчас()),
        )


def убрать_из_календаря(с: sqlite3.Connection, номер: int) -> None:
    """Игра вышла или выпала из выдачи — строка календаря удаляется."""
    with _запись(с):
        с.execute("DELETE FROM calendar WHERE номер = ?", (номер,))


def обновить_библиотеку(с: sqlite3.Connection, игры: list) -> None:
    """Библиотека Steam целиком заменяет прежнюю одной транзакцией: проданная или удалённая
    игра уходит, читатель не видит половины. Элемент — любой объект с полями `номер`, `имя`,
    `часы`, `часы_2нед`, `последний_запуск` (`steam_account.Своя`)."""
    with _запись(с):
        с.execute("DELETE FROM library")
        с.executemany(
            "INSERT OR REPLACE INTO library (номер, имя, часы, часы_2нед, последний_запуск) "
            "VALUES (?, ?, ?, ?, ?)",
            [
                (и.номер, и.имя, float(и.часы), float(и.часы_2нед), и.последний_запуск)
                for и in игры
            ],
        )


def обновить_список_steam(с: sqlite3.Connection, номера: list[int]) -> None:
    """Список желаемого Steam целиком заменяет прежний одной транзакцией."""
    with _запись(с):
        с.execute("DELETE FROM steam_wishlist")
        с.executemany(
            "INSERT OR IGNORE INTO steam_wishlist (номер) VALUES (?)", [(н,) for н in номера]
        )


def уже_играл(с: sqlite3.Connection, номер: int, понравилась: bool) -> None:
    """Игра вне Steam, в которую человек уже играл: из выдачи она уходит. Повтор меняет ответ."""
    with _запись(с):
        с.execute(
            """
            INSERT INTO played (номер, понравилась, отмечено) VALUES (?, ?, ?)
            ON CONFLICT (номер) DO UPDATE SET
                понравилась = excluded.понравилась, отмечено = excluded.отмечено
            """,
            (номер, int(понравилась), _сейчас()),
        )


def поставить_отметку(с: sqlite3.Connection, ключ: str, значение: str) -> None:
    """Отметка заданий: «последняя_сверка», «выход_сообщён:<номер>». Своя таблица, а не `meta`:
    ключ отметки не может задеть версию схемы."""
    with _запись(с):
        с.execute(
            "INSERT INTO marks (ключ, значение) VALUES (?, ?) "
            "ON CONFLICT (ключ) DO UPDATE SET значение = excluded.значение",
            (ключ, значение),
        )


# ---- чтение ----

# Игры, которых выдача не показывает: с минусом, купленные, «уже играл».
_СКРЫТЫЕ = """
    SELECT номер FROM suggestions WHERE оценка = 0
    UNION SELECT номер FROM library
    UNION SELECT номер FROM played
"""

# Поля игры для соединения с другими таблицами. Игры может не быть в `games`
# (предложение завели раньше, чем запомнили игру), поэтому везде `LEFT JOIN`.
_ПОЛЯ_ИГРЫ = """
    g.имя, g.описание, g.жанры, g.дата_текст, g.дата, g.вышла, g.цена, g.цена_без_скидки,
    g.скидка, g.валюта, g.бесплатна, g.отзывы_сводка, g.отзывы_положительных,
    g.отзывы_всего, g.обновлено, g.обложка
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
    """Предложения с полями игры, новые первыми. Игр с минусом, купленных и «уже играл» нет."""
    условие = "AND s.оценка IS NULL" if только_без_оценки else ""
    строки = с.execute(
        f"""
        SELECT s.номер, s.причина, s.предложено, s.оценка, s.причина_оценки, s.оценено,
            {_ПОЛЯ_ИГРЫ}
        FROM suggestions s LEFT JOIN games g ON g.номер = s.номер
        WHERE s.номер NOT IN (SELECT номер FROM library UNION SELECT номер FROM played)
            AND s.оценка IS NOT 0 {условие}
        ORDER BY s.предложено DESC, s.rowid DESC
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def календарь(с: sqlite3.Connection) -> list[dict]:
    """Игры календаря по дате выхода; без точной даты — в конце. Игр с минусом, купленных и
    «уже играл» нет."""
    строки = с.execute(
        f"""
        SELECT c.номер, c.причина, c.добавлено, {_ПОЛЯ_ИГРЫ}
        FROM calendar c LEFT JOIN games g ON g.номер = c.номер
        WHERE c.номер NOT IN ({_СКРЫТЫЕ})
        ORDER BY g.дата IS NULL, g.дата, c.добавлено, c.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def желаемое(с: sqlite3.Connection) -> list[dict]:
    """Игры с плюсом и список желаемого Steam, без купленных: поля игры и последняя известная
    цена из `отметить_цену`. Своё — первым, по времени оценки; затем Steam — по номеру.

    `источник` — «свой» либо «steam»; игра в обоих списках — «свой». Поля `цена` и `скидка` —
    из карточки игры; `последняя_цена`, `последняя_скидка` и `цена_отмечена` — из отметок цены,
    `None`, если цену ещё не отмечали. Игры списка Steam может не быть в `games` — имя `None`."""
    строки = с.execute(
        f"""
        SELECT w.номер, w.источник, s.причина_оценки, s.оценено, {_ПОЛЯ_ИГРЫ},
            p.цена AS последняя_цена, p.скидка AS последняя_скидка, p.отмечено AS цена_отмечена
        FROM (
            SELECT номер, 'свой' AS источник FROM suggestions WHERE оценка = 1
            UNION ALL
            SELECT номер, 'steam' FROM steam_wishlist
            WHERE номер NOT IN (SELECT номер FROM suggestions WHERE оценка = 1)
        ) w
        LEFT JOIN suggestions s ON s.номер = w.номер AND s.оценка = 1
        LEFT JOIN games g ON g.номер = w.номер
        LEFT JOIN prices p ON p.номер = w.номер
        WHERE w.номер NOT IN (SELECT номер FROM library)
        ORDER BY w.источник = 'steam', s.оценено DESC, w.номер
        """
    ).fetchall()
    return [_словарь(с_) for с_ in строки]


def показанные(с: sqlite3.Connection) -> set[int]:
    """Номера, которые повторно не судить: предложения, календарь и «уже играл»."""
    строки = с.execute(
        "SELECT номер FROM suggestions UNION SELECT номер FROM calendar "
        "UNION SELECT номер FROM played"
    ).fetchall()
    return {с_[0] for с_ in строки}


def отказано(с: sqlite3.Connection, отпечаток: str) -> set[int]:
    """Номера, которым модель отказала при этом вкусе. Вкус другой — прежние отказы не в счёт."""
    строки = с.execute(
        "SELECT номер FROM refusals WHERE отпечаток = ?", (отпечаток,)
    ).fetchall()
    return {с_[0] for с_ in строки}


def открытых(с: sqlite3.Connection) -> int:
    """Число открытых предложений: без оценки, не купленных, не «уже играл»."""
    return с.execute(
        """
        SELECT count(*) FROM suggestions
        WHERE оценка IS NULL
            AND номер NOT IN (SELECT номер FROM library UNION SELECT номер FROM played)
        """
    ).fetchone()[0]


def библиотека(с: sqlite3.Connection) -> list[dict]:
    """Библиотека Steam по убыванию часов игры."""
    строки = с.execute(
        "SELECT номер, имя, часы, часы_2нед, последний_запуск FROM library "
        "ORDER BY часы DESC, номер"
    ).fetchall()
    return [dict(с_) for с_ in строки]


def куплены(с: sqlite3.Connection) -> set[int]:
    return {с_[0] for с_ in с.execute("SELECT номер FROM library").fetchall()}


def список_steam(с: sqlite3.Connection) -> set[int]:
    return {с_[0] for с_ in с.execute("SELECT номер FROM steam_wishlist").fetchall()}


def отметка(с: sqlite3.Connection, ключ: str) -> str | None:
    строка = с.execute("SELECT значение FROM marks WHERE ключ = ?", (ключ,)).fetchone()
    return строка[0] if строка else None


def лента(с: sqlite3.Connection, сколько: int | None = None) -> list[dict]:
    """Строки ленты, новые первыми. `None` — все."""
    запрос = "SELECT номер, время, род, текст FROM feed ORDER BY номер DESC"
    if сколько is None:
        строки = с.execute(запрос).fetchall()
    else:
        строки = с.execute(запрос + " LIMIT ?", (сколько,)).fetchall()
    return [dict(с_) for с_ in строки]
