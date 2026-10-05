"""Поставщики моделей: кто отвечает на запрос, называет само имя модели.

Имя с приставкой `ollama/` уходит местной службе Ollama, имя без приставки — в DeepSeek.
Отдельного поля «поставщик» нет намеренно: поле `model` уже есть у настроек, профиля, агента
группы и наряда, и второе поле рядом с ним неизбежно разошлось бы с первым.

Модуль в сеть не ходит и состояния не держит: его читают и клиент (`api`), и счёт токенов
(`tokens`), а им нельзя зависеть друг от друга.
"""

from __future__ import annotations

import os

OLLAMA_PREFIX = "ollama/"
DEFAULT_OLLAMA_PORT = 11434
DEFAULT_OLLAMA_ROOT = f"http://127.0.0.1:{DEFAULT_OLLAMA_PORT}"


def is_local(model: str) -> bool:
    return model.startswith(OLLAMA_PREFIX)


def local_name(model: str) -> str:
    """Имя модели, как его знает Ollama: без приставки."""
    return model.removeprefix(OLLAMA_PREFIX)


def ollama_root() -> str:
    """Адрес службы без `/v1`. `OLLAMA_HOST` — переменная самой Ollama, своей не заводим.

    Ollama принимает в ней и `127.0.0.1:11434` без схемы — такому значению дописываем `http://`.
    Порт не назван — берём 11434, как сама Ollama: без этого адрес `myhost` ушёл бы на порт 80."""
    host = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
    if not host:
        return DEFAULT_OLLAMA_ROOT
    root = host if "://" in host else f"http://{host}"
    схема, _, остаток = root.partition("://")
    # У адреса IPv6 двоеточия внутри скобок портом не являются.
    if ":" not in остаток.rpartition("]")[2]:
        root = f"{схема}://{остаток}:{DEFAULT_OLLAMA_PORT}"
    return root
