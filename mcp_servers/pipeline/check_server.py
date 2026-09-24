"""Проверки сервера `pipeline` без сети.

Протокол проверяется настоящим клиентом MCP (`mcp.Client`) поверх сервера, поднятого в
процессе: подделка ответов проверила бы подделку, а не схему доводов и `structuredContent`.
Википедия подменена образцом `образцы/search.json` (снят настоящим запросом «Байкал»
2026-09-24), DeepSeek — функцией, запоминающей запрос. Настоящая функция обращения к DeepSeek
проверяется отдельно через `httpx2.MockTransport`.

Каталоги ключа и результатов — временные (`PIPELINE_CONFIG_DIR`, `PIPELINE_OUT_DIR`): проверка
не трогает настоящие настройки.

Запуск: `uv run python check_server.py`. Каждая проверка печатает «ок» или «ПРОВАЛ»; код выхода
1 при хоть одном провале.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import tempfile
from pathlib import Path

import httpx2
from mcp import Client

import server

ОБРАЗЕЦ = json.loads((Path(__file__).parent / "образцы" / "search.json").read_text("utf-8"))
КЛЮЧ = "sk-check-0123456789"
провалов = 0


def проверить(название: str, условие: bool, подробно: str = "") -> None:
    global провалов
    if условие:
        print(f"ок      {название}")
    else:
        провалов += 1
        print(f"ПРОВАЛ  {название}" + (f": {подробно}" if подробно else ""))


def текст(итог) -> str:
    return "\n".join(getattr(ч, "text", "") for ч in итог.content)


async def главная() -> None:
    временный = Path(tempfile.mkdtemp(prefix="pipeline-check-"))
    os.environ[server.ПЕРЕМЕННАЯ_НАСТРОЕК] = str(временный / "настройки")
    os.environ[server.ПЕРЕМЕННАЯ_РЕЗУЛЬТАТОВ] = str(временный / "результаты")
    проверить("путь настроек уведён во временный каталог", str(server.путь_настроек()).startswith(str(временный)))

    запросы_вики: list[tuple[str, str]] = []
    запросы_модели: list[list[dict]] = []
    ответ_модели = {"текст": "Байкал — глубочайшее озеро планеты [1].", "причина": "stop"}

    async def вики(lang: str, query: str) -> dict:
        запросы_вики.append((lang, query))
        return ОБРАЗЕЦ

    async def модель(ключ: str, имя: str, сообщения: list[dict]):
        запросы_модели.append(сообщения)
        return ответ_модели["текст"], имя, {"prompt_tokens": 10, "completion_tokens": 5}, ответ_модели["причина"]

    server._википедия = вики
    server._спросить_модель = модель
    ожидаемые = sorted(ОБРАЗЕЦ["query"]["pages"], key=lambda с: с["index"])

    async with Client(server.srv) as к:
        # --- список инструментов ---
        имена = {и.name for и in (await к.list_tools()).tools}
        проверить("три инструмента: search, summarize, saveToFile", имена == {"search", "summarize", "saveToFile"}, str(имена))

        # --- шаг 1: search ---
        итог = await к.call_tool("search", {"query": "Байкал"})
        найдено = итог.structured_content or {}
        проверить("search без ошибки", not итог.is_error, текст(итог))
        проверить("search спросил ru-раздел тем же запросом", запросы_вики == [("ru", "Байкал")], str(запросы_вики))
        проверить(
            "search: статьи в порядке выдачи, номера с 1",
            [(р["n"], р["title"]) for р in найдено.get("results", [])]
            == [(n, с["title"]) for n, с in enumerate(ожидаемые, 1)],
        )
        проверить(
            "search: длина вступления — как в образце",
            [р["chars"] for р in найдено.get("results", [])] == [len(с.get("extract", "").strip()) for с in ожидаемые],
        )
        search_id = найдено.get("search_id", "")

        # --- шаг 2: summarize без ключа — отказ с путём ---
        итог = await к.call_tool("summarize", {"search_id": search_id})
        проверить(
            "summarize без файла ключа — ошибка с путём",
            итог.is_error and str(server.путь_настроек()) in текст(итог),
            текст(итог),
        )
        проверить("без ключа модель не вызывалась", запросы_модели == [])

        server.путь_настроек().parent.mkdir(parents=True)
        server.путь_настроек().write_text(json.dumps({"api_key": КЛЮЧ}), "utf-8")

        # --- шаг 2: summarize ---
        итог = await к.call_tool("summarize", {"search_id": search_id, "focus": "глубина"})
        выжимка = итог.structured_content or {}
        проверить("summarize без ошибки", not итог.is_error, текст(итог))
        отправлено = запросы_модели[-1][-1]["content"] if запросы_модели else ""
        проверить(
            "в модель ушли ровно вступления из образца с номерами [n] по порядку",
            all(f"[{n}] {с['title']}\n{с.get('extract', '').strip() or '(вступления нет)'}" in отправлено
                for n, с in enumerate(ожидаемые, 1))
            and [отправлено.index(f"[{n}] ") for n in range(1, len(ожидаемые) + 1)]
            == sorted(отправлено.index(f"[{n}] ") for n in range(1, len(ожидаемые) + 1)),
        )
        проверить("focus дошёл до модели", "Сосредоточься на: глубина" in отправлено)
        проверить("модель по умолчанию — deepseek-v4-flash", выжимка.get("model") == "deepseek-v4-flash", str(выжимка.get("model")))
        проверить(
            "источники выжимки = результаты search",
            [(и["n"], и["title"], и["url"]) for и in выжимка.get("sources", [])]
            == [(р["n"], р["title"], р["url"]) for р in найдено.get("results", [])],
        )
        проверить("выжимка несёт usage и search_id", выжимка.get("usage", {}).get("prompt_tokens") == 10 and выжимка.get("search_id") == search_id)
        summary_id = выжимка.get("summary_id", "")

        # --- шаг 3: saveToFile ---
        итог = await к.call_tool("saveToFile", {"summary_id": summary_id, "filename": "baikal"})
        сохранено = итог.structured_content or {}
        проверить("saveToFile без ошибки", not итог.is_error, текст(итог))
        путь = Path(сохранено.get("path", "/нет"))
        проверить("файл в каталоге результатов, .md дописан", путь == (временный / "результаты" / "baikal.md").resolve(), str(путь))
        байты = путь.read_bytes() if путь.exists() else b""
        проверить("sha256 ответа = сумма файла на диске", сохранено.get("sha256") == hashlib.sha256(байты).hexdigest() and сохранено.get("bytes") == len(байты))
        содержимое = байты.decode("utf-8")
        проверить("в файле ровно текст выжимки", ответ_модели["текст"] + "\n" in содержимое)
        проверить(
            "в файле все источники с адресами",
            all(f"{n}. [{с['title']}]({с['fullurl']})" in содержимое for n, с in enumerate(ожидаемые, 1)),
        )

        # --- отказы ---
        итог = await к.call_tool("saveToFile", {"summary_id": summary_id, "filename": "baikal.md"})
        проверить("существующий файл — отказ", итог.is_error and "уже есть" in текст(итог), текст(итог))
        проверить("прежний файл цел", путь.read_bytes() == байты)
        for плохое in ["", "  ", "../x", "a/b", "a\\b", ".скрытый", "..", "a\nb", "a\tb", "a\0b"]:
            итог = await к.call_tool("saveToFile", {"summary_id": summary_id, "filename": плохое})
            проверить(f"имя {плохое!r} — отказ", итог.is_error and "недопустимое имя" in текст(итог), текст(итог))
        итог = await к.call_tool("saveToFile", {"summary_id": summary_id, "filename": "a.b"})
        проверить(
            "парная: имя a.b принимается и получает .md",
            not итог.is_error and Path((итог.structured_content or {}).get("path", "")).name == "a.b.md",
            текст(итог),
        )
        итог = await к.call_tool("summarize", {"search_id": "чужой"})
        проверить("чужой search_id — отказ", итог.is_error and "неизвестный search_id" in текст(итог), текст(итог))
        итог = await к.call_tool("saveToFile", {"summary_id": "чужой", "filename": "x"})
        проверить("чужой summary_id — отказ", итог.is_error and "неизвестный summary_id" in текст(итог), текст(итог))
        итог = await к.call_tool("search", {"query": "x", "lang": "ru.evil.com/"})
        проверить("раздел не код языка — отказ без обращения", итог.is_error and len(запросы_вики) == 1, текст(итог))

        for причина in ["length", "insufficient_system_resource", "content_filter", None]:
            ответ_модели["причина"] = причина
            до = len(server._выжимки)
            итог = await к.call_tool("summarize", {"search_id": search_id})
            проверить(f"finish_reason={причина} — ошибка, выжимка не заведена",
                      итог.is_error and f"finish_reason={причина}" in текст(итог) and len(server._выжимки) == до, текст(итог))
        ответ_модели["причина"] = "stop"
        итог = await к.call_tool("summarize", {"search_id": search_id})
        проверить("парная: finish_reason=stop — выжимка заведена", not итог.is_error, текст(итог))

        отказ = {"error": {"code": "srsearch-error", "info": "Недопустимый запрос"}}
        server._википедия = lambda lang, query: _готово(отказ)
        итог = await к.call_tool("search", {"query": "x"})
        проверить("ошибка MediaWiki — её текст, а не «ничего не найдено»",
                  итог.is_error and "Недопустимый запрос" in текст(итог) and "ничего не найдено" not in текст(итог), текст(итог))

        пустой = {"batchcomplete": True}
        server._википедия = lambda lang, query: _готово(пустой)
        итог = await к.call_tool("search", {"query": "ыыыы"})
        проверить("пустая выдача — «ничего не найдено»", итог.is_error and "ничего не найдено" in текст(итог), текст(итог))

    # --- настоящее обращение к DeepSeek через подменный транспорт ---
    server._спросить_модель = ИСХОДНАЯ_МОДЕЛЬ
    увиденное: dict = {}

    def отвечать(статус: int, тело: dict):
        def обработчик(запрос: httpx2.Request) -> httpx2.Response:
            увиденное["тело"] = json.loads(запрос.content)
            увиденное["заголовок"] = запрос.headers.get("authorization")
            return httpx2.Response(статус, json=тело)
        return обработчик

    прежний_клиент = httpx2.AsyncClient

    def клиент_с(обработчик):
        class Подменный(прежний_клиент):
            def __init__(self, **kw):
                super().__init__(transport=httpx2.MockTransport(обработчик), **kw)
        return Подменный

    try:
        server.httpx2.AsyncClient = клиент_с(отвечать(200, {
            "model": "deepseek-v4-flash",
            "choices": [{"message": {"content": "итог"}, "finish_reason": "stop"}],
            "usage": {"total_tokens": 3},
        }))
        итог = await server._спросить_модель(КЛЮЧ, "deepseek-v4-flash", [{"role": "user", "content": "x"}])
        проверить("DeepSeek: текст, модель, usage, причина", итог == ("итог", "deepseek-v4-flash", {"total_tokens": 3}, "stop"), str(итог))
        проверить("DeepSeek: рассуждения выключены, max_tokens не задан",
                  увиденное["тело"].get("thinking") == {"type": "disabled"} and "max_tokens" not in увиденное["тело"])
        проверить("DeepSeek: ключ в заголовке Authorization", увиденное["заголовок"] == f"Bearer {КЛЮЧ}")

        server.httpx2.AsyncClient = клиент_с(отвечать(200, {
            "model": None, "usage": None,
            "choices": [{"message": {"content": "итог"}, "finish_reason": "stop"}],
        }))
        итог = await server._спросить_модель(КЛЮЧ, "deepseek-v4-flash", [])
        проверить("DeepSeek: model и usage null — модель запроса и пустой usage", итог[1:3] == ("deepseek-v4-flash", {}), str(итог))

        server.httpx2.AsyncClient = клиент_с(отвечать(401, {"error": f"bad key {КЛЮЧ}"}))
        try:
            await server._спросить_модель(КЛЮЧ, "m", [])
            проверить("DeepSeek 401 — ошибка", False)
        except server.ToolError as exc:
            проверить("DeepSeek 401: ключ в тексте ошибки заменён ***", КЛЮЧ not in str(exc) and "***" in str(exc) and "401" in str(exc), str(exc))
    finally:
        server.httpx2.AsyncClient = прежний_клиент

    print(f"\nпровалов: {провалов}")


async def _готово(значение):
    return значение


ИСХОДНАЯ_МОДЕЛЬ = server._спросить_модель

if __name__ == "__main__":
    asyncio.run(главная())
    sys.exit(1 if провалов else 0)
