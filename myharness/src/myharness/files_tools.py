"""Встроенные файловые инструменты модели: `Read`, `Write`, `Edit`, `Glob`, `Grep`.

Имена, доводы и пределы взяты у Claude Code (`code.claude.com/docs/en/tools-reference.md`):
правила `permissions` в `.claude/settings.json` обе программы понимают одинаково. Своих чисел
здесь нет — каждое из документации Claude Code или из его программы 2.1.300.

Корень — рабочий каталог на миг сборки набора. Путь проходит `realpath`: ни `..`, ни
символическая ссылка наружу корня не выводят. Запись закрыта в `.git/`, `.claude/` и
`.mcp.json` на любой глубине: через них модель выдала бы себе права или запуск команды
(день 20: команда в `.git/config`). Каталог настроек закрыт и для чтения — там ключ API.

Чтение разрешено, пока его не запретил `deny`. Запись по умолчанию — только внутри хранилища
`git`, как у Codex: недоверенный каталог работает только на чтение, а в `git` любую правку
можно отменить. `allow` разрешает запись и вне `git`.
"""

from __future__ import annotations

import asyncio
import difflib
import fnmatch
import json
import os
import re
import subprocess
import sys
import unicodedata
from uuid import uuid4
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from . import config, mcp_tools, memory, tokens
from .agent import Agent, Инструменты

ИМЕНА = ("Read", "Write", "Edit", "Glob", "Grep")
ЧТЕНИЕ = ("Read", "Glob", "Grep")
ЗАПИСЬ = ("Edit", "Write")

СТРОК_ПО_УМОЛЧАНИЮ = 2000
# Строка длиннее — обрезается в показе `Read` (общая обрезка 2000 знаков в программе Claude Code
# 2.1.300). Дочитать её можно `Grep` с `output_mode: content`.
ДЛИНА_СТРОКИ = 2000
ПРЕДЕЛ_БАЙТ = 256 * 1024
ПРЕДЕЛ_ТОКЕНОВ = 25_000
ПЕРЕМЕННАЯ_ПРЕДЕЛА = "MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS"
ПРЕДЕЛ_GLOB = 100
ПЕРЕМЕННАЯ_ЗАПРЕТА = "MYHARNESS_DISALLOWED_TOOLS"
# Признак двоичного файла — нулевой байт в начале, как у `git` и `grep`.
ОБРАЗЕЦ_БАЙТ = 8192

ЗАКРЫТЫЕ_ИМЕНА = (".git", ".claude", ".mcp.json")


@dataclass
class Учёт:
    """Что модель прочитала в этом сеансе: путь → (время изменения в нс, прочитан ли целиком).

    Один на агента. Правка требует прежнего полного чтения: иначе модель переписала бы файл,
    которого не видела. Чтение с пометкой `PARTIAL view` полным не считается."""

    прочитано: dict[Path, tuple[int, bool]] = field(default_factory=dict)

    def отметить(self, путь: Path, полное: bool) -> None:
        время = путь.stat().st_mtime_ns
        прежнее = self.прочитано.get(путь)
        # Полное чтение не теряется от следующего частичного того же неизменного файла.
        if прежнее is not None and прежнее[0] == время and прежнее[1]:
            полное = True
        self.прочитано[путь] = (время, полное)

    def полное(self, путь: Path) -> tuple[int, bool] | None:
        return self.прочитано.get(путь)


def _тождество(путь: Path) -> tuple[int, int] | None:
    try:
        сведения = os.stat(путь)
    except OSError:
        return None
    return сведения.st_dev, сведения.st_ino


def _цепочка(путь: Path) -> set[tuple[int, int]]:
    """Тождества пути и всех его существующих предков."""
    return {т for уровень in (путь, *путь.parents) if (т := _тождество(уровень)) is not None}


def _имя_сверки(имя: str) -> str:
    """Имя для сверки с закрытыми: без регистра и формы Юникода, как сравнивает APFS.

    `casefold`, а не `lower`: `"ſ".casefold() == "s"`, и `.mcp.jſon` на APFS — тот же `.mcp.json`."""
    return unicodedata.normalize("NFKD", unicodedata.normalize("NFKD", имя).casefold())


_ЗАКРЫТЫЕ_ИМЕНА = {_имя_сверки(имя) for имя in ЗАКРЫТЫЕ_ИМЕНА}


