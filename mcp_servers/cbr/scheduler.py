"""Планировщик сервера `cbr`: задания сбора курсов по выражению cron.

Цикл живёт в процессе службы рядом с сервером MCP (как `@Scheduled` внутри того же
приложения Spring) и спит до ближайшего срока либо до `wake()` — своего шага опроса нет.

Пропущенные сроки не догоняются поштучно, как у `Persistent=true` в systemd и у `anacron`:
после запуска срок считается от ТЕКУЩЕГО времени, так что после простоя службы задание
выполняется один раз. Сбор идемпотентен (см. `store.py`), лишний запуск ничего не портит.

Откуда берутся курсы, планировщик не знает: получение передаёт сервер (`fetch`). Импортировать
`server` отсюда нельзя — служба запускает его как `__main__`, и импорт поднял бы вторую копию
модуля.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from collections.abc import Awaitable, Callable

from croniter import croniter

from store import Job, Observed, Store

# Получение курсов на дату: все валюты одного ответа ЦБ с датой, на которую ЦБ их установил.
Fetch = Callable[[dt.date], Awaitable[list[Observed]]]


def next_run(cron: str, after: dt.datetime) -> dt.datetime:
    """Ближайший срок строго после `after`, в поясе `after`. Негодное выражение — `ValueError`."""
    # Ровно пять полей, как у CronCreate в Claude Code: croniter принял бы и шестое поле —
    # секунды («* * * * * *» ходило бы к ЦБ раз в секунду), и сокращения вида «@reboot».
    if len(cron.split()) != 5 or not croniter.is_valid(cron):
        raise ValueError(f"негодное выражение cron {cron!r}: ожидается пять полей, например */30 * * * *")
    return croniter(cron, after).get_next(dt.datetime)


async def run_job(store: Store, job: Job, now: dt.datetime, fetch: Fetch) -> None:
    """Один запуск задания. Исключений не выпускает: любой сбой — запуск с текстом ошибки.

    Запрос — на ЗАВТРА по часам `now`: ЦБ отдаёт последний установленный курс, а после
    публикации (около 15:30 по Москве) это уже курс на завтра. Одним ответом приходят все
    валюты, поэтому запрос один на запуск, сколько бы валют ни было в задании.
    """
    try:
        курсы = {к.code: к for к in await fetch(now.date() + dt.timedelta(days=1))}
        нужные = [курсы[код] for код in job.currencies if код in курсы]
        новых = store.add_rates(нужные, now, job.id)
        нет = [код for код in job.currencies if код not in курсы]
        ошибка = f"ЦБ не дал курса {', '.join(нет)}" if нет else None
    except Exception as exc:  # noqa: BLE001 — сбой запуска не должен остановить цикл
        новых, ошибка = 0, str(exc) or type(exc).__name__
    store.record_run(job.id, now, ошибка, новых)


class Scheduler:
    def __init__(self, store: Store, clock: Callable[[], dt.datetime], fetch: Fetch) -> None:
        self._store = store
        self._clock = clock
        self._fetch = fetch
        self._событие = asyncio.Event()

    def wake(self) -> None:
        """Задания изменились — пересчитать, сколько спать."""
        self._событие.set()

    async def tick(self) -> None:
        """Выполнить все задания со сроком не позже текущего и сдвинуть их сроки."""
        for задание in self._store.jobs():
            сейчас = self._clock()
            if задание.next_run > сейчас:
                continue
            await run_job(self._store, задание, сейчас, self._fetch)
            # Задание могли снять, пока шёл запрос к ЦБ, — тогда сдвигать нечего: UPDATE
            # по несуществующему id ничего не делает.
            self._store.set_next_run(задание.id, next_run(задание.cron, self._clock()))

    async def run_forever(self) -> None:
        while True:
            # Сброс ДО прохода: `wake()` посреди прохода (задание завели, пока шёл запрос к ЦБ)
            # не должен потеряться.
            self._событие.clear()
            await self.tick()
            сроки = [з.next_run for з in self._store.jobs()]
            ждать = (min(сроки) - self._clock()).total_seconds() if сроки else None
            try:
                await asyncio.wait_for(self._событие.wait(), timeout=max(ждать, 0) if ждать is not None else None)
            except TimeoutError:
                pass
