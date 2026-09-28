"""Эмбеддинги `bge-m3` через местную службу Ollama (`POST /api/embed`).

Адрес — `$OLLAMA_HOST` (как у самой Ollama), по умолчанию `127.0.0.1:11434`.

Параметры просьбы выставлены явно, потому что умолчания Ollama портят индекс молча или
отвергают длинный вход:
- `truncate: false` — по умолчанию Ollama обрезает длинный вход, и кусок проиндексировался бы
  одним своим началом;
- `num_ctx: 8192` — окно `bge-m3`;
- `num_batch: 8192` — живой замер 2026-09-28 (Ollama 0.34.4): при умолчательных 2048 вход
  длиннее ~2000 токенов отвергается «input length exceeds the context length» даже с
  `num_ctx: 8192`.
"""

from __future__ import annotations

import os

import httpx2
import numpy as np

МОДЕЛЬ = "bge-m3"
ОКНО = 8192


class ОшибкаЭмбеддера(RuntimeError):
    pass


class ПереполнениеОкна(ОшибкаЭмбеддера):
    """Хотя бы один текст просьбы длиннее окна модели."""


def адрес() -> str:
    хост = os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    return хост if "://" in хост else f"http://{хост}"


def эмбеддинги(тексты: list[str], *, модель: str = МОДЕЛЬ, клиент: httpx2.Client | None = None) -> np.ndarray:
    """Матрица (len(тексты), dim) float32 в порядке текстов."""
    тело = {"model": модель, "input": тексты, "truncate": False,
            "options": {"num_ctx": ОКНО, "num_batch": ОКНО}}
    # Предела ожидания нет: служба местная, первая просьба ждёт загрузки модели в память,
    # а просьба на целый файл длится столько, сколько в нём кусков. Недоступная служба
    # отвечает отказом соединения сразу.
    свой = клиент is None
    клиент = клиент or httpx2.Client(timeout=None)
    try:
        try:
            ответ = клиент.post(f"{адрес()}/api/embed", json=тело)
        except httpx2.TransportError as e:
            raise ОшибкаЭмбеддера(f"Ollama недоступна по {адрес()}: {e}") from e
    finally:
        if свой:
            клиент.close()
    if ответ.status_code != 200:
        текст = ответ.text
        класс = ПереполнениеОкна if "context length" in текст else ОшибкаЭмбеддера
        raise класс(f"Ollama {ответ.status_code}: {текст}")
    векторы = ответ.json()["embeddings"]
    if len(векторы) != len(тексты):
        raise ОшибкаЭмбеддера(f"Ollama вернула {len(векторы)} векторов на {len(тексты)} текстов")
    return np.asarray(векторы, dtype=np.float32)
