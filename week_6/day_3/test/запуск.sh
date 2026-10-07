#!/bin/sh
# День 28 из песочницы: инструмент и индекс берутся из main, профили, журнал и состояние — здесь.
# Имена переменных латиницей намеренно: /bin/sh портит кириллические имена.
#   ./запуск.sh local   — наряд местной модели; каталог настроек БЕЗ ключа: в облако идти нечем
#   ./запуск.sh cloud   — наряд DeepSeek; каталог настроек рабочий
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state"
export MYHARNESS_PROFILES="$ROOT/../profiles"
export MYHARNESS_JOURNAL="$ROOT/myharness-journal.jsonl"
cd "$ROOT"
case "$1" in
  local) export MYHARNESS_CONFIG_DIR="$ROOT/настройки-без-ключа" ;;
  cloud) ;;
  *) echo "запуск.sh local|cloud" >&2; exit 1 ;;
esac
exec uv run --project "$HOME/challenge/main/myharness" myharness --batch "наряд-$1.json"
