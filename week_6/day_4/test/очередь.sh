#!/bin/sh
# Очередь прогонов: по одному, чтобы в памяти была одна модель разговора. Доводы — строки доводов наряд.py через «;».
# Имена латиницей: /bin/sh портит кириллические имена переменных.
cd "$(dirname "$0")/.." || exit 1
while pgrep -f "наряд.py" >/dev/null; do sleep 5; done
OLDIFS=$IFS; IFS=';'
for ARGS in $1; do
  IFS=$OLDIFS
  echo "=== $(date +%H:%M:%S) наряд.py $ARGS"
  # shellcheck disable=SC2086
  uv run -q наряд.py $ARGS 2>&1 | tail -4
  IFS=';'
done
echo "=== $(date +%H:%M:%S) очередь кончилась"
