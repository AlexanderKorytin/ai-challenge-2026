#!/bin/sh
# Живой прогон дня 15 из песочницы: инструмент берётся из main, состояние — здесь.
# Настройки НЕ трогаются: каталог настроек остаётся рабочим, ключ читается оттуда.
КОРЕНЬ="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$КОРЕНЬ/state"
export MYHARNESS_PROFILES="$КОРЕНЬ/profiles"
export MYHARNESS_PROFILE=ведущий
cd "$КОРЕНЬ"
exec uv run --project "$HOME/challenge/main/myharness" myharness "$@"
