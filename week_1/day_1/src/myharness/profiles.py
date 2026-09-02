"""Профили генерации: системная инструкция + параметры запроса одним объектом.

Профиль хранится JSON-файлом, длинный текст системной инструкции выносится в соседний
`.md` (в JSON он превратился бы в одну строку с экранированными переносами — нечитаемо
ни в редакторе, ни в различиях между версиями).

Кроме инструкции профиль может нести заготовку ввода (`prefill` / `prefill_file`): при выборе
профиля она подставляется в строку ввода. Нужна там, где вопрос к модели — часть самого
приёма и всегда один и тот же: например «составь промпт для решения такой-то задачи».
Заготовку видно до отправки, её можно поправить или стереть.

Профиль со списком `agents` — ведущий группы: перечисленные в нём профили поднимаются
как отдельные агенты, каждый со своей системной инструкцией и своим экраном, а сам ведущий
сводит их ответы. Профиль со списком `screens` раскладывает по вкладкам приём в несколько
шагов: каждый экран — своя инструкция, своя заготовка ввода и своя ветка разговора.

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
from .config import config_dir

DEFAULT_PROFILE_NAME = "default"


class Substitution(Template):
    """Подстановка $переменных с именами на любом языке.

    Стандартный Template распознаёт только латиницу, поэтому `$слов` он оставлял в тексте
    как есть — инструкция уходила в модель с долларом вместо числа, и заметить это можно
    было только по ответу. Тихая неподстановка хуже явной ошибки, поэтому шаблон расширен
    до любых буквенных имён.
    """

    idpattern = r"(?:[^\W\d]\w*)"


def user_profiles_dir() -> Path:
    return config_dir() / "profiles"


@dataclass
class Profile:
    name: str
    description: str = ""
    system: str | None = None  # итоговый текст инструкции (подстановки уже выполнены)
    system_file: str | None = None
    prefill: str | None = None  # заготовка ввода: подставляется в строку ввода при выборе профиля
    prefill_file: str | None = None
    keep_history: bool = True
    agents: list[str] = field(default_factory=list)  # непусто — профиль ведущего группы
    screens: list[str] = field(default_factory=list)  # непусто — набор рабочих экранов
    vars: dict[str, Any] = field(default_factory=dict)
    params: dict[str, Any] = field(default_factory=dict)
    source: Path | None = None

    def snapshot(self) -> dict[str, Any]:
        """Слепок профиля для журнала — по нему прогон можно воспроизвести."""
        snapshot: dict[str, Any] = {
            "name": self.name,
            "system": self.system,
            "keep_history": self.keep_history,
            "params": dict(self.params),
        }
        if self.agents:
            snapshot["agents"] = list(self.agents)
        if self.screens:
            snapshot["screens"] = list(self.screens)
        return snapshot

    def to_dict(self) -> dict[str, Any]:
        data: dict[str, Any] = {"name": self.name}
        if self.description:
            data["description"] = self.description
        if self.system_file:
            data["system_file"] = self.system_file
        elif self.system is not None:
            data["system"] = self.system
        if self.prefill_file:
            data["prefill_file"] = self.prefill_file
        elif self.prefill is not None:
            data["prefill"] = self.prefill
        data["keep_history"] = self.keep_history
        if self.agents:
            data["agents"] = list(self.agents)
        if self.screens:
            data["screens"] = list(self.screens)
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
    dirs.append(user_profiles_dir())
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


def _profile_names(raw: Any, field_name: str, warnings: list[str]) -> list[str]:
    """Список имён профилей (состав группы или набор рабочих экранов). Мусор в поле не должен
    ронять профиль — отбрасываем его с предупреждением, как и неизвестные параметры."""
    if raw is None:
        return []
    if not isinstance(raw, list):
        warnings.append(f"поле «{field_name}» — не список имён профилей, пропущено")
        return []
    names: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
        else:
            warnings.append(f"поле «{field_name}»: {item!r} — не имя профиля, пропущено")
    return names


def _from_dict(data: dict[str, Any], name: str, base_dir: Path, source: Path | None) -> tuple[Profile, list[str]]:
    warnings: list[str] = []
    known_meta = {
        "name",
        "description",
        "system",
        "system_file",
        "prefill",
        "prefill_file",
        "keep_history",
        "agents",
        "screens",
        "vars",
    }

    def _text(inline_key: str, file_key: str) -> str | None:
        value = data.get(inline_key)
        file_name = data.get(file_key)
        if not file_name:
            return value
        path = (base_dir / file_name).expanduser()
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            warnings.append(f"не удалось прочитать {path.name}: {exc}")
            return None

    system_text = _text("system", "system_file")
    system_file = data.get("system_file")
    prefill_text = _text("prefill", "prefill_file")

    variables = data.get("vars") or {}
    if variables:
        # safe_substitute по $имя: фигурные скобки примера JSON внутри инструкции не трогаются
        if system_text:
            system_text = Substitution(system_text).safe_substitute(variables)
        if prefill_text:
            prefill_text = Substitution(prefill_text).safe_substitute(variables)

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
        prefill=prefill_text.strip() if isinstance(prefill_text, str) else None,
        prefill_file=data.get("prefill_file"),
        keep_history=bool(data.get("keep_history", True)),
        agents=_profile_names(data.get("agents"), "agents", warnings),
        screens=_profile_names(data.get("screens"), "screens", warnings),
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
    directory = user_profiles_dir()
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{profile.name}.json"
    path.write_text(json.dumps(profile.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path
