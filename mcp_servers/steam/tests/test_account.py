"""Вход OpenID, библиотека и список желаемого Steam — без сети, на `httpx2.MockTransport`."""

import asyncio
import json
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx2
import pytest

import steam_account as sa

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"
ВОЗВРАТ = "http://127.0.0.1:8791/auth/steam"
КЛЮЧ = "ABCDEF0123456789ABCDEF0123456789"


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))["ответ"]


def клиент(обработчик) -> httpx2.AsyncClient:
    return httpx2.AsyncClient(transport=httpx2.MockTransport(обработчик))


def выполнить(сопрограмма):
    return asyncio.run(сопрограмма)


class Steam:
    """Подменный Steam: запоминает запросы и отвечает заданным текстом."""

    def __init__(self, текст="ns:http://specs.openid.net/auth/2.0\nis_valid:true\n", код=200):
        self.текст, self.код, self.запросы = текст, код, []

    def __call__(self, запрос: httpx2.Request) -> httpx2.Response:
        self.запросы.append(запрос)
        return httpx2.Response(self.код, text=self.текст)


def проверить(доводы, steam: Steam, возврат=ВОЗВРАТ):
    async def ход():
        async with клиент(steam) as к:
            return await sa.проверить_вход(к, доводы, возврат)
    return выполнить(ход())


# --- адрес входа ---

def test_адрес_входа():
    адрес = sa.адрес_входа(ВОЗВРАТ)
    части = urlsplit(адрес)
    assert f"{части.scheme}://{части.netloc}{части.path}" == sa.ВХОД
    д = {к: в[0] for к, в in parse_qs(части.query).items()}
    assert д == {
        "openid.ns": "http://specs.openid.net/auth/2.0",
        "openid.mode": "checkid_setup",
        "openid.return_to": ВОЗВРАТ,
        "openid.realm": "http://127.0.0.1:8791/",
        "openid.identity": "http://specs.openid.net/auth/2.0/identifier_select",
        "openid.claimed_id": "http://specs.openid.net/auth/2.0/identifier_select",
    }


# --- проверка входа ---

def test_годный_вход_steamid_и_тело_проверки():
    steam = Steam()
    доводы = {**образец("openid_valid.json"), "чужое": "x"}
    assert проверить(доводы, steam) == "76561197960287930"
    [запрос] = steam.запросы
    assert запрос.method == "POST" and str(запрос.url) == sa.ВХОД
    тело = {к: в[0] for к, в in parse_qs(запрос.content.decode()).items()}
    assert тело["openid.mode"] == "check_authentication"
    assert "чужое" not in тело
    assert тело["openid.sig"] == доводы["openid.sig"]
    assert тело["openid.claimed_id"] == доводы["openid.claimed_id"]


def test_подделка_is_valid_false():
    steam = Steam("ns:http://specs.openid.net/auth/2.0\nis_valid:false\n")
    assert проверить(образец("openid_valid.json"), steam) is None
    assert len(steam.запросы) == 1


def test_похожая_строка_не_годна():
    assert проверить(образец("openid_valid.json"), Steam("is_valid:true_not\n")) is None


ПОРЧА = {
    "чужой return_to": {"openid.return_to": "http://evil.example/auth/steam"},
    "return_to с хвостом": {"openid.return_to": ВОЗВРАТ + "?x=1"},
    "не id_res": {"openid.mode": "cancel"},
    "чужой op_endpoint": {"openid.op_endpoint": "https://evil.example/openid/login"},
    "claimed_id не того вида": {
        "openid.claimed_id": "https://evil.example/openid/id/76561197960287930",
        "openid.identity": "https://evil.example/openid/id/76561197960287930",
    },
    "claimed_id короткий": {
        "openid.claimed_id": "https://steamcommunity.com/openid/id/123",
        "openid.identity": "https://steamcommunity.com/openid/id/123",
    },
    "claimed_id с переводом строки": {
        "openid.claimed_id": "https://steamcommunity.com/openid/id/76561197960287930\n",
        "openid.identity": "https://steamcommunity.com/openid/id/76561197960287930\n",
    },
    "claimed_id не равен identity": {"openid.identity": "https://steamcommunity.com/openid/id/76561197960287931"},
    "claimed_id без подписи": {"openid.signed": "signed,op_endpoint,identity,return_to,response_nonce,assoc_handle"},
    "return_to без подписи": {"openid.signed": "signed,op_endpoint,claimed_id,identity,response_nonce,assoc_handle"},
}


