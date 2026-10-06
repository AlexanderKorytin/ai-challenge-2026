#!/bin/sh
# Запускает утилиту в песочнице macOS: исходящие соединения разрешены только на этот компьютер.
# Довод — запрос к утилите. Вторая команда показывает, что запрет настоящий.
cd "$(dirname "$0")/.." || exit 1
RULES='(version 1)(allow default)(deny network-outbound)(allow network-outbound (remote ip "localhost:*"))(allow network-outbound (remote unix-socket))'
echo "— утилита в песочнице без выхода в сеть:"
sandbox-exec -p "$RULES" .venv/bin/калории "$@"
echo "— тот же запрет, запрос наружу (должен упасть):"
sandbox-exec -p "$RULES" curl -s -m 5 -o /dev/null -w 'код %{http_code}\n' https://api.deepseek.com || echo "соединение запрещено"
