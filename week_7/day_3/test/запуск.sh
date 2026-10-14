#!/bin/sh
# День 33 из песочницы: инструмент из main, индекс поддержки и сервер support — из ветки дня.
# Кто спрашивает, задаёт SUPPORT_USER (номер пользователя): сервер support отдаёт данные только его.
# Имена переменных латиницей намеренно: /bin/sh портит кириллические имена.
#   ./запуск.sh окно <U-xx> [модель]  — myharness с профилем «поддержка» под пользователем, история с нуля
#   ./запуск.sh сценарий <модель>     — каждая строка реплики.txt («U-xx<TAB>реплика») — своё окно под своим пользователем
ROOT="$(cd "$(dirname "$0")" && pwd)"
export MYHARNESS_PROFILES="$ROOT/../profiles"
# Разговор и память myharness хранит по паре «каталог + профиль», а не по пользователю. Свой каталог
# состояния на пользователя: иначе Борис продолжил бы разговор Анны с её тикетом.
cd "$ROOT"
case "$1" in
  окно) [ -n "$2" ] || { echo "запуск.sh окно <U-xx> [модель]" >&2; exit 1; }
        export SUPPORT_USER="$2" MYHARNESS_STATE_DIR="$ROOT/state/$2" MYHARNESS_JOURNAL="$ROOT/журнал-окно.jsonl"
        rm -rf "$MYHARNESS_STATE_DIR"  # окно начинает с чистой истории: прошлый случай не подсказывает новому
        exec uv run --project "$HOME/challenge/main/myharness" myharness --profile поддержка ${3:+--model "$3"} ;;
  сценарий) [ -n "$2" ] || { echo "запуск.sh сценарий <модель>" >&2; exit 1; }
        NAME=$(printf %s "$2" | tr '/:' '--')
        export MYHARNESS_JOURNAL="$ROOT/журнал-сценарий-$NAME.jsonl"
        rm -f "$MYHARNESS_JOURNAL"
        ONE="$ROOT/state-реплика.txt"
        while IFS="	" read -r USER_ID LINE; do
          [ -n "$USER_ID" ] || continue
          # Свой пользователь и чистая история на каждый случай: история прошлого случая подсказала бы ответ.
          rm -rf "$ROOT/state/$USER_ID"
          printf '%s\n' "$LINE" > "$ONE"
          SUPPORT_USER="$USER_ID" MYHARNESS_STATE_DIR="$ROOT/state/$USER_ID" python3 окно.py "$2" "$ONE" < /dev/null || exit 1
        done < "$ROOT/реплики.txt"
        rm -f "$ONE" ;;
  *) echo "запуск.sh окно|сценарий" >&2; exit 1 ;;
esac
