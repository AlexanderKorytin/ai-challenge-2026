#!/bin/sh
# День 28, живой показ: окно myharness из песочницы с профилем rag-local.
# Модель выбирается в окне командой /model. Журнал показа — отдельный файл, в git не идёт.
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state"
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_PROFILE=rag-local
export MYHARNESS_JOURNAL="$ROOT/state/журнал-показа.jsonl"
cd "$ROOT"
exec uv run --project "$HOME/challenge/main/myharness" myharness
