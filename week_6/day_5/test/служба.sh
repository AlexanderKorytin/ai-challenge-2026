#!/bin/sh
# День 30: служба на домашнем сервере. Шлюз llm_gateway работает на этом Маке и слушает сеть
# на порту 8780; Ollama остаётся на 127.0.0.1:11434 и наружу не выходит.
# Токены клиентов — ~/.config/llm-gateway/tokens.env (права 600, вне git).
# Журнал запросов — ~/.config/llm-gateway/journal.jsonl.
set -a; . "$HOME/.config/llm-gateway/tokens.env"; set +a
export OLLAMA_HOST=http://127.0.0.1:11434
export LLM_MODELS=qwen3.5:9b-q4_K_M-32k
export LLM_RATE_PER_MINUTE=12
export LLM_GATEWAY_LOG="$HOME/.config/llm-gateway/journal.jsonl"
unset OLLAMA_API_KEY
cd "$HOME/challenge/main/llm_gateway" && exec uv run python gateway.py --host 0.0.0.0 --port 8780