@pytest.mark.parametrize("случай", list(ПОРЧА))
def test_негодный_вид_без_запроса_к_steam(случай):
    steam = Steam()
    assert проверить({**образец("openid_valid.json"), **ПОРЧА[случай]}, steam) is None
    assert steam.запросы == []


def test_пустые_доводы():
    steam = Steam()
    assert проверить({}, steam) is None and steam.запросы == []


def test_steam_не_ответил_исключение():
    def обрыв(запрос):
        raise httpx2.ConnectError("нет связи", request=запрос)
    with pytest.raises(sa.ОшибкаSteam):
        проверить(образец("openid_valid.json"), обрыв)


def test_steam_ответил_500_исключение():
    with pytest.raises(sa.ОшибкаSteam):
        проверить(образец("openid_valid.json"), Steam("", 500))


# --- библиотека ---

def библиотека(обработчик):
    async def ход():
        async with клиент(обработчик) as к:
            return await sa.библиотека(к, КЛЮЧ, "76561197960287930")
    return выполнить(ход())


def test_библиотека_часы_и_доводы():
    запросы = []

    def steam(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json={"response": образец("owned_sample.json")["response"]})

    игры = библиотека(steam)
    assert [и.номер for и in игры] == [1145360, 413150, 238320]
    hades, stardew, outlast = игры
    assert hades == sa.Своя(1145360, "Hades", 120.0, 1.5, 1760000000)
    assert stardew.часы == 0.75 and stardew.часы_2нед == 0.0
    assert outlast.часы == 0.0 and outlast.последний_запуск is None
    д = {к: в[0] for к, в in parse_qs(запросы[0].url.query.decode()).items()}
    assert запросы[0].url.path == "/IPlayerService/GetOwnedGames/v1/"
    assert д == {"key": КЛЮЧ, "steamid": "76561197960287930", "include_appinfo": "1",
                 "include_played_free_games": "1", "format": "json"}


@pytest.mark.parametrize("код", [401, 403])
def test_библиотека_ключ_не_принят_без_ключа_в_тексте(код):
    with pytest.raises(sa.КлючНеПринят) as беда:
        библиотека(lambda з: httpx2.Response(код, text=f"<html>key={КЛЮЧ}</html>"))
    assert "ключ Steam не принят" in str(беда.value)
    assert КЛЮЧ not in str(беда.value)


def test_библиотека_обрыв_без_адреса_в_тексте():
    def обрыв(запрос):
        raise httpx2.ConnectError(f"не достучался до {запрос.url}", request=запрос)
    with pytest.raises(sa.ОшибкаSteam) as беда:
        библиотека(обрыв)
    assert КЛЮЧ not in str(беда.value)
    # Исходная ошибка с адресом не осталась в цепочке.
    assert беда.value.__context__ is None and беда.value.__cause__ is None


def test_библиотека_закрыта():
    with pytest.raises(sa.БиблиотекаЗакрыта) as беда:
        библиотека(lambda з: httpx2.Response(200, json=образец("owned_private.json")))
    assert "Сведения об играх" in str(беда.value) and "Приватность" in str(беда.value)


def test_библиотека_пустая_открытая():
    assert библиотека(lambda з: httpx2.Response(200, json={"response": {"game_count": 0}})) == []


def test_библиотека_не_json():
    with pytest.raises(sa.ОшибкаSteam):
        библиотека(lambda з: httpx2.Response(200, text="<html>"))


# --- список желаемого ---

def желаемое(обработчик, ключ=""):
    async def ход():
        async with клиент(обработчик) as к:
            return await sa.список_желаемого(к, "76561197960287930", ключ)
    return выполнить(ход())


def test_список_желаемого_по_priority_без_ключа():
    запросы = []

    def steam(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("wishlist_sample.json"))

    assert желаемое(steam) == [413150, 1086940, 1145360]
    assert "key" not in parse_qs(запросы[0].url.query.decode())
    assert запросы[0].url.path == "/IWishlistService/GetWishlist/v1/"


def test_список_желаемого_с_ключом():
    запросы = []

    def steam(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("wishlist_empty.json"))

    assert желаемое(steam, КЛЮЧ) == []
    assert parse_qs(запросы[0].url.query.decode())["key"] == [КЛЮЧ]
