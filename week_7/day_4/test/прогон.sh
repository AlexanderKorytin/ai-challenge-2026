#!/bin/sh
# Один прогон сценария: сброс учебного проекта, наряд, итог правок файлом diff.
# Использование: ./прогон.sh <сценарий 1|2|3> <номер прогона>
set -e
cd "$(dirname "$0")"
scenario=$1; run=$2
./сброс.sh
out="$PWD/итоги/$scenario-$run"
cd проект
MYHARNESS_JOURNAL="$out.журнал.jsonl" uv run --project ../../../../myharness myharness --batch "../наряд-$scenario.json" > "$out.вывод.txt" 2>&1
# Новые файлы — в diff тоже: «намерение добавить» без фиксации.
git add -A -N
git diff > "$out.diff"
git diff --stat