def в_git(корень: Path) -> bool:
    """Лежит ли корень в хранилище `git`: `.git` (каталог или файл рабочей копии) в нём или выше."""
    корень = корень.resolve()
    return any((уровень / ".git").exists() for уровень in (корень, *корень.parents))


def права(корень: Path) -> tuple[tuple[str, ...], str | None]:
    """Доступные имена и жалоба — по правилам рабочего каталога на этот миг.

    Испорченный файл правил снимает все файловые инструменты: пропавший `deny` иначе дал бы
    запись по умолчанию. Запрет с путём (`Read(./.env)`) снимает инструменты своего рода
    целиком: путей правил здесь нет, а молча пропущенный запрет открыл бы то, что человек
    закрыл для Claude Code."""
    разрешения, жалоба = mcp_tools.прочитать_разрешения(корень)
    if жалоба:
        return (), f"файловые инструменты сняты: {жалоба}"
    # Запрет запуском, как ключ `--disallowedTools` у Claude Code: те же правила, что в `deny`.
    запуск = [п.strip() for п in os.environ.get(ПЕРЕМЕННАЯ_ЗАПРЕТА, "").split(",") if п.strip()]
    if запуск:
        разрешения = mcp_tools.Разрешения(разрешения.allow, (*разрешения.deny, *запуск))
    с_путём = [п for п in разрешения.deny if "(" in п and п.split("(", 1)[0] in (*ИМЕНА,)]
    запись_в_git = в_git(корень)
    доступны = []
    for имя in ИМЕНА:
        решение = разрешения.встроенный(имя)
        if решение is False:
            continue
        if имя in ЗАПИСЬ and решение is None and not запись_в_git:
            continue
        доступны.append(имя)
    жалоба_путей = None
    if с_путём:
        жалоба_путей = (
            "правило с путём не поддержано, инструменты его рода сняты: " + ", ".join(с_путём)
        )
    return tuple(доступны), жалоба_путей


СХЕМЫ: dict[str, dict] = {
    "Read": {
        "description": (
            "Читает текстовый файл рабочего каталога. Строки идут с номерами: номер, табуляция, "
            "текст. По умолчанию — первые 2000 строк; дальше читай доводами offset и limit. "
            "Ответ не длиннее 25 000 токенов: большой файл приходит началом с пометкой "
            "PARTIAL view. Файл больше 256 КБ целиком не читается — читай частями или ищи Grep. "
            "allow_large: true снимает пределы, меркой служит свободное место окна. Перед Edit "
            "и перед Write поверх файла его нужно прочитать целиком."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "путь от рабочего каталога или абсолютный"},
                "offset": {"type": "integer", "description": "номер первой строки, с 1"},
                "limit": {"type": "integer", "description": "сколько строк прочитать"},
                "allow_large": {"type": "boolean", "description": "снять пределы размера"},
            },
            "required": ["file_path"],
        },
    },
    "Write": {
        "description": (
            "Создаёт файл или пишет его целиком заново. Возвращает diff. Поверх существующего "
            "файла — только после его полного чтения Read. Для правки части файла бери Edit. "
            "Писать в .git, .claude и .mcp.json нельзя."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "путь от рабочего каталога или абсолютный"},
                "content": {"type": "string", "description": "весь текст файла"},
            },
            "required": ["file_path", "content"],
        },
    },
    "Edit": {
        "description": (
            "Заменяет в файле точный кусок old_string на new_string, без регулярных выражений. "
            "old_string должен встречаться ровно один раз; иначе дай кусок длиннее или "
            "replace_all: true. Только после полного чтения файла Read. Возвращает diff."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "путь от рабочего каталога или абсолютный"},
                "old_string": {"type": "string", "description": "заменяемый текст, точно как в файле"},
                "new_string": {"type": "string", "description": "новый текст"},
                "replace_all": {"type": "boolean", "description": "заменить все вхождения"},
            },
            "required": ["file_path", "old_string", "new_string"],
        },
    },
    "Glob": {
        "description": (
            "Ищет файлы по шаблону имени, например **/*.py. Отдаёт до 100 путей, новые по "
            "времени изменения первыми. .gitignore не учитывает, каталог .git пропускает."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "шаблон пути"},
                "path": {"type": "string", "description": "каталог поиска; по умолчанию рабочий"},
            },
            "required": ["pattern"],
        },
    },
    "Grep": {
        "description": (
            "Ищет текст регулярным выражением (синтаксис Python re) в файлах рабочего каталога. "
            "Учитывает .gitignore, двоичные файлы пропускает. output_mode: files_with_matches "
            "(по умолчанию) — пути файлов с совпадением; content — строки путь:номер:текст; "
            "count — число совпадений по файлам. head_limit и offset листают вывод."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "регулярное выражение"},
                "path": {"type": "string", "description": "файл или каталог; по умолчанию рабочий"},
                "glob": {"type": "string", "description": "отбор файлов по шаблону, например *.py"},
                "output_mode": {"type": "string", "enum": ["files_with_matches", "content", "count"]},
                "-i": {"type": "boolean", "description": "без учёта регистра"},
                "head_limit": {"type": "integer", "description": "сколько записей вывода отдать"},
                "offset": {"type": "integer", "description": "сколько записей вывода пропустить"},
            },
            "required": ["pattern"],
        },
    },
}


