#!/bin/sh
# День 29, живой показ: окно myharness из песочницы. Старт — профиль default и модель как есть;
# дальше в окне: /profile хозяин-таверны и /model ollama/tavern-keeper. Журнал показа в git не идёт.
# Реплику партии разворачивает местная модель: поиск в облако не ходит.
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state-показа"
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_PROFILE=default
export MYHARNESS_JOURNAL="$ROOT/state-показа/журнал-показа.jsonl"
export RAG_REWRITE_MODEL=ollama/tavern-keeper
cd "$ROOT"
exec uv run --project "$HOME/challenge/main/myharness" myharness --model ollama/qwen3.5:9b-q4_K_M
