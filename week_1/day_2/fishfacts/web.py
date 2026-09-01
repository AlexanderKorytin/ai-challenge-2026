"""Локальная страница сравнения: ответы ступеней рядом, разобранные в карточки.

Данные берутся из журнала прогонов myharness и из файлов серий — поэтому страница ничего
не знает про harness и не требует в нём ни строчки правок. Слушает только 127.0.0.1:
наружу такое отдавать незачем.

    uv run python -m fishfacts.web
"""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from myharness import profiles

from . import schema

JOURNAL_CANDIDATES = ("myharness-journal.jsonl", "test/myharness-journal.jsonl")
SERIES_DIR = Path("data")
MAX_ENTRIES_PER_PROFILE = 3
JOURNAL_TAIL = 400


def find_journal(explicit: str | None) -> Path | None:
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_file() else None
    for name in JOURNAL_CANDIDATES:
        path = Path(name)
        if path.is_file():
            return path
    return None


def read_journal(path: Path | None) -> list[dict]:
    if path is None:
        return []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()[-JOURNAL_TAIL:]
    except OSError:
        return []
    records = []
    for line in lines:
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return records


def entry_view(record: dict) -> dict:
    text = record.get("response") or ""
    parsed = schema.parse(text)
    fact = parsed.fact.model_dump() if parsed.fact else None
    usage = record.get("usage") or {}
    return {
        "ts": record.get("ts"),
        "query": record.get("query"),
        "text": text,
        "is_json": parsed.is_json,
        "schema_ok": parsed.schema_ok,
        "errors": parsed.errors[:3],
        "fact": fact,
        "status": (fact or {}).get("status") or (parsed.data or {}).get("status"),
        "finish_reason": record.get("finish_reason"),
        "completion_tokens": usage.get("completion_tokens"),
        "prompt_tokens": usage.get("prompt_tokens"),
        "elapsed_ms": record.get("elapsed_ms"),
    }


def series_runs(profile: str) -> list[dict]:
    """Прогоны серии в виде записей журнала — у них те же поля, что нужны карточке."""
    path = SERIES_DIR / f"series-{profile}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    records = []
    for run in payload.get("runs", []):
        records.append(
            {
                "ts": f"серия #{run.get('index')}",
                "query": run.get("query"),
                "response": run.get("text"),
                "finish_reason": run.get("finish_reason"),
                "elapsed_ms": run.get("elapsed_ms"),
                "usage": {
                    "completion_tokens": run.get("completion_tokens"),
                    "prompt_tokens": run.get("prompt_tokens"),
                },
            }
        )
    return records


def collect(journal_path: Path | None) -> dict:
    records = [r for r in read_journal(journal_path) if r.get("status") == "ok" and r.get("response")]
    by_profile: dict[str, list[dict]] = {}
    for record in records:
        name = (record.get("profile") or {}).get("name") or "—"
        by_profile.setdefault(name, []).append(record)

    columns = []
    for name in ("s1-free", "s2-json", "s3-json-limited"):
        profile, _ = profiles.load(name)
        entries = [entry_view(r) for r in by_profile.get(name, [])[-MAX_ENTRIES_PER_PROFILE:]]
        if not entries:
            # Живых прогонов из harness ещё не было — показываем последние из серии,
            # чтобы страница не пустовала, пока идёт настройка.
            entries = [entry_view(r) for r in series_runs(name)[-MAX_ENTRIES_PER_PROFILE:]]
        columns.append({"profile": name, "description": profile.description, "entries": list(reversed(entries))})

    series = []
    if SERIES_DIR.is_dir():
        for path in sorted(SERIES_DIR.glob("series-*.json")):
            try:
                series.append(json.loads(path.read_text(encoding="utf-8"))["summary"])
            except (OSError, json.JSONDecodeError, KeyError):
                continue
    return {
        "generated_at": datetime.now().strftime("%H:%M:%S"),
        "journal": str(journal_path) if journal_path else None,
        "columns": columns,
        "series": series,
    }


