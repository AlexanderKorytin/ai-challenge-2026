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
from typing import Any

from . import api, journal
from .profiles import Profile


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

    def build_messages(self, content: str) -> list[dict]:
        """Политика входа: что именно уйдёт в модель на этот вопрос.

        Системная инструкция всегда первая: так её видно отдельно от ввода пользователя,
        и так же работает кэширование общего начала запроса на стороне DeepSeek.

        Вопрос в память здесь НЕ дописывается. Память пополняется только после успешного
        ответа, в `exchange`. Причина: неотвеченный вопрос не должен оседать в памяти —
        иначе после сетевого сбоя модель на следующем ходу увидит вопрос, на который
        никто не отвечал, и станет отвечать на него повторно.

        Список собирается новый, а сообщения памяти копируются: наружу не уходит ничего,
        через что вызывающий мог бы переписать память агента."""
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
                status = "error"
                error_text = "модель вернула пустой ответ"
            if status == "ok":
                if render_error:
                    error_text = render_error
                # Пара кладётся ТОЛЬКО через `remember` — второй точки записи в память нет.
                if self.profile.keep_history:
                    self.remember(content, answer_text)
            # При любом неуспехе память не трогаем вовсе: вопроса там нет, `build_messages`
            # его туда не клал, — вычищать нечего.
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
                dropped_pairs=0,
                journal_error=journal_error,
            )
        return turn
