# План: разделение `cli.py`

Вход — утверждённый дизайн `docs/разделение-cli.md`. Поведение инструмента не меняется ни в
одном шаге: каждый шаг переносит определения и правит ввозы, тела функций остаются как есть.

**Задача не трогает обмен с моделью.** Живого прогона с настоящим ключом она не требует: ни
одна строка запроса, ответа, журнала и памяти не переписывается. Что действительно нужно
проверить руками — интерфейс (окна `prompt_toolkit` проверками покрыты не целиком), и это
делает стадия `Validation` одним запуском.

## Карта файлов

| Файл | За что отвечает после работы |
|---|---|
| `myharness/src/myharness/state.py` | состояние сеанса: `State`, единица очереди `Request`, глобальные факты для главного разговора |
| `myharness/src/myharness/workers.py` | очереди и их исполнители: общая очередь и очередь отдельной панели |
| `myharness/src/myharness/panes.py` | навигация по экранам и панелям, черновики ввода, заготовки |
| `myharness/src/myharness/conversation.py` | разговор между запусками: новый файл сессии, подъём разговора с диска |
| `myharness/src/myharness/strategies.py` | режимы контекста, ветви, экраны способов и рабочие экраны, смена профиля |
| `myharness/src/myharness/commands.py` | разбор введённой строки и раздача её командам |
| `myharness/src/myharness/commands_model.py` | `/auth`, `/model`, `/profile`, `/strategy` |
| `myharness/src/myharness/commands_memory.py` | `/facts`, `/branch`, `/remember`, `/memory`, `/forget` |
| `myharness/src/myharness/commands_params.py` | `/params`, `/set`, `/mouse` |
| `myharness/src/myharness/commands_context.py` | `/tokens`, `/budget`, `/context`, `/compact` |
| `myharness/src/myharness/agents_panel.py` | список агентов под строкой ввода и `/team` |
| `myharness/src/myharness/layout.py` | окна, сетка панелей, дополнение команд, сборка раскладки |
| `myharness/src/myharness/keys.py` | привязки клавиш |
| `myharness/src/myharness/cli.py` | точка входа: приветствие, цикл приложения, запуск |
| `myharness/tests/check_units.py`, `myharness/tests/check_app.py` | те же проверки, новые имена модулей |
| `myharness/src/myharness/archivist.py`, `compact.py`, `output.py` | ввоз `State` обычным образом вместо обхода круга |
| `bin/расход.py` | к токенам и деньгам добавляется время работы |

Направление ввозов — строго сверху вниз по таблице: `state` не знает никого из списка,
`cli` знает всех. Обратного ввоза нет ни одного.

## Договоры модулей

### `state.py`
**Владеет**: классом `State` — всем состоянием сеанса (экраны, очередь, расход, память,
фоновые службы) и единицей очереди `Request`. Доступ к полям имеют все модули пакета: состояние
передаётся первым доводом почти в каждую функцию.
**Отдаёт**: `Request`, `State`, `user_facts() -> list[str]`,
`profile_for_strategy(source: Profile, strategy: str) -> Profile`.
**Обещает**: конструктор `State` пригоден без окна и без ключа (его зовут проверки напрямую);
главный агент заведён к концу `__post_init__`; ни одно поле не заводит файла на диске.
**Не делает**: не ввозит `prompt_toolkit`-раскладку, не знает ни команд, ни клавиш, ни экранов
сверх `screens`; сам на диск не ходит, кроме чтения глобальных фактов в `user_facts`.

### `workers.py`
**Владеет**: жизнью задач очередей — общего исполнителя `worker` и `PaneWorker` отдельной
панели; ссылки на них лежат в `State` (`current_task`, `pane_workers`, `submission_tasks`).
**Отдаёт**: `PaneWorker`, `pane_worker(state, pane) -> PaneWorker`, `close_pane_workers(state)`,
`_track_submission(state, task)`, `async worker(state)`.
**Обещает**: `pane_worker` для одной и той же панели возвращает один и тот же исполнитель;
`close_pane_workers` дожидается отмены, после неё незавершённых задач панелей не остаётся.
**Не делает**: не знает раскладки и клавиш; в очередь сам ничего не кладёт.

