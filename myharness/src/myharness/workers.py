"""Очереди запросов и их исполнители: кто и в каком порядке ходит к модели.

Очередей две, и это не дублирование. Общая очередь состояния ведёт главный разговор, группу
агентов и набор способов — всё, что человек спрашивает с главного экрана. У неинтерактивной
панели своей очереди нет и не нужно, а вот у рабочего экрана со своей веткой разговора она
своя: без неё два вопроса, заданные на разных вкладках, встали бы друг за другом, хотя
разговоры у них разные.

Задачи обеих очередей живут в `state` (`current_task`, `pane_workers`, `submission_tasks`) —
там же, где всё прочее состояние сеанса. Здесь только их заведение, работа и остановка.
"""

from __future__ import annotations

import asyncio
from contextlib import suppress
from dataclasses import dataclass, field
from typing import Any

from . import archivist, compact, context_strategy, team, ui
from . import methods as methods_mod
from . import screens as screens_mod
from .output import append_log, deliver, run_turn
from .state import Request, State
@dataclass
class PaneWorker:
    """Последовательная очередь одной точной интерактивной панели.

    Исполнитель живёт до закрытия приложения. Его текущая задача отделена от общей задачи
    главной панели, поэтому отмена в одной панели не затрагивает соседнюю.
    """

    state: State
    pane: screens_mod.Pane
    queue: asyncio.Queue[Request] = field(default_factory=asyncio.Queue)
    runner: asyncio.Task[None] | None = None
    current_task: asyncio.Task[Any] | None = None
    busy: bool = False
    accepting: bool = True

    def start(self) -> None:
        if self.accepting and (self.runner is None or self.runner.done()):
            self.runner = asyncio.create_task(self.run())

    async def run(self) -> None:
        while True:
            request = await self.queue.get()
            self.busy = True
            assert self.pane.agent is not None
            task = asyncio.create_task(
                run_turn(self.state, self.pane.agent, request.content, pane=self.pane)
            )
            self.current_task = task
            try:
                await task
                self._ensure_facts_session_is_findable()
                # Сохраняем прежний побочный жизненный цикл общего исполнителя. Эти службы
                # сами решают, есть ли для них работа, и не задерживают следующую реплику.
                archivist.start(self.state)
                compact.start(self.state)
            except asyncio.CancelledError:
                # Отмена самого исполнителя означает закрытие приложения и обязана выйти
                # из цикла. Ctrl+C отменяет только дочерний обмен, поэтому у runner нет
                # собственного запроса на отмену и он продолжает брать очередь.
                current = asyncio.current_task()
                if current is not None and current.cancelling():
                    raise
                append_log(self.state, ui.system_fragments("запрос отменён"), self.pane)
            finally:
                self.current_task = None
                self.busy = False
                self.queue.task_done()

    def _ensure_facts_session_is_findable(self) -> None:
        """Связать записанный журнал фактов с обнаруживаемой линейной сессией.

        Извлечение может успеть успешно записать редакцию при отказе основного запроса.
        Тогда пары нет и SessionStore ещё не создал `.jsonl`; без этой отметки следующий
        запуск не нашёл бы лежащий рядом журнал фактов.
        """

        profile = self.pane.profile
        if (
            profile is None
            or profile.context_strategy != context_strategy.CONTEXT_FACTS
            or self.pane.agent is None
            or self.pane.agent.conversation_facts()[1] == 0
        ):
            return
        screen = next(
            (
                screen
                for screen in self.state.screens
                if any(candidate is self.pane for candidate in screen.panes)
            ),
            None,
        )
        store = screen.session_store if screen is not None else None
        if store is None or store.path.exists():
            return
        error = store.touch()
        if error:
            append_log(self.state, ui.error_fragments(error), self.pane)

    def enqueue(self, request: Request) -> bool:
        """Принять вопрос, пока экран не начал закрываться."""

        if not self.accepting:
            return False
        self.queue.put_nowait(request)
        return True

    async def drain_and_close(self) -> None:
        """Закрыть вход, закончить текущий обмен и очередь, затем остановить цикл."""

        self.accepting = False
        await self.queue.join()
        if self.runner is not None and not self.runner.done():
            self.runner.cancel()
        if self.runner is not None:
            with suppress(asyncio.CancelledError):
                await self.runner


    def cancel_active(self) -> None:
        if self.busy and self.current_task is not None:
            self.current_task.cancel()

    async def close(self) -> None:
        self.accepting = False
        if self.runner is not None and not self.runner.done():
            self.runner.cancel()
        if self.runner is not None:
            with suppress(asyncio.CancelledError):
                await self.runner
        # Оставшиеся единицы ещё не стали задачами обмена. На выходе приложения они
        # намеренно отбрасываются и отмечаются выполненными для целостного состояния очереди.
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()


