"""Полноэкранное приложение целиком: клавиши, меню, панель параметров, ответ модели.

Настоящий терминал не нужен: prompt_toolkit умеет работать на трубе и пустом выводе,
а вместо DeepSeek подставлен поддельный клиент — проверки ничего не стоят и не жгут ключ.
"""

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
(tmp / "profiles").mkdir(parents=True)

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.key_binding.key_processor import KeyPress  # noqa: E402
from prompt_toolkit.keys import Keys  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.layout.containers import Window  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from prompt_toolkit.data_structures import Point  # noqa: E402
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType  # noqa: E402

from myharness import api, cli, profiles, ui  # noqa: E402
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


def log_text(state, screen=None):
    return fragments_text((screen or state.main).log)


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
        config=Config(api_key="sk-test", model="deepseek-v4-flash"),
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
        check("в списке команды авторизованного, без /auth", count == 9, f"их {count}")

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
        check("экраны агентов заведены", [s.key for s in state.screens] == ["main", "analyst", "critic"], str([s.key for s in state.screens]))
        analyst_screen = state.screens[1]
        check("в экране агента его постановка задачи", "агент «analyst»" in log_text(state, analyst_screen) and "ты аналитик" in log_text(state, analyst_screen))
        check("в экране агента его рассуждения и ответ", "прикидываю…" in log_text(state, analyst_screen) and '"status": "ok"' in log_text(state, analyst_screen))
        # в главном экране — только сводка ведущего; ленты агентов остаются на своих вкладках
        check("вывод агентов в главный экран не льётся", "агент «analyst»" not in log_text(state))
        check("в главном экране сказано, где смотреть", "группа поднята: analyst, critic" in log_text(state))
        check("сводка ведущего пришла в главный экран", "свожу ответы агентов (2)" in log_text(state))

        agent_calls = fake.calls[before:]
        systems = [c["messages"][0]["content"] for c in agent_calls]
        check("каждому агенту ушла своя инструкция", "ты аналитик" in systems and "ты критик" in systems, str(systems))
        check("агенты не видели ответов друг друга", all(len(c["messages"]) == 2 for c in agent_calls[:2]))
        summary_call = agent_calls[-1]
        check("ведущему ушли ответы всех агентов", summary_call["messages"][0]["content"] == "сведи ответы" and "Ответ эксперта «critic»" in summary_call["messages"][1]["content"])

        check("полоса вкладок появилась", any("2 analyst" in text for text in screen_texts(app)))
        tabs = ui.tabs_fragments([(s.title, s.status) for s in state.screens], state.active, lambda i: cli.switch_screen(state, i))
        handler = next(f[2] for f in tabs if len(f) == 3 and "analyst" in f[1])
        handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        check("клик по вкладке открывает экран агента", state.active == 1 and "агент «analyst»" in fragments_text(app.layout.container.content.children[0].content.text()))
        cli.switch_screen(state, 0)
        check("возврат на главный экран", state.active == 0)

        records = [json.loads(line) for line in Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").splitlines()]
        team_records = [r for r in records if r.get("run_id")]
        check("в журнале отмечено, кто отвечал", {r.get("agent") for r in team_records} == {"analyst", "critic", "lead"}, str([r.get("agent") for r in team_records]))
        check("вся группа помечена одним прогоном", len({r["run_id"] for r in team_records}) == 1)

        print("\n8. Выход")
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
