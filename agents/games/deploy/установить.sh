#!/bin/bash
# Ставит три службы launchd игрового агента: веб (держится живым), задание недели
# (понедельник 10:00) и задание дня (каждый день 11:00). Пропущенный за сон Мака запуск по
# календарю launchd делает при пробуждении. Повторный запуск программы переставляет службы.
#
#   agents/games/deploy/установить.sh          — поставить или переставить
#   agents/games/deploy/установить.sh снять    — снять все три
set -euo pipefail

КОД="$(cd "$(dirname "$0")/.." && pwd)"
ДАННЫЕ="${GAMES_DATA_DIR:-$HOME/.local/share/games-agent}"
ЖУРНАЛЫ="$HOME/Library/Logs/games-agent"
АГЕНТЫ="$HOME/Library/LaunchAgents"
UV="$(command -v uv)"
ПУТЬ="$(dirname "$UV"):/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"
ДОМЕН="gui/$(id -u)"

снять() {
  for к in web week day; do
    launchctl bootout "$ДОМЕН/local.games-agent.$к" 2>/dev/null || true
    rm -f "$АГЕНТЫ/local.games-agent.$к.plist"
  done
}

if [[ "${1:-}" == "снять" ]]; then снять; echo "службы сняты"; exit 0; fi

mkdir -p "$ДАННЫЕ" "$ЖУРНАЛЫ" "$АГЕНТЫ"
(cd "$КОД" && "$UV" sync -q)
снять

собрать() {  # кратко программа доводы расписание
  local кратко="$1" файл="$АГЕНТЫ/local.games-agent.$1.plist"
  python3 - "$КОД/deploy/служба.plist.in" "$файл" <<PY
import sys
шаблон = open(sys.argv[1], encoding="utf-8").read()
for ключ, значение in {
    "@ИМЯ@": "local.games-agent.$кратко", "@UV@": "$UV", "@КОД@": "$КОД",
    "@ПРОГРАММА@": "$2", "@ДОВОДЫ@": """$3""", "@ДАННЫЕ@": "$ДАННЫЕ", "@ПУТЬ@": "$ПУТЬ",
    "@ЖУРНАЛЫ@": "$ЖУРНАЛЫ", "@КОРОТКО@": "$кратко", "@РАСПИСАНИЕ@": """$4""",
}.items():
    шаблон = шаблон.replace(ключ, значение)
open(sys.argv[2], "w", encoding="utf-8").write(шаблон)
PY
  plutil -lint "$файл" >/dev/null
  launchctl bootstrap "$ДОМЕН" "$файл"
}

собрать web web.py "" "  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
"
собрать week jobs.py "    <string>week</string>
" "  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
"
собрать day jobs.py "    <string>day</string>
" "  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
"
echo "службы поставлены; веб — http://127.0.0.1:8791, журналы — $ЖУРНАЛЫ"
