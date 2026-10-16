"""Советник `игровой-советник`: агент `myharness` на DeepSeek — опрос вкуса и пересмотр после минуса.

Устройство повторяет веб-чат дня 33: `Agent` профиля, разговор `memory` по паре «каталог агента
и профиль», инструменты — серверы MCP из `.mcp.json` каталога агента плюс `save_taste`. Карточка
и инварианты читаются по каталогу агента на каждую сборку запроса — человек нажимает минус
между двумя репликами.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

from myharness import api, config, invariants, memory, profiles, project_card
from myharness.agent import Agent, соединить
from myharness.mcp_tools import Соединения

import taste
from steam_store import Игра, Метка

ПРОФИЛЬ = "игровой-советник"
СЕРВЕР = Path(__file__).resolve().parents[2] / "mcp_servers" / "steam"
# Только чтение: писать модели не дано ничего, кроме вкуса через `save_taste`.
РАЗРЕШЕНИЯ = [
    f"mcp__steam__{имя}"
    for имя in (
        "steam_tags", "steam_search", "steam_game", "steam_find", "list_library",
        "list_suggestions", "list_wishlist", "list_calendar", "list_feed",
    )
]


def модель() -> str:
    return config.DEFAULT_MODEL


def подготовить_каталог(каталог: Path) -> None:
    """`.mcp.json` и разрешения каталога агента. Пишутся при каждом запуске заново: путь к
    серверу и каталогу данных следуют за кодом, а не за прошлой установкой."""
    (каталог / ".claude").mkdir(parents=True, exist_ok=True)
    mcp = {
        "mcpServers": {
            "steam": {
                "type": "stdio",
                "command": "uv",
                "args": ["run", "--project", str(СЕРВЕР), str(СЕРВЕР / "server.py")],
                "env": {"GAMES_DATA_DIR": str(каталог)},
            }
        }
    }
    (каталог / ".mcp.json").write_text(json.dumps(mcp, ensure_ascii=False, indent=1), "utf-8")
    (каталог / ".claude" / "settings.json").write_text(
        json.dumps({"permissions": {"allow": РАЗРЕШЕНИЯ}}, ensure_ascii=False, indent=1), "utf-8"
    )


def профиль() -> profiles.Profile:
    найден, предупреждения = profiles.load(ПРОФИЛЬ)
    if найден.source is None:
        raise RuntimeError(f"профиль {ПРОФИЛЬ} не загружен: {'; '.join(предупреждения)}")
    return найден


def _агент(
    каталог: Path, профиль_советника: profiles.Profile, метки: list[Метка], соединения: Соединения | None
) -> Agent:
    def карточка() -> str:
        найдена, жалобы = project_card.load(каталог)
        if найдена is None and жалобы:
            raise RuntimeError("; ".join(жалобы))
        return project_card.блок(найдена) if найдена is not None else ""

    def блок_инвариантов() -> str:
        список, _ = invariants.собрать(каталог, None, профиль_советника)
        return invariants.блок(список)

    async def набор():
        mcp = await соединения.набор(каталог) if соединения is not None else None
        return соединить(taste.набор(каталог, метки), mcp)

    return Agent(
        ПРОФИЛЬ, профиль_советника, project=карточка, инварианты=блок_инвариантов, инструменты=набор
    )


def поднять(
    каталог: Path, соединения: Соединения, метки: list[Метка], *, новый: bool = False
) -> tuple[Agent, list[tuple[str, str]]]:
    """Агент разговора и прежние пары (пусто при `новый`). Разговор переживает перезапуск."""
    профиль_советника = профиль()
    агент = _агент(каталог, профиль_советника, метки, соединения)
    путь = None if новый else memory.latest_session(каталог, ПРОФИЛЬ)
    пары: list[tuple[str, str]] = []
    if путь is not None:
        прочитано = memory.read_session(
            путь, window=0, system_fp=memory.fingerprint(профиль_советника.system)
        )
        пары = прочитано.pairs
        агент.restore(пары, прочитано.приложения)
    else:
        путь = memory.new_session(каталог, ПРОФИЛЬ)
    хранилище = memory.SessionStore(путь)
    if not путь.exists():
        # Пустой файл сразу: «Пересобрать вкус» без единой реплики иначе не пережил бы
        # перезапуск — `latest_session` нашёл бы прежний разговор (так же в дне 33).
        if ошибка := хранилище.touch():
            raise RuntimeError(ошибка)
    агент.set_store(хранилище)
    return агент, пары


def _подпись(каталог: Path) -> str:
    карточка, _ = project_card.load(каталог)
    return карточка.заполнена if карточка is not None else ""


ПОВОДЫ = {
    "минус": "Человек отверг предложенную игру",
    "уже играл: понравилась": "Человек уже играл в эту игру вне Steam, и она ему понравилась",
    "уже играл: не понравилась": "Человек уже играл в эту игру вне Steam, и она ему не понравилась",
}


async def пересмотреть(
    клиент: api.Clients, каталог: Path, игра: Игра, причина: str, метки: list[Метка],
    род: str = "минус",
) -> tuple[bool, str]:
    """Пересмотр вкуса: отдельный агент без разговора, один обмен.

    Род — повод из `ПОВОДЫ`. Возвращает (вкус записан, ответ модели). Записан ли вкус, видно по
    отметке времени карточки, а не по словам модели: модель может сказать «поправил» и не позвать
    `save_taste`."""
    до = _подпись(каталог)
    агент = _агент(каталог, профиль(), метки, None)
    жанры = ", ".join(игра.жанры) or "не указаны"
    текст = (
        f"{ПОВОДЫ[род]}: «{игра.имя}» (номер Steam {игра.номер}; жанры: {жанры}; "
        f"оценка пользователей Steam: {игра.отзывы.текст()}). "
        f"Причина: {причина.strip() or 'не названа'}. Пересмотри вкус по разделу «Пересмотр вкуса»."
    )
    ход = await агент.exchange(клиент, модель(), текст)
    if not ход.ok:
        return False, f"пересмотр не удался: {ход.error or ход.status}"
    return _подпись(каталог) != до, ход.text


ПРИВЕТСТВИЕ_СВЕРКИ = (
    "Пора сверить вкус: прошло полгода. Посмотри мою библиотеку (часы за это время) и мой "
    "нынешний вкус, предложи, что убрать из любимых и что добавить, и спроси меня."
)


if __name__ == "__main__":
    print("модуль советника; запуск — через web.py", file=sys.stderr)
