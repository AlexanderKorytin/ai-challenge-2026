"""Хранение настроек harness между запусками: API-ключ, модель и имя активного профиля.

Здесь лежит секрет, поэтому файл держим правами 600 и ничего лишнего в него не кладём.
Сами профили (системная инструкция и параметры) хранятся отдельными файлами — их можно
показывать на экране и фиксировать в git, ключ рядом с ними оказаться не должен.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "myharness"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROFILE = "default"


@dataclass
class Config:
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    profile: str = DEFAULT_PROFILE

    @property
    def is_authorized(self) -> bool:
        return bool(self.api_key)


def load() -> Config:
    if not CONFIG_PATH.exists():
        return Config()
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Config()
    return Config(
        api_key=data.get("api_key"),
        model=data.get("model", DEFAULT_MODEL),
        profile=data.get("profile", DEFAULT_PROFILE),
    )


def save(config: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
