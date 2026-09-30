"""Переписывание вопроса в поисковый запрос через DeepSeek.

Разговорный вопрос («что там с правами на настройки?») эмбеддинг и BM25 ловят хуже, чем
поисковый запрос с развёрнутыми сокращениями, синонимами и возможными именами. Переписывает
`deepseek-v4-flash` при `temperature` 0 без рассуждений: ответ — одна строка запроса.

Ключ — поле `api_key` файла настроек `myharness`: `$MYHARNESS_CONFIG_DIR/config.json`, иначе
`~/.config/myharness/config.json`. Файл только читается — ключ остаётся в одном месте, копии не
заводятся. Ключ не попадает ни в текст исключений, ни в вывод. На диск модуль не пишет.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx2

МОДЕЛЬ = "deepseek-v4-flash"
АДРЕС_DEEPSEEK = "https://api.deepseek.com/chat/completions"
# Те же пределы, что у клиента DeepSeek в myharness (`api.REQUEST_TIMEOUT`); пакет `rag` от
# `myharness` не зависит, поэтому числа повторены, как в `mcp_servers/pipeline`.
ОЖИДАНИЕ = httpx2.Timeout(connect=20.0, read=600.0, write=20.0, pool=20.0)
ИНСТРУКЦИЯ = (
    "Ты переписываешь вопрос пользователя в поисковый запрос к базе знаний программного проекта: "
    "требования, дизайны, планы, рабочий договор. Разверни сокращения, добавь синонимы и "
    "возможные имена терминов, параметров, команд и файлов по-русски и по-английски. Не отвечай "
    "на вопрос и ничего не выдумывай о проекте. Ответь одной строкой — только запрос, без пояснений."
)


class ОшибкаПереписывания(RuntimeError):
    pass


def путь_настроек() -> Path:
    каталог = os.environ.get("MYHARNESS_CONFIG_DIR")
    база = Path(каталог).expanduser() if каталог else Path.home() / ".config" / "myharness"
    return база / "config.json"


def _ключ() -> str:
    путь = путь_настроек()
    try:
        данные = json.loads(путь.read_text("utf-8"))
    except FileNotFoundError:
        raise ОшибкаПереписывания(f"нет ключа DeepSeek: файла {путь} нет") from None
    except (OSError, ValueError) as exc:
        raise ОшибкаПереписывания(f"файл настроек {путь} не читается: {type(exc).__name__}") from None
    ключ = данные.get("api_key") if isinstance(данные, dict) else None
    if not isinstance(ключ, str) or not ключ.strip():
        raise ОшибкаПереписывания(f"нет ключа DeepSeek: в {путь} нет поля api_key")
    return ключ.strip()


def переписать(вопрос: str, *, клиент: httpx2.Client | None = None) -> str:
    """Поисковый запрос одной строкой."""
    ключ = _ключ()
    тело = {"model": МОДЕЛЬ, "temperature": 0, "thinking": {"type": "disabled"},
            "messages": [{"role": "system", "content": ИНСТРУКЦИЯ}, {"role": "user", "content": вопрос}]}
    свой = клиент is None
    клиент = клиент or httpx2.Client(timeout=ОЖИДАНИЕ)
    try:
        try:
            ответ = клиент.post(АДРЕС_DEEPSEEK, json=тело, headers={"Authorization": f"Bearer {ключ}"})
        except httpx2.TimeoutException:
            raise ОшибкаПереписывания("DeepSeek не ответил вовремя") from None
        except httpx2.HTTPError as exc:
            raise ОшибкаПереписывания(f"нет связи с DeepSeek: {type(exc).__name__}") from None
    finally:
        if свой:
            клиент.close()
    if ответ.status_code >= 400:
        текст = " ".join(ответ.text.split()).replace(ключ, "***")
        raise ОшибкаПереписывания(f"DeepSeek ответил HTTP {ответ.status_code}: {текст}")
    try:
        запрос = " ".join((ответ.json()["choices"][0]["message"]["content"] or "").split())
    except (ValueError, KeyError, IndexError, TypeError):
        raise ОшибкаПереписывания("DeepSeek вернул ответ непонятного вида") from None
    if not запрос:
        raise ОшибкаПереписывания("DeepSeek вернул пустой запрос")
    return запрос
