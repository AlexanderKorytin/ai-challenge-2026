"""Сущность-собеседник: имя, профиль, память разговора и правила сборки запроса.

Здесь живёт вся логика разговора с моделью, отделённая от того, кто её показывает.
Модуль намеренно не знает ни про терминал, ни про панели: ему разрешены только `api`,
`journal`, `profiles` и стандартная библиотека. Поэтому агента можно проверить без
терминала, без окон и без единого нажатия клавиши.

Наружу агент отдаёт только результат — запись `Turn`. Внутренняя память остаётся
внутренней: её выдают копией, а пополняют единственным методом `remember`.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from . import api, journal

if TYPE_CHECKING:  # только подсказка типов — на выполнении профиль сюда не импортируется
    # Умолчание окна памяти живёт здесь, рядом с правилом обрезки, а поле профиля берёт его
    # отсюда — значение одно на весь harness. Значит, `profiles` импортирует `agent`, и
    # обычный импорт `Profile` замкнул бы круг. Замыкать его нечем: `Profile` нужен агенту
    # только как аннотация, а `from __future__ import annotations` делает аннотации строками.
    from .profiles import Profile

# Сколько пар «вопрос — ответ» держим по умолчанию. Десять пар — это обмены примерно на
# полчаса разговора: свежую инструкцию модель ещё слышит, а формат и тему из обменов
# получасовой давности уже не тянет. Дрейф на длинной истории заметен обменов с тридцати.
DEFAULT_WINDOW_PAIRS = 10

# Запас сверх окна: режем не на одиннадцатой паре, а на пятнадцатой — и сразу блоком в пять.
# Сдвигай окно каждый ход по одной паре — и начало запроса менялось бы каждый ход, обнуляя
# повторное использование неизменного начала на стороне сервера. Блоком полную цену платим
# раз в пять ходов вместо каждого.
WINDOW_SLACK_PAIRS = 5

# Аварийный потолок объёма памяти в знаках. Окно по числу пар не спасает от единственного
# случая: пользователь вставил в вопрос файл, и одна пара весит больше всего окна контекста.
# 60 000 знаков — это порядка 20 000 токенов, около трети окна: остаётся место и на
# системную инструкцию, и на новый вопрос, и на сам ответ.
HISTORY_CHARS_MAX = 60_000


def usage_tokens(usage: dict[str, Any] | None) -> int:
    """Сколько токенов стоил обмен по данным `usage`.

    Считать нечем — считаем нулём, а не падаем: `usage` приходит от сервера, и при обрыве
    потока или отказе его может не быть вовсе. Уронить на этом сбор статистики значило бы
    потерять и сам ответ, который уже получен.

    `total_tokens` предпочитаем сумме слагаемых: сервер кладёт туда собственный итог, и он
    учитывает то, чего в двух слагаемых нет, — например попадания в кэш начала запроса."""
    if not usage:
        return 0
    total = usage.get("total_tokens")
    if isinstance(total, int):
        return total
    parts = (usage.get("prompt_tokens"), usage.get("completion_tokens"))
    return sum(part for part in parts if isinstance(part, int))


@dataclass
class Turn:
    """Итог одного обмена с моделью — тем, кто позвал: тексту ответа и цене.

    Запись полная: по ней видно не только что ответила модель, но и что именно ей
    ушло, — вызывающему не приходится лезть внутрь агента за подробностями."""

    status: str
    text: str = ""
    reasoning: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)
    elapsed_ms: int = 0
    error: str | None = None
    # что фактически ушло в модель — иначе прогон не воспроизвести и не сверить
    request_messages: list[dict] = field(default_factory=list)
    # какой моделью получен ответ: модель меняется на лету, а запись остаётся
    model: str = ""
    # слепок профиля для журнала — инструкция и параметры на момент запроса
    profile_snapshot: dict[str, Any] = field(default_factory=dict)
    # сколько пар выброшено окном памяти перед этим запросом — чтобы показать усечение
    dropped_pairs: int = 0
    # текст ошибки записи журнала: сам обмен удался, а предупредить надо снаружи
    journal_error: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == "ok"


class Agent:
    """Собеседник со своей памятью: имя, профиль и накопленные пары «вопрос — ответ»."""

    def __init__(self, name: str, profile: Profile) -> None:
        self.name = name
        self.profile = profile
        self._messages: list[dict] = []
        # сколько пар выбросила обрезка перед последней сборкой запроса — отсюда в `Turn`
        self._dropped_pairs = 0
        # Счётчики за всё время жизни агента: их показывает список `/agents`.
        # СЧИТАЕМ И НЕУДАЧНЫЕ, И ОТМЕНЁННЫЕ ОБМЕНЫ — это не недосмотр, а решение.
        # Время на упавший запрос потрачено, токены на него у поставщика списаны, и список,
        # показывающий только удачные обмены, рисует агента дешевле, чем он есть.
        # Дороже всего обходится как раз агент, который падает и уходит на повтор.
        self.runs = 0  # сколько обменов было — всего, при любом исходе
        self.total_ms = 0  # сколько времени на них ушло
        self.total_tokens = 0  # сколько токенов израсходовано

    def history(self) -> list[dict]:
        """Память разговора — копией.

        Отдай сам список — и любой снаружи сможет незаметно дописать или стереть память
        агента, минуя `remember`. Тогда инкапсуляция перестаёт что-либо значить: правил
        пополнения памяти больше нет, а искать, кто её испортил, придётся по всему коду."""
        return [dict(m) for m in self._messages]

    def forget(self) -> None:
        """Забыть разговор целиком — например, когда сменился профиль."""
        self._messages.clear()

    def remember(self, question: str, answer: str) -> None:
        """Положить в память готовую пару «вопрос — ответ».

        ЕДИНСТВЕННАЯ точка записи в память. `exchange` пополняет память тоже через этот
        метод, а не напрямую через `self._messages`, — второй точки записи заводить нельзя,
        иначе правила пополнения снова разъедутся по разным местам."""
        self._messages.append({"role": "user", "content": question})
        self._messages.append({"role": "assistant", "content": answer})

    def _trim(self) -> int:
        """Обрезать память до окна. Возвращает число выброшенных пар.

        Два ограничителя подряд, и оба обязаны резать ТОЛЬКО целыми парами: начнись память
        с ответа — сервер откажет в запросе, причём в случайный момент работы, а не при
        проверках.

        Первый ограничитель — по числу пар. Он срабатывает не на превышении окна, а на
        превышении окна с запасом, и выбрасывает сразу весь запас: обрезка на каждом ходу
        меняла бы начало запроса на каждом ходу.

        Второй — аварийный, по объёму в знаках: одна пара со вставленным файлом весит больше
        любого разумного окна по парам. Он режет по одной старейшей паре, но последнюю пару
        не трогает никогда — иначе агент забудет то, о чём его только что спросили, и
        останется без ответа, к которому пользователь мог отослаться.

        Пересказ истории отдельным вызовом модели сюда сознательно не заводится: он вносит
        в разговор текст, которого никто не говорил, и отладить это потом нечем. Забывание
        честнее."""
        dropped = 0
        window = self.profile.history_window
        if window > 0 and len(self._messages) // 2 >= window + WINDOW_SLACK_PAIRS:
            del self._messages[: WINDOW_SLACK_PAIRS * 2]
            dropped += WINDOW_SLACK_PAIRS
        while len(self._messages) > 2 and sum(len(m["content"]) for m in self._messages) > HISTORY_CHARS_MAX:
            del self._messages[:2]
            dropped += 1
        return dropped

    def build_messages(self, content: str) -> list[dict]:
        """Политика входа: что именно уйдёт в модель на этот вопрос.

        Системная инструкция всегда первая: так её видно отдельно от ввода пользователя,
        и так же работает кэширование общего начала запроса на стороне DeepSeek.

        Вопрос в память здесь НЕ дописывается. Память пополняется только после успешного
        ответа, в `exchange`. Причина: неотвеченный вопрос не должен оседать в памяти —
        иначе после сетевого сбоя модель на следующем ходу увидит вопрос, на который
        никто не отвечал, и станет отвечать на него повторно.

        Список собирается новый, а сообщения памяти копируются: наружу не уходит ничего,
        через что вызывающий мог бы переписать память агента.

        Обрезка идёт здесь, в самом начале, до сборки: она ограничивает именно то, что уйдёт
        в модель. Обрежь после сборки — и запрос ушёл бы полным, а урезанной осталась бы одна
        память, то есть ограничение не работало бы ровно там, ради чего заведено."""
        self._dropped_pairs = self._trim()
        messages: list[dict] = []
        if self.profile.system:
            messages.append({"role": "system", "content": self.profile.system})
        if self.profile.keep_history:
            messages.extend(dict(m) for m in self._messages)
        messages.append({"role": "user", "content": content})
        return messages

    async def exchange(
        self,
        client,
        model: str,
        content: str,
        *,
        on_event: Callable[[api.StreamEvent], None] | None = None,
        agent: str | None = None,
        run_id: str | None = None,
    ) -> Turn:
        """Обмен с моделью: отправить вопрос, собрать поток ответа, записать прогон."""
        request_messages = self.build_messages(content)
        answer_text = ""
        reasoning_text = ""
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        status = "error"
        error_text: str | None = None
        render_error: str | None = None
        started = time.monotonic()
        try:
            async for event in client.stream_chat(model, request_messages, self.profile.params):
                if on_event is not None:
                    try:
                        on_event(event)
                    except Exception as exc:
                        # `on_event` — это отрисовка, она живёт СНАРУЖИ агента. Выпусти её
                        # исключение отсюда — и оно попадёт в общий обработчик ошибок обмена
                        # ниже, а пользователь увидит «ошибка запроса к DeepSeek» при
                        # полностью живой сети: прямая ложь, из-за которой он пойдёт чинить
                        # то, что не сломано. Проглотить совсем тоже нельзя — причину
                        # запоминаем и отдаём в `Turn.error`, но только если обмен в
                        # остальном удался, и текстом, где нет слова DeepSeek.
                        render_error = f"ответ получен, но показать его не удалось: {exc}"
                if event.kind == "meta":
                    finish_reason = event.finish_reason
                    usage = event.usage
                    continue
                if event.kind == "reasoning":
                    reasoning_text += event.text
                else:
                    answer_text += event.text
            status = "ok"
        except asyncio.CancelledError:
            status = "cancelled"
            raise
        except Exception as exc:  # сеть, лимиты, ошибки API — не роняем harness
            error_text = str(exc)
        finally:
            elapsed = time.monotonic() - started
            snapshot = self.profile.snapshot()
            if status == "ok" and not answer_text:
                # Поток дошёл до конца, а ответа нет. Наверх обязан уйти внятный отказ,
                # а не бодрое «успех» с пустой строкой, которую вызывающий покажет
                # пользователю как ответ модели.
                #
                # Причину называем, когда она известна. Самый частый случай при включённых
                # рассуждениях: модель израсходовала весь `max_tokens` на размышление и
                # оборвалась, не начав ответ, — тогда сервер отдаёт причину остановки
                # `length`. Сказать просто «пустой ответ» значит заставить человека гадать,
                # хотя ответ лежит в тех же данных: лечится увеличением `max_tokens`.
                status = "error"
                if finish_reason == "length":
                    error_text = (
                        "весь max_tokens ушёл на рассуждения, ответ не начат — увеличьте max_tokens"
                    )
                else:
                    error_text = "модель вернула пустой ответ"
            if status == "ok":
                if render_error:
                    error_text = render_error
                # Пара кладётся ТОЛЬКО через `remember` — второй точки записи в память нет.
                if self.profile.keep_history:
                    self.remember(content, answer_text)
            # При любом неуспехе память не трогаем вовсе: вопроса там нет, `build_messages`
            # его туда не клал, — вычищать нечего.
            # Счётчики пополняем здесь, в `finally`: сюда приходит и успех, и отказ, и отмена
            # (после `raise` тело `finally` всё равно выполняется). Пополни их в ветке успеха —
            # и статистика молча потеряла бы самые дорогие обмены.
            self.runs += 1
            self.total_ms += int(elapsed * 1000)
            self.total_tokens += usage_tokens(usage)
            entry: dict[str, Any] = {
                "status": status,
                "model": model,
                "profile": snapshot,
                "query": content,
                "messages": request_messages,
                "response": answer_text or None,
                "reasoning": reasoning_text or None,
                "finish_reason": finish_reason,
                "usage": usage or None,
                "elapsed_ms": int(elapsed * 1000),
                "error": error_text,
            }
            if agent:
                entry["agent"] = agent
            if run_id:
                entry["run_id"] = run_id
            journal_error = journal.append(entry)
            turn = Turn(
                status=status,
                text=answer_text,
                reasoning=reasoning_text,
                finish_reason=finish_reason,
                usage=usage,
                elapsed_ms=int(elapsed * 1000),
                error=error_text,
                request_messages=request_messages,
                model=model,
                profile_snapshot=snapshot,
                dropped_pairs=self._dropped_pairs,
                journal_error=journal_error,
            )
        return turn
