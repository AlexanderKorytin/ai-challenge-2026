"""Хранилище игр на временном файле. Каталог данных уводится в `tmp_path`:
настоящий `~/.local/share` проверки не трогают."""

import sqlite3
import threading
from datetime import date

import pytest

import games_db as db
from steam_store import Игра, Отзывы


@pytest.fixture(autouse=True)
def каталог(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path / "данные"))
    return tmp_path / "данные"


@pytest.fixture
def с():
    соединение = db.открыть()
    yield соединение
    соединение.close()


def test_путь_из_окружения(каталог):
    assert db.путь() == каталог / "games.sqlite"


def test_файл_и_режим(с, каталог):
    assert (каталог / "games.sqlite").exists()
    assert с.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert с.execute("PRAGMA busy_timeout").fetchone()[0] == 5000
    assert с.execute(
        "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
    ).fetchone()[0] == "1"


def test_пара_скидки(с):
    номер = 10
    assert db.отметить_цену(с, номер, 50000, 50) is True
    assert db.отметить_цену(с, номер, 50000, 50) is False
    assert db.отметить_цену(с, номер, 40000, 60) is True
    assert db.отметить_цену(с, номер, 100000, 0) is False
    assert db.отметить_цену(с, номер, 100000, 0) is False
    # После цены без скидки та же скидка снова новость.
    assert db.отметить_цену(с, номер, 40000, 60) is True
    assert db.отметить_цену(с, номер, 40000, 60) is False


def test_показанные_и_повтор(с):
    assert db.предложить(с, 1, "любит стратегии") is True
    assert db.предложить(с, 1, "ещё раз") is False
    assert db.в_календарь(с, 2, "ждёт выхода") is True
    assert db.в_календарь(с, 2, "ещё раз") is False
    assert db.показанные(с) == {1, 2}


def test_оценки(с):
    db.предложить(с, 1, "первая")
    db.предложить(с, 2, "вторая")
    db.оценить(с, 1, True, "нравится")
    db.оценить(с, 2, False, "не моё")
    assert [и["номер"] for и in db.желаемое(с)] == [1]
    assert 2 in db.показанные(с)
    db.снять_оценку(с, 2)
    assert 2 not in db.показанные(с)
    assert db.предложить(с, 2, "снова") is True


def test_оценка_игры_календаря(с):
    db.в_календарь(с, 3, "ждёт выхода")
    db.оценить(с, 3, True, "жду")
    assert [и["номер"] for и in db.желаемое(с)] == [3]
    assert db.предложить(с, 3, "повтор") is False


def test_желаемое_с_ценой(с):
    db.запомнить_игру(с, Игра(номер=5, имя="Игра", цена=100000, скидка=0, валюта="RUB"))
    db.оценить(с, 5, True, "нравится")
    db.отметить_цену(с, 5, 50000, 50)
    [и] = db.желаемое(с)
    assert и["имя"] == "Игра"
    assert и["последняя_цена"] == 50000 and и["последняя_скидка"] == 50


def test_игра_повтор_обновляет(с):
    игра = Игра(
        номер=7, имя="Старое имя", жанры=("Стратегии", "Инди"), дата_текст="3 авг. 2023 г.",
        дата=date(2023, 8, 3), цена=199900, отзывы=Отзывы("Очень положительные", 90, 100),
    )
    db.запомнить_игру(с, игра)
    db.запомнить_игру(с, Игра(**{**игра.__dict__, "имя": "Новое имя"}))
    запись = db.игра(с, 7)
    assert запись["имя"] == "Новое имя"
    assert запись["жанры"] == ["Стратегии", "Инди"]
    assert запись["дата"] == "2023-08-03" and запись["дата_текст"] == "3 авг. 2023 г."
    assert запись["отзывы"].startswith("Очень положительные, 90 %")
    assert запись["ссылка"].endswith("/app/7/")
    assert db.игра(с, 8) is None


def test_календарь_по_дате(с):
    db.запомнить_игру(с, Игра(номер=1, имя="Поздняя", дата=date(2027, 5, 1), вышла=False))
    db.запомнить_игру(с, Игра(номер=2, имя="Без даты", дата_текст="2027", вышла=False))
    db.запомнить_игру(с, Игра(номер=3, имя="Ранняя", дата=date(2026, 11, 1), вышла=False))
    for номер in (1, 2, 3):
        db.в_календарь(с, номер, "ждёт")
    assert [и["номер"] for и in db.календарь(с)] == [3, 1, 2]


def test_предложения_без_оценки(с):
    db.предложить(с, 1, "а")
    db.предложить(с, 2, "б")
    db.оценить(с, 1, True, "да")
    assert [и["номер"] for и in db.предложения(с)] == [2, 1]
    assert [и["номер"] for и in db.предложения(с, только_без_оценки=True)] == [2]


def test_лента(с):
    db.в_ленту(с, "скидка", "первая")
    db.в_ленту(с, "итог", "вторая")
    assert [з["текст"] for з in db.лента(с)] == ["вторая", "первая"]
    assert [з["текст"] for з in db.лента(с, 1)] == ["вторая"]
    with pytest.raises(ValueError):
        db.в_ленту(с, "прочее", "текст")


def test_два_соединения_пишут_поочерёдно(с):
    ошибки: list[BaseException] = []

    def писать(метка: str) -> None:
        соединение = db.открыть()
        try:
            for номер in range(50):
                db.в_ленту(соединение, "итог", f"{метка} {номер}")
                db.отметить_цену(соединение, номер, 1000 + номер, 10)
        except BaseException as exc:  # noqa: BLE001 — ошибку потока несём в проверку
            ошибки.append(exc)
        finally:
            соединение.close()

    потоки = [threading.Thread(target=писать, args=(м,)) for м in ("а", "б")]
    for п in потоки:
        п.start()
    for п in потоки:
        п.join()
    assert ошибки == []
    assert len(db.лента(с)) == 100


def test_чтение_видит_запись(с):
    db.в_ленту(с, "сбой", "магазин молчит")
    читатель = db.открыть(только_чтение=True)
    try:
        assert [з["текст"] for з in db.лента(читатель)] == ["магазин молчит"]
        with pytest.raises(sqlite3.OperationalError):
            читатель.execute("DELETE FROM feed")
    finally:
        читатель.close()


def test_новая_схема_не_трогается(с):
    с.execute("UPDATE meta SET значение = '99' WHERE ключ = 'версия_схемы'")
    с.close()
    with pytest.raises(db.НоваяСхема, match="99"):
        db.открыть()
    with pytest.raises(db.НоваяСхема, match="99"):
        db.открыть(только_чтение=True)
    # Файл не пересоздан: версия осталась прежней.
    сырое = sqlite3.connect(db.путь())
    assert сырое.execute("SELECT значение FROM meta").fetchone()[0] == "99"
    сырое.close()


def test_чтение_без_файла(каталог):
    with pytest.raises(FileNotFoundError, match="games.sqlite"):
        db.открыть(только_чтение=True)
    assert not каталог.exists()
