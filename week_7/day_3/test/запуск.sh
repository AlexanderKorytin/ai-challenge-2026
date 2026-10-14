#!/bin/sh
# День 33 из песочницы: инструмент из main, индекс поддержки и сервер support — из ветки дня.
# Имена переменных латиницей намеренно: /bin/sh портит кириллические имена.
#   ./запуск.sh окно [модель]        — myharness с профилем «поддержка» в этом терминале, история с нуля
#   ./запуск.sh сценарий <модель>    — реплики из реплики.txt сами набираются в окне
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state" MYHARNESS_PROFILES="$ROOT/../profiles"
cd "$ROOT"
case "$1" in
  окно) export MYHARNESS_JOURNAL="$ROOT/журнал-окно.jsonl"
        rm -rf "$ROOT/state"  # окно начинает с чистой истории: прошлый случай не подсказывает новому
        exec uv run --project "$HOME/challenge/main/myharness" myharness --profile поддержка ${2:+--model "$2"} ;;
  сценарий) [ -n "$2" ] || { echo "запуск.sh сценарий <модель>" >&2; exit 1; }
        NAME=$(printf %s "$2" | tr '/:' '--')
        export MYHARNESS_JOURNAL="$ROOT/журнал-сценарий-$NAME.jsonl"
        rm -f "$MYHARNESS_JOURNAL"
        # Профиль продолжает последнюю сессию: без чистки прошлый прогон подсказал бы ответ первой реплике.
        rm -rf "$ROOT/state"
        python3 окно.py "$2" "$ROOT/реплики.txt" ;;
  *) echo "запуск.sh окно|сценарий" >&2; exit 1 ;;
esac