### `panes.py`
**Владеет**: тем, какой экран и какая панель сейчас активны, и черновиком ввода каждой панели.
**Отдаёт**: `switch_screen(state, index)`, `switch_pane(state, index)`, `toggle_zoom(state)`,
`drop_agent_screens(state)`, `apply_prefill(state, profile, pane)`, `next_prefill(state, pane)`,
`active_profile(state) -> Profile`, `_active_input_pane(state)`, `_pane_profile(state, pane)`,
`_save_active_draft(state)`, `_restore_active_draft(state)`.
`active_strategy` в договор не входит: её не звал никто ни до разделения, ни после — удалена.
**Обещает**: при любом переходе черновик покидаемой панели сохранён, а черновик пришедшей —
восстановлен; `state.active` остаётся в границах списка экранов.
**Не делает**: к модели не ходит, файлов не пишет.

### `layout.py`
**Владеет**: окнами панелей (в них живёт прокрутка) и сборкой раскладки.
**Отдаёт**: `HarnessCompleter`, `LogWindow`, `pane_columns(count, width) -> int`,
`app_output()`, `click_only_mouse(output)`, `Раскладка` и `собрать_раскладку(state) -> Раскладка`.
`Раскладка` несёт то, что принадлежит окну и потому не приходит через состояние: `layout`,
`buffer`, `picker_active`, `many_agents`, `pane_window`. Полей пять, а не семь: `input_area` и
`занятость` в договор не вошли — читателя у них не оказалось (`занятость` и так лежит в
состоянии), а поле без читателя — обещание, которое нечем подтвердить.
**Обещает**: окно панели заводится один раз на панель — прокрутка переживает перерисовку;
`собрать_раскладку` оставляет в `state.input_buffer` буфер активной панели и восстанавливает
её черновик.
**Не делает**: клавиш не привязывает, `Application` не создаёт.

### `keys.py`
**Владеет**: привязками клавиш и ничем больше.
**Отдаёт**: `привязки(state, раскладка: Раскладка) -> KeyBindings`.
**Обещает**: при открытой панели выбора обычный ввод в строку не попадает; набор клавиш и их
условия те же, что были в `build_app`.
**Не делает**: окон не создаёт, состояния сверх нынешнего не заводит.

## Общая проверка шага

После каждого шага ведущий запускает одно и то же и только это:

```bash
cd myharness && uv run python -c "import myharness.cli"
cd myharness && uv run python tests/check_units.py && uv run python tests/check_app.py
```

Первая команда ловит круговой ввоз (он падает именно на ввозе), вторая — потерю определения и
разъехавшееся имя. Полный проход, итоговый просмотр и ручной запуск интерфейса — стадия
`Validation`, здесь их не повторяем.

---

## Шаг 1. Состояние сеанса живёт отдельно от интерфейса

**Файлы**: `myharness/src/myharness/state.py` (новый), `cli.py`, `archivist.py`, `compact.py`,
`output.py`, `myharness/tests/check_app.py`, `myharness/tests/check_units.py`
**Зависит от**: ничего
**Ход**: быстрый — перенос без правки тел; читателей за границей файлов шага нет, все
обращения к `State` правятся здесь же
**Требование**: не про поведение `myharness` — требования не меняются
**Проверка**: общая проверка шага; сверх неё — `grep -rn "from .cli import\|from myharness.cli"
myharness/src myharness/tests` не находит ни одного ленивого ввоза `State`
**Содержание**: в `state.py` переезжают `Request` (стр. 85–96), `State` (99–265),
`profile_for_strategy` (49–69), `user_facts` (529–545). Докстрока модуля называет границу и
объясняет, что именно этот переезд развязал: `archivist`, `compact` и `output` ввозили `State`
лениво или под `TYPE_CHECKING`, обходя круг через тяжёлый `cli`, — теперь ввоз обычный,
`from .state import State`. Ссылка `pane_workers: dict[int, PaneWorker]` остаётся подсказкой
типа: `PaneWorker` ввозится под `if TYPE_CHECKING` (файл живёт с `from __future__ import
annotations`, так что во время работы имя не нужно). В `cli.py` появляется
`from .state import Request, State, profile_for_strategy, user_facts`. В проверках
`cli.State` → `state_mod.State` при `from myharness import state as state_mod`.

