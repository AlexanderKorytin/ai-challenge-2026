"""Проверки встроенных файловых инструментов и выгрузки большого результата — без DeepSeek."""

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

failures = []


def check(name, condition, detail=""):
    mark = "OK " if condition else "СБОЙ"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


tmp = Path(tempfile.mkdtemp()).resolve()
os.environ["MYHARNESS_CONFIG_DIR"] = str(tmp / "config")
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")
os.environ.pop("MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS", None)
os.environ.pop("MYHARNESS_DISALLOWED_TOOLS", None)

from myharness import files_tools, tool_output  # noqa: E402
from myharness.agent import Инструменты  # noqa: E402

МОДЕЛЬ = "deepseek-v4-flash"


def собрать(корень, учёт=None, свободно=lambda: None):
    return files_tools.набор(
        корень, lambda: МОДЕЛЬ, свободно, учёт or files_tools.Учёт(), tool_output.каталог_выгрузки(корень)
    )


def имена(набор):
    return [с["function"]["name"] for с in набор.схемы] if набор else []


def вызов(набор, имя, **доводы):
    try:
        return asyncio.run(набор.исполнить(имя, json.dumps(доводы, ensure_ascii=False)))
    except Exception as exc:  # noqa: BLE001
        return f"ОТКАЗ: {exc}"


def хранилище(имя):
    корень = tmp / имя
    корень.mkdir()
    subprocess.run(["git", "init", "-q", str(корень)], check=True)
    return корень


print("\n# Файловые инструменты: чтение и корень")
проект = хранилище("проект")
(проект / "a.py").write_text("def load():\n    return 1\n\nload()\n", encoding="utf-8")
(проект / "big.txt").write_text("".join(f"строка {i}\n" for i in range(1, 3001)), encoding="utf-8")
(проект / "пустой.txt").write_text("", encoding="utf-8")
(проект / "bin.dat").write_bytes(b"\x00\x01\x02")
(tmp / "снаружи.txt").write_text("секрет", encoding="utf-8")
(проект / "ссылка.txt").symlink_to(tmp / "снаружи.txt")
набор = собрать(проект)
check("в хранилище git пять инструментов", sorted(имена(набор)) == sorted(files_tools.ИМЕНА), str(имена(набор)))
ответ = вызов(набор, "Read", file_path="a.py")
check("Read: строки с номерами через табуляцию", ответ.startswith("     1\tdef load():"), ответ[:60])
ответ = вызов(набор, "Read", file_path="big.txt")
check(
    "Read: 3000 строк — первые 2000 и пометка о показанных строках",
    "  2000\tстрока 2000" in ответ and "  2001\t" not in ответ and "из 3000" in ответ,
    ответ[-120:],
)
ответ = вызов(набор, "Read", file_path="big.txt", offset=2999, limit=5)
check("Read: offset и limit — строки 2999–3000", "  2999\tстрока 2999" in ответ and "  3000\tстрока 3000" in ответ, ответ)
check("Read: offset за концом — пометка с числом строк", "строк 3000" in вызов(набор, "Read", file_path="big.txt", offset=4000))
check("Read: пустой файл — пометка", "0 строк" in вызов(набор, "Read", file_path="пустой.txt"))
check("Read: двоичный файл — отказ", вызов(набор, "Read", file_path="bin.dat").startswith("ОТКАЗ: двоичный"))
check("Read: ../ наружу — отказ", "вне рабочего каталога" in вызов(набор, "Read", file_path="../снаружи.txt"))
check("Read: символическая ссылка наружу — отказ", "вне рабочего каталога" in вызов(набор, "Read", file_path="ссылка.txt"))
check("Read: абсолютный путь наружу — отказ", "вне рабочего каталога" in вызов(набор, "Read", file_path=str(tmp / "снаружи.txt")))
(tmp / "config").mkdir()
(tmp / "config" / "config.json").write_text('{"api_key": "sk-x"}', encoding="utf-8")
check(
    "Read: каталог настроек закрыт даже внутри корня",
    "каталог настроек" in вызов(собрать(tmp), "Read", file_path="config/config.json"),
)

