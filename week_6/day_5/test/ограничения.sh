#!/bin/sh
# День 30: два базовых ограничения службы — окно контекста и частота запросов.
IP="$(ipconfig getifaddr en0)"; U="http://$IP:8780/v1/chat/completions"
T="$(grep ^LLM_TOKEN_guest= "$HOME/.config/llm-gateway/tokens.env" | cut -d= -f2)"
echo "1. Окно контекста: запрос больше 32 768 токенов → $U"
python3 -c "
import json; print(json.dumps({'model':'qwen3.5:9b-q4_K_M-32k','max_tokens':5,'messages':[{'role':'user','content':'слово '*40000}]}))" \
  | curl -s -m 60 -H "Authorization: Bearer $T" -H "Content-Type: application/json" "$U" -d @- -w "\n   код ответа: %{http_code}\n" \
  | python3 -c "
import json,sys
t=sys.stdin.read().rsplit('\n',2)
try: print('  ', json.loads(t[0])['error']['code'], '—', json.loads(t[0])['error']['message'])
except Exception: print('  ', t[0][:120])
print(t[1])"
echo
echo "2. Частота: 14 коротких запросов одного клиента разом при пределе 12 в минуту"
for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14; do
  curl -s -m 120 -o /dev/null -D - -H "Authorization: Bearer $T" -H "Content-Type: application/json" "$U" \
    -d '{"model":"qwen3.5:9b-q4_K_M-32k","reasoning_effort":"none","max_tokens":3,"messages":[{"role":"user","content":"да?"}]}' \
    | awk 'NR==1{c=$2} tolower($1)=="retry-after:"{r=$2} END{gsub("\r","",r); print c (r?" Retry-After="r:"")}' &
done | sort | uniq -c | awk '{printf "   код %s%s — %s запросов\n", $2, ($3?" ("$3" с)":""), $1}'
wait
