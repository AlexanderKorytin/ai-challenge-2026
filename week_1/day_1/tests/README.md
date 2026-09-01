# Проверки harness

Запускаются без ключа DeepSeek и без настоящего терминала: вместо API — поддельный клиент,
вместо терминала — труба и пустой вывод prompt_toolkit. Настройки и профили на время
проверок подменяются временным каталогом (`MYHARNESS_CONFIG_DIR`, `MYHARNESS_PROFILES`) —
без этого они писали бы в настоящий `~/.config/myharness/config.json` и затирали ключ.

```bash
cd week_1/day_1
uv run python tests/check_units.py   # профили, журнал, параметры, панель, дополнения
uv run python tests/check_app.py     # приложение целиком: клавиши, панели, ответ, журнал
```
