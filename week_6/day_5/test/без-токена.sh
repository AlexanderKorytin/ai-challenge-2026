#!/bin/sh
# Запрос к службе по сетевому адресу Мака без токена: шлюз отвечает 401.
IP="$(ipconfig getifaddr en0)"
echo "Запрос без токена → http://$IP:8780/v1/models"
curl -s -m 15 -w "\nкод ответа: %{http_code}\n" "http://$IP:8780/v1/models"
echo
echo "Запрос с токеном → http://$IP:8780/v1/models"
curl -s -m 15 -w "\nкод ответа: %{http_code}\n" -H "Authorization: Bearer $(grep ^LLM_TOKEN_mac= "$HOME/.config/llm-gateway/tokens.env" | cut -d= -f2)" "http://$IP:8780/v1/models"
