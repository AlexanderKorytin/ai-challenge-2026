"""Ход заданий на подменённых магазине, судье и учётке: сети, ключей и местной модели не нужно."""

import asyncio
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

import games_db
import jobs
import judge
import steam_store as ss
import taste


@pytest.fixture
def среда(tmp_path, monkeypatch):
    monkeypatch.setattr(taste, "критерии", lambda к: ([taste.Критерий(122, "Ролевая игра", 5)], []))
    monkeypatch.setattr(taste, "запреты", lambda к: {2})
    вкус = {"отпечаток": "А"}
    monkeypatch.setattr(taste, "отпечаток", lambda к: вкус["отпечаток"])
    уведомления: list[str] = []

    async def уведомить(текст):
        уведомления.append(текст)

    monkeypatch.setattr(jobs, "уведомить", уведомить)
    return tmp_path, уведомления, вкус


def отзывы(доля, всего=1000):
    return ss.Отзывы("", int(доля * всего), всего)


def игра(номер, *, вышла=True, род="game", цена=100000, скидка=0, доля=0.9, дата=None):
    return ss.Игра(номер=номер, имя=f"игра {номер}", вышла=вышла, род=род, цена=цена,
                   цена_без_скидки=100000, скидка=скидка, отзывы=отзывы(доля), дата=дата)


def магазин(игры: dict[int, ss.Игра], страницы: list[list[int]] | None = None):
    # Поиск отдаёт номер со сводкой отзывов — сводку берём из самой игры.
    def пары(номера):
        return [(н, игры[н].отзывы if н in игры else ss.НЕТ_ОТЗЫВОВ) for н in номера]

    async def найти(метка, будущие, страница):
        if страницы is not None:
            return пары(страницы[страница]) if страница < len(страницы) else []
        return пары(игры) if страница == 0 else []

    async def взять(номер):
        return игры.get(номер)

    return jobs.Магазин(найти=найти, игра=взять)


def судья(судимые: list[int], подходит=lambda и: True):
    async def с(и):
        судимые.append(и.номер)
        return judge.Вердикт(подходит(и), "по вкусу")
    return с


