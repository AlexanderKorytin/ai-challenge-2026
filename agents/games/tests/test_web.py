"""Веб без сети и ключей: календарь, состояние, оценки, вход, ключи."""

import urllib.parse
from datetime import date, datetime, timezone

import pytest
from starlette.testclient import TestClient

import games_db
import secrets_store
import steam_account
import steam_store as ss
import taste
import web


@pytest.fixture
def клиент(monkeypatch):
    async def ничего():
        return None

    async def ollama():
        return {"служба": True, "модели": {м: True for м in web.МЕСТНЫЕ_МОДЕЛИ}}

    async def пересмотр(игра, причина, род):
        return {"вкус_записан": True, "пересмотр": "поправил"}

    monkeypatch.setattr(web, "_поднять_при_старте", ничего)
    monkeypatch.setattr(web, "_ollama", ollama)
    monkeypatch.setattr(web, "_пересмотр", пересмотр)
    async def в_индекс(*а):
        return None

    monkeypatch.setattr(web, "_в_индекс", в_индекс)
    monkeypatch.setattr(web, "_синхронизировать_сейчас", ничего)
    with TestClient(web.app, base_url="http://127.0.0.1:8791") as к:
        yield к


def игра(номер, *, доля=0.9, вышла=True, дата=None):
    return ss.Игра(номер=номер, имя=f"игра {номер}", вышла=вышла, дата=дата,
                   отзывы=ss.Отзывы("", int(доля * 1000), 1000))


def test_ics_событие_на_дату_и_без_даты_нет():
    записи = [
        {"номер": 1, "имя": "Игра, с запятой", "дата": "2027-03-04", "причина": "по вкусу",
         "ссылка": "https://store.steampowered.com/app/1/"},
        {"номер": 2, "имя": "Без даты", "дата": None, "причина": "", "ссылка": "x"},
    ]
    текст = web.календарь_ics(записи, datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert текст.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20270304" in текст and "SUMMARY:Выход: Игра\\, с запятой" in текст


def test_порядок_по_оценке_и_плюс(клиент):
    с = games_db.открыть()
    for н, доля in ((5, 0.6), (6, 0.97)):
        games_db.запомнить_игру(с, игра(н, доля=доля))
        games_db.предложить(с, н, "")
    д = клиент.get("/api/state").json()
    assert [и["номер"] for и in д["предложения"]] == [6, 5] and д["предложения"][0]["оценка_steamdb"] > 90
    assert клиент.post("/api/rate", json={"номер": 6, "плюс": True}).status_code == 200
    assert [и["номер"] for и in клиент.get("/api/state").json()["желаемое"]] == [6]
    assert клиент.post("/api/rate", json={"номер": 999, "плюс": True}).status_code == 404


def test_минус_на_игре_календаря_убирает_отовсюду(клиент):
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(8, вышла=False, дата=date(2027, 3, 4)))
    games_db.в_календарь(с, 8, "")
    assert "steam-8@" in клиент.get("/calendar.ics").text
    ответ = клиент.post("/api/rate", json={"номер": 8, "плюс": False, "причина": "скучно"})
    assert ответ.status_code == 200 and ответ.json()["вкус_записан"]
    assert клиент.get("/api/state").json()["календарь"] == []
    assert "steam-8@" not in клиент.get("/calendar.ics").text
    assert 8 in taste.запреты(web.каталог())


def test_уже_играл_не_понравилась_запрет(клиент):
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(9))
    games_db.предложить(с, 9, "")
    assert клиент.post("/api/played", json={"номер": 9, "понравилась": False}).status_code == 200
    assert клиент.get("/api/state").json()["предложения"] == [] and 9 in taste.запреты(web.каталог())


def test_вход_подделка_и_повтор(клиент, monkeypatch):
    async def проверить(к, доводы, возврат):
        return "76561197960287930" if доводы.get("openid.sig") == "годна" else None

    monkeypatch.setattr(steam_account, "проверить_вход", проверить)

    def начать():
        адрес = клиент.get("/auth/steam/start", follow_redirects=False).headers["location"]
        возврат = urllib.parse.parse_qs(urllib.parse.urlparse(адрес).query)["openid.return_to"][0]
        return urllib.parse.parse_qs(urllib.parse.urlparse(возврат).query)["state"][0]

    # Вход, который этот браузер не начинал, — отказ: чужой подписанный ответ не входит.
    assert клиент.get("/auth/steam", params={"state": "чужая", "openid.response_nonce": "0", "openid.sig": "годна"}, follow_redirects=False).status_code == 403
    assert клиент.get("/auth/steam", params={"state": начать(), "openid.response_nonce": "1", "openid.sig": "нет"}, follow_redirects=False).status_code == 403
    метка = начать()
    assert клиент.get("/auth/steam", params={"state": метка, "openid.response_nonce": "2", "openid.sig": "годна"}, follow_redirects=False).status_code == 302
    assert secrets_store.загрузить().steamid == "76561197960287930"
    # Та же метка входа второй раз — отказ.
    assert клиент.get("/auth/steam", params={"state": метка, "openid.response_nonce": "3", "openid.sig": "годна"}, follow_redirects=False).status_code == 403
    # Тот же ответ Steam с новой меткой — отказ: перехваченный ответ не входит повторно.
    assert клиент.get("/auth/steam", params={"state": начать(), "openid.response_nonce": "2", "openid.sig": "годна"}, follow_redirects=False).status_code == 400


def test_ключ_не_уходит_на_страницу(клиент, monkeypatch):
    async def годен(ключ):
        return None

    monkeypatch.setattr(web, "_проверить_deepseek", годен)
    ответ = клиент.post("/api/keys", json={"deepseek": "sk-тайна-123"})
    assert ответ.json()["итоги"]["deepseek"] == "принят" and "sk-тайна-123" not in ответ.text
    состояние = клиент.get("/api/state")
    assert состояние.json()["настройка"]["ключи"]["deepseek"] is True and "sk-тайна-123" not in состояние.text
    assert secrets_store.загрузить().deepseek_ключ == "sk-тайна-123"


def test_негодный_ключ_не_ложится_в_файл(клиент, monkeypatch):
    async def негоден(ключ):
        return "ключ DeepSeek не принят"

    monkeypatch.setattr(web, "_проверить_deepseek", негоден)
    assert клиент.post("/api/keys", json={"deepseek": "плохой"}).json()["итоги"]["deepseek"] == "ключ DeepSeek не принят"
    assert secrets_store.загрузить().deepseek_ключ == ""


def test_чужой_сайт_не_пишет(клиент):
    """Разбор рисков: «простой» запрос чужой страницы и подмена DNS."""
    assert клиент.post("/api/keys", content='{"itad":""}', headers={"Content-Type": "text/plain"}).status_code == 403
    assert клиент.post("/api/logout", json={}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert клиент.post("/api/logout", json={}, headers={"Origin": "http://127.0.0.1:8791"}).status_code == 200
    assert клиент.get("/api/state", headers={"Host": "rebind.evil.example:8791"}).status_code == 400


def test_кнопка_задания(клиент, monkeypatch):
    import jobs

    async def задание(род, каталог):
        raise jobs.УжеИдёт("задание «day» уже идёт")

    monkeypatch.setattr(jobs, "запустить", задание)
    assert клиент.post("/api/run/day", json={}).status_code == 202
    assert клиент.post("/api/run/xx", json={}).status_code == 400
    import time
    for _ in range(50):
        if any("уже идёт в службе" in з["текст"] for з in клиент.get("/api/state").json()["лента"]):
            break
        time.sleep(0.02)
    else:
        raise AssertionError("строки «уже идёт» в ленте нет")
