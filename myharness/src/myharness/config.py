"""Хранение настроек harness между запусками: API-ключ, модель и имя активного профиля.

Здесь лежит секрет, поэтому файл держим правами 600 и ничего лишнего в него не кладём.
Сами профили (системная инструкция и параметры) хранятся отдельными файлами — их можно
показывать на экране и фиксировать в git, ключ рядом с ними оказаться не должен.
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

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
    # Собирать ли факты о пользователе из разговора (агент-архивариус). Выключатель лежит
    # ЗДЕСЬ, а не в профиле, и это решение, а не удобство: профиль отвечает на вопрос «кем
    # сейчас работает модель», а память — про самого пользователя и его рабочую папку, к
    # личности модели отношения не имеет. Заведи мы поле профиля — память пришлось бы
    # включать в каждом профиле заново, и в половине из них её бы забыли.
    #
    # Умолчание — включено: память свойство инструмента, а не то, что каждый раз включают.
    remember: bool = True
    # Папки, которым человек доверил чтение и правку файлов (способность `files`, «Доверие
    # папке»). Не тайна: пути, а не ключи. Список только растёт — команды удаления нет.
    trusted_dirs: list[str] = field(default_factory=list)
    # Поля на миг `load` — по ним `save` отличает свою правку от чужой (см. `save`).
    _исходное: dict | None = field(default=None, repr=False, compare=False)

    @property
    def is_authorized(self) -> bool:
        return bool(self.api_key)


def _flag(value: Any, default: bool) -> bool:
    """Признак из файла настроек. Не `bool` в файле — берём умолчание.

    Файл настроек человек правит руками, и опечатка в нём не имеет права уронить инструмент
    на старте: упавшему harness нечем даже показать, что именно не так. Правило то же, что
    у остальных полей — испорченное значение равносильно отсутствующему."""
    return value if isinstance(value, bool) else default


_ПОЛЯ = ("api_key", "model", "profile", "remember")


def _прочитать() -> dict | None:
    """Объект файла настроек; `None` — файла нет либо он не читается как объект JSON.

    Одно определение «испорчен» на весь модуль: `[]` и неверный UTF-8 иначе роняли бы запуск
    ещё до окна (вопрос о доверии читает настройки первым)."""
    path = config_path()
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _из_данных(data: dict) -> Config:
    return Config(
        api_key=data.get("api_key"),
        model=data.get("model", DEFAULT_MODEL),
        profile=data.get("profile", DEFAULT_PROFILE),
        remember=_flag(data.get("remember"), True),
        trusted_dirs=_пути(data.get("trusted_dirs")),
    )


def _снимок(config: Config) -> dict:
    return {поле: getattr(config, поле) for поле in _ПОЛЯ}


def load() -> Config:
    data = _прочитать()
    config = _из_данных(data) if data is not None else Config()
    config._исходное = _снимок(config)
    return config


def _пути(value: Any) -> list[str]:
    return [п for п in value if isinstance(п, str)] if isinstance(value, list) else []


def испорчен() -> bool:
    """Файл настроек есть, но не читается как объект JSON."""
    return config_path().exists() and _прочитать() is None


def save(config: Config) -> None:
    """Записать настройки, не стирая чужих изменений.

    Второе окно держит в памяти настройки на миг своего запуска. Пиши оно их целиком — его
    `/model` вернул бы в файл старый ключ после `/auth` в первом окне и стёр бы папку, которой
    доверили там. Поэтому поле, которое это окно не меняло с `load`, берётся из файла, а список
    доверия объединяется. Запись — во временный файл с правами 600 и `os.replace`: читатель не
    увидит обрезанного файла."""
    path = config_path()
    data = _прочитать()
    исходное = config._исходное
    if data is not None:
        на_диске = _из_данных(data)
        for поле in _ПОЛЯ:
            if исходное is not None and getattr(config, поле) == исходное[поле]:
                setattr(config, поле, getattr(на_диске, поле))
        config.trusted_dirs = на_диске.trusted_dirs + [
            п for п in config.trusted_dirs if п not in на_диске.trusted_dirs
        ]
    path.parent.mkdir(parents=True, exist_ok=True)
    текст = json.dumps({**_снимок(config), "trusted_dirs": config.trusted_dirs}, ensure_ascii=False, indent=2)
    # Символическая ссылка — пишем в её цель: `os.replace` иначе подменил бы саму ссылку.
    path = path.resolve()
    if path.exists() and path.stat().st_nlink > 1:
        # Жёсткая ссылка — на месте: новый узел оставил бы ключ в старом, и сверка по тождеству
        # (способность `files`) перестала бы узнавать ссылку в рабочем каталоге.
        дескриптор = os.open(path, os.O_WRONLY | os.O_TRUNC)
        with os.fdopen(дескриптор, "w", encoding="utf-8") as файл:
            файл.write(текст)
        os.chmod(path, 0o600)
        config._исходное = _снимок(config)
        return
    временный = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    дескриптор = os.open(временный, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(дескриптор, "w", encoding="utf-8") as файл:
            файл.write(текст)
        os.chmod(временный, 0o600)
        os.replace(временный, path)
    except BaseException:
        временный.unlink(missing_ok=True)
        raise
    config._исходное = _снимок(config)
