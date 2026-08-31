"""Хранение настроек harness между запусками: API-ключ и модель по умолчанию."""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from pathlib import Path

CONFIG_DIR = Path.home() / ".config" / "myharness"
CONFIG_PATH = CONFIG_DIR / "config.json"

DEFAULT_MODEL = "deepseek-v4-flash"


@dataclass
class Config:
    api_key: str | None = None
    model: str = DEFAULT_MODEL

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
    return Config(api_key=data.get("api_key"), model=data.get("model", DEFAULT_MODEL))


def save(config: Config) -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(CONFIG_PATH, 0o600)
