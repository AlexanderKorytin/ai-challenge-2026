"""Прогон одного набора настроек: модель Ollama, профиль, наряд `myharness --batch`, ресурсы.

Запуск из папки дня: `uv run наряд.py <настройка> [--прогон N] [--набор подбор|итог|все] [--сценарий]`.

Набор настроек — ключ `настройки.json`:

    {"основа": "qwen3.5:9b-q4_K_M", "окно": 8192, "инструкция": "хозяин.md",
     "params": {"temperature": 0.7, "thinking": {"type": "disabled"}, "max_tokens": 600}}

`окно: null` — модель берётся как есть, окно задаёт служба. Иначе создаётся модель
`tavern:<квант>-<окно>` файлом модели из двух строк: окно у Ollama задаёт только файл модели.
Ключа нет в `params` — параметр модели не уходит, действует её умолчание.

Имя задания — `<настройка>.<прогон>-<задание>`, задание `q01`…`q25` либо `party` (сценарий
партии): по нему `оценка.py` находит записи журнала. После прогона в `test/ресурсы.jsonl`
дописывается строка с памятью модели по ответу службы `GET /api/ps`.

Ведущий работает без облака: каталог настроек прогона — `test/настройки-без-ключа`, ключа
DeepSeek в нём нет. В партии сервер поиска разворачивает уточняющую реплику по контексту
разговора; модель разворота — модель самого прогона (`RAG_REWRITE_MODEL`): без этого сервер
пошёл бы в DeepSeek, не нашёл бы ключа и молча отдал пустое найденное. Прочие `RAG_*` оболочки
в прогон не идут: поиск задаёт только `test/.mcp.json`. Пределов ожидания и числа попыток
программа не заводит.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

ПАПКА = Path(__file__).resolve().parent
ПЕСОЧНИЦА = ПАПКА / "test"
НАСТРОЙКИ = ПАПКА / "настройки.json"
НАБОР = ПАПКА / "контроль.jsonl"
СЦЕНАРИЙ = ПАПКА / "сценарий.json"
ИНСТРУКЦИИ = ПАПКА / "инструкции"
ЖУРНАЛ = ПЕСОЧНИЦА / "myharness-journal.jsonl"
РЕСУРСЫ = ПЕСОЧНИЦА / "ресурсы.jsonl"
MYHARNESS = Path.home() / "challenge" / "main" / "myharness"
СЕРВЕР_ПОИСКА = "rules"  # имя из test/.mcp.json
МОДЕЛЬ_ВЕКТОРОВ = "bge-m3"
ИМЯ_НАСТРОЙКИ = re.compile(r"^[a-z0-9-]+$")
КВАНТ = re.compile(r"-(q\d+)")


def служба(путь: str) -> dict:
    хост = os.environ.get("OLLAMA_HOST") or "127.0.0.1:11434"
    адрес = хост if "://" in хост else f"http://{хост}"
    # Местная служба: посредник из окружения к ней не нужен.
    открыватель = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with открыватель.open(f"{адрес}{путь}") as ответ:
        return json.load(ответ)


def имя_модели(основа: str, окно: int | None) -> str:
    """Имя модели Ollama набора настроек; с окном — `tavern:<квант>-<окно>`."""
    if окно is None:
        return основа
    квант = КВАНТ.search(основа)
    if not квант:
        raise ValueError(f"в имени основы {основа!r} нет сжатия вида -q4")
    return f"tavern:{квант.group(1)}-{окно}"


def имя_задания(настройка: str, прогон: int, задание: str) -> str:
    return f"{настройка}.{прогон}-{задание}"


def задания(настройка: str, прогон: int, набор: str | None, сценарий: bool) -> list[dict]:
    итог = []
    if набор:
        for строка in НАБОР.read_text("utf-8").splitlines():
            запись = json.loads(строка)
            if набор in ("все", запись["набор"]):
                итог.append({"agent": имя_задания(настройка, прогон, f"q{запись['n']:02d}"),
                             "profile": настройка, "ask": запись["реплика"]})
    if сценарий:
        итог.append({"agent": имя_задания(настройка, прогон, "party"), "profile": настройка,
                     "asks": json.loads(СЦЕНАРИЙ.read_text("utf-8"))["asks"]})
    return итог


def профиль(настройка: str, описание: dict) -> dict:
    return {"name": настройка, "description": f"день 29, набор настроек {настройка}",
            "system_file": f"{настройка}.md", "rag": СЕРВЕР_ПОИСКА, "keep_history": True, **описание["params"]}


def создать_модель(имя: str, основа: str, окно: int) -> None:
    файл = ПЕСОЧНИЦА / "наряды" / f"Modelfile-{имя.replace(':', '-')}"
    файл.parent.mkdir(parents=True, exist_ok=True)
    файл.write_text(f"FROM {основа}\nPARAMETER num_ctx {окно}\n", "utf-8")
    subprocess.run(["ollama", "create", имя, "-f", str(файл)], check=True, stdout=subprocess.DEVNULL)


def выгрузить_прочие(своя: str) -> None:
    """В памяти остаются модель прогона и модель векторов: память мерится у одной модели разговора."""
    for модель in служба("/api/ps").get("models", []):
        if модель["name"] != своя and not модель["name"].startswith(МОДЕЛЬ_ВЕКТОРОВ):
            subprocess.run(["ollama", "stop", модель["name"]], check=True)


def главная() -> None:
    разбор = argparse.ArgumentParser(prog="наряд")
    разбор.add_argument("настройка")
    разбор.add_argument("--прогон", type=int, default=1)
    разбор.add_argument("--набор", choices=["подбор", "итог", "все"])
    разбор.add_argument("--сценарий", action="store_true")
    доводы = разбор.parse_args()
    if not ИМЯ_НАСТРОЙКИ.match(доводы.настройка):
        sys.exit("наряд: имя настройки — латиница, цифры и дефис")
    if not доводы.набор and not доводы.сценарий:
        sys.exit("наряд: нужен --набор или --сценарий")
    описание = json.loads(НАСТРОЙКИ.read_text("utf-8")).get(доводы.настройка)
    if описание is None:
        sys.exit(f"наряд: настройки {доводы.настройка!r} нет в {НАСТРОЙКИ.name}")

    модель = имя_модели(описание["основа"], описание["окно"])
    if описание["окно"] is not None:
        создать_модель(модель, описание["основа"], описание["окно"])

    профили = ПЕСОЧНИЦА / "profiles"
    профили.mkdir(exist_ok=True)
    (профили / f"{доводы.настройка}.json").write_text(
        json.dumps(профиль(доводы.настройка, описание), ensure_ascii=False, indent=1) + "\n", "utf-8")
    (профили / f"{доводы.настройка}.md").write_text((ИНСТРУКЦИИ / описание["инструкция"]).read_text("utf-8"), "utf-8")

    наряд = ПЕСОЧНИЦА / "наряды" / f"{доводы.настройка}.{доводы.прогон}.json"
    наряд.parent.mkdir(exist_ok=True)
    наряд.write_text(json.dumps({"model": f"ollama/{модель}", "concurrency": 1,
                                 "tasks": задания(доводы.настройка, доводы.прогон, доводы.набор, доводы.сценарий)},
                                ensure_ascii=False, indent=1) + "\n", "utf-8")

    выгрузить_прочие(модель)
    окружение = {имя: значение for имя, значение in os.environ.items() if not имя.startswith("RAG_")}
    окружение = {**окружение, "RAG_REWRITE_MODEL": f"ollama/{модель}", "MYHARNESS_STATE_DIR": str(ПЕСОЧНИЦА / "state"), "MYHARNESS_PROFILES": str(профили),
                 "MYHARNESS_JOURNAL": str(ЖУРНАЛ), "MYHARNESS_CONFIG_DIR": str(ПЕСОЧНИЦА / "настройки-без-ключа")}
    код = subprocess.run(["uv", "run", "-q", "--project", str(MYHARNESS), "myharness", "--batch", str(наряд)],
                         cwd=ПЕСОЧНИЦА, env=окружение).returncode

    в_памяти = next((м for м in служба("/api/ps").get("models", []) if м["name"] == модель), None)
    на_диске = next((м for м in служба("/api/tags").get("models", []) if м["name"] == модель), {})
    последняя = json.loads(ЖУРНАЛ.read_text("utf-8").splitlines()[-1]) if ЖУРНАЛ.is_file() else {}
    своя = str(последняя.get("agent") or "").startswith(f"{доводы.настройка}.{доводы.прогон}-")
    if not своя or в_памяти is None:
        # Наряд не дошёл до своей записи либо модель не загружена: такая строка ресурсов перекрыла бы
        # хорошую строку прежнего прогона той же пары. Код выхода сам по себе не причина: наряд
        # отдаёт 1 и тогда, когда модель исчерпала окно на одной реплике, — это исход замера.
        print(f"наряд: {доводы.настройка}.{доводы.прогон}, модель {модель}, код {код} — ресурсы НЕ записаны", file=sys.stderr)
        sys.exit(код or 1)
    строка = {"настройка": доводы.настройка, "прогон": доводы.прогон, "run_id": последняя.get("run_id"),
              "модель": модель, "основа": описание["основа"], "size": в_памяти.get("size"), "size_vram": в_памяти.get("size_vram"),
              "context_length": в_памяти.get("context_length"), "файл_модели_байт": на_диске.get("size"),
              "ts": dt.datetime.now().astimezone().isoformat(timespec="seconds")}
    with РЕСУРСЫ.open("a", encoding="utf-8") as файл:
        файл.write(json.dumps(строка, ensure_ascii=False) + "\n")
    print(f"наряд: {доводы.настройка}.{доводы.прогон}, модель {модель}, код {код}, ресурсы записаны")
    sys.exit(код)


if __name__ == "__main__":
    главная()
