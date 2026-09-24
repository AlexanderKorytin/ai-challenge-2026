# План: пайплайн MCP-инструментов (день 19)

Дизайн: `docs/пайплайн-mcp.md` (утверждён 2026-09-24, пересмотрен 2026-09-24: sampling заменён
вызовом DeepSeek сервером). Состояние: `docs/состояние-пайплайн-mcp.md`.

## Карта файлов

| Файл | За что отвечает |
|---|---|
| `mcp_servers/pipeline/pyproject.toml` | uv-проект сервера: `mcp>=2.2,<3`, `httpx2>=2.12,<3` |
| `mcp_servers/pipeline/server.py` | три инструмента, хранилища в памяти, ключ из своего файла настроек, точка входа stdio |
| `mcp_servers/pipeline/check_server.py` | проверки без сети: сессия MCP в памяти, подменные MediaWiki и DeepSeek |
| `mcp_servers/pipeline/образцы/search.json` | ответ MediaWiki, снятый одним настоящим запросом |
| `week_4/day_4/` (ветка `w4d4`) | `README.md`, `test/.mcp.json`, `test/.claude/settings.json`, `test/живой_обмен.py` |

## Договор сервера с моделью (описания инструментов)

- `search(query: str, lang: str = "ru") -> Найдено{search_id, query, lang, results: [{n, title, url, chars}]}`
- `summarize(search_id: str, focus: str | None = None) -> Выжимка{summary_id, search_id, text, sources: [{n, title, url}], model, usage}`
- `saveToFile(summary_id: str, filename: str) -> Сохранено{path, bytes, sha256, summary_id}`

Описание каждого называет, откуда брать вход, и порядок цепочки: после `search` — `summarize`,
после него — `saveToFile`.

## Изменения требований

Не задевает: `myharness` не меняется, сервер `pipeline` требований OpenSpec не имеет (как `cbr`).

## Шаг 1. Сервер pipeline: search → summarize → saveToFile с передачей по ссылке
**Файлы**: `mcp_servers/pipeline/{pyproject.toml,server.py,check_server.py,образцы/search.json}`
**Зависит от**: ничего
**Ход**: быстрый — у кода сервера нет читателя за границей шага; модель читает только описания.
**Требование**: не про myharness
**Проверка**: `cd mcp_servers/pipeline && uv run python check_server.py` — настоящий клиент MCP
поверх сервера в памяти (`mcp.shared.memory.create_client_server_memory_streams`), MediaWiki —
образец, DeepSeek — подменная функция, запоминающая запрос. Цепочка: текст запроса к DeepSeek
содержит ровно вступления из образца с номерами `[n]` в порядке выдачи; источники выжимки =
результаты `search`; файл = Markdown из выжимки, `sha256` ответа = сумма файла на диске.
Отказы: чужой `search_id` / `summary_id`; имя пустое, с `/`, `\`, `..`, начинающееся с `.`;
существующий файл (прежнее содержимое цело); нет файла ключа (ошибка с путём); `finish_reason
== "length"`; ошибка DeepSeek с ключом в тексте — ключ в ответе заменён `***`. Парная проверка
соседнего значения: имя `a.b` принимается и получает `.md`. Каталоги ключа и результата —
временные (`PIPELINE_CONFIG_DIR`, `PIPELINE_OUT_DIR`).
**Содержание**:
- `search`: `GET https://{lang}.wikipedia.org/w/api.php` с `action=query&format=json&
  formatversion=2&generator=search&gsrsearch=<query>&prop=extracts|info&exintro=1&
  explaintext=1&exlimit=max&inprop=url`; `User-Agent: myharness-pipeline/0.1
  (https://github.com/AlexanderKorytin/ai-challenge-2026)`. Порядок — по `index`. `lang` —
  только `[a-z-]+`. Пустая выдача — ошибка «ничего не найдено». Хранилище
  `_найденное: dict[str, Найденное]`, id — `uuid4().hex[:12]`.
- `summarize`: ключ — `_прочитать_настройки() -> (api_key, model)` из
  `$PIPELINE_CONFIG_DIR/config.json` либо `~/.config/pipeline-mcp/config.json`; модель по
  умолчанию `deepseek-v4-flash`. `POST https://api.deepseek.com/chat/completions` без потока:
  `messages=[system: сжать по-русски только по тексту источников, помечать [n]; user: запрос,
  focus, источники «[n] заголовок\nвступление»]`, `thinking={"type": "disabled"}`, без
  `max_tokens`. Сбой HTTP — ошибка с кодом и текстом, из которого вычищен ключ. Хранилище
  `_выжимки`. Функция обращения к DeepSeek — отдельная `_спросить_модель(ключ, модель,
  сообщения) -> (текст, модель, usage, finish_reason)`, её подменяет проверка.
- `saveToFile`: каталог `$PIPELINE_OUT_DIR` либо `Path.cwd()/"результаты"`, создаётся. Имя:
  отказы как в проверке; `.md` дописывается, если нет. `open(путь, "x")`. Содержимое: `# <запрос>`,
  дата, модель, выжимка, `## Источники` — `n. [заголовок](адрес)`. `sha256` — от записанных байт.
- Ключ переносится ведущим один раз: `~/.config/myharness/config.json` → `~/.config/pipeline-mcp/
  config.json` скриптом Python без вывода, права 600.

## Шаг 2. Живая цепочка из песочницы дня: одна просьба — три вызова — файл
**Файлы**: ветка `w4d4` (`git worktree add ~/challenge/w4d4 -b w4d4 main` после фиксации шага 1), `week_4/day_4/README.md`, `test/.mcp.json`, `test/.claude/settings.json`, `test/живой_обмен.py`
**Зависит от**: шаг 1 (не от интерфейса: обмен идёт через `state` и `Agent`, как `живой_обмен.py` дня 18)
**Ход**: быстрый — песочница, читателей за границей нет.
**Требование**: не про myharness
**Проверка**: `cd week_4/day_4/test && uv run --project ~/challenge/main/myharness python
живой_обмен.py "Найди в Википедии про Байкал, сожми и сохрани в файл baikal"` — вызовы по порядку
`search`, `summarize`, `saveToFile`, id на входе каждого совпадает с id из выхода
предыдущего; `test/результаты/baikal.md` есть, его `sha256` = ответ `saveToFile`; источники файла
= заголовки из ответа `search`. Затем тот же сценарий в окне `myharness` глазами (для видео).
**Содержание**: `.mcp.json` — `pipeline` stdio: `uv run --project <сервер> <сервер>/server.py`, где
`<сервер>` = `/Users/aleksandrkorytin/challenge/main/mcp_servers/pipeline` (не `--directory`: он
меняет текущий каталог, и файл лёг бы в каталог сервера — найдено живым прогоном шага 1); `settings.json` —
`permissions.allow`: три `mcp__pipeline__*`. `живой_обмен.py` — по образцу дня 18. README —
постановка, устройство, как повторить.

## Самопроверка

Покрытие дизайна: три инструмента, передача по ссылке, вызов DeepSeek сервером, свой файл ключа,
отказ перезаписи — шаг 1; живая автоматическая цепочка — шаг 2. Живой обмен — шаг 2, от
интерфейса не зависит. Пути записи: `PIPELINE_OUT_DIR`, `PIPELINE_CONFIG_DIR` переопределяются.
Работу `Validation` шаги не повторяют.
