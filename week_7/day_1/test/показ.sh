#!/bin/sh
# День 31, живой показ: окно myharness в песочнице, вопросы задаются командой /help.
# Память разговора и журнал показа лежат отдельно от прогонов сценария.
# Каталог настроек можно увести переменной MYHARNESS_CONFIG_DIR: команда /model пишет выбор в настройки.
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state/показ"
export MYHARNESS_JOURNAL="$ROOT/журнал-окно.jsonl"
cd "$ROOT"
exec uv run --project "$HOME/challenge/main/myharness" myharness --model deepseek-v4-flash
