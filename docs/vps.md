# VPS челленджа

- IP: **38.180.117.69**, провайдер Inferno Solution. Имени нет — службы выходят наружу по IP.
- Заход с Мака: **`ssh challenge`** (запись в `~/.ssh/config`, ключ `~/.ssh/challenge_vps`,
  ed25519, root). **Вход только по ключу** с 2026-09-22 (за сутки до этого — 4696 попыток подбора
  пароля): `/etc/ssh/sshd_config.d/10-challenge.conf` читается раньше `50-cloud-init.conf`, а у
  sshd побеждает первое значение. Запасной вход при потере ключа — веб-консоль провайдера.
- VPN на Маке (Happ) режет исходящий порт 22 у любого адреса — адрес сервера внесён в его
  исключения (см. `docs/грабли.md`).
- ICMP закрыт снаружи — `ping` не проходит, это норма.
- OS: Ubuntu 24.04.4 LTS. Ресурсы: 1 vCPU, 961 MB RAM **+ 2 GB swap** (`/swapfile`,
  swappiness=10), диск 20 GB (~10 GB свободно).

**Служба MCP курсов ЦБ живёт не здесь, а на отдельном сервере `188.120.230.58`** (раздел ниже).
2026-09-23 замер показал: прямые соединения с Мака к `38.180.117.69` (HTTPS и ssh) замерзают
через 2–9 обменов, хотя сервер отвечает (`tcpdump`: ответ повторяется, подтверждения нет) — на
пути глушится поток от сервера к Маку. С самого сервера тот же ряд проходит. Долгое соединение
`myharness` на этом адресе замерзало бы на втором-третьем вызове. Служба `cbr-mcp` здесь
выключена (`systemctl disable --now`; в её базе осталось пробное задание №1). Caddy остался
со старым блоком `/mcp` — снаружи он отвечает 502.
В тот же день службу ставили на FirstVDS `83.136.235.149` за nginx панели ispmanager и сняли,
когда пользователь заказал чистый сервер: там служба выключена, входы nginx убраны.

### Что установлено (native, без Docker — по выбору юзера)

| | |
|---|---|
| Python | 3.12.3 (системный) + venv + pip + pipx |
| **uv** | 0.12.7 — в `/root/.local/bin`, в PATH через `~/.bashrc` |
| **ruff** | через `uv tool` (глобально) |
| Node.js | 22 LTS (NodeSource) + npm + pnpm |
| **Caddy** | 2.11 из официального хранилища (`dl.cloudsmith.io/public/caddy/stable`) — HTTPS-вход служб |
| Сборка | build-essential (gcc 13.3, make) |
| Прочее | git, tmux, jq, htop, ncdu, net-tools, dnsutils, rsync, tree, unzip |
| Firewall | ufw включён: **входящие закрыты, кроме SSH (22) и HTTPS (443, Caddy)** |
| git identity | `aleksandrkorytin` / `koritin84@gmail.com` |

**Не установлено** (ставим по факту заданий): Docker, Go, Rust, Java, nginx, БД.

### Рабочая папка на сервере — `/opt/challenge/`

git-репозиторий (ветка `main`), фиксации делаются на сервере.

```
services/<name>/   одна служба = папка со своим uv-проектом (pyproject.toml + .venv)
data/              рабочие данные, дампы            (gitignored)
logs/              логи сервисов                    (gitignored)
bin/               вспомогательные скрипты
.env               общие секреты, gitignored, chmod 600
```

## Службы

### `cbr-mcp` — MCP-сервер курсов ЦБ (дни 17 и 18) — на сервере `188.120.230.58`

