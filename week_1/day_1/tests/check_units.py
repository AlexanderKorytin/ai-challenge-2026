"""Проверки без обращения к DeepSeek: профили, журнал, параметры, панель выбора, дополнения."""

import ast
import asyncio
import json
import os
import subprocess
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
from myharness import cli, team  # noqa: E402
from myharness.agent import Agent
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
# Окно памяти профиля. Ноль — «окно выключено, память не обрезается»: так пользователь
# возвращает прежнее безграничное поведение. Мусор в поле отбрасывается с предупреждением,
# а не подставляется молча: молча подставленное значение сделало бы поведение необъяснимым.
(tmp / "profiles" / "window.json").write_text(
    json.dumps({"name": "window", "history_window": 3}, ensure_ascii=False), encoding="utf-8"
)
оконный, _ = profiles.load("window")
check("history_window прочитан", оконный.history_window == 3, str(оконный.history_window))
check("умолчание окна — десять пар", profile.history_window == 10, str(profile.history_window))
for мусор in ("три", -1, 2.5):
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
summary = team.build_summary_request("задача", [("analyst", "ответ А"), ("critic", "ответ Б")])
check("сводка несёт задачу и ответы каждого", "задача" in summary and "«analyst»" in summary and "ответ Б" in summary)

(tmp / "profiles" / "set.json").write_text(
    json.dumps({"name": "set", "methods": ["free", "meta"]}, ensure_ascii=False), encoding="utf-8"
)
method_set, _ = profiles.load("set")
check("набор способов прочитан", method_set.methods == ["free", "meta"] and method_set.agents == [])
check("широкое окно — три панели в ряд", cli.pane_columns(5, 200) == 3)
check("обычное окно — две", cli.pane_columns(5, 120) == 2)
check("узкое окно — панели одна под другой", cli.pane_columns(5, 80) == 1)

tabs = ui.tabs_fragments([("главный", "done"), ("analyst", "busy")], 0, lambda index: None)
check("вкладки подписаны и пронумерованы", "1 главный" in "".join(f[1] for f in tabs) and "2 analyst" in "".join(f[1] for f in tabs))
check("вкладка кликабельна — на фрагменте обработчик", any(len(f) == 3 for f in tabs))

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

исходник = Path(__file__).resolve().parents[1] / "src" / "myharness" / "agent.py"
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
нарушения = sorted(импортированное & ЗАПРЕЩЁННЫЕ)
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

print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
