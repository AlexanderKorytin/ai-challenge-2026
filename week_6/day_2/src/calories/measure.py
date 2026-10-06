"""Замер: числа модели против справочных чисел контрольного набора."""

import json
from dataclasses import dataclass
from pathlib import Path

from .core import Блюдо, оценить, разряд
from .ollama import ОтветНегоден


class НаборНегоден(Exception):
    """Контрольный набор нельзя прочитать."""


@dataclass(frozen=True)
class Строка:
    блюдо: str
    ккал_100: float
    ги: float
    оценка: Блюдо | None
    причина: str = ""  # почему оценки нет: «не еда» либо текст негодного ответа


@dataclass(frozen=True)
class Итог:
    строки: list[Строка]

    @property
    def оценённые(self) -> list[Строка]:
        return [с for с in self.строки if с.оценка is not None]

    @property
    def блюд(self) -> int:
        return len(self.строки)

    @property
    def без_оценки(self) -> int:
        return self.блюд - len(self.оценённые)

    @property
    def ошибка_ккал(self) -> float | None:
        """Средняя относительная ошибка калорийности, %."""
        с = self.оценённые
        return sum(abs(x.оценка.ккал_100 - x.ккал_100) / x.ккал_100 for x in с) * 100 / len(с) if с else None

    @property
    def ошибка_ги(self) -> float | None:
        """Средняя абсолютная ошибка гликемического индекса, единиц."""
        с = self.оценённые
        return sum(abs(x.оценка.ги - x.ги) for x in с) / len(с) if с else None

    @property
    def разряд_верен(self) -> float | None:
        """Доля блюд с верным разрядом гликемического индекса."""
        с = self.оценённые
        return sum(x.оценка.разряд == разряд(x.ги) for x in с) / len(с) if с else None


def прочитать(путь: str | Path) -> list[tuple[str, float, float]]:
    """Весь набор читается и сверяется до первого запроса к модели."""
    try:
        текст = Path(путь).read_text(encoding="utf-8")
    except OSError as ошибка:
        raise НаборНегоден(f"{путь}: {ошибка.strerror or ошибка}") from ошибка
    набор = []
    for номер, сырая in enumerate(текст.splitlines(), 1):
        if not сырая.strip():
            continue
        try:
            запись = json.loads(сырая)
            блюдо = запись["блюдо"]
            ккал, ги = запись["ккал_100"], запись["ги"]
        except (ValueError, KeyError, TypeError) as ошибка:
            raise НаборНегоден(f"{путь}, строка {номер}: {type(ошибка).__name__}: {ошибка}") from ошибка
        числа = all(isinstance(ч, (int, float)) and not isinstance(ч, bool) for ч in (ккал, ги))
        if not isinstance(блюдо, str) or not блюдо.strip() or not числа or ккал <= 0 or ги < 0:
            raise НаборНегоден(
                f"{путь}, строка {номер}: нужны название, ккал_100 больше 0 и ги не меньше 0"
            )
        набор.append((блюдо.strip(), float(ккал), float(ги)))
    if not набор:
        raise НаборНегоден(f"{путь}: в наборе нет ни одной строки")
    return набор


def замерить(путь: str | Path, **kw) -> Итог:
    строки = []
    for блюдо, ккал, ги in прочитать(путь):
        try:
            оценка = оценить(блюдо, **kw)
            причина = "" if оценка else "не еда"
        except ОтветНегоден as ошибка:
            оценка, причина = None, str(ошибка)
        строки.append(Строка(блюдо, ккал, ги, оценка, причина))
    return Итог(строки)


def показать(итог: Итог) -> str:
    ширина = max([len(с.блюдо) for с in итог.строки] + [5])
    вывод = [f"{'блюдо':<{ширина}}  ккал: справка → модель      ГИ: справка → модель   разряд"]
    for с in итог.строки:
        if с.оценка is None:
            вывод.append(f"{с.блюдо:<{ширина}}  без оценки: {с.причина}")
            continue
        о = с.оценка
        метка = "верен" if о.разряд == разряд(с.ги) else f"{разряд(с.ги)} → {о.разряд}"
        вывод.append(
            f"{с.блюдо:<{ширина}}  {с.ккал_100:>6.0f} → {о.ккал_100:<6.0f} ({(о.ккал_100 - с.ккал_100) / с.ккал_100 * 100:+5.0f} %)   "
            f"{с.ги:>4.0f} → {о.ги:<4.0f} ({о.ги - с.ги:+4.0f})   {метка}"
        )
    вывод.append("")
    вывод.append(f"Блюд в наборе: {итог.блюд}, без оценки: {итог.без_оценки}")
    if итог.оценённые:
        вывод.append(f"Средняя ошибка калорийности: {итог.ошибка_ккал:.1f} %")
        вывод.append(f"Средняя ошибка гликемического индекса: {итог.ошибка_ги:.1f} единиц")
        вывод.append(f"Разряд индекса верен: {итог.разряд_верен * 100:.0f} % блюд")
    return "\n".join(вывод)
