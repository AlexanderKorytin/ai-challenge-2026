"""Живой прогон дня 9: 15 ходов настоящим DeepSeek, без терминала.

Гоняет ТОТ ЖЕ код, что и интерфейс: настоящее приложение на трубе вместо клавиатуры и пустом
выводе вместо экрана. Отличие от показа одно — вопросы подаются программно, а не нажатием
Enter. Всё остальное настоящее: фоновый сжиматель, обрезка, выжимка в системной части, журнал.

Запуск: uv run прогон.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import tempfile
from pathlib import Path

from prompt_toolkit.input import create_pipe_input
from prompt_toolkit.application import create_app_session
from prompt_toolkit.output import DummyOutput

from myharness import api, cli, config, profiles

ПРОФИЛЬ = "сжатие"


def лента(state) -> list[str]:
    строки = []
    for кусок in state.main.first.log:
        if isinstance(кусок, list):
            строки.append("".join(т for _, т in кусок))
    return строки


async def главное() -> None:
    # Каталог состояния уводим во временный: без этого каждый прогон продолжает разговор
    # предыдущего, и числа выжимки складываются между прогонами — на пятнадцати ходах
    # получалась граница в двадцать пять пар. Проверять надо чистый лист.
    временный = Path(tempfile.mkdtemp(prefix="прогон-дня9-"))
    os.environ["MYHARNESS_STATE_DIR"] = str(временный)
    print("каталог состояния:", временный)

    наст = config.load()
    if not наст.is_authorized:
        sys.exit("ключ не задан: сперва /auth в самом инструменте")

    найденный, предупреждения = profiles.load(ПРОФИЛЬ)
    if найденный is None:
        sys.exit(f"профиль «{ПРОФИЛЬ}» не найден: {предупреждения}")
    for жалоба in предупреждения:
        print("профиль:", жалоба)

    state = cli.State(
        config=наст,
        client=api.DeepSeekClient(наст.api_key),
        model=наст.model,
        profile=найденный,
    )

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        app = cli.build_app(state)
        state.app = app
        # Хранилище разговора заводит именно эта строка, и без неё сжатие не работает вовсе:
        # заготовку принимает только агент, которому есть куда положить выжимку. Настоящий
        # запуск зовёт её в своей точке входа; собирая состояние руками, её легко пропустить —
        # что и вышло на трёх первых прогонах.
        cli.restore_conversation(state)
        worker = asyncio.create_task(cli.worker(state))
        run = asyncio.create_task(app.run_async())
        await asyncio.sleep(0.2)

        было_строк = len(лента(state))
        for номер, вопрос in enumerate(найденный.prefills, start=1):
            print(f"\n{'─' * 78}\nход {номер}: {вопрос[:70]}…", flush=True)
            pipe.send_text(вопрос + "\r")
            # Ждём, пока обмен закончится: пока идёт запрос, следующий вопрос слать
            # нельзя — он встал бы в очередь и перемешал ленту.
            await asyncio.sleep(0.5)
            for _ in range(600):
                if not state.busy:
                    break
                await asyncio.sleep(0.5)
            новые = лента(state)[было_строк:]
            было_строк = len(лента(state))
            for строка in новые:
                if строка.strip():
                    print("   ", строка[:110], flush=True)

        # Дать фоновому сжимателю доработать, если он ещё в пути.
        for _ in range(120):
            if not state.сжиматель.задача or state.сжиматель.задача.done():
                break
            await asyncio.sleep(0.5)

        выжимка = state.main_agent.выжимка()
        print(f"\n{'═' * 78}\nИТОГ\n")
        print("пар в памяти дословно:", len(state.main_agent.history()) // 2)
        print("пар прошло разговором:", state.main_agent.passed_pairs)
        if выжимка is None:
            print("ВЫЖИМКИ НЕТ — сжатие не сработало ни разу")
        else:
            print(f"выжимка: {len(выжимка.пункты)} пунктов, граница {выжимка.граница}, "
                  f"пересжатий {выжимка.поколение}, забыто дословно {выжимка.забыто_дословно}")
            print("\nвыжимка целиком:")
            for н, пункт in enumerate(выжимка.пункты, start=1):
                print(f"  {н}. {пункт}")

        print("\nответ на 15-м ходу:\n")
        история = state.main_agent.history()
        print(история[-1]["content"][:1500] if история else "память пуста")

        pipe.send_text("/exit\r")
        await asyncio.sleep(0.5)
        for задача in (run, worker):
            задача.cancel()


if __name__ == "__main__":
    asyncio.run(главное())