## Шаг 2. Очереди запросов живут отдельно

**Файлы**: `myharness/src/myharness/workers.py` (новый), `cli.py`, `myharness/tests/check_app.py`
**Зависит от**: шаг 1
**Ход**: быстрый — перенос без правки тел, все зовущие места правятся здесь же
**Требование**: не про поведение `myharness`
**Проверка**: общая проверка шага; в `check_app.py` остаются проходящими сценарии очереди
(`cli.worker` → `workers.worker`, отмена по Ctrl+C, очередь неглавной панели)
**Содержание**: переезжают `PaneWorker` (268–382) со всеми методами, `pane_worker` (385–396),
`close_pane_workers` (398–401), `_track_submission` (403–415), `worker` (1114–1167). Докстрока
называет владение: задачи очередей живут в `State`, а их заведение и остановка — здесь.

## Шаг 3. Навигация по экранам и черновики ввода живут отдельно

**Файлы**: `myharness/src/myharness/panes.py` (новый), `cli.py`, `myharness/tests/check_app.py`
**Зависит от**: шаги 1, 2
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/interface/spec.md` — поведение не меняется, требование
перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии переходов между экранами и панелями в `check_app.py`
(`switch_screen`, `switch_pane`, `toggle_zoom`) проходят через новые имена
**Содержание**: переезжают `active_profile` (72–77), `active_strategy` (79–82),
`_active_input_pane` (417–423), `_save_active_draft` (426–429), `_restore_active_draft`
(432–437), `_pane_profile` (440–443), `switch_screen` (446–459), `switch_pane` (462–478),
`toggle_zoom` (481–488), `drop_agent_screens` (491–523), `apply_prefill` (1031–1051),
`next_prefill` (1054–1064). Мёртвая `active_strategy` (79–82) не переносится, а удаляется:
её не звали ни код, ни проверки.

## Шаг 4. Подъём разговора с диска живёт отдельно

**Файлы**: `myharness/src/myharness/conversation.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/memory/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарий «второй запуск поднимает разговор» в `check_app.py`
(`cli.restore_conversation` → `conversation.restore_conversation`) проходит
**Содержание**: переезжают `open_new_session` (547–568), `забыть_счёт_занятости` (570–582),
`restore_conversation` (584–719). Новых путей записи на диск шаг не заводит: файл сессии
заводится как прежде, через `memory.SessionStore`, чей каталог уже переопределяется
`MYHARNESS_STATE_DIR`.

## Шаг 5. Режимы контекста и смена профиля живут отдельно

**Файлы**: `myharness/src/myharness/strategies.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3, 4
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/profiles/spec.md`, `openspec/specs/work-screens/spec.md` —
перечитываются на сверку, не правятся
**Проверка**: общая проверка шага; сценарии ветвей и режимов в `check_app.py`
(`ensure_strategy_screen`, `switch_profile`) проходят
**Содержание**: переезжают `_linear_strategy_agent` (730–768), `_branch_prefill` (771–790),
`create_strategy_screen` (793–895), `ensure_strategy_screen` (898–925), `use_strategy`
(928–940), `open_profile_surfaces` (943–954), `switch_profile` (960–1028),
`open_method_screens` (1067–1075), `open_work_screens` (1078–1108).

## Шаг 6. Список агентов и группа живут отдельно

**Файлы**: `myharness/src/myharness/agents_panel.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/team/spec.md`, `openspec/specs/interface/spec.md` —
перечитываются на сверку, не правятся
**Проверка**: общая проверка шага; сценарии «пока агент один, списка нет» и «список появился»
в `check_app.py` проходят через новое имя
**Содержание**: переезжают `AgentRow` (2170–2181), `collect_agents` (2184–2208), `agent_index`
(2211–2218), `goto_agent` (2221–2229), `show_agent_panel` (2232–2235), `step_agent` (2238–2244),
`cmd_team` (2134–2163).

## Шаг 7. Команды профиля и модели живут отдельно

