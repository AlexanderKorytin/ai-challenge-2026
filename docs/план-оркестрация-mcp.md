# План: оркестрация MCP (день 20)

Дизайн: `docs/оркестрация-mcp.md` (утверждён 2026-09-25). Состояние:
`docs/состояние-оркестрация-mcp.md`.

## Карта файлов

Всё в ветке `w4d5` (`git worktree add ~/challenge/w4d5 -b w4d5 main` после фиксации плана в
`main`), каталог `week_4/day_5/`:

| Файл | За что отвечает |
|---|---|
| `README.md` | постановка, сценарий, устройство песочницы, как повторить |
| `.gitignore` | `test/справки/` — вложенный репозиторий песочницы, `test/myharness-journal.jsonl` не игнорируется |
| `test/.mcp.json` | четыре сервера: `cbr`, `pipeline`, `filesystem`, `git` |
| `test/.claude/settings.json` | `permissions.allow` и `deny` точными именами |
| `test/profiles/справочник.json`, `.md` | профиль дня: инструкция без подсказки маршрута |
| `test/подготовить.sh` | заводит `test/справки/` заново: `git init`, первая фиксация `README.md` песочницы |
| `test/живой_обмен.py` | одна просьба без окна: `Agent.exchange` с профилем `справочник` и набором `mcp_tools.Соединения`, печать вызовов |
| `test/след.py` | сверка выбора, порядка и переноса данных по записи журнала |

## Изменения требований

Не задевает: `myharness` не меняется; поведение маршрутизации и следа уже описано
`openspec/specs/mcp/` («Вызов инструмента», «Разрешение модели», «Соединение на сеанс») и
`openspec/specs/journal/` (поле `звенья`).

## Шаг 1. Песочница: четыре сервера подключены, модели видны ровно разрешённые инструменты

**Файлы**: `week_4/day_5/{README.md,.gitignore}`, `test/.mcp.json`, `test/.claude/settings.json`,
`test/profiles/справочник.{json,md}`, `test/подготовить.sh`
**Зависит от**: ничего
**Ход**: быстрый — песочница, читателей за границей шага нет.
**Требование**: не про myharness
**Проверка**: `bash подготовить.sh` → `справки/.git` есть, одна фиксация. Затем из `test/`
короткий скрипт Python через `mcp_tools.Соединения().набор(Path.cwd())` печатает имена
предложенных функций и жалобы: ровно 13 имён (таблица ниже), жалоб нет. Парная проверка:
`mcp__git__git_reset` и `mcp__filesystem__edit_file` в наборе отсутствуют, хотя серверы их
объявляют (виден полный список через `/mcp` в окне).
**Содержание**:
- `.mcp.json` (`СПРАВКИ` = абсолютный путь `~/challenge/w4d5/week_4/day_5/test/справки`):
  - `cbr`: `http`, `https://188.120.230.58/mcp`, `Authorization: Bearer ${CBR_MCP_TOKEN}`;
  - `pipeline`: stdio, `uv run --project <main>/mcp_servers/pipeline <main>/mcp_servers/pipeline/server.py`,
    `env: {"PIPELINE_OUT_DIR": "<СПРАВКИ>/выжимки"}`;
  - `filesystem`: stdio, `npx -y @modelcontextprotocol/server-filesystem@<версия> <СПРАВКИ>`;
    версию закрепить последнюю из `npm view @modelcontextprotocol/server-filesystem version`;
  - `git`: stdio, `uvx --from mcp-server-git==<версия> mcp-server-git --repository <СПРАВКИ>`;
    версия — последняя с PyPI, не старше 2025.12.18.
