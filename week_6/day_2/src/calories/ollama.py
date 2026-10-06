"""Один запрос к службе Ollama на этой же машине. На другую машину модуль запрос не шлёт."""

import ipaddress
import json
import os

import httpx

АДРЕС_ПО_УМОЛЧАНИЮ = "http://127.0.0.1:11434"
МОДЕЛЬ_ПО_УМОЛЧАНИЮ = "qwen3.5:9b-q4_K_M-32k"


class СлужбаНедоступна(Exception):
    """Соединения со службой нет."""


class ЧужойАдрес(Exception):
    """Адрес службы указывает на другую машину; запрос не отправлен."""


class МоделиНет(Exception):
    """Служба не знает названной модели."""


class ОтветНегоден(Exception):
    """Ответ службы или модели нельзя принять."""


def адрес() -> str:
    """Адрес службы. Как у клиента Ollama: без схемы — `http`, без порта — 11434."""
    значение = os.environ.get("OLLAMA_HOST", "").strip().rstrip("/")
    if not значение:
        return АДРЕС_ПО_УМОЛЧАНИЮ
    if "://" in значение:
        return значение
    try:
        разобран = httpx.URL("http://" + значение)
    except httpx.InvalidURL as ошибка:
        raise СлужбаНедоступна(f"OLLAMA_HOST={значение}: адрес не разбирается ({ошибка})") from ошибка
    if разобран.port is None:
        разобран = разобран.copy_with(port=11434)
    return str(разобран).rstrip("/")


def местный() -> bool:
    """Правда, если адрес службы указывает на эту же машину."""
    try:
        узел = httpx.URL(адрес()).host
    except httpx.InvalidURL:
        return False
    if узел == "localhost":
        return True
    try:
        return ipaddress.ip_address(узел).is_loopback
    except ValueError:
        return False


def модель() -> str:
    return os.environ.get("CALORIES_MODEL", "").strip() or МОДЕЛЬ_ПО_УМОЛЧАНИЮ


def спросить(система: str, вопрос: str, схема: dict, *, клиент: httpx.Client | None = None) -> dict:
    """Шлёт вопрос модели и возвращает её ответ словарём по схеме."""
    if not местный():
        raise ЧужойАдрес(адрес())
    запрос = {
        "model": модель(),
        "stream": False,
        "think": False,
        "format": схема,
        "options": {"temperature": 0},
        "messages": [
            {"role": "system", "content": система},
            {"role": "user", "content": вопрос},
        ],
    }
    свой = клиент is None
    if свой:
        # Предела ожидания нет намеренно: первый ответ ждёт загрузки модели в память.
        # trust_env=False: посредник из окружения (HTTP_PROXY) увёл бы запрос с этой машины.
        клиент = httpx.Client(timeout=httpx.Timeout(None), trust_env=False)
    try:
        ответ = клиент.post(адрес() + "/api/chat", json=запрос)
    except (httpx.TransportError, httpx.InvalidURL) as ошибка:
        raise СлужбаНедоступна(f"{type(ошибка).__name__}: {ошибка}") from ошибка
    finally:
        if свой:
            клиент.close()
    if ответ.status_code == 404:
        raise МоделиНет(ответ.text.strip())
    if ответ.status_code != 200:
        raise ОтветНегоден(f"служба ответила кодом {ответ.status_code}: {ответ.text.strip()}")
    try:
        разобран = json.loads(ответ.json()["message"]["content"])
    except (ValueError, KeyError, TypeError) as ошибка:
        raise ОтветНегоден(f"ответ модели не разбирается как JSON: {ошибка}") from ошибка
    if not isinstance(разобран, dict):
        raise ОтветНегоден("ответ модели не объект JSON")
    return разобран
