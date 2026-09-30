"""Второй этап поиска: кандидаты по косинусу, повторное упорядочивание, отсечение по порогу.

```
вопрос ─► [переписать] ─► кандидаты: K₁ лучших по косинусу ─► второй этап ─► отсечь по порогу ─► не больше K₂
```

Режимы второго этапа (`РЕЖИМЫ`):

- `base` — косинус `bge-m3`, как в дне 22; порога нет, K₁ приравнивается K₂;
- `threshold` — косинус, отсечение `косинус < порог`;
- `heuristic` — слияние по местам (RRF) выдач косинуса и BM25 каждого запроса на кандидатах;
  своего калиброванного счёта у слияния нет, поэтому отсекает оно по косинусу, как `threshold`;
- `model` — вероятность перекрёстного кодировщика `BAAI/bge-reranker-v2-m3` для пары
  (исходный вопрос, текст куска); отсечение `счёт < порог`.

С переписанным запросом кандидаты — объединение K₁ лучших по исходному и по переписанному, без
повторов: переписанный запрос не может потерять то, что находил исходный. Косинус куска —
наибольший по запросам.

На диск модуль не пишет; в сеть ходит только через `считать` (Ollama) и кодировщик (первая
загрузка весов с Hugging Face в `$HF_HOME`).
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import numpy as np
import snowballstemmer

import embedder as эм
import store
from chunking import Кусок

РЕЖИМЫ = ("base", "threshold", "heuristic", "model")
КОДИРОВЩИК = "BAAI/bge-reranker-v2-m3"
# Постоянная RRF из статьи Cormack, Clarke, Büttcher 2009; то же умолчание у Elasticsearch и
# OpenSearch.
RRF_K = 60
# Умолчания BM25 у Lucene и Elasticsearch.
BM25_K1 = 1.2
BM25_B = 0.75
# `[\w-]+`: `top_k` и `SEP-2577` остаются одним словом.
СЛОВО = re.compile(r"[\w-]+")

Оценщик = Callable[[str, list[str]], list[float]]


@dataclass(frozen=True)
class Найденный:
    кусок: Кусок
    счёт: float  # счёт режима: косинус | RRF | вероятность кодировщика
    косинус: float  # наибольший косинус по всем запросам


@dataclass(frozen=True)
class Итог:
    куски: list[Найденный]  # отданные, по убыванию счёта
    кандидатов: int  # вошло во второй этап («до»)
    прошло: int  # прошло порог


class Слова:
    """Слова текста, приведённые к основе: русский стеммер, затем английский."""

    def __init__(self) -> None:
        self._ru = snowballstemmer.stemmer("russian")
        self._en = snowballstemmer.stemmer("english")
        self._основы: dict[str, str] = {}

    def __call__(self, текст: str) -> list[str]:
        итог = []
        for слово in СЛОВО.findall(текст.lower()):
            основа = self._основы.get(слово)
            if основа is None:
                основа = self._основы[слово] = self._en.stemWord(self._ru.stemWord(слово))
            итог.append(основа)
        return итог


class BM25:
    """Лексический счёт Okapi BM25 по формуле Lucene; статистика — по всем кускам стратегии."""

    def __init__(self, тексты: list[str], слова: Слова) -> None:
        self._слова = слова
        self._частоты = [Counter(слова(т)) for т in тексты]
        self._длины = np.array([sum(ч.values()) for ч in self._частоты], dtype=np.float64)
        self._средняя = float(self._длины.mean()) if len(тексты) else 0.0
        документов = Counter(слово for ч in self._частоты for слово in ч)
        n = len(тексты)
        self._idf = {с: math.log(1 + (n - д + 0.5) / (д + 0.5)) for с, д in документов.items()}

    def счёт(self, запрос: str, номера: list[int]) -> list[float]:
        слова = set(self._слова(запрос)) & self._idf.keys()
        итог = []
        for н in номера:
            частоты = self._частоты[н]
            норма = BM25_K1 * (1 - BM25_B + BM25_B * self._длины[н] / self._средняя)
            итог.append(sum(self._idf[с] * частоты[с] * (BM25_K1 + 1) / (частоты[с] + норма)
                            for с in слова if с in частоты))
        return итог


def _места(счёт: list[float], номера: list[int]) -> dict[int, int]:
    """Место каждого номера (с 1) по убыванию счёта; при равенстве — порядок `номера`."""
    порядок = sorted(range(len(номера)), key=lambda и: -счёт[и])
    return {номера[и]: место for место, и in enumerate(порядок, 1)}


def кодировщик() -> Оценщик:
    """Перекрёстный кодировщик на `mps`, если доступно, иначе на процессоре."""
    import torch
    from sentence_transformers import CrossEncoder

    устройство = "mps" if torch.backends.mps.is_available() else "cpu"
    модель = CrossEncoder(КОДИРОВЩИК, device=устройство)
    сигмоида = torch.nn.Sigmoid()

    def оценить(вопрос: str, тексты: list[str]) -> list[float]:
        # По одной паре: пакет добивается до длины самого длинного куска (до ~5 тыс. токенов), и
        # на видеоядре M3 Pro замер 2026-09-30 дал 20 кандидатов за 2,6 с при пакете 1 против
        # 6,8 с при умолчательных 32 и 3,7 с при пакете 8 с сортировкой по длине.
        return [float(с) for с in модель.predict([(вопрос, т) for т in тексты], batch_size=1,
                                                  activation_fn=сигмоида)]

    return оценить


class Поиск:
    def __init__(self, индекс: Path, strategy: str = "structure", считать=эм.эмбеддинги,
                 оценщик: Оценщик | None = None) -> None:
        self._куски, self._матрица = store.куски(индекс, strategy)
        self._считать = считать
        self._оценщик = оценщик
        self._bm25 = BM25([к.text for к in self._куски], Слова())

    def _оценить(self, вопрос: str, тексты: list[str]) -> list[float]:
        if self._оценщик is None:
            self._оценщик = кодировщик()
        return self._оценщик(вопрос, тексты)

    def упорядочить(self, вопрос: str, режим: str, k1: int, переписанный: str | None = None) -> list[Найденный]:
        """Все кандидаты по убыванию счёта режима, без отсечения."""
        if режим not in РЕЖИМЫ:
            raise ValueError(f"неизвестный режим {режим!r}: нужен один из {', '.join(РЕЖИМЫ)}")
        if k1 < 1:
            raise ValueError(f"K₁ должно быть не меньше 1, получено {k1}")
        запросы = [вопрос] if переписанный is None else [вопрос, переписанный]
        # По столбцу на запрос тем же умножением матрицы на вектор, что у `store.найти`:
        # косинус режима base совпадает с днём 22 до последнего бита.
        векторы = store.нормировать(self._считать(запросы))
        столбцы = [self._матрица @ в for в in векторы]
        номера: list[int] = []
        for столбец in столбцы:
            for н in np.argsort(-столбец, kind="stable")[:k1]:
                if int(н) not in номера:
                    номера.append(int(н))
        косинусы = [max(float(с[н]) for с in столбцы) for н in номера]

        if режим in ("base", "threshold"):
            счёт = косинусы
        elif режим == "heuristic":
            счёт = [0.0] * len(номера)
            for запрос, столбец in zip(запросы, столбцы):
                лексический = self._bm25.счёт(запрос, номера)
                # В выдаче BM25 только куски, где есть хоть одно слово запроса — как у
                # лексического поиска Elasticsearch; остальные слагаемого не получают.
                совпавшие = [н for н, с in zip(номера, лексический) if с > 0]
                места_bm25 = _места([с for с in лексический if с > 0], совпавшие)
                места_косинуса = _места([float(столбец[н]) for н in номера], номера)
                for и, н in enumerate(номера):
                    счёт[и] += 1 / (RRF_K + места_косинуса[н])
                    if н in места_bm25:
                        счёт[и] += 1 / (RRF_K + места_bm25[н])
        else:
            счёт = self._оценить(вопрос, [self._куски[н].text for н in номера])

        порядок = sorted(range(len(номера)), key=lambda и: -счёт[и])
        return [Найденный(self._куски[номера[и]], float(счёт[и]), косинусы[и]) for и in порядок]

    def отобрать(self, вопрос: str, режим: str, k1: int, k2: int, порог: float | None,
                 переписанный: str | None = None) -> Итог:
        if k2 < 1:
            raise ValueError(f"K₂ должно быть не меньше 1, получено {k2}")
        if режим == "base":
            if порог is not None:
                raise ValueError("режим base порога не принимает")
            k1 = k2
        elif порог is None:
            raise ValueError(f"режиму {режим} нужен порог")
        найденные = self.упорядочить(вопрос, режим, k1, переписанный)
        прошедшие = найденные if порог is None else [н for н in найденные if not отсечён(н, режим, порог)]
        return Итог(прошедшие[:k2], len(найденные), len(прошедшие))


def отсечён(найденный: Найденный, режим: str, порог: float) -> bool:
    """Отсекает ли порог кусок: `threshold`/`heuristic` — по косинусу, `model` — по счёту."""
    значение = найденный.счёт if режим == "model" else найденный.косинус
    return значение < порог
