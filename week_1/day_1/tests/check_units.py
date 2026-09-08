"""Проверки без обращения к DeepSeek: профили, журнал, параметры, панель выбора, дополнения."""

import argparse
import ast
import asyncio
import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import time
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
# Каталог состояния — туда же: разговоры и глобальные факты пишутся между запусками,
# и без переопределения проверки замусорили бы настоящий ~/.local/state пользователя.
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")

from myharness import api, journal, memory, params as params_mod, picker as picker_mod, profiles, ui  # noqa: E402
from myharness import archivist, config as config_mod  # noqa: E402
from myharness import batch, cli, methods, output, screens as screens_mod, team  # noqa: E402
from myharness.agent import Agent, Turn, usage_tokens
from myharness.config import Config  # noqa: E402
from prompt_toolkit.data_structures import Point  # noqa: E402
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType  # noqa: E402

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
# Окно памяти профиля. Ноль — «окно выключено, память не обрезается»: так пользователь
# возвращает прежнее безграничное поведение. Мусор в поле отбрасывается с предупреждением,
# а не подставляется молча: молча подставленное значение сделало бы поведение необъяснимым.
(tmp / "profiles" / "window.json").write_text(
    json.dumps({"name": "window", "history_window": 3}, ensure_ascii=False), encoding="utf-8"
)
оконный, _ = profiles.load("window")
check("history_window прочитан", оконный.history_window == 3, str(оконный.history_window))
check("умолчание окна — десять пар", profile.history_window == 10, str(profile.history_window))
for мусор in ("три", True, -1, 2.5):
    (tmp / "profiles" / "window_bad.json").write_text(
        json.dumps({"name": "window_bad", "history_window": мусор}, ensure_ascii=False), encoding="utf-8"
    )
    плохой, плохие = profiles.load("window_bad")
    check(
        f"history_window = {мусор!r} отвергнут с предупреждением",
        плохой.history_window == 10 and any("history_window" in w for w in плохие),
        f"{плохой.history_window} / {плохие}",
    )
check("окно памяти попадает в слепок для журнала", оконный.snapshot().get("history_window") == 3, str(оконный.snapshot()))
check("окно памяти сохраняется в файл профиля", оконный.to_dict().get("history_window") == 3, str(оконный.to_dict()))

# Раскладка цепочки. Умолчание — панели рядом на одной вкладке, «tabs» разводит шаги по
# отдельным вкладкам. Мусор отбрасываем с предупреждением по тому же правилу, что и окно
# памяти. Поле имеет смысл только у профиля-цепочки: у профиля без «screens» раскладывать
# нечего, и молчать об этом нельзя — человек написал то, что не сработает.
(tmp / "profiles" / "chain_tabs.json").write_text(
    json.dumps({"name": "chain_tabs", "screens": ["a", "b"], "layout": "tabs"}, ensure_ascii=False),
    encoding="utf-8",
)
вкладками, _ = profiles.load("chain_tabs")
check("layout = tabs прочитан", вкладками.layout == "tabs", вкладками.layout)
check("умолчание раскладки — панели рядом", profile.layout == "panes", profile.layout)
for мусор in (2, "колонки", True):
    (tmp / "profiles" / "layout_bad.json").write_text(
        json.dumps({"name": "layout_bad", "screens": ["a"], "layout": мусор}, ensure_ascii=False),
        encoding="utf-8",
    )
    плохой, плохие = profiles.load("layout_bad")
    check(
        f"layout = {мусор!r} отвергнут с предупреждением",
        плохой.layout == "panes" and any("layout" in w for w in плохие),
        f"{плохой.layout} / {плохие}",
    )
(tmp / "profiles" / "layout_alone.json").write_text(
    json.dumps({"name": "layout_alone", "layout": "tabs"}, ensure_ascii=False), encoding="utf-8"
)
одинокий, одинокие = profiles.load("layout_alone")
check(
    "layout без screens — предупреждение: раскладывать нечего",
    одинокий.layout == "panes" and any("layout" in w and "screens" in w for w in одинокие),
    str(одинокие),
)
check("раскладка попадает в слепок для журнала", вкладками.snapshot().get("layout") == "tabs", str(вкладками.snapshot()))
check("раскладка сохраняется в файл профиля", вкладками.to_dict().get("layout") == "tabs", str(вкладками.to_dict()))

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
# Числа взяты из правил, а не из наблюдения: системная инструкция кладётся поверх памяти
# и в саму память не входит; remember кладёт ровно два сообщения; новый вопрос
# дописывается в конец один раз. Отсюда 2, 2, 4 и 1.
без_истории, _ = profiles.load("s3")  # keep_history=False
одиночка = Agent("одиночка", без_истории)
msgs = одиночка.build_messages("щука")
check(
    "инструкция идёт первой, истории нет",
    [m["role"] for m in msgs] == ["system", "user"] and msgs[-1]["content"] == "щука",
    str(msgs),
)

с_историей, _ = profiles.load("s3")
с_историей.keep_history = True
собеседник = Agent("собеседник", с_историей)
msgs = собеседник.build_messages("щука")
check("пустая история не добавляет сообщений", [m["role"] for m in msgs] == ["system", "user"], str(msgs))

собеседник.remember("старое", "ответ на старое")
msgs = собеседник.build_messages("щука")
check(
    "непустая история подставляется целиком",
    [m["role"] for m in msgs] == ["system", "user", "assistant", "user"],
    str(msgs),
)
check(
    "новый вопрос попадает в запрос ровно один раз",
    sum(1 for m in msgs if m["content"] == "щука") == 1,
    str(msgs),
)

