"""Точка входа интерфейса: собрать приложение, поднять разговор и отдать управление окну.

Раньше этот файл был всем сразу — состоянием, очередями, командами, раскладкой и клавишами на
две с лишним тысячи строк. Теперь каждая из этих работ живёт в своём модуле, а здесь остались
только сборка и запуск: что во что складывается и в каком порядке случается при старте.

Порядок и есть содержание этого файла, и он не случаен. Приветствие печатается ДО подъёма
разговора, иначе строка «восстановлен разговор…» встала бы выше шапки и читалась бы так, будто
разговор подняли ещё до запуска. Клиент заводится после настроек, а закрывается после
исполнителей очередей — иначе Enter, нажатый перед самым выходом, ушёл бы в закрытый клиент.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from contextlib import suppress

from prompt_toolkit.application import Application
from prompt_toolkit.filters import Condition

from . import archivist, background, keys
from . import layout as layout_mod
from . import profiles, ui
from .api import DeepSeekClient
from .config import load as load_config
from .conversation import restore_conversation, поднять_слои
from .output import append_log
from .state import State
from .strategies import open_profile_surfaces
from .workers import close_pane_workers, worker


def build_app(state: State) -> Application:
    """Сложить приложение из раскладки и клавиш. Всё, что сложнее одной строки, живёт не здесь:
    окно собирает `layout`, клавиши привязывает `keys`."""

    раскладка = layout_mod.собрать_раскладку(state)
    kb = keys.привязки(state, раскладка)
    layout_mod.click_only_mouse(layout_mod.app_output())
    app = Application(
        layout=раскладка.layout,
        key_bindings=kb,
        style=ui.STYLE,
        full_screen=True,
        mouse_support=Condition(lambda: state.mouse_enabled),
        erase_when_done=True,
    )
    # Esc — начало alt-комбинаций и escape-последовательностей, поэтому prompt_toolkit
    # ждёт продолжения, прежде чем счесть клавишу самостоятельной. При стандартных
    # 0.5 и 1.0 с закрытие панели по Esc ощущается как залипание; сочетаний с Alt у нас
    # нет, поэтому ждать долго незачем.
    app.ttimeoutlen = 0.15
    app.timeoutlen = 0.3
    return app



def greet(state: State) -> None:
    """Шапка и подсказка про инструкцию. Вынесены из `repl`, потому что печатать их надо
    ДО восстановления разговора: строка «восстановлен разговор…», вылезшая выше приветствия,
    читается так, будто разговор подняли ещё до запуска инструмента."""
    append_log(state, ui.banner_fragments(state.model, state.config.is_authorized, state.profile.name))
    if state.profile.system:
        append_log(state, ui.system_fragments("профиль задаёт системную инструкцию — показать: /system"))


async def repl(state: State) -> None:
    app = build_app(state)
    state.app = app
    worker_task = asyncio.create_task(worker(state))
    try:
        await app.run_async()
    finally:
        # Сначала запрещаем новым обработчикам Enter пополнять очереди, затем одновременно
        # снимаем общий и панельные исполнители. Каждый из них дожидается уборки дочернего
        # `run_turn`, поэтому вращатели строк ожидания не переживают закрытие приложения.
        submissions = tuple(state.submission_tasks)
        for task in submissions:
            task.cancel()
        if submissions:
            await asyncio.gather(*submissions, return_exceptions=True)
        worker_task.cancel()
        await asyncio.gather(worker_task, close_pane_workers(state), return_exceptions=True)
        # Последний заход архивариуса — до закрытия клиента: без него всё, о чём говорили
        # после прошлого захода (до пяти обменов), в глобальную память не попало бы вовсе.
        await archivist.finish(state)
        # Идущий заход сжимателя, наоборот, снимаем и не дожидаемся: его итог — заготовка на
        # следующий обмен, а следующего обмена не будет. Ждать выхода ради неё значило бы
        # держать человека, уже сказавшего «выхожу», ради работы, которая никому не достанется.
        # Снять при этом обязательно: брошенная задача при закрытии цикла даёт предупреждение
        # «задача уничтожена, а она ещё работала» поверх прощального экрана.
        background.отменить(state.сжиматель)
        # Соединения MCP — до выхода из цикла событий: процесс местного сервера, не снятый
        # здесь, пережил бы терминал.
        await state.mcp.закрыть()
        if state.client:
            await state.client.aclose()


def silence_transport_noise(loop: asyncio.AbstractEventLoop) -> None:
    """Глушит одно конкретное сообщение httpcore2 2.12: при обрыве ответа по max_tokens
    тело остаётся недочитанным, и закрытие потока печатает «generator didn't stop after
    athrow()». Это шум чужой библиотеки, но в полноэкранном режиме он рвёт разметку экрана.
    Все прочие ошибки цикла обрабатываются как обычно."""
    default_handler = loop.get_exception_handler()

    def handler(target_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exception = context.get("exception")
        message = context.get("message", "")
        if isinstance(exception, RuntimeError) and "athrow" in str(exception):
            return
        if "closing of asynchronous generator" in message:
            return
        if default_handler is not None:
            default_handler(target_loop, context)
        else:
            target_loop.default_exception_handler(context)

    loop.set_exception_handler(handler)


async def _main(args: argparse.Namespace) -> None:
    """Поднять интерфейс на уже разобранных ключах.

    Ключи сюда приходят готовыми (их разбирает точка входа пакета) и здесь не разбираются
    заново: второй разбор — это второе место, где живут имена и значения по умолчанию, и
    расходятся такие места молча."""
    silence_transport_noise(asyncio.get_running_loop())
    cfg = load_config()
    profile_name = args.profile or os.environ.get("MYHARNESS_PROFILE") or cfg.profile
    profile, warnings = profiles.load(profile_name)
    if args.model:
        cfg.model = args.model
    client = DeepSeekClient(cfg.api_key) if cfg.is_authorized else None
    state = State(config=cfg, client=client, model=cfg.model, profile=profile)
    greet(state)
    for warning in warnings:
        append_log(state, ui.error_fragments(warning))
    # Прежний разговор поднимается ЗДЕСЬ: настройки и профиль уже прочитаны, агент уже есть,
    # ни один запрос ещё невозможен. В конструкторе состояния этому места нет — его зовут
    # проверки напрямую, и любое состояние начало бы читать и писать в каталог состояния.
    # Слои поднимаются ПЕРЕД разговором, а не после, ровно по одной причине: подъём разговора
    # решает, сколько пар влезет в порог, и считает при этом вес эфемерного хвоста — а хвост
    # берётся из активной задачи, которую и выбирает `поднять_слои`. Подними мы их после, вес
    # подъёма был бы занижен на длину хвоста, и первый же вопрос выбросил бы лишние пары.
    # (Карточка от порядка не зависит: её поставщик читает диск сам.)
    поднять_слои(state)
    restore_conversation(state)
    open_profile_surfaces(state, state.initial_strategy)
    await repl(state)


def main(args: argparse.Namespace) -> None:
    with suppress(KeyboardInterrupt):
        asyncio.run(_main(args))
