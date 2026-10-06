#!/bin/sh
# Сценарий показа для видеоотчёта. Запускать из любого каталога, после `uv sync` в папке дня.
cd "$(dirname "$0")/.." || exit 1
[ -x .venv/bin/калории ] || { echo "Нет .venv: сначала выполни uv sync в $(pwd)"; exit 1; }
step() { printf '\n\033[1m$ калории %s\033[0m\n' "$*"; .venv/bin/калории "$@"; }
ollama ps
step борщ со свининой
step арбуз
step табуретка
step 450
printf '\n\033[1m$ OLLAMA_HOST=192.168.1.5 калории борщ\033[0m\n'; OLLAMA_HOST=192.168.1.5 .venv/bin/калории борщ
exit 0