Отдельный сервер: чистая Ubuntu 24.04.5, 1 vCPU, 956 MB, диск 15 GB. Заход `ssh challenge-mcp`
(запись в `~/.ssh/config`, тот же ключ `challenge_vps`, root). **Вход только по ключу**:
`/etc/ssh/sshd_config.d/10-challenge.conf` читается раньше `40-hosting.conf` хостинга, а у sshd
побеждает первое значение. Адрес внесён в исключения Happ (иначе туннель режет порт 22).
Сетевой экран ufw: входящие закрыты, кроме 22/tcp и 443/tcp. Установлено: Caddy 2.11 из
официального хранилища (`dl.cloudsmith.io/public/caddy/stable`), uv в `/root/.local/bin`, git,
rsync, sqlite3. Рабочий каталог `/opt/challenge/` — git, секреты в `.env` (root 600).

- Код — `mcp_servers/cbr/` в репозитории `ai-challenge-2026`; доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ --exclude '*.sqlite3' mcp_servers/cbr/ challenge-mcp:/opt/challenge/services/cbr-mcp/`
  (без `--no-owner` файлы на сервере достались бы uid Мака), затем на сервере из
  `/opt/challenge/services/cbr-mcp`: `/root/.local/bin/uv sync --frozen`, `cp deploy/cbr-mcp.service /etc/systemd/system/`,
  `cp deploy/Caddyfile /etc/caddy/Caddyfile && caddy validate --config /etc/caddy/Caddyfile`,
  `systemctl daemon-reload`, `systemctl reload caddy` и `systemctl restart cbr-mcp`.
- Служба systemd `cbr-mcp` (`deploy/cbr-mcp.service`), слушает **только `127.0.0.1:8770`**.
  Работает **не от root**: временный пользователь systemd (`DynamicUser`), система только на
  чтение; запускает Python готового `.venv` напрямую, без uv.
- Планировщик сбора (день 18) живёт в том же процессе. Задания, журнал запусков и собранные
  курсы — SQLite `/var/lib/cbr-mcp/cbr.sqlite3` (`StateDirectory`; при `DynamicUser` настоящий
  путь — `/var/lib/private/cbr-mcp/`). Доставка `rsync --delete` её не трогает: база вне каталога
  кода. Посмотреть:
  `ssh challenge-mcp "sqlite3 -readonly /var/lib/private/cbr-mcp/cbr.sqlite3 'SELECT * FROM jobs'"`.
  База перенесена с `83.136.235.149` 2026-09-23 копией файла при остановленной службе.
- Наружу: **`https://188.120.230.58/mcp`** через Caddy (`deploy/Caddyfile` → `/etc/caddy/Caddyfile`).
  Сертификат Let's Encrypt на сам IP (профиль `shortlived`, живёт ~6 дней) Caddy получает,
  продлевает и подхватывает сам; проверка владения — TLS-ALPN на 443, порт 80 не слушается.
  Прочие пути — 404; запрос с чужим именем узла Caddy до службы не пропускает (пустой ответ).
- Служебный вход Caddy — файл `/var/lib/caddy/admin.sock` (каталог 700 caddy), не порт 2019:
  местные процессы, включая службу, перенастроить Caddy не могут.
- Токен — строка `CBR_MCP_TOKEN=` в `/opt/challenge/.env`. На Маке не хранится; клиент берёт
  его при запуске:
  `export CBR_MCP_TOKEN=$(ssh challenge-mcp "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")`.
- Проверка: без токена `curl -X POST https://188.120.230.58/mcp` — 401. Путь с Мака держит
  долгое соединение: 40 запросов `tools/list` (484 КБ) на одном соединении прошли 2026-09-23.
  Сертификат — `journalctl -u caddy | grep "certificate obtained"`; оповещения о сбое продления
  нет — сбой виден ошибкой TLS у клиента.

### `cbr-digest` — агент сводок 24/7 (день 18) — на сервере `188.120.230.58`

