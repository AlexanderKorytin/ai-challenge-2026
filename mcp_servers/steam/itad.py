"""Исторический минимум цены игры по IsThereAnyDeal (API v2, `docs.isthereanydeal.com`).

Два запроса: `games/lookup/v1` переводит номер Steam в номер игры IsThereAnyDeal, затем
`games/storelow/v2` отдаёт самую низкую цену за всю историю в магазине Steam. Минимум других
магазинов не берётся (решение ведущего 2026-10-16): уведомление сравнивает цену Steam с
минимумом Steam, иначе «минимум был 749 руб.» мог бы оказаться ценой ключа у перекупщика.

Ключ едет заголовком `ITAD-API-Key`, а не доводом `key` (документация разрешает оба): заголовка
нет ни в адресе, ни в тексте ошибок `httpx2`, ни в его журнале.

Страна — `RU`. Документация списка стран не даёт; `service/shops/v1?country=RU` отвечает списком
магазинов со Steam, а неизвестную страну (`XX`) отвергает с HTTP 400 (замер 2026-10-16). В какой
валюте придёт минимум для `RU`, не проверено без ключа: валюту несёт сам ответ.

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

from dataclasses import dataclass

import httpx2

API = "https://api.isthereanydeal.com"
СТРАНА = "RU"
# Номер магазина Steam у IsThereAnyDeal — по примеру `storelow.v2.success` документации.
МАГАЗИН_STEAM = 61


class ОшибкаITAD(Exception):
    """IsThereAnyDeal не ответил годно. Текст не содержит ключа."""


class КлючНеПринят(ОшибкаITAD):
    """IsThereAnyDeal отверг ключ. Живой замер 2026-10-16: неверный ключ — HTTP 403,
    `{"reason_phrase": "Invalid or expired api key"}`; 401 принимается так же."""


@dataclass(frozen=True)
class Минимум:
    # Цена — в мелких долях валюты (`amountInt`): для RUB — копейки, для USD — центы.
    цена: int
    валюта: str
    # Когда цена была самой низкой — строка ISO 8601 как в ответе: `2022-12-21T00:29:04+01:00`.
    когда: str


def разобрать_поиск(данные: object) -> str | None:
    """Номер игры IsThereAnyDeal из ответа `games/lookup/v1`; `None` — игра не найдена."""
    if not isinstance(данные, dict):
        raise ОшибкаITAD("lookup: ответ не того вида")
    игра = данные.get("game")
    if not данные.get("found") or not isinstance(игра, dict) or not isinstance(игра.get("id"), str):
        return None
    return игра["id"]


def разобрать_минимум(данные: object, номер: str) -> Минимум | None:
    """Минимум Steam для игры `номер` из ответа `games/storelow/v2`; `None` — истории цен в
    Steam нет. Магазин сверяется и здесь, хотя запрос просит только Steam: чужой магазин в
    ответе не должен стать «минимумом Steam» молча."""
    if not isinstance(данные, list):
        raise ОшибкаITAD("storelow: ответ не того вида")
    for запись in данные:
        if not isinstance(запись, dict) or запись.get("id") != номер:
            continue
        for низ in запись.get("lows") or []:
            if not isinstance(низ, dict):
                continue
            магазин = низ.get("shop")
            if not isinstance(магазин, dict) or магазин.get("id") != МАГАЗИН_STEAM:
                continue
            цена = низ.get("price")
            if not isinstance(цена, dict) or not isinstance(цена.get("amountInt"), int):
                return None
            return Минимум(int(цена["amountInt"]), str(цена.get("currency") or ""), str(низ.get("timestamp") or ""))
    return None


async def _запрос(к: httpx2.AsyncClient, метод: str, путь: str, ключ: str, **доводы) -> object:
    причина = ""
    try:
        ответ = await к.request(метод, f"{API}{путь}", headers={"ITAD-API-Key": ключ}, **доводы)
    except httpx2.HTTPError as exc:
        причина = type(exc).__name__
    # Вне `except`: исключение `httpx2` не должно остаться в `__context__` (в нём адрес запроса
    # и, при доводе `key`, ключ).
    if причина:
        raise ОшибкаITAD(f"{путь}: нет связи с IsThereAnyDeal ({причина})")
    if ответ.status_code in (401, 403):
        raise КлючНеПринят(f"{путь}: ключ IsThereAnyDeal не принят (HTTP {ответ.status_code})")
    if ответ.status_code != 200:
        raise ОшибкаITAD(f"{путь}: IsThereAnyDeal ответил HTTP {ответ.status_code}")
    try:
        return ответ.json()
    except ValueError:
        pass
    raise ОшибкаITAD(f"{путь}: IsThereAnyDeal вернул не JSON")


async def минимум(к: httpx2.AsyncClient, ключ: str, номер_steam: int) -> Минимум | None:
    """Исторический минимум игры Steam. Нет ключа, игра не найдена, истории нет — `None`."""
    if not ключ:
        return None
    # Заголовок HTTP — только ASCII. Иначе `httpx2` упал бы при сборке запроса исключением,
    # которое держит сам ключ.
    if not ключ.isascii():
        raise КлючНеПринят("ключ IsThereAnyDeal не принят: в нём знаки вне ASCII")
    номер = разобрать_поиск(await _запрос(к, "GET", "/games/lookup/v1", ключ, params={"appid": str(номер_steam)}))
    if номер is None:
        return None
    данные = await _запрос(
        к, "POST", "/games/storelow/v2", ключ,
        params={"country": СТРАНА, "shops": str(МАГАЗИН_STEAM)}, json=[номер],
    )
    return разобрать_минимум(данные, номер)
