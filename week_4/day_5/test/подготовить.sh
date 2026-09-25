#!/usr/bin/env bash
# Заводит песочницу справок заново: отдельный репозиторий git справки/, файлы — в справки/файлы/.
# Серверу filesystem и pipeline открыт только каталог файлы/: будь им виден корень репозитория,
# модель могла бы переписать .git/config (core.fsmonitor), и ближайший git_status исполнил бы
# чужую команду. Трогает только каталог справки/ рядом с этим файлом.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
sandbox="$here/справки"
rm -rf "$sandbox"
mkdir -p "$sandbox/файлы"
cd "$sandbox"
git init -q
# Фиксации модели подписывает песочница, а не глобальный адрес владельца — он попал бы на видео.
git config user.name "песочница"
git config user.email "sandbox@localhost"
echo "# Справки — песочница дня 20" > README.md
git add README.md
git commit -qm "песочница справок"
echo "готово: $sandbox ($(git log --oneline | wc -l | tr -d ' ') фиксация)"
