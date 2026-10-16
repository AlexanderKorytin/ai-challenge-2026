import json

import advisor


def test_каталог_агента_разрешения_и_сервер(tmp_path):
    advisor.подготовить_каталог(tmp_path)
    mcp = json.loads((tmp_path / ".mcp.json").read_text("utf-8"))
    assert mcp["mcpServers"]["steam"]["env"]["GAMES_DATA_DIR"] == str(tmp_path)
    разрешено = json.loads((tmp_path / ".claude" / "settings.json").read_text("utf-8"))["permissions"]["allow"]
    assert "mcp__steam__steam_find" in разрешено and "mcp__steam__list_library" in разрешено
    # Писать модели не дано ничего: в разрешениях только чтение.
    assert all(р.split("__")[-1].startswith(("steam_", "list_")) for р in разрешено)
