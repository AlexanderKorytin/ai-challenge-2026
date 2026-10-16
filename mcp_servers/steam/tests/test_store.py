"""Разбор ответов магазина на образцах, снятых 2026-10-16 настоящими запросами."""

import json
from datetime import date
from pathlib import Path

import httpx2
import pytest

import steam_store as ss

ОБРАЗЦЫ = Path(__file__).parent.parent / "образцы"


def образец(имя: str):
    return json.loads((ОБРАЗЦЫ / имя).read_text("utf-8"))


def test_дата_точная_и_неточная():
    assert ss.разобрать_дату("3 авг. 2023 г.") == date(2023, 8, 3)
    assert ss.разобрать_дату("3 Aug, 2023") == date(2023, 8, 3)
    assert ss.разобрать_дату("Aug 3, 2023") == date(2023, 8, 3)
    assert ss.разобрать_дату("12 мая 2027 г.") == date(2027, 5, 12)
    for неточная in ("I кв. 2027", "2027", "окт. 2026", "Скоро", ""):
        assert ss.разобрать_дату(неточная) is None


def test_игра_из_образца():
    отзывы = ss.разобрать_отзывы(образец("appreviews_1086940.json"))
    игра = ss.разобрать_игру(1086940, образец("appdetails_1086940.json"), отзывы)
    assert игра is not None
    assert игра.имя == "Baldur's Gate 3"
    assert "Ролевые игры" in игра.жанры
    assert игра.дата == date(2023, 8, 3) and игра.вышла
    assert игра.цена == 199900 and игра.скидка == 0 and игра.валюта == "RUB"
    assert игра.отзывы.текст().startswith("Крайне положительные, 97 % из ")
    assert "<" not in игра.описание
    assert игра.род == "game"


def test_неизвестная_игра():
    assert ss.разобрать_игру(1, образец("appdetails_missing.json")) is None


def test_отзывов_нет():
    assert ss.разобрать_отзывы({"query_summary": {"total_reviews": 0}}).текст() == "отзывов нет"


def test_поиск_номера_без_повторов():
    номера = ss.разобрать_поиск(образец("search_122.json"))
    assert len(номера) == 50 and len(set(номера)) == 50
    assert ss.разобрать_поиск({"results_html": '<a data-ds-appid="10,20"></a><a data-ds-appid="10"></a>'}) == [10]


def test_поиск_без_разметки_ошибка():
    with pytest.raises(ss.ОшибкаМагазина):
        ss.разобрать_поиск({"success": 1})


def test_метки():
    метки = ss.разобрать_метки(образец("tags.json"))
    assert ss.Метка(122, "Ролевая игра") in метки or any(м.номер == 122 for м in метки)
    assert len(метки) > 400


async def _с_ответами(коды, путь="/api/appdetails"):
    вызовы = []

    def отвечать(запрос):
        вызовы.append(запрос)
        код = коды[min(len(вызовы) - 1, len(коды) - 1)]
        return httpx2.Response(код, json={"ok": 1})

    async with httpx2.AsyncClient(base_url=ss.МАГАЗИН, transport=httpx2.MockTransport(отвечать)) as к:
        try:
            return await ss._взять(к, путь, {}), len(вызовы)
        except ss.ОшибкаМагазина as exc:
            return exc, len(вызовы)


def test_повтор_после_429(monkeypatch):
    import asyncio
    monkeypatch.setattr(ss, "ПАУЗЫ", (0.0, 0.0, 0.0))
    ответ, вызовов = asyncio.run(_с_ответами([429, 200]))
    assert ответ == {"ok": 1} and вызовов == 2
    ответ, вызовов = asyncio.run(_с_ответами([429]))
    assert isinstance(ответ, ss.ОшибкаМагазина) and вызовов == 4
    # 404 — не повторяется: магазин ответил, повтор дал бы то же.
    ответ, вызовов = asyncio.run(_с_ответами([404]))
    assert isinstance(ответ, ss.ОшибкаМагазина) and вызовов == 1


def test_оценка_steamdb():
    assert round(ss.оценка(ss.Отзывы("", 834482, 862216)), 2) == 0.96
    assert round(ss.оценка(ss.Отзывы("", 20, 20)), 2) == 0.80
    assert ss.оценка(ss.НЕТ_ОТЗЫВОВ) is None
    # Ради этого оценка и заведена: 100 % из 20 ниже 97 % из 862 тысяч.
    assert ss.оценка(ss.Отзывы("", 20, 20)) < ss.оценка(ss.Отзывы("", 834482, 862216))


def test_поиск_по_названию_из_образца():
    найдено = ss.разобрать_поиск_по_названию(образец("storesearch_baldurs_gat_3.json"))
    assert найдено[0] == (1086940, "Baldur's Gate 3")
    assert len(найдено) == 3
    assert ss.разобрать_поиск_по_названию({"total": 0, "items": []}) == []
    with pytest.raises(ss.ОшибкаМагазина):
        ss.разобрать_поиск_по_названию({"total": 0})


def test_найти_доводы_страницы():
    import asyncio
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json={"results_html": '<a data-ds-appid="7"></a>'})

    async def прогон():
        async with httpx2.AsyncClient(base_url=ss.МАГАЗИН, transport=httpx2.MockTransport(отвечать)) as к:
            assert await ss.найти(к, 122, будущие=False, страница=2) == [7]
            await ss.найти(к, 122, будущие=True)

    asyncio.run(прогон())
    вышедшие, будущие = (dict(з.url.params) for з in запросы)
    assert вышедшие["start"] == "100" and "sort_by" not in вышедшие
    assert вышедшие["category1"] == "998" and вышедшие["tags"] == "122"
    assert будущие["start"] == "0" and будущие["filter"] == "popularwishlist"
    assert будущие["category1"] == "998"


def test_найти_по_названию_адрес():
    import asyncio
    запросы = []

    def отвечать(запрос):
        запросы.append(запрос)
        return httpx2.Response(200, json=образец("storesearch_baldurs_gat_3.json"))

    async def прогон():
        async with httpx2.AsyncClient(base_url=ss.МАГАЗИН, transport=httpx2.MockTransport(отвечать)) as к:
            return await ss.найти_по_названию(к, "baldurs gat 3")

    assert asyncio.run(прогон())[0][0] == 1086940
    [з] = запросы
    assert з.url.path == "/api/storesearch/"
    assert dict(з.url.params) == {"cc": "ru", "l": "russian", "term": "baldurs gat 3"}


def test_поиск_со_сводкой_отзывов():
    пары = ss.разобрать_поиск_с_отзывами(образец("search_122.json"))
    assert len(пары) == 50 and len({н for н, _ in пары}) == 50
    с_отзывами = [о for _, о in пары if о.всего > 0]
    assert с_отзывами and all(0 < о.положительных <= о.всего for о in с_отзывами)
    assert ss.разобрать_поиск_с_отзывами({"results_html": '<a data-ds-appid="7"></a>'}) == [(7, ss.НЕТ_ОТЗЫВОВ)]