def pane_worker(state: State, pane: screens_mod.Pane) -> PaneWorker:
    """Вернуть единственного исполнителя точного объекта панели."""
    key = id(pane)
    existing = state.pane_workers.get(key)
    if existing is not None and existing.pane is pane:
        existing.start()
        return existing
    created = PaneWorker(state=state, pane=pane)
    state.pane_workers[key] = created
    created.start()
    return created


async def close_pane_workers(state: State) -> None:
    """Остановить и дождаться всех исполнителей, которые ещё принадлежат приложению."""
    await asyncio.gather(*(item.close() for item in tuple(state.pane_workers.values())))


def _track_submission(state: State, task: asyncio.Task[None]) -> None:
    """Привязать короткую задачу Enter к жизненному циклу приложения."""
    state.submission_tasks.add(task)

    def finished(done: asyncio.Task[None]) -> None:
        state.submission_tasks.discard(done)
        if not done.cancelled():
            # Получаем возможное исключение, чтобы цикл не напечатал предупреждение о
            # необработанной фоновой задаче поверх интерфейса.
            done.exception()

    task.add_done_callback(finished)


async def worker(state: State) -> None:
    """Прежний единый исполнитель только главной панели и её оркестраторов."""
    while True:
        request = await state.queue.get()
        lead = request.lead or state.profile
        state.busy = True
        # Чем занят запрос, помним отдельно: по одному лишь возвращённому значению не
        # отличить итог оркестратора от обмена, который сам себя уже показал.
        kind = "обмен"
        if lead.methods:
            kind = "набор"
            task = asyncio.create_task(methods_mod.run_all(state, request.content, lead))
        elif lead.agents:
            kind = "группа"
            task = asyncio.create_task(team.run(state, request.content, lead))
        else:
            # Вопрос в память не дописываем: памятью владеет агент, и кладёт он туда только
            # отвеченную пару — иначе после сетевого сбоя в истории остался бы вопрос,
            # на который никто не отвечал.
            task = asyncio.create_task(run_turn(state, state.main_agent, request.content, pane=request.pane))
        state.current_task = task
        # Сколько пар лежало в главном разговоре ДО обмена. Считать обмены по их исходу
        # нельзя: обмен главного экрана, итог группы и итог набора способов кладут пару
        # каждый по-своему, а отменённый и упавший — не кладут вовсе. Рост памяти отвечает
        # на нужный вопрос прямо: разговору, который читает архивариус, прибыло.
        было_пар = len(state.main_agent.history())
        try:
            result = await task
            # Единственное место, где итог оркестратора попадает в главный экран. Сами
            # оркестраторы решают, ЧТО считать итогом, но не куда его девать: набор способов
            # состоит из тех же оркестраторов, и пиши каждый из них наверх сам — четыре
            # способа писали бы вперемешку и в случайном порядке.
            if kind == "набор":
                deliver(state, request.content, result)
            elif kind == "группа" and result is not None:
                deliver(state, request.content, [result])
            if len(state.main_agent.history()) > было_пар:
                state.since_archive += 1
            # Заход заводится ОТДЕЛЬНОЙ задачей и никем не ожидается: архивариус не имеет
            # права задержать ни следующий запрос из очереди, ни ввод человека.
            archivist.start(state)
            # Сжиматель — там же и по той же причине. Заводится он не по счёту обменов, а по
            # подходу к ближайшему ограничителю обрезки: своей мерки «пора» у сжатия нет, оно
            # подбирает то, что решила выбросить обрезка.
            compact.start(state)
        except asyncio.CancelledError:
            current = asyncio.current_task()
            if current is not None and current.cancelling():
                raise
            append_log(state, ui.system_fragments("запрос отменён"), request.pane)
        finally:
            state.current_task = None
            state.busy = False
            state.queue.task_done()
