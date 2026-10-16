"""Окружение агента: каталоги `myharness` и профили — внутри каталогов самого агента.

Агент — отдельная программа: у другого человека `myharness` нет, и его настройки агент не читает
и не пишет (поправка пользователя 2026-10-16). `myharness` читает свои пути из окружения при
каждом обращении, поэтому достаточно задать их до первого обмена. `setdefault` — чтобы проверки
и службы могли увести пути сами.

Вызывать первой строкой точки входа: `web.py`, `jobs.py`, `menubar.py`.
"""

from __future__ import annotations

import os
from pathlib import Path

ЗДЕСЬ = Path(__file__).resolve().parent


def данные() -> Path:
    return Path(os.environ.get("GAMES_DATA_DIR") or Path.home() / ".local" / "share" / "games-agent")


def настройки() -> Path:
    return Path(os.environ.get("GAMES_CONFIG_DIR") or Path.home() / ".config" / "games-agent")


def задать() -> None:
    os.environ.setdefault("GAMES_DATA_DIR", str(данные()))
    os.environ.setdefault("GAMES_CONFIG_DIR", str(настройки()))
    os.environ.setdefault("MYHARNESS_CONFIG_DIR", str(настройки() / "myharness"))
    os.environ.setdefault("MYHARNESS_STATE_DIR", str(данные() / "myharness"))
    os.environ.setdefault("MYHARNESS_PROFILES", str(ЗДЕСЬ / "profiles"))
    # Журнал прогонов несёт вкус в запросах — его место в каталоге агента, а не там, откуда
    # запущен процесс (у служб это был бы код в репозитории).
    os.environ.setdefault("MYHARNESS_JOURNAL", str(данные() / "myharness-journal.jsonl"))
