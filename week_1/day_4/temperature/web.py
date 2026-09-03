"""Локальная страница прогона: один вопрос при трёх температурах, ответы по мере готовности.

Приложение делает три вещи: отдаёт страницу, отдаёт список вопросов и ведёт поток событий
прогона. Вся расчётная часть лежит в соседних модулях — здесь только связь между прогоном
(`temperature.grid`) и браузером.

Слушает только `127.0.0.1`: наружу этот сервис не отдаётся, порт в межсетевом экране не
открывается.

    uv run python -m temperature.web
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, StreamingResponse
from myharness import api
from myharness.config import load as load_config

from temperature import grid
from temperature.questions import QUESTIONS, REPEATS, TEMPERATURES, by_id

# --- настройки приложения -----------------------------------------------------

HOST = "127.0.0.1"
PORT = 8767

#: Разметка страницы. Путь берётся от файла модуля, а не от рабочего каталога: страницу
#: надо находить и при запуске из песочницы `test/`, откуда идут живые прогоны.
PAGE_PATH = Path(__file__).with_name("page.html")

НЕТ_КЛЮЧА = "ключ не задан: запустите myharness и выполните /auth"

ЗАГЛУШКА_СТРАНИЦЫ = """<!doctype html>
<meta charset="utf-8">
<title>Температура</title>
<h1>страница не прочиталась</h1>
<p>Ожидается файл <code>temperature/page.html</code> рядом с модулем.
Проверьте, на месте ли он и хватает ли прав на чтение.</p>
"""

#: Метка конца потока результатов. Отдельный объект, а не `None`: `None` — законное
#: значение внутри результата, и спутать их значило бы оборвать поток на полуслове.
КОНЕЦ = object()


# --- состояние приложения -----------------------------------------------------


class Состояние:
    """Клиент API и нумерация прогонов — то немногое, что живёт между запросами.

    Нумерация держится в памяти, а не вычитывается из журнала на каждое нажатие: пока
    прогон идёт, журнал ещё дописывается, и чтение посреди прогона выдало бы номера,
    которые уже заняты клетками в работе.
    """

    def __init__(self) -> None:
        self.клиент: api.DeepSeekClient | None = None
        # От ключа хранится только отпечаток: по нему видно, что ключ сменился (человек
        # выполнил `/auth`, пока сервер поднят), а сам секрет в памяти приложения не лежит.
        self.отпечаток_ключа: str | None = None
        self.замок = asyncio.Lock()
        self.собрано: dict[str, int] = {}

    def прочитать_собранное(self) -> None:
        """Наибольшие занятые номера прогонов из журнала — начальное значение нумерации."""
        self.собрано = grid.collected_repeats()

    def занять(self, question_id: str, count: int) -> int:
        """Отводит `count` подряд идущих номеров прогонов и возвращает первый из них.

        Номера отводятся до начала прогона, а не по его окончании: два одновременных
        нажатия кнопки не должны получить один и тот же номер. Отведённые номера не
        возвращаются даже при сбое — провалившаяся клетка тоже занимает своё место в
        журнале, и повторное использование её номера смешало бы две разные попытки.
        """
        начало = self.собрано.get(question_id, 0) + 1
        self.собрано[question_id] = начало + count - 1
        return начало

    async def клиент_для(self, api_key: str) -> api.DeepSeekClient:
        """Клиент API — один на приложение, заводится при первом обращении.

        Новый клиент на каждый запрос означал бы новый пул соединений на каждое нажатие
        кнопки; при девяти клетках подряд это заметно дороже одного общего.
        """
        отпечаток = hashlib.sha256(api_key.encode("utf-8")).hexdigest()
        async with self.замок:
            if self.клиент is None or self.отпечаток_ключа != отпечаток:
                await self.закрыть_клиента()
                self.клиент = api.DeepSeekClient(api_key)
                self.отпечаток_ключа = отпечаток
            return self.клиент

    async def закрыть_клиента(self) -> None:
        if self.клиент is not None:
            await self.клиент.aclose()
            self.клиент = None
            self.отпечаток_ключа = None


состояние = Состояние()


# --- события потока -----------------------------------------------------------


def кадр(данные: dict[str, Any]) -> str:
    """Одно событие потока сервера: `data: <json>` и пустая строка как разделитель.

    Русский текст идёт как есть (`ensure_ascii=False`), а переносы строк внутри ответа
    модели JSON экранирует сам — событие остаётся однострочным, как того требует формат.
    """
    return "data: " + json.dumps(данные, ensure_ascii=False) + "\n\n"


def событие_клетки(итог: grid.CellResult) -> dict[str, Any]:
    """Готовая клетка в виде события. Ключа доступа здесь нет и быть не может."""
    return {
        "kind": "cell",
        "temperature": итог.temperature,
        "repetition": итог.repetition,
        "value": итог.value,
        "answer": итог.answer,
        "usage": итог.usage,
        "elapsed_ms": итог.elapsed_ms,
        "finish_reason": итог.finish_reason,
        "status": итог.status,
        "error": итог.error,
    }


def событие_ошибки(сообщение: str) -> dict[str, Any]:
    return {"kind": "error", "message": сообщение}


def описание_сбоя(исключение: BaseException) -> str:
    return f"{type(исключение).__name__}: {исключение}"


# --- поток прогона ------------------------------------------------------------


async def поток_прогона(question_id: str) -> AsyncIterator[str]:
    """События одного нажатия кнопки: клетка за клеткой, затем `done` или `error`.

    Связь устроена через `asyncio.Queue`. `grid.run_question` зовёт `on_result` как
    обычную функцию из своего асинхронного кода — отдать оттуда значение наружу нельзя,
    а положить в очередь можно: `put_nowait` у неограниченной очереди не ждёт и не
    роняет вызывающего. Генератор потока разбирает очередь и отдаёт события браузеру по
    одному. Без очереди пришлось бы дождаться конца `run_question` и вывалить все девять
    ответов разом — на странице это выглядело бы как «ничего не происходит, потом всё
    сразу».

    Конец прогона отмечает `add_done_callback`: он кладёт в ту же очередь метку `КОНЕЦ`.
    Очередь соблюдает порядок, поэтому метка приходит после всех результатов — ни один
    из них не теряется.
    """
    try:
        вопрос = by_id(question_id)
    except KeyError:
        yield кадр(событие_ошибки(f"неизвестный вопрос: {question_id}"))
        return

    # Настройки читаются на каждый прогон: человек мог выполнить `/auth` уже после того,
    # как сервер был поднят.
    настройки = load_config()
    if not настройки.is_authorized:
        yield кадр(событие_ошибки(НЕТ_КЛЮЧА))
        return

    try:
        клиент = await состояние.клиент_для(настройки.api_key)
    except Exception as исключение:  # noqa: BLE001 — сбой отдаём событием, а не пятисотым ответом
        yield кадр(событие_ошибки(описание_сбоя(исключение)))
        return

    начало = состояние.занять(question_id, REPEATS)
    очередь: asyncio.Queue[Any] = asyncio.Queue()
    задача = asyncio.create_task(
        grid.run_question(клиент, вопрос, начало, REPEATS, очередь.put_nowait)
    )
    задача.add_done_callback(lambda _: очередь.put_nowait(КОНЕЦ))

    try:
        while True:
            элемент = await очередь.get()
            if элемент is КОНЕЦ:
                break
            yield кадр(событие_клетки(элемент))

        if задача.cancelled():
            yield кадр(событие_ошибки("прогон прерван"))
        elif (сбой := задача.exception()) is not None:
            yield кадр(событие_ошибки(описание_сбоя(сбой)))
        else:
            yield кадр({"kind": "done", "collected": состояние.собрано.get(question_id, 0)})
    finally:
        # Браузер закрыли или страницу обновили — генератор закрывают на полуслове.
        # Прогон в этом случае снимаем: платить за ответы, которых никто не увидит, незачем.
        if not задача.done():
            задача.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await задача


# --- приложение ---------------------------------------------------------------


@asynccontextmanager
async def жизненный_цикл(app: FastAPI) -> AsyncIterator[None]:
    """Нумерация прогонов читается на запуске, клиент API закрывается на остановке."""
    состояние.прочитать_собранное()
    try:
        yield
    finally:
        await состояние.закрыть_клиента()


app = FastAPI(title="Температура", lifespan=жизненный_цикл)


# HEAD перечислен рядом с GET намеренно: FastAPI, в отличие от Starlette, сам его не
# добавляет, и проверка вида `curl -sI` получала бы 405 вместо ответа страницы.
@app.api_route("/", methods=["GET", "HEAD"], response_class=HTMLResponse)
def страница() -> HTMLResponse:
    """Разметка страницы, читается с диска на каждый запрос.

    Именно на каждый, а не один раз на запуске: правка разметки видна по обновлению
    страницы, без перезапуска сервера. Страница локальная и маленькая — чтение файла
    здесь дешевле неудобства.
    """
    try:
        текст = PAGE_PATH.read_text(encoding="utf-8")
    except OSError:
        return HTMLResponse(ЗАГЛУШКА_СТРАНИЦЫ)
    return HTMLResponse(текст)


@app.get("/questions")
def вопросы() -> list[dict[str, str]]:
    """Набор вопросов для переключателей и для показа текста выбранного вопроса.

    Вид ответа (`answer_kind`) идёт вместе с вопросом, потому что без него страница не
    различит два разных смысла пустого `value` в событии клетки: «у этого вопроса
    сравнимого значения не бывает» (метафора, код) и «значение ждали, но разбор его не
    нашёл». Второе — показатель послушности разметке: при температуре 1.2 модель первой
    ломает требование «строго одна строка», и такая клетка должна бросаться в глаза, а
    не выглядеть нормой.
    """
    return [
        {"id": в.id, "title": в.title, "text": в.text, "answer_kind": в.answer_kind}
        for в in QUESTIONS
    ]


@app.get("/settings")
def настройки() -> dict:
    """Температуры и число прогонов — оттуда же, откуда их берёт прогон.

    Страница рисует столбцы по этому ряду, а не по своему списку. Иначе правка
    `TEMPERATURES` в `questions.py` оставила бы на странице старые столбцы, и каждая
    клетка молча падала бы в прочерк — отказ без единого сообщения.
    """
    return {"temperatures": list(TEMPERATURES), "repeats": REPEATS}


@app.get("/run")
def прогон(question: str) -> StreamingResponse:
    """Поток событий прогона: `REPEATS` повторов на каждую из трёх температур."""
    return StreamingResponse(
        поток_прогона(question),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            # Просьба к возможному посреднику не копить ответ в буфере: смысл всей
            # затеи в том, чтобы клетки заполнялись по одной.
            "X-Accel-Buffering": "no",
        },
    )


def main() -> None:
    print(f"страница прогона: http://{HOST}:{PORT}  (Ctrl+C — остановить)")
    uvicorn.run(app, host=HOST, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
