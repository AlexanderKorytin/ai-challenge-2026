#!/bin/sh
# Живой чат дня 25 из песочницы: инструмент берётся из main, состояние и профиль — здесь.
# Настройки НЕ трогаются: каталог настроек остаётся рабочим, ключ читается оттуда.
# Имя переменной латиницей намеренно: /bin/sh портит кириллические имена переменных.
# Без доводов — чат на экране Sticky Facts; с доводами — как есть, например:
#   ./запуск.sh --batch наряд.json
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state"
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_PROFILE=чат
export MYHARNESS_JOURNAL="$ROOT/myharness-journal.jsonl"
cd "$ROOT"
exec uv run --project "$HOME/challenge/main/myharness" myharness "$@"
