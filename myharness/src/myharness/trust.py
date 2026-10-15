"""Доверие папке: один вопрос при первом запуске окна в папке, ответ запоминается.

Устройство по Claude Code 2.1.300 (`code.claude.com/docs/en/security` и программа):

- ключ доверия — корень хранилища `git`, вне `git` — сама папка;
- доверие предка покрывает подпапку: поиск идёт вверх до корня `git`, вне `git` — до корня
  файловой системы;
- домашний каталог не запоминается — доверие на сеанс, вопрос при каждом запуске;
- «нет» — выход без окна.

Доверие открывает файловым инструментам запись (способность `files`): вопроса посреди ответа у
`myharness` нет, и после доверия правка идёт без вопросов, как в режимах `acceptEdits` и `auto`
Claude Code. Список лежит в файле настроек (`trusted_dirs`), Claude Code держит его в
`~/.claude.json`.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from . import config as config_mod

# Доверие на этот сеанс: домашний каталог и папка при испорченном файле настроек.
_СЕАНС: set[str] = set()


def корень_git(cwd: Path) -> Path | None:
    cwd = cwd.resolve()
    for уровень in (cwd, *cwd.parents):
        if (уровень / ".git").exists():
            return уровень
    return None


def ключ(cwd: Path) -> Path:
    """Что запоминается: корень хранилища `git` либо сама папка."""
    return корень_git(cwd) or cwd.resolve()


def доверена(cwd: Path) -> bool:
    cwd = cwd.resolve()
    корень = корень_git(cwd)
    доверенные = set(config_mod.load().trusted_dirs) | _СЕАНС
    for уровень in (cwd, *cwd.parents):
        if str(уровень) in доверенные:
            return True
        if корень is not None and уровень == корень:
            return False
    return False


def доверить(cwd: Path) -> str | None:
    """Запомнить доверие; строка — предупреждение, почему оно только на сеанс."""
    к = ключ(cwd)
    if к == Path.home().resolve():
        _СЕАНС.add(str(к))
        return "домашний каталог: доверие только на этот сеанс, как у Claude Code"
    if config_mod.испорчен():
        # Переписать испорченный файл умолчаниями — значит потерять ключ API.
        _СЕАНС.add(str(к))
        return f"файл настроек {config_mod.config_path()} испорчен: доверие только на этот сеанс"
    настройки = config_mod.load()
    if str(к) not in настройки.trusted_dirs:
        настройки.trusted_dirs.append(str(к))
        config_mod.save(настройки)
    return None


ВОПРОС = (
    "Папка: {путь}\n"
    "Это ваш проект или проект, которому вы доверяете? Если нет — сначала посмотрите, что в ней.\n"
    "myharness сможет читать и править файлы здесь.\n"
    "  1 — да, доверяю этой папке\n"
    "  2 — нет, выйти\n"
)


def _вывод(текст: str) -> None:
    # В поток ошибок: при `myharness > файл` вопрос иначе ушёл бы в файл, а программа ждала бы ввода.
    print(текст, file=sys.stderr)


def _ввод(приглашение: str) -> str:
    sys.stderr.write(приглашение)
    sys.stderr.flush()
    строка = sys.stdin.readline()
    if not строка:
        raise EOFError
    return строка


def спросить(cwd: Path, ввод: Callable[[str], str] = _ввод, вывод: Callable[[str], None] = _вывод) -> bool:
    """Вопрос о доверии до открытия окна; `True` — доверено, можно открывать окно."""
    вывод(ВОПРОС.format(путь=ключ(cwd)))
    while True:
        try:
            ответ = ввод("> ").strip().lower()
        except (EOFError, KeyboardInterrupt):
            return False
        if ответ in ("1", "да", "y", "yes"):
            предупреждение = доверить(cwd)
            if предупреждение:
                вывод(предупреждение)
            return True
        if ответ in ("2", "нет", "n", "no"):
            return False
        вывод("Ответьте 1 или 2.")
