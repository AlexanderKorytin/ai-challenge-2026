#!/bin/bash
# Подготовка к записи ролика. Запускать перед КАЖДЫМ дублем.
#
# Два состояния переживают запуск harness и ломают дубль по-разному:
#
#   1. сохранённый разговор пары «папка + профиль» — поднимается при выборе профиля.
#      У профиля «стог» запас окна 403 токена, и любая поднятая пара отправляет первый же
#      вопрос за потолок;
#   2. последний выбранный профиль в настройках — harness стартует с ним. После прошлого
#      дубля это «стог», и обычный первый вопрос уходит с документом на миллион токенов.
#
# Оба лечатся здесь. Журнал прогонов НЕ трогаем: по нему строится таблица в последней сцене.
#
# Имена переменных латиницей нарочно: bash 3.2, который стоит в macOS по умолчанию,
# кириллических имён не понимает и падает на первой же строке.
set -e
here="$(cd "$(dirname "$0")" && pwd)"
key="-Users-aleksandrkorytin-challenge-w2d3-week_2-day_3-test"

rm -rf "$HOME/.local/state/myharness/projects/$key"
python3 - <<'PY'
import json, pathlib
p = pathlib.Path.home() / ".config/myharness/config.json"
d = json.loads(p.read_text(encoding="utf-8"))
d["profile"] = "default"
d["model"] = "deepseek-v4-flash"
p.write_text(json.dumps(d, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
PY

echo "готово к дублю:"
echo "  · сохранённых разговоров дня нет"
echo "  · профиль при старте: default, модель: deepseek-v4-flash"
echo "  · записей в журнале: $(wc -l < "$here/myharness-journal.jsonl" 2>/dev/null || echo 0)"
echo
echo "запускать: cd $here && myharness"
