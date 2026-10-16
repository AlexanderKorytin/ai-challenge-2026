"""Ход заданий на подменённых магазине и судье: сети, ключа и местной модели не нужно."""

import asyncio

import pytest

import games_db
import jobs
import judge
import steam_store as ss
import taste


@pytest.fixture
def каталог(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path / "агент"))
    monkeypatch.setenv("MYHARNESS_STATE_DIR", str(tmp_path / "состояние"))
    monkeypatch.setenv("MYHARNESS_CONFIG_DIR", str(tmp_path / "настройки"))
    monkeypatch.setattr(taste, "критерии", lambda к: ([taste.Критерий(122, "Ролевая игра", 3)], []))
    monkeypatch.setattr(taste, "запреты", lambda к: {2})
    уведомления: list[str] = []
    monkeypatch.setattr(jobs, "уведомить", уведомления.append)
    return tmp_path, уведомления


def игра(номер, *, вышла=True, род="game", цена=100000, скидка=0):
    return ss.Игра(номер=номер, имя=f"игра {номер}", вышла=вышла, род=род, цена=цена,
                   цена_без_скидки=100000, скидка=скидка)


def магазин(игры: dict[int, ss.Игра]):
    async def найти(метка, будущие):
        return list(игры)

    async def взять(номер):
        return игры.get(номер)

    return jobs.Магазин(найти=найти, игра=взять)


def судья_с_учётом(судимые: list[int], подходит=lambda и: True):
    async def судья(и):
        судимые.append(и.номер)
        return judge.Вердикт(подходит(и), "по вкусу")
    return судья


def test_запрет_не_доходит_до_судьи(каталог):
    tmp, _ = каталог
    судимые: list[int] = []
    итог = asyncio.run(jobs.день(tmp, магазин({1: игра(1), 2: игра(2)}), судья_с_учётом(судимые)))
    assert судимые == [1]
    assert итог.запрещено == 1 and итог.подошло == ["игра 1"]


def test_показанная_не_судится_второй_раз(каталог):
    tmp, _ = каталог
    судимые: list[int] = []
    м = магазин({1: игра(1)})
    asyncio.run(jobs.день(tmp, м, судья_с_учётом(судимые)))
    итог = asyncio.run(jobs.день(tmp, м, судья_с_учётом(судимые)))
    assert судимые == [1] and итог.показаны == 1


def test_неделя_берёт_только_будущие_игры(каталог):
    tmp, _ = каталог
    судимые: list[int] = []
    игры = {1: игра(1, вышла=False), 3: игра(3, вышла=False, род="demo"), 4: игра(4, вышла=True)}
    итог = asyncio.run(jobs.неделя(tmp, магазин(игры), судья_с_учётом(судимые)))
    assert судимые == [1] and итог.не_игры == 2
    с = games_db.открыть()
    assert [з["номер"] for з in games_db.календарь(с)] == [1]


def test_негодный_ответ_модели_не_теряет_остальные(каталог):
    tmp, _ = каталог

    async def судья(и):
        if и.номер == 1:
            raise ValueError("ответ модели — не JSON")
        return judge.Вердикт(True, "да")

    итог = asyncio.run(jobs.день(tmp, магазин({1: игра(1), 5: игра(5)}), судья))
    assert итог.подошло == ["игра 5"] and len(итог.ошибки_разбора) == 1


def test_новая_скидка_один_раз(каталог):
    tmp, уведомления = каталог
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")
    со_скидкой = магазин({7: игра(7, цена=50000, скидка=50)})
    отказ = судья_с_учётом([], подходит=lambda и: False)
    asyncio.run(jobs.день(tmp, со_скидкой, отказ))
    asyncio.run(jobs.день(tmp, со_скидкой, отказ))
    assert len(уведомления) == 1 and "50 %" in уведомления[0]
    assert [з["род"] for з in games_db.лента(с) if з["род"] == "скидка"] == ["скидка"]
    # Парная: без скидки уведомления нет.
    asyncio.run(jobs.день(tmp, магазин({7: игра(7)}), отказ))
    assert len(уведомления) == 1


def test_без_критериев_задание_падает(каталог, monkeypatch):
    tmp, _ = каталог
    monkeypatch.setattr(taste, "критерии", lambda к: ([], []))
    with pytest.raises(RuntimeError, match="Критериев"):
        asyncio.run(jobs.день(tmp, магазин({}), судья_с_учётом([])))


def test_сбой_в_ленту_и_уведомление(каталог, monkeypatch):
    tmp, уведомления = каталог

    async def упасть(*а, **к):
        raise ss.ОшибкаМагазина("магазин ответил HTTP 503")

    monkeypatch.setattr(jobs, "день", упасть)
    with pytest.raises(ss.ОшибкаМагазина):
        asyncio.run(jobs.запустить("day", tmp))
    с = games_db.открыть()
    assert games_db.лента(с)[0]["род"] == "сбой" and "503" in уведомления[0]


def test_скидка_проверяется_и_при_сбое_отбора(каталог):
    tmp, уведомления = каталог
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")

    async def упасть(и):
        raise RuntimeError("местная модель не ответила")

    with pytest.raises(RuntimeError):
        asyncio.run(jobs.день(tmp, магазин({1: игра(1), 7: игра(7, цена=50000, скидка=50)}), упасть))
    assert any(з["род"] == "скидка" for з in games_db.лента(с)) and "50 %" in уведомления[0]


def test_сбой_одной_игры_не_мешает_скидке_другой(каталог):
    tmp, уведомления = каталог
    с = games_db.открыть()
    for н in (7, 8):
        games_db.запомнить_игру(с, игра(н))
        games_db.оценить(с, н, True, "")

    async def взять(номер):
        if номер == 7:
            raise ss.ОшибкаМагазина("магазин ответил HTTP 429")
        return игра(8, цена=50000, скидка=50)

    async def найти(метка, будущие):
        return []

    asyncio.run(jobs.день(tmp, jobs.Магазин(найти=найти, игра=взять), судья_с_учётом([])))
    роды = [з["род"] for з in games_db.лента(с)]
    assert "скидка" in роды and "сбой" in роды
