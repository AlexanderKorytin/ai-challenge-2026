"""Прогон того же, что будет на записи: три вопроса подряд по документу «на грани».

Нужен затем, чтобы на съёмке не выяснилось, что переполнение наступает не там, где обещано,
или что первый же запрос не проходит. Профиль берётся тот самый, что и в harness.
"""

from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path
from uuid import uuid4

ЗДЕСЬ = Path(__file__).parent
os.environ.setdefault("MYHARNESS_JOURNAL", str(ЗДЕСЬ / "потолок-журнал.jsonl"))

from myharness import config, profiles, tokens  # noqa: E402
from myharness.agent import Agent  # noqa: E402
from myharness.api import DeepSeekClient  # noqa: E402

МОДЕЛЬ = "deepseek-v4-flash"
# Вопросы больше не пишутся здесь: их задаёт очередь профиля — ровно та, что пойдёт на записи.



async def main() -> None:
    профиль, замечания = profiles.load("стог")
    вопросы = профиль.prefills  # ровно та очередь, что пойдёт на записи
    for з in замечания:
        print("замечание профиля:", з)
    агент = Agent("стог", профиль)
    клиент = DeepSeekClient(config.load().api_key)
    прогон = uuid4().hex[:8]
    try:
        for номер, вопрос in enumerate(вопросы, 1):
            запрос = агент.build_messages(вопрос)
            предсказание = агент.predict_tokens(запрос, МОДЕЛЬ)
            место_под_ответ = профиль.params.get("max_tokens", 0)
            print(f"\n── вопрос {номер}: {вопрос[:60]}…")
            print(f"   предсказано входа: {предсказание:,} + {место_под_ответ} на ответ = "
                  f"{предсказание + место_под_ответ:,} из {tokens.CONTEXT_WINDOW:,}")
            обмен = await агент.exchange(клиент, МОДЕЛЬ, вопрос, agent="стог", run_id=прогон)
            if обмен.ok:
                расход = tokens.normalize(обмен.usage)
                print(f"   ПРОШЁЛ за {обмен.elapsed_ms / 1000:.0f} с: вход {расход['prompt_tokens']:,} "
                      f"(из кэша {расход['prompt_cache_hit_tokens']:,}), выход {расход['completion_tokens']}, "
                      f"{tokens.format_price(tokens.price(обмен.usage, МОДЕЛЬ))}")
                print(f"   ответ: {обмен.text[:150]}")
            else:
                print(f"   ОТКАЗ за {обмен.elapsed_ms / 1000:.0f} с: {(обмен.error or '')[:300]}")
                break
    finally:
        await клиент.aclose()


asyncio.run(main())
