"""Задания по расписанию: неделя — будущие выпуски в календарь, день — предложения и скидки.

Ход ведёт программа: читает критерии и запреты из карточки, ищет по меткам, отсеивает запреты
и уже показанные игры и только потом спрашивает местную модель об одной игре за раз. Запрет
отсеивает программа ДО модели — это гарантия; модель видит запреты в запросе как довод.

Запуск: `python -m jobs week` либо `python -m jobs day`, рабочий каталог — каталог агента
(`GAMES_DATA_DIR`): карточка `myharness` берётся по папке. Любой сбой — запись «сбой» в ленте,
уведомление macOS и код выхода 1: агент без человека не молчит неделю.
"""

from __future__ import annotations

import asyncio
import os
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path

import games_db
import steam_store as ss
from myharness import api, config

import judge
import taste

Судья = Callable[[ss.Игра], Awaitable[judge.Вердикт]]


@dataclass
class Магазин:
    """Магазин заданий. В проверках подменяется: сети у них нет."""

    найти: Callable[[int, bool], Awaitable[list[int]]]
    игра: Callable[[int], Awaitable[ss.Игра | None]]


@dataclass
class Итог:
    найдено: int = 0
    запрещено: int = 0
    показаны: int = 0
    не_игры: int = 0
    судимо: int = 0
    подошло: list[str] = field(default_factory=list)
    ошибки_разбора: list[str] = field(default_factory=list)
    жалобы_критериев: list[str] = field(default_factory=list)

    def текст(self, имя: str) -> str:
        части = [
            f"{имя}: найдено {self.найдено}, отсеяно запретами {self.запрещено}, "
            f"уже показаны {self.показаны}, не игры либо не та дата {self.не_игры}, "
            f"судимо {self.судимо}, подошло {len(self.подошло)}"
        ]
        if self.подошло:
            части.append("подошли: " + ", ".join(self.подошло))
        if self.ошибки_разбора:
            части.append(f"ответ модели не разобран {len(self.ошибки_разбора)} раз: {self.ошибки_разбора[0]}")
        if self.жалобы_критериев:
            части.append("строки «Критериев» не того вида пропущены: " + "; ".join(self.жалобы_критериев))
        return ". ".join(части)