**Файлы**: `myharness/src/myharness/commands_model.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3, 5
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/commands/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии `/strategy` и `/profile` в `check_app.py`
(`cli.cmd_strategy`, `cli.switch_profile`) проходят
**Содержание**: переезжают `cmd_auth` (1173–1176), `do_auth` (1178–1200), `set_model`
(1203–1207), `cmd_model` (1210–1247), `open_profile_picker` (1250–1282), `cmd_profile`
(1285–1307), `open_strategy_picker` (1310–1334), `cmd_strategy` (1337–1416).

## Шаг 8. Команды памяти и ветвей живут отдельно

**Файлы**: `myharness/src/myharness/commands_memory.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3, 4, 5
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/memory/spec.md`, `openspec/specs/commands/spec.md` —
перечитываются на сверку, не правятся
**Проверка**: общая проверка шага; сценарии `/facts` и `/branch` в `check_app.py` проходят
**Содержание**: переезжают `cmd_facts` (1419–1529), `_activate_branch` (1532–1555),
`_open_branch_picker` (1558–1588), `cmd_branch` (1591–1706), `cmd_remember` (1837–1851),
`cmd_memory` (1854–1890), `cmd_forget` (1893–1909).

## Шаг 9. Команды параметров живут отдельно

**Файлы**: `myharness/src/myharness/commands_params.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/params/spec.md`, `openspec/specs/commands/spec.md` —
перечитываются на сверку, не правятся
**Проверка**: общая проверка шага; сценарии панели значений и своего значения в `check_app.py`
(`open_value_picker`, `apply_custom_value`) проходят
**Содержание**: переезжают `cmd_params` (1709–1714), `set_param` (1717–1729), `open_value_picker`
(1732–1766), `open_param_picker` (1769–1786), `cmd_set` (1789–1799), `apply_custom_value`
(1801–1819), `toggle_mouse` (1822–1834).

## Шаг 10. Команды токенов и контекста живут отдельно

**Файлы**: `myharness/src/myharness/commands_context.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 1, 3
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/tokens/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии `/tokens`, `/context`, `/compact` в `check_app.py`
проходят
**Содержание**: переезжают `cmd_tokens` (1912–1963), `cmd_budget` (1966–2022),
`сбросить_счёт_отказов` (2025–2039), `cmd_context` (2042–2075), `cmd_compact` (2078–2131).

## Шаг 11. Разбор введённой строки живёт отдельно

**Файлы**: `myharness/src/myharness/commands.py` (новый), `cli.py`,
`myharness/tests/check_app.py`
**Зависит от**: шаги 2, 6, 7, 8, 9, 10
**Ход**: быстрый — перенос без правки тел
**Требование**: `openspec/specs/commands/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии ввода в `check_app.py` (`cli.handle_submit`,
`cli.handle_command`) проходят через новое имя
**Содержание**: переезжают `handle_command` (2247–2323) и `handle_submit` (2326–2394). Ввозы
команд идут по имени модуля (`from . import commands_model`), а не поимённо: так в раздаче
видно, к какой семье принадлежит команда.

## Шаг 12. Раскладка окна собирается отдельным модулем

**Файлы**: `myharness/src/myharness/layout.py` (новый), `cli.py`,
`myharness/tests/check_app.py`, `myharness/tests/check_units.py`
**Зависит от**: шаги 1, 3, 11
**Ход**: быстрый — в отличие от прочих шагов здесь не только перенос: `build_app` делится.
Читателем за границей файлов шага был бы `keys.py`, но его ещё нет — договор `Раскладка`
выписан выше и проверяется шагом 13
**Требование**: `openspec/specs/interface/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии сетки панелей (`cli.pane_columns` → `layout`),
дополнения команд (`HarnessCompleter`) и разворота панели в проверках проходят
**Содержание**: переезжают `HarnessCompleter` (2400–2459), `LogWindow` (2462–2472),
`pane_columns` (2475–2480), `app_output` (2483–2486), `click_only_mouse` (2489–2512) и вся
первая половина `build_app` (2515 — строка перед `kb = KeyBindings()`): окна панелей, заголовки,
сетка, разделители, панель агентов, строка состояния, полоска занятости, плавающие окна,
`Layout`. Заводится
`@dataclass Раскладка` с полями `layout: Layout`, `input_area: TextArea`, `buffer: Buffer`,
`picker_active: Condition`, `many_agents: Condition`,
`pane_window: Callable[[screens_mod.Pane], LogWindow]` и функция
`собрать_раскладку(state: State) -> Раскладка`. Побочные действия
конца нынешней первой половины (`state.input_buffer = input_area.buffer`,
`_restore_active_draft`, `apply_prefill` активной панели, `state.занятость = занятость`)
остаются внутри `собрать_раскладку` — они и сейчас идут ровно там.

