"""Веб: файл календаря и пути состояния без сети и ключа."""

from datetime import datetime, timezone

from starlette.testclient import TestClient

import games_db
import steam_store as ss
import web


def test_ics_событие_на_дату_и_без_даты_нет():
    записи = [
        {"номер": 1, "имя": "Игра, с запятой", "дата": "2027-03-04", "причина": "по вкусу",
         "ссылка": "https://store.steampowered.com/app/1/"},
        {"номер": 2, "имя": "Без даты", "дата": None, "причина": "", "ссылка": "x"},
    ]
    текст = web.календарь_ics(записи, datetime(2026, 10, 16, tzinfo=timezone.utc))
    assert текст.startswith("BEGIN:VCALENDAR\r\n") and текст.endswith("END:VCALENDAR\r\n")
    assert текст.count("BEGIN:VEVENT") == 1
    assert "DTSTART;VALUE=DATE:20270304" in текст and "DTEND;VALUE=DATE:20270305" in текст
    assert "SUMMARY:Выход: Игра\\, с запятой" in текст


def test_состояние_и_плюс(monkeypatch):
    with TestClient(web.app) as к:
        д = к.get("/api/state").json()
        assert д["опрос_пройден"] is False and д["предложения"] == []
        с = games_db.открыть()
        games_db.запомнить_игру(с, ss.Игра(номер=5, имя="Пятая"))
        games_db.предложить(с, 5, "по вкусу")
        assert [и["номер"] for и in к.get("/api/state").json()["предложения"]] == [5]
        assert к.post("/api/rate", json={"номер": 5, "плюс": True}).status_code == 200
        д = к.get("/api/state").json()
        assert д["предложения"] == [] and [и["номер"] for и in д["желаемое"]] == [5]
        assert к.post("/api/rate", json={"номер": 999, "плюс": True}).status_code == 404
        assert к.get("/calendar.ics").headers["content-type"].startswith("text/calendar")