os.environ["MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS"] = "300"
урезано = вызов(набор, "Read", file_path="big.txt")
check("Read: целый файл сверх предела токенов — начало и PARTIAL view", "[PARTIAL view:" in урезано, урезано[-150:])
check(
    "Read: часть сверх предела — отказ с советом",
    "меньшей частью" in вызов(набор, "Read", file_path="big.txt", offset=1, limit=500),
)
check(
    "Read: allow_large при неизвестном свободном месте — отказ",
    "неизвестно" in вызов(набор, "Read", file_path="big.txt", allow_large=True),
)
большой = собрать(проект, свободно=lambda: 10**6)
check(
    "Read: allow_large со свободным местом — файл без пометки PARTIAL",
    "[PARTIAL view" not in вызов(большой, "Read", file_path="big.txt", allow_large=True),
)
del os.environ["MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS"]
(проект / "огромный.txt").write_text("x" * 300_000, encoding="utf-8")
check("Read: файл больше 256 КБ целиком — отказ", "256 КБ" in вызов(набор, "Read", file_path="огромный.txt"))

print("\n# Файловые инструменты: поиск")
(проект / ".gitignore").write_text("скрытый.py\n", encoding="utf-8")
(проект / "скрытый.py").write_text("load()\n", encoding="utf-8")
(проект / "b.py").write_text("from a import load\nload()\n", encoding="utf-8")
ответ = вызов(набор, "Grep", pattern=r"load\(\)")
check("Grep: файлы с совпадением, без игнорируемых .gitignore", "a.py" in ответ and "b.py" in ответ and "скрытый" not in ответ, ответ)
ответ = вызов(набор, "Grep", pattern=r"load\(\)", output_mode="content", glob="b.py")
check("Grep content: путь:номер:строка", ответ.strip() == "b.py:2:load()", ответ)
ответ = вызов(набор, "Grep", pattern="load", output_mode="count")
check("Grep count: по файлам и итог", "a.py:2" in ответ and "итого:" in ответ, ответ)
check("Grep: offset за концом", вызов(набор, "Grep", pattern="load", offset=50) == "No entries at this offset")
ответ = вызов(набор, "Grep", pattern="load", head_limit=1)
check("Grep: head_limit 1 — одна запись и подсказка offset", ответ.count("\n") == 1 and "offset 1" in ответ, ответ)
поиск = tmp / "много"
поиск.mkdir()
for i in range(101):
    (поиск / f"ф{i}.txt").write_text("x", encoding="utf-8")
    os.utime(поиск / f"ф{i}.txt", (i, 1_000_000 + i))
ответ = вызов(собрать(поиск), "Glob", pattern="*.txt")
строки = ответ.splitlines()
check("Glob: 101 файл — 100 путей и пометка", len(строки) == 101 and "100 из 101" in строки[-1], строки[-1])
check("Glob: новые первыми", строки[0] == "ф100.txt" and строки[99] == "ф1.txt", str(строки[:2]))

print("\n# Файловые инструменты: правка и запись")
учёт = files_tools.Учёт()
набор = собрать(проект, учёт)
check("Edit без Read — отказ", "сначала прочитай" in вызов(набор, "Edit", file_path="a.py", old_string="return 1", new_string="return 2"))
вызов(набор, "Read", file_path="a.py")
ответ = вызов(набор, "Edit", file_path="a.py", old_string="return 1", new_string="return 2")
check("Edit после Read — diff", ответ.startswith("--- a/a.py") and "-    return 1" in ответ and "+    return 2" in ответ, ответ)
check("Edit: правка на диске", "return 2" in (проект / "a.py").read_text(encoding="utf-8"))
check("Edit: два вхождения без replace_all — отказ", "встречается 2 раз" in вызов(набор, "Edit", file_path="a.py", old_string="load()", new_string="go()"))
ответ = вызов(набор, "Edit", file_path="a.py", old_string="load()", new_string="go()", replace_all=True)
check("Edit: replace_all меняет оба", (проект / "a.py").read_text(encoding="utf-8").count("go()") == 2, ответ)
ответ = вызов(набор, "Write", file_path="docs/USES.md", content="# места\n")
check("Write: новый файл без чтения, каталог создан, diff от /dev/null", ответ.startswith("--- /dev/null") and (проект / "docs" / "USES.md").exists(), ответ)
check("Write поверх без Read — отказ", "сначала прочитай" in вызов(набор, "Write", file_path="b.py", content="x"))
вызов(набор, "Read", file_path="b.py")
time.sleep(0.01)
(проект / "b.py").write_text("изменён снаружи\n", encoding="utf-8")
check("Write: файл изменился после чтения — отказ", "изменился после чтения" in вызов(набор, "Write", file_path="b.py", content="x"))
(проект / "sub" / ".claude").mkdir(parents=True)
check("Write в .git/config — отказ", "закрыта" in вызов(набор, "Write", file_path=".git/config", content="x"))
check("Write в sub/.claude/settings.json — отказ", "закрыта" in вызов(набор, "Write", file_path="sub/.claude/settings.json", content="{}"))
check("Write в .mcp.json — отказ", "закрыта" in вызов(набор, "Write", file_path=".mcp.json", content="{}"))
check("Write в .GIT/config (регистр) — отказ", "закрыта" in вызов(набор, "Write", file_path=".GIT/config", content="x"))
os.environ["MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS"] = "300"
вызов(набор, "Read", file_path="big.txt")
check("Edit после PARTIAL view — отказ", "сначала прочитай" in вызов(набор, "Edit", file_path="big.txt", old_string="строка 1\n", new_string="x\n"))
del os.environ["MYHARNESS_FILE_READ_MAX_OUTPUT_TOKENS"]

