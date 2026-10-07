"""Командная строка индекса: `index`, `search`, `compare`, `rerank-compare`.

Запуск из корня репозитория: `uv run --project rag rag/cli.py <команда>`.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

import numpy as np

import chunking as ч
import compare as ср
import embedder as эм
import rerank as рр
import rerank_compare as срр
import rewrite as пер
import store

КОРЕНЬ = Path(__file__).resolve().parent.parent
ИНДЕКС = КОРЕНЬ / "rag" / "index.sqlite"
НАБОР = КОРЕНЬ / "rag" / "control.jsonl"
ОТВЕТЫ = КОРЕНЬ / "rag" / "answers.jsonl"
ОТРИЦАТЕЛЬНЫЕ = КОРЕНЬ / "rag" / "negative.jsonl"
ПУТИ_КОРПУСА = ["CLAUDE.md", "openspec/specs", "docs"]
# Наборы, по которым меряются поиск и ответы. Их вопросы в корпусе стоять не должны.
НАБОРЫ_ЗАМЕРА = [НАБОР, ОТВЕТЫ, ОТРИЦАТЕЛЬНЫЕ, КОРЕНЬ / "rag" / "far.jsonl"]


def _сжать(текст: str) -> str:
    """Текст для сверки с вопросом: без разметки `*«»"' и обратных кавычек, пробелы сведены, регистр снят."""
    return re.sub(r"\s+", " ", re.sub(r"[`*«»\"']", "", текст)).casefold().strip()


def утечки(корпус: list[tuple[str, str]], вопросы: list[str]) -> list[tuple[str, str]]:
    """[(source, вопрос)] — документы корпуса, где дословно стоит вопрос набора замера.

    Беда дня 28: дизайн дня 22 пересказывал контрольные вопросы таблицей с фактами, таблица
    попала в индекс, и поиск возвращал модели сам вопрос с ответом — «источник найден» и доля
    фактов врали в пользу режима, которому попался этот кусок. Документ ссылается на вопрос
    номером (`q06`), а текст вопроса живёт только в файле набора."""
    сжатые = [(в, _сжать(в)) for в in вопросы]
    return [(source, в) for source, текст in корпус for в, с in сжатые if с and с in _сжать(текст)]


def вопросы_замера() -> list[str]:
    return [json.loads(с)["вопрос"] for путь in НАБОРЫ_ЗАМЕРА if путь.is_file()
            for с in путь.read_text("utf-8").splitlines() if с.strip()]


def разделить(кусок: ч.Кусок) -> tuple[ч.Кусок, ч.Кусок]:
    """Делит кусок надвое по ближайшему к середине `\\n\\n` (нет его — `\\n`, нет и его — по середине).
    Части сохраняют section и получают chunk_id `<id>.0` и `<id>.1`."""
    текст = кусок.text
    середина = len(текст) // 2
    разрез = середина
    for разделитель in ("\n\n", "\n"):
        места = [и + len(разделитель) for и in range(len(текст)) if текст.startswith(разделитель, и)]
        места = [м for м in места if 0 < м < len(текст)]
        if места:
            разрез = min(места, key=lambda м: abs(м - середина))
            break
    return (replace(кусок, chunk_id=f"{кусок.chunk_id}.0", end=кусок.start + разрез, text=текст[:разрез]),
            replace(кусок, chunk_id=f"{кусок.chunk_id}.1", start=кусок.start + разрез, text=текст[разрез:]))


def векторы(куски: list[ч.Кусок], считать=эм.эмбеддинги) -> tuple[list[ч.Кусок], np.ndarray]:
    """Эмбеддинги кусков одной просьбой; при переполнении окна — по одному, а переполнивший
    кусок делится по абзацам надвое и повторяется рекурсивно. Число токенов знает только модель."""
    try:
        return куски, считать([к.text for к in куски])
    except эм.ПереполнениеОкна:
        if len(куски) == 1:
            if len(куски[0].text) < 2:
                raise
            части = разделить(куски[0])
        else:
            части = куски
        итог = [векторы([к], считать) for к in части]
        return [к for список, _ in итог for к in список], np.concatenate([м for _, м in итог])


def _git(*доводы: str) -> str:
    return subprocess.run(["git", *доводы], cwd=КОРЕНЬ, capture_output=True, text=True, check=True).stdout.strip()


def собрать(strategy: str, куски: list[ч.Кусок], индекс: Path, fixed_size: int | None, считать=эм.эмбеддинги) -> dict:
    """Эмбеддинги по файлам (одна просьба на файл) и запись сборки стратегии в индекс."""
    начало = time.monotonic()
    по_файлам: dict[str, list[ч.Кусок]] = {}
    for к in куски:
        по_файлам.setdefault(к.source, []).append(к)
    все_куски: list[ч.Кусок] = []
    все_векторы = []
    for номер, (source, свои) in enumerate(по_файлам.items(), 1):
        print(f"\r{strategy}: файл {номер}/{len(по_файлам)}", end="", file=sys.stderr, flush=True)
        готовые, матрица = векторы(свои, считать)
        все_куски += готовые
        все_векторы.append(матрица)
    print(file=sys.stderr)
    матрица = np.concatenate(все_векторы)
    сведения = {"model": эм.МОДЕЛЬ, "dim": int(матрица.shape[1]), "commit_hash": _git("rev-parse", "HEAD"),
                "dirty": int(bool(_git("status", "--porcelain", "--", *ПУТИ_КОРПУСА))),
                "built_at": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
                "seconds": round(time.monotonic() - начало, 1), "chunks": len(все_куски), "fixed_size": fixed_size}
    store.записать(индекс, strategy, все_куски, матрица, сведения)
    return сведения


