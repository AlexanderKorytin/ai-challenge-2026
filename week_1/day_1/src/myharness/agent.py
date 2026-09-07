"""Сущность-собеседник: имя, профиль, память разговора и правила сборки запроса.

Здесь живёт вся логика разговора с моделью, отделённая от того, кто её показывает.
Модуль намеренно не знает ни про терминал, ни про панели: ему разрешены только `api`,
`journal`, `profiles` и стандартная библиотека. Поэтому агента можно проверить без
терминала, без окон и без единого нажатия клавиши.

Наружу агент отдаёт только результат — запись `Turn`. Внутренняя память остаётся
внутренней: её выдают копией, а пополняют единственным методом `remember`.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

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
