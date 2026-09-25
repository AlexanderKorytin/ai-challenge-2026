"""Сверка следа одного обмена: выбор инструментов, порядок вызовов и перенос данных между серверами.

Читает запись обмена из журнала `myharness` (последнюю с полем `звенья` или по `--run-id`),
выписывает вызовы по порядку с сервером и доводами и сверяет их со сценарием «справка по юаню».

Сверяется частичный порядок — только причинные зависимости, а не точная последовательность:
модель вправе поменять местами `get_rate` и `search` или вызвать их одним кругом.

    python3 след.py [--журнал ПУТЬ] [--run-id ID]

Код выхода: 0 — всё сошлось, 1 — есть провал, 2 — нечего сверять.
"""

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

ОБЯЗАТЕЛЬНЫЕ = [
    ("cbr", "get_rate"),
    ("cbr", "get_rate_dynamics"),
    ("pipeline", "search"),
    ("pipeline", "summarize"),
    ("pipeline", "saveToFile"),
    ("filesystem", "write_file"),
    ("git", "git_add"),
    ("git", "git_commit"),
]
СЕРВЕРЫ = {"cbr", "pipeline", "filesystem", "git"}
# Причинные рёбра: левый инструмент обязан отработать успешно раньше правого. Рёбра к git_add
# сверяются отдельно — по добавлению именно справки и выжимки, а не по первому git_add.
РЁБРА = [
    ("search", "summarize"),
    ("summarize", "saveToFile"),
    ("get_rate", "write_file"),
    ("get_rate_dynamics", "write_file"),
    ("summarize", "write_file"),
]


@dataclass
class Вызов:
    n: int
    сервер: str
    инструмент: str
    доводы: dict
    результат: str
    ошибка: bool


def прочитать_запись(журнал: Path, run_id: str | None) -> dict | None:
    найдена = None
    for строка in журнал.read_text(encoding="utf-8").splitlines():
        try:
            запись = json.loads(строка)
        except json.JSONDecodeError:
            continue
        if not запись.get("звенья"):
            continue
        if run_id is None or запись.get("run_id") == run_id:
            найдена = запись
    return найдена