print("\n# Файловые инструменты: разрешения")
вне_git = tmp / "вне"
вне_git.mkdir()
check("вне git — только чтение", sorted(имена(собрать(вне_git))) == ["Glob", "Grep", "Read"], str(имена(собрать(вне_git))))
(вне_git / ".claude").mkdir()
правила = вне_git / ".claude" / "settings.json"
правила.write_text(json.dumps({"permissions": {"allow": ["Edit"]}}), encoding="utf-8")
check("вне git с allow Edit — запись есть", {"Edit", "Write"} <= set(имена(собрать(вне_git))), str(имена(собрать(вне_git))))
(проект / ".claude").mkdir(exist_ok=True)
правила_проекта = проект / ".claude" / "settings.json"
правила_проекта.write_text(json.dumps({"permissions": {"deny": ["Edit"]}}), encoding="utf-8")
check("в git с deny Edit — записи нет", sorted(имена(собрать(проект))) == ["Glob", "Grep", "Read"], str(имена(собрать(проект))))
правила_проекта.write_text(json.dumps({"permissions": {"deny": ["Read(./.env)"]}}), encoding="utf-8")
с_путём = собрать(проект)
check(
    "deny с путём снимает род целиком и жалуется",
    sorted(имена(с_путём)) == ["Edit", "Write"] and "с путём" in (с_путём.жалоба or ""),
    f"{имена(с_путём)} {с_путём.жалоба}",
)
правила_проекта.write_text("{испорчен", encoding="utf-8")
испорчен = собрать(проект)
check("испорченный файл правил — инструментов нет, жалоба", испорчен is not None and not имена(испорчен) and "сняты" in (испорчен.жалоба or ""))
правила_проекта.write_text(json.dumps({"permissions": {}}), encoding="utf-8")
живой = собрать(проект)
правила_проекта.write_text(json.dumps({"permissions": {"deny": ["Edit"]}}), encoding="utf-8")
check("снятое посреди круга правило отказывает следующему вызову", "не разрешён" in вызов(живой, "Write", file_path="новый.md", content="x"))
правила_проекта.write_text(json.dumps({"permissions": {"allow": ["mcp__x__y"]}}), encoding="utf-8")
os.environ["MYHARNESS_DISALLOWED_TOOLS"] = "Edit"
check("запрет запуском MYHARNESS_DISALLOWED_TOOLS=Edit — записи нет", sorted(имена(собрать(проект))) == ["Glob", "Grep", "Read"])
del os.environ["MYHARNESS_DISALLOWED_TOOLS"]

print("\n# Большой результат инструмента")


def набор_из(текст=None, ошибка=None):
    async def исполнить(имя, доводы):
        if ошибка is not None:
            raise RuntimeError(ошибка)
        return текст
    return Инструменты(схемы=[{"type": "function", "function": {"name": "x"}}], исполнить=исполнить)


