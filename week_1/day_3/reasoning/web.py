"""Локальная страница сравнения: четыре способа рядом, с ответами и ценой.

Данные берутся из файлов серий (`data/series-*.json`), поэтому страница ничего не знает ни
про harness, ни про API: прогоны и разметка делаются отдельно, а здесь только показ. Слушает
127.0.0.1 — наружу такое отдавать незачем.

    uv run python -m reasoning.web
"""

from __future__ import annotations

import argparse
import html
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import metrics

DATA_DIR = Path("data")

TITLES = {
    "free": "1. Без инструкций",
    "chain_of_thought": "2. Пошагово + рассуждения",
    "chain_of_thought_nothinking": "2к. Пошагово без рассуждений",
    "meta_prompting": "3. Промпт от модели",
    "multiprompting": "4. Группа экспертов",
    "multiprompting-nothinking": "4. Группа без рассуждений",
    "multiprompting-thinking": "4. Группа + рассуждения",
    "meta_prompting-thinking": "3. Промпт от модели + рассуждения",
}
ORDER = (
    "free",
    "chain_of_thought_nothinking",
    "chain_of_thought",
    "meta_prompting",
    "meta_prompting-thinking",
    "multiprompting-nothinking",
    "multiprompting",
    "multiprompting-thinking",
)

STYLE = """
:root { color-scheme: light dark; }
body { margin: 0; padding: 24px; font: 14px/1.5 -apple-system, Segoe UI, Roboto, sans-serif; }
h1 { font-size: 20px; margin: 0 0 4px; }
p.sub { margin: 0 0 20px; opacity: .65; }
table.total { border-collapse: collapse; margin-bottom: 28px; }
table.total th, table.total td { padding: 6px 14px; border-bottom: 1px solid rgba(128,128,128,.3); text-align: right; }
table.total th:first-child, table.total td:first-child { text-align: left; }
.cols { display: flex; gap: 16px; align-items: flex-start; overflow-x: auto; }
.col { flex: 1 0 320px; }
.col h2 { font-size: 15px; margin: 0 0 8px; }
.score { font-weight: 600; }
.run { border: 1px solid rgba(128,128,128,.3); border-radius: 8px; padding: 10px 12px; margin-bottom: 10px; }
.run.ok { border-left: 4px solid #3f9a54; }
.run.bad { border-left: 4px solid #c25450; }
.run.unknown { border-left: 4px solid #999; }
.meta { font-size: 12px; opacity: .65; margin-bottom: 6px; }
.answer { white-space: pre-wrap; max-height: 210px; overflow: auto; }
details { margin-top: 8px; }
summary { cursor: pointer; font-size: 12px; opacity: .8; }
details .step { white-space: pre-wrap; font-size: 12px; margin: 6px 0 0; padding-left: 10px;
  border-left: 2px solid rgba(128,128,128,.35); max-height: 180px; overflow: auto; }
"""


def load_series() -> list[tuple[str, metrics.Summary, list[metrics.Run]]]:
    found: dict[str, tuple[metrics.Summary, list[metrics.Run]]] = {}
    for path in sorted(DATA_DIR.glob("series-*.json")):
        payload = json.loads(path.read_text(encoding="utf-8"))
        runs = [metrics.Run.from_dict(item) for item in payload["runs"]]
        name = path.stem.removeprefix("series-")
        found[name] = (metrics.summarize(name, runs), runs)
    ordered = [name for name in ORDER if name in found] + [name for name in found if name not in ORDER]
    return [(name, *found[name]) for name in ordered]


def total_table(items: list[tuple[str, metrics.Summary, list[metrics.Run]]]) -> str:
    rows = [
        (
            "<tr><th>способ</th><th>верных</th><th>у экспертов</th><th>прогонов</th>"
            "<th>запросов</th><th>токенов</th><th>сек/ответ</th></tr>"
        )
    ]
    for name, summary, _ in items:
        judged = summary.answered - summary.unjudged
        share = "—" if summary.accuracy is None else f"{summary.correct}/{judged} ({summary.accuracy * 100:.0f}%)"
        per_answer = summary.elapsed_ms / summary.answered / 1000 if summary.answered else 0
        tokens = summary.prompt_tokens + summary.completion_tokens
        in_steps = f"{summary.steps_correct}/{summary.runs}" if summary.has_steps else "—"
        rows.append(
            f"<tr><td>{html.escape(TITLES.get(name, name))}</td><td class='score'>{share}</td>"
            f"<td>{in_steps}</td><td>{summary.runs}</td><td>{summary.requests}</td>"
            f"<td>{tokens}</td><td>{per_answer:.1f}</td></tr>"
        )
    return "<table class='total'>" + "".join(rows) + "</table>"


def run_card(run: metrics.Run) -> str:
    state = {True: "ok", False: "bad", None: "unknown"}[run.correct]
    mark = {True: "✓ верно", False: "✕ неверно", None: "? без вердикта"}[run.correct]
    meta = f"прогон {run.index} · {mark}"
    if run.verdict_why:
        meta += f" · {html.escape(run.verdict_why)}"
    meta += f" · {run.requests} запр. · {run.prompt_tokens + run.completion_tokens} ток."
    body = html.escape(run.answer.strip() or run.error or "пусто")
    steps = ""
    if run.steps:
        marks = {True: "✓", False: "✕", None: "?"}
        parts = "".join(
            f"<p class='step'><b>{marks[step.correct]} {html.escape(step.name)}</b><br>"
            f"{html.escape(step.text.strip()[:1500])}</p>"
            for step in run.steps
        )
        correct_here = sum(1 for step in run.steps if step.correct is True)
        steps = (
            f"<details><summary>ответы экспертов: {len(run.steps)}, верных {correct_here}</summary>"
            f"{parts}</details>"
        )
    return f"<div class='run {state}'><div class='meta'>{meta}</div><div class='answer'>{body}</div>{steps}</div>"


def page(question: str) -> str:
    items = load_series()
    if not items:
        return "<h1>Нет данных</h1><p>Сначала прогоните: uv run python -m reasoning.series</p>"
    columns = []
    for name, summary, runs in items:
        judged = summary.answered - summary.unjudged
        share = "—" if summary.accuracy is None else f"{summary.correct}/{judged}"
        cards = "".join(run_card(run) for run in runs)
        columns.append(
            f"<div class='col'><h2>{html.escape(TITLES.get(name, name))}</h2>"
            f"<div class='meta'>верных {share}</div>{cards}</div>"
        )
    return (
        f"<h1>Одна задача — четыре способа</h1>"
        f"<p class='sub'>{html.escape(question)}</p>"
        + total_table(items)
        + "<div class='cols'>"
        + "".join(columns)
        + "</div>"
    )


class Handler(BaseHTTPRequestHandler):
    question = ""

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        body = f"<!doctype html><meta charset='utf-8'><title>Способы промптинга</title><style>{STYLE}</style>"
        body += page(self.question)
        data = body.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, *args) -> None:  # тишина: страница локальная, журнал ни к чему
        return


def main() -> None:
    parser = argparse.ArgumentParser(prog="reasoning.web", description="Страница сравнения способов промптинга")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--task", default=str(DATA_DIR / "task.txt"))
    args = parser.parse_args()
    task_path = Path(args.task)
    Handler.question = task_path.read_text(encoding="utf-8").strip() if task_path.is_file() else ""
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"страница сравнения: http://127.0.0.1:{args.port}  (Ctrl+C — остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
