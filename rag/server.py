"""MCP-сервер поиска по индексу знаний проекта: один инструмент `search`.

Поиск живёт в одном месте — здесь, поверх `store` и `embedder`, — а наружу выходит протоколом
MCP: режим RAG агента `myharness` зовёт его перед каждым обменом (поле профиля `rag`), и тот же
сервер можно подключить к любой среде, умеющей MCP.

Ответ — текст, куски по убыванию близости, между кусками пустая строка:

    [1] openspec/specs/config/spec.md › Requirement: Права файла настроек (0.712)
    <текст куска>

Строка-заголовок куска всегда начинается с `[n] <путь> ›` — по ней `rag/eval.py` узнаёт, какие
источники были найдены. Менять формат только вместе с ним.

В скобках — счёт режима: косинус у `base` и `threshold`, слияние по местам (RRF) у
`heuristic`, вероятность перекрёстного кодировщика у `model` (см. `rerank.py`). Если порог
отсёк всех кандидатов, ответ — одна строка `В базе не нашлось достаточно близкого (…)` без
заголовков: блок найденного у агента остаётся, кусков в нём ноль.

Стратегия нарезки — `structure` (победитель сравнения дня 21). Индекс — `$RAG_INDEX`, иначе
`rag/index.sqlite`. На диск сервер не пишет.

Второй этап задаётся окружением запуска (читает его только точка входа):

- `RAG_MODE` — `base` (умолчание, поиск дня 22), `threshold`, `heuristic`, `model`;
- `RAG_REWRITE` — `1`: перед поиском вопрос переписывается через DeepSeek (`rewrite.py`, ключ
  из настроек `myharness` только чтением);
- `RAG_K2` — сколько кусков отдать (умолчание 5, как в дне 22); довод `k` инструмента его
  перекрывает;
- `RAG_K1` — сколько кандидатов по косинусу берёт второй этап;
- `RAG_THRESHOLD` — порог отсечения: косинус у `threshold`/`heuristic`, вероятность у `model`.

Режимам кроме `base` `RAG_K1` и `RAG_THRESHOLD` обязательны: умолчания порога нет, его
выбирает сравнение поиска. `base` порога не принимает.

Запуск (stdio): `uv run --project <каталог rag> <каталог rag>/server.py`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Mapping

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from pydantic import Field

import embedder as эм
import rerank as рр
import rewrite as пер

СТРАТЕГИЯ = "structure"
ИНДЕКС_ПО_УМОЛЧАНИЮ = Path(__file__).resolve().parent / "index.sqlite"
K2_ПО_УМОЛЧАНИЮ = 5


@dataclass(frozen=True)
class Настройки:
    режим: str = "base"
    переписывать: bool = False
    k1: int | None = None  # у base не нужен: K₁ приравнивается K₂
    k2: int = K2_ПО_УМОЛЧАНИЮ
    порог: float | None = None


def настройки_из_окружения(окружение: Mapping[str, str]) -> Настройки:
    """Разбор `RAG_*`; неполные или противоречивые настройки — `ValueError` до первого поиска."""
    режим = окружение.get("RAG_MODE") or "base"
    if режим not in рр.РЕЖИМЫ:
        raise ValueError(f"RAG_MODE={режим!r}: нужен один из {', '.join(рр.РЕЖИМЫ)}")
    переписывание = окружение.get("RAG_REWRITE") or "0"
    if переписывание not in ("0", "1"):
        raise ValueError(f"RAG_REWRITE={переписывание!r}: нужно 0 или 1")

    def число(имя: str, вид: type):
        значение = окружение.get(имя)
        if not значение:
            return None
        try:
            return вид(значение)
        except ValueError:
            raise ValueError(f"{имя}={значение!r}: не число") from None

    k1, k2, порог = число("RAG_K1", int), число("RAG_K2", int), число("RAG_THRESHOLD", float)
    if режим == "base":
        if порог is not None or k1 is not None:
            raise ValueError("режим base не принимает RAG_K1 и RAG_THRESHOLD: K₁ у него равен K₂, порога нет")
    elif k1 is None or порог is None:
        raise ValueError(f"режиму {режим} нужны RAG_K1 и RAG_THRESHOLD")
    настройки = Настройки(режим, переписывание == "1", k1, K2_ПО_УМОЛЧАНИЮ if k2 is None else k2, порог)
    for имя, значение in (("RAG_K1", настройки.k1), ("RAG_K2", настройки.k2)):
        if значение is not None and значение < 1:
            raise ValueError(f"{имя} должно быть не меньше 1, получено {значение}")
    return настройки


def путь_индекса() -> Path:
    return Path(os.environ.get("RAG_INDEX") or ИНДЕКС_ПО_УМОЛЧАНИЮ)


def открыть_поиск(индекс: Path, считать=эм.эмбеддинги, оценщик: рр.Оценщик | None = None) -> рр.Поиск:
    if not индекс.is_file():
        raise LookupError(f"индекса {индекс} нет — соберите: rag/cli.py index")
    return рр.Поиск(индекс, СТРАТЕГИЯ, считать, оценщик)


def найти_текст(вопрос: str, поиск: рр.Поиск, настройки: Настройки, k: int | None = None,
                переписать=пер.переписать) -> str:
    """Отобранные куски текстом по договору формата (см. описание модуля); `k` перекрывает K₂."""
    k2 = настройки.k2 if k is None else k
    переписанный = переписать(вопрос) if настройки.переписывать else None
    k1 = настройки.k1 if настройки.k1 is not None else k2
    итог = поиск.отобрать(вопрос, настройки.режим, k1, k2, настройки.порог, переписанный)
    if not итог.куски:
        return f"В базе не нашлось достаточно близкого (режим {настройки.режим}, порог {настройки.порог})."
    части = []
    for место, найденный in enumerate(итог.куски, 1):
        кусок = найденный.кусок
        раздел = кусок.section or кусок.title
        части.append(f"[{место}] {кусок.source} › {раздел} ({найденный.счёт:.3f})\n{кусок.text.strip()}")
    return "\n\n".join(части)


# Настройки разбирает точка входа при запуске, чтобы ошибка окружения остановила сервер сразу,
# а не всплыла в первом обмене. Поиск (куски, матрица, BM25; кодировщик — ещё позже)
# открывается при первом вызове один раз на процесс: без индекса сервер всё равно поднимается и
# отвечает понятной ошибкой инструмента.
_настройки = Настройки()
_поиск: рр.Поиск | None = None

srv = MCPServer(
    "rag",
    instructions="Поиск по знаниям проекта: требования openspec/specs, документы docs/, CLAUDE.md.",
)


@srv.tool()
def search(
    query: Annotated[str, Field(description="Вопрос или запрос своими словами")],
    k: Annotated[int | None, Field(description="Сколько кусков вернуть; по умолчанию — настройка сервера")] = None,
) -> str:
    """Находит в базе знаний проекта куски, ближайшие к запросу по смыслу (эмбеддинги bge-m3).

    Ответ: куски по убыванию счёта, у каждого строка `[n] <путь> › <раздел> (счёт)` и текст."""
    global _поиск
    try:
        if _поиск is None:
            _поиск = открыть_поиск(путь_индекса())
        return найти_текст(query, _поиск, _настройки, k)
    except (эм.ОшибкаЭмбеддера, пер.ОшибкаПереписывания, LookupError, ValueError) as exc:
        raise ToolError(str(exc)) from None


if __name__ == "__main__":
    _настройки = настройки_из_окружения(os.environ)
    srv.run()
