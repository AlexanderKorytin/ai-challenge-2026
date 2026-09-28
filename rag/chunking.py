"""Нарезка корпуса документов проекта двумя стратегиями.

Корпус — знания самого проекта: требования `openspec/specs/*/spec.md`, дизайны и планы
`docs/**/*.md` и `CLAUDE.md`.

- `structure` режет по заголовкам Markdown уровней 1–3: кусок — раздел вместе со всеми его
  подразделами `####` и глубже. Для OpenSpec это ровно «требование со всеми сценариями».
- `fixed` режет окном `размер` символов с перекрытием 20 % (доля — по умолчаниям LangChain
  1000/200 и LlamaIndex 1024/200). Сам `размер` не выдумывается: он равен медиане длины
  кусков `structure` на том же корпусе, чтобы сравнение мерило *где режем*, а не *сколько кладём*.

Обещание для всех кусков: `text == файл[start:end]`. Сети модуль не знает, на диск не пишет.
"""

from __future__ import annotations

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
    пути = [корень / "CLAUDE.md", *(корень / "openspec" / "specs").glob("*/spec.md"),
            *(корень / "docs").rglob("*.md")]
    return sorted((п.relative_to(корень).as_posix(), п.read_text("utf-8")) for п in пути if п.is_file())


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