- Программа `agents/digest/digest.py` (клиент MCP на библиотеке `myharness`), на сервере
  `/opt/challenge/agents/digest`, библиотека — `/opt/challenge/myharness` (тот же относительный
  путь, что в репозитории). Доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache myharness/ challenge-mcp:/opt/challenge/myharness/`,
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ agents/digest/ challenge-mcp:/opt/challenge/agents/digest/`,
  затем на сервере из `/opt/challenge/agents/digest`:
  `/root/.local/bin/uv sync --frozen --reinstall-package myharness` (без ключа uv оставит в `.venv`
  прежнюю копию библиотеки: зависимость по пути пересобирается, только если менялся её
  `pyproject.toml`),
  `cp deploy/cbr-digest.{service,timer} /etc/systemd/system/`, `systemctl daemon-reload`.
- **С 2026-09-23 таймер выключен** по решению пользователя (`systemctl disable --now
  cbr-digest.timer`): каждый запуск — платный вызов DeepSeek. Включить обратно —
  `systemctl enable --now cbr-digest.timer`. Задание сбора №2 тоже снято (`delete_schedule`),
  заданий сбора на сервере нет; сам сервер `cbr-mcp` работает, собранные 6 курсов и 5 сводок
  остались в базе.
- Таймер `cbr-digest.timer` (`OnCalendar=*:0/10`, `Persistent=true`) запускает разовую службу
  `cbr-digest`: агент DeepSeek читает собранное инструментами `cbr` (разрешены только чтение —
  `agents/digest/.claude/settings.json`), программа сохраняет текст `save_digest` в базу
  `cbr-mcp`. К серверу агент ходит напрямую `http://127.0.0.1:8770/mcp`, мимо Caddy.
- Ключ `DEEPSEEK_API_KEY` — строка в `/opt/challenge/.env` (root 600) рядом с `CBR_MCP_TOKEN`;
  служба не от root (`DynamicUser`), журнал прогонов агента —
  `/var/lib/cbr-digest/myharness-journal.jsonl` (`StateDirectory`).
- Проверка: `systemctl list-timers cbr-digest.timer` — ближайший и прошлый запуск;
  `journalctl -u cbr-digest -o cat -n 30` — вызовы инструментов и текст последней сводки;
  ручной запуск — `systemctl start cbr-digest` (каждый тратит деньги DeepSeek и пишет сводку в
  рабочую базу). Предел одного запуска — `TimeoutStartSec=5min`; без инструментов или без их
  вызовов агент сводку не сохраняет и выходит с кодом 1.

### `llm-gateway` — шлюз перед службой Ollama (день 30) — на сервере `188.120.230.58`

**Состояние на конец 2026-10-09: вариант оставлен, служба дня работает на Маке** (ветка `w6d5`,
`week_6/day_5/README.md`). Агент туннеля на Маке снят, поэтому шлюз на сервере отвечает 503.
Причина — VPN Happ на Маке, а не сервер: около 15:25 VPN переподключился, и адреса серверов
перестали идти мимо него (маршрут к `188.120.230.58` ушёл в `utun`). Через VPN HTTPS к серверу
зависает на приветствии TLS, а ssh обрывается («Connection closed», «timed out during banner
exchange») — так же и к `38.180.117.69`. В обход VPN сервер отвечает за 0,09 с:
`curl --interface en0 https://188.120.230.58/v1/models`, `ssh -o BindInterface=en0 challenge-mcp`.
В журнале sshd отказов по перегрузке нет. **Сервер недоступен — первым делом проверить его в
обход VPN этими двумя командами.** Агент туннеля за время сбоя перезапускался 46 раз. На сервере остались служба `llm-gateway`, пользователь
`llm-tunnel`, `/etc/ssh/sshd_config.d/90-llm-tunnel.conf`, `/opt/challenge/llm-gateway.env` и
пути `/v1/*`, `/api/*` в Caddy. Уборка не сделана. Прежде чем ставить агент снова, добавить в
его файл `ThrottleInterval`, чтобы перезапуск не шёл чаще раза в минуту.

Модель `qwen3.5:9b-q4_K_M-32k` работает в Ollama на Маке. Сервер даёт вход из сети:
`https://188.120.230.58`, пути `/v1/*` и `/api/*`. Устройство и отказы — `llm_gateway/README.md`,
дизайн — `docs/местная-служба.md`.

