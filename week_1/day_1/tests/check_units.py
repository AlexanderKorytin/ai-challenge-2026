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
from myharness import batch, cli, methods, screens as screens_mod, team  # noqa: E402
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
код_успеха, вывод_успеха = выполнить_наряд(удачный_путь)
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
check("путь каталога кодируется в имя папки", memory.project_key(Path("/a/b c")) == "-a-b-c", memory.project_key(Path("/a/b c")))
check(
    "сессии лежат под каталогом состояния",
    memory.sessions_dir(Path("/a/b")) == tmp / "state" / "projects" / "-a-b",
    str(memory.sessions_dir(Path("/a/b"))),
)
check("файл глобальных фактов лежит в каталоге состояния", memory.facts_path() == tmp / "state" / "memory.md", str(memory.facts_path()))

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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
