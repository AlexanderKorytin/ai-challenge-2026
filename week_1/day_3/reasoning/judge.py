"""Судья: сверяет ответы серии с эталоном и проставляет вердикты.

Зачем отдельно от прогонов: ответы стоят денег и времени, а разметка — нет. Судью можно
переспрашивать, менять ему инструкцию и пересчитывать вердикты по уже собранным ответам,
не трогая саму серию.

Судье нельзя верить на слово: прежде чем принять его цифры, ответы надо просмотреть глазами
(`--show <способ>`) и убедиться, что он не считает верным уклончивое «возможно, они сёстры,
а возможно, сводные братья». Результат такой сверки идёт в отчёт дня.

    uv run python -m reasoning.judge                 # разметить всё неразмеченное
    uv run python -m reasoning.judge --recheck       # пересудить заново
    uv run python -m reasoning.judge --show free     # прочитать ответы и вердикты глазами
"""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from myharness import api, profiles
from myharness.config import load as load_config

from . import metrics

DATA_DIR = Path("data")
TASK_FILE = DATA_DIR / "task.txt"
ANSWER_FILE = DATA_DIR / "answer.txt"
JUDGE_PROFILE = "judge"


def series_files(names: list[str] | None) -> list[Path]:
    if names:
        return [DATA_DIR / f"series-{name}.json" for name in names]
    return sorted(DATA_DIR.glob("series-*.json"))


def load_runs(path: Path) -> list[metrics.Run]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    return [metrics.Run.from_dict(item) for item in payload["runs"]]


def save_runs(path: Path, runs: list[metrics.Run]) -> None:
    method = runs[0].method if runs else path.stem.removeprefix("series-")
    payload = {"summary": metrics.summarize(method, runs).to_dict(), "runs": [run.to_dict() for run in runs]}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def verdict_request(question: str, reference: str, answer: str) -> str:
    return (
        f"Условие задачи:\n{question}\n\n"
        f"Эталонный ответ:\n{reference}\n\n"
        f"Ответ модели:\n{answer.strip()}"
    )


async def judge_one(client, model: str, profile: profiles.Profile, request: str) -> tuple[bool | None, str]:
    messages = [{"role": "system", "content": profile.system or ""}, {"role": "user", "content": request}]
    text = ""
    try:
        async for event in client.stream_chat(model, messages, profile.params):
            if event.kind == "content":
                text += event.text
    except Exception as exc:
        return None, f"судья не ответил: {exc}"
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None, f"судья вернул не JSON: {text[:120]}"
    correct = parsed.get("correct")
    if not isinstance(correct, bool):
        return None, f"судья не дал вердикта: {text[:120]}"
    return correct, str(parsed.get("why", ""))


async def judge_runs(client, model: str, runs: list[metrics.Run], question: str, reference: str, recheck: bool,
                     concurrency: int) -> int:
    profile, warnings = profiles.load(JUDGE_PROFILE)
    for warning in warnings:
        print(f"  ! {warning}")
    limiter = asyncio.Semaphore(concurrency)
    pending = [run for run in runs if run.answer and not run.error and (recheck or run.correct is None)]

    async def judge(run: metrics.Run) -> None:
        async with limiter:
            run.correct, run.verdict_why = await judge_one(
                client, model, profile, verdict_request(question, reference, run.answer)
            )

    await asyncio.gather(*(judge(run) for run in pending))
    return len(pending)


def show(path: Path, limit: int) -> None:
    """Ответы и вердикты подряд — чтобы проверить судью, а не поверить ему."""
    runs = load_runs(path)
    print(f"\n▸ {path.stem.removeprefix('series-')}: {len(runs)} прогонов, показываю {min(limit, len(runs))}")
    for run in runs[:limit]:
        mark = {True: "✓ верно", False: "✕ неверно", None: "? без вердикта"}[run.correct]
        print(f"\n── прогон {run.index} — {mark} ({run.verdict_why})")
        if run.steps:
            for step in run.steps:
                print(f"   [{step.name}] {step.text.strip()[:300]}")
        print(f"   {run.answer.strip()[:600]}")


async def main_async(args: argparse.Namespace) -> int:
    files = [path for path in series_files(args.profile) if path.is_file()]
    if not files:
        print("Нет файлов серии — сначала прогоните reasoning.series")
        return 1
    if args.show:
        for path in files:
            if path.stem.removeprefix("series-") in args.show:
                show(path, args.limit)
        return 0

    if not TASK_FILE.is_file() or not ANSWER_FILE.is_file():
        print(f"Нужны {TASK_FILE} и {ANSWER_FILE} — условие задачи и эталонный ответ")
        return 1
    question = TASK_FILE.read_text(encoding="utf-8").strip()
    reference = ANSWER_FILE.read_text(encoding="utf-8").strip()

    config = load_config()
    if not config.is_authorized:
        print("Ключ DeepSeek не задан. Запустите myharness и выполните /auth.")
        return 1
    model = args.model or config.model
    client = api.DeepSeekClient(config.api_key)
    summaries: list[metrics.Summary] = []
    try:
        for path in files:
            runs = load_runs(path)
            judged = await judge_runs(client, model, runs, question, reference, args.recheck, args.concurrency)
            save_runs(path, runs)
            summary = metrics.summarize(runs[0].method if runs else path.stem, runs)
            summaries.append(summary)
            print(f"  {path.name}: размечено {judged}")
    finally:
        await client.aclose()

    print("\n" + metrics.table(summaries))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="reasoning.judge", description="Разметка ответов серии по эталону")
    parser.add_argument("--profile", action="append", help="способ (можно несколько); по умолчанию все файлы серии")
    parser.add_argument("--recheck", action="store_true", help="пересудить и уже размеченное")
    parser.add_argument("--show", action="append", help="показать ответы способа глазами, без обращения к API")
    parser.add_argument("--limit", type=int, default=20, help="сколько прогонов показывать при --show")
    parser.add_argument("--concurrency", type=int, default=5, help="сколько вердиктов запрашивать одновременно")
    parser.add_argument("--model", help="модель DeepSeek (по умолчанию из настроек harness)")
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