```
клиент → Caddy :443 → шлюз 127.0.0.1:8780 → 127.0.0.1:11435 (туннель ssh) → Ollama на Маке
```

- **Служба работает, пока Мак включён и не спит.** Туннеля нет — шлюз отвечает 503
  «узел модели недоступен».
- Код — `llm_gateway/` и библиотека `myharness/`. Доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ --exclude .pytest_cache myharness/ challenge-mcp:/opt/challenge/myharness/`,
  то же для `llm_gateway/` в `/opt/challenge/llm_gateway/`, затем на сервере из
  `/opt/challenge/llm_gateway`: `/root/.local/bin/uv sync --frozen --reinstall-package myharness`,
  `cp deploy/llm-gateway.service /etc/systemd/system/`, `systemctl daemon-reload`,
  `systemctl restart llm-gateway`.
- Служба systemd `llm-gateway` (`deploy/llm-gateway.service`): слушает `127.0.0.1:8780`, не от
  root (`DynamicUser`), ходит только на петлю (`IPAddressAllow=localhost`). Память со словарём
  модели — 227 МБ (замер 2026-10-09); свободно на сервере после запуска 403 МБ.
- Токены клиентов — `/opt/challenge/llm-gateway.env` (root 600): строки `LLM_TOKEN_mac` и
  `LLM_TOKEN_challenge`. Новый клиент — новая строка `LLM_TOKEN_<имя>=$(openssl rand -hex 32)`
  и `systemctl restart llm-gateway`. Клиент берёт токен при запуске:
  `export OLLAMA_API_KEY=$(ssh challenge-mcp "grep ^LLM_TOKEN_mac= /opt/challenge/llm-gateway.env | cut -d= -f2")`.
- Настройки шлюза — строки `Environment=` модуля: список моделей `LLM_MODELS`, частота
  `LLM_RATE_PER_MINUTE=12` (решение пользователя 2026-10-09).
- Журнал запросов — `/var/lib/private/llm-gateway/journal.jsonl`, строка на запрос, без текста
  запросов и ответов. Ротации нет.
- Caddy: пути `/v1/*` и `/api/*` в общем файле `mcp_servers/cbr/deploy/Caddyfile`. Прежний файл
  сохранён на сервере как `/etc/caddy/Caddyfile.before-llm`.
- **Туннель.** Его держит агент launchd на Маке `com.challenge.llm-tunnel`
  (`llm_gateway/deploy/com.challenge.llm-tunnel.plist` → `~/Library/LaunchAgents/`), журнал —
  `~/Library/Logs/llm-tunnel.log`. Снять: `launchctl bootout gui/$(id -u)/com.challenge.llm-tunnel`.
  Поставить: `launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.challenge.llm-tunnel.plist`.
- Пользователь туннеля на сервере — `llm-tunnel` (без оболочки, дом `/var/lib/llm-tunnel`), ключ
  на Маке — `~/.ssh/llm_tunnel`. Ключу разрешено одно действие — слушать `127.0.0.1:11435`. Это
  держат два слоя: `/etc/ssh/sshd_config.d/90-llm-tunnel.conf` (`Match User llm-tunnel`,
  `AllowTcpForwarding remote`, `PermitListen 127.0.0.1:11435`) и строка `authorized_keys`
  (`restrict,port-forwarding,permitlisten=…,permitopen="127.0.0.1:1"`). Замер 2026-10-09:
  оболочка — «This account is currently not available», порт 9999 — «remote port forwarding
  failed», прямая переадресация `-L` — «administratively prohibited».
- Проверка: `curl -s -o /dev/null -w '%{http_code}' https://188.120.230.58/v1/models` — 401 без
  токена; с токеном — список из одной модели. `systemctl is-active llm-gateway`.
- Остаточные риски: любой процесс сервера ходит в `127.0.0.1:11435` мимо шлюза и получает всю
  службу Ollama Мака; предела размера тела запроса у шлюза нет.