## Шаг 13. Клавиши живут отдельно от раскладки

**Файлы**: `myharness/src/myharness/keys.py` (новый), `cli.py`
**Зависит от**: шаг 12
**Ход**: быстрый — перенос тел обработчиков без правки; читатель один и он в файлах шага
**Требование**: `openspec/specs/interface/spec.md` — перечитывается на сверку, не правится
**Проверка**: общая проверка шага; сценарии клавиш в `check_app.py` (ввод, отмена, прокрутка,
переходы по экранам, разворот панели, свёрнутые размышления) проходят
**Содержание**: вторая половина `build_app` (от `kb = KeyBindings()` до `click_only_mouse(...)`)
переезжает в `привязки(state: State, раскладка: layout_mod.Раскладка) -> KeyBindings`.
Обработчики берут `buffer` и `picker_active` из `раскладка`, а не из замыкания `build_app`.
В `cli.build_app` остаётся сборка: `раскладка = layout.собрать_раскладку(state)`,
`kb = keys.привязки(state, раскладка)`, `click_only_mouse(app_output())`, создание
`Application` и две строки про `ttimeoutlen`/`timeoutlen`.

## Шаг 14. Ни одно определение не потеряно, докстроки называют границы

**Файлы**: все новые модули, `cli.py`, `myharness/README.md`
**Зависит от**: шаги 1–13
**Ход**: быстрый — сверка и текст
**Требование**: не про поведение `myharness`
**Проверка**: разбором дерева кода сверить, что множество имён уровня модуля во всех новых
файлах плюс остаток `cli.py` совпадает с 80 исходными именами `cli.py` (вершина `211ca92`) —
ни одного лишнего, ни одного потерянного; `grep -rn "^from \.\|^from myharness" ` по новым
модулям не находит обратных ввозов (ввоз `cli` из любого модуля пакета, кроме `__init__`)
**Дополнено по ходу**: в `check_units.py` добавить проверку, что каждое имя набора
`ЗАПРЕЩЁННЫЕ`, кроме `prompt_toolkit`, — настоящий файл в `src/myharness`. Иначе опечатка в
имени модуля молча снимает запрет на ввоз интерфейса.
**Содержание**: у каждого нового модуля докстрока по образцу `output.py` и `args.py`: что
модуль знает, чего не знает, и какой узел развязан переездом. Докстрока `cli.py` переписывается:
он больше не «полноэкранный REPL со всем внутри», а точка входа. Если `myharness/README.md`
описывает состав пакета — обновить таблицу модулей там же.

## Шаг 15. Измеритель расхода показывает и время работы

**Файлы**: `bin/расход.py`
**Зависит от**: ничего (можно вести одновременно с любым шагом)
**Ход**: быстрый — одно место, читателей за границей файла нет
**Требование**: не про поведение `myharness`
**Проверка**: `python3 bin/расход.py --день 2026-09-12` печатает по дню строку времени —
первая запись, последняя, протяжённость; `python3 bin/расход.py --день 2026-01-01` (день без
работы) не падает и время не печатает
**Содержание**: в разборе записей Claude Code уже читается `timestamp` каждой записи
(`bin/расход.py:163`). По дню копится минимальная и максимальная отметка, в итог добавляется
строка вида `время: 09:14 → 20:41, протяжённость 11 ч 27 мин`. Протяжённость — календарная
разница между первой и последней записью дня, и в выводе это названо прямо: это время, в течение
которого шла работа, а не сумма ожиданий ответа. Для OMP отметки берутся из того же запроса к
`stats.db`, где уже есть `timestamp`.