print("\n8. Группа агентов и заготовка ввода")
(tmp / "profiles" / "lead.json").write_text(
    json.dumps(
        {"name": "lead", "system": "сведи ответы", "agents": ["analyst", 7, "critic", "  "]},
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
(tmp / "profiles" / "analyst.json").write_text(
    json.dumps({"name": "analyst", "system": "ты аналитик", "keep_history": False}, ensure_ascii=False),
    encoding="utf-8",
)
(tmp / "profiles" / "meta.md").write_text("Составь промпт для задачи про $кого\n", encoding="utf-8")
(tmp / "profiles" / "meta.json").write_text(
    json.dumps({"name": "meta", "prefill_file": "meta.md", "vars": {"кого": "шофёров"}}, ensure_ascii=False),
    encoding="utf-8",
)

lead, lead_warnings = profiles.load("lead")
check("состав группы прочитан", lead.agents == ["analyst", "critic"], str(lead.agents))
check("мусор в составе отсеян с предупреждением", any("не имя профиля" in w for w in lead_warnings), str(lead_warnings))
check("состав попадает в слепок для журнала", lead.snapshot().get("agents") == ["analyst", "critic"])
check("обычный профиль остаётся без группы", profiles.load("analyst")[0].agents == [])

(tmp / "profiles" / "steps.json").write_text(
    json.dumps({"name": "steps", "screens": ["meta", "analyst"]}, ensure_ascii=False), encoding="utf-8"
)
steps, _ = profiles.load("steps")
check("набор рабочих экранов прочитан", steps.screens == ["meta", "analyst"] and steps.agents == [])
check("набор экранов попадает в слепок для журнала", steps.snapshot().get("screens") == ["meta", "analyst"])

meta, _ = profiles.load("meta")
check("заготовка ввода прочитана из файла", meta.prefill == "Составь промпт для задачи про шофёров", repr(meta.prefill))
check("заготовка не путается с системной инструкцией", meta.system is None)

team_state = cli.State(config=Config(api_key="sk-test"), client=None, model="deepseek-v4-flash", profile=lead)
analyst_profile, _ = profiles.load("analyst")
board, summary_screen = team.ensure_screens(team_state, lead, [analyst_profile])
again, _ = team.ensure_screens(team_state, lead, [analyst_profile])
check("экраны группы заводятся один раз", board is again and len(team_state.screens) == 3)
check("у каждого эксперта своя панель", [p.key for p in board.panes] == ["analyst"])
check("сводка ведущего — отдельная вкладка", summary_screen.title.endswith("сводка"))
analyst_pane = board.panes[0]
cli.append_log(team_state, [("", "личное")], analyst_pane)
check("ответ эксперта идёт в его панель", "личное" in "".join(t for _, t in analyst_pane.log))
check("главный экран при этом чист", "личное" not in "".join(t for _, t in team_state.main.first.log))
cli.switch_screen(team_state, 1)
check("переключение экрана меняет показываемую ленту", team_state.screen is board)
cli.drop_agent_screens(team_state)
check("смена профиля закрывает экраны группы", len(team_state.screens) == 1 and team_state.active == 0)

agent_messages = analyst_pane.agent.build_messages("вопрос")
check("агенту уходит его инструкция и вопрос", [m["role"] for m in agent_messages] == ["system", "user"])
check("keep_history=false не копит историю агента", analyst_pane.agent.history() == [])
check(
    "у панели эксперта есть свой собеседник",
    analyst_pane.agent is not None
    and analyst_pane.agent.name == analyst_profile.name
    and analyst_pane.agent.history() == [],
)
# И-2. Повтор имени в составе группы или в наборе способов. Молча схлопнуть нельзя — это
# опечатка в профиле, и о ней надо сказать; оставить как есть тоже нельзя — одно имя дважды
# означает два запроса ОДНОМУ собеседнику: две одинаковые пары в его памяти, две ленты в одной
# панели и двойная цена за один и тот же ответ.
(tmp / "profiles" / "critic.json").write_text(
    json.dumps({"name": "critic", "system": "ты критик", "keep_history": False}, ensure_ascii=False),
    encoding="utf-8",
)
(tmp / "profiles" / "twins.json").write_text(
    json.dumps(
        {"name": "twins", "agents": ["analyst", "critic", "analyst"], "methods": ["free", "free"]},
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
близнецы, близнецовые = profiles.load("twins")
check(
    "повтор в составе группы отброшен, порядок первого появления сохранён",
    близнецы.agents == ["analyst", "critic"],
    str(близнецы.agents),
)
check(
    "про повтор в составе сказано вслух",
    any("agents" in w and "analyst" in w and "повтор" in w for w in близнецовые),
    "; ".join(близнецовые),
)
check("повтор в наборе способов отброшен", близнецы.methods == ["free"], str(близнецы.methods))
check(
    "про повтор способа тоже сказано",
    any("methods" in w and "free" in w and "повтор" in w for w in близнецовые),
    "; ".join(близнецовые),
)
исполнители = team.load_agents(team_state, близнецы)
check(
    "состав из трёх имён с повтором даёт двух исполнителей",
    [профиль.name for профиль in исполнители] == ["analyst", "critic"],
    str([профиль.name for профиль in исполнители]),
)

summary = team.build_summary_request("задача", [("analyst", "ответ А"), ("critic", "ответ Б")])
check("сводка несёт задачу и ответы каждого", "задача" in summary and "«analyst»" in summary and "ответ Б" in summary)

(tmp / "profiles" / "set.json").write_text(
    json.dumps({"name": "set", "methods": ["free", "meta"]}, ensure_ascii=False), encoding="utf-8"
)
method_set, _ = profiles.load("set")
check("набор способов прочитан", method_set.methods == ["free", "meta"] and method_set.agents == [])

# Итог цепочки. Умолчание — ответы всех шагов, подписанные: цепочка, кончающаяся проверяющим,
# иначе отдала бы наверх один вердикт без предмета вердикта. Проверено на живой демонстрации
# дня 6: код оставался на своей вкладке, а в главный экран приезжало «ГОДЕН».
check(
    "один блок подписи не получает",
    methods.chain_outcome_text([("решение", "fun main() {}")]) == "fun main() {}",
    methods.chain_outcome_text([("решение", "fun main() {}")]),
)
итог_цепочки = methods.chain_outcome_text([("решение", "fun main() {}"), ("проверка", "ГОДЕН")])
check(
    "несколько блоков подписаны именами шагов",
    итог_цепочки == "[решение]\nfun main() {}\n\n[проверка]\nГОДЕН",
    repr(итог_цепочки),
)

(tmp / "profiles" / "chain_res.json").write_text(
    json.dumps(
        {"name": "chain_res", "screens": ["free", "meta"], "result": ["meta", "нет-такого"]},
        ensure_ascii=False,
    ),
    encoding="utf-8",
)
сужение, сужение_предупреждения = profiles.load("chain_res")
check("итог сужается полем result", сужение.result == ["meta"], str(сужение.result))
check(
    "шаг вне screens отброшен с предупреждением",
    any("нет-такого" in w and "result" in w for w in сужение_предупреждения),
    str(сужение_предупреждения),
)
check("состав итога попадает в слепок для журнала", сужение.snapshot().get("result") == ["meta"])

(tmp / "profiles" / "res_no_screens.json").write_text(
    json.dumps({"name": "res_no_screens", "result": ["meta"]}, ensure_ascii=False), encoding="utf-8"
)
_, без_шагов = profiles.load("res_no_screens")
check(
    "result без screens отброшен с предупреждением",
    any("result" in w and "screens" in w for w in без_шагов),
    str(без_шагов),
)
check("широкое окно — три панели в ряд", cli.pane_columns(5, 200) == 3)
check("обычное окно — две", cli.pane_columns(5, 120) == 2)
check("узкое окно — панели одна под другой", cli.pane_columns(5, 80) == 1)

print("\n8. Разбор параметров для API")
direct, extra = api.split_params(profiles.load("s3")[0].params)
check("thinking и reasoning_effort уходят в extra_body", "thinking" in extra and "thinking" not in direct)
check("response_format уходит прямым аргументом", direct.get("response_format") == {"type": "json_object"})

print("\n9. Агент")
agent_profile, _ = profiles.load("s3")
agent = Agent("советник", agent_profile)
check("новый агент помнит своё имя", agent.name == "советник", agent.name)
check("память нового агента пуста", agent.history() == [], str(agent.history()))
# история отдаётся копией: иначе любой снаружи незаметно перепишет память агента
снимок = agent.history()
снимок.append({"role": "user", "content": "подброшено"})
check("история отдаётся копией", agent.history() == [], str(agent.history()))
agent.remember("вопрос", "ответ")
check(
    "готовая пара кладётся в память",
    agent.history() == [
        {"role": "user", "content": "вопрос"},
        {"role": "assistant", "content": "ответ"},
    ],
    str(agent.history()),
)

# Шаг 8. Хранилище разговора приходит агенту СНАРУЖИ, и умолчание — выключено.
# Иначе пакетный наряд на сто заданий завёл бы сто записей на диске и переписал разговор
# пользователя, а панели исполнителей группы — ещё десяток. Включать хранилище будет только
# главный экран, и делает это он сам.
разговоры = tmp / "разговоры"


def файлы_состояния():
    """Всё, что лежит в каталоге состояния. Сравнение списков до и после — единственный
    способ утверждать «на диск не писали»: агент без хранилища не обязан сообщать, куда он
    не записал."""
    корень = Path(os.environ["MYHARNESS_STATE_DIR"])
    if not корень.exists():
        return []
    # Путь И размер: сравнение одних путей пропускало включённое умолчание хранилища —
    # файл уже создан соседней проверкой, дописывание в него набор путей не меняет.
    # Проверяющий поймал это поломкой кода, которую весь набор прошёл не заметив.
    return sorted(f"{п}:{п.stat().st_size}" for п in корень.rglob("*") if п.is_file())


# Кладём факт ДО проверок агента: без него «агент без фактов ведёт себя как раньше»
# проходит и при включённом умолчании — каталог фактов пуст, подмешивать нечего.
# Тоже находка проверяющего: поломка «умолчание фактов включено» прошла незамеченной.
memory.add_fact("зовут Александр")
факты_на_диске, _ = memory.load_facts()
check("факт для проверок агента положен", факты_на_диске == ["зовут Александр"], str(факты_на_диске))

до_молчуна = файлы_состояния()
профиль_молчуна, _ = profiles.load("s3")
профиль_молчуна.keep_history = True
молчун = Agent("молчун", профиль_молчуна)
молчун.remember("вопрос", "ответ")
check("агент без хранилища на диск не пишет", файлы_состояния() == до_молчуна, str(файлы_состояния()))
check("но в памяти пара у него есть", len(молчун.history()) == 2, str(молчун.history()))

путь_разговора = разговоры / "беседа.jsonl"
профиль_записного, _ = profiles.load("s3")
профиль_записного.keep_history = True
записной = Agent("записной", профиль_записного, store=memory.SessionStore(путь_разговора))
записной.remember("вопрос на диск", "ответ с диска", model="deepseek-v4-flash")
строки_разговора = путь_разговора.read_text(encoding="utf-8").strip().splitlines()
check("пара легла на диск двумя строками", len(строки_разговора) == 2, str(строки_разговора))
вопрос_на_диске = json.loads(строки_разговора[0]) if строки_разговора else {}
ответ_на_диске = json.loads(строки_разговора[1]) if len(строки_разговора) > 1 else {}
check(
    "вопрос записан ролью user и без пометок",
    вопрос_на_диске.get("role") == "user"
    and вопрос_на_диске.get("content") == "вопрос на диск"
    and "model" not in вопрос_на_диске
    and "profile" not in вопрос_на_диске,
    str(вопрос_на_диске),
)
# Пометки нужны на ответе: через месяц по файлу должно быть видно, кем и под какой
# инструкцией он получен, — иначе восстановленный разговор ничем не отличить от чужого.
check(
    "ответ несёт профиль, модель и отпечаток инструкции",
    ответ_на_диске.get("role") == "assistant"
    and ответ_на_диске.get("content") == "ответ с диска"
    and ответ_на_диске.get("profile") == профиль_записного.name
    and ответ_на_диске.get("model") == "deepseek-v4-flash"
    and ответ_на_диске.get("system_fp") == memory.fingerprint(профиль_записного.system),
    str(ответ_на_диске),
)
check("удачная запись ошибки не оставляет", записной.store_error is None, repr(записной.store_error))

# Профиль с выключенной историей разговора на диске не заводит. Иначе на диске оказался бы
# разговор, которого нет в запросе к модели: `build_messages` память такого профиля не берёт.
путь_без_истории = разговоры / "без-истории.jsonl"
профиль_беспамятного, _ = profiles.load("s3")  # keep_history=False
беспамятный = Agent("беспамятный", профиль_беспамятного, store=memory.SessionStore(путь_без_истории))
беспамятный.remember("вопрос", "ответ")
check("профиль без истории на диск не пишет", not путь_без_истории.exists(), str(путь_без_истории))

# Сбой записи не имеет права уронить пополнение памяти: ответ уже получен и уже оплачен.
# Недоступность задаём родителем-файлом: под обычным файлом каталога не создать никакими
# правами, ядро отдаёт ENOTDIR. Несуществующий корень не годится — под root он был бы создан.
преграда = tmp / "преграда"
преграда.write_text("это файл, а не каталог", encoding="utf-8")
профиль_неудачника, _ = profiles.load("s3")
профиль_неудачника.keep_history = True
неудачник = Agent("неудачник", профиль_неудачника, store=memory.SessionStore(преграда / "сессия.jsonl"))
неудачник.remember("вопрос", "ответ")
check("сбой записи пополнения памяти не рушит", len(неудачник.history()) == 2, str(неудачник.history()))
check(
    "сбой записи назван текстом, а не исключением",
    "не удалось записать разговор" in (неудачник.store_error or ""),
    repr(неудачник.store_error),
)

# Шаг 9. Восстановление идёт ТЕМ ЖЕ методом пополнения, с выключенной на время загрузки
# записью на диск: иначе чтение файла тут же переписывало бы его самим собой.
путь_восстановления = разговоры / "восстановление.jsonl"
хранилище_восстановления = memory.SessionStore(путь_восстановления)
хранилище_восстановления.append("user", "прежний вопрос")
хранилище_восстановления.append("assistant", "прежний ответ")
байты_до_восстановления = путь_восстановления.read_bytes()
профиль_восстановления, _ = profiles.load("s3")
профиль_восстановления.keep_history = True
восстановленный = Agent("восстановленный", профиль_восстановления, store=хранилище_восстановления)
восстановленный.restore([("вопрос 1", "ответ 1"), ("вопрос 2", "ответ 2")])
check(
    "восстановленные пары легли в память в своём порядке",
    восстановленный.history()
    == [
        {"role": "user", "content": "вопрос 1"},
        {"role": "assistant", "content": "ответ 1"},
        {"role": "user", "content": "вопрос 2"},
        {"role": "assistant", "content": "ответ 2"},
    ],
    str(восстановленный.history()),
)
check(
    "восстановление не пишет на диск ни байта",
    путь_восстановления.read_bytes() == байты_до_восстановления,
    путь_восстановления.read_text(encoding="utf-8"),
)
# ОТКУДА ЧИСЛО: инструкция + две восстановленные пары (4 сообщения) + новый вопрос = 6.
запрос_после_восстановления = восстановленный.build_messages("новый вопрос")
check(
    "восстановленные пары уходят в следующий запрос",
    len(запрос_после_восстановления) == 6 and запрос_после_восстановления[1]["content"] == "вопрос 1",
    str([m["content"][:20] for m in запрос_после_восстановления]),
)
# Признак «идёт загрузка» обязан сниматься: иначе агент, однажды что-то восстановивший,
# перестал бы писать на диск до конца сеанса — и разговор пропал бы целиком.
восстановленный.remember("свежий вопрос", "свежий ответ", model="deepseek-v4-flash")
check(
    "после восстановления запись на диск возвращается",
    путь_восстановления.read_bytes() != байты_до_восстановления,
    путь_восстановления.read_text(encoding="utf-8"),
)

# Требование «память пополняется единственным способом» касается и внутренностей самого
# агента: вторая точка записи ведёт себя снаружи точно так же, и поведением её не поймать.
# Поэтому смотрим в дерево разбора — как в проверке запрещённых импортов.
def дописывающие_в_память(имя_файла):
    """Имена методов, которые дописывают в `self._messages` напрямую."""
    исходник = Path(__file__).resolve().parents[1] / "src" / "myharness" / имя_файла
    дерево = ast.parse(исходник.read_text(encoding="utf-8"))
    найденные = set()
    for узел in ast.walk(дерево):
        if not isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for внутри in ast.walk(узел):
            цель = None
            if (
                isinstance(внутри, ast.Call)
                and isinstance(внутри.func, ast.Attribute)
                and внутри.func.attr in {"append", "extend", "insert"}
            ):
                цель = внутри.func.value
            elif isinstance(внутри, ast.AugAssign):
                цель = внутри.target
            if (
                isinstance(цель, ast.Attribute)
                and цель.attr == "_messages"
                and isinstance(цель.value, ast.Name)
                and цель.value.id == "self"
            ):
                найденные.add(узел.name)
    return sorted(найденные)


точки_записи = дописывающие_в_память("agent.py")
check("память пополняется ровно из одного места", точки_записи == ["remember"], ", ".join(точки_записи))

# Шаг 10. Глобальные факты приходят ВЫЗЫВАЕМЫМ объектом, а не готовым списком: они меняются
# по ходу сеанса (командой `/remember` и архивариусом), и замороженный на старте список
# отстал бы к первому же запросу.
живые_факты = ["зовут Александр"]
профиль_знающего, _ = profiles.load("s3")  # keep_history=False — память здесь не при чём
знающий = Agent("знающий", профиль_знающего, facts=lambda: list(живые_факты))
системное_знающего = знающий.build_messages("вопрос")[0]
check(
    "факты подмешаны в системное сообщение",
    системное_знающего["role"] == "system"
    and (профиль_знающего.system or "") in системное_знающего["content"]
    and "зовут Александр" in системное_знающего["content"],
    str(системное_знающего)[:200],
)
check(
    "инструкция и факты разделены пустой строкой",
    системное_знающего["content"] == f"{профиль_знающего.system}\n\n{memory.facts_block(живые_факты)}",
    repr(системное_знающего["content"][-200:]),
)
живые_факты.append("любимый цвет — синий")
check(
    "факты берутся заново на каждый запрос",
    "любимый цвет — синий" in знающий.build_messages("второй вопрос")[0]["content"],
    знающий.build_messages("второй вопрос")[0]["content"][-120:],
)
живые_факты.clear()
check(
    "без фактов системное сообщение — прежнее слово в слово",
    знающий.build_messages("третий вопрос")[0]["content"] == профиль_знающего.system,
    repr(знающий.build_messages("третий вопрос")[0]["content"][-120:]),
)
незнающий = Agent("незнающий", профиль_знающего)
check(
    "агент, созданный без фактов, ведёт себя как раньше",
    незнающий.build_messages("вопрос")[0]["content"] == профиль_знающего.system,
    repr(незнающий.build_messages("вопрос")[0]["content"][-120:]),
)

# Профиль без инструкции, но с фактами: системное сообщение — один блок фактов. Без этого
# правила факты пропадали бы ровно у встроенного `default`, которым и пользуются чаще всего.
профиль_безмолвного, _ = profiles.load("s3")
профиль_безмолвного.system = None
безмолвный = Agent("безмолвный", профиль_безмолвного, facts=lambda: ["зовут Александр"])
check(
    "профиль без инструкции получает системное сообщение из одних фактов",
    безмолвный.build_messages("вопрос")[0]
    == {"role": "system", "content": memory.facts_block(["зовут Александр"])},
    str(безмолвный.build_messages("вопрос")[0]),
)
без_фактов: list[str] = []
пустой_безмолвный = Agent("пустой безмолвный", профиль_безмолвного, facts=lambda: без_фактов)
check(
    "без инструкции и без фактов системного сообщения нет вовсе",
    пустой_безмолвный.build_messages("вопрос")[0]["role"] == "user",
    str(пустой_безмолвный.build_messages("вопрос")[0]),
)

# Проверка «агент без фактов ведёт себя как раньше» отработала на НЕПУСТОЙ глобальной памяти —
# только так она что-то значит. Дальше факты считают поштучно, поэтому убираем за собой.
memory.remove_fact(1)
check("факт для проверок агента убран", memory.load_facts()[0] == [], str(memory.load_facts()[0]))


# Срыв восстановления посреди загрузки. Признак «идёт восстановление» снимается в `finally`,
# и без этого агент до конца сеанса молча не писал бы разговор на диск: память есть, файла нет.
# Обоснование записано в комментарии кода, а держать его должна проверка.
путь_срыва = разговоры / "срыв.jsonl"
профиль_срыва, _ = profiles.load("s3")
профиль_срыва.keep_history = True
сорванный = Agent("сорванный", профиль_срыва, store=memory.SessionStore(путь_срыва))
try:
    сорванный.restore([("вопрос 1", "ответ 1"), ("вопрос 2",)])  # вторая пара неполная
except (ValueError, TypeError):
    pass
сорванный.remember("после срыва", "ответ после срыва")
check(
    "после сорвавшегося восстановления запись на диск возвращается",
    путь_срыва.exists() and "после срыва" in путь_срыва.read_text(encoding="utf-8"),
    путь_срыва.read_text(encoding="utf-8") if путь_срыва.exists() else "файла нет",
)

# Источник фактов приходит снаружи, и его сбой не имеет права уронить обмен: сборка запроса
# идёт ДО начала обмена, и исключение отсюда улетело бы мимо всей обработки — ни ответа, ни
# записи прогона в журнал.
def срывающиеся_факты():
    raise RuntimeError("глобальная память недоступна")


профиль_сбойных, _ = profiles.load("s3")
сбойные = Agent("сбойные факты", профиль_сбойных, facts=срывающиеся_факты)
запрос_сбойных = сбойные.build_messages("вопрос")
check(
    "сбой источника фактов не роняет сборку запроса",
    запрос_сбойных[0]["content"] == профиль_сбойных.system,
    str(запрос_сбойных[0]),
)
check(
    "о сбое источника фактов сказано текстом",
    сбойные.store_error is not None and "глобальную память" in сбойные.store_error,
    str(сбойные.store_error),
)


print("\n10. Обмен агента с моделью")


class StubClient:
    """Подставной DeepSeek: отдаёт заданные события и запоминает, с чем его позвали.

    Управляемее, чем FakeClient из check_app.py: набор событий задаётся снаружи, вместо
    ответа можно бросить заданное исключение, а между событиями — ждать переданные
    «ворота» (`asyncio.Event`). Последнее нужно проверке отмены: пока ворота закрыты,
    поток стоит на полуслове, и обмен успевают отменить."""

    def __init__(self, events=None, error=None, gate=None):
        self.events = (
            list(events)
            if events is not None
            else [
                api.StreamEvent("reasoning", "прикидываю…"),
                api.StreamEvent("content", "щука"),
                api.StreamEvent("meta", finish_reason="stop", usage={"prompt_tokens": 12, "completion_tokens": 3}),
            ]
        )
        self.error = error
        self.gate = gate
        self.calls = []

    async def stream_chat(self, model, messages, params=None):
        self.calls.append({"model": model, "messages": [dict(m) for m in messages], "params": dict(params or {})})
        if self.error is not None:
            raise self.error
        for index, event in enumerate(self.events):
            if index and self.gate is not None:
                await self.gate.wait()
            yield event


def journal_lines():
    return Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").strip().splitlines()


# Шаг 17. Два обмена подряд копят память.
# ОТКУДА ЧИСЛА: подставной клиент отдаёт содержимое, значит каждый успешный обмен кладёт
# в память ровно одну пару = 2 сообщения; два обмена = 4. Во второй запрос уходит
# инструкция + пара из памяти + новый вопрос = 4.
диалоговый, _ = profiles.load("s3")
диалоговый.keep_history = True
беседа = Agent("беседа", диалоговый)
пара_обменов = StubClient()
asyncio.run(беседа.exchange(пара_обменов, "deepseek-v4-flash", "вопрос 1"))
asyncio.run(беседа.exchange(пара_обменов, "deepseek-v4-flash", "вопрос 2"))
check("после двух обменов в памяти две пары", len(беседа.history()) == 4, str(беседа.history()))
check(
    "роли идут парами",
    [m["role"] for m in беседа.history()] == ["user", "assistant", "user", "assistant"],
    str([m["role"] for m in беседа.history()]),
)
check(
    "во второй запрос ушла память первого",
    len(пара_обменов.calls[1]["messages"]) == 4,
    str(пара_обменов.calls[1]["messages"]),
)

# Шаг 61. Агент копит свою статистику: сколько обменов, сколько времени, сколько токенов.
# Считаем ВСЕ обмены, включая упавшие и отменённые: время на них потрачено, а список,
# показывающий только удачные, рисует агента дешевле, чем он есть на самом деле.


class МедленныйStubClient(StubClient):
    """Тот же подставной клиент, но с крошечной задержкой перед первым событием.

    Без неё обмен укладывается в микросекунды, `total_ms` считает целые миллисекунды и
    остаётся нулём — и проверка «время копится» ничего бы не проверяла."""

    async def stream_chat(self, model, messages, params=None):
        await asyncio.sleep(0.01)
        async for событие in super().stream_chat(model, messages, params):
            yield событие


check("расход берётся из total_tokens, когда сервер его дал", usage_tokens({"total_tokens": 100, "prompt_tokens": 7}) == 100)
check("без total_tokens слагаемые складываются", usage_tokens({"prompt_tokens": 12, "completion_tokens": 3}) == 15)
check("без usage расход считается нулём, а не ошибкой", usage_tokens(None) == 0 and usage_tokens({}) == 0)

# ОТКУДА ЧИСЛА: подставной клиент отдаёт usage с 12 и 3 — по 15 токенов за обмен, за два 30.
профиль_счетовода, _ = profiles.load("s3")
счетовод = Agent("счетовод", профиль_счетовода)
медленный = МедленныйStubClient()
asyncio.run(счетовод.exchange(медленный, "deepseek-v4-flash", "вопрос 1"))
asyncio.run(счетовод.exchange(медленный, "deepseek-v4-flash", "вопрос 2"))
check("два обмена посчитаны", счетовод.runs == 2, str(счетовод.runs))
check("время обменов накоплено", счетовод.total_ms > 0, str(счетовод.total_ms))
check("токены сложены по обоим ответам", счетовод.total_tokens == 30, str(счетовод.total_tokens))
asyncio.run(счетовод.exchange(StubClient(error=RuntimeError("сеть упала")), "deepseek-v4-flash", "вопрос 3"))
check("упавший обмен посчитан наравне с удачными", счетовод.runs == 3, str(счетовод.runs))
check("но токенов упавший обмен не прибавил", счетовод.total_tokens == 30, str(счетовод.total_tokens))

# Шаг 18. Сбой не оставляет следа в памяти.
до_сбоя = len(беседа.history())
сбойный = asyncio.run(
    беседа.exchange(StubClient(error=RuntimeError("сеть упала")), "deepseek-v4-flash", "вопрос 3")
)
check("сбой не выдаётся за успех", сбойный.ok is False, сбойный.status)
check("состояние прогона названо", сбойный.status == "error", сбойный.status)
check("текст ошибки сохранён", "сеть упала" in (сбойный.error or ""), repr(сбойный.error))
check("неотвеченный вопрос в памяти не осел", len(беседа.history()) == до_сбоя, str(беседа.history()))

# Шаг 19. Пустой ответ — не успех.
до_пустого = len(беседа.history())
пустой = asyncio.run(
    беседа.exchange(
        StubClient(events=[api.StreamEvent("meta", finish_reason="stop", usage={})]),
        "deepseek-v4-flash",
        "вопрос 4",
    )
)
check("пустой ответ не считается успехом", пустой.ok is False, f"{пустой.status} / {пустой.error!r}")

# Тот же пустой ответ, но оборванный по длине: причина известна, значит её надо назвать.
# Случай не выдуманный — на живом прогоне наряда модель израсходовала весь max_tokens на
# рассуждения и не начала ответ, заплачено 1412 токенов, показано «пустой ответ».
оборванный = asyncio.run(
    беседа.exchange(
        StubClient(events=[api.StreamEvent("meta", finish_reason="length", usage={})]),
        "deepseek-v4-flash",
        "вопрос 4a",
    )
)
check("обрыв по длине назван причиной пустого ответа", "max_tokens" in (оборванный.error or ""), repr(оборванный.error))
check("обрыв по длине — тоже не успех", оборванный.ok is False, оборванный.status)
check("пустой ответ в память не попадает", len(беседа.history()) == до_пустого, str(беседа.history()))

# Шаг 22. Запись в журнал — при любом исходе, силами самого агента.
одиночный_профиль, _ = profiles.load("s3")  # keep_history=False — память здесь не при чём
писарь = Agent("писарь", одиночный_профиль)
до_журнала = len(journal_lines())
asyncio.run(писарь.exchange(StubClient(), "deepseek-v4-flash", "вопрос без имени"))
после_журнала = journal_lines()
check("один обмен — одна запись", len(после_журнала) == до_журнала + 1, f"{до_журнала} → {len(после_журнала)}")
без_имени = json.loads(после_журнала[-1])
asyncio.run(
    писарь.exchange(StubClient(), "deepseek-v4-flash", "вопрос с именем", agent="аналитик", run_id="прогон-1")
)
с_именем = json.loads(journal_lines()[-1])
check(
    "имя исполнителя и прогон появляются только когда переданы",
    "agent" not in без_имени
    and "run_id" not in без_имени
    and с_именем.get("agent") == "аналитик"
    and с_именем.get("run_id") == "прогон-1",
    f"{без_имени} / {с_именем}",
)
check("ключ в журнал не попадает", "sk-" not in json.dumps(с_именем, ensure_ascii=False))

# Шаг 23. Порядок событий, склейка кусков и ошибка отрисовки.
роды = []
поточный = StubClient()
поток, _ = profiles.load("s3")
наблюдаемый = asyncio.run(
    Agent("наблюдатель", поток).exchange(
        поточный, "deepseek-v4-flash", "вопрос", on_event=lambda event: роды.append(event.kind)
    )
)
check("события приходят в своём порядке", роды == ["reasoning", "content", "meta"], str(роды))
check(
    "склейка кусков равна ответу",
    "".join(e.text for e in поточный.events if e.kind == "content") == наблюдаемый.text,
    repr(наблюдаемый.text),
)
check(
    "рассуждения собраны отдельно",
    "".join(e.text for e in поточный.events if e.kind == "reasoning") == наблюдаемый.reasoning,
    repr(наблюдаемый.reasoning),
)
# Запись обмена обязана быть полной: без неё прогон не воспроизвести и не сверить — по одному
# тексту ответа не видно ни что спросили, ни какой моделью, ни с какими параметрами.
check(
    "в записи обмена лежит то, что фактически ушло в модель",
    наблюдаемый.request_messages == поточный.calls[0]["messages"],
    str(наблюдаемый.request_messages),
)
check("в записи обмена названа модель", наблюдаемый.model == "deepseek-v4-flash", repr(наблюдаемый.model))
check(
    "в записи обмена лежит слепок профиля на момент запроса",
    наблюдаемый.profile_snapshot == поток.snapshot() and наблюдаемый.profile_snapshot.get("name") == "s3",
    str(наблюдаемый.profile_snapshot),
)
check(
    "слепок несёт инструкцию и параметры запроса",
    наблюдаемый.profile_snapshot.get("system") == поток.system
    and наблюдаемый.profile_snapshot.get("params") == поток.params,
    str(наблюдаемый.profile_snapshot),
)


def падающая_отрисовка(event):
    raise RuntimeError("панель закрыта")


кривой_профиль, _ = profiles.load("s3")
кривой = asyncio.run(
    Agent("кривой", кривой_профиль).exchange(
        StubClient(), "deepseek-v4-flash", "вопрос", on_event=падающая_отрисовка
    )
)
check(
    "ошибка отрисовки не выдаётся за сетевой сбой",
    кривой.ok is True and "DeepSeek" not in (кривой.error or ""),
    f"{кривой.status} / {кривой.error!r}",
)
# Одного «обмен удался, про DeepSeek не сказано» мало: этому условию отвечает и пустая ошибка,
# то есть полное молчание о том, что ответ получен, но на экран не попал. Пользователь при таком
# молчании видит пустую панель и не знает, повторять ли запрос, — поэтому текст обязан быть.
check(
    "про несостоявшийся показ ответа сказано прямо",
    bool(кривой.error) and "показать" in кривой.error and "панель закрыта" in кривой.error,
    repr(кривой.error),
)


# Шаг 24. Отмена по Ctrl+C: наверх пробрасывается, память цела, запись в журнале есть.
async def отменить_обмен(агент):
    ворота = asyncio.Event()  # так и не открываем: поток замирает после первого события
    клиент = StubClient(gate=ворота)
    задача = asyncio.create_task(агент.exchange(клиент, "deepseek-v4-flash", "вопрос на полуслове"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if клиент.calls:
            break
    задача.cancel()
    try:
        await задача
    except asyncio.CancelledError:
        return True
    return False


до_отмены_память = len(беседа.history())
до_отмены_журнал = len(journal_lines())
поймано = asyncio.run(отменить_обмен(беседа))
check("отмена пробрасывается наверх", поймано is True)
check("память при отмене не испорчена", len(беседа.history()) == до_отмены_память, str(беседа.history()))
после_отмены = journal_lines()
check(
    "отменённый прогон всё равно записан",
    len(после_отмены) == до_отмены_журнал + 1 and json.loads(после_отмены[-1])["status"] == "cancelled",
    после_отмены[-1],
)

# Шаг 25. `/clear` посреди идущего обмена. Команды harness исполняются немедленно, мимо
# очереди, — значит `forget()` приходит в середину обмена. Обмен, чью память успели забыть,
# свой результат в память возвращать НЕ ИМЕЕТ ПРАВА: пользователь увидел «история диалога
# очищена», сменил тему и обязан получить чистый разговор, а не пару от прежнего.
async def забыть_посреди_обмена(агент):
    ворота = asyncio.Event()  # держим поток на полуслове, пока не позовём forget()
    клиент = StubClient(gate=ворота)
    задача = asyncio.create_task(агент.exchange(клиент, "deepseek-v4-flash", "вопрос на полуслове"))
    for _ in range(100):
        await asyncio.sleep(0.01)
        if клиент.calls:
            break
    агент.forget()
    сразу = len(агент.history())
    ворота.set()  # отпускаем поток: обмен дойдёт до конца уже после забывания
    await задача
    return сразу, len(агент.history())


забывчивый_профиль, _ = profiles.load("s3")
забывчивый_профиль.keep_history = True
забывчивый = Agent("забывчивый", забывчивый_профиль)
сразу_после, после_обмена = asyncio.run(забыть_посреди_обмена(забывчивый))
check("forget() чистит память сразу", сразу_после == 0, str(сразу_после))
check(
    "обмен, чью память забыли, пару назад не кладёт",
    после_обмена == 0,
    f"в памяти {после_обмена} сообщений",
)
# И обратное утверждение: без вмешательства память по-прежнему пополняется — иначе «починка»
# свелась бы к тому, что память не пополняется никогда.
asyncio.run(забывчивый.exchange(StubClient(), "deepseek-v4-flash", "вопрос после очистки"))
check("обмен без вмешательства память пополняет", len(забывчивый.history()) == 2, str(забывчивый.history()))

# Шаг 8. Обмен и хранилище. Успешный обмен кладёт пару на диск, и модель в записи — та,
# с которой шёл именно этот обмен: модель меняется на лету командой `/model`, и записанная
# при создании агента врала бы обо всех последующих обменах.
путь_обмена = разговоры / "обмен.jsonl"
профиль_обменщика, _ = profiles.load("s3")
профиль_обменщика.keep_history = True
обменщик = Agent("обменщик", профиль_обменщика, store=memory.SessionStore(путь_обмена))
удачный_обмен = asyncio.run(обменщик.exchange(StubClient(), "deepseek-v4-pro", "вопрос на диск"))
строки_обмена = путь_обмена.read_text(encoding="utf-8").strip().splitlines()
check("успешный обмен записан на диск парой строк", len(строки_обмена) == 2, str(строки_обмена))
check(
    "на диск ушла модель этого обмена",
    len(строки_обмена) > 1 and json.loads(строки_обмена[1])["model"] == "deepseek-v4-pro",
    строки_обмена[-1] if строки_обмена else "",
)
check("у удачной записи ошибки нет", удачный_обмен.store_error is None, repr(удачный_обмен.store_error))

# Неуспешный обмен на диск не пишет ничего: пары нет — записывать нечего.
asyncio.run(обменщик.exchange(StubClient(error=RuntimeError("сеть упала")), "deepseek-v4-flash", "вопрос в пустоту"))
check(
    "упавший обмен на диск ничего не добавил",
    len(путь_обмена.read_text(encoding="utf-8").strip().splitlines()) == 2,
    путь_обмена.read_text(encoding="utf-8"),
)

# Сбой записи разговора не имеет права уронить обмен: ответ уже получен и уже оплачен.
# Зацепка срабатывает в `finally` — до записи в журнал и до сборки результата, и исключение
# оттуда уничтожило бы и ответ, и запись прогона.
профиль_падающего, _ = profiles.load("s3")
профиль_падающего.keep_history = True
падающий = Agent("падающий", профиль_падающего, store=memory.SessionStore(преграда / "обмен.jsonl"))
до_сбойной_записи = len(journal_lines())
итог_со_сбоем = asyncio.run(падающий.exchange(StubClient(), "deepseek-v4-flash", "вопрос при сбое диска"))
check("сбой записи разговора обмена не роняет", итог_со_сбоем.ok, f"{итог_со_сбоем.status} / {итог_со_сбоем.error!r}")
check("ответ при сбое записи цел", итог_со_сбоем.text == "щука", repr(итог_со_сбоем.text))
check(
    "сбой записи уехал наружу полем результата",
    "не удалось записать разговор" in (итог_со_сбоем.store_error or ""),
    repr(итог_со_сбоем.store_error),
)
check(
    "прогон при сбое записи всё равно попал в журнал",
    len(journal_lines()) == до_сбойной_записи + 1,
    f"{до_сбойной_записи} → {len(journal_lines())}",
)
# Ошибка записи описывает ИМЕННО этот обмен и в следующий не тянется: иначе интерфейс
# показывал бы её после каждого запроса до конца сеанса.
следующий_итог = asyncio.run(
    падающий.exchange(StubClient(error=RuntimeError("сеть упала")), "deepseek-v4-flash", "вопрос следом")
)
check("ошибка записи в следующий обмен не тянется", следующий_итог.store_error is None, repr(следующий_итог.store_error))


# Итог оркестратора кладёт в память главного агента `output.deliver` — вторая точка вызова
# `remember`, и правила у неё обязаны быть те же: запись на диск с моделью приложения и с
# оглядкой на `keep_history`.
class ПодставноеСостояние:
    """Ровно то, чем пользуется `output.deliver`: главный экран, его агент и имя модели."""

    def __init__(self, агент, модель):
        self.main = screens_mod.Screen(key=screens_mod.MAIN_KEY, title="главный")
        self.focus = self.main
        self.app = None
        self.main_agent = агент
        self.model = модель
        self.journal_warned = False
        self.store_warned = False


путь_итога = разговоры / "итог.jsonl"
профиль_итогового, _ = profiles.load("s3")
профиль_итогового.keep_history = True
итоговый = Agent("итоговый", профиль_итогового, store=memory.SessionStore(путь_итога))
output.deliver(
    ПодставноеСостояние(итоговый, "deepseek-v4-pro"),
    "вопрос группе",
    [output.Outcome("сводка группы", "lead", "готово")],
)
строки_итога = путь_итога.read_text(encoding="utf-8").strip().splitlines()
check("итог оркестратора лёг на диск", len(строки_итога) == 2, str(строки_итога))
check(
    "в записи итога — модель приложения",
    len(строки_итога) > 1 and json.loads(строки_итога[1])["model"] == "deepseek-v4-pro",
    строки_итога[-1] if строки_итога else "",
)

путь_итога_без_истории = разговоры / "итог-без-истории.jsonl"
профиль_безысторного, _ = profiles.load("s3")  # keep_history=False
безысторный = Agent("безысторный", профиль_безысторного, store=memory.SessionStore(путь_итога_без_истории))
output.deliver(
    ПодставноеСостояние(безысторный, "deepseek-v4-pro"),
    "вопрос способам",
    [output.Outcome("ответ способа", "plain", "готово")],
)
# Сбой записи итога обязан быть назван вслух. Здесь `Turn` не собирается, и без отдельного
# предупреждения ошибка ушла бы в тишину: человек уверен, что разговор переживёт перезапуск.
не_каталог = разговоры / "не-каталог"
не_каталог.write_text("я обычный файл", encoding="utf-8")
профиль_немого, _ = profiles.load("s3")
профиль_немого.keep_history = True
немой = Agent("немой", профиль_немого, store=memory.SessionStore(не_каталог / "итог.jsonl"))
состояние_немого = ПодставноеСостояние(немой, "deepseek-v4-pro")
output.deliver(состояние_немого, "вопрос", [output.Outcome("сводка группы", "lead", "готово")])
лента_немого = "".join(текст for _, текст in состояние_немого.main.first.log)
check(
    "о сбое записи итога сказано в ленте",
    "не удалось записать разговор" in лента_немого,
    лента_немого[-200:],
)
output.deliver(состояние_немого, "второй вопрос", [output.Outcome("сводка группы", "lead", "снова")])
лента_немого_2 = "".join(текст for _, текст in состояние_немого.main.first.log)
check(
    "о сбое записи говорят один раз за сеанс",
    лента_немого_2.count("не удалось записать разговор") == 1,
    str(лента_немого_2.count("не удалось записать разговор")),
)

# Второй путь показа той же ошибки — обычный обмен. Поднимать ради этого настоящий обмен с
# подставным клиентом дорого, а правило важное: проверяем разбором дерева, что `run_turn`
# действительно зовёт предупреждение. Проверка слабее опытной, но правило она держит —
# без неё вызов молча выпадал при правке, и мутация это подтвердила.
дерево_вывода = ast.parse(Path(myharness_dir := Path(output.__file__)).read_text(encoding="utf-8"))
зовущие_warn_store = {
    узел.name
    for узел in ast.walk(дерево_вывода)
    if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef))
    and any(
        isinstance(в, ast.Call) and isinstance(в.func, ast.Name) and в.func.id == "warn_store"
        for в in ast.walk(узел)
    )
}
check(
    "о сбое записи разговора предупреждают и после обычного обмена",
    "run_turn" in зовущие_warn_store,
    str(sorted(зовущие_warn_store)),
)

check(
    "итог при выключенной истории на диск не пишется",
    not путь_итога_без_истории.exists(),
    str(путь_итога_без_истории),
)

print("\n10a. Ведущий группы сводит разово")

# Ведущему на вход приходит уже готовый текст — задача и ответы экспертов, — и помнить
# прошлые заседания ему незачем. Помнил бы — каждый следующий прогон тащил бы в САМЫЙ
# дорогой запрос инструмента весь предыдущий: и ответы экспертов, и прежнюю сводку.
# ОТКУДА ЧИСЛО: разовый запрос сводки — это инструкция ведущего плюс текст со всеми
# ответами, то есть ровно 2 сообщения, сколько бы прогонов ни было до него.
(tmp / "profiles" / "solo.json").write_text(
    json.dumps({"name": "solo", "system": "ты одиночка", "keep_history": False}, ensure_ascii=False),
    encoding="utf-8",
)
(tmp / "profiles" / "chief.json").write_text(
    # keep_history не задан — значит true, как у обычного профиля пользователя: именно на
    # таком профиле копившаяся сводка и вылезла.
    json.dumps({"name": "chief", "system": "сведи ответы экспертов", "agents": ["solo"]}, ensure_ascii=False),
    encoding="utf-8",
)
шеф, _ = profiles.load("chief")
check("у профиля ведущего история включена, как у любого профиля", шеф.keep_history is True)
шеф_состояние = cli.State(config=Config(api_key="sk-test"), client=StubClient(), model="deepseek-v4-flash", profile=шеф)
asyncio.run(team.run(шеф_состояние, "вопрос первого заседания", шеф))
asyncio.run(team.run(шеф_состояние, "вопрос второго заседания", шеф))
сводки = [
    вызов for вызов in шеф_состояние.client.calls
    if вызов["messages"][0]["content"] == "сведи ответы экспертов"
]
check("сводка собиралась на каждом прогоне", len(сводки) == 2, str(len(сводки)))
check(
    "второй прогон не тащит в сводку первый",
    len(сводки[1]["messages"]) == 2,
    str([m["role"] for m in сводки[1]["messages"]]),
)
check(
    "в запросе сводки только инструкция и материал",
    [m["role"] for m in сводки[1]["messages"]] == ["system", "user"],
    str([m["role"] for m in сводки[1]["messages"]]),
)
check("профиль пользователя правкой не задет", шеф.keep_history is True and шеф.system == "сведи ответы экспертов")

# Тот же профиль сводки, но у ведущего нет своей инструкции: подставляется запасная — и
# разовость от этого не зависит (раньше при непустой инструкции возвращался сам профиль).
безмолвный, _ = profiles.load("chief")
безмолвный.system = None
сводный = team.summary_profile(безмолвный)
check("без своей инструкции берётся запасная", сводный.system == team.DEFAULT_LEAD_INSTRUCTION, str(сводный.system))
check("запасная инструкция не пишется в сам профиль ведущего", безмолвный.system is None)
check("профиль сводки — всегда копия", сводный is not безмолвный and team.summary_profile(шеф) is not шеф)
check(
    "сводка не помнит прошлых заседаний в обоих случаях",
    сводный.keep_history is False and team.summary_profile(шеф).keep_history is False,
    f"{сводный.keep_history} / {team.summary_profile(шеф).keep_history}",
)

print("\n11. Изоляция агента")

# Главное свойство дня — разговор с моделью не тянет за собой интерфейс. Утверждаем это
# проверкой, а не обещанием в описании: обещание молча перестаёт быть правдой после первого
# же «просто добавлю сюда импорт».

# Смотреть в собственный sys.modules нельзя: сам этот файл импортирует `cli`, значит
# `prompt_toolkit` в нём уже загружен и проверка была бы всегда «зелёной». Нужен чистый
# процесс, ничего, кроме `myharness.agent`, не импортировавший.
КОД_ПРОБЫ = (
    "import sys, myharness.agent; "
    "print(','.join(sorted(m for m in sys.modules if m.startswith('prompt_toolkit'))))"
)
проба = subprocess.run(
    [sys.executable, "-c", КОД_ПРОБЫ],
    capture_output=True,
    text=True,
    check=False,  # ненулевой код возврата разбираем сами, ниже, — он тоже сбой проверки
)
подтянутое = проба.stdout.strip()
check(
    "импорт myharness.agent не тянет интерфейс",
    проба.returncode == 0 and not подтянутое,
    подтянутое or проба.stderr.strip(),
)

# Второе утверждение — про сам текст `agent.py`: каких имён в его импортах быть не должно.
# Разбираем через `ast`, а не поиском по строкам: в модуле эти имена упоминаются в
# пояснениях («показ целиком — в output»), и поиск по строкам споткнулся бы о комментарий,
# объявив нарушением то, что нарушением не является.
ЗАПРЕЩЁННЫЕ = {"cli", "screens", "team", "methods", "output", "ui", "prompt_toolkit"}


def запретное_в_импортах(имя_файла):
    """Какие из запрещённых имён импортирует модуль. Разбор идёт по дереву, а не поиском по
    строкам: эти имена упоминаются в пояснениях («показ целиком — в output»), и поиск по
    строкам споткнулся бы о комментарий, объявив нарушением то, что нарушением не является."""
    исходник = Path(__file__).resolve().parents[1] / "src" / "myharness" / имя_файла
    дерево = ast.parse(исходник.read_text(encoding="utf-8"))
    импортированное = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.Import):
            импортированное.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
        elif isinstance(узел, ast.ImportFrom):
            if узел.module:
                импортированное.add(узел.module.split(".")[0])
            # `from . import api, journal` — имя модуля лежит в самих псевдонимах
            if узел.level and not узел.module:
                импортированное.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
    return sorted(импортированное & ЗАПРЕЩЁННЫЕ)


нарушения = запретное_в_импортах("agent.py")
check("agent.py не импортирует интерфейс", not нарушения, ", ".join(нарушения))

print("\n12. Окно памяти")

# Память не может расти без предела. Причины две, и обе не про деньги: сеанс упирается в
# предел окна контекста примерно на сотом обмене и просто перестаёт работать, а задолго до
# того модель начинает тянуть формат и тему из старых обменов и хуже слышит свежую
# инструкцию. Режем блоками, а не по одной паре за ход: сдвигай окно каждый ход — и начало
# запроса менялось бы каждый ход, обнуляя повторное использование неизменного начала
# на стороне сервера.

# ОТКУДА ЧИСЛА: сосчитаны по правилу, а не подсмотрены у кода. Окно — 3 пары, запас
# WINDOW_SLACK_PAIRS = 5, значит порог обрезки — 3 + 5 = 8 пар. Обрезка идёт В НАЧАЛЕ
# сборки запроса, то есть до отправки: она ограничивает то, что уходит в модель.
# Обмены с 1-го по 8-й застают в памяти 0…7 пар — порога нет, обрезки нет. После восьмого
# в памяти 8 пар = 16 сообщений. Девятый обмен застаёт порог: выброшены 5 старейших пар,
# осталось 3 пары = 6 сообщений, и в запрос уходит инструкция + 6 + новый вопрос = 8
# сообщений. После девятого в памяти 3 + 1 = 4 пары = 8 сообщений.
оконный_профиль, _ = profiles.load("s3")
оконный_профиль.keep_history = True
оконный_профиль.history_window = 3
окно = Agent("окно", оконный_профиль)
оконный_клиент = StubClient()
восьмой = None
for номер in range(1, 9):
    восьмой = asyncio.run(окно.exchange(оконный_клиент, "deepseek-v4-flash", f"вопрос {номер}"))
check("до порога память не трогается", len(окно.history()) == 16, str(len(окно.history())))
check("до порога ничего не выброшено", восьмой.dropped_pairs == 0, str(восьмой.dropped_pairs))

девятый = asyncio.run(окно.exchange(оконный_клиент, "deepseek-v4-flash", "вопрос 9"))
check("на пороге выброшен целый блок", девятый.dropped_pairs == 5, str(девятый.dropped_pairs))
check(
    "в запрос ушло только окно",
    len(оконный_клиент.calls[8]["messages"]) == 8,
    str([m["content"] for m in оконный_клиент.calls[8]["messages"]]),
)
check("после обрезки память соответствует окну", len(окно.history()) == 8, str(len(окно.history())))

# Чего обрезка не имеет права выбросить. Каждое из этих утверждений закрывает свой отказ:
# начатая с ответа память — отказ сервера в случайный момент, а не при проверках;
# нечётная длина — распавшаяся пара; потерянная последняя пара — забытый только что
# заданный вопрос.
память = окно.history()
check("память начинается с вопроса", память[0]["role"] == "user", память[0]["role"])
check("резали целыми парами", len(память) % 2 == 0, str(len(память)))
check(
    "последняя пара на месте",
    память[-2] == {"role": "user", "content": "вопрос 9"} and память[-1]["content"] == девятый.text,
    str(память[-2:]),
)
check(
    "системная инструкция не выброшена",
    оконный_клиент.calls[8]["messages"][0]["role"] == "system",
    str(оконный_клиент.calls[8]["messages"][0]),
)

# Аварийный потолок по объёму. Окно по числу пар не спасает от одного случая: пользователь
# вставил в вопрос файл, и одна пара весит больше всего окна контекста. Окно по парам здесь
# выключено (history_window = 0) — так видно, что сработал именно потолок. Две пары по
# 40 000 знаков в каждой половине дают 160 000 знаков против потолка 60 000: старшая пара
# уходит, последняя не трогается никогда.
объёмный_профиль, _ = profiles.load("s3")
объёмный_профиль.keep_history = True
объёмный_профиль.history_window = 0
толстяк = Agent("толстяк", объёмный_профиль)
толстяк.remember("х" * 40_000, "ы" * 40_000)
толстяк.remember("э" * 40_000, "ю" * 40_000)
толстый = asyncio.run(толстяк.exchange(StubClient(), "deepseek-v4-flash", "коротко"))
check("потолок по объёму выбросил старейшую пару", толстый.dropped_pairs == 1, str(толстый.dropped_pairs))
check(
    "последняя пара уцелела даже за потолком",
    len(толстяк.history()) == 4 and толстяк.history()[0]["content"].startswith("э"),
    str([len(m["content"]) for m in толстяк.history()]),
)

print("\n13. Пакетный наряд")

# Пакетный режим — это ещё и проверка изоляции ДЕЛОМ. Про `agent` сказано, что он отделён от
# интерфейса; наряд либо выполняется в процессе, где `prompt_toolkit` не загружен вовсе,
# либо не выполняется. Поэтому здесь, кроме разбора и выполнения наряда, повторена проба
# из раздела 11 — уже для `batch`.

# Профиль-заготовка с НЕразрешённой переменной: `$область` в самом файле не подставлена, и
# это не ошибка — её подставляет каждое задание наряда своим `vars`. Так один файл даёт
# сколько угодно разных собеседников. Проверено отдельно: `safe_substitute` оставляет
# неразрешённое имя в тексте как есть и загрузку профиля не роняет.
(tmp / "profiles" / "expert.json").write_text(
    json.dumps(
        {
            "name": "expert",
            "description": "заготовка эксперта",
            "system": "Ты эксперт в области $область. Отвечай одним предложением.",
        },
        ensure_ascii=False,
    ),
    encoding="utf-8",
)


def наряд(данные, имя="order.json"):
    """Файл наряда во временном каталоге — путь к нему."""
    путь = tmp / имя
    путь.write_text(json.dumps(данные, ensure_ascii=False), encoding="utf-8")
    return путь


полный, предупреждения = batch.load_order(
    наряд(
        {
            "model": "deepseek-v4-flash",
            "concurrency": 3,
            "tasks": [
                {"agent": "физик", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос 1"},
                {"agent": "химик", "profile": "expert", "vars": {"область": "химия"}, "ask": "вопрос 2"},
                {"agent": "биолог", "profile": "expert", "vars": {"область": "биология"}, "ask": "вопрос 3"},
            ],
        }
    )
)
check("наряд с тремя заданиями читается", len(полный.tasks) == 3, str(len(полный.tasks)))
check("чистый наряд не даёт предупреждений", предупреждения == [], "; ".join(предупреждения))
check("модель взята из наряда", полный.model == "deepseek-v4-flash", полный.model)
check("одновременность взята из наряда", полный.concurrency == 3, str(полный.concurrency))
check(
    "vars задания подставлены в инструкцию заготовки",
    полный.tasks[0].prepared.system == "Ты эксперт в области физика. Отвечай одним предложением.",
    str(полный.tasks[0].prepared.system),
)
check(
    "у каждого задания своя подстановка",
    "биология" in полный.tasks[2].prepared.system and "физика" not in полный.tasks[2].prepared.system,
    str(полный.tasks[2].prepared.system),
)
# А это — про саму заготовку: подстановка обязана делать копию. Правка на месте сегодня
# незаметна (каждое задание грузит профиль само), но станет молчаливой порчей в тот день,
# когда заготовку начнут грузить один раз на весь наряд.
общая_заготовка, _ = profiles.load("expert")
инструкция_до = общая_заготовка.system
batch._prepare(общая_заготовка, {"область": "физика"})
check(
    "подстановка не портит саму заготовку",
    общая_заготовка.system == инструкция_до,
    str(общая_заготовка.system),
)

# ОТКУДА ЧИСЛО: восьмёрка задана условием (`DEFAULT_CONCURRENCY`), а не подсмотрена в коде.
без_поля, _ = batch.load_order(
    наряд(
        {"tasks": [{"agent": "один", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"}]},
        "no-concurrency.json",
    )
)
check("умолчание одновременности — восемь", без_поля.concurrency == 8, str(без_поля.concurrency))

# Мусор в наряде не роняет разбор: непригодное задание пропускается с предупреждением,
# остальные читаются. Молча подставленный `default` вместо пропавшего профиля сделал бы
# собеседника безликим, а результат наряда необъяснимым, — так же поступает и группа
# агентов (`team.load_agents`).
кривой, кривые = batch.load_order(
    наряд(
        {
            "tasks": [
                {"agent": "целый", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"},
                {"agent": "немой", "profile": "expert", "vars": {"область": "химия"}},
                {"agent": "чужой", "profile": "нет-такого-профиля", "ask": "вопрос"},
            ]
        },
        "broken.json",
    )
)
имена = [задание.agent for задание in кривой.tasks]
check("задание без вопроса пропускается", "немой" not in имена, str(имена))
check(
    "про задание без вопроса есть предупреждение",
    any("немой" in строка for строка in кривые),
    "; ".join(кривые),
)
check("задание с неизвестным профилем пропускается", "чужой" not in имена, str(имена))
check(
    "про неизвестный профиль есть предупреждение",
    any("чужой" in строка and "не найден" in строка for строка in кривые),
    "; ".join(кривые),
)
check("пригодное задание уцелело", имена == ["целый"], str(имена))


class Считающий:
    """Подставной DeepSeek, следящий за ОДНОВРЕМЕННОСТЬЮ: сколько запросов идёт разом.

    Задержка внутри ответа обязательна: без неё задания успевают отработать по очереди, пик
    оказывается единицей независимо от ограничителя, и проверка «зеленеет» на сломанном коде.
    Заданное слово в вопросе превращает ответ в сбой — этим проверяется, что упавшее задание
    не обрывает наряд."""

    def __init__(self, задержка=0.02, падать_на=""):
        self.задержка = задержка
        self.падать_на = падать_на
        self.сейчас = 0
        self.пик = 0
        self.всего = 0

    async def stream_chat(self, model, messages, params=None):
        self.всего += 1
        if self.падать_на and self.падать_на in messages[-1]["content"]:
            raise RuntimeError("сеть упала")
        self.сейчас += 1
        self.пик = max(self.пик, self.сейчас)
        try:
            await asyncio.sleep(self.задержка)
            yield api.StreamEvent("content", "ответ")
            yield api.StreamEvent("meta", finish_reason="stop", usage={"total_tokens": 7})
        finally:
            self.сейчас -= 1


# ОТКУДА ЧИСЛА: шесть заданий и одновременность 2 записаны в самом наряде. Значит пик
# одновременных запросов обязан быть ровно 2, а выполниться обязаны все 6 — ограничитель
# задерживает работу, но не отменяет её.
шестёрка, _ = batch.load_order(
    наряд(
        {
            "concurrency": 2,
            "tasks": [
                {
                    "agent": f"агент {номер}",
                    "profile": "expert",
                    "vars": {"область": f"тема {номер}"},
                    "ask": f"вопрос {номер}",
                }
                for номер in range(1, 7)
            ],
        },
        "six.json",
    )
)
счётчик = Считающий()
ход_работы = []
шесть_ответов = asyncio.run(batch.run_order(шестёрка, счётчик, on_line=ход_работы.append))
check("одновременно шло не больше двух", счётчик.пик == 2, str(счётчик.пик))
check("выполнены все шесть", счётчик.всего == 6, str(счётчик.всего))
check("ответов ровно по числу заданий", len(шесть_ответов) == 6, str(len(шесть_ответов)))
check("все шесть ответили", all(ответ.ok for ответ in шесть_ответов), str([о.status for о in шесть_ответов]))
# Ход работы уходит строками в `on_line`, а не печатается: иначе проверить `run_order`
# можно было бы только перехватом вывода процесса.
check("ход работы ушёл в on_line", len(ход_работы) >= 6, str(len(ход_работы)))
# Один прогон — один `run_id` на весь наряд, а имя задания стоит в поле `agent`.
записи = [json.loads(строка) for строка in journal_lines()[-6:]]
check("у всего наряда один run_id", len({запись["run_id"] for запись in записи}) == 1, str(записи[0].get("run_id")))
check(
    "в журнале стоит имя задания",
    {запись["agent"] for запись in записи} == {f"агент {номер}" for номер in range(1, 7)},
    str(sorted(запись["agent"] for запись in записи)),
)

# Сбой одного задания не имеет права обрывать наряд: `gather` собирает все ответы.
сбойный, _ = batch.load_order(
    наряд(
        {
            "tasks": [
                {"agent": "первый", "profile": "expert", "vars": {"область": "физика"}, "ask": "обычный вопрос"},
                {"agent": "падающий", "profile": "expert", "vars": {"область": "химия"}, "ask": "вопрос падать"},
                {"agent": "третий", "profile": "expert", "vars": {"область": "биология"}, "ask": "ещё вопрос"},
            ]
        },
        "with-failure.json",
    )
)
капризный = Считающий(падать_на="падать")
итоги = asyncio.run(batch.run_order(сбойный, капризный, on_line=lambda _: None))
check(
    "сбой одного задания не отменил остальные",
    [ответ.ok for ответ in итоги] == [True, False, True],
    str([ответ.status for ответ in итоги]),
)

сводка = batch.summary_lines(сбойный, итоги)
текст_сводки = "\n".join(сводка)
check("в заголовке сводки есть модель и число заданий", "заданий 3" in сводка[0], сводка[0])
check(
    "в сводке есть имя каждого задания",
    all(задание.agent in текст_сводки for задание in сбойный.tasks),
    текст_сводки,
)
check(
    "у сбойного задания видно, что он не удался",
    any("падающий" in строка and "сбой" in строка for строка in сводка),
    текст_сводки,
)
check(
    "удавшиеся задания в сводке остались",
    sum(1 for строка in сводка if " ок " in строка) == 2,
    текст_сводки,
)
check("итоговая строка считает ответивших", "ответили 2 из 3" in сводка[-1], сводка[-1])

# И-7. Счёт токенов — одно правило в одном месте. Своя копия правила в пакетном режиме
# считала иначе и падала на строке вместо числа: сервер отдаёт `usage` как есть, и ронять на
# нём сводку по уже выполненному наряду недопустимо — работа сделана и оплачена.
check("счёт токенов в наряде — та же функция, что у агента", batch.usage_tokens is usage_tokens)
check("второй функции подсчёта в наряде не осталось", not hasattr(batch, "_tokens"))
одиночный_наряд, _ = batch.load_order(
    наряд(
        {"tasks": [{"agent": "один", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"}]},
        "tokens.json",
    )
)
# ОТКУДА ЧИСЛО: в `usage` пришла строка вместо числа во входных токенах — по правилу агента
# нечисловое слагаемое просто не считается, остаются 3 выходных.
кривой_расход = batch.summary_lines(
    одиночный_наряд,
    [Turn(status="ok", usage={"prompt_tokens": "двенадцать", "completion_tokens": 3})],
)
check("строка вместо числа в usage не роняет сводку", "израсходовано 3 токенов" in кривой_расход[-1], кривой_расход[-1])


# Ветка, которую иначе не достать. Сетевые сбои `exchange` ловит сам и возвращает `Turn`,
# поэтому до `gather` исключение долетает только из кода ВОКРУГ обмена — например, из
# сорвавшегося `on_line` вызывающего. Наряд обязан досчитаться и здесь: один сорвавшийся
# обработчик строк не стоит всей остальной работы.
def срывающийся(строка):
    if "падающий" in строка:
        raise RuntimeError("обработчик строк сорвался")


устойчивые = asyncio.run(batch.run_order(сбойный, Считающий(), on_line=срывающийся))
check(
    "сорвавшийся обработчик строк не отменил наряд",
    [ответ.ok for ответ in устойчивые] == [True, False, True],
    str([ответ.status for ответ in устойчивые]),
)
check(
    "сорванное задание записано сбоем, а не потеряно",
    устойчивые[1].error == "обработчик строк сорвался",
    str(устойчивые[1].error),
)

# Мусор в поле «concurrency». Отдельной строкой отсеивается `bool`: в Python он подкласс
# `int`, и `true` иначе прошло бы как «один запрос за раз» — наряд выполнялся бы по одному
# заданию, и нигде бы об этом не говорилось.
for мусор in ("два", True, 0, -1, 2.5):
    кривая, кривые_предупреждения = batch.load_order(
        наряд(
            {
                "concurrency": мусор,
                "tasks": [{"agent": "один", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"}],
            },
            "bad-concurrency.json",
        )
    )
    check(
        f"concurrency = {мусор!r} отвергнут с предупреждением",
        кривая.concurrency == 8 and any("concurrency" in строка for строка in кривые_предупреждения),
        f"{кривая.concurrency} / {кривые_предупреждения}",
    )

# Задание без имени работу не теряет — имя лишь метка в журнале и в сводке. Но молчать нельзя:
# без предупреждения человек не поймёт, откуда в сводке взялось имя, которого он не писал.
безымянный, безымянные = batch.load_order(
    наряд(
        {"tasks": [{"profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"}]},
        "no-name.json",
    )
)
check("задание без имени не выброшено", len(безымянный.tasks) == 1, str(len(безымянный.tasks)))
check("ему назначено имя по порядковому номеру", безымянный.tasks[0].agent == "задание 1", безымянный.tasks[0].agent)
check(
    "о назначенном имени предупреждено",
    any("нет имени" in строка and "задание 1" in строка for строка in безымянные),
    "; ".join(безымянные),
)

print("\n13a. Точка входа пакетного режима")

# Код возврата наряда читают расписания и оболочки: молчаливый ноль на сорванном наряде
# означает, что о сбое никто не узнает. Ключи `--model` и `--concurrency` — единственный способ
# прогнать один и тот же наряд другой моделью или мягче по частоте, и они обязаны доходить до
# запроса, а не оставаться украшением справки.


class НарядныйКлиент:
    """Подставной DeepSeek для точки входа: помнит, какой моделью его звали и сколько запросов
    шло разом. Слово «падать» в вопросе превращает ответ в сбой."""

    последний = None

    def __init__(self, api_key):
        self.api_key = api_key
        self.модели = []
        self.сейчас = 0
        self.пик = 0
        self.закрыт = False
        НарядныйКлиент.последний = self

    async def stream_chat(self, model, messages, params=None):
        self.модели.append(model)
        self.сейчас += 1
        self.пик = max(self.пик, self.сейчас)
        try:
            await asyncio.sleep(0.02)
            if "падать" in messages[-1]["content"]:
                raise RuntimeError("сеть упала")
            yield api.StreamEvent("content", "ответ")
            yield api.StreamEvent("meta", finish_reason="stop", usage={"total_tokens": 5})
        finally:
            self.сейчас -= 1

    async def aclose(self):
        self.закрыт = True


def выполнить_наряд(путь, *, model=None, concurrency=None):
    """Точка входа целиком, с подставным клиентом. Возвращает код возврата и напечатанное."""
    вывод = io.StringIO()
    настоящий = batch.api.DeepSeekClient
    batch.api.DeepSeekClient = НарядныйКлиент
    try:
        with contextlib.redirect_stdout(вывод):
            код = asyncio.run(
                batch.main(argparse.Namespace(batch=str(путь), model=model, concurrency=concurrency))
            )
    finally:
        batch.api.DeepSeekClient = настоящий
    return код, вывод.getvalue()


# Ключ пакетному режиму приходит из файла настроек — своего он не спрашивает. Каталог настроек
# уведён во временный ещё в начале файла, настоящий ключ пользователя не задет.
(tmp / "config").mkdir(parents=True, exist_ok=True)
(tmp / "config" / "config.json").write_text(
    json.dumps({"api_key": "sk-проверочный", "model": "deepseek-v4-flash", "profile": "default"}, ensure_ascii=False),
    encoding="utf-8",
)

удачный_путь = наряд(
    {
        "model": "deepseek-v4-flash",
        "concurrency": 4,
        "tasks": [
            {"agent": "первый", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"},
            {"agent": "второй", "profile": "expert", "vars": {"область": "химия"}, "ask": "ещё вопрос"},
            {"agent": "третий", "profile": "expert", "vars": {"область": "биология"}, "ask": "и ещё"},
        ],
    },
    "entry-ok.json",
)
# Наряд разговора не сохраняет и не восстанавливает. Требование `openspec/specs/batch/`:
# наряд самодостаточен, и подмешанная в него вчерашняя переписка сделала бы результат
# невоспроизводимым. Плюс восемь одновременных заданий писали бы в один файл наперегонки.
состояние_до_наряда = файлы_состояния()
код_успеха, вывод_успеха = выполнить_наряд(удачный_путь)
check(
    "наряд не оставляет следа в каталоге состояния",
    файлы_состояния() == состояние_до_наряда,
    str(set(файлы_состояния()) - set(состояние_до_наряда)),
)
check("наряд, где ответили все, даёт код 0", код_успеха == 0, str(код_успеха))
check("сводка напечатана", "ответили 3 из 3" in вывод_успеха, вывод_успеха)
check("клиент закрыт после наряда", НарядныйКлиент.последний.закрыт is True)

сорванный_путь = наряд(
    {
        "model": "deepseek-v4-flash",
        "tasks": [
            {"agent": "первый", "profile": "expert", "vars": {"область": "физика"}, "ask": "вопрос"},
            {"agent": "падающий", "profile": "expert", "vars": {"область": "химия"}, "ask": "вопрос падать"},
        ],
    },
    "entry-fail.json",
)
код_сбоя, вывод_сбоя = выполнить_наряд(сорванный_путь)
check("сорвавшееся задание даёт ненулевой код возврата", код_сбоя != 0, str(код_сбоя))
check("остальные задания при этом выполнены", "ответили 1 из 2" in вывод_сбоя, вывод_сбоя)

# ОТКУДА ЧИСЛА: в самом наряде записаны модель `deepseek-v4-flash` и одновременность 4.
# Ключи командной строки обязаны их перебить — иначе прогнать тот же наряд другой моделью
# или мягче по частоте нельзя.
код_ключей, вывод_ключей = выполнить_наряд(удачный_путь, model="deepseek-v4-pro", concurrency=1)
клиент_ключей = НарядныйКлиент.последний
check("наряд с ключами выполнен", код_ключей == 0, str(код_ключей))
check(
    "--model перебил модель наряда в самом запросе",
    set(клиент_ключей.модели) == {"deepseek-v4-pro"},
    str(set(клиент_ключей.модели)),
)
check("--model виден и в сводке", "модель deepseek-v4-pro" in вывод_ключей, вывод_ключей.splitlines()[-2:])
check("--concurrency ограничил одновременность до одного", клиент_ключей.пик == 1, str(клиент_ключей.пик))
check("--concurrency виден и в сводке", "одновременно 1" in вывод_ключей, вывод_ключей.splitlines()[-2:])

# И то же утверждение об изоляции, что в разделе 11, — теперь про сам пакетный режим.
# Чистый процесс: этот файл импортирует `cli`, значит в собственном `sys.modules`
# `prompt_toolkit` уже есть и проверка была бы всегда «зелёной».
ПРОБА_НАРЯДА = (
    "import sys, myharness.batch; print(','.join(sorted(m for m in sys.modules if m.startswith('prompt_toolkit'))))"
)
проба_наряда = subprocess.run(
    [sys.executable, "-c", ПРОБА_НАРЯДА],
    capture_output=True,
    text=True,
    check=False,
)
подтянутое_нарядом = проба_наряда.stdout.strip()
check(
    "импорт myharness.batch не тянет интерфейс",
    проба_наряда.returncode == 0 and not подтянутое_нарядом,
    подтянутое_нарядом or проба_наряда.stderr.strip(),
)
нарушения_наряда = запретное_в_импортах("batch.py")
check("batch.py не импортирует интерфейс", not нарушения_наряда, ", ".join(нарушения_наряда))

print("\n14. Сто агентов в одном процессе")

# Наряд на сотню заданий должен подниматься мгновенно. ОТКУДА ЧИСЛА: сто агентов — условие
# проверки, порог в секунду взят с большим запасом. Создание объекта — доли миллисекунды,
# и порог ловит не медлительность, а настоящую беду: поднятие сетевого соединения на каждого
# агента. Именно ради этого клиент API приходит аргументом вызова, а не хранится в агенте.
заготовка, _ = profiles.load("expert")
начало = time.perf_counter()
сотня = [
    Agent(f"агент {номер}", batch._prepare(заготовка, {"область": f"тема {номер}"}))
    for номер in range(100)
]
затрачено = time.perf_counter() - начало
check("создано сто агентов", len(сотня) == 100, str(len(сотня)))
check(
    "настройки у всех разные",
    len({агент.profile.system for агент in сотня}) == 100,
    str(len({агент.profile.system for агент in сотня})),
)
# Память у каждого своя: общий список сообщений на всех означал бы, что сотый агент читает
# чужой разговор и отвечает не на то.
сотня[0].remember("вопрос первому", "ответ первого")
check("памяти у них независимые", сотня[99].history() == [], str(сотня[99].history()))
check("создание уложилось в секунду", затрачено < 1.0, f"{затрачено:.3f} с")

print("\n15. Строки списка агентов")

# Форматирование проверяем отдельно от приложения: это чистые функции, и их края —
# ноль обменов, круглые минуты, тысячи токенов — в живом прогоне не поймать.
check("секунды до минуты", ui.format_duration(12_400) == "12.4 с", ui.format_duration(12_400))
check("после минуты — минуты и секунды", ui.format_duration(382_000) == "6 м 22 с", ui.format_duration(382_000))
check("ровно минута уже не секунды", ui.format_duration(60_000) == "1 м 0 с", ui.format_duration(60_000))
check("токены до тысячи — как есть", ui.format_tokens(46) == "46", ui.format_tokens(46))
check("тысячи сокращаются", ui.format_tokens(92_417) == "92.4k", ui.format_tokens(92_417))

# Колонка занятия. Пока агент работает — начало его вопроса; освободился — имя профиля;
# оборвался — слово «ошибка». Многострочный вопрос сворачивается до первой строки, иначе
# сетка списка разъезжается.
check(
    "занятый агент показывает начало вопроса",
    ui.agent_occupation(screens_mod.BUSY, "как из рубашки\nсделать птицу?", "meta") == "как из рубашки",
    ui.agent_occupation(screens_mod.BUSY, "как из рубашки\nсделать птицу?", "meta"),
)
check(
    "свободный агент показывает профиль",
    ui.agent_occupation(screens_mod.DONE, "прежний вопрос", "meta") == "meta",
)
check(
    "оборвавшийся агент назван ошибкой словом",
    ui.agent_occupation(screens_mod.ERROR, "вопрос", "meta") == "ошибка · meta",
    ui.agent_occupation(screens_mod.ERROR, "вопрос", "meta"),
)
check(
    "занятый агент без вопроса не показывает пустоту",
    ui.agent_occupation(screens_mod.BUSY, "   \n ", "meta") == "meta",
)

# Окно прокрутки списка. Строка, на которой стоит пользователь, обязана быть видна: иначе
# закрашенный кружок «вы здесь» просто пропадает с экрана.
check("список короче предела показан целиком", ui.panel_slice(4, 0, limit=12) == (0, 4), str(ui.panel_slice(4, 0, limit=12)))
check("длинный список ведёт окно за выбором", ui.panel_slice(20, 10, limit=6) == (7, 13), str(ui.panel_slice(20, 10, limit=6)))
check("у начала списка окно не уходит в минус", ui.panel_slice(20, 0, limit=6) == (0, 6), str(ui.panel_slice(20, 0, limit=6)))
check("у конца списка окно упирается в конец", ui.panel_slice(20, 19, limit=6) == (14, 20), str(ui.panel_slice(20, 19, limit=6)))
for активный in range(20):
    начало, конец = ui.panel_slice(20, активный, limit=6)
    if not начало <= активный < конец:
        check("выбранная строка всегда внутри окна", False, f"строка {активный} вне ({начало}, {конец})")
        break
else:
    check("выбранная строка всегда внутри окна", True)

# Сам список. Ширину задаём вручную — терминала в проверках нет, а от ширины зависит и
# обрезка колонки занятия, и правый край с временем и токенами.
список = ui.agent_panel_fragments(
    [
        (screens_mod.IDLE, "main", "default", 0, 0, 0),
        (screens_mod.DONE, "analyst", "analyst", 12_400, 92_417, 3),
        (screens_mod.ERROR, "critic", "ошибка · critic", 900, 0, 1),
    ],
    active=1,
    width=78,
    on_click=lambda index: None,
)
текст_списка = "".join(фрагмент[1] for фрагмент in список)
строки_списка = текст_списка.split("\n")
check("над списком стоит строка подсказок", "↑/↓" in строки_списка[0] and "клик" in строки_списка[0], строки_списка[0])
check("строка на агента, главный первой", строки_списка[1].strip().startswith("○ main"), строки_списка[1])
check("закрашен тот, на кого смотрим", строки_списка[2].strip().startswith("● analyst"), строки_списка[2])
check("время и токены прижаты вправо", строки_списка[2].rstrip().endswith("↓ 92.4k"), repr(строки_списка[2]))
check("ещё не отвечавший показан прочерками, а не нулями", строки_списка[1].count("—") == 2, строки_списка[1])
check(
    "все строки списка одной ширины",
    all(len(строка) == 78 for строка in строки_списка[1:4]),
    str([len(строка) for строка in строки_списка[1:4]]),
)
check(
    "щелчок ловится всей строкой, включая отбивку",
    all(len(фрагмент) == 3 for фрагмент in список if фрагмент[1] and фрагмент[1] != "\n" and "↑/↓" not in фрагмент[1]),
)

# Переход по щелчку — по номеру строки, а не по имени: имена агентов повторяются от набора
# к набору, а номер строки в списке всегда один.
переходы = []
ui.agent_panel_fragments(
    [(screens_mod.IDLE, "main", "default", 0, 0, 0), (screens_mod.IDLE, "analyst", "analyst", 0, 0, 0)],
    active=0,
    width=78,
    on_click=переходы.append,
)[-3][2](
    MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset())
)
check("щелчок по строке зовёт переход с её номером", переходы == [1], str(переходы))

# Длинный список: показаны не все строки, и об остатке сказано вслух, а не молча обрезано.
длинный = ui.agent_panel_fragments(
    [(screens_mod.IDLE, f"агент{номер}", "профиль", 0, 0, 0) for номер in range(20)],
    active=0,
    width=78,
    on_click=lambda index: None,
)
текст_длинного = "".join(фрагмент[1] for фрагмент in длинный)
check("длинный список говорит, сколько строк осталось ниже", "ниже ещё 8" in текст_длинного, текст_длинного[-40:])

print("\n16. Каталог состояния, запись и чтение разговора")

# Разговор между запусками — не настройки и не кэш, у него свой каталог. Проверяем ровно то,
# из-за чего однажды затёрли рабочий ключ: путь обязан уводиться переменной окружения.
check("каталог состояния берётся из переменной окружения", memory.state_dir() == tmp / "state", str(memory.state_dir()))
сохранённая_переменная = os.environ.pop("MYHARNESS_STATE_DIR")
check(
    "без переменной каталог лежит под ~/.local/state",
    memory.state_dir() == Path.home() / ".local" / "state" / "myharness",
    str(memory.state_dir()),
)
os.environ["MYHARNESS_STATE_DIR"] = сохранённая_переменная

# Имя папки проекта — закодированный путь рабочего каталога: по нему человек глазами найдёт
# свой разговор, а два разных каталога не сольются в один.
check("путь каталога кодируется в имя папки", memory.project_key(Path("/a/b c")) == "-a-b%20c", memory.project_key(Path("/a/b c")))
# Кодирование обязано быть обратимым. Свались пробел, двоеточие и дефис в один «-» — четыре
# разных каталога поделили бы один разговор, и человек читал бы чужую переписку в своей папке.
ключи_путей = {memory.project_key(Path(путь)) for путь in ("/a/b c", "/a/b:c", "/a/b-c", "/a/b/c")}
check("разные пути дают разные имена папок", len(ключи_путей) == 4, str(sorted(ключи_путей)))
# Читаемость имени — половина смысла: свой разговор человек находит глазами. Буквы любого
# алфавита остаются буквами, кодируется только то, что буквой не является.
check("кириллица в имени папки остаётся читаемой", memory.project_key(Path("/дом/проект")) == "-дом-проект", memory.project_key(Path("/дом/проект")))
check(
    "сессии лежат под каталогом состояния",
    memory.sessions_dir(Path("/a/b")) == tmp / "state" / "projects" / "-a-b",
    str(memory.sessions_dir(Path("/a/b"))),
)

# Запуск из «./test» и из «test» — один и тот же каталог, и разговор обязан быть один.
# Отсюда resolve() внутри project_key.
прежний_каталог = Path.cwd()
каталог_проекта = tmp / "проект"
каталог_проекта.mkdir()
os.chdir(каталог_проекта)
check(
    "относительный и абсолютный путь дают один ключ",
    memory.project_key(Path(".")) == memory.project_key(каталог_проекта),
    f"{memory.project_key(Path('.'))} != {memory.project_key(каталог_проекта)}",
)
os.chdir(прежний_каталог)

# Запись строки разговора. Хранилище — вспомогательный механизм: обмен уже удался и уже
# оплачен, ронять его отказом диска нельзя, поэтому ошибка возвращается текстом.
путь_сессии = memory.sessions_dir(каталог_проекта) / "сессия.jsonl"
хранилище = memory.SessionStore(путь_сессии)
check("первая запись проходит без ошибки", хранилище.append("user", "вопрос") is None)
check(
    "вторая запись проходит без ошибки",
    хранилище.append("assistant", "ответ", {"role": "подделка", "model": "flash"}) is None,
)
строки_сессии = путь_сессии.read_text(encoding="utf-8").strip().splitlines()
check("две записи — две строки", len(строки_сессии) == 2, str(len(строки_сессии)))
запись = json.loads(строки_сессии[1])
check("метка времени и содержимое на месте", bool(запись["ts"]) and запись["content"] == "ответ", str(запись))
check("поля meta попали в строку", запись.get("model") == "flash", str(запись))
# Роль задаёт чередование сообщений в запросе к API — подменить её полем meta нельзя,
# иначе восстановление соберёт разговор задом наперёд.
check("meta не затирает роль", запись["role"] == "assistant", str(запись["role"]))
check("права файла разговора 600", oct(путь_сессии.stat().st_mode & 0o777) == "0o600", oct(путь_сессии.stat().st_mode & 0o777))
# Каталог закрываем целиком: его имя — закодированный путь рабочей папки, то есть само по себе
# рассказывает соседям по машине, над чем человек работает.
check(
    "каталоги состояния закрыты правами 700",
    all(
        oct(каталог.stat().st_mode & 0o777) == "0o700"
        for каталог in (путь_сессии.parent, путь_сессии.parent.parent, tmp / "state")
    ),
    str([oct(каталог.stat().st_mode & 0o777) for каталог in (путь_сессии.parent, путь_сессии.parent.parent, tmp / "state")]),
)
# Недоступный путь задаём через родителя-файл: ENOTDIR приходит независимо от прав, тогда как
# «несуществующий корень» под root был бы просто создан — проверка прошла бы вхолостую.
файл_вместо_каталога = tmp / "не-каталог"
файл_вместо_каталога.write_text("я файл", encoding="utf-8")
ошибка_записи = memory.SessionStore(файл_вместо_каталога / "сессия.jsonl").append("user", "вопрос")
check("недоступный путь → текст ошибки, без исключения", isinstance(ошибка_записи, str) and bool(ошибка_записи), str(ошибка_записи))
# Обещание «не бросает» дано безусловно, а в meta приезжает что угодно от вызывающего кода.
ошибка_meta = хранилище.append("assistant", "ответ", {"объект": object()})
check("несериализуемое значение в meta → текст ошибки, без исключения", isinstance(ошибка_meta, str) and bool(ошибка_meta), str(ошибка_meta))


def строка_разговора(role, content, **поля):
    return json.dumps({"ts": "2026-09-08T12:00:00+00:00", "role": role, "content": content, **поля}, ensure_ascii=False)


каталог_чтения = tmp / "state" / "чтение"
каталог_чтения.mkdir(parents=True)
целый_файл = каталог_чтения / "целый.jsonl"
целый_файл.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            строка_разговора("assistant", "о1", system_fp="abc123"),
            строка_разговора("user", "в2"),
            строка_разговора("assistant", "о2", system_fp="abc123"),
            строка_разговора("user", "в3"),
            строка_разговора("assistant", "о3", system_fp="abc123"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
целое = memory.read_session(целый_файл, window=0, system_fp="abc123")
check("целый файл восстановлен целиком", целое.pairs == [("в1", "о1"), ("в2", "о2"), ("в3", "о3")], str(целое.pairs))
check("сохранённых пар посчитано столько же", целое.saved_pairs == 3, str(целое.saved_pairs))
check("у целого файла предупреждений нет", целое.warnings == [], str(целое.warnings))
check("взята отметка времени последней записи", целое.last_ts == "2026-09-08T12:00:00+00:00", целое.last_ts)
check("совпавший отпечаток инструкции не тревожит", целое.fingerprint_changed is False)

# Окно режет то, что уйдёт в модель, но человеку называется полное число сохранённых пар:
# молча подставленный кусок прошлого разговора отлаживают как «модель отвечает не на то».
окно_в_одну_пару = memory.read_session(целый_файл, window=1, system_fp="abc123")
check("окно оставляет последние пары", окно_в_одну_пару.pairs == [("в3", "о3")], str(окно_в_одну_пару.pairs))
check("при окне названо полное число сохранённых пар", окно_в_одну_пару.saved_pairs == 3, str(окно_в_одну_пару.saved_pairs))

# Инструкцию профиля правят между запусками, и прежние ответы получены под другой. Молчать
# об этом нельзя — расхождение спишут на модель.
чужой_отпечаток = memory.read_session(целый_файл, window=0, system_fp="ffffff")
check("изменившийся отпечаток инструкции замечен", чужой_отпечаток.fingerprint_changed is True)

# Дальше — края, которых в живом прогоне не поймать. Общее правило: файл испорчен частично,
# а восстановление всё равно доходит до конца и говорит, что потеряло.
битый_файл = каталог_чтения / "битый.jsonl"
битый_файл.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            "{не json",
            строка_разговора("assistant", "о1"),
            строка_разговора("user", "в2"),
            строка_разговора("assistant", "о2"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
битое = memory.read_session(битый_файл, window=0, system_fp="abc123")
check("испорченная строка пропущена, пары собраны", битое.pairs == [("в1", "о1"), ("в2", "о2")], str(битое.pairs))
# Номер строки сверяем целиком: «есть двойка в тексте» прошло бы и на строке 12, и на 20.
check(
    "об испорченной строке названо её место",
    any(предупреждение.startswith("строка 2 ") for предупреждение in битое.warnings),
    str(битое.warnings),
)

# Самый опасный случай и причина, по которой разбор не имеет права обрывать чтение: испорчена
# строка ОТВЕТА. Вопрос повисает, дальше идёт следующий вопрос — и обрыв сбора унёс бы весь
# остаток разговора, ради сохранности которого и выбрано дописывание.
битый_ответ = каталог_чтения / "битый-ответ.jsonl"
битый_ответ.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            строка_разговора("assistant", "о1"),
            строка_разговора("user", "в2"),
            "{оборвано",
            строка_разговора("user", "в3"),
            строка_разговора("assistant", "о3"),
            строка_разговора("user", "в4"),
            строка_разговора("assistant", "о4"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
оборванный_ответ = memory.read_session(битый_ответ, window=0, system_fp="abc123")
check(
    "битая строка на месте ответа не уносит остаток разговора",
    оборванный_ответ.pairs == [("в1", "о1"), ("в3", "о3"), ("в4", "о4")],
    str(оборванный_ответ.pairs),
)
check("из четырёх пар выжили три", оборванный_ответ.saved_pairs == 3, str(оборванный_ответ.saved_pairs))

# Нарушенный порядок пересинхронизирует чтение, а не обрывает его: повисший вопрос назван
# по номеру строки и отброшен, следующая пара собрана как ни в чём не бывало.
файл_двух_вопросов = каталог_чтения / "два-вопроса.jsonl"
файл_двух_вопросов.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            строка_разговора("user", "в2"),
            строка_разговора("assistant", "о2"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
два_вопроса = memory.read_session(файл_двух_вопросов, window=0, system_fp="abc123")
check("вопрос подряд за вопросом не ломает чтение", два_вопроса.pairs == [("в2", "о2")], str(два_вопроса.pairs))
check(
    "об отброшенном повисшем вопросе названо его место",
    any(предупреждение.startswith("строка 1:") for предупреждение in два_вопроса.warnings),
    str(два_вопроса.warnings),
)

# Роль не угадываем: API требует строгого чередования user/assistant, и догадка упала бы
# позже, на стороне сервера, без видимой причины. Отпечаток с отброшенной строки не берём —
# иначе судьбу «инструкция изменилась» решает мусор.
файл_с_чужой_ролью = каталог_чтения / "роль.jsonl"
файл_с_чужой_ролью.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            строка_разговора("assistant", "о1", system_fp="abc123"),
            строка_разговора("system", "инструкция", system_fp="ffffff"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
чужая_роль = memory.read_session(файл_с_чужой_ролью, window=0, system_fp="abc123")
check("запись с неизвестной ролью отброшена", чужая_роль.pairs == [("в1", "о1")], str(чужая_роль.pairs))
check("о неизвестной роли предупреждено", any("роль" in предупреждение for предупреждение in чужая_роль.warnings), str(чужая_роль.warnings))
check("отпечаток с отброшенной строки в расчёт не идёт", чужая_роль.fingerprint_changed is False)

# Оборванная сериализация даёт запись с нетекстовым содержимым. Назвать её «неизвестной
# ролью» — послать человека искать роль, которой нет.
файл_без_текста = каталог_чтения / "содержимое.jsonl"
файл_без_текста.write_text(
    "\n".join(
        [
            строка_разговора("user", None),
            строка_разговора("user", "в2"),
            строка_разговора("assistant", "о2"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
без_текста = memory.read_session(файл_без_текста, window=0, system_fp="abc123")
check("запись без текста отброшена, разговор прочитан", без_текста.pairs == [("в2", "о2")], str(без_текста.pairs))
check(
    "предупреждение говорит о содержимом, а не о роли",
    any("содержим" in предупреждение and "роль" not in предупреждение for предупреждение in без_текста.warnings),
    str(без_текста.warnings),
)

# Вопрос без ответа в хвосте: память обязана идти парами, иначе сервер откажет в запросе.
# Предупреждение ровно одно — двойное сообщение об одной беде читается как две беды.
файл_с_хвостом = каталог_чтения / "хвост.jsonl"
файл_с_хвостом.write_text(
    "\n".join(
        [
            строка_разговора("user", "в1"),
            строка_разговора("assistant", "о1"),
            строка_разговора("user", "в2"),
        ]
    )
    + "\n",
    encoding="utf-8",
)
хвост = memory.read_session(файл_с_хвостом, window=0, system_fp="abc123")
check("вопрос без ответа отброшен", хвост.pairs == [("в1", "о1")], str(хвост.pairs))
check("об отброшенном хвосте предупреждено ровно раз", len(хвост.warnings) == 1, str(хвост.warnings))

# Обрыв процесса посреди многобайтового знака — самый вероятный вид порчи файла: UTF-8 в
# кириллице двухбайтовый, и половина знака остаётся на диске.
файл_с_битыми_байтами = каталог_чтения / "байты.jsonl"
файл_с_битыми_байтами.write_bytes(
    (строка_разговора("user", "в1") + "\n" + строка_разговора("assistant", "о1") + "\n").encode("utf-8")
    + b'{"ts": "2026-09-08T12:00:00+00:00", "role": "user", "content": "\xd0"}\n'
)
битые_байты = memory.read_session(файл_с_битыми_байтами, window=0, system_fp="abc123")
check("файл с не-UTF-8 байтами не роняет чтение", битые_байты.pairs == [("в1", "о1")], str(битые_байты.pairs))
check("о порче знаков сказано вслух", bool(битые_байты.warnings), str(битые_байты.warnings))

# Первый запуск в новой папке — самый частый случай: файла нет, и это не беда, а норма.
пустое = memory.read_session(каталог_чтения / "нет-такого.jsonl", window=0, system_fp="abc123")
check(
    "отсутствующего файла хватает для пустого восстановления без предупреждений",
    пустое.pairs == [] and пустое.saved_pairs == 0 and пустое.last_ts == "" and пустое.warnings == [] and пустое.fingerprint_changed is False,
    str(пустое),
)

# Имя файла сессии несёт и время, и профиль: время впереди даёт порядок без открытия файлов,
# профиль в конце — отбор сессий пары «каталог, профиль».
# Сессия — файл со случайным именем в каталоге профиля. Имя намеренно ничего не сообщает:
# осмысленное имя тянуло за собой время в имени, счётчик, сдвиг момента вперёд и сверку формы
# имени — пять механизмов ради одного решения вложить в имя смысл.
каталог_выбора = tmp / "выбор"
каталог_выбора.mkdir()
check(
    "профиль — уровень каталога внутри папки рабочего каталога",
    memory.profile_dir(каталог_выбора, "default") == memory.sessions_dir(каталог_выбора) / "default",
    str(memory.profile_dir(каталог_выбора, "default")),
)
check("отсутствующий каталог профиля даёт None", memory.latest_session(каталог_выбора, "default") is None)
папка_профиля = memory.profile_dir(каталог_выбора, "default")
папка_профиля.mkdir(parents=True)
check("пустой каталог профиля даёт None", memory.latest_session(каталог_выбора, "default") is None)

# Порядок — по времени ИЗМЕНЕНИЯ файла: «продолжить последний разговор» это про тот, с которым
# работали последним, а не про тот, который раньше начат. Вернулись вчера к старой сессии —
# продолжать надо её. Время задаём явно: ожидание сделало бы проверку медленной и шаткой.
ранняя_сессия = папка_профиля / "aaaa1111.jsonl"
поздняя_сессия = папка_профиля / "bbbb2222.jsonl"
ранняя_сессия.write_text("", encoding="utf-8")
поздняя_сессия.write_text("", encoding="utf-8")
os.utime(ранняя_сессия, (1_700_000_000, 1_700_000_000))
os.utime(поздняя_сессия, (1_700_000_100, 1_700_000_100))
check("берётся самая свежая по времени изменения", memory.latest_session(каталог_выбора, "default") == поздняя_сессия, str(memory.latest_session(каталог_выбора, "default")))
# И то же самое наоборот: свежим делаем файл, который по имени идёт первым. Имя на выбор не
# влияет вовсе — иначе вернулась бы прежняя беда «поднялся более старый разговор».
os.utime(ранняя_сессия, (1_700_000_200, 1_700_000_200))
check("выбор идёт от времени, а не от имени", memory.latest_session(каталог_выбора, "default") == ранняя_сессия, str(memory.latest_session(каталог_выбора, "default")))

# Посторонние обитатели каталога выбор не уводят: чужое расширение — не разговор, вложенный
# каталог — тем более, даже если он свежее всех.
заметка = папка_профиля / "заметка.txt"
заметка.write_text("", encoding="utf-8")
вложенный_каталог = папка_профиля / "вложенный.jsonl"
вложенный_каталог.mkdir()
for посторонний in (заметка, вложенный_каталог):
    os.utime(посторонний, (1_800_000_000, 1_800_000_000))
check("посторонний файл и вложенный каталог выбор не уводят", memory.latest_session(каталог_выбора, "default") == ранняя_сессия, str(memory.latest_session(каталог_выбора, "default")))

# Ничья по времени изменения разрешается по имени. Наносекундный st_mtime совпадает редко,
# но после копирования каталога состояния или rsync — запросто, и тогда «продолжить
# последний разговор» решалось бы жребием: порядком, в котором каталог отдала файловая
# система. В фактах ничья уже разрешена явно, в сессиях правило обязано быть таким же.
папка_ничьей = memory.profile_dir(каталог_выбора, "ничья")
папка_ничьей.mkdir(parents=True)
for имя_ровесника in ("aaa1", "bbb2", "ccc3", "ddd4", "eee5", "fff6", "ggg7", "hhh8"):
    ровесник = папка_ничьей / f"{имя_ровесника}.jsonl"
    ровесник.write_text("", encoding="utf-8")
    os.utime(ровесник, (1_700_000_000, 1_700_000_000))
# Порядок обхода каталога подменяем нарочно: положись проверка на настоящий порядок файловой
# системы — она проходила бы или падала по жребию, ровно как и сам разбор ничьей.
обычный_обход = Path.iterdir


def обход_по_возрастанию(self):
    return iter(sorted(обычный_обход(self)))


def обход_по_убыванию(self):
    return iter(sorted(обычный_обход(self), reverse=True))


ничья_при_разном_обходе = []
for подменённый_обход in (обход_по_возрастанию, обход_по_убыванию):
    Path.iterdir = подменённый_обход
    ничья_при_разном_обходе.append(memory.latest_session(каталог_выбора, "ничья"))
Path.iterdir = обычный_обход
check(
    "ничья по времени разрешается по имени, а не порядком файловой системы",
    {путь.name for путь in ничья_при_разном_обходе} == {"hhh8.jsonl"},
    str([путь.name for путь in ничья_при_разном_обходе]),
)

# Кодирование имени профиля закрепляем прямо, а не через пары «похожих» имён: пары остаются
# разными и при сломанном кодировании, а вот профиль «..» (поле profile в config.json
# правится руками) увёл бы разговор ВОН из каталога проекта.
check("знак пути в имени профиля кодируется", memory.profile_dir(каталог_выбора, "a/b").name == "a-b", memory.profile_dir(каталог_выбора, "a/b").name)
check("точка в имени профиля кодируется", memory.profile_dir(каталог_выбора, ".").name == "%2E", memory.profile_dir(каталог_выбора, ".").name)
check("две точки в имени профиля кодируются", memory.profile_dir(каталог_выбора, "..").name == "%2E%2E", memory.profile_dir(каталог_выбора, "..").name)
check("пробел в имени профиля кодируется", memory.profile_dir(каталог_выбора, "моя схема").name == "моя%20схема", memory.profile_dir(каталог_выбора, "моя схема").name)
check(
    "каталог профиля не выходит за пределы каталога рабочей папки",
    all(
        memory.profile_dir(каталог_выбора, опасный).resolve().parent == memory.sessions_dir(каталог_выбора).resolve()
        for опасный in ("..", ".", "../..", "/", "a/b")
    ),
    str([str(memory.profile_dir(каталог_выбора, опасный).resolve()) for опасный in ("..", ".", "../..", "/", "a/b")]),
)
# Пустое имя профиля: Path("/a") / "" возвращает сам «/a», и файлы сессий легли бы в каталог
# проекта вперемешку с каталогами профилей.
check("пустое имя профиля не роняет сессии в каталог проекта", memory.profile_dir(каталог_выбора, "") == memory.sessions_dir(каталог_выбора) / "-", str(memory.profile_dir(каталог_выбора, "")))

# Два профиля — два каталога. Сведи их кодирование к одному имени, и один читал бы переписку
# другого: ловушки «default»/«my-default» и «моя схема»/«моя-схема» — ровно про это.
for профиль in ("analyst", "my-default", "моя схема", "моя-схема"):
    memory.profile_dir(каталог_выбора, профиль).mkdir(parents=True)
    (memory.profile_dir(каталог_выбора, профиль) / "cccc3333.jsonl").write_text("", encoding="utf-8")
check("сессия другого профиля не видна", memory.latest_session(каталог_выбора, "default") == ранняя_сессия, str(memory.latest_session(каталог_выбора, "default")))
check(
    "профили default и my-default лежат в разных каталогах",
    memory.profile_dir(каталог_выбора, "default") != memory.profile_dir(каталог_выбора, "my-default"),
    str(memory.profile_dir(каталог_выбора, "my-default")),
)
check(
    "профили «моя схема» и «моя-схема» лежат в разных каталогах",
    memory.profile_dir(каталог_выбора, "моя схема") != memory.profile_dir(каталог_выбора, "моя-схема"),
    str(memory.profile_dir(каталог_выбора, "моя схема")),
)
check(
    "каждый профиль читает сессию из своего каталога",
    all(memory.latest_session(каталог_выбора, профиль).parent == memory.profile_dir(каталог_выбора, профиль) for профиль in ("analyst", "my-default", "моя схема", "моя-схема")),
    str([str(memory.latest_session(каталог_выбора, профиль)) for профиль in ("analyst", "my-default", "моя схема", "моя-схема")]),
)

# Новая сессия: случайное имя, уникальность даром — ни счётчика, ни часов, ни разбора имени.
каталог_новой = tmp / "новая"
каталог_новой.mkdir()
тройка_сессий = [memory.new_session(каталог_новой, "default") for _ in range(3)]
check("три новые сессии подряд — три разных пути", len(set(тройка_сессий)) == 3, str([путь.name for путь in тройка_сессий]))
check("новая сессия лежит в каталоге профиля", all(путь.parent == memory.profile_dir(каталог_новой, "default") for путь in тройка_сессий), str(тройка_сессий[0].parent))
check("имя новой сессии оканчивается на .jsonl", all(путь.suffix == ".jsonl" for путь in тройка_сессий), str([путь.name for путь in тройка_сессий]))
# Пустых файлов не заводим: пустой файл разговора неотличим от начатого и брошенного.
check("новые сессии файлов на диске не заводят", not any(путь.exists() for путь in тройка_сессий))
memory.SessionStore(тройка_сессий[0]).append("user", "в1")
check("записанная сессия находится как последняя", memory.latest_session(каталог_новой, "default") == тройка_сессий[0], str(memory.latest_session(каталог_новой, "default")))

# Отпечаток инструкции: его дело — заметить, что инструкцию правили между запусками.
check("одинаковый текст даёт одинаковый отпечаток", memory.fingerprint("ты помощник") == memory.fingerprint("ты помощник"))
check("изменённый текст даёт другой отпечаток", memory.fingerprint("ты помощник") != memory.fingerprint("ты помощник."))
check("отпечаток короткий", len(memory.fingerprint("ты помощник")) == 6, memory.fingerprint("ты помощник"))
# Пустая строка отпечатка означала бы «запись старого формата, отпечатка нет». Отсутствие
# инструкции — другой случай, и путать их нельзя.
check(
    "отсутствие инструкции тоже имеет отпечаток",
    memory.fingerprint(None) == memory.fingerprint("") and memory.fingerprint(None) != "",
    memory.fingerprint(None),
)

# Глобальные факты — второй слой памяти: он про человека, а не про папку. Устройство — файл на
# факт: общего изменяемого состояния нет, значит и гонки между двумя копиями инструмента нет.
check("каталог фактов лежит в каталоге состояния", memory.facts_dir() == tmp / "state" / "memory", str(memory.facts_dir()))
check("на пустом месте фактов нет", memory.load_facts() == ([], []), str(memory.load_facts()))
добавлен, сообщение_факта = memory.add_fact("зовут Александр")
check("факт добавлен", добавлен is True and сообщение_факта == "зовут Александр", str((добавлен, сообщение_факта)))
файлы_фактов = sorted(memory.facts_dir().glob("*.md"))
check("добавление завело ровно один файл", len(файлы_фактов) == 1, str([путь.name for путь in файлы_фактов]))
check("факт прочитан обратно", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
# Имя файла человек читает глазами: слаг из первых слов, кириллица остаётся кириллицей.
check("имя файла факта читается глазами", файлы_фактов[0].name.startswith("зовут-александр-"), файлы_фактов[0].name)
содержимое_факта = файлы_фактов[0].read_text(encoding="utf-8")
# Происхождение факта: через месяц при разборе памяти это единственный способ понять, откуда
# взялась строка — сам человек её сказал или её вывел архивариус.
check("в заголовке записан источник", "источник: человек" in содержимое_факта, содержимое_факта)
check("в заголовке записано время добавления в UTC", "добавлен: " in содержимое_факта and "Z\n" in содержимое_факта, содержимое_факта)
check("тело файла — сам факт", содержимое_факта.rstrip().endswith("зовут Александр"), содержимое_факта)
# Каталог закрыт: сами имена файлов рассказывают о человеке. Файлы — как разговор, 600.
check("права каталога фактов 700", oct(memory.facts_dir().stat().st_mode & 0o777) == "0o700", oct(memory.facts_dir().stat().st_mode & 0o777))
check("права файла факта 600", oct(файлы_фактов[0].stat().st_mode & 0o777) == "0o600", oct(файлы_фактов[0].stat().st_mode & 0o777))
memory.add_fact("любимый цвет — синий", source="архивариус")
файл_архивариуса = next(путь for путь in memory.facts_dir().glob("*.md") if "цвет" in путь.name)
check("источник архивариуса записан в заголовок", "источник: архивариус" in файл_архивариуса.read_text(encoding="utf-8"), файл_архивариуса.read_text(encoding="utf-8"))

# Повтор ищется сравнением текста со всеми загруженными фактами, а не по имени файла: иначе
# правка факта руками ломала бы отбор повторов.
повтор, сообщение_повтора = memory.add_fact("  Зовут алЕксандр  ")
check("повтор в другом регистре и с пробелами отброшен", повтор is False and "уже" in сообщение_повтора, str((повтор, сообщение_повтора)))
пустой_факт, сообщение_пустого = memory.add_fact("   ")
check("пробельный факт отброшен", пустой_факт is False and bool(сообщение_пустого), str((пустой_факт, сообщение_пустого)))
# Потолок длины. FACTS_MAX считает штуки и от факта в двенадцать тысяч знаков не спасает, а
# факт уезжает в системную инструкцию КАЖДОГО запроса — за деньги пользователя.
длинный_факт, сообщение_длинного = memory.add_fact("я" * 201)
check(
    "факт длиннее 200 знаков отброшен с указанием длины",
    длинный_факт is False and "200" in сообщение_длинного and "201" in сообщение_длинного,
    str((длинный_факт, сообщение_длинного)),
)
check("ровно 200 знаков ещё принимаются", memory.add_fact("я" * 200)[0] is True)
# Предел имени файла в ext4 — 255 БАЙТ, а знак кириллицы занимает два: слаг, обрезанный по
# знакам, дал бы имя в 410 байт, и законный факт не записался бы на сервере проекта вовсе.
# На макбуке (APFS считает знаки) беда не проявляется — потому и проверяем байты.
файл_длинного_факта = next(путь for путь in memory.facts_dir().glob("я*.md"))
check("имя файла факта укладывается в предел файловой системы", len(файл_длинного_факта.name.encode("utf-8")) <= 255, str(len(файл_длинного_факта.name.encode("utf-8"))))
check("слаг обрезан по байтам, а не по знакам", len(файл_длинного_факта.name.encode("utf-8")) <= 80 + len("-000000.md"), файл_длинного_факта.name)
# Резать по байтам, не глядя на границы знаков, — значит разрубить букву пополам: в имени
# останется битый байт, который при чтении каталога станет «\ufffd».
check("обрезка прошла по границе знака", "\ufffd" not in файл_длинного_факта.name, файл_длинного_факта.name)
check("длинный факт прочитан обратно целиком", ("я" * 200) in memory.load_facts()[0], str([len(факт) for факт in memory.load_facts()[0]]))
# Предел, пришедшийся на СЕРЕДИНУ многобайтового знака: 79 однобайтовых знаков плюс кириллица —
# 80-й байт оказывается первой половиной буквы «я». Резать там, не глядя на границу, значит
# оставить в имени файла битый байт.
memory.add_fact("a" * 79 + "яяяяя" + " хвост")
файл_на_границе = next(путь for путь in memory.facts_dir().glob("aaa*.md"))
check("обрезка на середине знака не оставляет битого байта", "\ufffd" not in файл_на_границе.name, файл_на_границе.name)
check("обрезка остановилась на границе знака", файл_на_границе.name.startswith("a" * 79 + "-"), файл_на_границе.name)
файл_на_границе.unlink()  # дальше считаем факты поштучно, лишний тут только мешает
check("после отказов в памяти три факта", len(memory.load_facts()[0]) == 3, str(memory.load_facts()[0]))
# Факт — одна строка. Перевод строки внутри схлопывается в пробел, иначе тело файла стало бы
# двумя строками, и вторая при чтении оказалась бы отдельным непонятным фактом.
memory.add_fact("любит\nсиний")
check("перевод строки внутри факта схлопнут", "любит синий" in memory.load_facts()[0], str(memory.load_facts()[0]))

# Порядок обязан быть одинаков в двух запущенных копиях: по времени добавления, при совпадении
# по имени файла. Иначе `/forget 2` в одном окне убирает не то, что видно во втором.
первое_чтение = memory.load_facts()[0]
второе_чтение = memory.load_facts()[0]
check("порядок фактов одинаков при двух чтениях", первое_чтение == второе_чтение, f"{первое_чтение} / {второе_чтение}")

# Испорченный файл факта не мешает прочитать остальные — и об этом говорят вслух.
битый_факт = memory.facts_dir() / "битый-000000.md"
битый_факт.write_text("---\nдобавлен: 2026-09-08T10:00:00Z\n---\n\n", encoding="utf-8")
факты_с_битым, предупреждения_фактов = memory.load_facts()
check("испорченный файл факта не мешает прочитать остальные", len(факты_с_битым) == 4, str(факты_с_битым))
check("об испорченном файле факта предупреждено", bool(предупреждения_фактов), str(предупреждения_фактов))
битый_факт.unlink()
# Второй вид порчи: заголовок открыт и не закрыт. Не отбракуй такой файл — факт собрался бы
# из текста собственного заголовка, и в системную инструкцию уехало бы «добавлен: …».
незакрытый_факт = memory.facts_dir() / "незакрытый-000000.md"
незакрытый_факт.write_text("---\nдобавлен: 2026-09-08T10:00:00Z\nисточник: человек\n\nзовут Пётр\n", encoding="utf-8")
факты_с_незакрытым, предупреждения_незакрытого = memory.load_facts()
check("файл с незакрытым заголовком отброшен", not any("добавлен" in факт or "Пётр" in факт for факт in факты_с_незакрытым), str(факты_с_незакрытым))
check("о незакрытом заголовке предупреждено", bool(предупреждения_незакрытого), str(предупреждения_незакрытого))
незакрытый_факт.unlink()

# Запись идёт черновиком с подменой имени, а не поверх файла: читатель никогда не увидит
# недописанный факт. Прямую запись ловим по номеру узла — подмена его меняет, запись поверх
# сохраняет. Пустое тело делает файл испорченным, поэтому повтор не распознаётся и тот же
# факт пишется в то же имя.
путь_подмены = next(путь for путь in memory.facts_dir().glob("*.md") if путь.name.startswith("зовут-александр-"))
путь_подмены.write_text("", encoding="utf-8")
узел_до_записи = путь_подмены.stat().st_ino
memory.add_fact("зовут Александр")
check("запись факта идёт через черновик и подмену", путь_подмены.stat().st_ino != узел_до_записи, f"{узел_до_записи} → {путь_подмены.stat().st_ino}")
check("черновиков в каталоге не остаётся", list(memory.facts_dir().glob("*.tmp")) == [] and list(memory.facts_dir().glob(".*")) == [], str(list(memory.facts_dir().iterdir())))

# Удаление. Номер — тот, что показан человеку в /memory, то есть с единицы.
for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()
for текст in ("альфа", "бета", "гамма"):
    memory.add_fact(текст)
check("три факта на месте и по порядку", memory.load_facts()[0] == ["альфа", "бета", "гамма"], str(memory.load_facts()[0]))
убран, убранный_текст = memory.remove_fact(2)
check("удаление идёт по номеру с единицы, как показано человеку", убран is True and убранный_текст == "бета", str((убран, убранный_текст)))
check("удаление убрало файл и сдвинуло нумерацию", memory.load_facts()[0] == ["альфа", "гамма"], str(memory.load_facts()[0]))
check("файлов в каталоге столько же, сколько фактов", len(list(memory.facts_dir().glob("*.md"))) == 2, str(list(memory.facts_dir().glob("*.md"))))
for неверный_номер in (0, -1, 99):
    отказ, сообщение_номера = memory.remove_fact(неверный_номер)
    check(f"номер {неверный_номер} даёт сообщение, а не исключение", отказ is False and bool(сообщение_номера), str((отказ, сообщение_номера)))
check("промах по номеру ничего не удалил", memory.load_facts()[0] == ["альфа", "гамма"], str(memory.load_facts()[0]))
memory.remove_fact(1)
memory.remove_fact(1)
отказ_на_пустой, сообщение_пустой_памяти = memory.remove_fact(1)
check("удаление из пустой памяти даёт сообщение", отказ_на_пустой is False and bool(сообщение_пустой_памяти), str((отказ_на_пустой, сообщение_пустой_памяти)))

# Файл, исчезнувший между перечислением каталога и чтением, — не беда, а норма устройства
# «файл на факт»: его только что убрала вторая копия инструмента. Предупреждать тут не о
# чем, иначе человек получал бы тревогу за штатную работу. Исчезновение подстраиваем
# подменой чтения: дождаться настоящей гонки в проверке невозможно.
for текст in ("альфа", "бета"):
    memory.add_fact(текст)
исчезающий_факт = next(путь for путь in memory.facts_dir().glob("альфа*.md"))
обычное_чтение = Path.read_text


def чтение_с_исчезновением(self, *args, **kwargs):
    if self == исчезающий_факт:
        self.unlink()
    return обычное_чтение(self, *args, **kwargs)


Path.read_text = чтение_с_исчезновением
факты_после_исчезновения, предупреждения_исчезновения = memory.load_facts()
Path.read_text = обычное_чтение
check("исчезнувший при чтении файл пропущен", факты_после_исчезновения == ["бета"], str(факты_после_исчезновения))
check("об исчезнувшем файле не предупреждают", предупреждения_исчезновения == [], str(предупреждения_исчезновения))
for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()

# Потолок числа фактов. Молча вытеснить старое нельзя — человек не узнает, что потерял.
for номер in range(memory.FACTS_MAX):
    memory.add_fact(f"факт номер {номер}")
переполнение, сообщение_переполнения = memory.add_fact("лишний факт")
check(
    "факт сверх потолка отброшен с указанием на /forget",
    переполнение is False and "/forget" in сообщение_переполнения,
    str((переполнение, сообщение_переполнения)),
)
факты_после_переполнения = memory.load_facts()[0]
check(
    "при переполнении старое не вытеснено",
    len(факты_после_переполнения) == memory.FACTS_MAX and "лишний факт" not in факты_после_переполнения,
    str(len(факты_после_переполнения)),
)

# Недоступный каталог отвечает текстом, а не исключением: память — вспомогательный механизм.
# Недоступность задаём родителем-файлом: ENOTDIR приходит независимо от прав, тогда как
# несуществующий корень под root был бы просто создан.
os.environ["MYHARNESS_STATE_DIR"] = str(файл_вместо_каталога / "state")
не_добавлен, сообщение_сбоя = memory.add_fact("зовут Александр")
check("недоступный каталог → текст ошибки, без исключения", не_добавлен is False and bool(сообщение_сбоя), str((не_добавлен, сообщение_сбоя)))
check("нечитаемый каталог фактов даёт пустой список, а не исключение", memory.load_facts() == ([], []), str(memory.load_facts()))
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")

# Два запущенных инструмента (второй запуск в другой папке, архивариус фоном) пишут разные
# файлы и не встречаются. Ради этого и выбран файл на факт вместо общего файла: там две копии
# затирали память друг друга целиком — из тридцати фактов уцелевало пятнадцать.
каталог_гонки = tmp / "гонка"
среда_гонки = {**os.environ, "MYHARNESS_STATE_DIR": str(каталог_гонки)}
код_гонки = "import sys\nfrom myharness import memory\nfor номер in range(15):\n    memory.add_fact(f'{sys.argv[1]} факт {номер}')\n"
процессы_гонки = [subprocess.Popen([sys.executable, "-c", код_гонки, метка], env=среда_гонки) for метка in ("первый", "второй")]
for процесс in процессы_гонки:
    try:
        процесс.wait(timeout=60)
    except subprocess.TimeoutExpired:
        процесс.kill()
os.environ["MYHARNESS_STATE_DIR"] = str(каталог_гонки)
факты_гонки = memory.load_facts()[0]
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")
check("две одновременные записи не теряют фактов", len(факты_гонки) == 30, str(len(факты_гонки)))

# Блок фактов для системного сообщения. Пустой список не меняет инструкцию вовсе: заголовок
# без списка сообщил бы модели, что о человеке известно, что о нём ничего не известно.
check("пустой список даёт пустую строку", memory.facts_block([]) == "", memory.facts_block([]))
check("пробельные факты не создают блок", memory.facts_block(["", "   "]) == "", memory.facts_block(["", "   "]))
блок_фактов = memory.facts_block(["зовут Александр", "любимый цвет — синий"])
check(
    "блок фактов — заголовок и перечень",
    блок_фактов.splitlines() == ["Что известно о пользователе:", "- зовут Александр", "- любимый цвет — синий"],
    блок_фактов,
)

print("\n17. Архивариус: факты из разговора")

# Каталог фактов после прошлого раздела заполнен под потолок — начинаем с чистого места,
# иначе первое же добавление упрётся в FACTS_MAX и проверка будет мерить не то.
for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()

# Слово «json» и пример структуры обязаны стоять в самой инструкции: без них DeepSeek
# отклоняет запрос с response_format=json_object отказом на стороне сервера — не пустым
# ответом, а кодом ошибки, в котором пришлось бы разбираться отдельно.
# Архивариуса зовёт `cli`, значит обратный импорт замкнул бы круг. Разрывается он тем же
# приёмом, что в `output`: состояние приходит аргументом, а имя типа берётся под
# `TYPE_CHECKING` — на выполнении такого импорта не происходит, и в счёт он не идёт.
def импорты_на_выполнении(имя_файла):
    """Что модуль импортирует НА ВЫПОЛНЕНИИ. Тело `if TYPE_CHECKING` пропускается: круга
    оно не создаёт, а запрещать его значило бы запретить и подсказки типов."""
    исходник = Path(__file__).resolve().parents[1] / "src" / "myharness" / имя_файла
    дерево = ast.parse(исходник.read_text(encoding="utf-8"))
    только_для_типов = set()
    for узел in ast.walk(дерево):
        if isinstance(узел, ast.If) and "TYPE_CHECKING" in ast.unparse(узел.test):
            for ветка in узел.body:
                только_для_типов.update(id(вложенный) for вложенный in ast.walk(ветка))
    импортированное = set()
    for узел in ast.walk(дерево):
        if id(узел) in только_для_типов:
            continue
        if isinstance(узел, ast.Import):
            импортированное.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
        elif isinstance(узел, ast.ImportFrom):
            if узел.module:
                импортированное.add(узел.module.split(".")[0])
            if узел.level and not узел.module:
                импортированное.update(псевдоним.name.split(".")[0] for псевдоним in узел.names)
    return импортированное


импорты_архивариуса = импорты_на_выполнении("archivist.py")
check(
    "archivist.py не импортирует cli и терминал",
    not (импорты_архивариуса & {"cli", "prompt_toolkit"}),
    str(sorted(импорты_архивариуса)),
)
check("имя состояния взято только для подсказок типов", "cli" in запретное_в_импортах("archivist.py"))

check("в инструкции архивариуса есть слово json", "json" in archivist.ARCHIVIST_INSTRUCTION.lower())
check(
    "в инструкции есть пример структуры ответа",
    '{"факты"' in archivist.ARCHIVIST_INSTRUCTION,
    archivist.ARCHIVIST_INSTRUCTION[-200:],
)
# Три ограничения инструкции закрывают три конкретные беды: чужие слова в памяти, догадки
# вместо фактов и строка, бесполезная вне того разговора, где она сказана.
check("инструкция берёт только слова человека", "Только то, что сказал сам человек" in archivist.ARCHIVIST_INSTRUCTION)
check("инструкция запрещает догадки", "догадки" in archivist.ARCHIVIST_INSTRUCTION)
check("инструкция разрешает пустой ответ", "пустой список" in archivist.ARCHIVIST_INSTRUCTION)
check("инструкция требует самодостаточной строки", "понятная без разговора" in archivist.ARCHIVIST_INSTRUCTION)

# Профиль архивариуса: дешёвая модель, без рассуждений, строгий JSON. Своей истории у него
# нет — каждый заход самодостаточен, а помни он прошлые заходы, второй тащил бы в запрос
# весь первый.
профиль_архивариуса = archivist.archivist_profile()
check("архивариус ходит дешёвой моделью", archivist.ARCHIVIST_MODEL == "deepseek-v4-flash", archivist.ARCHIVIST_MODEL)
check("архивариусу не нужна своя история", профиль_архивариуса.keep_history is False)
check(
    "рассуждения выключены",
    профиль_архивариуса.params.get("thinking") == {"type": "disabled"},
    str(профиль_архивариуса.params),
)
check(
    "ответ строго json",
    профиль_архивариуса.params.get("response_format") == {"type": "json_object"},
    str(профиль_архивариуса.params),
)
check("температура низкая — выписка фактов не сочинение", профиль_архивариуса.params.get("temperature") == 0.2)
check("ответ короткий", профиль_архивариуса.params.get("max_tokens") == 512)
# Профиль отдаётся новым на каждый вызов: общий словарь параметров правился бы из фоновой
# задачи, и правка уехала бы в следующий заход.
check(
    "профиль архивариуса — новый на каждый вызов",
    archivist.archivist_profile() is not профиль_архивариуса
    and archivist.archivist_profile().params is not профиль_архивариуса.params,
)

# Запрос: в нём видно, где говорит человек, а где модель. На этом различии стоит вся
# инструкция — «только то, что сказал сам человек».
запрос_архивариуса = archivist.extract_request(
    [("меня зовут Александр", "приятно познакомиться"), ("люблю синий", "хороший выбор")]
)
check(
    "в запрос вошли реплики человека",
    "меня зовут Александр" in запрос_архивариуса and "люблю синий" in запрос_архивариуса,
    запрос_архивариуса,
)
check("в запрос вошли ответы модели", "приятно познакомиться" in запрос_архивариуса, запрос_архивариуса)
check(
    "человек и модель в запросе различимы",
    "Человек:" in запрос_архивариуса and "Модель:" in запрос_архивариуса,
    запрос_архивариуса,
)
check("пустой кусок разговора даёт пустой запрос", archivist.extract_request([]) == "")

# Разбор ответа. Архивариус — фоновая задача: исключение отсюда никто не увидит, поэтому
# любой мусор обязан давать пустой список, а не падение.
check(
    "факты достаются из ответа",
    archivist.parse_facts('{"факты": ["зовут Александр", "любимый цвет — синий"]}')
    == ["зовут Александр", "любимый цвет — синий"],
    str(archivist.parse_facts('{"факты": ["зовут Александр"]}')),
)
for мусор in ("не json", "", "{}", "[]", '{"факты": "строка"}', '{"факты": null}', "null", '"строка"'):
    check(f"мусор {мусор!r} даёт пустой список", archivist.parse_facts(мусор) == [], str(archivist.parse_facts(мусор)))
check("нестроковые элементы пропускаются", archivist.parse_facts('{"факты": [1, 2]}') == [])
check(
    "нестроковый элемент не уносит соседей",
    archivist.parse_facts('{"факты": [1, "зовут Александр"]}') == ["зовут Александр"],
)
check("пробельный факт отброшен", archivist.parse_facts('{"факты": ["   "]}') == [])
# Потолок длины тот же, что у хранилища фактов: факт — короткая самодостаточная строка,
# а не абзац, и уезжает он в системную инструкцию каждого запроса.
check(
    "факт длиннее потолка отброшен",
    archivist.parse_facts(json.dumps({"факты": ["я" * (memory.FACT_CHARS_MAX + 1)]}, ensure_ascii=False)) == [],
)
check(
    "факт ровно по потолку принят",
    archivist.parse_facts(json.dumps({"факты": ["я" * memory.FACT_CHARS_MAX]}, ensure_ascii=False))
    == ["я" * memory.FACT_CHARS_MAX],
)
check("перевод строки внутри факта схлопнут", archivist.parse_facts('{"факты": ["любит\\nсиний"]}') == ["любит синий"])


def состояние_архивариуса(события=None):
    """Состояние с подставным клиентом — как у главного экрана, но без терминала."""
    return cli.State(
        config=Config(api_key="sk-test"),
        client=StubClient(events=события),
        model="deepseek-v4-flash",
        profile=profiles.builtin_default(),
    )


ОТВЕТ_С_ФАКТОМ = [
    api.StreamEvent("content", '{"факты": ["зовут Александр"]}'),
    api.StreamEvent("meta", finish_reason="stop", usage={"prompt_tokens": 20, "completion_tokens": 5}),
]


async def заход_архивариуса():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    состояние.main_agent.remember("меня зовут Александр", "приятно познакомиться")
    await archivist.run(состояние)
    return состояние


состояние_после_захода = asyncio.run(заход_архивариуса())
check("архивариус записал факт", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
файл_нового_факта = next(memory.facts_dir().glob("*.md"))
# Происхождение отличает выписанное архивариусом от сказанного человеком — через месяц
# другого способа понять, откуда взялась строка, нет.
check(
    "источник факта — архивариус",
    "источник: архивариус" in файл_нового_факта.read_text(encoding="utf-8"),
    файл_нового_факта.read_text(encoding="utf-8"),
)
# Молчаливого роста памяти не бывает: каждый выписанный факт назван вслух, иначе выдумку
# архивариуса нечем заметить и нечего убирать.
check(
    "новый факт объявлен в ленте",
    "запомнил: зовут Александр" in "".join(текст for _, текст in состояние_после_захода.main.first.log),
    "".join(текст for _, текст in состояние_после_захода.main.first.log),
)
# Архивариус — сторонний наблюдатель: он не пополняет разговор человека и не пишет в его
# файл сессии, иначе собственный запрос архивариуса оказался бы репликой пользователя.
check(
    "разговор главного агента архивариус не трогает",
    len(состояние_после_захода.main_agent.history()) == 2,
    str(состояние_после_захода.main_agent.history()),
)
вызов_архивариуса = состояние_после_захода.client.calls[0]
check(
    "архивариусу ушла его инструкция",
    вызов_архивариуса["messages"][0]["content"] == archivist.ARCHIVIST_INSTRUCTION,
    вызов_архивариуса["messages"][0]["content"][:120],
)
check(
    "архивариусу ушёл разговор, а не голый вопрос",
    "меня зовут Александр" in вызов_архивариуса["messages"][-1]["content"],
    вызов_архивариуса["messages"][-1]["content"],
)
check("архивариус ходил дешёвой моделью", вызов_архивариуса["model"] == archivist.ARCHIVIST_MODEL, вызов_архивариуса["model"])
check(
    "запрос архивариуса помечен в журнале",
    any(
        json.loads(строка).get("agent") == archivist.AGENT_NAME
        for строка in Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").splitlines()
    ),
)

# Повтор того же разговора новых фактов не добавляет: отбор повторов живёт в хранилище, и
# архивариус на него опирается, а не обходит.
состояние_повтора = asyncio.run(заход_архивариуса())
check("повторный заход не удваивает факт", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
check(
    "повтор в ленте не объявляется",
    "запомнил" not in "".join(текст for _, текст in состояние_повтора.main.first.log),
    "".join(текст for _, текст in состояние_повтора.main.first.log),
)

for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()


async def заход_без_разговора():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    await archivist.run(состояние)
    return состояние


состояние_без_разговора = asyncio.run(заход_без_разговора())
check("без разговора архивариус к модели не ходит", состояние_без_разговора.client.calls == [])
check("и фактов не заводит", memory.load_facts()[0] == [])


async def заход_со_сбоем():
    состояние = cli.State(
        config=Config(api_key="sk-test"),
        client=StubClient(error=RuntimeError("сеть отвалилась")),
        model="deepseek-v4-flash",
        profile=profiles.builtin_default(),
    )
    состояние.main_agent.remember("меня зовут Александр", "приятно познакомиться")
    await archivist.run(состояние)
    await archivist.run(состояние)
    return состояние


состояние_сбоя = asyncio.run(заход_со_сбоем())
лента_сбоя = "".join(текст for _, текст in состояние_сбоя.main.first.log)
# Сбой фонового механизма не имеет права ни уронить заход, ни повторяться после каждого
# обмена: причина у него обычно постоянная, и вторая такая строка вытеснила бы из ленты
# сами ответы. То же правило, что у журнала прогонов.
check("о сбое архивариуса сказано", "архивариус" in лента_сбоя, лента_сбоя)
check("о сбое сказано ровно один раз за сеанс", лента_сбоя.count("архивариус") == 1, лента_сбоя)
check("сбой архивариуса фактов не завёл", memory.load_facts()[0] == [])


async def очередь_архивариуса():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    check("пока обменов мало, архивариус не нужен", not archivist.due(состояние))
    состояние.since_archive = archivist.EVERY_N - 1
    check("за обмен до срока — ещё не пора", not archivist.due(состояние))
    состояние.since_archive = archivist.EVERY_N
    check("на пятом обмене пора", archivist.due(состояние))
    # Двух архивариусов разом быть не должно: они прочтут один и тот же кусок разговора и
    # выпишут из него одни и те же факты, заплатив дважды.
    ворота = asyncio.Event()
    состояние.archivist_task = asyncio.create_task(ворота.wait())
    check("пока архивариус работает, второго не заводим", not archivist.due(состояние))
    ворота.set()
    await состояние.archivist_task
    check("закончил — можно снова", archivist.due(состояние))
    # Выключатель лежит в настройках инструмента: память про пользователя и его папку, а не
    # про то, кем сейчас работает модель.
    состояние.config.remember = False
    check("при выключенном сборе не пора никогда", not archivist.due(состояние))


asyncio.run(очередь_архивариуса())
check("архивариус заходит раз в пять обменов", archivist.EVERY_N == 5)

# Выход. Без последнего захода всё, о чём говорили после прошлого (до четырёх обменов),
# в глобальную память не попало бы вовсе.
for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()


async def выход_с_накопленным():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    состояние.main_agent.remember("меня зовут Александр", "приятно познакомиться")
    состояние.since_archive = 2
    await archivist.finish(состояние)
    return состояние


состояние_выхода = asyncio.run(выход_с_накопленным())
check("при выходе архивариус заходит последний раз", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
check("и выход его дожидается", состояние_выхода.archivist_task is not None and состояние_выхода.archivist_task.done())
for путь in memory.facts_dir().glob("*.md"):
    путь.unlink()


async def выход_без_обменов():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    состояние.main_agent.remember("меня зовут Александр", "приятно познакомиться")
    состояние.since_archive = 0
    await archivist.finish(состояние)
    return состояние


состояние_пустого_выхода = asyncio.run(выход_без_обменов())
# Выход сразу после запуска не имеет права стоить запроса: выписывать не из чего.
check("без обменов с прошлого захода при выходе никто не ходит", состояние_пустого_выхода.client.calls == [])
check("и задачи не заводится", состояние_пустого_выхода.archivist_task is None)


async def выход_с_выключенным_сбором():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    состояние.main_agent.remember("меня зовут Александр", "приятно познакомиться")
    состояние.since_archive = 3
    состояние.config.remember = False
    await archivist.finish(состояние)
    return состояние


check("при выключенном сборе выход архивариуса не зовёт", asyncio.run(выход_с_выключенным_сбором()).client.calls == [])


async def выход_не_ждёт_дольше_срока():
    состояние = состояние_архивариуса(ОТВЕТ_С_ФАКТОМ)
    состояние.archivist_task = asyncio.create_task(asyncio.sleep(30))
    начало = time.monotonic()
    await archivist.finish(состояние)
    прошло = time.monotonic() - начало
    with contextlib.suppress(asyncio.CancelledError):
        await состояние.archivist_task
    return прошло, состояние.archivist_task


# Заставлять человека ждать выхода дольше — хуже, чем потерять несколько фактов. Срок на
# время проверки укорачиваем: ждать настоящие пять секунд ради этого незачем.
настоящий_срок = archivist.EXIT_WAIT_SECONDS
archivist.EXIT_WAIT_SECONDS = 0.05
время_выхода, брошенная_задача = asyncio.run(выход_не_ждёт_дольше_срока())
archivist.EXIT_WAIT_SECONDS = настоящий_срок
check("выход не ждёт затянувшийся заход", время_выхода < 1.0, f"{время_выхода:.2f} с")
check("затянувшийся заход при выходе снимается", брошенная_задача.cancelled())
check("срок ожидания при выходе — пять секунд", archivist.EXIT_WAIT_SECONDS == 5.0)

print("\n18. Выключатель сбора фактов в настройках инструмента")

# Умолчание — включено: память свойство инструмента, а не то, что надо включать в каждом
# профиле заново.
check("умолчание — сбор включён", Config().remember is True)
config_mod.save(Config(api_key="sk-test", remember=False))
check("выключенный сбор переживает перезапуск", config_mod.load().remember is False)
данные_настроек = json.loads(config_mod.config_path().read_text(encoding="utf-8"))
check("выключатель лежит в настройках инструмента", данные_настроек["remember"] is False, str(данные_настроек))
# Испорченное значение даёт умолчание, как и у остальных полей: падение на старте не
# оставляет пользователю выхода — файл настроек он правит руками.
# Мусор проверяем и «истинный», и «ложный»: приведение вида `bool(value)` первый случай
# проходит по совпадению — `bool("нет")` истинно, — а на втором молча выключает память,
# которую человек не выключал.
for мусор_настроек in ("нет", "false", 0, None, [], 3):
    данные_настроек["remember"] = мусор_настроек
    config_mod.config_path().write_text(json.dumps(данные_настроек, ensure_ascii=False), encoding="utf-8")
    check(
        f"испорченное значение {мусор_настроек!r} даёт умолчание, а не падение",
        config_mod.load().remember is True,
        str(config_mod.load().remember),
    )
данные_настроек["remember"] = False
config_mod.config_path().write_text(json.dumps(данные_настроек, ensure_ascii=False), encoding="utf-8")
check("настоящее «выключено» умолчанием не подменяется", config_mod.load().remember is False)
config_mod.save(Config(api_key="sk-test"))

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
