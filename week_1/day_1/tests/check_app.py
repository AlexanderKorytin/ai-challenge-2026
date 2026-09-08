"""Полноэкранное приложение целиком: клавиши, меню, панель параметров, ответ модели.

Настоящий терминал не нужен: prompt_toolkit умеет работать на трубе и пустом выводе,
а вместо DeepSeek подставлен поддельный клиент — проверки ничего не стоят и не жгут ключ.
"""

import ast
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["MYHARNESS_PROFILES"] = str(tmp / "profiles")
# Настройки — во временный каталог: иначе проверки пишут в настоящий config.json
# пользователя и затирают его ключ. Так уже случилось однажды.
os.environ["MYHARNESS_CONFIG_DIR"] = str(tmp / "config")
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")
# Каталог состояния — туда же: разговоры и глобальные факты пишутся между запусками,
# и без переопределения проверки замусорили бы настоящий ~/.local/state пользователя.
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")
(tmp / "profiles").mkdir(parents=True)

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.key_binding.key_processor import KeyPress  # noqa: E402
from prompt_toolkit.keys import Keys  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.layout.containers import Window  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from prompt_toolkit.data_structures import Point  # noqa: E402
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType  # noqa: E402

from myharness import api, archivist, cli, memory, picker as picker_mod, profiles, ui  # noqa: E402
from myharness.config import Config  # noqa: E402

failures = []
DOWN, ENTER, ESC = "\x1b[B", "\r", "\x1b"


def check(name, condition, detail=""):
    print(f"  [{'OK ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


class FakeClient:
    """Отвечает заранее заданным потоком, запоминая, с чем его позвали."""

    def __init__(self):
        self.calls = []
        self.closed = False

    async def stream_chat(self, model, messages, params=None):
        self.calls.append({"model": model, "messages": messages, "params": dict(params or {})})
        yield api.StreamEvent("reasoning", "прикидываю…")
        yield api.StreamEvent("content", '{"status": "ok", "name": "щука"}')
        yield api.StreamEvent("meta", finish_reason="length", usage={"prompt_tokens": 12, "completion_tokens": 34})

    async def list_models(self):
        return ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]

    async def aclose(self):
        self.closed = True


def fragments_text(fragments):
    """Фрагмент — (стиль, текст) либо (стиль, текст, обработчик мыши): вкладки кликабельны."""
    return "".join(fragment[1] for fragment in fragments)


def log_text(state, target=None):
    """Лента экрана (его первой панели) или конкретной панели."""
    target = target or state.main
    pane = target.first if hasattr(target, "first") else target
    return fragments_text(pane.log)


def panel_fragments(app):
    """Фрагменты списка агентов прямо из раскладки — с обработчиками щелчка.

    Ищем по строке подсказок над списком: собственного имени у окна нет, а подделывать
    фрагменты рядом с приложением значило бы проверять не то, что видит пользователь."""
    for window in app.layout.walk():
        if not isinstance(window, Window):
            continue
        getter = getattr(window.content, "text", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, list) and any("↑/↓" in item[1] for item in value if len(item) >= 2):
            return value
    return []


def panel_text(app):
    return "".join(item[1] for item in panel_fragments(app))


def press(app, key, sequence):
    app.key_processor.feed(KeyPress(key, sequence))
    app.key_processor.process_keys()


def screen_texts(app):
    """Тексты всех окон текущей раскладки — так видно, что показывает строка состояния."""
    out = []
    for window in app.layout.walk():
        if not isinstance(window, Window):
            continue
        getter = getattr(window.content, "text", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, list):
            out.append(fragments_text(value))
    return out


