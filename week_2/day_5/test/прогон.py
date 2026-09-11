"""Живой прогон дня 10: три стратегии на одном сценарии, без скрытых повторов."""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path
from uuid import uuid4

from myharness import api, config, memory, profiles, tokens
from myharness.agent import Agent, Turn

СТРАТЕГИИ = ("sliding", "facts", "branching")
РЕЗУЛЬТАТ = Path("live-results.json")
ЖУРНАЛ = Path("live-journal.jsonl")


def запись_хода(номер: int, ветвь: str | None, run_id: str, turn: Turn) -> dict:
    return {
        "message": номер,
        "branch": ветвь,
        "run_id": run_id,
        "status": turn.status,
        "error": turn.error,
        "store_error": turn.store_error,
        "facts_error": turn.facts_error,
        "response": turn.text,
        "request_messages": turn.request_messages,
        "usage": turn.usage or None,
        "facts_usage": turn.facts_usage,
        "selected_pairs": turn.selected_pairs,
        "omitted_pairs": turn.omitted_pairs,
        "facts_revision": turn.facts_revision,
        "facts_revision_after": turn.facts_revision_after,
        "branch_head": turn.branch_head,
        "branch_checkpoint": turn.branch_checkpoint,
    }


def итог_расхода(ходы: list[dict]) -> dict:
    основные = [ход["usage"] for ход in ходы if ход["usage"]]
    служебные = [ход["facts_usage"] for ход in ходы if ход["facts_usage"]]
    неизвестно_основных = sum(ход["usage"] is None for ход in ходы)
    ожидается_служебных = sum(1 for ход in ходы if ход.get("facts_revision") is not None)
    неизвестно_служебных = ожидается_служебных - len(служебные)
    return {
        "main": tokens.total_usage(основные),
        "facts": tokens.total_usage(служебные),
        "total": tokens.total_usage([*основные, *служебные]),
        "main_calls": len(основные),
        "facts_calls": len(служебные),
        "unknown_main_calls": неизвестно_основных,
        "unknown_facts_calls": неизвестно_служебных,
    }


def ошибка_хода(turn: Turn) -> str | None:
    причины = []
    if not turn.ok:
        причины.append(turn.error or turn.status)
    if turn.store_error:
        причины.append(turn.store_error)
    if turn.facts_error:
        причины.append(turn.facts_error)
    if not turn.usage:
        причины.append("сервер не вернул usage основного обращения")
    if turn.facts_revision is not None and not turn.facts_usage:
        причины.append("сервер не вернул usage извлекателя фактов")
    return "; ".join(причины) or None


async def обмен(agent: Agent, client: api.DeepSeekClient, model: str, номер: int, текст: str, ветвь: str | None = None) -> tuple[dict, str | None]:
    run_id = f"day10-{agent.profile.context_strategy}-{номер}-{uuid4().hex}"
    turn = await agent.exchange(client, model, текст, run_id=run_id)
    print(f"{agent.profile.context_strategy:9} {номер:02d}: {turn.status}", flush=True)
    return запись_хода(номер, ветвь, run_id, turn), ошибка_хода(turn)


async def линейный(имя: str, client: api.DeepSeekClient, model: str) -> dict:
    profile, warnings = profiles.load(имя)
    if warnings:
        return {"complete": False, "errors": warnings, "turns": [], "final": {}}
    path = memory.new_session(Path.cwd(), profile.name, profile.context_strategy)
    store = memory.SessionStore(path)
    error = store.touch()
    if error:
        return {"complete": False, "errors": [error], "turns": [], "final": {}}
    facts_store = memory.FactsStore(path) if имя == "facts" else None
    agent = Agent(имя, profile, store=store, facts_store=facts_store)
    ходы: list[dict] = []
    ошибки: list[str] = []
    for номер, текст in enumerate(profile.prefills, start=1):
        ход, ошибка = await обмен(agent, client, model, номер, текст)
        ходы.append(ход)
        if ошибка:
            ошибки.append(f"сообщение {номер}: {ошибка}")
            break
    return {
        "complete": not ошибки and len(ходы) == 15,
        "errors": ошибки,
        "turns": ходы,
        "final": {
            "A": ходы[13]["response"] if len(ходы) > 13 else None,
            "B": ходы[14]["response"] if len(ходы) > 14 else None,
        },
        "usage": итог_расхода(ходы),
    }


async def ветвящийся(client: api.DeepSeekClient, model: str) -> dict:
    profile, warnings = profiles.load("branching")
    if warnings:
        return {"complete": False, "errors": warnings, "turns": [], "final": {}}
    store = memory.BranchStore(memory.new_session(Path.cwd(), profile.name, "branching"))
    root = Agent("branching", profile, branch_store=store)
    ходы: list[dict] = []
    ошибки: list[str] = []
    for номер, текст in enumerate(profile.prefills[:5], start=1):
        ход, ошибка = await обмен(root, client, model, номер, текст)
        ходы.append(ход)
        if ошибка:
            ошибки.append(f"сообщение {номер}: {ошибка}")
            break
    branches = None
    if not ошибки:
        branches, ошибка = root.split_branches("A", "B")
        if ошибка:
            ошибки.append(f"split: {ошибка}")
    if branches is not None:
        маршрут = [
            *(('A', номер, текст) for номер, текст in zip(range(6, 10), profile.branch_prefills['A'][:4], strict=True)),
            *(('B', номер, текст) for номер, текст in zip(range(10, 14), profile.branch_prefills['B'][:4], strict=True)),
            ('A', 14, profile.branch_prefills['A'][4]),
            ('B', 15, profile.branch_prefills['B'][4]),
        ]
        for ветвь, номер, текст in маршрут:
            ход, ошибка = await обмен(branches[ветвь], client, model, номер, текст, ветвь)
            ходы.append(ход)
            if ошибка:
                ошибки.append(f"сообщение {номер}, ветвь {ветвь}: {ошибка}")
                break
    по_номеру = {ход["message"]: ход for ход in ходы}
    return {
        "complete": not ошибки and len(ходы) == 15,
        "errors": ошибки,
        "turns": ходы,
        "final": {
            "A": по_номеру.get(14, {}).get("response"),
            "B": по_номеру.get(15, {}).get("response"),
        },
        "usage": итог_расхода(ходы),
    }


async def главное() -> None:
    cfg = config.load()
    if not cfg.is_authorized:
        sys.exit("ключ не задан: сначала выполните /auth в myharness")
    os.environ["MYHARNESS_STATE_DIR"] = tempfile.mkdtemp(prefix="myharness-day10-")
    os.environ["MYHARNESS_JOURNAL"] = str(ЖУРНАЛ.resolve())
    for path in (РЕЗУЛЬТАТ, ЖУРНАЛ):
        path.unlink(missing_ok=True)
    client = api.DeepSeekClient(cfg.api_key)
    sliding, facts, branching = await asyncio.gather(
        линейный("sliding", client, cfg.model),
        линейный("facts", client, cfg.model),
        ветвящийся(client, cfg.model),
    )
    result = {
        "model": cfg.model,
        "complete": all(item.get("complete") for item in (sliding, facts, branching)),
        "strategies": {"sliding": sliding, "facts": facts, "branching": branching},
    }
    РЕЗУЛЬТАТ.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"результат: {РЕЗУЛЬТАТ}; complete={result['complete']}")


if __name__ == "__main__":
    asyncio.run(главное())
