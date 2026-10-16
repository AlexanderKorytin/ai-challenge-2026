"""IsThereAnyDeal: разбор образцов документации и ход запросов на `httpx2.MockTransport`."""

import asyncio
import json
from pathlib import Path

import httpx2
import pytest

import itad

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"
КЛЮЧ = "0123456789abcdef0123456789abcdef01234567"


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))["ответ"]


def минимум(обработчик, ключ=КЛЮЧ, номер=1086940):
    async def ход():
        async with httpx2.AsyncClient(transport=httpx2.MockTransport(обработчик)) as к:
            return await itad.минимум(к, ключ, номер)
    return asyncio.run(ход())


def test_разбор_поиска():
    assert itad.разобрать_поиск(образец("itad_lookup_found.json")) == "018d937f-07fc-72ed-8517-d8e24cb1eb22"
    assert itad.разобрать_поиск(образец("itad_lookup_notfound.json")) is None


def test_разбор_минимума_только_steam():
    данные = образец("itad_storelow.json")
    # У DLGamer минимум 660, у Steam 659: берётся Steam, а не меньшая цена вообще.
    assert itad.разобрать_минимум(данные, "01849782-1017-7389-8de4-c97c587fd7e3") == itad.Минимум(
        659, "USD", "2022-12-21T00:29:04+01:00")
    # У второй игры минимум DLGamer 1000, а Steam ни разу не снижал цену: 3999.
    assert itad.разобрать_минимум(данные, "01849783-6a26-7147-ab32-71804ca47e8e").цена == 3999
    assert itad.разобрать_минимум(данные, "нет-такого") is None
    assert itad.разобрать_минимум([{"id": "x", "lows": []}], "x") is None
    # Ответ без Steam — минимума Steam нет, чужой магазин не подставляется.
    только_чужой = [{"id": "x", "lows": [данные[0]["lows"][0]]}]
    assert itad.разобрать_минимум(только_чужой, "x") is None
    with pytest.raises(itad.ОшибкаITAD):
        itad.разобрать_минимум({"low": 1}, "x")


def test_нет_ключа_без_запроса():
    запросы = []
    assert минимум(lambda з: запросы.append(з), ключ="") is None
    assert запросы == []


def test_ход_запросов():
    запросы = []
    найдено = образец("itad_lookup_found.json")
    низы = образец("itad_storelow.json")
    найдено["game"]["id"] = низы[0]["id"]

    def сервер(запрос):
        запросы.append(запрос)
        if запрос.url.path == "/games/lookup/v1":
            return httpx2.Response(200, json=найдено)
        return httpx2.Response(200, json=низы)

    assert минимум(сервер) == itad.Минимум(659, "USD", "2022-12-21T00:29:04+01:00")
    поиск, низ = запросы
    assert поиск.method == "GET" and поиск.url.params["appid"] == "1086940"
    assert низ.method == "POST" and низ.url.path == "/games/storelow/v2"
    assert низ.url.params["country"] == "RU" and низ.url.params["shops"] == "61"
    assert json.loads(низ.content) == [низы[0]["id"]]
    # Ключ — заголовком, не в адресе.
    for з in запросы:
        assert з.headers["ITAD-API-Key"] == КЛЮЧ
        assert КЛЮЧ not in str(з.url)


def test_игра_не_найдена():
    запросы = []

    def сервер(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("itad_lookup_notfound.json"))

    assert минимум(сервер) is None
    assert len(запросы) == 1


def test_ключ_не_принят_без_ключа_в_тексте():
    with pytest.raises(itad.КлючНеПринят) as беда:
        минимум(lambda з: httpx2.Response(403, json=образец("itad_bad_key.json")))
    assert КЛЮЧ not in str(беда.value)


def test_ключ_не_ascii():
    with pytest.raises(itad.КлючНеПринят) as беда:
        минимум(lambda з: httpx2.Response(200), ключ="ключ-кириллицей")
    assert "ключ-кириллицей" not in str(беда.value)


def test_обрыв_исключение():
    def обрыв(запрос):
        raise httpx2.ConnectError("нет связи", request=запрос)
    with pytest.raises(itad.ОшибкаITAD) as беда:
        минимум(обрыв)
    assert беда.value.__context__ is None
