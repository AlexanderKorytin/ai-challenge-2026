#!/bin/sh
# День 26 из песочницы: инструмент берётся из main, состояние, профиль и журнал — здесь.
# Настройки НЕ трогаются: каталог настроек остаётся рабочим, модель задаётся доводом и в
# настройки не пишется. Имя переменной латиницей намеренно: /bin/sh портит кириллические имена.
# Без доводов — живой разговор с местной моделью; с доводами — как есть, например:
#   ./запуск.sh --batch наряд.json
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state"
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_PROFILE=местная
export MYHARNESS_JOURNAL="$ROOT/myharness-journal.jsonl"
cd "$ROOT"
if [ "$#" -eq 0 ]; then
  exec uv run --project "$HOME/challenge/main/myharness" myharness --model ollama/qwen3.5:9b-q4_K_M-32k
fi
exec uv run --project "$HOME/challenge/main/myharness" myharness "$@"
