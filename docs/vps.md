# VPS челленджа

- IP: **38.180.117.69**, провайдер Inferno Solution.
- Заход с Мака юзера: **`ssh challenge`** (алиас в `~/.ssh/config`,
  ключ `~/.ssh/challenge_vps`, ed25519, root). Пароль-авторизация оставлена как запасной вход.
- ICMP закрыт снаружи — `ping` не проходит, это норма.
- OS: Ubuntu 24.04.4 LTS. Ресурсы: 1 vCPU, 961 MB RAM **+ 2 GB swap** (`/swapfile`,
  swappiness=10), диск 20 GB (~11 GB свободно). RAM в обрез — тяжёлые сборки могут
  упираться, swap спасает.

### Что установлено (native, без Docker — по выбору юзера)

| | |
|---|---|
| Python | 3.12.3 (системный) + venv + pip + pipx |
| **uv** | 0.12.7 — в `/root/.local/bin`, в PATH через `~/.bashrc` |
| **ruff** | через `uv tool` (глобально) |
| Node.js | 22 LTS (NodeSource) + npm + pnpm |
| Сборка | build-essential (gcc 13.3, make) |
| Прочее | git, tmux, jq, htop, ncdu, net-tools, dnsutils, rsync, tree, unzip |
| Firewall | ufw включён: **всё входящее закрыто, кроме SSH (22)** |
| git identity | `aleksandrkorytin` / `koritin84@gmail.com` |

**Не установлено** (ставим по факту заданий): Docker, Go, Rust, Java, nginx, БД.

### Рабочая папка на сервере — `/opt/challenge/`

git-репозиторий (ветка `main`), заготовка структуры зафиксирована.

```
services/<name>/   один сервис/агент = папка со своим uv-проектом (pyproject.toml + .venv)
data/              рабочие данные, дампы            (gitignored)
logs/              логи сервисов                    (gitignored)
bin/               вспомогательные скрипты
.env               общие секреты, gitignored, chmod 600
README.md          конвенции
smoke_test.py      проверка API одним запросом: `uv run smoke_test.py`
```

- `.env` содержит `ANTHROPIC_API_KEY` — **заполняет юзер вручную**, в репозиторий не фиксируем.
- Новый сервис:
  ```
  cd /opt/challenge/services && uv init <name> && cd <name>
  uv add anthropic python-dotenv httpx fastapi "uvicorn[standard]"
  uv run <entrypoint>.py
  ```
