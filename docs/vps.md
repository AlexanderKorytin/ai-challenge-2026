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

**Служба MCP курсов ЦБ живёт не здесь, а на втором сервере — `83.136.235.149`** (раздел ниже).
2026-09-23 замер показал: прямые соединения с Мака к `38.180.117.69` (HTTPS и ssh) замерзают
через 2–9 обменов, хотя сервер отвечает (`tcpdump`: ответ повторяется, подтверждения нет) — на
пути глушится поток от сервера к Маку. С самого сервера и к российскому `83.136.235.149` тот же
ряд проходит. Долгое соединение `myharness` на этом адресе замерзало бы на втором-третьем
вызове. Служба `cbr-mcp` здесь выключена (`systemctl disable --now`), Caddy остался.

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

### `cbr-mcp` — MCP-сервер курсов ЦБ (дни 17 и 18) — на сервере `83.136.235.149`

Второй сервер: FirstVDS, Ubuntu с панелью ispmanager, 1 vCPU, 956 MB. Заход `ssh challenge-fvds`
(запись в `~/.ssh/config`, тот же ключ `challenge_vps`, root, вход только по ключу). Панель
владеет nginx и сетевым экраном: всё, чем она владеет, меняется только её штатными средствами
(`/usr/local/mgr5/sbin/mgrctl -m ispmgr <функция>`) либо каталогами своих вставок.

- Код — `mcp_servers/cbr/` в репозитории `ai-challenge-2026`; доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ --exclude '*.sqlite3' mcp_servers/cbr/ challenge-fvds:/opt/challenge/services/cbr-mcp/`
  (без `--no-owner` файлы на сервере достались бы uid Мака), затем на сервере
  `/root/.local/bin/uv sync --frozen`, копии файлов `deploy/` в `/etc` (`nginx-mcp.conf` →
  `snippets/cbr-mcp.conf`, `nginx-ip.conf` → `conf.d/cbr-mcp-ip.conf`, `*.service`/`*.timer` →
  `systemd/system/`), `nginx -t` до `systemctl reload nginx`, `systemctl daemon-reload` и
  `systemctl restart cbr-mcp`.
- Служба systemd `cbr-mcp` (`deploy/cbr-mcp.service`), слушает **только `127.0.0.1:8770`**.
  Работает **не от root**: временный пользователь systemd (`DynamicUser`), система только на
  чтение; запускает Python готового `.venv` напрямую, без uv.
- Планировщик сбора (день 18) живёт в том же процессе. Задания, журнал запусков и собранные
  курсы — SQLite `/var/lib/cbr-mcp/cbr.sqlite3` (`StateDirectory`; при `DynamicUser` настоящий
  путь — `/var/lib/private/cbr-mcp/`). Доставка `rsync --delete` её не трогает: база вне каталога
  кода. Посмотреть: `ssh challenge-fvds "sqlite3 -readonly /var/lib/private/cbr-mcp/cbr.sqlite3 'SELECT * FROM jobs'"`.
- Наружу, два входа с одной пересылкой `deploy/nginx-mcp.conf` → `/etc/nginx/snippets/cbr-mcp.conf`
  (копия, а не ссылка в каталог доставки; подключается строкой `include`):
  - **`https://83.136.235.149:8443/mcp`** — рабочий. Блок `deploy/nginx-ip.conf` →
    `/etc/nginx/conf.d/cbr-mcp-ip.conf`; сертификат Let's Encrypt на сам IP, который панель
    получает и продлевает своим acme.sh (живёт ~6 дней, `/usr/local/mgr5/etc/scripts/acmesh/83.136.235.149/`,
    задача панели `acmesh.certs.update` ежедневно). nginx перечитывает его таймером
    `nginx-reload.timer` раз в сутки (`deploy/nginx-reload.{service,timer}`).
  - `https://koritin84.fvds.ru/mcp` — по имени (`/etc/nginx/vhosts-resources/koritin84.fvds.ru/mcp.conf`);
    заработает, когда панель выпустит сертификат на имя: `fvds.ru` не входит в список публичных
    суффиксов, и предел Let's Encrypt — 50 сертификатов в неделю на всех клиентов FirstVDS.
    2026-09-22 и 2026-09-23 выпуск упёрся в этот предел; панель повторяет сама.
- Токен — строка `CBR_MCP_TOKEN=` в `/opt/challenge/.env` этого сервера. На Маке не хранится;
  клиент берёт его при запуске:
  `export CBR_MCP_TOKEN=$(ssh challenge-fvds "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")`.
- Сетевой экран — у панели (цепочки `ispmgr_*`, политика «принимать»): снаружи видны 22, 53, 80,
  443, 1500, 1501, 8443. Служба слушает только `127.0.0.1`; закрыть лишнее — отдельная задача.
- Проверка: без токена `curl -X POST https://83.136.235.149:8443/mcp` — 401; чужое имя узла —
  421. Срок IP-сертификата (продление не оповещает о сбое):
  `ssh challenge-fvds openssl x509 -enddate -noout -in /usr/local/mgr5/etc/scripts/acmesh/83.136.235.149/fullchain.cer`.
