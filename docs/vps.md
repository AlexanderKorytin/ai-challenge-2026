# VPS челленджа

С 2026-09-22 — новый сервер; прежний `38.180.117.69` (Inferno Solution) выведен из работы.

- IP: **83.136.235.149**, имя **`koritin84.fvds.ru`** (выдано хостингом FirstVDS, указывает на
  этот адрес).
- Заход с Мака: **`ssh challenge`** (запись в `~/.ssh/config`, ключ `~/.ssh/challenge_vps`,
  ed25519, root). **Вход только по ключу**: пароль и клавиатурный вход закрыты файлом
  `/etc/ssh/sshd_config.d/10-challenge.conf` — он читается раньше `40-hosting.conf` хостинга, а у
  sshd побеждает первое значение.
- VPN на Маке (Happ) режет исходящий порт 22 у любого адреса — адрес сервера внесён в его
  исключения (см. `docs/грабли.md`).
- OS: Ubuntu 24.04.5 LTS. Ресурсы: 1 vCPU, 956 MB RAM + 477 MB swap, диск 15 GB.

## Панель ispmanager

Сервер — образ хостинга с панелью ispmanager (веб-консоль `https://83.136.235.149:1500`).
Решение пользователя 2026-09-22 — работать через панель, а не переустанавливать чистую систему.
Панель ведёт nginx, сетевой экран (свои цепочки iptables `ispmgr_*`; `ufw` выключен), DNS
(`named`), MySQL и PHP; её службы занимают около 690 MB памяти из 956.

**Правило**: всё, чем владеет панель, меняется только её штатными средствами — командой
`/usr/local/mgr5/sbin/mgrctl -m ispmgr <функция>` или каталогами своих вставок. Ручная правка
её файлов будет переписана при следующем изменении через панель.

| Что | Как заведено |
|---|---|
| сайт `koritin84.fvds.ru` | `mgrctl -m ispmgr webdomain.edit sok=ok name=koritin84.fvds.ru owner=www-root email=… php=off php_enable=off secure=on ssl_cert=letsencrypt redirect_http=on` |
| сертификат Let's Encrypt | `mgrctl -m ispmgr letsencrypt.generate sok=ok username=www-root domain_name=koritin84.fvds.ru domain=koritin84.fvds.ru crtname=koritin84.fvds.ru_le2 enable_cert=on email=… domain_type=web`; продлевает панель (`letsencrypt.periodic`) |
| своя вставка nginx сайта | `/etc/nginx/vhosts-resources/koritin84.fvds.ru/*.conf` — панель подключает в оба блока сайта и не перезаписывает |

Предел Let's Encrypt: `fvds.ru` не входит в список публичных суффиксов, поэтому **50
сертификатов в неделю на всех клиентов FirstVDS** — выпуск может отказать «too many
certificates already issued for fvds.ru»; панель сама повторяет после названного времени.

Сетевой экран сейчас открыт (политика «принимать»): снаружи видны 22, 53, 80, 443, 1500, 1501, 8443.
Службы челленджа слушают только `127.0.0.1` и выходят наружу через nginx по HTTPS. Закрыть
лишнее — отдельная задача.

## Установлено сверх образа

| | |
|---|---|
| **uv** | 0.12.17 — `/root/.local/bin/uv` |
| git, tmux, jq, rsync | из apt |
| Python | 3.12.3 (системный); проекты служб — своим uv-окружением |

## Рабочая папка — `/opt/challenge/`

git-репозиторий (ветка `main`), фиксации делаются на сервере.

```
services/<имя>/   служба: свой uv-проект, запуск службой systemd
data/, logs/      рабочие данные и журналы (вне git)
.env              тайны, права 600, вне git
```

## Службы

### `cbr-mcp` — MCP-сервер курсов ЦБ (день 17)

- Код — `mcp_servers/cbr/` в репозитории `ai-challenge-2026`; доставка:
  `rsync -a --no-owner --no-group --delete --exclude .venv --exclude __pycache__ mcp_servers/cbr/ challenge:/opt/challenge/services/cbr-mcp/`
  (без `--no-owner` файлы на сервере достались бы uid Мака), затем на сервере
  `uv sync --frozen` и `systemctl restart cbr-mcp`.
- Служба systemd `cbr-mcp` (`deploy/cbr-mcp.service`), слушает **только `127.0.0.1:8770`**.
  Работает **не от root**: временный пользователь systemd (`DynamicUser`), система только на
  чтение; запускает Python готового `.venv` напрямую, без uv.
- Наружу, два входа с одной пересылкой `deploy/nginx-mcp.conf` (подключается строкой `include`):
  - **`https://83.136.235.149:8443/mcp`** — рабочий. Блок `deploy/nginx-ip.conf` →
    `/etc/nginx/conf.d/cbr-mcp-ip.conf`; сертификат Let's Encrypt на сам IP, который панель
    получает и продлевает своим acme.sh (живёт ~6 дней, продление каждые 3 дня,
    `/usr/local/mgr5/etc/scripts/acmesh/83.136.235.149/`). nginx перечитывает его таймером
    `nginx-reload.timer` раз в сутки (`deploy/nginx-reload.{service,timer}`).
  - `https://koritin84.fvds.ru/mcp` — по имени (`/etc/nginx/vhosts-resources/koritin84.fvds.ru/mcp.conf`);
    заработает, когда панель выпустит сертификат на имя: 2026-09-22 дважды упёрся в предел fvds.ru.
- Токен — строка `CBR_MCP_TOKEN=` в `/opt/challenge/.env`. На Маке не хранится; клиент берёт
  его при запуске: `export CBR_MCP_TOKEN=$(ssh challenge "grep ^CBR_MCP_TOKEN= /opt/challenge/.env | cut -d= -f2")`.
- Проверка: без токена `curl -X POST https://koritin84.fvds.ru/mcp` — 401. Имя
  `www.koritin84.fvds.ru` сайт принимает, но служба отвечает 421 — пропускается только
  `koritin84.fvds.ru`.
