"""Учётная запись Steam: вход OpenID 2.0, библиотека, список желаемого.

Вход — кнопка «Войти через Steam», как у сторонних сайтов. Человек вводит пароль на странице
Steam; агент получает только ответ OpenID со SteamID и проверяет его у самого Steam
(`check_authentication`). Подделка ответа — `None`; Steam не ответил — исключение: человек должен
различать «вход отклонён» и «Steam недоступен».

Библиотеку и список желаемого отдаёт Steam Web API по ключу человека. Ключ едет в адресе запроса
(`?key=…`), а `httpx2` кладёт адрес в текст своих ошибок и в свой журнал. Поэтому ошибки связи
заменяются своими без адреса, а журнал `httpx2` поднят до `WARNING` (см. ниже).

Состояния у модуля нет, на диск он не пишет.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from urllib.parse import urlencode, urlsplit

import httpx2

ВХОД = "https://steamcommunity.com/openid/login"
ПРОСТРАНСТВО = "http://specs.openid.net/auth/2.0"
ВЫБОР_ЛИЧНОСТИ = "http://specs.openid.net/auth/2.0/identifier_select"
ВЕБ_API = "https://api.steampowered.com"
# `fullmatch` и `\Z` вместо `$`: `$` пропускает перевод строки в конце, а `claimed_id` пришёл
# от человека и может быть любым.
_ЛИЧНОСТЬ = re.compile(r"https://steamcommunity\.com/openid/id/(\d{17})\Z")
# Поля, без подписи которых ответ не годен. `check_authentication` проверяет подпись только у
# полей из `openid.signed`; поле вне списка можно подменить, и Steam всё равно скажет
# `is_valid:true`.
# `response_nonce` — тоже: на нём держится защита веба от повтора ответа (разбор рисков).
_ПОДПИСАННЫЕ = ("op_endpoint", "claimed_id", "identity", "return_to", "response_nonce")

# Журнал `httpx2` пишет полный адрес запроса на уровне `INFO`, а в адресе — ключ Steam. Любая
# служба, включившая `INFO` у корневого журнала, иначе сложила бы ключ в файл журнала.
logging.getLogger("httpx2").setLevel(logging.WARNING)


class ОшибкаSteam(Exception):
    """Steam не ответил годно. Текст не содержит ни ключа, ни адреса запроса."""


class КлючНеПринят(ОшибкаSteam):
    """Steam отверг ключ Web API (HTTP 401 или 403)."""


class БиблиотекаЗакрыта(ОшибкаSteam):
    """Steam ответил пустым `response`: настройки приватности прячут игры."""


@dataclass(frozen=True)
class Своя:
    номер: int
    имя: str
    часы: float
    часы_2нед: float
    # Время последнего запуска — секунды эпохи Unix, как отдаёт Steam. `None` — не запускал
    # ни разу (Steam пишет 0).
    последний_запуск: int | None


def адрес_входа(возврат: str) -> str:
    """Адрес страницы входа Steam. `realm` — схема, хост и порт возврата: Steam показывает его
    человеку как имя сайта, и `return_to` обязан лежать внутри него."""
    части = urlsplit(возврат)
    доводы = {
        "openid.ns": ПРОСТРАНСТВО,
        "openid.mode": "checkid_setup",
        "openid.return_to": возврат,
        "openid.realm": f"{части.scheme}://{части.netloc}/",
        "openid.identity": ВЫБОР_ЛИЧНОСТИ,
        "openid.claimed_id": ВЫБОР_ЛИЧНОСТИ,
    }
    return f"{ВХОД}?{urlencode(доводы)}"


def _годен_по_виду(доводы: dict[str, str], возврат: str) -> str | None:
    """SteamID, если ответ годен по виду; иначе `None`. Сеть не нужна."""
    if доводы.get("openid.ns") != ПРОСТРАНСТВО or доводы.get("openid.mode") != "id_res":
        return None
    # Сравнение строгое, со строкой запроса: Steam возвращает `return_to` ровно таким, каким
    # получил. Ответ, выданный Steam для чужого сайта или другого пути, сюда не подходит, хотя
    # его подпись Steam и подтвердит.
    if доводы.get("openid.return_to") != возврат:
        return None
    if доводы.get("openid.op_endpoint") != ВХОД:
        return None
    личность = доводы.get("openid.claimed_id", "")
    if личность != доводы.get("openid.identity"):
        return None
    if not (найдено := _ЛИЧНОСТЬ.fullmatch(личность)):
        return None
    подписаны = доводы.get("openid.signed", "").split(",")
    if any(поле not in подписаны for поле in _ПОДПИСАННЫЕ):
        return None
    return найдено.group(1)


async def проверить_вход(к: httpx2.AsyncClient, доводы: dict[str, str], возврат: str) -> str | None:
    """SteamID из ответа OpenID либо `None`, если ответ подделан или не того вида.

    Ответ не годный по виду отвергается без запроса к Steam: проверять подпись у заведомо чужого
    ответа незачем. Годный по виду уходит к Steam целиком (`openid.*`) с
    `openid.mode=check_authentication`; Steam отвечает `is_valid:true` только на свою подпись."""
    steamid = _годен_по_виду(доводы, возврат)
    if steamid is None:
        return None
    тело = {ключ: значение for ключ, значение in доводы.items() if ключ.startswith("openid.")}
    тело["openid.mode"] = "check_authentication"
    причина = ""
    try:
        ответ = await к.post(ВХОД, data=тело)
    except httpx2.HTTPError as exc:
        причина = type(exc).__name__
    # Исключение поднимается вне `except`: иначе ошибка `httpx2` с адресом осталась бы в
    # `__context__` и попала бы в напечатанный след.
    if причина:
        raise ОшибкаSteam(f"Steam не ответил на проверку входа: {причина}")
    if ответ.status_code != 200:
        raise ОшибкаSteam(f"Steam ответил на проверку входа HTTP {ответ.status_code}")
    # Ответ — строки «ключ:значение» (OpenID 2.0, п. 4.1.1). Ищется целая строка: подстрока
    # нашлась бы и в `is_valid:true_not`.
    строки = [с.strip() for с in ответ.text.splitlines()]
    return steamid if "is_valid:true" in строки else None


async def _взять(к: httpx2.AsyncClient, путь: str, доводы: dict[str, str], что: str) -> dict:
    """`response` ответа Web API. Ошибки — `ОшибкаSteam` без адреса: в адресе ключ."""
    причина = ""
    try:
        ответ = await к.get(f"{ВЕБ_API}{путь}", params=доводы)
    except httpx2.HTTPError as exc:
        причина = type(exc).__name__
    if причина:
        raise ОшибкаSteam(f"{что}: нет связи со Steam ({причина})")
    if ответ.status_code in (401, 403):
        raise КлючНеПринят(f"{что}: ключ Steam не принят (HTTP {ответ.status_code})")
    if ответ.status_code != 200:
        raise ОшибкаSteam(f"{что}: Steam ответил HTTP {ответ.status_code}")
    try:
        данные = ответ.json()
    except ValueError:
        данные = None
    тело = данные.get("response") if isinstance(данные, dict) else None
    if not isinstance(тело, dict):
        raise ОшибкаSteam(f"{что}: ответ Steam не того вида")
    return тело


ПОДСКАЗКА_ПРИВАТНОСТИ = (
    "Steam не отдал игры: их прячут настройки приватности. Откройте профиль Steam → "
    "«Редактировать профиль» → «Приватность» и поставьте «Сведения об играх»: «Открытый»."
)


async def библиотека(к: httpx2.AsyncClient, ключ: str, steamid: str) -> list[Своя]:
    """Игры учётной записи с часами игры, в порядке ответа Steam."""
    тело = await _взять(к, "/IPlayerService/GetOwnedGames/v1/", {
        "key": ключ, "steamid": steamid, "include_appinfo": "1",
        "include_played_free_games": "1", "format": "json",
    }, "библиотека")
    # Пустой `response` — так Steam отвечает на закрытые «Сведения об играх». Пустая открытая
    # библиотека приходит иначе: `game_count: 0`.
    if not тело:
        raise БиблиотекаЗакрыта(ПОДСКАЗКА_ПРИВАТНОСТИ)
    игры: list[Своя] = []
    for и in тело.get("games") or []:
        if not isinstance(и, dict) or "appid" not in и:
            continue
        запуск = int(и.get("rtime_last_played") or 0)
        игры.append(Своя(
            номер=int(и["appid"]),
            имя=str(и.get("name") or и["appid"]),
            часы=int(и.get("playtime_forever") or 0) / 60,
            часы_2нед=int(и.get("playtime_2weeks") or 0) / 60,
            последний_запуск=запуск or None,
        ))
    return игры


async def список_желаемого(к: httpx2.AsyncClient, steamid: str, ключ: str) -> list[int]:
    """Номера игр списка желаемого Steam в порядке, который задал человек (`priority`).

    Ключ не обязателен: открытый список Steam отдаёт и без него. Пустой и закрытый список
    приходят одинаково — `{"response": {}}` (замер 2026-10-16), различить их нельзя."""
    доводы = {"steamid": steamid}
    if ключ:
        доводы["key"] = ключ
    тело = await _взять(к, "/IWishlistService/GetWishlist/v1/", доводы, "список желаемого")
    записи = [з for з in тело.get("items") or [] if isinstance(з, dict) and "appid" in з]
    # `sorted` устойчив: при равном `priority` остаётся порядок ответа.
    записи = sorted(записи, key=lambda з: int(з.get("priority") or 0))
    return [int(з["appid"]) for з in записи]
