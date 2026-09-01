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

DEFAULT_MODEL = "deepseek-v4-flash"
DEFAULT_PROFILE = "default"


def config_dir() -> Path:
    """Каталог настроек. Переопределяется `$MYHARNESS_CONFIG_DIR` — это нужно проверкам:
    без переопределения они писали бы в настоящий файл пользователя и затирали его ключ."""
    override = os.environ.get("MYHARNESS_CONFIG_DIR")
    return Path(override).expanduser() if override else Path.home() / ".config" / "myharness"


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class Config:
    api_key: str | None = None
    model: str = DEFAULT_MODEL
    profile: str = DEFAULT_PROFILE

    @property
    def is_authorized(self) -> bool:
        return bool(self.api_key)


def load() -> Config:
    path = config_path()
    if not path.exists():
        return Config()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return Config()
    return Config(
        api_key=data.get("api_key"),
        model=data.get("model", DEFAULT_MODEL),
        profile=data.get("profile", DEFAULT_PROFILE),
    )


def save(config: Config) -> None:
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(config), ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(path, 0o600)
