"""Сравнение ответов с RAG и без RAG по журналу наряда `myharness`.

Наряд — по заданию на вопрос и режим, имя задания `q<NN>-rag` либо `q<NN>-plain`, все с одним
`run_id`. Контрольный набор — `rag/answers.jsonl`: `n`, `вопрос`, `ожидание`, `факты`,
`источники`.

Мера по вопросу и режиму:

- **факты** — доля строк `факты`, найденных в ответе подстрокой; сравнение без учёта регистра и
  без пробелов (`30 000` и `30000` — одно число);
- **источник найден** (только RAG) — хоть один путь из `источники` стоит в строках `[n] <путь> ›`
  блока найденного в отправленном последнем сообщении (формат — договор `server.py`);
- **источник назван** — хоть один путь из `источники` назван в тексте ответа.

Задание режима RAG, в отправленном сообщении которого нет блока найденного, помечается «поиск не
состоялся» и в средние режима не идёт: ответ без базы, засчитанный режиму RAG, врал бы в его
пользу или против.

Запуск: `uv run --project rag rag/eval.py --journal <журнал> --run <run_id> --out <файл.md>`.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

КОРЕНЬ = Path(__file__).resolve().parent.parent
НАБОР = КОРЕНЬ / "rag" / "answers.jsonl"
ЗАГОЛОВОК_НАЙДЕННОГО = "Найдено в базе знаний по вопросу:"  # myharness.agent.ЗАГОЛОВОК_НАЙДЕННОГО
СТРОКА_КУСКА = re.compile(r"^\[(\d+)\] (.+?) › ", re.M)
РЕЖИМЫ = ("plain", "rag")


def _сжать(текст: str) -> str:
    return re.sub(r"\s+", "", текст).casefold()


def доля_фактов(ответ: str, факты: list[str]) -> float | None:
    if not факты:
        return None
    сжатый = _сжать(ответ)
    return sum(_сжать(ф) in сжатый for ф in факты) / len(факты)


def найденные_источники(отправленное: str) -> list[str] | None:
    """Пути кусков блока найденного; `None` — блока нет, поиска не было."""
    if not отправленное.startswith(ЗАГОЛОВОК_НАЙДЕННОГО):
        return None
    return [путь for _, путь in СТРОКА_КУСКА.findall(отправленное)]


def загрузить(журнал: Path, run_id: str) -> dict[tuple[int, str], dict]:
    записи: dict[tuple[int, str], dict] = {}
    for строка in журнал.read_text("utf-8").splitlines():
        if not строка.strip():
            continue
        запись = json.loads(строка)
        имя = запись.get("agent") or ""
        совпало = re.fullmatch(r"q(\d+)-(rag|plain)", имя)
        if запись.get("run_id") != run_id or not совпало:
            continue
        записи[(int(совпало[1]), совпало[2])] = запись
    return записи


def оценить(набор: list[dict], записи: dict[tuple[int, str], dict]) -> list[dict]:
    итог = []
    for вопрос in набор:
        строка = {"вопрос": вопрос}
        for режим in РЕЖИМЫ:
            запись = записи.get((вопрос["n"], режим))
            if запись is None:
                строка[режим] = None
                continue
            ответ = запись.get("response") or ""
            отправленное = (запись.get("messages") or [{}])[-1].get("content") or ""
            найдено = найденные_источники(отправленное) if режим == "rag" else None
            строка[режим] = {
                "статус": запись.get("status"),
                "ответ": ответ,
                "факты": доля_фактов(ответ, вопрос["факты"]),
                "поиск_был": найдено is not None,
                "найдено": найдено or [],
                "источник_найден": bool(set(вопрос["источники"]) & set(найдено or [])) if вопрос["источники"] else None,
                "источник_назван": any(и in ответ for и in вопрос["источники"]) if вопрос["источники"] else None,
                "токены": (запись.get("usage") or {}).get("total_tokens"),
            }
        итог.append(строка)
    return итог


def _доля(значение: float | None) -> str:
    return "—" if значение is None else f"{значение:.2f}"


def _да(значение: bool | None) -> str:
    return "—" if значение is None else ("да" if значение else "нет")


def отчёт(оценки: list[dict], run_id: str) -> str:
    строки = [f"# Сравнение: ответ с RAG и без RAG\n", f"Наряд `run_id` = `{run_id}`.\n",
              "| # | вопрос | факты без RAG | факты с RAG | источник найден | источник назван с RAG |",
              "|---|---|---|---|---|---|"]
    средние: dict[str, list[float]] = {р: [] for р in РЕЖИМЫ}
    без_поиска = []
    for о in оценки:
        в = о["вопрос"]
        plain, rag = о["plain"], о["rag"]
        if rag is not None and not rag["поиск_был"]:
            без_поиска.append(в["n"])
        for режим, данные in (("plain", plain), ("rag", rag)):
            if данные and данные["факты"] is not None and (режим == "plain" or данные["поиск_был"]):
                средние[режим].append(данные["факты"])
        if rag is None:
            факты_rag = "нет записи"
        else:
            факты_rag = _доля(rag["факты"]) if rag["поиск_был"] else "поиск не состоялся"
        строки.append(f"| {в['n']} | {в['вопрос']} | {_доля(plain['факты']) if plain else 'нет записи'} | {факты_rag} | "
                      f"{_да(rag and rag['источник_найден'])} | {_да(rag and rag['источник_назван'])} |")
    строки.append("")
    for режим, подпись in (("plain", "без RAG"), ("rag", "с RAG")):
        значения = средние[режим]
        среднее = sum(значения) / len(значения) if значения else None
        строки.append(f"- средняя доля фактов {подпись}: **{_доля(среднее)}** по {len(значения)} вопросам с фактами")
    if без_поиска:
        строки.append(f"- **поиск не состоялся** в вопросах: {', '.join(map(str, без_поиска))} — в среднее RAG не вошли")
    строки.append("\n## Ответы\n")
    for о in оценки:
        в = о["вопрос"]
        строки.append(f"### {в['n']}. {в['вопрос']}\n")
        строки.append(f"**Ожидание:** {в['ожидание']}  ")
        строки.append(f"**Факты:** {', '.join(f'`{ф}`' for ф in в['факты']) or '—'}  ")
        строки.append(f"**Источники:** {', '.join(f'`{и}`' for и in в['источники']) or '— (не применимо)'}\n")
        for режим, подпись in (("plain", "Без RAG"), ("rag", "С RAG")):
            данные = о[режим]
            if данные is None:
                строки.append(f"**{подпись}:** записи в журнале нет\n")
                continue
            добавка = ""
            if режим == "rag":
                добавка = f"; найдено: {', '.join(f'`{п}`' for п in данные['найдено']) or 'поиск не состоялся'}"
            строки.append(f"**{подпись}** (факты {_доля(данные['факты'])}{добавка}):\n")
            строки.append("> " + данные["ответ"].strip().replace("\n", "\n> ") + "\n")
    return "\n".join(строки) + "\n"


def главная(argv: list[str] | None = None) -> None:
    разбор = argparse.ArgumentParser(prog="eval", description="Сравнение ответов с RAG и без по журналу наряда")
    разбор.add_argument("--journal", type=Path, required=True)
    разбор.add_argument("--run", required=True, help="run_id наряда")
    разбор.add_argument("--answers", type=Path, default=НАБОР)
    разбор.add_argument("--out", type=Path)
    доводы = разбор.parse_args(argv)
    набор = [json.loads(с) for с in доводы.answers.read_text("utf-8").splitlines() if с.strip()]
    записи = загрузить(доводы.journal, доводы.run)
    if not записи:
        sys.exit(f"eval: в {доводы.journal} нет записей наряда {доводы.run} с именами q<NN>-rag|plain")
    текст = отчёт(оценить(набор, записи), доводы.run)
    if доводы.out:
        доводы.out.write_text(текст, "utf-8")
        print(f"записано: {доводы.out}")
    else:
        print(текст)


if __name__ == "__main__":
    главная()