async def main():
    fake = FakeClient()
    state = cli.State(
        # Сбор фактов выключен на время разделов 1–11: архивариус — лишний запрос к
        # подставному клиенту, и он сбил бы счёт вызовов там, где вызовы считают поимённо.
        # Включают его обратно в разделе 12в, где он и проверяется.
        config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
        client=fake,
        model="deepseek-v4-flash",
        profile=profiles.builtin_default(),
    )

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = cli.build_app(state)
        state.app = app
        worker = asyncio.create_task(cli.worker(state))
        run = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.1)

        async def send(text, pause=0.12):
            pipe.send_text(text)
            await asyncio.sleep(pause)

        print("\n1. Меню команд по «/»")
        await send("/")
        buffer = app.layout.get_buffer_by_name("text-area") or app.current_buffer
        check("список команд открылся без Enter", buffer.complete_state is not None)
        count = len(buffer.complete_state.completions) if buffer.complete_state else 0
        check("в списке команды авторизованного, без /auth", count == 13, f"их {count}")
        check("пока агент один, списка агентов нет", not cli.show_agent_panel(state))

        await send(DOWN)
        check("стрелка выбирает пункт", buffer.complete_state.current_completion is not None)
        await send(ENTER)
        check("Enter подставляет команду, а не отправляет её", buffer.text.startswith("/"), repr(buffer.text))
        check("сообщение в очередь не ушло", state.queue.qsize() == 0)

        await send(ESC + "\x7f" * 40)  # закрыть меню и очистить строку

        print("\n2. Панель параметров")
        await send("/set temperature" + ENTER)
        check("панель открылась", state.picker is not None)
        await send("щука")  # печать при открытой панели не должна проходить
        check("панель модальна — текст в строку не попал", state.picker is not None and "щука" not in buffer.text)
        await send(DOWN + ENTER)
        check("значение выбрано стрелкой и применено", "temperature" in state.profile.params, str(state.profile.params))
        check("панель закрылась", state.picker is None)
        await send("/set max_tokens" + ENTER)
        # Одиночный Esc через трубу не доходит: \x1b — префикс escape-последовательностей,
        # и парсер ждёт продолжения, которого в тесте не будет (в живом терминале его
        # выпускает таймаут). Поэтому клавишу подаём прямо обработчику.
        app.key_processor.feed(KeyPress(Keys.Escape, "\x1b"))
        app.key_processor.process_keys()
        await asyncio.sleep(0.5)  # Esc выпускается по таймауту ожидания продолжения (app.timeoutlen)
        check(
            "Esc закрывает без изменений",
            state.picker is None and "max_tokens" not in state.profile.params,
            f"panel={state.picker is not None}, params={state.profile.params}",
        )
        await send("/set max_tokens" + ENTER)
        await send("\x03")  # Ctrl+C — второй путь выхода из панели
        check("Ctrl+C тоже закрывает панель", state.picker is None and "max_tokens" not in state.profile.params)

        print("\n3. Списки — панель выбора, а не текст в логе")
        await send("/model" + ENTER, pause=0.25)
        check("/model открыл панель", state.picker is not None and len(state.picker.items) == 3, str(state.picker))
        check("текущая модель помечена", state.picker.marked == 0)
        await send(DOWN + ENTER)
        check("модель выбрана стрелкой", state.model == "deepseek-v4-pro", state.model)

        await send("/profile" + ENTER, pause=0.2)
        check("/profile открыл панель", state.picker is not None)
        titles = [i.label for i in state.picker.items]
        check("в панели есть профили", "s3" in titles or "default" in titles, str(titles))
        await send(ESC, pause=0.5)

        await send("/params" + ENTER, pause=0.2)
        check("/params открыл панель параметров", state.picker is not None and len(state.picker.items) >= 7)
        await send(ESC, pause=0.5)
        check("панель закрыта перед следующим шагом", state.picker is None)

        print("\n4. Строка состояния")
        check("показывает, что ключ есть", any("● авторизован" in s for s in screen_texts(app)))
        state.config.api_key = None
        check("сразу отражает потерю ключа", any("не авторизован" in s for s in screen_texts(app)))
        state.config.api_key = "sk-test"
        check("и возвращается обратно без перезапуска", any("● авторизован" in s for s in screen_texts(app)))

        print("\n5. Ответ модели")
        await send("щука" + ENTER, pause=0.4)
        text = log_text(state)
        check("вопрос показан", "› щука" in text)
        check("рассуждения показаны", "прикидываю…" in text)
        check("ответ показан", '"status": "ok"' in text)
        check("расход токенов показан", "токены: вход 12, выход 34" in text)
        check("обрыв по лимиту назван прямо", "упёрлось в max_tokens" in text)
        check("подсказка, что делать с обрывом", "/set max_tokens" in text)
        check("temperature ушла в запрос", fake.calls[0]["params"].get("temperature") is not None)
        check("запрос ушёл выбранной моделью", fake.calls[0]["model"] == "deepseek-v4-pro", fake.calls[0]["model"])

        print("\n6. Журнал")
        record = json.loads(Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").strip().splitlines()[0])
        check("прогон записан", record["status"] == "ok" and record["query"] == "щука")
        check("в записи слепок профиля с параметрами", "temperature" in record["profile"]["params"])
        check("в записи причина остановки и токены", record["finish_reason"] == "length" and record["usage"]["completion_tokens"] == 34)
        check("ключ в журнал не попал", "sk-test" not in json.dumps(record, ensure_ascii=False))

        print("\n6a. История разговора")
        # Профиль с накоплением истории: два вопроса подряд обязаны попасть в один разговор.
        profiles_dir = tmp / "profiles"
        (profiles_dir / "talky.json").write_text(
            json.dumps({"name": "talky", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile talky" + ENTER, pause=0.25)
        before = len(fake.calls)
        await send("первый вопрос" + ENTER, pause=0.4)
        await send("второй вопрос" + ENTER, pause=0.4)
        first, second = fake.calls[before], fake.calls[before + 1]
        # Подставной клиент всегда отдаёт содержимое ответа, значит после первого обмена
        # в истории лежит ровно одна пара «вопрос — ответ» = 2 сообщения. Системная
        # инструкция кладётся поверх истории и в саму историю не входит.
        check(
            "в первый запрос ушли только инструкция и вопрос",
            len(first["messages"]) == 2,
            str([m["role"] for m in first["messages"]]),
        )
        check(
            "во второй запрос ушла история предыдущего обмена",
            len(second["messages"]) == 4,
            str([m["role"] for m in second["messages"]]),
        )
        check(
            "роли второго запроса идут по порядку",
            [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"],
            str([m["role"] for m in second["messages"]]),
        )
        check(
            "в истории лежит первый вопрос, а не второй",
            second["messages"][1]["content"] == "первый вопрос",
            repr(second["messages"][1]["content"]),
        )

        print("\n6b. Обрезка памяти видна пользователю")
        # Молчаливая обрезка недопустима: первая же потерянная отсылка («сделай короче», а
        # сокращать уже нечего) будет отлажена пользователем как «модель поглупела». Поле
        # `dropped_pairs` само по себе ничего не значит — важно, что о нём СКАЗАНО в ленте.
        # ОТКУДА ЧИСЛА: окно — 1 пара, запас обрезки — 5, значит порог 1 + 5 = 6 пар. Кладём
        # в память ровно шесть пар и задаём вопрос: обрезка выбрасывает весь запас — 5 пар.
        (profiles_dir / "forgetful.json").write_text(
            json.dumps(
                {"name": "forgetful", "system": "помни немного", "keep_history": True, "history_window": 1},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        await send("/profile forgetful" + ENTER, pause=0.25)
        for номер in range(6):
            state.main_agent.remember(f"старый вопрос {номер}", f"старый ответ {номер}")
        mark = len(state.main.first.log)
        await send("свежий вопрос" + ENTER, pause=0.4)
        свежее = fragments_text(state.main.first.log[mark:])
        check("про обрезку памяти сказано в ленте", "память обрезана" in свежее, свежее[-200:])
        check("названо, сколько пар выброшено", "выброшено 5 пар" in свежее, свежее[-200:])

        print("\n7. Группа агентов")
        profiles_dir = tmp / "profiles"
        for name, system in (("analyst", "ты аналитик"), ("critic", "ты критик")):
            (profiles_dir / f"{name}.json").write_text(
                json.dumps({"name": name, "system": system, "keep_history": False}, ensure_ascii=False),
                encoding="utf-8",
            )
        (profiles_dir / "lead.json").write_text(
            json.dumps(
                {"name": "lead", "system": "сведи ответы", "keep_history": False, "agents": ["analyst", "critic"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "meta.json").write_text(
            json.dumps({"name": "meta", "prefill": "составь промпт для задачи"}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile meta" + ENTER, pause=0.25)
        check("заготовка профиля подставлена в строку ввода", buffer.text == "составь промпт для задачи", repr(buffer.text))
        check("но сама не отправлена — ждём Enter", state.queue.qsize() == 0 and not state.busy)
        await send("\x7f" * 40)

        await send("/team" + ENTER, pause=0.2)
        check("без группы /team объясняет, чего не хватает", "группа не задана" in log_text(state))

        before = len(fake.calls)
        await send("/profile lead" + ENTER, pause=0.25)
        check("состав группы показан при выборе профиля", "● analyst" in log_text(state) and "● critic" in log_text(state))

        await send("почему так?" + ENTER, pause=0.8)
        check("экраны группы заведены", [s.key for s in state.screens] == ["main", "lead", "lead:summary"], str([s.key for s in state.screens]))
        board = state.screens[1]
        summary_screen = state.screens[2]
        check("у каждого эксперта своя панель", [p.key for p in board.panes] == ["analyst", "critic"], str([p.key for p in board.panes]))
        analyst_pane = board.panes[0]
        check("в панели эксперта его постановка задачи", "● analyst" in log_text(state, analyst_pane) and "ты аналитик" in log_text(state, analyst_pane))
        check("инструкция свёрнута, но доступна целиком", "(/system — целиком)" in log_text(state, analyst_pane))
        check("в панели эксперта его рассуждения и ответ", "прикидываю…" in log_text(state, analyst_pane) and '"status": "ok"' in log_text(state, analyst_pane))
        check("ответ соседа в чужую панель не попадает", "ты критик" not in log_text(state, analyst_pane))
        check("вывод агентов в главный экран не льётся", "ты аналитик" not in log_text(state))
        check("в главном экране сказано, где смотреть", "группа поднята: analyst, critic" in log_text(state))
        check("сводка ведущего — на своей вкладке", "свожу ответы агентов (2)" in log_text(state, summary_screen))

        agent_calls = fake.calls[before:]
        systems = [c["messages"][0]["content"] for c in agent_calls]
        check("каждому агенту ушла своя инструкция", "ты аналитик" in systems and "ты критик" in systems, str(systems))
        check("агенты не видели ответов друг друга", all(len(c["messages"]) == 2 for c in agent_calls[:2]))
        summary_call = agent_calls[-1]
        check("ведущему ушли ответы всех агентов", summary_call["messages"][0]["content"] == "сведи ответы" and "Ответ эксперта «critic»" in summary_call["messages"][1]["content"])

        check("список агентов появился", cli.show_agent_panel(state))
        check("в списке видны агенты группы", "analyst" in panel_text(app) and "critic" in panel_text(app), panel_text(app))
        handler = next(f[2] for f in panel_fragments(app) if len(f) == 3 and "analyst" in f[1])
        handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        check("клик по строке списка открывает экран агента", state.active == 1 and state.screen is board)

        # Итог оркестратора возвращается в главный экран: человеку, ведущему разговор, не
        # приходится идти на чужую вкладку и смотреть, чем всё кончилось.
        check("сводка ведущего вернулась в главный экран", "сводка группы «lead»" in log_text(state), log_text(state)[-300:])
        хвост = log_text(state).split("сводка группы «lead»")[-1]
        check("в главном экране лежит сам ответ, а не ссылка на вкладку", '"status": "ok"' in хвост[:200], хвост[:200])
        память = state.main_agent.history()
        check("итог попал и в память главного агента", len(память) == 2 and память[0]["content"] == "почему так?", str(память))
        check("в памяти лежит текст итога", '"status": "ok"' in память[1]["content"], str(память))
        await send("/system" + ENTER, pause=0.25)
        check("/system на панели эксперта показывает его инструкцию", "ты аналитик" in log_text(state, board.panes[0]))
        cli.switch_screen(state, 0)
        check("возврат на главный экран", state.active == 0)

        records = [json.loads(line) for line in Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").splitlines()]
        team_records = [r for r in records if r.get("run_id")]
        check("в журнале отмечено, кто отвечал", {r.get("agent") for r in team_records} == {"analyst", "critic", "lead"}, str([r.get("agent") for r in team_records]))
        check("вся группа помечена одним прогоном", len({r["run_id"] for r in team_records}) == 1)

        print("\n8. Рабочие экраны и мышь")
        (profiles_dir / "step_ask.json").write_text(
            json.dumps(
                {
                    "name": "step_ask",
                    "description": "вставьте логическую задачу",
                    "system": "составь промпт",
                    "prefill": "вставьте логическую задачу",
                    "keep_history": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "step_solve.json").write_text(
            json.dumps({"name": "step_solve", "prefill": "вставьте промпт", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "two_steps.json").write_text(
            json.dumps({"name": "two_steps", "screens": ["step_ask", "step_solve"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile two_steps" + ENTER, pause=0.3)
        check("рабочие экраны открыты вместо экранов группы", [s.key for s in state.screens] == ["main", "step_ask", "step_solve"], str([s.key for s in state.screens]))
        check("сразу открыт первый шаг", state.active == 1)
        check("заготовка первого шага в строке ввода", buffer.text == "вставьте логическую задачу", repr(buffer.text))
        check("вводная шага написана в его ленте", "вставьте логическую задачу" in log_text(state, state.screens[1]))

        before = len(fake.calls)
        await send("\x7f" * 40 + "задача про шофёров" + ENTER, pause=0.5)
        step_screen = state.screens[1]
        check("вопрос и ответ остались на своём экране", "задача про шофёров" in log_text(state, step_screen) and '"status": "ok"' in log_text(state, step_screen))
        check("главный экран не тронут", "задача про шофёров" not in log_text(state))
        check("экран остался открытым — не перескочили на главный", state.active == 1)
        check("ушла инструкция этого экрана", fake.calls[before]["messages"][0]["content"] == "составь промпт")

        cli.switch_screen(state, 2)
        check("заготовка второго шага подставилась при переходе", buffer.text == "вставьте промпт", repr(buffer.text))
        buffer.text = "своё"
        cli.switch_screen(state, 1)
        check("набранное вручную заготовка не затирает", buffer.text == "своё")
        buffer.text = ""

        # Поведение изменено осознанно: мышь у harness всегда. Прежний переключатель «или клики,
        # или выделение» опирался на ложный выбор — губило выделение отслеживание перетаскивания
        # (1003h), а не сами клики. Приложение просит у терминала только нажатия, поэтому клики
        # и выделение текста уживаются без команд.
        check("мышь у harness сразу — клики работают без команд", state.mouse_enabled is True)
        check("приложение перехватывает мышь с самого начала", app.mouse_support() is True)
        check("строка состояния молчит, пока всё в порядке", not any("отдана терминалу" in text for text in screen_texts(app)))
        app.key_processor.feed(KeyPress(Keys.F2, "\x1bOQ"))
        app.key_processor.process_keys()
        await asyncio.sleep(0.15)
        check("F2 — аварийный выход: мышь целиком терминалу", state.mouse_enabled is False and app.mouse_support() is False)
        check("о потере кликов сказано в строке состояния", any("отдана терминалу" in text for text in screen_texts(app)))
        await send("/mouse" + ENTER, pause=0.2)
        check("/mouse возвращает мышь harness", state.mouse_enabled is True and app.mouse_support() is True)

        print("\n9. Набор способов: один вопрос — все подходы сразу")
        (profiles_dir / "plain.json").write_text(
            json.dumps({"name": "plain", "system": "отвечай прямо", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "task.json").write_text(
            json.dumps({"name": "task", "methods": ["plain", "two_steps", "lead"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile task" + ENTER, pause=0.3)
        keys = [s.key for s in state.screens]
        check("вкладки способов открыты сразу, до вопроса", keys == ["main", "plain", "two_steps", "lead", "lead:summary"], str(keys))
        check("остались на главном экране — вопрос вводится здесь", state.active == 0)
        check("в главном сказано, что задача уйдёт во все способы", "способы: plain, two_steps, lead" in log_text(state))

        before = len(fake.calls)
        await send("как из рубашки сделать птицу?" + ENTER, pause=1.2)
        plain_screen, chain_screen, board = state.screens[1], state.screens[2], state.screens[3]
        check("простой способ ответил на своей вкладке", '"status": "ok"' in log_text(state, plain_screen))
        check("у цепочки панель на каждый шаг", [p.key for p in chain_screen.panes] == ["step_ask", "step_solve"], str([p.key for p in chain_screen.panes]))
        solve_pane = chain_screen.panes[1]
        check("второй шаг получил ответ первого автоматически", '"status": "ok"' in log_text(state, solve_pane) and "как из рубашки" in log_text(state, solve_pane))
        solve_calls = [c for c in fake.calls[before:] if c["messages"][-1]["content"].count("как из рубашки") == 1 and "{" in c["messages"][-1]["content"]]
        check("в запрос второго шага вошёл текст первого", bool(solve_calls), "промпт первого шага во второй запрос не попал")
        check("у группы панели по экспертам", [p.key for p in board.panes] == ["analyst", "critic"], str([p.key for p in board.panes]))
        check("эксперты отвечали в свои панели", all('"status": "ok"' in log_text(state, pane) for pane in board.panes))
        check("сводка ведущего на своей вкладке", "свожу ответы агентов" in log_text(state, state.screens[4]))

        # Итоги всех способов возвращаются в главный экран — и в порядке набора, а не в
        # порядке, в каком способы управились: сравнивают их по столбцам набора.
        главный = log_text(state)
        итоги = [
            главный.find("ответ способа «plain»"),
            главный.find("итог цепочки «two_steps»"),
            главный.find("сводка группы «lead»", главный.find("способы: plain")),
        ]
        check("итог каждого способа вернулся в главный экран", all(место > 0 for место in итоги), str(итоги))
        # Предмет работы не теряется: в итоге цепочки есть ответ КАЖДОГО шага, а не только
        # последнего. На дне 6 последним шагом стоял проверяющий, и наверх уезжал вердикт без кода.
        хвост_цепочки = главный.split("итог цепочки «two_steps»")[-1]
        check(
            "в итоге цепочки подписаны оба шага, а не только последний",
            "[step_ask]" in хвост_цепочки[:600] and "[step_solve]" in хвост_цепочки[:600],
            хвост_цепочки[:300],
        )
        check("итоги идут в порядке набора, а не готовности", итоги == sorted(итоги), str(итоги))
        память = state.main_agent.history()
        check("на весь прогон одна пара в памяти, а не по паре на способ", len(память) == 2, str(len(память)))
        подписи = ["[ответ способа «plain»]", "[итог цепочки «two_steps»]", "[сводка группы «lead»]"]
        check(
            "в памяти итоги подписаны, иначе это склейка из трёх ответов",
            all(подпись in память[1]["content"] for подпись in подписи),
            память[1]["content"][:200],
        )

        cli.switch_screen(state, 3)
        check("панель по умолчанию первая", state.screen.active_pane == 0)
        cli.switch_pane(state, 1)
        check("Alt+стрелка переводит на соседнюю панель", state.screen.pane.key == "critic")
        cli.switch_pane(state, 2)
        check("панели перебираются по кругу", state.screen.pane.key == "analyst")
        cli.toggle_zoom(state)
        check("F3 разворачивает панель на весь экран", state.screen.zoomed is True)
        cli.toggle_zoom(state)
        check("и возвращает сетку", state.screen.zoomed is False)
        cli.switch_screen(state, 0)

        print("\n10. Список агентов под строкой ввода")
        # Группа поднимается заново, поверх набора способов: так в списке заведомо есть и
        # отвечавшие агенты, и один не отвечавший — собеседник главного экрана. Смена
        # профиля заводит его заново, поэтому прежние обмены на него не переносятся.
        await send("/profile lead" + ENTER, pause=0.3)
        await send("кто из вас прав?" + ENTER, pause=0.9)
        rows = cli.collect_agents(state)
        # Агентов ровно четыре: собеседник главного экрана, два эксперта и ведущий на сводке.
        check(
            "порядок строк повторяет порядок экранов и панелей",
            [row.agent.name for row in rows] == ["main", "analyst", "critic", "lead:summary"],
            str([row.agent.name for row in rows]),
        )
        строки = [строка for строка in panel_text(app).split("\n") if строка.strip()]
        check("список стоит под строкой ввода без всяких команд", cli.show_agent_panel(state))
        check("над списком строка подсказок", "↑/↓" in строки[0] and "клик" in строки[0], строки[0])
        check("главный разговор — первой строкой", строки[1].strip().startswith("○ main") or строки[1].strip().startswith("● main"), строки[1])
        check(
            "у отвечавшего агента посчитаны токены",
            any("↓ 46" in строка and "critic" in строка for строка in строки),
            str(строки),
        )
        check(
            "агент, который ещё не отвечал, показан прочерками",
            next(строка for строка in строки if "main" in строка).count("—") == 2,
            next(строка for строка in строки if "main" in строка),
        )

        # Стрелки водят по строкам списка: тот же путь, что и щелчок мышью.
        cli.switch_screen(state, 0)
        cli.switch_pane(state, 0)
        press(app, Keys.Down, "\x1b[B")
        await asyncio.sleep(0.15)
        check(
            "↓ переводит на следующего агента списка",
            state.screen.key == "lead" and state.screen.pane.key == "analyst",
            f"{state.screen.key} / {state.screen.pane.key}",
        )
        check("закрашен тот, на кого перешли", any(строка.strip().startswith("● analyst") for строка in panel_text(app).split("\n")), panel_text(app))
        press(app, Keys.Up, "\x1b[A")
        await asyncio.sleep(0.15)
        check("↑ возвращает на предыдущего", state.active == 0 and state.screen.key == "main")

        # Меню команд ходит теми же стрелками, и отнимать их у него нельзя.
        await send("/")
        press(app, Keys.Down, "\x1b[B")
        await asyncio.sleep(0.15)
        check("при открытом меню команд стрелка выбирает команду, а не агента", state.active == 0 and buffer.complete_state is not None)
        await send(ESC + "\x7f" * 40)

        # Клик мышью по строке: обработчик висит на самой строке, включая отбивку справа.
        row_handler = next(f[2] for f in panel_fragments(app) if len(f) == 3 and "critic" in f[1])
        row_handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        check(
            "клик по строке переводит на экран и панель агента",
            state.screen.key == "lead" and state.screen.pane.key == "critic",
            f"{state.screen.key} / {state.screen.pane.key}",
        )
        cli.switch_screen(state, 0)

        print("\n11. Цепочка вкладками: каждый шаг на своей вкладке")
        # Раскладка панелями (раздел 9) остаётся умолчанием — здесь профиль цепочки просит
        # «tabs», и те же самые шаги должны разъехаться по отдельным вкладкам, не потеряв
        # передачу работы между собой.
        (profiles_dir / "tab_ask.json").write_text(
            json.dumps(
                {"name": "tab_ask", "title": "постановка", "system": "составь промпт", "keep_history": False},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "tab_solve.json").write_text(
            json.dumps({"name": "tab_solve", "title": "решение", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "two_tabs.json").write_text(
            json.dumps(
                {"name": "two_tabs", "title": "по вкладкам", "screens": ["tab_ask", "tab_solve"], "layout": "tabs"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "task_tabs.json").write_text(
            json.dumps({"name": "task_tabs", "methods": ["plain", "two_tabs"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile task_tabs" + ENTER, pause=0.3)
        keys = [s.key for s in state.screens]
        check(
            "у цепочки с layout=tabs по вкладке на шаг, ключ несёт имя цепочки",
            keys == ["main", "plain", "two_tabs:tab_ask", "two_tabs:tab_solve"],
            str(keys),
        )
        titles = [s.title for s in state.screens[2:]]
        check("заголовки вкладок различимы и взяты у профилей шагов", titles == ["постановка", "решение"], str(titles))
        check(
            "вкладки шагов только на просмотр — ввод уходит в главный экран",
            all(not s.interactive for s in state.screens[2:]) and state.active == 0,
        )

        before = len(fake.calls)
        await send("как из рубашки сделать птицу?" + ENTER, pause=1.2)
        ask_tab, solve_tab = state.screens[2], state.screens[3]
        check(
            "каждый шаг ответил в свою вкладку",
            '"status": "ok"' in log_text(state, ask_tab) and '"status": "ok"' in log_text(state, solve_tab),
            log_text(state, solve_tab),
        )
        solve_calls = [
            c
            for c in fake.calls[before:]
            if c["messages"][-1]["content"].count("как из рубашки") == 1 and "{" in c["messages"][-1]["content"]
        ]
        check("в запрос второго шага вошёл ответ первого", bool(solve_calls), "ответ первого шага во второй запрос не попал")

        await send("а обратно?" + ENTER, pause=1.2)
        check(
            "повторный вопрос новых вкладок не заводит",
            [s.key for s in state.screens] == keys,
            str([s.key for s in state.screens]),
        )
        cli.switch_screen(state, 0)

        print("\n12. Память между запусками")
        # Сбор фактов на время остального прогона выключен намеренно: архивариус — лишний
        # запрос к подставному клиенту, и он сбил бы счёт вызовов в разделах выше. Здесь его
        # включают обратно и проверяют отдельно.
        cli.switch_screen(state, 0)
        await send("/profile talky" + ENTER, pause=0.3)
        каталог_запуска = Path.cwd()
        файл_разговора = state.store.path if state.store else None
        check("разговор пишется в файл сессии профиля", файл_разговора is not None and файл_разговора.exists(), str(файл_разговора))
        check(
            "файл разговора лежит в каталоге профиля, а не проекта",
            файл_разговора.parent == memory.profile_dir(каталог_запуска, "talky"),
            str(файл_разговора.parent),
        )
        check("права файла разговора 600", oct(файл_разговора.stat().st_mode & 0o777) == "0o600", oct(файл_разговора.stat().st_mode & 0o777))

        # Перезапуск без второго процесса: собираем состояние заново в том же каталоге и с
        # тем же профилем — ровно то, что делает `_main` при следующем запуске.
        сохранённых_пар = len(memory.read_session(файл_разговора, window=0, system_fp="").pairs)
        второй_запуск = cli.State(
            config=Config(api_key="sk-test", model="deepseek-v4-flash", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        check("новое состояние пустое до восстановления", второй_запуск.main_agent.history() == [])
        cli.restore_conversation(второй_запуск)
        поднятое = второй_запуск.main_agent.history()
        check(
            "второй запуск в той же папке поднял разговор",
            len(поднятое) == сохранённых_пар * 2 and поднятое[0]["content"] == "первый вопрос",
            str(поднятое),
        )
        отчёт = log_text(второй_запуск)
        check("в ленте строка отчёта о восстановлении", "восстановлен разговор:" in отчёт, отчёт)
        check("в отчёте оба числа — сколько поднято и сколько сохранено", f"из {сохранённых_пар} сохранённых" in отчёт, отчёт)
        check("в отчёте время последней записи", "последняя " in отчёт, отчёт)
        # Решение пользователя: экран остаётся чистым, память полной. Печатай мы реплики,
        # каждый запуск начинался бы с простыни прошлого разговора.
        check("сами реплики в ленту не печатаются", "первый вопрос" not in отчёт, отчёт)
        check("восстановление продолжает найденную сессию, а не заводит новую", второй_запуск.store.path == файл_разговора, str(второй_запуск.store.path))
        # Восстановление идёт единственным методом пополнения памяти и не пишет на диск:
        # иначе чтение файла тут же удваивало бы его самим собой.
        размер_после_восстановления = файл_разговора.stat().st_size
        cli.restore_conversation(второй_запуск)
        check(
            "восстановление на диск ничего не пишет",
            файл_разговора.stat().st_size == размер_после_восстановления,
            str(файл_разговора.stat().st_size),
        )

        # Чистый запуск не сообщает ничего: разговора не было, и говорить не о чем.
        чистый_каталог = tmp / "чистая-папка"
        чистый_каталог.mkdir()
        прежний_каталог = os.getcwd()
        os.chdir(чистый_каталог)
        чистый_запуск = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        cli.restore_conversation(чистый_запуск)
        check("в новой папке восстанавливать нечего", чистый_запуск.main_agent.history() == [])
        check("и в ленте об этом ни строки", log_text(чистый_запуск) == "", log_text(чистый_запуск))
        check("хранилище всё равно открыто — обмену есть куда писать", чистый_запуск.store is not None)
        os.chdir(прежний_каталог)

        # Правку инструкции между запусками называют вслух: молча продолженный разговор
        # выглядел бы цельным, не будучи им.
        правленый = profiles.load("talky")[0]
        правленый.system = "болтай иначе"
        запуск_с_правкой = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=правленый,
        )
        cli.restore_conversation(запуск_с_правкой)
        check("о правке инструкции сказано", "инструкция профиля изменилась" in log_text(запуск_с_правкой), log_text(запуск_с_правкой))

        # Три места, куда встроена память, поведением из этой проверки не достаются:
        # `_main` и `repl` поднимают настоящее приложение, а проверка собирает состояние
        # сама. Смотрим дерево разбора — тем же приёмом, каким в check_units проверяется
        # единственность точки записи в память.
        def вызовы(имя_функции):
            дерево = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
            for узел in ast.walk(дерево):
                if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef)) and узел.name == имя_функции:
                    return {ast.unparse(вызов.func) for вызов in ast.walk(узел) if isinstance(вызов, ast.Call)}
            return set()

        check("запуск поднимает прежний разговор", "restore_conversation" in вызовы("_main"), str(sorted(вызовы("_main"))))
        # В конструкторе состояния восстановлению не место: его зовут проверки напрямую, и
        # любое собранное состояние начало бы читать и писать в каталог состояния человека.
        check("конструктор состояния на диск не ходит", "restore_conversation" not in вызовы("__post_init__"))
        check("смена профиля подхватывает разговор нового профиля", "restore_conversation" in вызовы("switch_profile"))
        check("очередь запросов заводит заход архивариуса", "archivist.start" in вызовы("worker"), str(sorted(вызовы("worker"))))
        check("выход зовёт архивариуса последний раз", "archivist.finish" in вызовы("repl"), str(sorted(вызовы("repl"))))

        # Испорченную строку в файле разговора не проглатываем молча: восстановление
        # продолжается, но человеку сказано, что часть переписки не прочлась.
        (profiles_dir / "битый.json").write_text(
            json.dumps({"name": "битый", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        битая_сессия = memory.new_session(каталог_запуска, "битый")
        хранилище_битой = memory.SessionStore(битая_сессия)
        хранилище_битой.append("user", "целый вопрос")
        хранилище_битой.append("assistant", "целый ответ")
        with битая_сессия.open("a", encoding="utf-8") as файл:
            файл.write("{это не json\n")
        запуск_с_битой = cli.State(
            config=Config(api_key="sk-test", profile="битый", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("битый")[0],
        )
        cli.restore_conversation(запуск_с_битой)
        check("испорченная строка названа вслух", "испорчена" in log_text(запуск_с_битой), log_text(запуск_с_битой))
        check("и остальной разговор всё равно поднят", len(запуск_с_битой.main_agent.history()) == 2, str(запуск_с_битой.main_agent.history()))

        print("\n12a. /clear открывает новую сессию")
        прежний_файл = state.store.path
        пар_до_очистки = len(memory.read_session(прежний_файл, window=0, system_fp="").pairs)
        await send("/clear" + ENTER, pause=0.25)
        check("сказано, что начат новый разговор", "начат новый разговор" in log_text(state), log_text(state)[-200:])
        check("память главного агента пуста", state.main_agent.history() == [])
        # Стереть файл значило бы издевательство наоборот: ошибочный /clear был бы
        # необратим. Файл остаётся, но следующий запуск его уже не поднимет.
        check("прежний файл разговора цел", прежний_файл.exists() and len(memory.read_session(прежний_файл, window=0, system_fp="").pairs) == пар_до_очистки)
        check("пишем уже в новый файл", state.store.path != прежний_файл, str(state.store.path))
        await send("после очистки" + ENTER, pause=0.4)
        запуск_после_очистки = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        cli.restore_conversation(запуск_после_очистки)
        поднятое_после_очистки = запуск_после_очистки.main_agent.history()
        check(
            "перезапуск после /clear поднимает только новый разговор",
            [сообщение["content"] for сообщение in поднятое_после_очистки[:1]] == ["после очистки"],
            str(поднятое_после_очистки),
        )
        check("стёртое обратно не возвращается", not any("первый вопрос" == сообщение["content"] for сообщение in поднятое_после_очистки))

        # Очистка обязана пережить перезапуск ДАЖЕ БЕЗ единого обмена после неё. Заводись
        # файл при первой записи, а не сразу, — очистил, вышел молча, и следующий запуск
        # нашёл бы самым свежим прежний файл и поднял ровно то, что человек стёр.
        # Ведём на своём профиле: пустая сессия, оставленная у `talky`, сбила бы соседние
        # проверки, которым нужен его разговор.
        (profiles_dir / "забывчивый.json").write_text(
            json.dumps({"name": "забывчивый", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile забывчивый" + ENTER, pause=0.3)
        await send("сказано до очистки" + ENTER, pause=0.4)
        await send("/clear" + ENTER, pause=0.3)
        check("новый файл разговора заведён сразу, до первой реплики", state.store.path.exists(), str(state.store.path))
        молчаливый_запуск = cli.State(
            config=Config(api_key="sk-test", profile="забывчивый", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("забывчивый")[0],
        )
        cli.restore_conversation(молчаливый_запуск)
        check(
            "очистка без единого обмена переживает перезапуск",
            молчаливый_запуск.main_agent.history() == [],
            str(молчаливый_запуск.main_agent.history()),
        )
        await send("/profile talky" + ENTER, pause=0.3)


        # Смена профиля — переход в другой разговор, потому что ключ пары изменился.
        (profiles_dir / "talky2.json").write_text(
            json.dumps({"name": "talky2", "system": "болтай иначе", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile talky2" + ENTER, pause=0.3)
        check("у другого профиля свой каталог разговоров", state.store.path.parent == memory.profile_dir(каталог_запуска, "talky2"), str(state.store.path.parent))
        check("чужой разговор не подхвачен", state.main_agent.history() == [], str(state.main_agent.history()))
        await send("вопрос второму профилю" + ENTER, pause=0.4)
        # Шапка приложения печатается один раз за сеанс. Она уехала было в смену профиля
        # вместе с правкой порядка печати при запуске, и каждый /profile рисовал рамку заново.
        шапок_до_возврата = log_text(state).count("myharness  ·  DeepSeek API")
        await send("/profile talky" + ENTER, pause=0.35)
        check(
            "смена профиля не печатает шапку заново",
            log_text(state).count("myharness  ·  DeepSeek API") == шапок_до_возврата,
            str((шапок_до_возврата, log_text(state).count("myharness  ·  DeepSeek API"))),
        )
        вернулись = state.main_agent.history()
        check(
            "возврат к прежнему профилю поднимает ЕГО разговор",
            bool(вернулись) and вернулись[0]["content"] == "после очистки",
            str(вернулись),
        )
        check("отчёт о восстановлении показан при смене профиля", "восстановлен разговор:" in log_text(state), log_text(state)[-300:])

        print("\n12b. Команды глобальной памяти")
        for путь in memory.facts_dir().glob("*.md"):
            путь.unlink()
        await send("/memory" + ENTER, pause=0.2)
        check("на пустой памяти сказано, что она пуста", "глобальная память пуста" in log_text(state), log_text(state)[-200:])
        await send("/remember" + ENTER, pause=0.2)
        check("без текста /remember объясняет, чего не хватает", "нужен текст факта" in log_text(state), log_text(state)[-200:])
        check("и панель выбора не открывает — факт пишут словами", state.picker is None)
        await send("/remember зовут Александр" + ENTER, pause=0.2)
        check("факт объявлен в ленте", "запомнил: зовут Александр" in log_text(state), log_text(state)[-200:])
        check("факт лёг в глобальную память", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
        await send("/remember любимый цвет — синий" + ENTER, pause=0.2)
        await send("/memory" + ENTER, pause=0.2)
        память_в_ленте = log_text(state)[-400:]
        check("список фактов пронумерован", "1. зовут Александр" in память_в_ленте and "2. любимый цвет — синий" in память_в_ленте, память_в_ленте)
        check("над списком сказано, собираются ли факты", "сбор фактов" in память_в_ленте, память_в_ленте)

        # Факты уезжают в системную инструкцию главного разговора — оттуда «как меня зовут»
        # отвечается в любой папке, даже в новой, где разговора ещё не было.
        before = len(fake.calls)
        await send("как меня зовут?" + ENTER, pause=0.4)
        системное = fake.calls[before]["messages"][0]["content"]
        check("факты ушли в системную инструкцию", "зовут Александр" in системное, системное)
        check("инструкция профиля при этом на месте", "болтай" in системное, системное)

        await send("/forget 1" + ENTER, pause=0.2)
        check("удаление названо вслух", "забыто: зовут Александр" in log_text(state), log_text(state)[-200:])
        check("факт убран, нумерация сдвинулась", memory.load_facts()[0] == ["любимый цвет — синий"], str(memory.load_facts()))
        await send("/forget" + ENTER, pause=0.2)
        check("без номера /forget объясняет, чего не хватает", "нужен номер" in log_text(state), log_text(state)[-200:])
        await send("/forget семь" + ENTER, pause=0.2)
        check("не-номер даёт внятное сообщение, а не падение", "не номер" in log_text(state), log_text(state)[-200:])
        await send("/forget 99" + ENTER, pause=0.2)
        check("промах по номеру объяснён", "нет факта с номером 99" in log_text(state), log_text(state)[-200:])

        # Собеседник, собранный конструктором состояния, обязан знать факты сразу — до
        # первой смены профиля. Проверка выше идёт после `/profile`, и одна она пропустила бы
        # потерю фактов у главного агента при запуске.
        свежее_состояние = cli.State(
            config=Config(api_key="sk-test", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        системное_свежего = свежее_состояние.main_agent.build_messages("привет")[0]["content"]
        check("собеседник главного экрана знает факты с самого запуска", "любимый цвет — синий" in системное_свежего, системное_свежего)

        видимые = [имя for имя, _, _ in ui.visible_commands(True)]
        check("все три команды памяти есть в меню", {"/remember", "/memory", "/forget"} <= set(видимые), str(видимые))
        справка = fragments_text(ui.help_fragments())
        check("и в справке", all(команда in справка for команда in ("/remember", "/memory", "/forget")), справка[:200])

        print("\n12c. Архивариус: раз в пять обменов, фоном")
        for путь in memory.facts_dir().glob("*.md"):
            путь.unlink()
        # Подставной клиент отвечает архивариусу тем же, чем всем: это не JSON со списком
        # фактов, поэтому сам по себе он памяти не пополняет. Здесь проверяется, КОГДА
        # архивариус заводится, а что он делает с ответом — в check_units.
        await send("/memory off" + ENTER, pause=0.2)
        check("выключатель сохранён в настройках инструмента", state.config.remember is False)
        state.since_archive = 0
        before = len(fake.calls)
        for номер in range(archivist.EVERY_N):
            await send(f"вопрос при выключенном сборе {номер}" + ENTER, pause=0.35)
        check("при выключенном сборе архивариус не заводится ни разу", state.archivist_task is None)
        check("и лишних запросов не делает", len(fake.calls) - before == archivist.EVERY_N, str(len(fake.calls) - before))

        await send("/memory on" + ENTER, pause=0.2)
        check("сбор включён обратно", state.config.remember is True)
        state.since_archive = 0
        before = len(fake.calls)
        for номер in range(archivist.EVERY_N - 1):
            await send(f"вопрос до срока {номер}" + ENTER, pause=0.35)
        check("до пятого обмена архивариус не заводится", state.archivist_task is None, str(state.since_archive))
        await send("пятый вопрос" + ENTER, pause=0.35)
        check("на пятом обмене заход заведён", state.archivist_task is not None)
        check("счётчик обнулён — следующий заход не раньше чем через пять", state.since_archive == 0, str(state.since_archive))
        # Ввод не блокируется: строка ввода принимает текст, пока архивариус ходит к модели.
        buffer.text = ""
        await send("набрано, пока архивариус работает")
        check("ввод при работающем архивариусе не заблокирован", buffer.text == "набрано, пока архивариус работает", repr(buffer.text))
        await send("\x7f" * 60)
        check("пока заход идёт, второго не заводим", not archivist.due(state))
        await asyncio.wait({state.archivist_task}, timeout=5)
        check("заход завершился", state.archivist_task.done())
        всего_запросов = len(fake.calls) - before
        check("на пять обменов пришёлся ровно один запрос архивариуса", всего_запросов == archivist.EVERY_N + 1, str(всего_запросов))
        запрос_архивариуса = fake.calls[-1]
        check(
            "архивариус получил кусок разговора со своей инструкцией",
            запрос_архивариуса["messages"][0]["content"] == archivist.ARCHIVIST_INSTRUCTION,
            запрос_архивариуса["messages"][0]["content"][:80],
        )
        check(
            "пока архивариус работал, в строке состояния было сказано",
            "запоминаю…" in fragments_text(ui.status_fragments("m", True, "talky", False, True, True)),
        )
        check(
            "когда не работает — не сказано",
            "запоминаю…" not in fragments_text(ui.status_fragments("m", True, "talky", False, True, False)),
        )
        print("\n13. Выход")
        await send("/exit" + ENTER)
        await asyncio.sleep(0.15)
        check("приложение завершилось", run.done())
        worker.cancel()
        with __import__("contextlib").suppress(asyncio.CancelledError):
            await worker


asyncio.run(main())
print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
