"""Профили генерации: системная инструкция + параметры запроса одним объектом.

Профиль хранится JSON-файлом, длинный текст системной инструкции выносится в соседний
`.md` (в JSON он превратился бы в одну строку с экранированными переносами — нечитаемо
ни в редакторе, ни в различиях между версиями).

Профили ищутся в нескольких местах, ближний перекрывает дальний:
  1. `$MYHARNESS_PROFILES`, если переменная задана;
  2. `profiles/` в каталоге запуска и выше по дереву (до домашней папки) — профили проекта:
     harness, запущенный из песочницы `day_2/test/`, подхватит `day_2/profiles/`;
  3. `~/.config/myharness/profiles` — личные;
  4. встроенный `default` — на случай, когда файлов нет вообще.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from string import Template
from typing import Any

from . import params as params_mod
from .config import CONFIG_DIR

USER_PROFILES_DIR = CONFIG_DIR / "profiles"
DEFAULT_PROFILE_NAME = "default"


@dataclass
class Profile:
    name: str
    description: str = ""
    system: str | None = None  # итоговый текст инструкции (подстановки уже выполнены)
    system_file: str | None = None
    keep_history: bool = True
    vars: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        """Слепок профиля для журнала — по нему прогон можно воспроизвести."""
        return {
            "name": self.name,
            "system": self.system,
            "keep_history": self.keep_history,
            "params": dict(self.params),
        }

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.description:
            data["description"] = self.description
        if self.system_file:
            data["system_file"] = self.system_file
        elif self.system is not None:
            data["system"] = self.system
        data["keep_history"] = self.keep_history
        if self.vars:
            data["vars"] = self.vars
        data.update(self.params)
        return data


def builtin_default() -> Profile:
    return Profile(name=DEFAULT_PROFILE_NAME, description="без ограничений: ничего не задаём, историю храним")


MAX_UPWARD_LEVELS = 5


def _upward_dirs(start: Path) -> list[Path]:
    """`profiles/` в каталоге запуска и у ближайших родителей — не выше домашней папки."""
    found: list[Path] = []
    current = start
    home = Path.home()
    for _ in range(MAX_UPWARD_LEVELS):
        found.append(current / "profiles")
        if current == home or current.parent == current:
            break
        current = current.parent
    return found


def search_dirs() -> list[Path]:
    dirs: list[Path] = []
    env_dir = os.environ.get("MYHARNESS_PROFILES")
    if env_dir:
        dirs.append(Path(env_dir).expanduser())
    try:
        dirs.extend(_upward_dirs(Path.cwd().resolve()))
    except OSError:  # каталог запуска удалён из-под нас
        pass
    dirs.append(USER_PROFILES_DIR)
    seen: set[Path] = set()
    unique: list[Path] = []
    for d in dirs:
        resolved = d.expanduser()
        if resolved not in seen:
            seen.add(resolved)
            unique.append(resolved)
    return unique


def available() -> list[tuple[str, Path | None]]:
    """Имена профилей с источником; ближний каталог перекрывает дальний."""
    found: dict[str, Path] = {}
    for directory in reversed(search_dirs()):  # дальние первыми, ближние затирают
        if not directory.is_dir():
            continue
        for path in sorted(directory.glob("*.json")):
            found[path.stem] = path
    items: list[tuple[str, Path | None]] = sorted(found.items())
    if DEFAULT_PROFILE_NAME not in found:
        items.insert(0, (DEFAULT_PROFILE_NAME, None))
    return items


def _from_dict(data: dict[str, Any], name: str, base_dir: Path, source: Path | None) -> tuple[Profile, list[str]]:
    warnings: list[str] = []
    known_meta = {"name", "description", "system", "system_file", "keep_history", "vars"}

    system_text: str | None = data.get("system")
    system_file = data.get("system_file")
    if system_file:
        path = (base_dir / system_file).expanduser()
        try:
            system_text = path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"не удалось прочитать {path.name}: {exc}")
            system_text = None

    variables = data.get("vars") or {}
    if system_text and variables:
        # safe_substitute по $имя: фигурные скобки примера JSON внутри инструкции не трогаются
        system_text = Template(system_text).safe_substitute(variables)

    collected: dict[str, Any] = {}
    for key, value in data.items():
        if key in known_meta:
            continue
        if key in params_mod.SPECS:
            collected[key] = value
        else:
            warnings.append(f"неизвестный параметр «{key}» — пропущен")

    profile = Profile(
        name=data.get("name") or name,
        description=data.get("description", ""),
        system=system_text.strip() if isinstance(system_text, str) else None,
        system_file=system_file,
        keep_history=bool(data.get("keep_history", True)),
        vars=dict(variables),
        params=collected,
        source=source,
    )
    return profile, warnings


def load(name: str) -> tuple[Profile, list[str]]:
    """Профиль по имени. Если файла нет, а имя — default, отдаём встроенный."""
    for directory in search_dirs():
        path = directory / f"{name}.json"
        if not path.is_file():
            continue
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            return builtin_default(), [f"профиль «{name}» испорчен ({exc}) — взят default"]
        if not isinstance(data, dict):
            return builtin_default(), [f"профиль «{name}»: ожидался объект JSON — взят default"]
        return _from_dict(data, name, path.parent, path)
    if name == DEFAULT_PROFILE_NAME:
        return builtin_default(), []
    return builtin_default(), [f"профиль «{name}» не найден — взят default"]


def save(profile: Profile) -> Path:
    USER_PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    path = USER_PROFILES_DIR / f"{profile.name}.json"
    path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
