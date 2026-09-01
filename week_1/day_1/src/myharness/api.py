"""Тонкая обёртка над DeepSeek API (OpenAI-совместимый формат)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from openai import AsyncOpenAI, AuthenticationError, Timeout

BASE_URL = "https://api.deepseek.com"

# SDK по умолчанию даёт всего 5с на установление соединения — на неидеальной сети
# (VPN и т.п.) этого мало и вылезает ложный "Request timed out" при живом сервисе.
REQUEST_TIMEOUT = Timeout(connect=20.0, read=120.0, write=20.0, pool=20.0)

# Резервный список — используется, если GET /models недоступен.
FALLBACK_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]

# Параметры, которые уходят прямыми аргументами метода, и те, что кладутся в extra_body.
DIRECT_PARAMS = ("temperature", "top_p", "max_tokens", "stop", "response_format")
EXTRA_BODY_PARAMS = ("thinking", "reasoning_effort")


@dataclass
class StreamEvent:
    kind: str  # "reasoning" | "content" | "meta"
    text: str = ""
    finish_reason: str | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class AuthError(Exception):
    """Ключ отсутствует или не принят DeepSeek API."""


def split_params(params: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    direct = {k: v for k, v in params.items() if k in DIRECT_PARAMS and v is not None}
    extra = {k: v for k, v in params.items() if k in EXTRA_BODY_PARAMS and v is not None}
    return direct, extra


class DeepSeekClient:
    def __init__(self, api_key: str) -> None:
        self._client = AsyncOpenAI(api_key=api_key, base_url=BASE_URL, timeout=REQUEST_TIMEOUT)

    async def validate(self) -> bool:
        """False — ключ отклонён сервером (401). Прочие сбои (сеть и т.п.) пробрасываются вызывающему."""
        try:
            await self._client.models.list()
        except AuthenticationError:
            return False
        return True

    async def list_models(self) -> list[str]:
        response = await self._client.models.list()
        ids = sorted({m.id for m in response.data})
        return ids or FALLBACK_MODELS

    async def stream_chat(
        self,
        model: str,
        messages: list[dict],
        params: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamEvent]:
        """Потоковый ответ. Последним отдаёт событие "meta" с причиной остановки и расходом токенов."""
        direct, extra_body = split_params(params or {})
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            stream_options={"include_usage": True},
            extra_body=extra_body,
            **direct,
        )
        finish_reason: str | None = None
        usage: dict[str, Any] = {}
        async for chunk in stream:
            if chunk.usage is not None:
                usage = chunk.usage.model_dump(exclude_none=True)
            if not chunk.choices:
                continue
            choice = chunk.choices[0]
            if choice.finish_reason:
                finish_reason = choice.finish_reason
            delta = choice.delta
            if delta is None:
                continue
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamEvent("reasoning", reasoning)
            if delta.content:
                yield StreamEvent("content", delta.content)
        yield StreamEvent("meta", finish_reason=finish_reason, usage=usage)

    async def aclose(self) -> None:
        await self._client.close()