def уведомить(текст: str) -> None:
    """Системное уведомление macOS. Сбой показа не роняет задание: запись в ленте уже есть."""
    строка = текст.replace("\\", "\\\\").replace('"', '\\"')
    try:
        subprocess.run(
            ["osascript", "-e", f'display notification "{строка}" with title "Игровой агент"'],
            check=False, capture_output=True, timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        pass


async def _отобрать(
    каталог: Path, с, магазин: Магазин, судья: Судья, *, будущие: bool
) -> Итог:
    итог = Итог()
    критерии, итог.жалобы_критериев = taste.критерии(каталог)
    if not критерии:
        raise RuntimeError("в карточке нет «Критериев»: опрос вкуса не пройден")
    запреты = taste.запреты(каталог)
    показанные = games_db.показанные(с)

    номера: list[int] = []
    for критерий in sorted(критерии, key=lambda к: -к.вес):
        for номер in await магазин.найти(критерий.метка, будущие):
            if номер not in номера:
                номера.append(номер)
    итог.найдено = len(номера)

    for номер in номера:
        if номер in запреты:
            итог.запрещено += 1
            continue
        if номер in показанные:
            итог.показаны += 1
            continue
        игра = await магазин.игра(номер)
        # Выдача «скоро выйдет» полна демоверсий, а вышедшая игра в ней — устаревшая выдача.
        if игра is None or игра.род != "game" or игра.вышла == будущие:
            итог.не_игры += 1
            continue
        games_db.запомнить_игру(с, игра)
        итог.судимо += 1
        try:
            вердикт = await судья(игра)
        except ValueError as exc:
            # Негодный ответ на одну игру — не повод терять остальные; число видно в итоге.
            итог.ошибки_разбора.append(f"{игра.имя}: {exc}")
            continue
        if not вердикт.подходит:
            continue
        записано = (
            games_db.в_календарь(с, номер, вердикт.причина)
            if будущие
            else games_db.предложить(с, номер, вердикт.причина)
        )
        if записано:
            итог.подошло.append(игра.имя)
    return итог


def _сумма(копейки: int, валюта: str) -> str:
    # Регион `cc=ru` даёт рубли; другая валюта подписывается своим кодом, а не «руб.».
    return f"{копейки / 100:.0f} руб." if валюта in ("RUB", "") else f"{копейки / 100:.2f} {валюта}"


async def _скидки(с, магазин: Магазин) -> list[str]:
    """Новые скидки на желаемое: запись в ленте и уведомление на каждую.

    Сбой одной игры не останавливает проверку остальных: однодневная скидка на соседнюю игру
    дороже порядка. Сбои собираются одной строкой «сбой» в ленте."""
    новые: list[str] = []
    сбои: list[str] = []
    for запись in games_db.желаемое(с):
        try:
            игра = await магазин.игра(int(запись["номер"]))
        except ss.ОшибкаМагазина as exc:
            сбои.append(f"{запись.get('имя') or запись['номер']}: {exc}")
            continue
        if игра is None or игра.цена is None:
            continue
        games_db.запомнить_игру(с, игра)
        текст = (
            f"Скидка {игра.скидка} % на «{игра.имя}»: {_сумма(игра.цена, игра.валюта)} "
            f"вместо {_сумма(игра.цена_без_скидки or игра.цена, игра.валюта)}"
        )
        # Отметка цены и строка ленты — одной транзакцией (`games_db.отметить_цену`).
        if games_db.отметить_цену(с, игра.номер, игра.цена, игра.скидка, текст):
            уведомить(текст)
            новые.append(текст)
    if сбои:
        текст = "Проверка скидок не дошла до игр: " + "; ".join(сбои)
        games_db.в_ленту(с, "сбой", текст)
        уведомить(текст)
    return новые


async def неделя(каталог: Path, магазин: Магазин, судья: Судья) -> Итог:
    с = games_db.открыть()
    try:
        итог = await _отобрать(каталог, с, магазин, судья, будущие=True)
        games_db.в_ленту(с, "итог", итог.текст("Неделя, будущие выпуски"))
        return итог
    finally:
        с.close()


async def день(каталог: Path, магазин: Магазин, судья: Судья) -> Итог:
    с = games_db.открыть()
    try:
        # Скидки — первыми и отдельно от отбора: сбой отбора (магазин отказал на сотой игре,
        # служба Ollama не поднята) не должен стоить человеку скидки (находка просмотра).
        try:
            await _скидки(с, магазин)
        except Exception as exc:
            текст = f"Проверка скидок упала: {type(exc).__name__}: {exc}"
            games_db.в_ленту(с, "сбой", текст)
            уведомить(текст)
        итог = await _отобрать(каталог, с, магазин, судья, будущие=False)
        games_db.в_ленту(с, "итог", итог.текст("День, вышедшие игры"))
        return итог
    finally:
        с.close()


async def запустить(род: str, каталог: Path | None = None) -> Итог:
    """Настоящий запуск: магазин Steam и местная модель. Сбой — в ленту, затем наверх."""
    каталог = каталог or Path.cwd()
    клиент_модели = api.Clients(config.load().api_key)
    try:
        async with ss.клиент() as к:
            магазин = Магазин(
                найти=lambda метка, будущие: ss.найти(к, метка, будущие=будущие),
                игра=lambda номер: ss.игра(к, номер),
            )

            async def судья(игра: ss.Игра) -> judge.Вердикт:
                return await judge.судить(клиент_модели, каталог, игра)

            задание = неделя if род == "week" else день
            return await задание(каталог, магазин, судья)
    except Exception as exc:
        текст = f"Задание «{'неделя' if род == 'week' else 'день'}» упало: {type(exc).__name__}: {exc}"
        try:
            с = games_db.открыть()
            try:
                games_db.в_ленту(с, "сбой", текст)
            finally:
                с.close()
        finally:
            уведомить(текст)
        raise
    finally:
        await клиент_модели.aclose()


def main(доводы: list[str]) -> int:
    if доводы not in (["week"], ["day"]):
        print("запуск: python -m jobs week|day", file=sys.stderr)
        return 2
    try:
        итог = asyncio.run(запустить(доводы[0]))
    except Exception as exc:  # запись в ленте и уведомление уже сделаны
        print(f"сбой: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    print(итог.текст("неделя" if доводы[0] == "week" else "день"))
    return 0


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUNBUFFERED", "1")
    sys.exit(main(sys.argv[1:]))
