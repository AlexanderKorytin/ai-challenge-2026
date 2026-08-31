"""Тонкая обёртка над DeepSeek API (OpenAI-совместимый формат)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

from openai import AsyncOpenAI, AuthenticationError, Timeout

BASE_URL = "https://api.deepseek.com"

# SDK по умолчанию даёт всего 5с на установление соединения — на неидеальной сети
# (VPN и т.п.) этого мало и вылезает ложный "Request timed out" при живом сервисе.
REQUEST_TIMEOUT = Timeout(connect=20.0, read=120.0, write=20.0, pool=20.0)

# Резервный список — используется, если GET /models недоступен.
FALLBACK_MODELS = ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]


def supports_thinking(model: str) -> bool:
    """У pro-моделей есть режим размышлений (reasoning_content), у flash — нет."""
    return "pro" in model


@dataclass
class StreamEvent:
    kind: str  # "reasoning" | "content"
    text: str


class AuthError(Exception):
    """Ключ отсутствует или не принят DeepSeek API."""


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

    async def stream_chat(self, model: str, messages: list[dict]) -> AsyncIterator[StreamEvent]:
        extra_body = {"thinking": {"type": "enabled"}, "reasoning_effort": "high"} if supports_thinking(model) else {}
        stream = await self._client.chat.completions.create(
            model=model,
            messages=messages,
            stream=True,
            extra_body=extra_body,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            reasoning = getattr(delta, "reasoning_content", None)
            if reasoning:
                yield StreamEvent("reasoning", reasoning)
            if delta.content:
                yield StreamEvent("content", delta.content)

    async def aclose(self) -> None:
        await self._client.close()
