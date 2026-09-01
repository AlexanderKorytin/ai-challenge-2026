"""Проверки без обращения к DeepSeek: профили, журнал, параметры, панель выбора, дополнения."""

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

failures = []


def raises(fn, arg):
    try:
        fn(arg)
    except ValueError:
        return True
    return False


def check(name, condition, detail=""):
    mark = "OK " if condition else "СБОЙ"
    print(f"  [{mark}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


tmp = Path(tempfile.mkdtemp())
os.environ["MYHARNESS_PROFILES"] = str(tmp / "profiles")
# Настройки — во временный каталог: иначе проверки пишут в настоящий config.json
# пользователя и затирают его ключ. Так уже случилось однажды.
os.environ["MYHARNESS_CONFIG_DIR"] = str(tmp / "config")
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")

from myharness import api, journal, params as params_mod, picker as picker_mod, profiles, ui  # noqa: E402
from myharness import cli  # noqa: E402
from myharness.config import Config  # noqa: E402

print("\n1. Профили")
(tmp / "profiles").mkdir(parents=True)
(tmp / "profiles" / "s3.md").write_text(
    "Отвечай JSON. Поле summary — не длиннее $summary_max_words слов.\n"
    'Пример: {"status": "ok", "summary": "…"}\n',
    encoding="utf-8",
)
(tmp / "profiles" / "s3.json").write_text(
    json.dumps(
        {
            "name": "s3",
            "description": "json с ограничениями",
            "system_file": "s3.md",
            "keep_history": False,
            "vars": {"summary_max_words": 60},
            "temperature": 0.2,
            "max_tokens": 700,
            "stop": ["\n\n###"],
            "response_format": {"type": "json_object"},
            "thinking": {"type": "disabled"},
            "top_k": 40,
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
profile, warnings = profiles.load("s3")
check("подстановка $summary_max_words", "не длиннее 60 слов" in (profile.system or ""))
check("фигурные скобки примера JSON уцелели", '{"status": "ok"' in (profile.system or ""))
check("неизвестный параметр top_k отсеян с предупреждением", "top_k" in " ".join(warnings) and "top_k" not in profile.params)
check("параметры прочитаны", profile.params["max_tokens"] == 700 and profile.params["stop"] == ["\n\n###"])
check("keep_history=false прочитан", profile.keep_history is False)
check("профиль виден в списке", any(n == "s3" for n, _ in profiles.available()))
missing, warns = profiles.load("нет-такого")
check("несуществующий профиль → default + предупреждение", missing.name == "default" and bool(warns))

print("\n2. Журнал")
err = journal.append({"status": "ok", "query": "щука", "usage": {"completion_tokens": 10}})
lines = Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").strip().splitlines()
record = json.loads(lines[0])
check("запись без ошибки", err is None)
check("метка времени и поля на месте", "ts" in record and record["query"] == "щука")
os.environ["MYHARNESS_JOURNAL"] = "/несуществующий/каталог/журнал.jsonl"
check("недоступный путь → текст ошибки, без исключения", journal.append({"x": 1}) is not None)
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")

print("\n3. Параметры")
check("разбор своего stop", params_mod.SPECS["stop"].parse("###, \\nКОНЕЦ") == ["###", "\nКОНЕЦ"])
check("больше 16 стоп-строк отвергнуто", raises(params_mod.SPECS["stop"].parse, ",".join(str(i) for i in range(17))))
check("max_tokens отвергает ноль", raises(params_mod.SPECS["max_tokens"].parse, "0"))
check("temperature принимает запятую", params_mod.SPECS["temperature"].parse("0,7") == 0.7)
check("reasoning_effort бесполезен без thinking", params_mod.inapplicable_reason("reasoning_effort", {"thinking": {"type": "disabled"}}) is not None)
check("в меню нет параметров, которых нет в API", not ({"top_k", "seed", "frequency_penalty", "presence_penalty"} & set(params_mod.SPECS)))

print("\n4. Панель выбора")
chosen = []
p = picker_mod.Picker(title="t", description="d", items=[picker_mod.Item("а"), picker_mod.Item("б")], on_choose=chosen.append, marked=1)
p.move(1)
p.choose()
check("движение по кругу и выбор", p.index == 1 and chosen == [None])
check("панель отрисовывается", len(picker_mod.fragments(p)) > 5)

print("\n5. Меню команд")
state = cli.State(config=Config(api_key=None), client=None, model="deepseek-v4-flash", profile=profiles.builtin_default())
comp = cli.HarnessCompleter(state)


class Doc:
    def __init__(self, text):
        self.text_before_cursor = text


names = [c.text for c in comp.get_completions(Doc("/"), None)]
check("без авторизации в меню только /auth", set(names) == {"/auth"}, str(names))
state.config.api_key = "sk-test"
names = [c.text for c in comp.get_completions(Doc("/"), None)]
check("после авторизации появились остальные, а /auth ушёл",
      {"/set", "/profile", "/params", "/model"} <= set(names) and "/auth" not in names, str(names))
check("аргументы /set — параметры", [c.text for c in comp.get_completions(Doc("/set temp"), None)] == ["temperature"])
check("аргументы /profile — профили", "s3" in [c.text for c in comp.get_completions(Doc("/profile "), None)])
check("обычный текст меню не открывает", [c.text for c in comp.get_completions(Doc("щука"), None)] == [])

print("\n6. Установка параметров через панель")
cli.open_value_picker(state, "temperature")
check("панель открыта на текущем значении", state.picker is not None and state.picker.marked == 0)
state.picker.move(1)
state.picker.choose()
check("значение применено", state.profile.params.get("temperature") == 0.0, str(state.profile.params))
check("профиль помечен как несохранённый", state.profile_dirty is True)
cli.open_value_picker(state, "temperature")
state.picker.index = len(state.picker.items) - 1
state.picker.choose()
check("пункт «своё значение» переводит в режим ввода", state.awaiting_custom == "temperature")
cli.apply_custom_value(state, "1,4")
check("своё значение разобрано", state.profile.params.get("temperature") == 1.4)
state.awaiting_custom = "temperature"
cli.apply_custom_value(state, "не число")
check("ошибка разбора не роняет и не меняет значение", state.profile.params.get("temperature") == 1.4)
cli.open_value_picker(state, "temperature")
state.picker.index = 0  # «не задавать»
state.picker.choose()
check("параметр снимается", "temperature" not in state.profile.params)

print("\n7. Сборка запроса")
state.profile, _ = profiles.load("s3")
msgs = cli.build_request_messages(state, "щука")
check("системная инструкция первая", msgs[0]["role"] == "system")
check("без истории — только система и вопрос", len(msgs) == 2 and msgs[1]["content"] == "щука")
state.messages.append({"role": "user", "content": "старое"})
msgs = cli.build_request_messages(state, "щука")
check("keep_history=false игнорирует накопленную историю", len(msgs) == 2)
state.profile.keep_history = True
msgs = cli.build_request_messages(state, "щука")
check("keep_history=true подставляет историю", len(msgs) == 2 and msgs[1]["content"] == "старое")

print("\n8. Разбор параметров для API")
direct, extra = api.split_params(profiles.load("s3")[0].params)
check("thinking и reasoning_effort уходят в extra_body", "thinking" in extra and "thinking" not in direct)
check("response_format уходит прямым аргументом", direct.get("response_format") == {"type": "json_object"})

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
