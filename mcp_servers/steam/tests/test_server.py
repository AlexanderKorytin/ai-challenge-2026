"""Сервер настоящим клиентом MCP в памяти: схема инструментов и чтение пустого хранилища."""

import asyncio

from mcp import Client

import server

ИМЕНА = {
    "steam_tags", "steam_search", "steam_game",
    "list_suggestions", "list_wishlist", "list_calendar", "list_feed",
}


def test_семь_инструментов_и_пустое_хранилище(tmp_path, monkeypatch):
    monkeypatch.setenv("GAMES_DATA_DIR", str(tmp_path))

    async def прогон():
        async with Client(server.srv) as к:
            имена = {и.name for и in (await к.list_tools()).tools}
            assert имена == ИМЕНА
            итог = await к.call_tool("list_wishlist", {})
            assert not итог.is_error
            assert итог.structured_content == {"желаемое": []}

    asyncio.run(прогон())
    # Сервер только читает: пустое хранилище он не заводит.
    assert not (tmp_path / "games.sqlite").exists()
