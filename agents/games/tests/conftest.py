"""Общая фикстура проверок: все пути записи — во временный каталог.

Правило проекта: проверки однажды писали в настоящий файл настроек и затёрли ключ. Поэтому
каталог состояния и настроек `myharness` и каталог данных агента уводятся в `tmp_path` у
КАЖДОЙ проверки, а не только у тех, что пишут."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def каталоги(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("MYHARNESS_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("MYHARNESS_CONFIG_DIR", str(tmp_path / "config"))
    # Файл ключей агента (`secrets_store`): без увода проверки веба писали бы настоящие ключи.
    monkeypatch.setenv("GAMES_CONFIG_DIR", str(tmp_path / "games-config"))
    # Журнал прогонов `myharness`: `agent_env` при ввозе ставит ему настоящий путь агента.
    monkeypatch.setenv("MYHARNESS_JOURNAL", str(tmp_path / "journal.jsonl"))
    данные = tmp_path / "games"
    данные.mkdir()
    monkeypatch.setenv("GAMES_DATA_DIR", str(данные))
    return данные