def _целое(доводы: dict, имя: str, наименьшее: int) -> int | None:
    значение = доводы.get(имя)
    if значение is None:
        return None
    if isinstance(значение, bool) or not isinstance(значение, int) or значение < наименьшее:
        raise ValueError(f"{имя} — целое не меньше {наименьшее}")
    return значение


def _предел_чтения() -> int:
    значение = os.environ.get(ПЕРЕМЕННАЯ_ПРЕДЕЛА)
    if значение and значение.isdigit() and int(значение) > 0:
        return int(значение)
    return ПРЕДЕЛ_ТОКЕНОВ


def _текст(путь: Path) -> str:
    """Содержимое текстового файла либо отказ: двоичный и не UTF-8 модель не читает."""
    данные = путь.read_bytes()
    if b"\0" in данные[:ОБРАЗЕЦ_БАЙТ]:
        raise ValueError(f"двоичный файл: {путь.name}")
    try:
        return данные.decode("utf-8")
    except UnicodeDecodeError:
        raise ValueError(f"файл не в UTF-8: {путь.name}") from None


def _строки_diff(текст: str) -> list[str]:
    return [строка if строка.endswith("\n") else строка + "\n" for строка in текст.splitlines(True)]


class _Набор:
    def __init__(self, корень: Path, модель: Callable[[], str], свободно: Callable[[], int | None],
                 учёт: Учёт, выгрузка: Path) -> None:
        self.корень = корень.resolve()
        self.модель = модель
        self.свободно = свободно
        self.учёт = учёт
        self.выгрузка = выгрузка

    # --- пути -------------------------------------------------------------------------------
    #
    # Сверка идёт по тождеству файла (`st_dev`, `st_ino`), а не по тексту пути: macOS не различает
    # регистр и форму Юникода, и `CFG/config.json` открыл бы закрытый `cfg`. Ссылки и `..` снимает
    # `resolve()`, после него цепочка предков пути — настоящая.

    def _закрыто_почему(self, путь: Path, *, запись: bool) -> str | None:
        """Причина отказа для пути после `resolve()`; `None` — путь доступен."""
        цепочка = _цепочка(путь)
        настройки = {_тождество(config.config_dir()), _тождество(config.config_path())} - {None}
        # Сам файл настроек — тоже по тождеству: жёсткая ссылка на него внутри корня иначе прошла бы.
        if настройки & цепочка:
            return "каталог настроек myharness закрыт"
        выгрузка = _тождество(self.выгрузка)
        if not запись and выгрузка is not None and выгрузка in цепочка:
            return None
        состояние = _тождество(memory.state_dir())
        if состояние is not None and состояние in цепочка:
            return "каталог состояния myharness закрыт"
        части = self._части(путь)
        if части is None:
            return "путь вне рабочего каталога"
        if запись:
            имена = [_имя_сверки(часть) for часть in части]
            if any(имя in _ЗАКРЫТЫЕ_ИМЕНА for имя in имена):
                return "запись закрыта: .git, .claude и .mcp.json правит только человек"
        return None

    def _части(self, путь: Path) -> list[str] | None:
        """Звенья пути от корня; `None` — путь не внутри корня."""
        корень = _тождество(self.корень)
        части: list[str] = []
        for уровень in (путь, *путь.parents):
            if корень is not None and _тождество(уровень) == корень:
                return list(reversed(части))
            части.append(уровень.name)
        return None

    def путь(self, сырое: object, *, запись: bool = False) -> Path:
        if not isinstance(сырое, str) or not сырое.strip():
            raise ValueError("не задан путь")
        путь = Path(сырое).expanduser()
        if not путь.is_absolute():
            путь = self.корень / путь
        путь = путь.resolve()
        причина = self._закрыто_почему(путь, запись=запись)
        if причина is not None:
            raise ValueError(f"{причина}: {сырое}")
        return путь

    def доступен(self, путь: Path) -> bool:
        """Покажет ли обход каталога этот файл: после `resolve()` он читается по правилам `путь`."""
        try:
            return self._закрыто_почему(путь.resolve(), запись=False) is None
        except OSError:
            return False

    def имя(self, путь: Path) -> str:
        части = self._части(путь)
        return "/".join(части) if части else str(путь)

    # --- Read -------------------------------------------------------------------------------

    def read(self, доводы: dict) -> str:
        путь = self.путь(доводы.get("file_path"))
        if путь.is_dir():
            raise ValueError(f"это каталог, а не файл: {доводы.get('file_path')} — смотри Glob")
        if not путь.is_file():
            raise ValueError(f"файла нет: {доводы.get('file_path')}")
        начало = _целое(доводы, "offset", 1)
        сколько = _целое(доводы, "limit", 1)
        большой = доводы.get("allow_large") is True
        частью = начало is not None or сколько is not None
        размер = путь.stat().st_size
        if размер > ПРЕДЕЛ_БАЙТ and not частью and not большой:
            raise ValueError(
                f"файл {размер} байт больше 256 КБ: читай частями доводами offset и limit или ищи Grep"
            )
        строки = _текст(путь).splitlines()
        if not строки:
            self.учёт.отметить(путь, True)
            return "(файл пуст: 0 строк)"
        первая = начало or 1
        if первая > len(строки):
            return f"(offset {первая} за концом файла: строк {len(строки)})"
        кусок = строки[первая - 1 : первая - 1 + (сколько or СТРОК_ПО_УМОЛЧАНИЮ)]
        if большой:
            предел = self.свободно()
            if предел is None:
                raise ValueError("allow_large: свободное место окна неизвестно — читай частями offset и limit")
        else:
            предел = _предел_чтения()

        def показ(часть: list[str]) -> str:
            return "\n".join(
                f"{первая + i:>6}\t{строка[:ДЛИНА_СТРОКИ]}"
                + (f" … [строка обрезана: {len(строка)} знаков]" if len(строка) > ДЛИНА_СТРОКИ else "")
                for i, строка in enumerate(часть)
            )

        текст = показ(кусок)
        вес = tokens.count_text(текст, self.модель())
        урезано = False
        if вес > предел:
            if частью or большой:
                raise ValueError(
                    f"часть файла — {вес} токенов, больше предела {предел}: читай меньшей частью "
                    "(limit) или ищи Grep"
                )
            # Целый файл сверх предела — начало, пока влезает, и пометка, как у Claude Code.
            число = max(1, len(кусок) * предел // вес)
            while число > 1 and tokens.count_text(показ(кусок[:число]), self.модель()) > предел:
                число = число * 9 // 10
            кусок = кусок[:число]
            текст = показ(кусок)
            if tokens.count_text(текст, self.модель()) > предел:
                raise ValueError(f"первая строка больше предела {предел} токенов: ищи в файле Grep")
            урезано = True
        последняя = первая + len(кусок) - 1
        полное = первая == 1 and последняя == len(строки) and all(len(с) <= ДЛИНА_СТРОКИ for с in кусок)
        self.учёт.отметить(путь, полное)
        if урезано:
            текст += (
                f"\n[PARTIAL view: показаны строки {первая}–{последняя} из {len(строки)}; "
                "читай дальше доводами offset и limit]"
            )
        elif not полное and последняя == len(строки) and первая == 1:
            текст += "\n(есть обрезанные строки — чтение не полное; дочитай их Grep с output_mode content)"
        elif not полное:
            текст += f"\n(показаны строки {первая}–{последняя} из {len(строки)})"
        return текст

    # --- Glob -------------------------------------------------------------------------------

    def glob(self, доводы: dict) -> str:
        шаблон = доводы.get("pattern")
        if not isinstance(шаблон, str) or not шаблон:
            raise ValueError("не задан pattern")
        if Path(шаблон).is_absolute() or ".." in Path(шаблон).parts:
            raise ValueError("шаблон Glob — от path, без .. и абсолютного пути")
        основа = self.путь(доводы.get("path") or ".")
        if not основа.is_dir():
            raise ValueError(f"не каталог: {доводы.get('path')}")
        найдено = [
            путь for путь in основа.glob(шаблон)
            if ".git" not in путь.relative_to(основа).parts and путь.is_file() and self.доступен(путь)
        ]
        if not найдено:
            return "файлов не найдено"
        найдено.sort(key=lambda путь: путь.stat().st_mtime_ns, reverse=True)
        строки = [self.имя(путь.resolve()) for путь in найдено[:ПРЕДЕЛ_GLOB]]
        if len(найдено) > ПРЕДЕЛ_GLOB:
            строки.append(f"(показаны {ПРЕДЕЛ_GLOB} из {len(найдено)}; уточни шаблон или path)")
        return "\n".join(строки)

    # --- Grep -------------------------------------------------------------------------------

    def _файлы(self, основа: Path) -> list[Path]:
        """Файлы поиска: в хранилище `git` — без игнорируемых, иначе обход без `.git`."""
        if основа.is_file():
            return [основа]
        if в_git(основа):
            итог = subprocess.run(
                # Ни наблюдателя файловой системы, ни крючков из чужих настроек хранилища.
                ["git", "-c", "core.fsmonitor=false", "-c", "core.hooksPath=/dev/null",
                 "-C", str(основа), "ls-files", "-co", "--exclude-standard", "-z"],
                capture_output=True, check=False,
            )
            if итог.returncode == 0:
                пути = [основа / имя for имя in итог.stdout.decode("utf-8", "replace").split("\0") if имя]
                return [путь for путь in пути if путь.is_file() and self.доступен(путь)]
        найдено = []
        for каталог, подкаталоги, файлы in os.walk(основа):
            подкаталоги[:] = [имя for имя in подкаталоги if имя != ".git"]
            for имя in файлы:
                путь = Path(каталог) / имя
                if self.доступен(путь):
                    найдено.append(путь)
        return найдено

    def grep(self, доводы: dict) -> str:
        шаблон = доводы.get("pattern")
        if not isinstance(шаблон, str) or not шаблон:
            raise ValueError("не задан pattern")
        try:
            выражение = re.compile(шаблон, re.IGNORECASE if доводы.get("-i") is True else 0)
        except re.error as exc:
            raise ValueError(f"негодное регулярное выражение: {exc}") from None
        режим = доводы.get("output_mode") or "files_with_matches"
        if режим not in ("files_with_matches", "content", "count"):
            raise ValueError(f"output_mode — files_with_matches, content или count, а не {режим}")
        отбор = доводы.get("glob")
        сколько = _целое(доводы, "head_limit", 1)
        пропуск = _целое(доводы, "offset", 0) or 0
        основа = self.путь(доводы.get("path") or ".")
        if not основа.exists():
            raise ValueError(f"пути нет: {доводы.get('path')}")
        записи: list[str] = []
        файлов_с_совпадением = 0
        совпадений = 0
        попадания: list[tuple[int, str]] = []
        for путь in self._файлы(основа):
            имя = self.имя(путь.resolve())
            if isinstance(отбор, str) and отбор and not (
                fnmatch.fnmatch(путь.name, отбор) or fnmatch.fnmatch(имя, отбор)
            ):
                continue
            try:
                текст = _текст(путь)
            except (ValueError, OSError):
                continue
            строки = [(номер, строка) for номер, строка in enumerate(текст.splitlines(), 1) if выражение.search(строка)]
            if not строки:
                continue
            файлов_с_совпадением += 1
            совпадений += len(строки)
            if режим == "files_with_matches":
                попадания.append((путь.stat().st_mtime_ns, имя))
            elif режим == "count":
                записи.append(f"{имя}:{len(строки)}")
            else:
                записи.extend(f"{имя}:{номер}:{строка}" for номер, строка in строки)
        if режим == "files_with_matches":
            записи = [имя for _, имя in sorted(попадания, reverse=True)]
        if not записи:
            return "совпадений нет"
        if пропуск >= len(записи):
            return "No entries at this offset"
        всего = len(записи)
        конец = min(всего, пропуск + сколько) if сколько else всего
        показано = записи[пропуск:конец]
        if конец < всего:
            показано.append(f"(записи {пропуск + 1}–{конец} из {всего}; дальше — offset {конец})")
        if режим == "count":
            показано.append(f"итого: {совпадений} совпадений в {файлов_с_совпадением} файлах")
        return "\n".join(показано)

    # --- Edit и Write -----------------------------------------------------------------------

    def _прочитан(self, путь: Path, доводы: dict) -> tuple[int, bool]:
        отметка = self.учёт.полное(путь)
        if отметка is None or not отметка[1]:
            raise ValueError(f"сначала прочитай файл целиком инструментом Read: {доводы.get('file_path')}")
        return отметка

    def _записать(self, путь: Path, прежний: str, новый: str, создан: bool) -> str:
        # Текст кодируется до записи: одиночная суррогатная пара иначе обнулила бы файл. Запись —
        # во временный файл рядом и `os.replace`: обрыв посреди записи не теряет прежний файл.
        данные = новый.encode("utf-8")
        права = (путь.stat().st_mode & 0o777) if not создан else None
        временный = путь.with_name(f".{путь.name}.{uuid4().hex}.tmp")
        дескриптор = os.open(временный, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
        try:
            with os.fdopen(дескриптор, "wb") as файл:
                файл.write(данные)
            if права is not None:
                os.chmod(временный, права)
            os.replace(временный, путь)
        except BaseException:
            временный.unlink(missing_ok=True)
            raise
        self.учёт.отметить(путь, True)
        имя = self.имя(путь)
        откуда = "/dev/null" if создан else f"a/{имя}"
        разница = "".join(difflib.unified_diff(_строки_diff(прежний), _строки_diff(новый), откуда, f"b/{имя}"))
        return разница or f"(файл {имя} не изменился)"

    def edit(self, доводы: dict) -> str:
        путь = self.путь(доводы.get("file_path"), запись=True)
        if not путь.is_file():
            raise ValueError(f"файла нет: {доводы.get('file_path')} — новый файл создаёт Write")
        self._прочитан(путь, доводы)
        старое, новое = доводы.get("old_string"), доводы.get("new_string")
        if not isinstance(старое, str) or not isinstance(новое, str):
            raise ValueError("old_string и new_string — строки")
        if not старое:
            raise ValueError("пустая old_string: новый файл создаёт Write")
        if старое == новое:
            raise ValueError("old_string и new_string совпадают")
        текст = _текст(путь)
        число = текст.count(старое)
        if число == 0:
            raise ValueError("old_string в файле не найден — перечитай файл Read")
        все = доводы.get("replace_all") is True
        if число > 1 and not все:
            raise ValueError(f"old_string встречается {число} раз: дай кусок длиннее или replace_all: true")
        return self._записать(путь, текст, текст.replace(старое, новое) if все else текст.replace(старое, новое, 1), False)

    def write(self, доводы: dict) -> str:
        путь = self.путь(доводы.get("file_path"), запись=True)
        содержимое = доводы.get("content")
        if not isinstance(содержимое, str):
            raise ValueError("content — строка")
        if путь.is_dir():
            raise ValueError(f"это каталог: {доводы.get('file_path')}")
        if путь.exists():
            время, _ = self._прочитан(путь, доводы)
            if путь.stat().st_mtime_ns != время:
                raise ValueError(f"файл изменился после чтения — перечитай Read: {доводы.get('file_path')}")
            return self._записать(путь, _текст(путь), содержимое, False)
        путь.parent.mkdir(parents=True, exist_ok=True)
        return self._записать(путь, "", содержимое, True)


def свободное_место(агент: Agent | None) -> int | None:
    """Свободное место окна для `Read` с `allow_large`: окно модели минус вес текущего запроса.

    `None` — неизвестно (обмена ещё не было или окно модели неизвестно): `Read` тогда отказывает."""
    if агент is None or агент.вес_запроса is None:
        return None
    окно = tokens.window(агент.модель_обмена)
    return None if окно is None else max(0, окно - агент.вес_запроса)


def набор(
    корень: Path,
    модель: Callable[[], str],
    свободно: Callable[[], int | None],
    учёт: Учёт,
    выгрузка: Path,
) -> Инструменты | None:
    """Файловые инструменты для рабочего каталога `корень`; `None` — ни одного и жаловаться не на что.

    Схемы `Write` и `Edit` есть, только если запись разрешена (см. `права`). Правила
    перечитываются и на миг вызова: снятое посреди круга правило отказывает следующему вызову."""
    доступны, жалоба = права(корень)
    if not доступны and жалоба is None:
        return None
    исполнитель = _Набор(корень, модель, свободно, учёт, выгрузка)
    действия = {
        "Read": исполнитель.read, "Write": исполнитель.write, "Edit": исполнитель.edit,
        "Glob": исполнитель.glob, "Grep": исполнитель.grep,
    }

    async def исполнить(имя: str, доводы: str) -> str:
        if имя not in права(корень)[0]:
            raise ValueError(f"инструмент {имя} не разрешён")
        try:
            разобранное = json.loads(доводы) if доводы else {}
        except json.JSONDecodeError as exc:
            raise ValueError(f"доводы не JSON: {exc}") from None
        if not isinstance(разобранное, dict):
            raise ValueError("доводы — не объект JSON")
        try:
            if имя == "Grep":
                return await _grep_процессом(корень, выгрузка, разобранное)
            # В потоке: обход дерева и чтение файлов не держат окно, ввод-вывод отпускает
            # блокировку интерпретатора.
            return await asyncio.to_thread(действия[имя], разобранное)
        except UnicodeEncodeError:
            raise ValueError("текст не кодируется в UTF-8 (одиночная суррогатная пара?) — файл не тронут") from None
        except OSError as exc:
            raise ValueError(f"{exc.strerror or exc}: {доводы_путь(разобранное)}") from None

    схемы = [{"type": "function", "function": {"name": имя, **СХЕМЫ[имя]}} for имя in доступны]
    return Инструменты(схемы=схемы, исполнить=исполнить, жалоба=жалоба)


def доводы_путь(доводы: dict) -> str:
    return str(доводы.get("file_path") or доводы.get("path") or "")


async def _grep_процессом(корень: Path, выгрузка: Path, доводы: dict) -> str:
    """`Grep` в отдельном процессе, который отмена обмена убивает.

    Выражение с перебором с возвратом (`(a|aa)*$c`) держит блокировку интерпретатора Python на всё
    время одного `re.search`: поток не спасает, окно замерзает на десятки секунд. Claude Code ищет
    через `ripgrep` отдельным процессом — здесь тот же приём со своим исполнителем `re`."""
    процесс = await asyncio.create_subprocess_exec(
        sys.executable, "-m", "myharness.files_tools",
        stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
    )
    вход = json.dumps({"корень": str(корень), "выгрузка": str(выгрузка), "доводы": доводы}, ensure_ascii=False)
    try:
        вывод, ошибки = await процесс.communicate(вход.encode("utf-8"))
    except BaseException:
        if процесс.returncode is None:
            процесс.kill()
            await процесс.wait()
        raise
    try:
        ответ = json.loads(вывод.decode("utf-8"))
    except ValueError:
        raise RuntimeError(f"поиск упал: {ошибки.decode('utf-8', 'replace')[-2000:]}") from None
    if "ошибка" in ответ:
        raise ValueError(ответ["ошибка"])
    return ответ["итог"]


def _исполнитель_grep() -> None:
    """Точка входа процесса `Grep`: задание JSON на входе, итог либо ошибка JSON на выходе."""
    задание = json.loads(sys.stdin.buffer.read().decode("utf-8"))
    исполнитель = _Набор(Path(задание["корень"]), lambda: "", lambda: None, Учёт(), Path(задание["выгрузка"]))
    try:
        ответ = {"итог": исполнитель.grep(задание["доводы"])}
    except (ValueError, OSError) as exc:
        ответ = {"ошибка": str(exc)}
    sys.stdout.buffer.write(json.dumps(ответ, ensure_ascii=False).encode("utf-8"))


if __name__ == "__main__":
    _исполнитель_grep()