def разобрать(звенья: list[dict]) -> list[Вызов]:
    результаты = {з["tool_call_id"]: з.get("content") or "" for з in звенья if з.get("role") == "tool"}
    вызовы: list[Вызов] = []
    for звено in звенья:
        for в in звено.get("tool_calls") or []:
            имя = в["function"]["name"]
            _, сервер, инструмент = (имя.split("__", 2) + ["", ""])[:3] if имя.startswith("mcp__") else ("", "", имя)
            try:
                доводы = json.loads(в["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                доводы = {}
            результат = результаты.get(в["id"], "")
            вызовы.append(
                Вызов(len(вызовы) + 1, сервер, инструмент, доводы, результат, результат.startswith("ошибка инструмента"))
            )
    return вызовы


def как_json(текст: str) -> dict:
    try:
        значение = json.loads(текст)
    except json.JSONDecodeError:
        return {}
    return значение if isinstance(значение, dict) else {}


def сверить(запись: dict, база: Path) -> int:
    """`база` — каталог, от которого сервер filesystem разрешает относительный путь: рабочий
    каталог `myharness`, он же каталог журнала."""
    вызовы = разобрать(запись["звенья"])
    print(f"СЛЕД run_id={запись.get('run_id')}  вызовов: {len(вызовы)}")
    for в in вызовы:
        доводы = json.dumps(в.доводы, ensure_ascii=False)
        доводы = доводы if len(доводы) <= 90 else доводы[:89] + "…"
        print(f"{в.n:>3}  {'✗' if в.ошибка else '✓'}  {в.сервер:<10} {в.инструмент:<26} {доводы}")

    провалы = 0

    def да(название: str, условие: bool) -> None:
        nonlocal провалы
        провалы += not условие
        print(f"{'ок    ' if условие else 'ПРОВАЛ'}  {название}")

    успешные = [в for в in вызовы if not в.ошибка]
    первый: dict[str, Вызов] = {}
    for в in успешные:
        первый.setdefault(в.инструмент, в)
    # Фиксаций может быть несколько; в итоге лежит всё, что добавлено до последней.
    commit = next((в for в in reversed(успешные) if в.инструмент == "git_commit"), None)
    # Всё, что после последней фиксации, в неё не попало и справкой не считается.
    до_фиксации = [в for в in успешные if commit is None or в.n < commit.n]

    # Добавленное — объединение всех успешных git_add до фиксации: модель вправе добавлять файлы
    # по одному. Сервер git разрешает `files` от `repo_path`; добавленный каталог (в том числе `.`)
    # покрывает всё, что под ним.
    добавления = [в for в in до_фиксации if в.инструмент == "git_add"]
    добавлено: list[tuple[Path, int]] = []
    for в in добавления:
        корень = (база / (в.доводы.get("repo_path") or "")).resolve()
        добавлено += [((корень / ф).resolve(), в.n) for ф in в.доводы.get("files") or []]

    def когда_добавлен(путь: Path | None) -> int | None:
        """Номер последнего git_add, взявшего этот файл сам или каталогом над ним."""
        if путь is None:
            return None
        номера = [n for добавленный, n in добавлено if путь == добавленный or добавленный in путь.parents]
        return max(номера, default=None)

    def путь_записи(в: Вызов) -> Path:
        путь = Path(в.доводы.get("path", ""))
        return (путь if путь.is_absolute() else база / путь).resolve()

    # Справка — последняя успешная запись write_file до фиксации, чей путь вошёл в git_add
    # после неё: именно эта версия и зафиксирована.
    записи = [в for в in до_фиксации if в.инструмент == "write_file"]
    справка = next(
        (в for в in reversed(записи) if (к := когда_добавлен(путь_записи(в))) is not None and к > в.n),
        None,
    )
    save = первый.get("saveToFile")
    путь_выжимки = Path(как_json(save.результат).get("path", "")).resolve() if save else None

    print("\n1. ВЫБОР")
    for сервер, инструмент in ОБЯЗАТЕЛЬНЫЕ:
        в = первый.get(инструмент)
        да(f"{сервер}.{инструмент} вызван успешно", в is not None and в.сервер == сервер)
    чужие = {в.сервер for в in вызовы} - СЕРВЕРЫ
    да(f"посторонних серверов нет{': ' + ', '.join(sorted(чужие)) if чужие else ''}", not чужие)
    да("справку записал write_file, и она вошла в фиксацию", справка is not None)
    да("выжимку записал saveToFile, а не write_file",
       путь_выжимки is not None and all(путь_записи(в) != путь_выжимки for в in записи))

    print("\n2. ПОРЯДОК (причинные рёбра)")
    позиция = {и: в.n for и, в in первый.items()}
    if справка is not None:
        позиция["write_file"] = справка.n
    for левый, правый in РЁБРА:
        л, п = позиция.get(левый), позиция.get(правый)
        да(f"{левый} → {правый}", л is not None and п is not None and л < п)
    к_справке, к_выжимке = когда_добавлен(путь_записи(справка) if справка else None), когда_добавлен(путь_выжимки)
    да("write_file → git_add справки", справка is not None and к_справке is not None and справка.n < к_справке)
    да("saveToFile → git_add выжимки", save is not None and к_выжимке is not None and save.n < к_выжимке)
    да("git_add → git_commit", commit is not None and bool(добавления))

    print("\n3. ПЕРЕНОС ДАННЫХ")
    search, summarize, rate = первый.get("search"), первый.get("summarize"), первый.get("get_rate")
    search_id = как_json(search.результат).get("search_id") if search else None
    да("summarize получил search_id из ответа search",
       bool(search_id) and summarize is not None and summarize.доводы.get("search_id") == search_id)
    summary_id = как_json(summarize.результат).get("summary_id") if summarize else None
    да("saveToFile получил summary_id из ответа summarize",
       bool(summary_id) and save is not None and save.доводы.get("summary_id") == summary_id)
    да(f"git_add взял справку {путь_записи(справка) if справка else '—'}", к_справке is not None)
    да(f"git_add взял выжимку {путь_выжимки or '—'}", к_выжимке is not None)

    # Курс за единицу — `unit_rate` из ответа get_rate (данные сервера приходят JSON). Модель
    # пишет его как сервер, до копеек или до четырёх знаков ЦБ, точкой или запятой. Ищется целым
    # числом: `12.65` не должно совпасть с `112.65` или `12.651`.
    курс = как_json(rate.результат).get("unit_rate") if rate else None
    if isinstance(курс, (int, float)) and справка is not None:
        виды = {вид for к in (repr(float(курс)), f"{курс:.2f}", f"{курс:.4f}") for вид in (к, к.replace(".", ","))}
        текст = справка.доводы.get("content", "")
        найден = any(re.search(rf"(?<![\d.,]){re.escape(вид)}(?![\d])", текст) for вид in виды)
        да(f"в справке курс ЦБ {курс}", найден)
    else:
        да("в справке курс ЦБ", False)

    print("\n4. ИТОГ")
    хэш = re.search(r"\b([0-9a-f]{7,40})\b", commit.результат) if commit else None
    да("git_commit вернул хэш", хэш is not None)
    ответ = запись.get("response") or ""
    да("хэш фиксации назван в ответе", хэш is not None and хэш.group(1)[:7] in ответ)

    print(f"\n{'ВСЁ СОШЛОСЬ' if not провалы else f'ПРОВАЛОВ: {провалы}'}")
    return 1 if провалы else 0


def main() -> int:
    разбор = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    разбор.add_argument("--журнал", type=Path, default=Path("myharness-journal.jsonl"))
    разбор.add_argument("--run-id")
    доводы = разбор.parse_args()
    if not доводы.журнал.exists():
        print(f"журнала нет: {доводы.журнал}")
        return 2
    запись = прочитать_запись(доводы.журнал, доводы.run_id)
    if запись is None:
        print("в журнале нет обмена с вызовами инструментов" + (f" и run_id {доводы.run_id}" if доводы.run_id else ""))
        return 2
    return сверить(запись, доводы.журнал.resolve().parent)


if __name__ == "__main__":
    sys.exit(main())
