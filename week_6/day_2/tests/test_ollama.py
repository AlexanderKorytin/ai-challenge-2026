import json

import httpx
import pytest

from calories import ollama
from calories.ollama import МоделиНет, ОтветНегоден, СлужбаНедоступна, ЧужойАдрес, спросить

from conftest import служба

СХЕМА = {"type": "object"}


def test_запрос_идёт_местной_службе_без_рассуждений_и_по_схеме():
    запросы = []
    ответ = спросить("система", "вопрос", СХЕМА, клиент=служба({"а": 1}, запросы=запросы))
    assert ответ == {"а": 1}
    assert str(запросы[0].url) == "http://127.0.0.1:11434/api/chat"
    тело = json.loads(запросы[0].content)
    assert тело["think"] is False and тело["stream"] is False
    assert тело["format"] == СХЕМА
    assert тело["model"] == "qwen3.5:9b-q4_K_M-32k"
    assert тело["options"] == {"temperature": 0}
    assert [(с["role"], с["content"]) for с in тело["messages"]] == [("system", "система"), ("user", "вопрос")]


def test_адрес_и_модель_меняет_окружение(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "localhost:9999")
    monkeypatch.setenv("CALORIES_MODEL", "другая")
    запросы = []
    спросить("с", "в", СХЕМА, клиент=служба({}, запросы=запросы))
    assert str(запросы[0].url) == "http://localhost:9999/api/chat"
    assert json.loads(запросы[0].content)["model"] == "другая"


def test_единственный_адрес_в_коде_местный():
    """Утилита работает без облака: в пакете нет адресов, кроме местной службы."""
    import pathlib
    import re

    адреса = set()
    for файл in pathlib.Path(ollama.__file__).parent.glob("*.py"):
        адреса |= set(re.findall(r"https?://[^\s\"']+", файл.read_text(encoding="utf-8")))
    assert адреса == {"http://127.0.0.1:11434"}


def test_нет_соединения():
    def обрыв(запрос):
        raise httpx.ConnectError("отказ")

    with pytest.raises(СлужбаНедоступна):
        спросить("с", "в", СХЕМА, клиент=httpx.Client(transport=httpx.MockTransport(обрыв)))


def test_модели_нет():
    with pytest.raises(МоделиНет):
        спросить("с", "в", СХЕМА, клиент=служба(код=404))


def test_иной_код_службы():
    with pytest.raises(ОтветНегоден, match="500"):
        спросить("с", "в", СХЕМА, клиент=служба(код=500))


@pytest.mark.parametrize("содержимое", ["не json", "[1, 2]"])
def test_ответ_не_объект(содержимое):
    with pytest.raises(ОтветНегоден):
        спросить("с", "в", СХЕМА, клиент=служба(содержимое))


@pytest.mark.parametrize("значение, итог", [
    ("", "http://127.0.0.1:11434"),
    ("127.0.0.1", "http://127.0.0.1:11434"),
    ("0.0.0.0", "http://0.0.0.0:11434"),
    ("localhost:9999", "http://localhost:9999"),
    ("https://example.org", "https://example.org"),
])
def test_адрес_как_у_клиента_ollama(monkeypatch, значение, итог):
    monkeypatch.setenv("OLLAMA_HOST", значение)
    assert ollama.адрес() == итог


@pytest.mark.parametrize("значение, свой", [
    ("", True), ("localhost:9999", True), ("127.0.0.5", True), ("[::1]:11434", True), ("0.0.0.0", True),
    ("localhost.evil.com", False), ("localhost@evil.com", False),
    ("192.168.1.5", False), ("https://example.org", False),
])
def test_местный_адрес_парой(monkeypatch, значение, свой):
    monkeypatch.setenv("OLLAMA_HOST", значение)
    assert ollama.местный() is свой


@pytest.mark.parametrize("значение", ["192.168.1.5", "https://example.org", "example.org:11434"])
def test_на_чужую_машину_запрос_не_уходит(monkeypatch, значение):
    monkeypatch.setenv("OLLAMA_HOST", значение)
    запросы = []
    with pytest.raises(ЧужойАдрес):
        спросить("с", "в", СХЕМА, клиент=служба({}, запросы=запросы))
    assert запросы == []


def test_негодный_адрес_не_трассировка(monkeypatch):
    monkeypatch.setenv("OLLAMA_HOST", "localhost:abc")
    with pytest.raises(СлужбаНедоступна):
        спросить("с", "в", СХЕМА, клиент=служба({}))


def test_свой_клиент_не_берёт_посредника_из_окружения(monkeypatch):
    """Без довода `клиент` запрос идёт прямо: посредник из окружения его не перехватывает."""
    создан = {}
    настоящий = httpx.Client

    def клиент(**kw):
        создан.update(kw)
        return настоящий(transport=httpx.MockTransport(
            lambda з: httpx.Response(200, json={"message": {"content": "{}"}})), **kw)

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example:3128")
    monkeypatch.setattr(ollama.httpx, "Client", клиент)
    assert спросить("с", "в", СХЕМА) == {}
    assert создан["trust_env"] is False
    assert создан["timeout"] == httpx.Timeout(None)