PAGE = """<!doctype html>
<html lang="ru"><head><meta charset="utf-8">
<title>Агент по рыбам · сравнение ступеней контроля</title>
<style>
 :root { color-scheme: light dark; --bg:#f6f7f9; --card:#fff; --line:#dfe3e8; --dim:#6b7280;
         --ok:#15803d; --bad:#b91c1c; --accent:#0f766e; }
 @media (prefers-color-scheme: dark) { :root { --bg:#15181d; --card:#1c2027; --line:#2c323b;
         --dim:#9aa3af; --ok:#4ade80; --bad:#f87171; --accent:#5eead4; } }
 * { box-sizing: border-box; }
 body { margin:0; padding:24px; background:var(--bg); font:14px/1.5 -apple-system,system-ui,sans-serif; }
 h1 { font-size:19px; margin:0 0 4px; }
 .sub { color:var(--dim); margin-bottom:20px; font-size:13px; }
 .cols { display:grid; grid-template-columns:repeat(3,1fr); gap:16px; align-items:start; }
 @media (max-width:1000px) { .cols { grid-template-columns:1fr; } }
 .col > h2 { font-size:14px; margin:0 0 2px; }
 .col > p { color:var(--dim); font-size:12px; margin:0 0 10px; min-height:32px; }
 .card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:12px; margin-bottom:12px; }
 .head { display:flex; justify-content:space-between; gap:8px; font-size:12px; color:var(--dim); margin-bottom:8px; }
 .badge { padding:1px 7px; border-radius:99px; border:1px solid var(--line); font-size:11px; }
 .ok { color:var(--ok); border-color:currentColor; } .bad { color:var(--bad); border-color:currentColor; }
 .name { font-weight:600; font-size:15px; } .lat { color:var(--dim); font-style:italic; font-size:12px; }
 dl { display:grid; grid-template-columns:auto 1fr; gap:2px 10px; margin:8px 0 0; font-size:13px; }
 dt { color:var(--dim); } dd { margin:0; }
 pre { white-space:pre-wrap; word-break:break-word; background:transparent; border:1px dashed var(--line);
       border-radius:8px; padding:8px; font:12px/1.45 ui-monospace,SFMono-Regular,Menlo,monospace; margin:10px 0 0; }
 .txt { white-space:pre-wrap; font-size:13px; max-height:320px; overflow:auto; }
 table { border-collapse:collapse; width:100%; margin-top:8px; font-size:13px; background:var(--card); }
 th,td { border:1px solid var(--line); padding:6px 9px; text-align:left; }
 th { color:var(--dim); font-weight:500; }
 .err { color:var(--bad); font-size:12px; margin-top:6px; }
 h2.section { font-size:15px; margin:28px 0 0; }
</style></head><body>
<h1>Агент поиска информации о рыбах</h1>
<div class="sub" id="sub">…</div>
<div class="cols" id="cols"></div>
<h2 class="section">Серии одинаковых запросов</h2>
<div id="series"></div>
<script>
const esc = s => String(s ?? "").replace(/[&<>]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;"}[c]));
const rng = r => r && (r.min !== null || r.max !== null) ? `${r.min ?? "?"} – ${r.max ?? "?"}` : "—";

function card(e) {
  const badges = [
    `<span class="badge ${e.is_json ? "ok" : "bad"}">${e.is_json ? "JSON" : "не JSON"}</span>`,
    `<span class="badge ${e.schema_ok ? "ok" : "bad"}">${e.schema_ok ? "схема" : "схема ✕"}</span>`,
    e.finish_reason === "length" ? `<span class="badge bad">обрыв по лимиту</span>` : "",
  ].join(" ");
  let body;
  if (e.fact && e.fact.status === "ok") {
    const f = e.fact;
    body = `<div class="name">${esc(f.name)}</div><div class="lat">${esc(f.scientific_name)}</div>
      <dl><dt>длина, см</dt><dd>${rng(f.length_cm)}</dd>
          <dt>вес, кг</dt><dd>${rng(f.weight_kg)}</dd>
          <dt>ареал</dt><dd>${esc((f.habitat||[]).join(", ")) || "—"}</dd>
          <dt>питание</dt><dd>${esc((f.diet||[]).join(", ")) || "—"}</dd>
          <dt>описание</dt><dd>${esc(f.summary)}</dd></dl>
      <pre>${esc(e.text)}</pre>`;
  } else if (e.fact) {
    body = `<div class="name">запрос отклонён</div><dl><dt>причина</dt><dd>${esc(e.fact.reason)}</dd></dl>
      <pre>${esc(e.text)}</pre>`;
  } else {
    body = `<div class="txt">${esc(e.text)}</div>`;
  }
  const errs = (e.errors||[]).length ? `<div class="err">${e.errors.map(esc).join("<br>")}</div>` : "";
  return `<div class="card"><div class="head"><span>«${esc(e.query)}» · ${esc(e.ts||"")}</span>
    <span>${e.completion_tokens ?? "?"} токенов · ${((e.elapsed_ms||0)/1000).toFixed(1)} с</span></div>
    <div class="head">${badges}</div>${body}${errs}</div>`;
}

function seriesTable(rows) {
  if (!rows.length) return `<p class="sub">Серий пока нет: запустите <code>uv run python -m fishfacts.series</code>.</p>`;
  const head = ["профиль","запрос","прогонов","JSON","без чистки","схема","разных форм","токенов","слов в summary","остановка"];
  const body = rows.map(s => `<tr><td>${esc(s.profile)}</td><td>${esc(s.query)}</td><td>${s.runs}</td>
    <td>${(s.json_share*100).toFixed(0)}%</td><td>${(s.clean_json_share*100).toFixed(0)}%</td>
    <td>${(s.schema_share*100).toFixed(0)}%</td><td>${s.distinct_shapes}</td>
    <td>${s.avg_completion_tokens}</td><td>${s.max_summary_words}</td>
    <td>${esc(Object.entries(s.finish_reasons).map(([k,v])=>k+"×"+v).join(", "))}</td></tr>`).join("");
  return `<table><tr>${head.map(h=>`<th>${h}</th>`).join("")}</tr>${body}</table>`;
}

async function refresh() {
  const data = await (await fetch("/data.json")).json();
  document.getElementById("sub").textContent =
    `обновлено ${data.generated_at} · журнал: ${data.journal || "не найден"}`;
  document.getElementById("cols").innerHTML = data.columns.map(c => `<div class="col">
      <h2>${esc(c.profile)}</h2><p>${esc(c.description)}</p>
      ${c.entries.length ? c.entries.map(card).join("") : `<div class="card sub">ответов пока нет</div>`}
    </div>`).join("");
  document.getElementById("series").innerHTML = seriesTable(data.series);
}
refresh(); setInterval(refresh, 2000);
</script></body></html>
"""


class Handler(BaseHTTPRequestHandler):
    journal: Path | None = None

    def do_GET(self) -> None:  # noqa: N802 — имя задано базовым классом
        if self.path.startswith("/data.json"):
            payload = json.dumps(collect(self.journal), ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
        elif self.path in ("/", "/index.html"):
            payload = PAGE.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
        else:
            self.send_error(404)
            return
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, *args) -> None:  # тишина в консоли: страница опрашивает сервер раз в 2 с
        pass


def main() -> None:
    parser = argparse.ArgumentParser(prog="fishfacts.web", description="Страница сравнения ступеней контроля")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--journal", help="путь к журналу прогонов myharness")
    args = parser.parse_args()

    Handler.journal = find_journal(args.journal)
    if Handler.journal is None:
        print("Журнал не найден — карточки появятся после первых запросов в myharnessfish.")
    else:
        print(f"Журнал: {Handler.journal}")
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Страница: http://127.0.0.1:{args.port}  (Ctrl+C — остановить)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
