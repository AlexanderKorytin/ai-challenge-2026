"""Нарезка корпуса документов проекта двумя стратегиями.

Корпус — только требования проекта: `openspec/specs/*/spec.md`. Дизайны, планы, файлы состояния
(`docs/`) и `CLAUDE.md` в базу не входят с 2026-10-07 (решение пользователя, день 28). Причин две.
Документы о замерах пересказывали контрольные вопросы с ответами, и поиск возвращал модели сам
вопрос. И планы с файлами состояния — три четверти прежней базы — вытесняли требования из
найденного. Требование — первичный источник; что и почему решали, ассистент по базе не отвечает.

- `structure` режет по заголовкам Markdown уровней 1–3: кусок — раздел вместе со всеми его
  подразделами `####` и глубже. Для OpenSpec это ровно «требование со всеми сценариями».
- `fixed` режет окном `размер` символов с перекрытием 20 % (доля — по умолчаниям LangChain
  1000/200 и LlamaIndex 1024/200). Сам `размер` не выдумывается: он равен медиане длины
  кусков `structure` на том же корпусе, чтобы сравнение мерило *где режем*, а не *сколько кладём*.

`корпус_каталога` отдаёт в том же виде чужой корпус — каталог файлов Markdown вне проекта (книга
правил игры дня 29). Он идёт только в свой индекс: `cli.py index --corpus` требует `--index`.

Обещание для всех кусков: `text == файл[start:end]`. Сети модуль не знает, на диск не пишет.
"""

from __future__ import annotations

import fnmatch
import re
import statistics
from dataclasses import dataclass
from pathlib import Path

ЗАГОЛОВОК = re.compile(r"^(#{1,6}) (.+)$")
РАЗДЕЛИТЕЛЬ = " › "


@dataclass(frozen=True)
class Кусок:
    chunk_id: str  # f"{strategy}:{source}:{n}", n — порядковый номер в файле с 0
    strategy: str  # "structure" | "fixed"
    source: str  # путь от корня репозитория, разделитель "/"
    title: str  # первый заголовок "# " файла; нет его — имя файла без .md
    section: str  # цепочка заголовков уровней 2–4 на позиции start; "" до первого
    start: int  # смещение в символах внутри файла, включительно
    end: int  # исключительно
    text: str


def корпус(корень: Path) -> list[tuple[str, str]]:
    """[(source, текст)] корпуса, по возрастанию source."""
    пути = [*(корень / "openspec" / "specs").glob("*/spec.md")]
    return sorted((п.relative_to(корень).as_posix(), п.read_text("utf-8")) for п in пути if п.is_file())


def корпус_каталога(каталог: Path, включить: list[str] | None = None,
                    исключить: list[str] | None = None) -> list[tuple[str, str]]:
    """[(source, текст)] чужого корпуса: все `*.md` каталога рекурсивно, source — путь от каталога.

    `включить` сужает корпус до названных путей от каталога: файл `.md` берётся как есть,
    каталог — рекурсивно. Названного пути нет — `ValueError`: опечатка в имени иначе молча
    собрала бы индекс без целого раздела документации.

    `исключить` — шаблоны `fnmatch` по имени источника (путь от каталога): подошедший файл в
    корпус не входит. Так из индекса помощника уходят рабочие записи `docs/план-*.md` и
    `docs/состояние-*.md`: их «карты файлов» вытесняли из выдачи сами требования."""
    пары = _корпус_каталога(каталог, включить)
    return [(имя, текст) for имя, текст in пары if not any(fnmatch.fnmatchcase(имя, ш) for ш in исключить or ())]


