"""MCP-сервер цепочки из трёх инструментов: поиск в Википедии, выжимка, сохранение в файл.

`search` получает данные (вступления статей Википедии), `summarize` их обрабатывает (сжимает
моделью DeepSeek), `saveToFile` сохраняет результат (Markdown на диск). Данные между
инструментами идут **по ссылке**: модель переносит только `search_id` и `summary_id`, а тексты
лежат в памяти процесса сервера. Пересказывая найденное доводом, модель могла бы его исказить
или обрезать, и корректность передачи было бы нечем проверить.

Сервер местный: `myharness` запускает его дочерним процессом по stdio (`.mcp.json` рабочего
каталога), хранилища живут столько же, сколько соединение сеанса.

Ключ DeepSeek — в своём файле настроек `~/.config/pipeline-mcp/config.json` (права 600,
каталог переопределяет `PIPELINE_CONFIG_DIR`). MCP sampling — просьба к модели клиента — не
используется: протокол 2026-07-28 объявил его устаревшим (SEP-2577).

Запуск (транспорт stdio): `uv run --project <каталог сервера> <каталог сервера>/server.py`.
Именно `--project`, а не `--directory`: второй меняет текущий каталог процесса, и
`результаты/` легли бы в каталог сервера, а не в рабочий каталог человека (живой прогон
2026-09-24).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import httpx2
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import BaseModel, Field

ПЕРЕМЕННАЯ_НАСТРОЕК = "PIPELINE_CONFIG_DIR"
ПЕРЕМЕННАЯ_РЕЗУЛЬТАТОВ = "PIPELINE_OUT_DIR"
МОДЕЛЬ_ПО_УМОЛЧАНИЮ = "deepseek-v4-flash"  # умолчание myharness
АДРЕС_DEEPSEEK = "https://api.deepseek.com/chat/completions"
# Правила Википедии требуют назваться и дать способ связи; почта здесь не нужна — хватит адреса
# репозитория.
ПРЕДСТАВЛЕНИЕ = "myharness-pipeline/0.1 (https://github.com/AlexanderKorytin/ai-challenge-2026)"
# Те же пределы, что у клиента DeepSeek в myharness (`api.REQUEST_TIMEOUT`): чтение — десять
# минут, потому что большой запрос модель читает минутами, и обрыв раньше выдал бы живой сервис
# за мёртвый. Прочие — про установление связи.
ОЖИДАНИЕ_МОДЕЛИ = httpx2.Timeout(connect=20.0, read=600.0, write=20.0, pool=20.0)
ИНСТРУКЦИЯ_ВЫЖИМКИ = (
    "Ты сжимаешь найденные источники в связную выжимку на русском языке. Опирайся только на "
    "текст источников ниже, ничего не добавляй от себя. После каждого утверждения ставь номер "
    "источника в квадратных скобках, например [2]. Источники, не относящиеся к запросу, пропускай."
)

_ЯЗЫК = re.compile(r"[a-z][a-z-]*")


# --- модели выхода -------------------------------------------------------------------------
# Имена полей латиницей: схема уходит модели как JSON Schema, а описания полей — по-русски.


class Result(BaseModel):
    n: int = Field(description="Номер источника в выдаче, с 1; по нему выжимка ссылается [n]")
    title: str = Field(description="Заголовок статьи")
    url: str = Field(description="Адрес статьи")
    chars: int = Field(description="Длина вступления статьи в знаках")


class Found(BaseModel):
    search_id: str = Field(description="Ссылка на найденное; передайте её в summarize")
    query: str = Field(description="Поисковый запрос")
    lang: str = Field(description="Раздел Википедии")
    results: list[Result] = Field(description="Найденные статьи в порядке выдачи Википедии")


class Source(BaseModel):
    n: int = Field(description="Номер источника")
    title: str = Field(description="Заголовок статьи")
    url: str = Field(description="Адрес статьи")


class Summary(BaseModel):
    summary_id: str = Field(description="Ссылка на выжимку; передайте её в saveToFile")
    search_id: str = Field(description="Из какого поиска сделана выжимка")
    text: str = Field(description="Выжимка с пометками источников [n]")
    sources: list[Source] = Field(description="Источники выжимки")
    model: str = Field(description="Модель DeepSeek, сделавшая выжимку")
    usage: dict[str, Any] = Field(description="Расход токенов на выжимку по отчёту DeepSeek")


class Saved(BaseModel):
    path: str = Field(description="Полный путь сохранённого файла")
    bytes: int = Field(description="Размер файла в байтах")
    sha256: str = Field(description="Контрольная сумма SHA-256 записанного содержимого")
    summary_id: str = Field(description="Какая выжимка сохранена")


# --- хранилища в памяти --------------------------------------------------------------------


@dataclass(frozen=True)
class Найденное:
    query: str
    lang: str
    статьи: tuple[tuple[str, str, str], ...]  # (заголовок, адрес, вступление) в порядке выдачи


@dataclass(frozen=True)
class Сжатое:
    search_id: str
    query: str
    текст: str
    источники: tuple[Source, ...]
    модель: str


_найденное: dict[str, Найденное] = {}
_выжимки: dict[str, Сжатое] = {}


def _новый_id() -> str:
    return uuid.uuid4().hex[:12]


# --- внешние обращения (проверки их подменяют) ---------------------------------------------


async def _википедия(lang: str, query: str) -> dict:
    """Ответ MediaWiki API на поиск со вступлениями статей, одним запросом.

    Ожидаемые сбои — `ToolError`: только его текст библиотека передаёт клиенту, прочее она прячет
    за «Error executing tool …». Предел ожидания — умолчание клиента; своего числа нет.
    """
    доводы = {
        "action": "query",
        "format": "json",
        "formatversion": "2",
        "generator": "search",
        "gsrsearch": query,
        "prop": "extracts|info",
        "exintro": "1",
        "explaintext": "1",
        "exlimit": "max",
        "inprop": "url",
    }
    try:
        async with httpx2.AsyncClient(headers={"User-Agent": ПРЕДСТАВЛЕНИЕ}) as клиент:
            ответ = await клиент.get(f"https://{lang}.wikipedia.org/w/api.php", params=доводы)
            ответ.raise_for_status()
            return ответ.json()
    except httpx2.TimeoutException as exc:
        raise ToolError("Википедия не ответила вовремя") from exc
    except httpx2.HTTPStatusError as exc:
        raise ToolError(f"Википедия ответила HTTP {exc.response.status_code}") from exc
    except httpx2.HTTPError as exc:
        raise ToolError(f"нет связи с Википедией ({lang}): {type(exc).__name__}") from exc
    except ValueError as exc:
        raise ToolError("Википедия вернула не JSON") from exc


async def _спросить_модель(
    ключ: str, модель: str, сообщения: list[dict]
) -> tuple[str, str, dict, str | None]:
    """Один запрос к DeepSeek без потока: (текст, модель, usage, finish_reason).

    Рассуждения выключены: выжимка — прочтение, а не сочинение. `max_tokens` не задаётся —
    своих потолков нет. Текст ошибки DeepSeek вычищается от ключа.
    """
    тело = {"model": модель, "messages": сообщения, "thinking": {"type": "disabled"}}
    try:
        async with httpx2.AsyncClient(timeout=ОЖИДАНИЕ_МОДЕЛИ) as клиент:
            ответ = await клиент.post(
                АДРЕС_DEEPSEEK, json=тело, headers={"Authorization": f"Bearer {ключ}"}
            )
    except httpx2.TimeoutException as exc:
        raise ToolError("DeepSeek не ответил вовремя") from exc
    except httpx2.HTTPError as exc:
        raise ToolError(f"нет связи с DeepSeek: {type(exc).__name__}") from exc
    if ответ.status_code >= 400:
        текст = " ".join(ответ.text.split())
        raise ToolError(_без_ключа(f"DeepSeek ответил HTTP {ответ.status_code}: {текст}", ключ))
    try:
        данные = ответ.json()
        выбор = данные["choices"][0]
        return (
            выбор["message"]["content"] or "",
            данные.get("model") or модель,
            данные.get("usage") or {},
            выбор.get("finish_reason"),
        )
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise ToolError("DeepSeek вернул ответ непонятного вида") from exc


def _без_ключа(текст: str, ключ: str) -> str:
    return текст.replace(ключ, "***") if ключ else текст


# --- настройки и каталог результатов -------------------------------------------------------


def путь_настроек() -> Path:
    каталог = os.environ.get(ПЕРЕМЕННАЯ_НАСТРОЕК, "").strip()
    база = Path(каталог) if каталог else Path.home() / ".config" / "pipeline-mcp"
    return база / "config.json"


def _прочитать_настройки() -> tuple[str, str]:
    """(ключ, модель) из файла настроек; нет файла, порча или нет ключа — `ToolError` с путём."""
    путь = путь_настроек()
    try:
        данные = json.loads(путь.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise ToolError(f"нет ключа DeepSeek: файла {путь} нет") from None
    except (OSError, ValueError) as exc:
        raise ToolError(f"файл настроек {путь} не читается: {type(exc).__name__}") from None
    ключ = данные.get("api_key") if isinstance(данные, dict) else None
    if not isinstance(ключ, str) or not ключ.strip():
        raise ToolError(f"нет ключа DeepSeek: в {путь} нет поля api_key")
    модель = данные.get("model") or МОДЕЛЬ_ПО_УМОЛЧАНИЮ
    return ключ.strip(), str(модель)


def каталог_результатов() -> Path:
    каталог = os.environ.get(ПЕРЕМЕННАЯ_РЕЗУЛЬТАТОВ, "").strip()
    return Path(каталог) if каталог else Path.cwd() / "результаты"


def _имя_файла(filename: str) -> str:
    имя = filename.strip()
    if not имя or "/" in имя or "\\" in имя or имя.startswith(".") or any(ord(з) < 32 for з in имя):
        raise ToolError(
            f"недопустимое имя файла {filename!r}: нужно простое имя без каталогов, "
            "не начинающееся с точки"
        )
    return имя if имя.lower().endswith(".md") else имя + ".md"


# --- сервер и инструменты ------------------------------------------------------------------


srv = MCPServer(
    "pipeline",
    instructions=(
        "Цепочка из трёх инструментов: search находит статьи Википедии, summarize сжимает "
        "найденное в выжимку, saveToFile сохраняет выжимку в файл Markdown. Данные между "
        "ними передаются ссылками search_id и summary_id."
    ),
)


@srv.tool()
async def search(
    query: Annotated[str, Field(description="Что искать")],
    lang: Annotated[
        str, Field(description="Раздел Википедии: ru, en, de…; по умолчанию ru")
    ] = "ru",
) -> Found:
    """Шаг 1 цепочки: ищет статьи в Википедии и запоминает их вступления на сервере.

    Возвращает search_id и список статей. Чтобы получить выжимку найденного, следующим шагом
    вызовите summarize с этим search_id — сами тексты передавать не нужно.
    """
    запрос = query.strip()
    if not запрос:
        raise ToolError("пустой запрос")
    раздел = lang.strip().lower()
    if not _ЯЗЫК.fullmatch(раздел):
        raise ToolError(f"недопустимый раздел Википедии {lang!r}: нужен код вроде ru или en")
    ответ = await _википедия(раздел, запрос)
    # Отказ MediaWiki приходит с HTTP 200 и полем `error`, без `query`: без этой проверки он
    # выдал бы себя за пустую выдачу, и модель переформулировала бы запрос вместо починки.
    if isinstance(ответ.get("error"), dict):
        причина = ответ["error"].get("info") or ответ["error"].get("code") or "без описания"
        raise ToolError(f"Википедия отказала: {причина}")
    страницы = (ответ.get("query") or {}).get("pages") or []
    страницы = sorted(страницы, key=lambda с: с.get("index", 0))
    статьи = tuple(
        (с.get("title", ""), с.get("fullurl", ""), (с.get("extract") or "").strip())
        for с in страницы
    )
    if not статьи:
        raise ToolError(f"в Википедии ({раздел}) по запросу {запрос!r} ничего не найдено")
    search_id = _новый_id()
    _найденное[search_id] = Найденное(запрос, раздел, статьи)
    return Found(
        search_id=search_id,
        query=запрос,
        lang=раздел,
        results=[
            Result(n=n, title=заголовок, url=адрес, chars=len(вступление))
            for n, (заголовок, адрес, вступление) in enumerate(статьи, 1)
        ],
    )


@srv.tool()
async def summarize(
    search_id: Annotated[str, Field(description="search_id из ответа search")],
    focus: Annotated[
        str | None,
        Field(description="На чём сосредоточиться в выжимке; не задано — на запросе поиска"),
    ] = None,
) -> Summary:
    """Шаг 2 цепочки: сжимает найденное search в выжимку с пометками источников [n].

    Тексты статей берутся на сервере по search_id. Возвращает summary_id и текст выжимки.
    Чтобы сохранить её, следующим шагом вызовите saveToFile с этим summary_id.
    """
    найденное = _найденное.get(search_id.strip())
    if найденное is None:
        raise ToolError(f"неизвестный search_id {search_id!r}: сначала выполните search")
    ключ, модель = _прочитать_настройки()
    источники = "\n\n".join(
        f"[{n}] {заголовок}\n{вступление or '(вступления нет)'}"
        for n, (заголовок, _, вступление) in enumerate(найденное.статьи, 1)
    )
    запрос = f"Запрос: {найденное.query}\n"
    if focus and focus.strip():
        запрос += f"Сосредоточься на: {focus.strip()}\n"
    сообщения = [
        {"role": "system", "content": ИНСТРУКЦИЯ_ВЫЖИМКИ},
        {"role": "user", "content": f"{запрос}\nИсточники:\n\n{источники}"},
    ]
    текст, ответила, usage, причина = await _спросить_модель(ключ, модель, сообщения)
    # Целым ответ бывает только при `stop`: `length`, `insufficient_system_resource` и
    # `content_filter` у DeepSeek обрывают текст, и обрубок ушёл бы на диск как целая выжимка.
    if причина != "stop":
        raise ToolError(
            f"выжимка не завершена (finish_reason={причина}) — не сохраняю обрезанный текст"
        )
    текст = текст.strip()
    if not текст:
        raise ToolError("модель вернула пустую выжимку")
    summary_id = _новый_id()
    список = tuple(
        Source(n=n, title=заголовок, url=адрес)
        for n, (заголовок, адрес, _) in enumerate(найденное.статьи, 1)
    )
    _выжимки[summary_id] = Сжатое(search_id.strip(), найденное.query, текст, список, ответила)
    return Summary(
        summary_id=summary_id,
        search_id=search_id.strip(),
        text=текст,
        sources=list(список),
        model=ответила,
        usage=usage,
    )


def содержимое_файла(выжимка: Сжатое, когда: dt.datetime) -> str:
    """Markdown сохраняемой выжимки; проверка сверяет файл с этим же текстом."""
    строки = [
        f"# {выжимка.query}",
        "",
        f"Дата: {когда:%Y-%m-%d %H:%M}. Модель: {выжимка.модель}. Источник: Википедия.",
        "",
        выжимка.текст,
        "",
        "## Источники",
        "",
        *(f"{и.n}. [{и.title}]({и.url})" for и in выжимка.источники),
    ]
    return "\n".join(строки) + "\n"


@srv.tool()
async def saveToFile(
    summary_id: Annotated[str, Field(description="summary_id из ответа summarize")],
    filename: Annotated[
        str, Field(description="Имя файла без каталогов; .md дописывается, если его нет")
    ],
) -> Saved:
    """Шаг 3 цепочки: сохраняет выжимку summarize в файл Markdown в каталоге результатов.

    Текст берётся на сервере по summary_id. Существующий файл не перезаписывается — выберите
    другое имя.
    """
    выжимка = _выжимки.get(summary_id.strip())
    if выжимка is None:
        raise ToolError(f"неизвестный summary_id {summary_id!r}: сначала выполните summarize")
    имя = _имя_файла(filename)
    каталог = каталог_результатов()
    данные = содержимое_файла(выжимка, dt.datetime.now()).encode("utf-8")
    try:
        каталог.mkdir(parents=True, exist_ok=True)
        путь = каталог / имя
        with open(путь, "xb") as файл:
            файл.write(данные)
    except FileExistsError:
        raise ToolError(f"файл {каталог / имя} уже есть — выберите другое имя") from None
    except OSError as exc:
        raise ToolError(f"не удалось записать {каталог / имя}: {exc.strerror or type(exc).__name__}") from None
    return Saved(
        path=str(путь.resolve()),
        bytes=len(данные),
        sha256=hashlib.sha256(данные).hexdigest(),
        summary_id=summary_id.strip(),
    )


if __name__ == "__main__":
    srv.run()
