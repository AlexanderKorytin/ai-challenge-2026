"""Серия прогонов одной задачи всеми четырьмя способами.

Каждый способ — профиль harness, и каждый прогоняется много раз: один ответ ничего не
доказывает, у API нет `seed`, и на задаче с подвохом модель отвечает то так, то этак.
Способ определяется по самому профилю:

* обычный профиль          — один запрос (способы 1 и 2 и контрольная клетка);
* профиль со `screens`     — цепочка шагов: ответ первого шага уходит во второй (способ 3);
* профиль со `agents`      — группа: агенты отвечают параллельно, ведущий сводит (способ 4).

    uv run python -m reasoning.series --runs 20
    uv run python -m reasoning.series --profile free --profile multiprompting --runs 5
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from pathlib import Path

from myharness import api, profiles, screens, team
from myharness.config import load as load_config

from . import metrics

DEFAULT_METHODS = (
    "free",
    "chain_of_thought",
    "chain_of_thought_nothinking",
    "meta_prompting",
    "multiprompting",
)
DATA_DIR = Path("data")
TASK_FILE = DATA_DIR / "task.txt"


class Reply:
    """Ответ на один запрос: текст, рассуждения и цена."""

    def __init__(self) -> None:
        self.text = ""
        self.reasoning = ""
        self.usage: dict = {}
        self.finish_reason: str | None = None
        self.elapsed_ms = 0
        self.error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.text)

    def as_step(self, name: str, kind: str = metrics.AGENT) -> metrics.Step:
        return metrics.Step(
            name=name,
            kind=kind,
            text=self.text,
            prompt_tokens=int(self.usage.get("prompt_tokens") or 0),
            completion_tokens=int(self.usage.get("completion_tokens") or 0),
            reasoning_tokens=int((self.usage.get("completion_tokens_details") or {}).get("reasoning_tokens") or 0),
            elapsed_ms=self.elapsed_ms,
            error=self.error,
        )


async def ask(client: api.DeepSeekClient, model: str, profile: profiles.Profile, messages: list[dict]) -> Reply:
    """Один запрос к модели. Сбой сети не должен ронять серию — он становится полем error."""
    reply = Reply()
    started = time.monotonic()
    try:
        async for event in client.stream_chat(model, messages, profile.params):
            if event.kind == "content":
                reply.text += event.text
            elif event.kind == "reasoning":
                reply.reasoning += event.text
            elif event.kind == "meta":
                reply.finish_reason, reply.usage = event.finish_reason, event.usage
    except Exception as exc:  # сеть, лимиты, отказ сервера
        reply.error = str(exc)
    reply.elapsed_ms = int((time.monotonic() - started) * 1000)
    return reply


def flat_usage(reply: Reply) -> dict:
    details = reply.usage.get("completion_tokens_details") or {}
    return {
        "prompt_tokens": reply.usage.get("prompt_tokens") or 0,
        "completion_tokens": reply.usage.get("completion_tokens") or 0,
        "reasoning_tokens": details.get("reasoning_tokens") or 0,
    }


async def run_single(client, model: str, profile: profiles.Profile, question: str, index: int) -> metrics.Run:
    messages: list[dict] = []
    if profile.system:
        messages.append({"role": "system", "content": profile.system})
    messages.append({"role": "user", "content": question})
    reply = await ask(client, model, profile, messages)
    return metrics.Run(
        method=profile.name,
        index=index,
        answer=reply.text,
        reasoning=reply.reasoning,
        finish_reason=reply.finish_reason,
        elapsed_ms=reply.elapsed_ms,
        error=reply.error,
        usage=flat_usage(reply),
    )


async def run_chain(
    client, model: str, profile: profiles.Profile, question: str, index: int, thinking: str | None = None
) -> metrics.Run:
    """Способ 3: первым запросом модель составляет промпт, вторым — решает им задачу.

    Промпт составляется заново в каждом прогоне. Иначе мы измеряли бы один удачно или
    неудачно вышедший промпт, а не приём целиком.
    """
    run = metrics.Run(method=profile.name, index=index)
    step_profiles = [profiles.load(name)[0] for name in profile.screens]
    for step in step_profiles:
        force_thinking(step, thinking)
    if len(step_profiles) != 2:
        run.error = "цепочка ожидает ровно два шага: составить промпт и решить им задачу"
        return run

    ask_profile, solve_profile = step_profiles
    first = await ask(
        client,
        model,
        ask_profile,
        ([{"role": "system", "content": ask_profile.system}] if ask_profile.system else [])
        + [{"role": "user", "content": question}],
    )
    # составленный промпт — это не ответ на задачу, судить его по эталону бессмысленно
    run.steps.append(first.as_step(ask_profile.name, metrics.STAGE))
    if not first.ok:
        run.error = first.error or "модель не вернула промпт"
        return run

    # промпт от модели идёт как обычное сообщение — ровно так же, как его вставил бы человек
    solve_messages: list[dict] = []
    if solve_profile.system:
        solve_messages.append({"role": "system", "content": solve_profile.system})
    solve_messages.append({"role": "user", "content": f"{first.text.strip()}\n\n{question}"})
    second = await ask(client, model, solve_profile, solve_messages)
    run.answer = second.text
    run.reasoning = second.reasoning
    run.finish_reason = second.finish_reason
    run.elapsed_ms = first.elapsed_ms + second.elapsed_ms
    run.error = second.error
    run.usage = flat_usage(second)
    return run


async def run_team(
    client, model: str, lead: profiles.Profile, question: str, index: int, thinking: str | None = None
) -> metrics.Run:
    """Способ 4: агенты отвечают независимо и одновременно, ведущий сводит их ответы.

    Здесь та же схема, что и в harness, только без экранов: агенты не видят ответов друг
    друга, иначе «независимые мнения» превратились бы в эхо первого.
    """
    run = metrics.Run(method=lead.name, index=index)
    agents = [profiles.load(name)[0] for name in lead.agents]
    for agent in agents:
        force_thinking(agent, thinking)
    replies = await asyncio.gather(
        *(
            ask(client, model, agent, screens.build_messages(agent, screens.Screen(key=agent.name, title=agent.name), question))
            for agent in agents
        )
    )
    for agent, reply in zip(agents, replies, strict=True):
        run.steps.append(reply.as_step(agent.name))

    answers = [(agent.name, reply.text) for agent, reply in zip(agents, replies, strict=True) if reply.ok]
    if not answers:
        run.error = "ни один агент не ответил"
        return run

    instruction = lead.system or team.DEFAULT_LEAD_INSTRUCTION
    summary = await ask(
        client,
        model,
        lead,
        [
            {"role": "system", "content": instruction},
            {"role": "user", "content": team.build_summary_request(question, answers)},
        ],
    )
    run.answer = summary.text
    run.reasoning = summary.reasoning
    run.finish_reason = summary.finish_reason
    run.elapsed_ms = max(step.elapsed_ms for step in run.steps) + summary.elapsed_ms
    run.error = summary.error
    run.usage = flat_usage(summary)
    return run


async def one_run(
    client, model: str, profile: profiles.Profile, question: str, index: int, limiter, thinking: str | None = None
) -> metrics.Run:
    async with limiter:
        if profile.agents:
            return await run_team(client, model, profile, question, index, thinking)
        if profile.screens:
            return await run_chain(client, model, profile, question, index, thinking)
        return await run_single(client, model, profile, question, index)


def force_thinking(profile: profiles.Profile, mode: str | None) -> None:
    """Переопределить режим рассуждений у профиля и у всех, кого он поднимает.

    Режим — половина клетки замера: одна и та же группа экспертов с рассуждениями и без них
    даёт разные результаты, и сравнивать её со способом «решай пошагово» честно только при
    одинаковом режиме. Чтобы не заводить зеркальный набор профилей, режим задаётся флагом.
    """
    if mode is None:
        return
    profile.params["thinking"] = {"type": "enabled" if mode == "on" else "disabled"}


async def run_method(
    client, model: str, name: str, question: str, runs: int, concurrency: int, thinking: str | None = None
) -> list[metrics.Run]:
    profile, warnings = profiles.load(name)
    for warning in warnings:
        print(f"  ! {warning}")
    force_thinking(profile, thinking)
    if thinking:
        print(f"  режим рассуждений переопределён: {thinking}")
    limiter = asyncio.Semaphore(concurrency)
    tasks = [one_run(client, model, profile, question, i + 1, limiter, thinking) for i in range(runs)]
    return list(await asyncio.gather(*tasks))


def save(name: str, runs: list[metrics.Run], thinking: str | None = None) -> Path:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    # режим в имени файла: иначе прогон с рассуждениями затёр бы прогон без них
    suffix = {"on": "-thinking", "off": "-nothinking"}.get(thinking or "", "")
    path = DATA_DIR / f"series-{name}{suffix}.json"
    payload = {
        "summary": metrics.summarize(name, runs).to_dict(),
        "runs": [run.to_dict() for run in runs],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


async def main_async(args: argparse.Namespace) -> int:
    config = load_config()
    if not config.is_authorized:
        print("Ключ DeepSeek не задан. Запустите myharness и выполните /auth.")
        return 1
    task_path = Path(args.task)
    if not task_path.is_file():
        print(f"Не найден файл с условием задачи: {task_path}")
        return 1
    question = task_path.read_text(encoding="utf-8").strip()

    model = args.model or config.model
    client = api.DeepSeekClient(config.api_key)
    summaries: list[metrics.Summary] = []
    try:
        for name in args.profile:
            print(f"\n▸ {name}: {args.runs} прогонов (модель {model})")
            runs = await run_method(client, model, name, question, args.runs, args.concurrency, args.thinking)
            path = save(name, runs, args.thinking)
            summary = metrics.summarize(name, runs)
            summaries.append(summary)
            print(f"  результат: {path}")
            for failure in summary.failures:
                print(f"  ✕ {failure}")
    finally:
        await client.aclose()

    print("\n" + metrics.table(summaries))
    print("\nОтветы ещё не проверены — вердикты проставит судья: uv run python -m reasoning.judge")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(prog="reasoning.series", description="Одна задача четырьмя способами промптинга")
    parser.add_argument("--profile", action="append", help="способ (можно несколько); по умолчанию все")
    parser.add_argument("--runs", type=int, default=20, help="сколько раз повторить каждый способ")
    parser.add_argument("--concurrency", type=int, default=5, help="сколько прогонов держать одновременно")
    parser.add_argument("--model", help="модель DeepSeek (по умолчанию из настроек harness)")
    parser.add_argument("--task", default=str(TASK_FILE), help="файл с условием задачи")
    parser.add_argument(
        "--thinking",
        choices=("on", "off"),
        help="переопределить режим рассуждений у профиля и всех, кого он поднимает",
    )
    args = parser.parse_args()
    if not args.profile:
        args.profile = list(DEFAULT_METHODS)
    raise SystemExit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