def test_запрет_и_покупка_не_доходят_до_судьи(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.обновить_библиотеку(с, [SimpleNamespace(номер=3, имя="своя", часы=1.0, часы_2нед=0.0, последний_запуск=None)])
    судимые: list[int] = []
    итог = asyncio.run(jobs.день(tmp, магазин({1: игра(1), 2: игра(2), 3: игра(3)}), судья(судимые)))
    assert судимые == [1] and итог.подошло == ["игра 1"]


def test_к_судье_по_убыванию_оценки(среда):
    tmp, _, _ = среда
    судимые: list[int] = []
    игры = {1: игра(1, доля=0.6), 2_0: игра(20, доля=0.95), 30: ss.Игра(номер=30, имя="без отзывов")}
    asyncio.run(jobs.день(tmp, магазин({1: игры[1], 20: игры[20], 30: игры[30]}), судья(судимые)))
    assert судимые == [20, 1, 30]


def test_десять_открытых_судья_не_зовётся(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    for н in range(100, 110):
        games_db.запомнить_игру(с, игра(н))
        games_db.предложить(с, н, "")
    судимые: list[int] = []
    итог = asyncio.run(jobs.день(tmp, магазин({1: игра(1)}), судья(судимые)))
    assert судимые == [] and "новых не искал" in итог.текст("день")


def test_останавливается_на_нужном_числе(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(jobs, "НУЖНО", 2)
    судимые: list[int] = []
    asyncio.run(jobs.день(tmp, магазин({н: игра(н) for н in (1, 3, 4, 5)}), судья(судимые)))
    assert len(судимые) == 2


def test_отказ_помнится_при_том_же_вкусе(среда):
    tmp, _, вкус = среда
    судимые: list[int] = []
    м = магазин({1: игра(1)})
    отказ = судья(судимые, lambda и: False)
    asyncio.run(jobs.день(tmp, м, отказ))
    asyncio.run(jobs.день(tmp, м, отказ))
    assert судимые == [1]
    вкус["отпечаток"] = "Б"  # вкус поменялся — прежний отказ снова судится
    asyncio.run(jobs.день(tmp, м, отказ))
    assert судимые == [1, 1]


def test_первая_страница_с_годными(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(1))
    games_db.предложить(с, 1, "")
    судимые: list[int] = []
    asyncio.run(jobs.день(tmp, магазин({5: игра(5)}, страницы=[[1], [5]]), судья(судимые)))
    assert судимые == [5]


def test_сбой_магазина_на_одной_игре(среда):
    tmp, _, _ = среда

    async def взять(номер):
        if номер == 1:
            raise ss.ОшибкаМагазина("HTTP 503")
        return игра(номер)

    м = jobs.Магазин(найти=lambda м, б, стр: asyncio.sleep(0, [(1, ss.НЕТ_ОТЗЫВОВ), (4, ss.НЕТ_ОТЗЫВОВ)] if стр == 0 else []), игра=взять)
    итог = asyncio.run(jobs.день(tmp, м, судья([])))
    assert итог.подошло == ["игра 4"] and len(итог.сбои_магазина) == 1


def test_скидка_с_минимумом_один_раз_и_на_список_steam(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    учётка = jobs.Учётка(
        библиотека=lambda: asyncio.sleep(0, []), список_желаемого=lambda: asyncio.sleep(0, [7])
    )

    async def минимум(номер):
        return SimpleNamespace(цена=30000, валюта="RUB", когда="2026-06-01T00:00:00Z")

    м = магазин({7: игра(7, цена=50000, скидка=50)})
    отказ = судья([], lambda и: False)
    asyncio.run(jobs.день(tmp, м, отказ, учётка, минимум))
    asyncio.run(jobs.день(tmp, м, отказ, учётка, минимум))
    скидки = [у for у in уведомления if у.startswith("Скидка")]
    assert len(скидки) == 1 and "минимум был 300 руб. (2026-06-01)" in скидки[0]


def test_скидка_и_при_сбое_отбора(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(7))
    games_db.оценить(с, 7, True, "")

    async def упасть(и):
        raise RuntimeError("местная модель не ответила")

    with pytest.raises(RuntimeError):
        asyncio.run(jobs.день(tmp, магазин({1: игра(1), 7: игра(7, цена=50000, скидка=50)}), упасть))
    assert any(з["род"] == "скидка" for з in games_db.лента(с))


def test_выход_сегодня_одно_уведомление(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(8, вышла=False, дата=date.today()))
    games_db.в_календарь(с, 8, "")
    for _ in range(2):
        asyncio.run(jobs.день(tmp, магазин({}), судья([])))
    assert [у for у in уведомления if у.startswith("Сегодня выходит")] == ["Сегодня выходит «игра 8»"]


def test_неделя_обновляет_календарь(среда):
    tmp, _, _ = среда
    с = games_db.открыть()
    for н in (8, 9):
        games_db.запомнить_игру(с, игра(н, вышла=False))
        games_db.в_календарь(с, н, "")
    новая_дата = date.today() + timedelta(days=30)
    м = магазин({8: игра(8, вышла=False, дата=новая_дата), 9: игра(9, вышла=True)})
    asyncio.run(jobs.неделя(tmp, м, судья([], lambda и: False)))
    календарь = {з["номер"]: з for з in games_db.календарь(с)}
    assert календарь[8]["дата"] == новая_дата.isoformat() and 9 not in календарь
    assert 9 in [з["номер"] for з in games_db.предложения(с)]


def test_сверка_через_полгода(среда):
    tmp, уведомления, _ = среда
    с = games_db.открыть()
    asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))
    assert not [у for у in уведомления if "сверки" in у]
    games_db.поставить_отметку(с, "последняя_сверка", (date.today() - timedelta(days=200)).isoformat())
    asyncio.run(jobs.неделя(tmp, магазин({}), судья([])))
    assert any("сверки" in у for у in уведомления)


def test_без_ключа_steam_одна_строка(среда):
    tmp, _, _ = среда
    for _ in range(2):
        asyncio.run(jobs.день(tmp, магазин({}), судья([])))
    с = games_db.открыть()
    assert sum("Ключа Steam нет" in з["текст"] for з in games_db.лента(с)) == 1


def test_без_критериев_задание_падает(среда, monkeypatch):
    tmp, _, _ = среда
    monkeypatch.setattr(taste, "критерии", lambda к: ([], []))
    with pytest.raises(RuntimeError, match="Критериев"):
        asyncio.run(jobs.день(tmp, магазин({}), судья([])))


def test_негодная_игра_не_держит_страницу(среда):
    """Опыт просмотра: страница 0 — [уже предложена, не вышла], страница 1 — [годная]."""
    tmp, _, _ = среда
    с = games_db.открыть()
    games_db.запомнить_игру(с, игра(1))
    games_db.предложить(с, 1, "")
    игры = {1: игра(1), 7: игра(7, вышла=False, дата=date.today() + timedelta(days=60)), 5: игра(5)}
    судимые: list[int] = []
    for _ in range(2):
        asyncio.run(jobs.день(tmp, магазин(игры, страницы=[[1, 7], [5]]), судья(судимые)))
    assert судимые == [5]


def test_не_игра_запоминается(среда):
    tmp, _, _ = среда
    вызовы: list[int] = []

    async def взять(номер):
        вызовы.append(номер)
        return игра(номер, род="demo")

    м = jobs.Магазин(найти=lambda м, б, стр: asyncio.sleep(0, [(3, ss.НЕТ_ОТЗЫВОВ)] if стр == 0 else []), игра=взять)
    asyncio.run(jobs.день(tmp, м, судья([])))
    asyncio.run(jobs.день(tmp, м, судья([])))
    assert вызовы == [3]


def test_замок_задания(среда):
    замок = jobs._замок("day")
    try:
        with pytest.raises(jobs.УжеИдёт):
            jobs._замок("day")
    finally:
        замок.close()
    jobs._замок("day").close()
