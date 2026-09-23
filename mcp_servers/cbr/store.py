"""Хранилище сервера `cbr`: задания сбора, их запуски и собранные курсы в одном файле SQLite.

SQLite, а не JSON: сводка — отбор по датам с минимумом и максимумом, а запись одного запуска не
переписывает весь файл. Путь задаёт тот, кто создаёт хранилище (служба — `CBR_MCP_DB`, проверки
— временный каталог).

Курс хранится один раз на пару «валюта + дата установления»: повторный сбор в тот же день новых
строк не даёт, поэтому лишний запуск задания ничего не портит. Время — строкой ISO с поясом.

Вызовы синхронные и идут прямо из цикла `asyncio`: каждый — доли миллисекунды на локальном
файле, а процесс службы один.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
from dataclasses import dataclass
from pathlib import Path

СХЕМА = """
CREATE TABLE IF NOT EXISTS jobs (
    -- AUTOINCREMENT: номер снятого задания не достаётся новому, иначе новое унаследовало бы
    -- журнал запусков снятого (журнал при снятии остаётся).
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    currencies TEXT NOT NULL,
    cron TEXT NOT NULL,
    created_at TEXT NOT NULL,
    next_run TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS runs (
    job_id INTEGER NOT NULL,
    at TEXT NOT NULL,
    error TEXT,
    new INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS rates (
    code TEXT NOT NULL,
    rate_date TEXT NOT NULL,
    name TEXT NOT NULL,
    nominal INTEGER NOT NULL,
    value REAL NOT NULL,
    unit_rate REAL NOT NULL,
    seen_at TEXT NOT NULL,
    job_id INTEGER NOT NULL,
    PRIMARY KEY (code, rate_date)
);
"""


@dataclass(frozen=True)
class Job:
    id: int
    currencies: list[str]
    cron: str
    created_at: dt.datetime
    next_run: dt.datetime
    runs_ok: int
    runs_failed: int
    last_run_at: dt.datetime | None
    last_error: str | None


@dataclass(frozen=True)
class Observed:
    code: str
    name: str
    rate_date: dt.date
    nominal: int
    value: float
    unit_rate: float


@dataclass(frozen=True)
class Summary:
    code: str
    name: str
    count: int
    first: Observed
    last: Observed
    change: float
    change_pct: float
    min: Observed
    max: Observed
    mean: float
    last_seen_at: dt.datetime


def _время(текст: str) -> dt.datetime:
    return dt.datetime.fromisoformat(текст)


class Store:
    def __init__(self, путь: Path) -> None:
        путь.parent.mkdir(parents=True, exist_ok=True)
        self._бд = sqlite3.connect(путь)
        self._бд.executescript(СХЕМА)

    def close(self) -> None:
        self._бд.close()

    def add_job(
        self, currencies: list[str], cron: str, next_run: dt.datetime, now: dt.datetime
    ) -> Job:
        with self._бд:
            курсор = self._бд.execute(
                "INSERT INTO jobs (currencies, cron, created_at, next_run) VALUES (?, ?, ?, ?)",
                (",".join(currencies), cron, now.isoformat(), next_run.isoformat()),
            )
        return next(з for з in self.jobs() if з.id == курсор.lastrowid)

    def jobs(self) -> list[Job]:
        строки = self._бд.execute(
            """
            SELECT j.id, j.currencies, j.cron, j.created_at, j.next_run,
                   COUNT(r.job_id) FILTER (WHERE r.error IS NULL),
                   COUNT(r.job_id) FILTER (WHERE r.error IS NOT NULL),
                   MAX(r.at),
                   (SELECT error FROM runs WHERE job_id = j.id ORDER BY at DESC, rowid DESC LIMIT 1)
            FROM jobs j LEFT JOIN runs r ON r.job_id = j.id
            GROUP BY j.id ORDER BY j.id
            """
        ).fetchall()
        return [
            Job(
                id=с[0],
                currencies=с[1].split(","),
                cron=с[2],
                created_at=_время(с[3]),
                next_run=_время(с[4]),
                runs_ok=с[5],
                runs_failed=с[6],
                last_run_at=_время(с[7]) if с[7] else None,
                last_error=с[8],
            )
            for с in строки
        ]

    def delete_job(self, job_id: int) -> bool:
        # Курсы и журнал запусков остаются: курс — факт, а не принадлежность задания.
        with self._бд:
            return self._бд.execute("DELETE FROM jobs WHERE id = ?", (job_id,)).rowcount > 0

    def set_next_run(self, job_id: int, next_run: dt.datetime) -> None:
        with self._бд:
            self._бд.execute(
                "UPDATE jobs SET next_run = ? WHERE id = ?", (next_run.isoformat(), job_id)
            )

    def record_run(self, job_id: int, at: dt.datetime, error: str | None, new: int) -> None:
        with self._бд:
            self._бд.execute(
                "INSERT INTO runs (job_id, at, error, new) VALUES (?, ?, ?, ?)",
                (job_id, at.isoformat(), error, new),
            )

    def add_rates(self, rates: list[Observed], seen_at: dt.datetime, job_id: int) -> int:
        with self._бд:
            return sum(
                self._бд.execute(
                    "INSERT OR IGNORE INTO rates VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        к.code,
                        к.rate_date.isoformat(),
                        к.name,
                        к.nominal,
                        к.value,
                        к.unit_rate,
                        seen_at.isoformat(),
                        job_id,
                    ),
                ).rowcount
                for к in rates
            )

    def summary(
        self, code: str, date_from: dt.date | None, date_to: dt.date | None
    ) -> Summary | None:
        строки = self._бд.execute(
            """
            SELECT code, name, rate_date, nominal, value, unit_rate, seen_at FROM rates
            WHERE code = ? AND rate_date >= ? AND rate_date <= ?
            ORDER BY rate_date
            """,
            (
                code,
                date_from.isoformat() if date_from else "",
                date_to.isoformat() if date_to else "9999-12-31",
            ),
        ).fetchall()
        if not строки:
            return None
        курсы = [
            Observed(
                code=с[0],
                name=с[1],
                rate_date=dt.date.fromisoformat(с[2]),
                nominal=с[3],
                value=с[4],
                unit_rate=с[5],
            )
            for с in строки
        ]
        # Сравнение по курсу за единицу: номинал у валюты ЦБ может сменить. Курсы упорядочены по
        # дате, а `min`/`max` берут первый из равных — при равенстве выходит более ранняя дата.
        первый, последний = курсы[0], курсы[-1]
        изменение = последний.unit_rate - первый.unit_rate
        return Summary(
            code=первый.code,
            name=последний.name,
            count=len(курсы),
            first=первый,
            last=последний,
            change=изменение,
            change_pct=изменение / первый.unit_rate * 100,
            min=min(курсы, key=lambda к: к.unit_rate),
            max=max(курсы, key=lambda к: к.unit_rate),
            mean=sum(к.unit_rate for к in курсы) / len(курсы),
            last_seen_at=max(_время(с[6]) for с in строки),
        )
