#!/bin/bash
# Установка игрового агента на Мак.
#
#   установить.sh приложение — «Игровой агент.app» в ~/Applications. Первый запуск приложения
#                              поднимает значок и веб; после опроса вкуса значок сам вызывает
#                              режим «службы».
#   установить.sh службы     — четыре службы launchd: веб и значок (держатся живыми, стартуют при
#                              входе в систему), задание недели (понедельник 10:00) и задание
#                              дня (каждый день 11:00). Пропущенный за сон Мака запуск по
#                              календарю launchd делает при пробуждении. Повтор переставляет.
#   установить.sh снять      — снять службы и приложение.
set -euo pipefail
# Имена переменных и функций — латиницей: bash 3.2 на macOS не принимает иных.

CODE="$(cd "$(dirname "$0")/.." && pwd)"
LOGS="$HOME/Library/Logs/games-agent"
AGENTS="$HOME/Library/LaunchAgents"
APP="$HOME/Applications/Игровой агент.app"
UV="$(command -v uv || true)"
if [[ -z "$UV" ]]; then
  echo "Нужен uv — менеджер окружений Python. Поставьте его командой:" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  echo "затем откройте новое окно Терминала и запустите установку снова." >&2
  exit 1
fi
SEARCH_PATH="$(dirname "$UV"):/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
DOMAIN="gui/$(id -u)"
SERVICES=(web menubar week day)

remove_service() {
  launchctl bootout "$DOMAIN/local.games-agent.$1" 2>/dev/null || true
  # bootout возвращается раньше, чем служба снята, и bootstrap сразу после него отвечает
  # «Input/output error» (находка просмотра). Ждём, пока launchd её забудет — до 10 с.
  for _ in $(seq 50); do
    launchctl print "$DOMAIN/local.games-agent.$1" >/dev/null 2>&1 || break
    sleep 0.2
  done
  rm -f "$AGENTS/local.games-agent.$1.plist"
}

install_service() {  # имя программа доводы расписание
  local file="$AGENTS/local.games-agent.$1.plist"
  "$UV" run --project "$CODE" python - "$CODE/deploy/служба.plist.in" "$file" "$1" "$2" "$3" "$4" "$UV" "$CODE" "$SEARCH_PATH" "$LOGS" <<'PY'
import sys
from xml.sax.saxutils import escape
шаблон, файл, имя, программа, доводы, расписание, uv, код, путь, журналы = sys.argv[1:]
текст = open(шаблон, encoding="utf-8").read()
# Пути экранируются: `&` или `<` в имени каталога сломали бы файл службы.
for ключ, значение in {
    "@ИМЯ@": escape("local.games-agent." + имя), "@UV@": escape(uv), "@КОД@": escape(код),
    "@ПРОГРАММА@": escape(программа), "@ПУТЬ@": escape(путь), "@ЖУРНАЛЫ@": escape(журналы),
    "@КОРОТКО@": escape(имя), "@ДОВОДЫ@": доводы, "@РАСПИСАНИЕ@": расписание,
}.items():
    текст = текст.replace(ключ, значение)
open(файл, "w", encoding="utf-8").write(текст)
PY
  plutil -lint "$file" >/dev/null
  launchctl bootstrap "$DOMAIN" "$file"
}

services() {
  mkdir -p "$LOGS" "$AGENTS"
  (cd "$CODE" && "$UV" sync -q)
  (cd "$CODE/../../mcp_servers/steam" && "$UV" sync -q)
  for s in "${SERVICES[@]}"; do remove_service "$s"; done
  KEEP="  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
"
  install_service web web.py "" "$KEEP"
  install_service menubar menubar.py "" "$KEEP"
  install_service week jobs.py "    <string>week</string>
" "  <key>StartCalendarInterval</key>
  <dict><key>Weekday</key><integer>1</integer><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
"
  install_service day jobs.py "    <string>day</string>
" "  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>11</integer><key>Minute</key><integer>0</integer></dict>
"
  # Первые предложения — сразу, а не в 11:00 завтра (итоговый просмотр): задание дня само
  # собирает RAG, если его ещё нет; неделя — календарь будущих выпусков.
  launchctl kickstart "$DOMAIN/local.games-agent.day" 2>/dev/null || true
  launchctl kickstart "$DOMAIN/local.games-agent.week" 2>/dev/null || true
  echo "службы поставлены; веб — http://127.0.0.1:8791, журналы — $LOGS"
}

make_app() {
  (cd "$CODE" && "$UV" sync -q)
  (cd "$CODE/../../mcp_servers/steam" && "$UV" sync -q)
  rm -rf "$APP"
  mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources" "$LOGS"
  cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Игровой агент</string>
  <key>CFBundleIdentifier</key><string>local.games-agent</string>
  <key>CFBundleExecutable</key><string>games-agent</string>
  <key>CFBundleIconFile</key><string>значок</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSUIElement</key><true/>
</dict></plist>
PLIST
  # Запускающая программа не живёт долго: macOS уже запущенное приложение лишь «активирует», и
  # второй щелчок ничего бы не делал (живой сценарий пользователя). Поэтому каждый щелчок —
  # новый короткий запуск: веб отвечает — открыть его; иначе поднять значок отдельно и выйти.
  cat > "$APP/Contents/MacOS/games-agent" <<SH
#!/bin/bash
export PATH="$SEARCH_PATH"
if curl -s -m 2 -o /dev/null http://127.0.0.1:8791/api/state; then exec open http://127.0.0.1:8791; fi
if [[ -f "$AGENTS/local.games-agent.menubar.plist" ]]; then
  launchctl kickstart "$DOMAIN/local.games-agent.web" 2>/dev/null || true
  launchctl kickstart "$DOMAIN/local.games-agent.menubar" 2>/dev/null || true
  for _ in \$(seq 40); do curl -s -m 1 -o /dev/null http://127.0.0.1:8791/api/state && break; sleep 0.5; done
  exec open http://127.0.0.1:8791
fi
if ! pgrep -f "$CODE/menubar.py" >/dev/null; then
  nohup "$UV" run --project "$CODE" python "$CODE/menubar.py" --open >>"$LOGS/app.log" 2>&1 &
fi
exit 0
SH
  chmod +x "$APP/Contents/MacOS/games-agent"
  "$UV" run --project "$CODE" python "$CODE/deploy/значок.py" "$APP/Contents/Resources/значок.icns"
  echo "приложение: $APP"
}

case "${1:-}" in
  приложение) make_app ;;
  службы) services ;;
  снять) for s in "${SERVICES[@]}"; do remove_service "$s"; done; rm -rf "$APP"; echo "сняты службы и приложение" ;;
  *) echo "запуск: установить.sh приложение|службы|снять" >&2; exit 2 ;;
esac