каталог = tool_output.каталог_выгрузки(проект)
обёрнут = tool_output.с_выгрузкой(набор_из("я" * 60_000), каталог, lambda: МОДЕЛЬ)
ответ = asyncio.run(обёрнут.исполнить("x", "{}"))
файлы = list(каталог.glob("*.txt"))
check("60 000 знаков — файл и ответ с путём и началом", len(файлы) == 1 and str(файлы[0]) in ответ and "я" * 2000 in ответ and "я" * 2001 not in ответ, ответ[:200])
check("файл выгрузки — права 600, каталог 700", oct(файлы[0].stat().st_mode & 0o777) == "0o600" and oct(каталог.stat().st_mode & 0o777) == "0o700")
check("выгрузка лежит в $MYHARNESS_STATE_DIR", str(tmp / "state") in str(файлы[0]))
ответ = asyncio.run(tool_output.с_выгрузкой(набор_из(("hello world " * 4000)[:40_000]), каталог, lambda: МОДЕЛЬ).исполнить("x", "{}"))
check("40 000 знаков (меньше 25 000 токенов) — как есть", ответ == ("hello world " * 4000)[:40_000])
try:
    asyncio.run(tool_output.с_выгрузкой(набор_из(ошибка="о" * 20_000), каталог, lambda: МОДЕЛЬ).исполнить("x", "{}"))
    ответ = ""
except RuntimeError as exc:
    ответ = str(exc)
check("ошибка в 20 000 знаков — 5000 + пометка + 5000", ответ.startswith("о" * 5000) and "пропущено 10000" in ответ and len(ответ) < 10_100, str(len(ответ)))
try:
    asyncio.run(tool_output.с_выгрузкой(набор_из(ошибка="о" * 10_000), каталог, lambda: МОДЕЛЬ).исполнить("x", "{}"))
except RuntimeError as exc:
    ответ = str(exc)
check("ошибка в 10 000 знаков — как есть", ответ == "о" * 10_000)
ответ = вызов(собрать(проект), "Read", file_path=str(файлы[0]), offset=30, limit=1)
check("Read читает выгрузку вне корня; строка в 60 000 знаков перенесена по 2000", ответ.split("\n")[0] == "    30\t" + "я" * 2000, ответ[:40])
обёрнут = tool_output.с_выгрузкой(набор_из(json.dumps({"k": ["я" * 100] * 600}, ensure_ascii=False)), каталог, lambda: МОДЕЛЬ)
путь_json = asyncio.run(обёрнут.исполнить("x", "{}")).split("сохранён в ", 1)[1].split(". Начало", 1)[0]
check("выгрузка JSON разложена по строкам", len(Path(путь_json).read_text(encoding="utf-8").splitlines()) > 600)
(проект / "длинная.txt").write_text("д" * 2500 + "\n", encoding="utf-8")
ответ = вызов(собрать(проект), "Read", file_path="длинная.txt")
check("Read: строка длиннее 2000 знаков обрезана с пометкой", "строка обрезана: 2500" in ответ and "д" * 2001 not in ответ, ответ[-60:])
check("Write в каталог выгрузки — отказ", "ОТКАЗ" in вызов(собрать(проект), "Write", file_path=str(каталог / "x.txt"), content="x"))
(tmp / "state" / "projects").mkdir(parents=True)
(tmp / "state" / "projects" / "разговор.jsonl").write_text("{}", encoding="utf-8")
check("Read: прочий каталог состояния закрыт", "каталог состояния" in вызов(собрать(tmp), "Read", file_path="state/projects/разговор.jsonl"))

