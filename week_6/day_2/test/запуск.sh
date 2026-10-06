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
exit 0
