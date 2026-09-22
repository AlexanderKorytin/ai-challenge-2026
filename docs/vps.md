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

2026-09-22 на день пробовали сервер FirstVDS `83.136.235.149` с панелью ispmanager и вернулись:
панель владеет nginx и сетевым экраном и переписывает ручные правки, её службы занимали ~690 MB
из 956, а сертификат на выданное имя `*.fvds.ru` упирается в общий недельный предел Let's
Encrypt на всех клиентов хостинга. На нём остались остановленные следы дня (служба `cbr-mcp`,
сайт панели) — сервер можно вернуть хостингу.

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

### `cbr-mcp` — MCP-сервер курсов ЦБ (день 17)

- Код — `mcp_servers/cbr/` в репозитории `ai-challenge-2026`; доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ mcp_servers/cbr/ challenge:/opt/challenge/services/cbr-mcp/`
  (без `--no-owner` файлы на сервере достались бы uid Мака), затем на сервере
  `uv sync --frozen`, `cp deploy/cbr-mcp.service /etc/systemd/system/`,
  `cp deploy/Caddyfile /etc/caddy/Caddyfile && caddy validate --config /etc/caddy/Caddyfile`,
  `systemctl reload caddy` и `systemctl restart cbr-mcp`.
- Служба systemd `cbr-mcp` (`deploy/cbr-mcp.service`), слушает **только `127.0.0.1:8770`**.
  Работает **не от root**: временный пользователь systemd (`DynamicUser`), система только на
  чтение; запускает Python готового `.venv` напрямую, без uv.
- Наружу: **`https://38.180.117.69/mcp`** через Caddy (`deploy/Caddyfile` → `/etc/caddy/Caddyfile`).
  Сертификат Let's Encrypt на сам IP (профиль `shortlived`, живёт ~6 дней) Caddy получает,
  продлевает и подхватывает сам; проверка владения — TLS-ALPN на 443, порт 80 не слушается.
  Прочие пути — 404 (сопоставление путей Caddy без учёта регистра: `/MCP` доходит до службы и
  получает 401 от её замка).
- Токен — строка `CBR_MCP_TOKEN=` в `/opt/challenge/.env`. На Маке не хранится; клиент берёт
  его при запуске: `export CBR_MCP_TOKEN=$(ssh challenge "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")`.
- Служебный вход Caddy — файл `/var/lib/caddy/admin.sock` (каталог 700 caddy), не порт 2019:
  местные процессы, включая службу, перенастроить Caddy не могут.
- Проверка: без токена `curl -X POST https://38.180.117.69/mcp` — 401; чужое имя узла Caddy
  не пропускает к службе (пустой ответ). Сертификат —
  `journalctl -u caddy | grep "certificate obtained"`; продления Caddy ещё не наблюдали (первое —
  25–26.09), оповещения о сбое нет — сбой виден ошибкой TLS у клиента.
