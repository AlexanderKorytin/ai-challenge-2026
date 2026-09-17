#!/bin/sh
# Показ дня 14: инструмент из main, состояние — только в песочнице дня (глобальные инварианты,
# карточка и задача не попадают в настоящую память), ключ — из настоящих настроек.
cd "$(dirname "$0")"
export MYHARNESS_STATE_DIR="$PWD/state"
exec uv run --project "$HOME/challenge/main/myharness" myharness --profile архитектор
