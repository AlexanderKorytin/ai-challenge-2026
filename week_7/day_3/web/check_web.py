"""Проверки веб-чата без модели: вход, новый пользователь, сеансы, каталог сеанса, отказы.

Все пути — во временном каталоге: рабочие данные, каталоги сеансов, состояние и журнал.
Обмен с моделью здесь не проверяется — его проверяет живой прогон.

Запуск: `uv run python check_web.py` из каталога `web`. Код выхода 1 при хоть одном провале.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

провалов = 0


def проверить(название: str, условие: bool, подробно: str = "") -> None:
    global провалов
    if условие:
        print(f"ок      {название}")
    else:
        провалов += 1
        print(f"ПРОВАЛ  {название}{': ' + подробно if подробно else ''}")


def главная(каталог: Path) -> None:
    os.environ["SUPPORT_DATA"] = str(каталог / "данные.json")
    os.environ["SUPPORT_WEB_SESSIONS"] = str(каталог / "сеансы")
    os.environ["MYHARNESS_STATE_DIR"] = str(каталог / "state")
    os.environ["MYHARNESS_JOURNAL"] = str(каталог / "журнал.jsonl")
    from starlette.testclient import TestClient

    import app
    import store

    with TestClient(app.app) as к:
        # Предусловие: приложение работает с копией во временном каталоге, а не с данными дня.
        проверить("рабочие данные скопированы во временный каталог",
                  store.путь() == каталог / "данные.json" and store.путь().exists())

        def войти(имя: str) -> tuple[dict, dict]:
            ответ = к.post("/api/login", json={"name": имя})
            sid = ответ.cookies.get("sid", "")
            к.cookies.clear()
            return ответ.json(), {"Cookie": f"sid={sid}"}

        анна, у_анны = войти("Анна Смирнова")
        проверить("вход «Анна Смирнова»: U-01", анна["user"]["user_id"] == "U-01", str(анна))
        ирина, у_ирины = войти("Ирина")
        users = json.loads(store.путь().read_text("utf-8"))["users"]
        проверить("вход «Ирина»: заведена U-06 в рабочих данных",
                  ирина["user"]["user_id"] == "U-06" and any(п["name"] == "Ирина" for п in users), str(ирина))
        проверить("пустое имя: 400", к.post("/api/login", json={"name": "  "}).status_code == 400)

        проверить("тикеты без куки: 401", к.get("/api/tickets").status_code == 401)
        проверить("тикеты с выдуманной кукой: 401",
                  к.get("/api/tickets", headers={"Cookie": "sid=0000"}).status_code == 401)
        свои = [т["ticket_id"] for т in к.get("/api/tickets", headers=у_анны).json()["tickets"]]
        проверить("кука Анны видит T-1001 и не видит T-1002", свои == ["T-1001"], str(свои))
        проверить("у Ирины тикетов нет", к.get("/api/tickets", headers=у_ирины).json()["tickets"] == [])

        _, у_бориса = войти("борис петров")
        mcp = json.loads((каталог / "сеансы" / "U-02" / ".mcp.json").read_text("utf-8"))
        разрешено = json.loads((каталог / "сеансы" / "U-02" / ".claude" / "settings.json").read_text("utf-8"))
        проверить("каталог сеанса Бориса: SUPPORT_USER = U-02, данные — рабочая копия",
                  mcp["mcpServers"]["support"]["env"] == {"SUPPORT_USER": "U-02", "SUPPORT_DATA": str(store.путь())},
                  str(mcp["mcpServers"]["support"]))
        проверить("разрешения сеанса: чтение и три инструмента записи",
                  set(app.ЗАПИСЬ) <= set(разрешено["permissions"]["allow"])
                  and "mcp__support__get_ticket" in разрешено["permissions"]["allow"], str(разрешено))
        проверить("разрешения песочницы test/ не тронуты: записи там нет",
                  not set(app.ЗАПИСЬ) & set(json.loads(app.ШАБЛОН_РАЗРЕШЕНИЙ.read_text("utf-8"))["permissions"]["allow"]))

        чужая = к.post("/api/ask", json={"text": "привет", "model": "gpt-5"}, headers=у_бориса)
        проверить("модель вне списка: 400", чужая.status_code == 400, чужая.text)
        проверить("пустой вопрос: 400",
                  к.post("/api/ask", json={"text": " "}, headers=у_бориса).status_code == 400)
        проверить("вопрос без входа: 401", к.post("/api/ask", json={"text": "привет"}).status_code == 401)

        # Повторный вход того же пользователя заменяет прежний сеанс.
        _, у_анны_2 = войти("анна смирнова")
        проверить("повторный вход Анны: прежняя кука больше не действует",
                  к.get("/api/tickets", headers=у_анны).status_code == 401
                  and к.get("/api/tickets", headers=у_анны_2).status_code == 200)
        # История: пара в разговоре Анны поднимается при повторном входе; «Новый разговор» без
        # вопросов переживает повторный вход.
        сеанс_анны = next(с for с in app.ПРИЛОЖЕНИЕ.сеансы.values() if с.пользователь["user_id"] == "U-01")
        сеанс_анны.агент.remember("Тикет T-1001?", "Ключ не принят.")
        анна, у_анны = войти("Анна Смирнова")
        проверить("повторный вход: прежний разговор поднят", анна["history"] == [["Тикет T-1001?", "Ключ не принят."]],
                  str(анна["history"]))
        проверить("новый разговор: 200", к.post("/api/new", headers=у_анны).status_code == 200)
        анна, у_анны = войти("Анна Смирнова")
        проверить("новый разговор пережил повторный вход: истории нет", анна["history"] == [], str(анна["history"]))
        проверить("тело-массив на входе: 400", к.post("/api/login", json=["Анна"]).status_code == 400)
        проверить("тело-массив в вопросе: 400", к.post("/api/ask", json=["x"], headers=у_анны).status_code == 400)

        к.post("/api/logout", headers=у_ирины)
        проверить("после выхода кука Ирины не действует", к.get("/api/tickets", headers=у_ирины).status_code == 401)


if __name__ == "__main__":
    with tempfile.TemporaryDirectory() as каталог:
        главная(Path(каталог))
    sys.exit(1 if провалов else 0)
