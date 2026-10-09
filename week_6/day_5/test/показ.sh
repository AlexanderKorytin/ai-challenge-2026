#!/bin/sh
# День 30, живой показ: окно myharness, чат с местной моделью через шлюз на этом Маке.
# Довод — имя клиента шлюза (mac либо guest): у каждого свой токен и своя память разговора.
# Два окна рядом с разными клиентами шлют запросы одновременно.
# Старт сразу с местной моделью (довод --model): шапка называет её с первой секунды.
# Настройки довод не меняет.
ROOT="$(cd "$(dirname "$0")" && pwd)"
WHO="${1:-mac}"
export MYHARNESS_STATE_DIR="$ROOT/state-показа/$WHO"
export MYHARNESS_JOURNAL="$ROOT/state-показа/журнал-$WHO.jsonl"
# Профиль дня: рассуждения выключены (ответ за секунды), поиска по базе знаний нет.
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_PROFILE=служба
export OLLAMA_HOST=http://127.0.0.1:8780
OLLAMA_API_KEY="$(grep "^LLM_TOKEN_$WHO=" "$HOME/.config/llm-gateway/tokens.env" | cut -d= -f2)"
export OLLAMA_API_KEY
cd "$ROOT"
exec uv run --project "$HOME/challenge/main/myharness" myharness --model ollama/qwen3.5:9b-q4_K_M-32k