- `settings.json`: `allow` — `mcp__cbr__get_rate`, `mcp__cbr__get_rate_dynamics`,
  `mcp__pipeline__search`, `mcp__pipeline__summarize`, `mcp__pipeline__saveToFile`,
  `mcp__filesystem__write_file`, `mcp__filesystem__read_text_file`,
  `mcp__filesystem__list_directory`, `mcp__filesystem__list_allowed_directories`,
  `mcp__git__git_status`, `mcp__git__git_add`, `mcp__git__git_commit`, `mcp__git__git_log`;
  `deny` — `mcp__filesystem__edit_file`, `mcp__filesystem__move_file`, `mcp__git__git_reset`,
  `mcp__git__git_checkout`, `mcp__git__git_create_branch`, `mcp__git__git_diff`. Имена
  инструментов filesystem сверяются по `/mcp` до фиксации: разошлись — правится список.
- `справочник.json`: `{"system_file": "справочник.md", "keep_history": true}`; `справочник.md` —
  три правила из дизайна (запись и фиксация в этом каталоге разрешены заранее — не спрашивать;
  работа кончена фиксацией, ответ называет её хэш; после «ошибки инструмента» тот же вызов с теми
  же доводами не повторять). Ролей `saveToFile` и `write_file` и порядка вызовов инструкция не
  называет.
- `подготовить.sh`: удаляет `справки/` (только этот путь, проверка, что он внутри `test/`),
  `git init`, `README.md` одной строкой, `git commit` с автором песочницы.

## Шаг 2. Длинная цепочка на одну просьбу и сверка её следа

**Файлы**: `test/живой_обмен.py`, `test/след.py`
**Зависит от**: шаг 1 (не от интерфейса: обмен идёт через `Agent.exchange`, как `живой_обмен.py`
дня 19)
**Ход**: быстрый — песочница, читателей за границей шага нет.
**Требование**: не про myharness
**Проверка**: `bash подготовить.sh && uv run --project ~/challenge/main/myharness python
живой_обмен.py "<просьба из дизайна>"`, затем `python3 след.py` — все пункты «ок», код 0.
Парная проверка непустоты: та же запись с выкинутым вызовом `git_commit` и его результатом
(копия журнала во временном файле, ключ `--журнал`) — «ПРОВАЛ» пунктов 1 и 4 и код 1. Затем тот же сценарий в окне `myharness` глазами — для видео.
**Содержание**:
- `живой_обмен.py` — по образцу дня 19, но профиль берётся `profiles.load("справочник")`
  (поиск идёт по `profiles/` рабочего каталога); печать `→ вызов` / `← результат`; своей сверки
  нет — её делает `след.py` по журналу, одинаково для прогона скриптом и прогона в окне.
- `след.py [--журнал ПУТЬ] [--run-id ID]`: берёт последнюю запись с полем `звенья` (или с
  данным `run_id`) из `./myharness-journal.jsonl`. Из звеньев строит список вызовов
  `(n, сервер, инструмент, доводы, результат, ошибка)`: `tool_calls` звена `assistant` и
  результат `tool` по `tool_call_id`; сервер и инструмент — разбором `mcp__<сервер>__<инструмент>`;
  ошибка — результат начинается с «ошибка инструмента». Печатает таблицу следа, затем проверки:
  1. выбор: вызваны `get_rate`, `get_rate_dynamics`, `search`, `summarize`, `saveToFile`,
     `write_file`, `git_add`, `git_commit`; серверы следа ⊆ {cbr, pipeline, filesystem, git};
  2. порядок (первый успешный вызов каждого): `search < summarize < saveToFile`;
     `get_rate, get_rate_dynamics, summarize < write_file`; `saveToFile, write_file < git_add <
     git_commit`;
  3. перенос: `summarize.search_id` = `search_id` из результата `search`; `saveToFile.summary_id`
     = `summary_id` из результата `summarize`; доводы `git_add.files` покрывают путь справки
     (`write_file.path`) и путь выжимки (`saveToFile` → `path`), сравнение по
     `Path(...).resolve()` относительно `repo_path`; в `write_file.content` есть курс из результата
     `get_rate` (строкой, как прислал сервер, запятая либо точка);
  4. итог: у `git_commit` нет ошибки; хэш из его результата (первые 7 знаков) есть в `response`
     записи.
  Отказ читать журнал или отсутствие записи — сообщение и код 2.
