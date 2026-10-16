"""RAG по истории человека: похожие игры с его отметкой — часы, плюс, минус с причиной.

Вкус в карточке — обобщение, а точнее всего говорят решения человека по похожим играм. Перед
судом над кандидатом отбор находит самые похожие игры истории и кладёт их в запрос местной
модели: «похожа на Hades — 120 ч; на Outlast — минус: скучно».

Векторы — `bge-m3` в местной Ollama через `rag/embedder.py`. Файл загружается по пути, а не
пакетом: пакет `rag` целиком тянет `sentence-transformers` и `torch`, а `embedder.py` зависит
только от `httpx2` и `numpy`, и в нём уже учтены грабли Ollama с `num_batch`.

Индекс — `$GAMES_DATA_DIR/similar.sqlite`: документ на игру, вектор нормирован. Игр истории —
сотни, поэтому поиск — перебором косинуса в памяти, отдельного индекса ближайших соседей не нужно.
"""

from __future__ import annotations

import importlib.util
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import numpy as np

import games_db
from steam_store import Игра

ВЛОЖЕНИЕ = Path(__file__).resolve().parents[2] / "rag" / "embedder.py"
# Число похожих — K₂ дня 23 (замер поиска проекта), своё здесь не подбирается.
ПОХОЖИХ = 5

Считать = Callable[[list[str]], np.ndarray]


def _эмбеддер():
    спец = importlib.util.spec_from_file_location("games_embedder", ВЛОЖЕНИЕ)
    if спец is None or спец.loader is None:
        raise RuntimeError(f"нет модуля векторов {ВЛОЖЕНИЕ}")
    модуль = importlib.util.module_from_spec(спец)
    спец.loader.exec_module(модуль)
    return модуль


def векторы_bge(тексты: list[str]) -> np.ndarray:
    return _эмбеддер().эмбеддинги(тексты)


@dataclass(frozen=True)
class Документ:
    номер: int
    имя: str
    текст: str
    # Отметка человека: «120 ч», «плюс», «минус: скучно», «уже играл: понравилась», «в списке
    # желаемого Steam». Её и видит модель — сам текст нужен только для вектора.
    отметка: str


def текст_игры(игра: Игра) -> str:
    """Текст вектора: имя, жанры, описание. Цена и отзывы — не про сходство."""
    жанры = ", ".join(игра.жанры)
    return f"{игра.имя}. {жанры}. {игра.описание}".strip()


def путь() -> Path:
    return games_db.путь().parent / "similar.sqlite"


class Индекс:
    def __init__(self, считать: Считать = векторы_bge, файл: Path | None = None) -> None:
        self._считать = считать
        self._файл = файл or путь()
        self._файл.parent.mkdir(parents=True, exist_ok=True)
        with self._соединение() as с:
            с.execute(
                "CREATE TABLE IF NOT EXISTS docs (номер INTEGER PRIMARY KEY, имя TEXT, "
                "текст TEXT, отметка TEXT, вектор BLOB)"
            )

    def _соединение(self) -> sqlite3.Connection:
        с = sqlite3.connect(self._файл, timeout=5)
        с.execute("PRAGMA busy_timeout = 5000")
        return с

    def _вектора(self, документы: list[Документ]) -> np.ndarray:
        матрица = np.asarray(self._считать([д.текст for д in документы]), dtype=np.float32)
        нормы = np.linalg.norm(матрица, axis=1, keepdims=True)
        return матрица / np.where(нормы == 0, 1, нормы)

    def пересобрать(self, документы: list[Документ]) -> None:
        """Полная пересборка — раз в неделю, в задании недели. Векторы считаются ДО замены:
        служба Ollama упала — прежний индекс остаётся целым."""
        вектора = self._вектора(документы) if документы else np.zeros((0, 0), np.float32)
        with self._соединение() as с:
            с.execute("DELETE FROM docs")
            с.executemany(
                "INSERT INTO docs VALUES (?, ?, ?, ?, ?)",
                [(д.номер, д.имя, д.текст, д.отметка, в.tobytes()) for д, в in zip(документы, вектора)],
            )

    def дописать(self, документ: Документ) -> None:
        """Новая отметка человека — сразу в индекс: иначе минус неделю не влиял бы на отбор."""
        вектор = self._вектора([документ])[0]
        with self._соединение() as с:
            с.execute(
                "INSERT INTO docs VALUES (?, ?, ?, ?, ?) ON CONFLICT (номер) DO UPDATE SET "
                "имя = excluded.имя, текст = excluded.текст, отметка = excluded.отметка, "
                "вектор = excluded.вектор",
                (документ.номер, документ.имя, документ.текст, документ.отметка, вектор.tobytes()),
            )

    def размер(self) -> int:
        with self._соединение() as с:
            return с.execute("SELECT count(*) FROM docs").fetchone()[0]

    def похожие(self, игра: Игра, k: int = ПОХОЖИХ) -> list[tuple[float, Документ]]:
        """Самые похожие игры истории, кроме самой игры. Индекс пуст — пустой список."""
        with self._соединение() as с:
            строки = с.execute(
                "SELECT номер, имя, текст, отметка, вектор FROM docs WHERE номер != ?", (игра.номер,)
            ).fetchall()
        if not строки:
            return []
        матрица = np.stack([np.frombuffer(с[4], dtype=np.float32) for с in строки])
        запрос = self._вектора([Документ(игра.номер, игра.имя, текст_игры(игра), "")])[0]
        близости = матрица @ запрос
        порядок = np.argsort(-близости)[:k]
        return [
            (float(близости[i]), Документ(строки[i][0], строки[i][1], строки[i][2], строки[i][3]))
            for i in порядок
        ]


def блок(похожие: list[tuple[float, Документ]]) -> str:
    """Строки для запроса отбора. Пусто — пустая строка: раздел не нужен."""
    if not похожие:
        return ""
    строки = ["## Похожие игры из истории человека"]
    строки += [f"- {д.имя} — {д.отметка} (сходство {б:.2f})" for б, д in похожие]
    return "\n".join(строки)