def команда_index(доводы) -> None:
    корпус = ч.корпус(КОРЕНЬ)
    найдено = утечки(корпус, вопросы_замера())
    if найдено:
        sys.exit("index: в корпусе стоят вопросы наборов замера — индекс вернул бы поиску сам вопрос с ответом:\n"
                 + "\n".join(f"  {source}: {в}" for source, в in найдено)
                 + "\nЗамените текст вопроса в документе номером (q06) и ссылкой на файл набора.")
    structure = [к for source, текст in корпус for к in ч.нарезать_structure(source, текст)]
    размер = ч.размер_fixed(structure)
    if доводы.strategy in ("structure", "all"):
        с = собрать("structure", structure, доводы.index, None)
        print(f"structure: {с['chunks']} кусков за {с['seconds']} с")
    if доводы.strategy in ("fixed", "all"):
        fixed = [к for source, текст in корпус for к in ч.нарезать_fixed(source, текст, размер)]
        с = собрать("fixed", fixed, доводы.index, размер)
        print(f"fixed (окно {размер}): {с['chunks']} кусков за {с['seconds']} с")


def команда_search(доводы) -> None:
    вектор = эм.эмбеддинги([доводы.вопрос])[0]
    for место, (близость, к) in enumerate(store.найти(доводы.index, доводы.strategy, вектор, доводы.k), 1):
        print(f"{место}. {близость:.3f}  {к.source}  ‹{к.section or к.title}›  [{к.chunk_id}]")
        for строка in к.text.strip().splitlines()[:3]:
            print(f"     {строка[:110]}")


def команда_compare(доводы) -> None:
    набор = ср.загрузить_набор(НАБОР, ч.корпус(КОРЕНЬ))
    сборки = store.стратегии(доводы.index)
    данные = {s: store.куски(доводы.index, s) for s in сборки}
    вопросы = store.нормировать(эм.эмбеддинги([з["вопрос"] for з in набор]))
    текст = ср.отчёт(сборки, данные, набор, вопросы)
    if доводы.out:
        доводы.out.write_text(текст, "utf-8")
        print(f"записано: {доводы.out}")
    else:
        print(текст)


def команда_rerank_compare(доводы) -> None:
    if (доводы.k1 is None) != (доводы.k2 is None):
        raise ValueError("--k1 и --k2 задаются вместе")
    if (доводы.tau is None) != (доводы.theta is None) or (доводы.tau is not None and доводы.k1 is None):
        raise ValueError("--tau и --theta задаются вместе и только с --k1 и --k2")
    вопросы = срр.загрузить(НАБОР, ОТВЕТЫ, ОТРИЦАТЕЛЬНЫЕ)
    переписанные = срр.переписанные(вопросы, доводы.rewrites, пер.переписать)
    # Кодировщик грузится при первой паре, а не при создании: сетка без model его не трогает.
    кодировщик = None

    def оценить(вопрос: str, тексты: list[str]) -> list[float]:
        nonlocal кодировщик
        кодировщик = кодировщик or рр.кодировщик()
        return кодировщик(вопрос, тексты)

    поиск = рр.Поиск(доводы.index, считать=срр.с_памятью_векторов(эм.эмбеддинги),
                     оценщик=срр.с_памятью_оценок(оценить))
    текст = срр.отчёт(срр.Замер(поиск, вопросы, переписанные), доводы.k1, доводы.k2, доводы.tau, доводы.theta,
                      доводы.reason)
    доводы.out.write_text(текст, "utf-8")
    print(f"записано: {доводы.out}")


def главная(argv: list[str] | None = None) -> None:
    разбор = argparse.ArgumentParser(prog="rag", description="Индекс документов проекта")
    разбор.add_argument("--index", type=Path, default=ИНДЕКС, help=f"файл индекса (по умолчанию {ИНДЕКС})")
    команды = разбор.add_subparsers(dest="команда", required=True)
    и = команды.add_parser("index", help="нарезать корпус, посчитать эмбеддинги, записать индекс")
    и.add_argument("--strategy", choices=["structure", "fixed", "all"], default="all")
    и.set_defaults(действие=команда_index)
    п = команды.add_parser("search", help="найти куски по вопросу")
    п.add_argument("вопрос")
    п.add_argument("--strategy", choices=["structure", "fixed"], default="structure")
    п.add_argument("-k", type=int, default=5)
    п.set_defaults(действие=команда_search)
    с = команды.add_parser("compare", help="сравнить стратегии на контрольном наборе")
    с.add_argument("--out", type=Path)
    с.set_defaults(действие=команда_compare)
    р = команды.add_parser("rerank-compare", help="сравнить режимы второго этапа поиска")
    р.add_argument("--out", type=Path, required=True)
    р.add_argument("--rewrites", type=Path, required=True,
                   help="файл переписанных запросов: читается, недостающие дописываются")
    р.add_argument("--k1", type=int, help="кандидатов до второго этапа; с --k2 — кривые порогов")
    р.add_argument("--k2", type=int, help="кусков после второго этапа")
    р.add_argument("--tau", type=float, help="порог косинуса threshold/heuristic; с --theta — итог и разбор")
    р.add_argument("--theta", type=float, help="порог вероятности model")
    р.add_argument("--reason", default="", help="почему выбраны эти числа — строкой в отчёт")
    р.set_defaults(действие=команда_rerank_compare)
    доводы = разбор.parse_args(argv)
    try:
        доводы.действие(доводы)
    except (эм.ОшибкаЭмбеддера, пер.ОшибкаПереписывания, LookupError, ValueError) as e:
        sys.exit(f"rag: {e}")


if __name__ == "__main__":
    главная()
