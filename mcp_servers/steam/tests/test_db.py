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
    ).fetchone()[0] == "2"


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


# ---- схема 2 ----

# Схема версии 1 — дословно как её писал код шагов 1–3. Копия, а не `db.СХЕМА`: правка
# модуля не должна менять старый файл, на котором проверяется переход.
СХЕМА_1 = """
CREATE TABLE IF NOT EXISTS meta (
    ключ TEXT PRIMARY KEY,
    значение TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS games (
    номер INTEGER PRIMARY KEY,
    имя TEXT NOT NULL,
    описание TEXT NOT NULL DEFAULT '',
    жанры TEXT NOT NULL DEFAULT '[]',
    дата_текст TEXT NOT NULL DEFAULT '',
    дата TEXT,
    вышла INTEGER NOT NULL DEFAULT 1,
    цена INTEGER,
    цена_без_скидки INTEGER,
    скидка INTEGER NOT NULL DEFAULT 0,
    валюта TEXT NOT NULL DEFAULT '',
    бесплатна INTEGER NOT NULL DEFAULT 0,
    отзывы_сводка TEXT NOT NULL DEFAULT '',
    отзывы_положительных INTEGER NOT NULL DEFAULT 0,
    отзывы_всего INTEGER NOT NULL DEFAULT 0,
    обновлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS suggestions (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    предложено TEXT NOT NULL,
    оценка INTEGER,
    причина_оценки TEXT,
    оценено TEXT
);
CREATE TABLE IF NOT EXISTS calendar (
    номер INTEGER PRIMARY KEY,
    причина TEXT NOT NULL,
    добавлено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS prices (
    номер INTEGER PRIMARY KEY,
    цена INTEGER NOT NULL,
    скидка INTEGER NOT NULL,
    отмечено TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS feed (
    номер INTEGER PRIMARY KEY AUTOINCREMENT,
    время TEXT NOT NULL,
    род TEXT NOT NULL,
    текст TEXT NOT NULL
);
"""


def _файл_версии_1(файл) -> None:
    файл.parent.mkdir(parents=True, exist_ok=True)
    сырое = sqlite3.connect(файл)
    сырое.executescript(СХЕМА_1)
    сырое.execute("INSERT INTO meta VALUES ('версия_схемы', '1')")
    сырое.execute(
        "INSERT INTO suggestions (номер, причина, предложено) VALUES (42, 'старое', '2026-10-01')"
    )
    сырое.commit()
    сырое.close()


def test_переход_с_версии_1(каталог):
    _файл_версии_1(db.путь())
    # Читатель переходить не вправе: файл версии 1 он отвергает и не трогает.
    with pytest.raises(db.НоваяСхема, match="версия схемы 1"):
        db.открыть(только_чтение=True)
    с = db.открыть()
    try:
        таблицы = {с_[0] for с_ in с.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        assert {"refusals", "library", "steam_wishlist", "played", "marks"} <= таблицы
        assert [и["номер"] for и in db.предложения(с)] == [42]
        assert с.execute(
            "SELECT значение FROM meta WHERE ключ = 'версия_схемы'"
        ).fetchone()[0] == "2"
    finally:
        с.close()
    # Повторное открытие перехода не повторяет.
    db.открыть().close()


def test_незнакомая_старая_версия(каталог):
    _файл_версии_1(db.путь())
    сырое = sqlite3.connect(db.путь())
    сырое.execute("UPDATE meta SET значение = '0'")
    сырое.commit()
    сырое.close()
    with pytest.raises(db.НоваяСхема, match="перехода нет"):
        db.открыть()


def test_отказы_по_отпечатку(с):
    db.запомнить_отказ(с, 1, "вкус-а")
    db.запомнить_отказ(с, 1, "вкус-а")
    db.запомнить_отказ(с, 2, "вкус-а")
    assert db.отказано(с, "вкус-а") == {1, 2}
    assert db.отказано(с, "вкус-б") == set()


class _Своя:
    def __init__(self, номер, имя, часы, часы_2нед=0.0, последний_запуск=None):
        self.номер, self.имя, self.часы = номер, имя, часы
        self.часы_2нед, self.последний_запуск = часы_2нед, последний_запуск


def test_купленная_пропадает_из_выдачи(с):
    db.предложить(с, 1, "предложение")
    db.в_календарь(с, 1, "календарь")
    db.оценить(с, 1, True, "хочу")
    db.обновить_список_steam(с, [1])
    assert [и["номер"] for и in db.желаемое(с)] == [1]
    db.обновить_библиотеку(с, [_Своя(1, "Купленная", 3.5)])
    assert db.куплены(с) == {1}
    assert db.предложения(с) == []
    assert db.календарь(с) == []
    assert db.желаемое(с) == []


def test_минус_убирает_из_календаря(с):
    db.в_календарь(с, 1, "ждёт")
    db.в_календарь(с, 2, "ждёт")
    db.оценить(с, 1, False, "не моё")
    assert [и["номер"] for и in db.календарь(с)] == [2]
    assert [и["номер"] for и in db.предложения(с)] == []
    db.убрать_из_календаря(с, 2)
    assert db.календарь(с) == []


def test_открытых(с):
    for номер in (1, 2, 3, 4, 5):
        db.предложить(с, номер, "п")
    db.оценить(с, 1, True, "да")
    db.оценить(с, 2, False, "нет")
    db.обновить_библиотеку(с, [_Своя(3, "Купил", 1.0)])
    db.уже_играл(с, 4, понравилась=True)
    assert db.открытых(с) == 1
    assert [и["номер"] for и in db.предложения(с, только_без_оценки=True)] == [5]


def test_уже_играл_в_показанных(с):
    db.уже_играл(с, 9, понравилась=False)
    assert 9 in db.показанные(с)
    db.в_календарь(с, 9, "ждёт")
    assert db.календарь(с) == []


def test_желаемое_со_списком_steam(с):
    db.запомнить_игру(с, Игра(номер=5, имя="Своя"))
    db.оценить(с, 5, True, "нравится")
    db.обновить_список_steam(с, [5, 6])
    желаемое = db.желаемое(с)
    assert [(и["номер"], и["источник"]) for и in желаемое] == [(5, "свой"), (6, "steam")]
    # Игры списка Steam ещё нет в `games` — имя неизвестно, запись всё равно есть.
    assert желаемое[1]["имя"] is None
    assert db.список_steam(с) == {5, 6}
    db.обновить_список_steam(с, [7])
    assert db.список_steam(с) == {7}


def test_библиотека_по_часам_и_замена(с):
    db.обновить_библиотеку(с, [_Своя(1, "Мало", 2.0), _Своя(2, "Много", 120.0, 5.5, 1760000000)])
    assert [(и["номер"], и["часы"]) for и in db.библиотека(с)] == [(2, 120.0), (1, 2.0)]
    assert db.библиотека(с)[0]["последний_запуск"] == 1760000000
    db.обновить_библиотеку(с, [_Своя(3, "Новая", 1.0)])
    assert db.куплены(с) == {3}


def test_отметки(с):
    assert db.отметка(с, "последняя_сверка") is None
    db.поставить_отметку(с, "последняя_сверка", "2026-10-16")
    db.поставить_отметку(с, "последняя_сверка", "2027-04-16")
    assert db.отметка(с, "последняя_сверка") == "2027-04-16"
