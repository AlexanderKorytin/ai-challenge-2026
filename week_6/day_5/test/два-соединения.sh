#!/bin/sh
# Два клиента с разными токенами шлют по запросу чата одновременно на сетевой адрес Мака.
IP="$(ipconfig getifaddr en0)"; U="http://$IP:8780/v1/chat/completions"
T1="$(grep ^LLM_TOKEN_mac= "$HOME/.config/llm-gateway/tokens.env" | cut -d= -f2)"
T2="$(grep ^LLM_TOKEN_guest= "$HOME/.config/llm-gateway/tokens.env" | cut -d= -f2)"
ask() { # имя клиента, токен, вопрос
  curl -s -m 180 -H "Authorization: Bearer $2" -H "Content-Type: application/json" "$U" \
    -w "\n%{http_code} %{time_total}" \
    -d "{\"model\":\"qwen3.5:9b-q4_K_M-32k\",\"reasoning_effort\":\"none\",\"max_tokens\":120,\"messages\":[{\"role\":\"user\",\"content\":\"$3\"}]}" \
  | python3 -c "
import json,sys
t=sys.stdin.read().rsplit('\n',1); код,сек=t[1].split()
try: ответ=json.loads(t[0])['choices'][0]['message']['content'].replace('\n',' ')[:100]
except Exception: ответ=t[0][:100]
print(f'[$1] код {код}, {float(сек):.1f} с — {ответ}')"
}
echo "Два соединения одновременно → $U"
ask mac "$T1" "Что такое очередь запросов? Одно предложение." &
ask guest "$T2" "Чем поток отличается от процесса? Одно предложение." &
wait
echo "Журнал шлюза:"
tail -2 "$HOME/.config/llm-gateway/journal.jsonl" | python3 -c "
import json,sys
for l in sys.stdin:
    r=json.loads(l); print('  ', r['время'][11:19], r['клиент'], 'код', r['код'], r['мс'], 'мс, токенов выхода', r.get('выход'))"