def _корпус_каталога(каталог: Path, включить: list[str] | None) -> list[tuple[str, str]]:
    if включить is None:
        пути = [п for п in каталог.rglob("*.md") if п.is_file()]
    else:
        пути = []
        корень = каталог.resolve()
        for имя in включить:
            путь = (каталог / имя).resolve()
            if путь != корень and корень not in путь.parents:
                raise ValueError(f"--include: путь {имя} лежит вне каталога {каталог}")
            if путь.is_dir():
                пути.extend(п for п in путь.rglob("*.md") if п.is_file())
            elif путь.is_file() and путь.suffix == ".md":
                пути.append(путь)
            else:
                raise ValueError(f"--include: в каталоге {каталог} нет каталога или файла .md с именем {имя}")
        пути = list(dict.fromkeys(пути))
        return sorted((п.relative_to(корень).as_posix(), п.read_text("utf-8")) for п in пути)
    return sorted((п.relative_to(каталог).as_posix(), п.read_text("utf-8")) for п in пути)


def заголовки(текст: str) -> list[tuple[int, int, str]]:
    """[(смещение начала строки, уровень, текст)] заголовков вне ограждений кода."""
    итог: list[tuple[int, int, str]] = []
    в_ограждении = False
    смещение = 0
    for строка in текст.splitlines(keepends=True):
        голая = строка.rstrip("\r\n")
        if голая.lstrip().startswith("```"):
            в_ограждении = not в_ограждении
        elif not в_ограждении and (найдено := ЗАГОЛОВОК.match(голая)):
            итог.append((смещение, len(найдено.group(1)), найдено.group(2).strip()))
        смещение += len(строка)
    return итог


def заглавие(source: str, текст: str) -> str:
    for _, уровень, имя in заголовки(текст):
        if уровень == 1:
            return имя
    return Path(source).stem


def _цепочка(список: list[tuple[int, int, str]], позиция: int) -> str:
    """Цепочка заголовков уровней 2–4, действующая на позиции."""
    стопка: list[tuple[int, str]] = []
    for смещение, уровень, имя in список:
        if смещение > позиция:
            break
        стопка = [(у, и) for у, и in стопка if у < уровень]
        if 2 <= уровень <= 4:
            стопка.append((уровень, имя))
    return РАЗДЕЛИТЕЛЬ.join(и for _, и in стопка)


def разделы(текст: str) -> list[tuple[int, int]]:
    """Границы разделов стратегии structure: [start, end) встык от 0 до len(текст)."""
    начала = sorted({0, *(с for с, уровень, _ in заголовки(текст) if уровень <= 3)})
    концы = [*начала[1:], len(текст)]
    return [(н, к) for н, к in zip(начала, концы) if н < к]


def _куски(strategy: str, source: str, текст: str, границы: list[tuple[int, int]]) -> list[Кусок]:
    список = заголовки(текст)
    имя = заглавие(source, текст)
    итог: list[Кусок] = []
    for start, end in границы:
        if not текст[start:end].strip():
            continue
        итог.append(Кусок(f"{strategy}:{source}:{len(итог)}", strategy, source, имя,
                          _цепочка(список, start), start, end, текст[start:end]))
    return итог


def нарезать_structure(source: str, текст: str) -> list[Кусок]:
    return _куски("structure", source, текст, разделы(текст))


def окна(длина: int, размер: int) -> list[tuple[int, int]]:
    """Окна [start, end) размера `размер` с шагом `размер - размер // 5` (перекрытие 20 %)."""
    if размер < 1:
        raise ValueError(f"размер окна должен быть положительным: {размер}")
    шаг = размер - размер // 5
    итог: list[tuple[int, int]] = []
    start = 0
    while start < длина:
        end = min(start + размер, длина)
        итог.append((start, end))
        if end == длина:
            break
        start += шаг
    return итог


def нарезать_fixed(source: str, текст: str, размер: int) -> list[Кусок]:
    return _куски("fixed", source, текст, окна(len(текст), размер))


def размер_fixed(куски_structure: list[Кусок]) -> int:
    """Медиана длины кусков structure, округлённая вниз."""
    return int(statistics.median(len(к.text) for к in куски_structure))
