#!/bin/bash
# Вызовы ssh идут через одно постоянное соединение (ControlMaster): новые соединения с Мака к
# серверам 2026-10-09 временами не устанавливались («timed out during banner exchange»).
# День 30: два устройства шлют по одному запросу чата одновременно.
# Устройство 1 — Мак. Устройство 2 — сервер challenge (другая сеть), ходит по HTTPS на внешний адрес.
# Мак ходит к шлюзу через проброс ssh: путь от домашней сети к порту 443 сервера 2026-10-09 закрылся.
# Запуск: ./два_устройства.sh <каталог для ответов>. Токены берутся с сервера и на диск не пишутся.
TOK=$(ssh -o ConnectTimeout=20 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 challenge-mcp "grep ^LLM_TOKEN_mac= /opt/challenge/llm-gateway.env | cut -d= -f2")
TOK2=$(ssh -o ConnectTimeout=20 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 challenge-mcp "grep ^LLM_TOKEN_challenge= /opt/challenge/llm-gateway.env | cut -d= -f2")
[ -n "$TOK" ] && [ -n "$TOK2" ] || { echo "нет токенов"; exit 1; }
ssh -o ConnectTimeout=20 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 -N -L 127.0.0.1:8781:127.0.0.1:8780 challenge-mcp & FWD=$!; sleep 3
B1='{"model":"qwen3.5:9b-q4_K_M-32k","reasoning_effort":"none","max_tokens":120,"messages":[{"role":"user","content":"Что такое очередь запросов? Два предложения."}]}'
B2='{"model":"qwen3.5:9b-q4_K_M-32k","reasoning_effort":"none","max_tokens":120,"messages":[{"role":"user","content":"Чем поток отличается от процесса? Два предложения."}]}'
date +"старт %H:%M:%S"
( curl -s -m 180 -w '\n[Мак] код %{http_code}, %{time_total} с\n' -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" http://127.0.0.1:8781/v1/chat/completions -d "$B1" > $1/mac.out ) &
( printf 'header = "Authorization: Bearer %s"\nheader = "Content-Type: application/json"\n' "$TOK2" | ssh -o ConnectTimeout=20 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 challenge "curl -s -m 180 -w '\n[challenge] код %{http_code}, %{time_total} с\n' -K - https://188.120.230.58/v1/chat/completions -d '$B2'" > $1/ch.out ) &
wait %2 %3
kill $FWD 2>/dev/null
for f in mac ch; do python3 - "$1/$f.out" <<'P'
import json,sys
t=open(sys.argv[1]).read().strip().rsplit("\n",1)
try:
    d=json.loads(t[0]); print(t[-1], "| токенов выхода", d["usage"]["completion_tokens"], "|", d["choices"][0]["message"]["content"].replace("\n"," ")[:110])
except Exception as e: print(t[-1][:200], "| разбор:", e)
P
done
ssh -o ConnectTimeout=20 -o ControlMaster=auto -o ControlPath=$HOME/.ssh/cm-%C -o ControlPersist=600 challenge-mcp 'tail -2 /var/lib/private/llm-gateway/journal.jsonl | python3 -c "
import json,sys
for l in sys.stdin:
    r=json.loads(l); print(r[\"время\"][11:19], r[\"клиент\"], r[\"код\"], r[\"мс\"], \"мс\")"'
