"""Индекс в одном файле SQLite: куски с метаданными и векторами, сведения о каждой сборке.

Схема читается поиском следующих дней — менять её только вместе с читателями.
Вектор хранится `float32`, нормированным до длины 1, поэтому косинус — скалярное
произведение. Поиск — полным перебором через `numpy`: на тысячах кусков это миллисекунды.
Эмбеддингов модуль не считает, сети не знает.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import numpy as np

from chunking import Кусок

СХЕМА = """
CREATE TABLE IF NOT EXISTS сборки (strategy TEXT PRIMARY KEY, model TEXT, dim INTEGER,
  commit_hash TEXT, dirty INTEGER, built_at TEXT, seconds REAL, chunks INTEGER, fixed_size INTEGER);
CREATE TABLE IF NOT EXISTS куски (chunk_id TEXT PRIMARY KEY, strategy TEXT, source TEXT,
  title TEXT, section TEXT, start INTEGER, "end" INTEGER, text TEXT, vector BLOB);
CREATE INDEX IF NOT EXISTS куски_стратегии ON куски(strategy);
"""
ПОЛЯ_СБОРКИ = ("model", "dim", "commit_hash", "dirty", "built_at", "seconds", "chunks", "fixed_size")


def _открыть(путь: Path) -> sqlite3.Connection:
    путь.parent.mkdir(parents=True, exist_ok=True)
    соединение = sqlite3.connect(путь)
    соединение.executescript(СХЕМА)
    return соединение


def нормировать(векторы: np.ndarray) -> np.ndarray:
    векторы = np.asarray(векторы, dtype=np.float32)
    длины = np.linalg.norm(векторы, axis=-1, keepdims=True)
    return векторы / np.where(длины == 0, 1, длины)


def записать(путь: Path, strategy: str, куски: list[Кусок], векторы: np.ndarray, сведения: dict) -> None:
    """Заменяет сборку стратегии целиком одной транзакцией; чужие стратегии не трогает."""
    if len(куски) != len(векторы):
        raise ValueError(f"{len(куски)} кусков на {len(векторы)} векторов")
    if any(к.strategy != strategy for к in куски):
        raise ValueError(f"в сборке {strategy} есть куски чужой стратегии")
    векторы = нормировать(векторы)
    соединение = _открыть(путь)
    try:
        with соединение:
            соединение.execute("DELETE FROM куски WHERE strategy = ?", (strategy,))
            соединение.executemany(
                'INSERT INTO куски VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)',
                [(к.chunk_id, к.strategy, к.source, к.title, к.section, к.start, к.end, к.text, в.tobytes())
                 for к, в in zip(куски, векторы)])
            соединение.execute(
                "INSERT OR REPLACE INTO сборки VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (strategy, *(сведения.get(поле) for поле in ПОЛЯ_СБОРКИ)))
    finally:
        соединение.close()


def стратегии(путь: Path) -> dict[str, dict]:
    """strategy -> сведения сборки."""
    соединение = _открыть(путь)
    try:
        строки = соединение.execute("SELECT * FROM сборки ORDER BY strategy").fetchall()
    finally:
        соединение.close()
    return {с[0]: dict(zip(ПОЛЯ_СБОРКИ, с[1:])) for с in строки}


def куски(путь: Path, strategy: str) -> tuple[list[Кусок], np.ndarray]:
    """Все куски стратегии по порядку (source, start) и матрица их векторов."""
    соединение = _открыть(путь)
    try:
        строки = соединение.execute(
            'SELECT chunk_id, strategy, source, title, section, start, "end", text, vector '
            "FROM куски WHERE strategy = ? ORDER BY source, start, chunk_id", (strategy,)).fetchall()
    finally:
        соединение.close()
    if not строки:
        raise LookupError(f"в индексе {путь} нет сборки «{strategy}»")
    return ([Кусок(*с[:8]) for с in строки],
            np.stack([np.frombuffer(с[8], dtype=np.float32) for с in строки]))


def найти(путь: Path, strategy: str, вектор: np.ndarray, k: int) -> list[tuple[float, Кусок]]:
    """k лучших кусков по косинусу, по убыванию."""
    список, матрица = куски(путь, strategy)
    близость = матрица @ нормировать(вектор)
    порядок = np.argsort(-близость, kind="stable")[:k]
    return [(float(близость[и]), список[и]) for и in порядок]
