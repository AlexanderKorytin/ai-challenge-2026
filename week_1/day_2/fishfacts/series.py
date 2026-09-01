"""Серия одинаковых запросов по одному или нескольким профилям.

Задача дня — добиться одинаковой по форме выдачи на повторяющихся запросах. Один прогон
этого не покажет: у модели нет `seed`, и разброс виден только на серии. Запросы идут
параллельно, иначе полсотни ответов ждать слишком долго.

    uv run python -m fishfacts.series --query щука --runs 10
    uv run python -m fishfacts.series --profile s2-json --profile s3-json-limited --runs 50
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from myharness import api, profiles
from myharness.config import load as load_config

from . import metrics, schema

DEFAULT_PROFILES = ("s1-free", "s2-json", "s3-json-limited")
RESULTS_DIR = Path("data")


async def one_run(
    client: api.DeepSeekClient,
    model: str,
    profile: profiles.Profile,
    query: str,
    index: int,
    limiter: asyncio.Semaphore,
) -> metrics.Run:
    messages: list[dict] = []
    if profile.system:
        messages.append({"role": "system", "content": profile.system})
    messages.append({"role": "user", "content": query})

    async with limiter:
        started = time.monotonic()
        text = ""
        finish_reason: str | None = None
        usage: dict = {}
        try:
            async for event in client.stream_chat(model, messages, profile.params):
                if event.kind == "content":
                    text += event.text
                elif event.kind == "meta":
                    finish_reason, usage = event.finish_reason, event.usage
        except Exception as exc:  # сеть, лимиты — один сбойный прогон не должен ронять серию
            elapsed = int((time.monotonic() - started) * 1000)
            return metrics.Run(
                profile=profile.name, query=query, index=index, elapsed_ms=elapsed, error=str(exc)
            )
        elapsed = int((time.monotonic() - started) * 1000)

    parsed = schema.parse(text)
    return metrics.Run.from_parsed(profile.name, query, index, parsed, finish_reason, usage, elapsed)


async def run_profile(
    client: api.DeepSeekClient,
    model: str,
    name: str,
    query: str,
    runs: int,
    concurrency: int,
    max_tokens: int | None = None,
) -> tuple[metrics.Summary, list[metrics.Run]]:
    profile, warnings = profiles.load(name)
    for warning in warnings:
        print(f"  ! {warning}")
    if max_tokens is not None:
        profile.params["max_tokens"] = max_tokens
        print(f"  max_tokens переопределён: {max_tokens}")
    limiter = asyncio.Semaphore(concurrency)
    tasks = [one_run(client, model, profile, query, i + 1, limiter) for i in range(runs)]
    results = await asyncio.gather(*tasks)
    return metrics.summarize(name, query, list(results)), list(results)


async def main_async(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.is_authorized:
        print("Ключ DeepSeek не задан. Запустите myharnessfish и выполните /auth.")
        return 1

    model = args.model or config.model
    client = api.DeepSeekClient(config.api_key)
    summaries: list[metrics.Summary] = []
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)

    try:
        for name in args.profile:
            print(f"\n▸ {name}: {args.runs} прогонов запроса «{args.query}» (модель {model})")
            summary, runs = await run_profile(
                client, model, name, args.query, args.runs, args.concurrency, args.max_tokens
            )
            summaries.append(summary)
            payload = {"summary": summary.to_dict(), "runs": [run.to_dict() for run in runs]}
            path = RESULTS_DIR / f"series-{name}.json"
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  результат: {path}")
            for failure in summary.failures:
                print(f"  ✕ {failure}")
    finally:
        await client.aclose()

    print("\n" + metrics.table(summaries))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="fishfacts.series", description="Серия одинаковых запросов к агенту по рыбам"
    )
    parser.add_argument("--profile", action="append", help="профиль (можно несколько); по умолчанию все три ступени")
    parser.add_argument("--query", default="щука", help="запрос, повторяемый в серии")
    parser.add_argument("--runs", type=int, default=10, help="сколько раз повторить запрос")
    parser.add_argument("--concurrency", type=int, default=5, help="сколько запросов держать одновременно")
    parser.add_argument("--model", help="модель DeepSeek (по умолчанию из настроек harness)")
    parser.add_argument(
        "--max-tokens",
        type=int,
        dest="max_tokens",
        help="переопределить max_tokens профиля — так видно, как жёсткий лимит рвёт формат",
    )
    args = parser.parse_args()
    if not args.profile:
        args.profile = list(DEFAULT_PROFILES)
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
