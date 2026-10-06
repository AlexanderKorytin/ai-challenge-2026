import json

import httpx
import pytest


@pytest.fixture(autouse=True)
def _чистое_окружение(monkeypatch):
    monkeypatch.delenv("OLLAMA_HOST", raising=False)
    monkeypatch.delenv("CALORIES_MODEL", raising=False)


def служба(*ответы, запросы=None, код=200):
    """Клиент с поддельной службой: отдаёт ответы модели по очереди, запросы складывает в список."""
    очередь = list(ответы)

    def обработчик(запрос: httpx.Request) -> httpx.Response:
        if запросы is not None:
            запросы.append(запрос)
        if код != 200:
            return httpx.Response(код, text="ошибка службы")
        содержимое = очередь.pop(0)
        if not isinstance(содержимое, str):
            содержимое = json.dumps(содержимое, ensure_ascii=False)
        return httpx.Response(200, json={"message": {"role": "assistant", "content": содержимое}})

    return httpx.Client(transport=httpx.MockTransport(обработчик))


def блюдо(ккал_100=100, порция_г=200, ги=50, еда=True):
    return {"еда": еда, "ккал_100": ккал_100, "порция_г": порция_г, "ги": ги}
