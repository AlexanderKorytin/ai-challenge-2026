#!/bin/sh
# День 31 из песочницы: инструмент, индекс помощника и проект для вопросов берутся из main.
# Имена переменных латиницей намеренно: /bin/sh портит кириллические имена.
#   ./запуск.sh окно [модель]        — myharness в этом терминале; вопросы задаются командой /help
#   ./запуск.sh сценарий <модель>    — семь вопросов из вопросы.txt сами набираются в окне
#   ./запуск.sh наряд cloud|local    — четыре вопроса пакетным режимом (без инструментов MCP)
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_STATE_DIR="$ROOT/state"
cd "$ROOT"
case "$1" in
  окно) export MYHARNESS_JOURNAL="$ROOT/журнал-окно.jsonl"
        exec uv run --project "$HOME/challenge/main/myharness" myharness ${2:+--model "$2"} ;;
  сценарий) [ -n "$2" ] || { echo "запуск.sh сценарий <модель>" >&2; exit 1; }
        NAME=$(printf %s "$2" | tr '/:' '--')
        export MYHARNESS_JOURNAL="$ROOT/журнал-сценарий-$NAME.jsonl"
        rm -f "$MYHARNESS_JOURNAL"
        python3 окно.py "$2" "$ROOT/вопросы.txt" ;;
  наряд) export MYHARNESS_PROFILES="$ROOT/../profiles" MYHARNESS_JOURNAL="$ROOT/журнал-наряд.jsonl"
        exec uv run --project "$HOME/challenge/main/myharness" myharness --batch "наряд-$2.json" ;;
  *) echo "запуск.sh окно|сценарий|наряд" >&2; exit 1 ;;
esac
