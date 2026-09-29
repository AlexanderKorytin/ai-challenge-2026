"""MCP-сервер поиска по индексу знаний проекта: один инструмент `search`.

Поиск живёт в одном месте — здесь, поверх `store` и `embedder`, — а наружу выходит протоколом
MCP: режим RAG агента `myharness` зовёт его перед каждым обменом (поле профиля `rag`), и тот же
сервер можно подключить к любой среде, умеющей MCP.

Ответ — текст, куски по убыванию близости, между кусками пустая строка:

    [1] openspec/specs/config/spec.md › Requirement: Права файла настроек (0.712)
    <текст куска>

Строка-заголовок куска всегда начинается с `[n] <путь> ›` — по ней `rag/eval.py` узнаёт, какие
источники были найдены. Менять формат только вместе с ним.

Стратегия нарезки — `structure` (победитель сравнения дня 21). Индекс — `$RAG_INDEX`, иначе
`rag/index.sqlite`. На диск сервер не пишет, ключей не знает.

Запуск (stdio): `uv run --project <каталог rag> <каталог rag>/server.py`.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import embedder as эм
import store

СТРАТЕГИЯ = "structure"
ИНДЕКС_ПО_УМОЛЧАНИЮ = Path(__file__).resolve().parent / "index.sqlite"


def путь_индекса() -> Path:
    return Path(os.environ.get("RAG_INDEX") or ИНДЕКС_ПО_УМОЛЧАНИЮ)


def найти_текст(вопрос: str, k: int, индекс: Path, считать=эм.эмбеддинги) -> str:
    """k ближайших кусков текстом по договору формата (см. описание модуля)."""
    if k < 1:
        raise ValueError(f"k должно быть не меньше 1, получено {k}")
    if not индекс.is_file():
        raise LookupError(f"индекса {индекс} нет — соберите: rag/cli.py index")
    if СТРАТЕГИЯ not in store.стратегии(индекс):
        raise LookupError(f"в индексе {индекс} нет сборки стратегии {СТРАТЕГИЯ}")
    вектор = считать([вопрос])[0]
    части = []
    for место, (близость, кусок) in enumerate(store.найти(индекс, СТРАТЕГИЯ, вектор, k), 1):
        раздел = кусок.section or кусок.title
        части.append(f"[{место}] {кусок.source} › {раздел} ({близость:.3f})\n{кусок.text.strip()}")
    return "\n\n".join(части)


srv = MCPServer(
    "rag",
    instructions="Поиск по знаниям проекта: требования openspec/specs, документы docs/, CLAUDE.md.",
)


@srv.tool()
def search(
    query: Annotated[str, Field(description="Вопрос или запрос своими словами")],
    k: Annotated[int, Field(description="Сколько кусков вернуть; по умолчанию 5")] = 5,
) -> str:
    """Находит в базе знаний проекта куски, ближайшие к запросу по смыслу (эмбеддинги bge-m3).

    Ответ: куски по убыванию близости, у каждого строка `[n] <путь> › <раздел> (близость)` и текст."""
    try:
        return найти_текст(query, k, путь_индекса())
    except (эм.ОшибкаЭмбеддера, LookupError, ValueError) as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    srv.run()
