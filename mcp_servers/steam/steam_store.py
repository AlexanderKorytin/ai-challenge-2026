"""Магазин Steam: запросы и разбор ответов.

Ключ не нужен: все адреса — открытые адреса витрины магазина. Поиск (`search/results`) открытым
API не является: номера игр достаются из HTML выдачи по атрибуту `data-ds-appid`. Магазин сменит
разметку — `найти` вернёт пустой список, и задание напишет об этом в ленту.

Регион `cc=ru` и язык `l=russian` — цены в рублях, описания и даты по-русски.

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from dataclasses import dataclass, field
from datetime import date

import httpx2

МАГАЗИН = "https://store.steampowered.com"
РЕГИОН = {"cc": "ru", "l": "russian"}
# Заголовок HTTP — только ASCII: русский текст `httpx2` отвергает при сборке клиента.
ПРЕДСТАВЛЕНИЕ = "games-agent/0.1 (personal game picker)"
ОЖИДАНИЕ = httpx2.Timeout(30.0)
# Паузы перед повтором при 429 и 5xx. Магазин режет частоту `appdetails` окном около 5 минут
# (по опыту сообщества, не замерено): последняя пауза перекрывает окно целиком, иначе все
# повторы упирались бы в тот же отказ (находка просмотра: 2 + 4 + 8 с не перекрывали ничего).
# Заголовок `Retry-After`, если магазин его прислал, старше этих чисел.
ПАУЗЫ = (5.0, 30.0, 300.0)

МЕСЯЦЫ = {
    "янв": 1, "фев": 2, "мар": 3, "апр": 4, "мая": 5, "май": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "окт": 10, "ноя": 11, "дек": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6, "jul": 7,
    "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}
_ДАТА = re.compile(r"^\s*(\d{1,2})\s+([A-Za-zА-Яа-яё]+)\.?,?\s+(\d{4})")
_ДАТА_АНГЛ = re.compile(r"^\s*([A-Za-z]+)\s+(\d{1,2}),\s+(\d{4})")
_НОМЕР = re.compile(r'data-ds-appid="([\d,]+)"')


class ОшибкаМагазина(Exception):
    """Магазин не ответил годно и после повторов."""


@dataclass(frozen=True)
class Метка:
    номер: int
    имя: str


@dataclass(frozen=True)
class Отзывы:
    сводка: str
    положительных: int
    всего: int

    def текст(self) -> str:
        """«Крайне положительные, 97 % из 862 216» либо «отзывов нет»."""
        if self.всего <= 0:
            return "отзывов нет"
        доля = round(100 * self.положительных / self.всего)
        всего = f"{self.всего:,}".replace(",", " ")
        return f"{self.сводка}, {доля} % из {всего}" if self.сводка else f"{доля} % из {всего}"


НЕТ_ОТЗЫВОВ = Отзывы("", 0, 0)


@dataclass(frozen=True)
class Игра:
    номер: int
    имя: str
    описание: str = ""
    жанры: tuple[str, ...] = ()
    дата_текст: str = ""
    дата: date | None = None
    вышла: bool = True
    # Цена — в копейках, как отдаёт магазин (`199900` — 1999 руб.). `None` — цены нет:
    # бесплатная игра либо игра, которая ещё не продаётся.
    цена: int | None = None
    цена_без_скидки: int | None = None
    скидка: int = 0
    валюта: str = ""
    бесплатна: bool = False
    отзывы: Отзывы = field(default=НЕТ_ОТЗЫВОВ)
    # Род записи магазина: `game`, `demo`, `dlc`, `music`… Выдача «скоро выйдет» полна
    # демоверсий — задания берут только `game`.
    род: str = "game"
    # Адрес обложки из `appdetails` (`header_image`): у новых игр путь с хешем, по номеру не угадать.
    обложка: str = ""

    @property
    def ссылка(self) -> str:
        return f"{МАГАЗИН}/app/{self.номер}/"


def разобрать_дату(текст: str) -> date | None:
    """Точная дата из строки магазина. «3 авг. 2023 г.», «3 Aug, 2023», «Aug 3, 2023» — дата;
    «I кв. 2027», «2027», «окт. 2026», «Скоро» — `None`: точного дня нет."""
    if m := _ДАТА.match(текст or ""):
        день, месяц, год = m.group(1), m.group(2), m.group(3)
    elif m := _ДАТА_АНГЛ.match(текст or ""):
        месяц, день, год = m.group(1), m.group(2), m.group(3)
    else:
        return None
    номер_месяца = МЕСЯЦЫ.get(месяц.casefold()[:3])
    if номер_месяца is None:
        return None
    try:
        return date(int(год), номер_месяца, int(день))
    except ValueError:
        return None


def разобрать_метки(данные: object) -> list[Метка]:
    if not isinstance(данные, list):
        raise ОшибкаМагазина("список меток магазина — не список")
    return [
        Метка(int(м["tagid"]), str(м["name"]))
        for м in данные
        if isinstance(м, dict) and "tagid" in м and "name" in м
    ]


def разобрать_поиск(данные: object) -> list[int]:
    """Номера игр выдачи поиска, без повторов, в порядке выдачи. У набора из нескольких игр
    атрибут несёт номера через запятую — берётся первый."""
    if not isinstance(данные, dict) or not isinstance(данные.get("results_html"), str):
        raise ОшибкаМагазина("ответ поиска без results_html")
    номера: list[int] = []
    for найдено in _НОМЕР.findall(данные["results_html"]):
        номер = int(найдено.split(",")[0])
        if номер not in номера:
            номера.append(номер)
    return номера


def разобрать_отзывы(данные: object) -> Отзывы:
    сводка = данные.get("query_summary") if isinstance(данные, dict) else None
    if not isinstance(сводка, dict):
        return НЕТ_ОТЗЫВОВ
    всего = int(сводка.get("total_reviews") or 0)
    return Отзывы(
        сводка=СВОДКИ.get(str(сводка.get("review_score_desc") or ""), str(сводка.get("review_score_desc") or "")),
        положительных=int(сводка.get("total_positive") or 0),
        всего=всего,
    )


# `appreviews` отдаёт сводку по-английски при любом языке запроса. Слова — те же, что на
# русской странице магазина.
СВОДКИ = {
    "Overwhelmingly Positive": "Крайне положительные",
    "Very Positive": "Очень положительные",
    "Positive": "Положительные",
    "Mostly Positive": "В основном положительные",
    "Mixed": "Смешанные",
    "Mostly Negative": "В основном отрицательные",
    "Negative": "Отрицательные",
    "Very Negative": "Очень отрицательные",
    "Overwhelmingly Negative": "Крайне отрицательные",
}


def разобрать_игру(номер: int, данные: object, отзывы: Отзывы = НЕТ_ОТЗЫВОВ) -> Игра | None:
    """Игра из ответа `appdetails`. `None` — магазин такой игры не знает (`success: false`).
    Ответ другого вида — `ОшибкаМагазина`: молча выдать его за «игры нет» значило бы потерять
    игру из желаемого без строки в ленте."""
    запись = данные.get(str(номер)) if isinstance(данные, dict) else None
    if not isinstance(запись, dict):
        raise ОшибкаМагазина(f"appdetails {номер}: ответ не того вида")
    if not запись.get("success"):
        return None
    д = запись.get("data")
    if not isinstance(д, dict):
        return None
    выход = д.get("release_date") if isinstance(д.get("release_date"), dict) else {}
    цена = д.get("price_overview") if isinstance(д.get("price_overview"), dict) else None
    дата_текст = str(выход.get("date") or "")
    return Игра(
        номер=номер,
        имя=str(д.get("name") or номер),
        описание=_без_разметки(str(д.get("short_description") or "")),
        жанры=tuple(str(ж.get("description")) for ж in д.get("genres") or [] if isinstance(ж, dict)),
        дата_текст=дата_текст,
        дата=разобрать_дату(дата_текст),
        вышла=not bool(выход.get("coming_soon")),
        цена=int(цена["final"]) if цена and "final" in цена else None,
        цена_без_скидки=int(цена["initial"]) if цена and "initial" in цена else None,
        скидка=int(цена.get("discount_percent") or 0) if цена else 0,
        валюта=str(цена.get("currency") or "") if цена else "",
        бесплатна=bool(д.get("is_free")),
        отзывы=отзывы,
        род=str(д.get("type") or "game"),
        обложка=str(д.get("header_image") or ""),
    )


def _без_разметки(текст: str) -> str:
    return " ".join(re.sub(r"<[^>]+>", " ", текст).split())


def клиент() -> httpx2.AsyncClient:
    return httpx2.AsyncClient(
        base_url=МАГАЗИН, timeout=ОЖИДАНИЕ, headers={"User-Agent": ПРЕДСТАВЛЕНИЕ}
    )


def _ждать(ответ: httpx2.Response, пауза: float | None) -> float | None:
    """Пауза из `Retry-After` (секунды), если она есть и попытки ещё остались."""
    if пауза is None:
        return None
    try:
        return max(float(ответ.headers.get("Retry-After", "")), 0.0)
    except ValueError:
        return пауза


async def _взять(к: httpx2.AsyncClient, путь: str, доводы: dict[str, str]) -> object:
    """Ответ JSON магазина. 429 и 5xx — повтор после паузы; исчерпаны — `ОшибкаМагазина`."""
    последняя = ""
    for пауза in (*ПАУЗЫ, None):
        try:
            ответ = await к.get(путь, params=доводы)
        except httpx2.HTTPError as exc:
            последняя = f"нет связи с магазином: {type(exc).__name__}"
        else:
            if ответ.status_code == 200:
                try:
                    return ответ.json()
                except (json.JSONDecodeError, ValueError):
                    raise ОшибкаМагазина(f"{путь}: магазин вернул не JSON") from None
            последняя = f"магазин ответил HTTP {ответ.status_code}"
            if ответ.status_code != 429 and ответ.status_code < 500:
                break
            пауза = _ждать(ответ, пауза)
        if пауза is None:
            break
        await asyncio.sleep(пауза)
    raise ОшибкаМагазина(f"{путь}: {последняя}")


async def метки(к: httpx2.AsyncClient) -> list[Метка]:
    return разобрать_метки(await _взять(к, "/tagdata/populartags/russian", {}))


# Длина страницы выдачи — умолчание магазина при `infinite=1`, её же сдвигает `start`.
ДЛИНА_СТРАНИЦЫ = 50


_СТРОКА = re.compile(r'data-ds-appid="([\d,]+)"')
_СВОДКА = re.compile(r'search_review_summary[^"]*"\s+data-tooltip-html="([^"]*)"')
_ДОЛЯ = re.compile(r"(\d+)%\D+([\d\s,.\u00a0]+)")


def разобрать_поиск_с_отзывами(данные: object) -> list[tuple[int, Отзывы]]:
    """Номера выдачи вместе со сводкой отзывов из подсказки выдачи. Сводка — прикидка для
    порядка кандидатов: магазин порой считает её по обзорам на языке запроса. Точную оценку по
    всем языкам задание берёт только для тех игр, что идут на суд. Нет сводки — «отзывов нет»."""
    if not isinstance(данные, dict) or not isinstance(данные.get("results_html"), str):
        raise ОшибкаМагазина("ответ поиска без results_html")
    html = данные["results_html"]
    начала = [(m.start(), int(m.group(1).split(",")[0])) for m in _СТРОКА.finditer(html)]
    итог: list[tuple[int, Отзывы]] = []
    виденные: set[int] = set()
    for i, (начало, номер) in enumerate(начала):
        if номер in виденные:
            continue
        виденные.add(номер)
        конец = начала[i + 1][0] if i + 1 < len(начала) else len(html)
        отзывы = НЕТ_ОТЗЫВОВ
        if сводка := _СВОДКА.search(html, начало, конец):
            текст = сводка.group(1).replace("&lt;", "<").replace("&gt;", ">")
            if доля := _ДОЛЯ.search(текст):
                всего = int(re.sub(r"\D", "", доля.group(2)) or 0)
                отзывы = Отзывы(текст.split("<")[0].strip(), round(всего * int(доля.group(1)) / 100), всего)
        итог.append((номер, отзывы))
    return итог


async def найти_с_отзывами(
    к: httpx2.AsyncClient, метка: int, *, будущие: bool, страница: int = 0
) -> list[tuple[int, Отзывы]]:
    """То же, что `найти`, но со сводкой отзывов каждой игры — без запроса на игру."""
    return разобрать_поиск_с_отзывами(await _взять(к, "/search/results/", _доводы_поиска(метка, будущие, страница)))


def _доводы_поиска(метка: int, будущие: bool, страница: int) -> dict[str, str]:
    # `category1=998` — только игры: демоверсии и дополнения отсеивает сам магазин.
    доводы = {
        **РЕГИОН, "tags": str(метка), "infinite": "1", "category1": "998",
        "start": str(ДЛИНА_СТРАНИЦЫ * страница),
    }
    if будущие:
        # Самые желанные будущие игры на все даты вперёд. `comingsoon` отдавал первой страницей
        # только ближайшие 3–4 дня (замер просмотра 2026-10-16).
        доводы["filter"] = "popularwishlist"
    return доводы


async def найти(
    к: httpx2.AsyncClient, метка: int, *, будущие: bool, страница: int = 0
) -> list[int]:
    """Одна страница выдачи по метке — 50 игр. Пустой список — выдача кончилась.

    Вышедшие — в порядке магазина по умолчанию, по соответствию метке. `sort_by=Reviews_DESC`
    поднимал малоизвестные игры с высокой долей отзывов, а умолчание даёт заметные (замер
    2026-10-16). Будущие — самые желанные (`filter=popularwishlist`)."""
    # `category1=998` — только игры: демоверсии и дополнения отсеивает сам магазин.
    доводы = {
        **РЕГИОН, "tags": str(метка), "infinite": "1", "category1": "998",
        "start": str(ДЛИНА_СТРАНИЦЫ * страница),
    }
    if будущие:
        # Самые желанные будущие игры на все даты вперёд. `comingsoon` отдавал первой страницей
        # только ближайшие 3–4 дня (замер просмотра 2026-10-16), и игры недели позже выходили
        # мимо календаря навсегда.
        доводы["filter"] = "popularwishlist"
    return разобрать_поиск(await _взять(к, "/search/results/", доводы))


def разобрать_поиск_по_названию(данные: object) -> list[tuple[int, str]]:
    """Пары «номер, имя» из `api/storesearch` в порядке выдачи. Выдача несёт и дополнения
    (`Baldur's Gate 3 - Digital Deluxe Edition DLC`): выбор оставлен модели, она видит имя."""
    if not isinstance(данные, dict) or not isinstance(данные.get("items"), list):
        raise ОшибкаМагазина("ответ поиска по названию без items")
    найдено: list[tuple[int, str]] = []
    for з in данные["items"]:
        if isinstance(з, dict) and "id" in з and "name" in з:
            найдено.append((int(з["id"]), str(з["name"])))
    return найдено


async def найти_по_названию(к: httpx2.AsyncClient, название: str) -> list[tuple[int, str]]:
    """Поиск магазина по названию. Лёгкие опечатки магазин прощает, не все: «baldurs gat 3»
    находит, «stardew valey» — нет. Исправляет написание модель, магазин подтверждает."""
    return разобрать_поиск_по_названию(
        await _взять(к, "/api/storesearch/", {**РЕГИОН, "term": название})
    )


def оценка(отзывы: Отзывы) -> float | None:
    """Оценка SteamDB от 0 до 1; отзывов нет — `None`.

    Магазин ранжирует по одной доле положительных, и 100 % из 20 встаёт выше 97 % из 862 тысяч.
    Формула тянет долю к 0,5 тем сильнее, чем меньше отзывов, и порога не требует."""
    if отзывы.всего <= 0:
        return None
    доля = отзывы.положительных / отзывы.всего
    return доля - (доля - 0.5) * 2 ** (-math.log10(отзывы.всего + 1))


async def отзывы(к: httpx2.AsyncClient, номер: int) -> Отзывы:
    данные = await _взять(
        к, f"/appreviews/{номер}",
        {"json": "1", "language": "all", "purchase_type": "all", "num_per_page": "0"},
    )
    return разобрать_отзывы(данные)


async def игра(к: httpx2.AsyncClient, номер: int, *, с_отзывами: bool = True) -> Игра | None:
    """Сведения об игре. `с_отзывами=False` — без запроса `appreviews`: задания берут сводку
    отзывов из выдачи поиска (решение пользователя 2026-10-16: «точная не нужна»), и на игру
    остаётся один запрос."""
    данные = await _взять(к, "/api/appdetails", {**РЕГИОН, "appids": str(номер)})
    найдена = разобрать_игру(номер, данные)
    if найдена is None or not с_отзывами:
        return найдена
    return разобрать_игру(номер, данные, await отзывы(к, номер))
