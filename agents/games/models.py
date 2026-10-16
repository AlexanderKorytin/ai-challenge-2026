"""Местные модели агента: выбор при установке, загрузка из веба, вариант для разговора.

Мастер первого запуска показывает сборки `qwen3.5` с размером загрузки и пометкой, подходят ли
они памяти этого Мака (решение пользователя 2026-10-16). Выбор лежит отметкой хранилища.

Отбору хватает окна службы по умолчанию: запрос короткий. Разговору с инструментами — нет: для
него агент собирает вариант выбранной модели с окном 32 768 (`/api/create`), как файл модели дня
26. Вариант нужен, только когда облачного ключа нет и советник идёт на местной модели.
"""

from __future__ import annotations

import json
import os
import subprocess
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx2

import games_db


@dataclass(frozen=True)
class Сборка:
    имя: str
    гб: float           # загрузка, замер каталога Ollama 2026-10-16
    память: int         # память Мака, ГБ, с которой сборка работает целиком на видеоядре
    заметка: str


# Пока одна сборка — единственная замеренная в проекте (решение пользователя 2026-10-16).
# 9b целиком на видеоядре занимает 7,3 ГБ (CLAUDE.md, «Стек») — отсюда «от 16 ГБ». Другие
# сборки войдут в выбор после замера на сценариях опроса и отбора (отложенная задача 20):
# 2b в опросе не зовёт инструменты, 4b и 27b не замерялись.
ВЫБОР = (
    Сборка("qwen3.5:9b-q4_K_M", 6.6, 16, "замерена в проекте"),
)
ПО_УМОЛЧАНИЮ = ВЫБОР[0].имя
ВЕКТОРЫ = "bge-m3"
ВАРИАНТ = "games-agent-chat"
ОКНО_РАЗГОВОРА = 32768
ОТМЕТКА = "местная_модель"


def адрес() -> str:
    хост = os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    return хост if "://" in хост else f"http://{хост}"


def память_мака() -> int:
    """Память Мака в ГБ (`sysctl hw.memsize`); не узнали — 0, и пометки «подходит» не будет."""
    try:
        байты = int(subprocess.run(["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5).stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return 0
    return round(байты / 2**30)


def выбрана(с) -> str:
    return games_db.отметка(с, ОТМЕТКА) or ПО_УМОЛЧАНИЮ


def выбрать(с, имя: str) -> None:
    if имя not in {с_.имя for с_ in ВЫБОР}:
        raise ValueError(f"сборки {имя} нет в списке")
    games_db.поставить_отметку(с, ОТМЕТКА, имя)


async def есть(к: httpx2.AsyncClient) -> set[str] | None:
    """Модели службы; служба не отвечает — `None`. `bge-m3` в списке — `bge-m3:latest`."""
    try:
        ответ = await к.get(f"{адрес()}/api/tags")
        имена = {м.get("name", "") for м in ответ.json().get("models", [])}
    except (httpx2.HTTPError, ValueError):
        return None
    return имена | {и.removesuffix(":latest") for и in имена}


async def скачать(к: httpx2.AsyncClient, имя: str) -> AsyncIterator[dict]:
    """Загрузка модели службой: поток хода `{status, total, completed}`. Ошибка службы — строка
    `{"error": …}` в потоке, а не исключение: её показывает страница."""
    async with к.stream("POST", f"{адрес()}/api/pull", json={"model": имя, "stream": True}, timeout=None) as ответ:
        async for строка in ответ.aiter_lines():
            if строка.strip():
                try:
                    yield json.loads(строка)
                except ValueError:
                    yield {"error": f"служба ответила не JSON: {строка[:200]}"}


async def вариант_для_разговора(к: httpx2.AsyncClient, имя: str) -> None:
    """Вариант выбранной модели с окном 32 768 под именем `games-agent-chat`; повтор пересоздаёт
    его — так вариант следует за сменой выбора."""
    ответ = await к.post(
        f"{адрес()}/api/create",
        json={"model": ВАРИАНТ, "from": имя, "parameters": {"num_ctx": ОКНО_РАЗГОВОРА}, "stream": False},
        timeout=None,
    )
    if ответ.status_code != 200:
        raise RuntimeError(f"служба Ollama не собрала вариант модели: {ответ.text[:300]}")