print("\n# Находки просмотра: обходы и порча")
check("Write в .mcp.jſon (длинная s) — отказ", "закрыта" in вызов(набор, "Write", file_path=".mcp.j\u017fon", content="{}") and not (проект / ".mcp.json").exists())
check("Write в .mcp.json/x — отказ", "закрыта" in вызов(набор, "Write", file_path=".mcp.json/x", content="{}"))
check(
    "Read: каталог настроек другим регистром — отказ",
    "каталог настроек" in вызов(собрать(tmp), "Read", file_path="CONFIG/config.json"),
    вызов(собрать(tmp), "Read", file_path="CONFIG/config.json")[:80],
)
(проект / "секрет-ссылка.txt").symlink_to(tmp / "снаружи.txt")
check("Grep не читает файл вне корня через ссылку", "секрет" not in вызов(собрать(проект), "Grep", pattern="секрет", output_mode="content"))
check("Glob с .. — отказ", "ОТКАЗ" in вызов(собрать(проект), "Glob", pattern="../*"))
(проект / "ссылка-каталог").symlink_to(tmp / "вне", target_is_directory=True)
check("Glob через ссылку-каталог наружу — пусто", "файлов не найдено" == вызов(собрать(проект), "Glob", pattern="ссылка-каталог/**/*"))
(проект / "цел.txt").write_text("цел\n", encoding="utf-8")
порча = собрать(проект, files_tools.Учёт())
вызов(порча, "Read", file_path="цел.txt")
ответ = вызов(порча, "Edit", file_path="цел.txt", old_string="цел", new_string="\ud800")
check("Edit с одиночной суррогатной парой — отказ, файл цел", "ОТКАЗ" in ответ and (проект / "цел.txt").read_text(encoding="utf-8") == "цел\n", ответ)
обёрнут = tool_output.с_выгрузкой(набор_из("[" * 60_000), каталог, lambda: МОДЕЛЬ)
check("выгрузка глубоко вложенного текста — файл, а не ошибка", "сохранён в" in asyncio.run(обёрнут.исполнить("x", "{}")))

os.link(tmp / "config" / "config.json", проект / "жёсткая.json")
check("жёсткая ссылка на config.json в корне — Grep её не читает", "sk-x" not in вызов(собрать(проект), "Grep", pattern="sk-", output_mode="content"))
(проект / "перебор.txt").write_text("a" * 40 + "\n", encoding="utf-8")


async def отмена_перебора():
    задача = asyncio.create_task(собрать(проект).исполнить("Grep", json.dumps({"pattern": "(a|aa)*$c", "path": "перебор.txt"})))
    await asyncio.sleep(0.5)
    начало = time.monotonic()
    тик = asyncio.create_task(asyncio.sleep(0.05))
    await тик
    отзыв = time.monotonic() - начало
    задача.cancel()
    try:
        await задача
    except asyncio.CancelledError:
        pass
    return отзыв, time.monotonic() - начало


отзыв, снятие = asyncio.run(отмена_перебора())
check("Grep с перебором не держит цикл событий, отмена снимает сразу", отзыв < 1 and снятие < 2, f"{отзыв:.1f} с / {снятие:.1f} с")

print("\n# Подключение: наряд и главный агент")
from myharness import api, batch, ui  # noqa: E402


class Запоминающий:
    def __init__(self):
        self.инструменты = []

    async def stream_chat(self, model, messages, params=None, **доводы):
        self.инструменты.append(sorted(с["function"]["name"] for с in доводы.get("tools") or []))
        yield api.StreamEvent("content", "готово")
        yield api.StreamEvent("meta", finish_reason="stop", usage={"prompt_tokens": 1, "completion_tokens": 1})


def наряд_в(каталог):
    клиент = Запоминающий()
    прежний = Path.cwd()
    os.chdir(каталог)
    try:
        наряд = batch.Order(model=МОДЕЛЬ, concurrency=1, tasks=[batch.Task("файлы", "default", {}, ("привет",))])
        asyncio.run(batch.run_order(наряд, клиент, on_line=lambda строка: None))
    finally:
        os.chdir(прежний)
    return клиент.инструменты


чистый = хранилище("наряд")
check("наряд в хранилище git — пять файловых схем", наряд_в(чистый) == [sorted(files_tools.ИМЕНА)], str(наряд_в(чистый)))
вне = tmp / "наряд-вне"
вне.mkdir()
check("наряд вне git — три схемы чтения", наряд_в(вне) == [["Glob", "Grep", "Read"]], str(наряд_в(вне)))
check("наряд не пишет в каталог состояния", not (tmp / "state" / "tool-results" / "наряд").exists() and not any((tmp / "state").glob("projects/*наряд*")))
check(
    "окно: вызов Edit показан путём, diff — строками с цветом",
    ui.tool_call_fragments("Edit", json.dumps({"file_path": "README.md", "old_string": "a", "new_string": "b"}))[0][1] == "⚙ Edit: README.md"
    and ("class:diff-add", "    +b") in ui.tool_result_fragments("Edit", "--- a/README.md\n+++ b/README.md\n@@ -1 +1 @@\n-a\n+b"),
)
check("окно: результат Read — числом строк", ui.tool_result_fragments("Read", "     1\ta\n     2\tb")[0][1] == "  ↳ Read: строк 2")

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
