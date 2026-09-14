"""Слои памяти в ЖИВОМ приложении: те же нажатия, что делает человек.

Третий файл проверок рядом с `check_units` и `check_app`, и заведён он не для порядка. Обе
прежние проверки собирают состояние руками — они зовут `State(...)` и функции напрямую, минуя
настоящую точку входа. Из-за этого в дне 9 по построению нельзя было поймать беду, где
хранилище разговора просто не заводилось: код был верен, а звать его было некому.

Здесь наоборот: поднимается `cli.repl`, ввод идёт трубой, вывод — пустым терминалом, клиент
подставной. Проверяется то, что видит человек: команды слоёв, их вывод в ленте и то, что
уходит в модель.

Запуск: uv run python tests/check_layers.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["MYHARNESS_PROFILES"] = str(tmp / "profiles")
os.environ["MYHARNESS_CONFIG_DIR"] = str(tmp / "config")
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")
(tmp / "profiles").mkdir(parents=True)
папка = tmp / "проект"
папка.mkdir()
os.chdir(папка)

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from myharness import api, cli, profiles, project_card, state as state_mod, workspace  # noqa: E402
from myharness.config import Config  # noqa: E402

ENTER, ESC, BACKSPACE = "\r", "\x1b", "\x7f"
сбои = []


def check(имя, условие, подробность=""):
    print(f"  [{'OK ' if условие else 'СБОЙ'}] {имя}" + (f" — {подробность}" if подробность and not условие else ""))
    if not условие:
        сбои.append(имя)


class FakeClient:
    def __init__(self):
        self.calls = []

    async def stream_chat(self, model, messages, params=None):
        self.calls.append({"model": model, "messages": messages, "params": dict(params or {})})
        yield api.StreamEvent("content", "ответ модели")
        yield api.StreamEvent("meta", finish_reason="stop", usage={"prompt_tokens": 5, "completion_tokens": 2})

    async def list_models(self):
        return list(api.FALLBACK_MODELS)

    async def aclose(self):
        pass


def лента(state):
    return "".join(ф[1] for ф in state.main.first.log)


async def main():
    fake = FakeClient()
    state = state_mod.State(
        config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
        client=fake,
        model="deepseek-v4-flash",
        profile=profiles.builtin_default(),
    )
    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        run = asyncio.create_task(cli.repl(state))
        while state.app is None:
            await asyncio.sleep(0)
        app = state.app
        await asyncio.sleep(0.1)

        async def send(текст, пауза=0.12):
            pipe.send_text(текст)
            await asyncio.sleep(пауза)

        print("\nШаг 6: /task ведёт рабочую память")
        await send("/task" + ENTER)
        check("без задачи сказано, что её нет", "нет рабочей задачи" in лента(state), лента(state)[-200:])
        await send("/task new разделение cli.py" + ENTER)
        check("задача заведена и названа", "рабочая задача: «разделение cli.py»" in лента(state), лента(state)[-300:])
        check("активная задача попала в состояние сеанса", state.слаг_задачи is not None)
        await send("/task план вынести состояние сеанса" + ENTER)
        await send("/task сейчас шаг 2: вынести команды памяти" + ENTER)
        await send("/task этап execution" + ENTER)
        поднятая = workspace.load_task(папка, state.слаг_задачи)[0]
        check("этап приведён к верхнему регистру", поднятая.этап == "EXECUTION", поднятая.этап)
        await send("/task" + ENTER)
        показ = лента(state)[-600:]
        check("задача показана целиком", "План" in показ and "вынести состояние сеанса" in показ, показ)
        check("и путь файла назван", ".md" in показ, показ)
        await send("/task ерунда текст" + ENTER)
        check("неизвестное слово команды названо", "не понимаю «ерунда»" in лента(state), лента(state)[-200:])
        await send("/task open нет такой задачи" + ENTER)
        check("несуществующая задача не молчит", "нет задачи" in лента(state), лента(state)[-200:])

        # Главный дефект, найденный просмотром: команда писала из устаревшего снимка и
        # затирала правку, сделанную человеком в файле руками. Проверяем именно это.
        файл_задачи = workspace.task_path(папка, state.слаг_задачи)
        файл_задачи.write_text(
            файл_задачи.read_text(encoding="utf-8").replace("этап: EXECUTION", "этап: EXECUTION\nприоритет: высокий")
            + "\n## Мои вопросы\n- а не переписать ли всё\n",
            encoding="utf-8",
        )
        await send("/task находка cli.py — 2923 строки" + ENTER)
        после = файл_задачи.read_text(encoding="utf-8")
        check("правка человека в заголовке пережила команду", "приоритет: высокий" in после, после)
        check("и чужой раздел тоже", "## Мои вопросы" in после and "а не переписать ли всё" in после, после)
        check("а сама находка записана", "cli.py — 2923 строки" in после, после)
        await send("/task new разделение cli.py" + ENTER)
        повтор = лента(state)[-700:]
        check("повторный /task new говорит, что продолжает прежнюю", "уже заведена" in повтор, повтор)
        check("и показывает её целиком", "вынести состояние сеанса" in повтор, повтор)

        print("\nШаг 7: /project ведёт карточку")
        await send("/project" + ENTER)
        check("карточки нет — сказано и названа команда", "/project new" in лента(state), лента(state)[-200:])
        await send("/project Стек Python, зависимости через uv" + ENTER)
        check("первая строка завела карточку", "карточка проекта заведена" in лента(state), лента(state)[-200:])
        await send("/project Где что лежит код в myharness/src" + ENTER)
        check("имя раздела из нескольких слов распознано", "где что лежит:" in лента(state), лента(state)[-200:])
        await send("/project Ограничения ключ API не покидает настроек" + ENTER)
        await send("/project" + ENTER)
        карточка_в_ленте = лента(state)[-700:]
        check("карточка показана целиком", "Стек" in карточка_в_ленте and "Где что лежит" in карточка_в_ленте, карточка_в_ленте)
        await send("/project Погода солнечно" + ENTER)
        check("неизвестный раздел назван", "не понимаю" in лента(state), лента(state)[-200:])

        print("\nПодсказки по пробелу")
        буфер = app.layout.get_buffer_by_name("text-area") or app.current_buffer
        await send("/task ")
        варианты = [c.text for c in буфер.complete_state.completions] if буфер.complete_state else []
        check("после /task и пробела предложены слова команды", {"new", "план", "забыть", "done"} <= set(варианты), str(варианты))
        await send("забыть ")
        варианты = [c.text for c in буфер.complete_state.completions] if буфер.complete_state else []
        check("после /task забыть предложены разделы", "план" in варианты and "находки" in варианты, str(варианты))
        await send("план ")
        подсказки_строк = (
            [(c.text, c.display_meta_text) for c in буфер.complete_state.completions]
            if буфер.complete_state
            else []
        )
        check(
            "а после раздела — номера строк вместе с их текстом",
            подсказки_строк and подсказки_строк[0][0] == "1" and "вынести состояние сеанса" in подсказки_строк[0][1],
            str(подсказки_строк),
        )
        await send(ESC)
        await send(BACKSPACE * 40)  # очищаем строку ввода нажатиями, а не в обход окна
        check("строка ввода очищена нажатиями", буфер.text == "", repr(буфер.text))
        await send("/project ")
        варианты = [c.text for c in буфер.complete_state.completions] if буфер.complete_state else []
        check(
            "после /project предложены и слова, и разделы карточки",
            {"new", "забыть"} <= set(варианты) and "Где что лежит" in варианты,
            str(варианты),
        )
        await send(ESC)
        await send(BACKSPACE * 40)

        print("\nУдаление из слоёв")
        await send("/task план вторая строка плана" + ENTER)
        await send("/task" + ENTER)
        нумерация = лента(state)[-800:]
        check("строки разделов пронумерованы", "1. вынести состояние сеанса" in нумерация and "2. вторая строка плана" in нумерация, нумерация)
        await send("/task забыть план 2" + ENTER)
        поднятая = workspace.load_task(папка, state.слаг_задачи)[0]
        check("строка убрана из задачи по номеру", поднятая.пункты("План") == ["вынести состояние сеанса"], str(поднятая.пункты("План")))
        await send("/task забыть план 7" + ENTER)
        check("промах по номеру назван", "нет строки 7" in лента(state), лента(state)[-200:])
        # Раздел называется тем же словом, каким в него пишут: писали «/task находка», значит
        # и убирать должно «/task забыть находка».
        await send("/task находка cli.py — 80 определений" + ENTER)
        до_удаления_находок = workspace.load_task(папка, state.слаг_задачи)[0].пункты("Находки")
        await send("/task забыть находка 1" + ENTER)
        поднятая = workspace.load_task(папка, state.слаг_задачи)[0]
        check(
            "раздел убирается тем же словом, каким в него пишут",
            поднятая.пункты("Находки") == до_удаления_находок[1:]
            and "убрано из задачи" in лента(state),
            f"было {до_удаления_находок}, стало {поднятая.пункты('Находки')}",
        )
        await send("/task забыть план ²" + ENTER)
        check(
            "непригодный номер отвечает подсказкой, а не тишиной",
            "нужен раздел и номер строки" in лента(state),
            лента(state)[-200:],
        )
        await send("/project забыть Стек 1" + ENTER)
        карточка_после = project_card.load(папка)[0]
        check("строка убрана из карточки по номеру", "Python, зависимости через uv" not in str(карточка_после.разделы), str(карточка_после.разделы))
        до_удаления = project_card.load(папка)[0].текст()
        await send("/project забыть карточку" + ENTER)
        check("карточка убрана целиком", project_card.load(папка)[0] is None, str(project_card.load(папка)))
        копия = project_card.прежний_путь(папка)
        check(
            "а рядом лежит копия с тем же содержимым",
            копия.exists() and копия.read_text(encoding="utf-8") == до_удаления,
            копия.read_text(encoding="utf-8") if копия.exists() else "копии нет",
        )
        check("и путь копии назван человеку", str(копия) in лента(state), лента(state)[-300:])
        check("а сама карточка напечатана перед удалением", "Где что лежит" in лента(state)[-900:], лента(state)[-900:])
        await send("/project Стек Python и uv" + ENTER)
        await send("/project Где что лежит код в myharness/src" + ENTER)
        await send("/project Ограничения ключ API не покидает настроек" + ENTER)

        print("\nШаг 9: /memory показывает слои, /system печатает точный текст")
        await send("/memory" + ENTER)
        слои = лента(state)[-900:]
        check("показаны все четыре слоя", all(
            слово in слои for слово in ("разговор", "рабочее", "проект", "о человеке")
        ), слои)
        await send("/remember зовут Александр" + ENTER)
        await send("/memory" + ENTER)
        память = лента(state)[-900:]
        check("записи пронумерованы под слоями", "1. зовут Александр" in память, память)

        await send("/system" + ENTER)
        напечатанное = лента(state)[-1600:]
        check("системная часть напечатана", "системная часть запроса" in напечатанное, напечатанное[:200])
        check("в ней записи о человеке", "зовут Александр" in напечатанное, напечатанное[-400:])
        check("в ней карточка проекта", "Карточка проекта" in напечатанное, напечатанное[-600:])
        check("хвост напечатан отдельно", "хвост запроса" in напечатанное and "рабочее-состояние" in напечатанное, напечатанное[-600:])

        # Точность: напечатанное обязано совпасть с тем, что реально уходит в модель.
        до = len(fake.calls)
        await send("вопрос модели" + ENTER, пауза=0.4)
        ушедшее = fake.calls[до]["messages"]
        системное = ушедшее[0]["content"]
        последнее = ушедшее[-1]["content"]
        check("системная часть /system совпала с ушедшей в модель", системное in напечатанное.replace("  ", ""), системное[:120])
        check("хвост ушёл перед вопросом", последнее.startswith("<рабочее-состояние>") and последнее.endswith("вопрос модели"), последнее[:80])
        check("в памяти агента лежит вопрос без хвоста", all(
            "<рабочее-состояние>" not in сообщение["content"] for сообщение in state.main_agent.history()
        ), str(state.main_agent.history()))

        print("\nЗакрытие задачи")
        await send("/task done" + ENTER)
        хвост_ленты = лента(state)[-700:]
        check("перед закрытием задача показана целиком", "вынести состояние сеанса" in хвост_ленты, хвост_ленты)
        check("сказано про вынос в записи", "/remember" in хвост_ленты, хвост_ленты)
        check("активной задачи больше нет", state.слаг_задачи is None)
        check("файла рабочей памяти нет", not list((workspace.work_dir(папка)).glob("*.md")), str(list(workspace.work_dir(папка).glob("*.md"))))
        check("а карточка и записи целы", project_card.load(папка)[0] is not None)

        await send("/exit" + ENTER, пауза=0.3)
        await asyncio.wait_for(run, timeout=5)

    print()
    if сбои:
        print(f"ПРОВАЛЕНО: {len(сбои)} — " + "; ".join(сбои))
        return 1
    print("Сценарий пройден целиком")
    return 0


sys.exit(asyncio.run(main()))
