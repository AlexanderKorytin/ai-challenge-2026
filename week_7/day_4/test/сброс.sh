#!/bin/sh
# Учебный проект к начальному состоянию: две фиксации — «начало» и «команда done».
# Проект живёт в test/проект/ своим хранилищем git; в ветку дня не входит (.gitignore).
set -e
cd "$(dirname "$0")"
rm -rf проект
cp -R исходник проект
cd проект
git init -q
git add .
git -c user.email=учебный@проект -c user.name=учебный commit -qm "начало: add, list, report"
cp ../после/tasks/cli.py tasks/cli.py
git add .
git -c user.email=учебный@проект -c user.name=учебный commit -qm "команда done: отметить задачу выполненной"
echo "проект сброшен: $(git log --oneline | wc -l | tr -d ' ') фиксации"
