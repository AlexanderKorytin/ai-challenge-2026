"""Инструменты серверов MCP для модели: разрешения, соединения на сеанс, набор обмена.

`mcp_client` знает файл серверов и опрос для показа `/mcp`; этот модуль отдаёт инструменты
модели. Три решения, которые держат его устройство:

- **Разрешение — как у Claude Code.** Модели предлагаются только инструменты, разрешённые
  правилами `permissions.allow` в `.claude/settings.json` и `.claude/settings.local.json`
  рабочего каталога: тот же файл и тот же вид правил (`mcp__<сервер>`, `mcp__<сервер>__*`,
  `mcp__<сервер>__<инструмент>`). `permissions.deny` старше `allow` — правило запрета,
  написанное для Claude Code, запрещает и здесь. Неразрешённые не предлагаются вовсе: так
  Claude Code ведёт себя там, где спросить человека некого.
- **Соединение живёт весь сеанс**, как у Claude Code: не открывается заново на каждый вызов и
  на каждый обмен. Перед обменом состав сверяется с `.mcp.json` и правилами — лишнее
  закрывается, новое и умершее открывается.
- **У каждого соединения своя задача.** Вход и выход из контекста соединения библиотеки (и
  stdio, и HTTP) обязаны идти в одной задаче `asyncio`: внутри них области отмены `anyio`, и
  выход из чужой задачи ломает их. Поэтому держатель соединения — долгоживущая задача, которая
  открывает контекст и ждёт сигнала остановки, а вызовы идут через её сессию из задачи обмена.

Своих пределов нет: соединение ждётся под `MCP_TIMEOUT` (как опрос `/mcp`), вызов — под
`MCP_TOOL_TIMEOUT`, только если он задан (имя и единицы Claude Code); не задан — вызов
останавливает человек, как любой круг инструментов.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from mcp import ClientSession

from . import mcp_client
from .agent import Инструменты
from .mcp_client import Инструмент, НегоднаяЗапись, Сервер

ФАЙЛЫ_ПРАВИЛ = (Path(".claude") / "settings.json", Path(".claude") / "settings.local.json")
ПЕРЕМЕННАЯ_ПРЕДЕЛА_ВЫЗОВА = "MCP_TOOL_TIMEOUT"
# Предел API DeepSeek на имя функции: `^[a-zA-Z0-9_-]{1,64}$`. Не наш потолок — запрос с
# длинным именем сервер отвергнет целиком, вместе с вопросом человека.
ПРЕДЕЛ_ИМЕНИ_API = 64
_НЕ_ЗНАК_ИМЕНИ = re.compile(r"[^A-Za-z0-9_-]")


# --- разрешения ----------------------------------------------------------------------------


def _подходит(правило: str, полное_имя: str) -> bool:
    """Правило Claude Code против имени `mcp__<сервер>__<инструмент>`.

    Три вида и только три: `mcp__<сервер>` и `mcp__<сервер>__*` — весь сервер,
    `mcp__<сервер>__<инструмент>` — ТОЧНО этот инструмент. Правило на инструмент по приставке не
    сверяется: иначе `mcp__cbr__get_rate` разрешал бы и `mcp__cbr__get_rate__delete`, который
    сервер заведёт завтра.
    """
    хвост = правило.removeprefix("mcp__")
    if "__" not in хвост:
        # Весь сервер. `__` после имени обязателен: иначе `mcp__cb` разрешил бы `cbr`.
        return полное_имя.startswith(правило + "__")
    if хвост.endswith("__*") and "__" not in хвост[:-3]:
        return полное_имя.startswith(правило[:-1])
    return правило == полное_имя


@dataclass(frozen=True)
class Разрешения:
    """Правила MCP из файлов настроек Claude Code; прочие виды правил (`Bash(...)`) не наши."""

    allow: tuple[str, ...] = ()
    deny: tuple[str, ...] = ()

    def разрешён(self, полное_имя: str) -> bool:
        if any(_подходит(п, полное_имя) for п in self.deny):
            return False
        return any(_подходит(п, полное_имя) for п in self.allow)

    def сервер_возможен(self, сервер: str) -> bool:
        """Может ли у сервера оказаться хоть один разрешённый инструмент.

        Без этого соединение открывалось бы с каждым сервером файла, даже с тем, чьи
        инструменты модели не достанутся никогда, — и процесс `npx` запускался бы впустую.
        """
        весь = f"mcp__{сервер}"
        if весь in self.deny or f"{весь}__*" in self.deny:
            return False
        return any(п == весь or п.startswith(весь + "__") for п in self.allow)


def прочитать_разрешения(каталог: Path) -> tuple[Разрешения, str | None]:
    """Правила MCP обоих файлов вместе и жалоба на испорченный файл.

    Испорченный файл не даёт ни одного правила — ни разрешающего, ни запрещающего; раз
    запрещено всё, что не разрешено явно, из-за порчи модель ничего лишнего не получит. Жалоба
    называет путь: иначе человек искал бы, почему пропали инструменты.
    """
    allow: list[str] = []
    deny: list[str] = []
    жалобы: list[str] = []
    for имя in ФАЙЛЫ_ПРАВИЛ:
        путь = каталог / имя
        if not путь.exists():
            continue
        try:
            данные = json.loads(путь.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            жалобы.append(f"{путь}: не читается как JSON ({exc})")
            continue
        права = данные.get("permissions", {}) if isinstance(данные, dict) else None
        if not isinstance(права, dict):
            жалобы.append(f"{путь}: permissions — не объект")
            continue
        списки: list[tuple[str, list[str]]] = [("allow", allow), ("deny", deny)]
        годные = []
        for ключ, _ in списки:
            значение = права.get(ключ, [])
            if not isinstance(значение, list) or not all(isinstance(п, str) for п in значение):
                жалобы.append(f"{путь}: permissions.{ключ} — не список строк")
                break
            годные.append([п for п in значение if п.startswith("mcp__")])
        else:
            allow.extend(годные[0])
            deny.extend(годные[1])
    return Разрешения(tuple(allow), tuple(deny)), "; ".join(жалобы) or None


# --- соединение одного сервера -------------------------------------------------------------


@dataclass
class _Держатель:
    """Соединение одного сервера, живущее в своей задаче до сигнала остановки."""

    сервер: Сервер
    готово: asyncio.Event = field(default_factory=asyncio.Event)
    стоп: asyncio.Event = field(default_factory=asyncio.Event)
    сессия: ClientSession | None = None
    инструменты: tuple[Инструмент, ...] = ()
    сбой: str | None = None
    задача: asyncio.Task[None] | None = None

    def запустить(self) -> None:
        self.задача = asyncio.create_task(self._жить(), name=f"mcp:{self.сервер.имя}")

    async def _жить(self) -> None:
        ответ_http: dict[str, str] = {}
        try:
            async with mcp_client.соединение(self.сервер, ответ_http) as (сессия, _):
                self.инструменты = await mcp_client.все_инструменты(сессия, self.сервер.имя)
                self.сессия = сессия
                self.готово.set()
                await self.стоп.wait()
        except Exception as exc:  # noqa: BLE001 — сбой сервера становится жалобой, не падением
            self.сбой = mcp_client.описать_сбой(exc, self.сервер, ответ_http)
        finally:
            self.сессия = None
            self.готово.set()

    def жив(self) -> bool:
        return self.задача is not None and not self.задача.done() and self.сбой is None

    async def отвечает(self, срок: float) -> bool:
        """Отвечает ли сервер на `ping` за `срок` секунд.

        Задача держателя смерти процесса сервера не замечает: библиотека видит конец потока,
        но задача ждёт сигнала остановки, и сессия числилась бы живой — умершее соединение не
        переоткрылось бы никогда, а модель получала бы ошибку на каждый вызов.
        """
        сессия = self.сессия
        if сессия is None or not self.жив():
            return False
        try:
            await asyncio.wait_for(сессия.send_ping(), срок)
        except Exception:  # noqa: BLE001 — любой сбой проверки значит «переоткрыть»
            return False
        return True

    async def остановить(self) -> None:
        self.стоп.set()
        if self.задача is None:
            return
        if not self.готово.is_set():
            # Ещё соединяется: сигнала остановки он не дождётся — снимаем задачу.
            self.задача.cancel()
        await asyncio.gather(self.задача, return_exceptions=True)


# --- вызов ---------------------------------------------------------------------------------


def _предел_вызова() -> float | None:
    """`MCP_TOOL_TIMEOUT` в секундах; не задан — предела нет. Кривое значение — ошибка с именем."""
    текст = os.environ.get(ПЕРЕМЕННАЯ_ПРЕДЕЛА_ВЫЗОВА, "").strip()
    if not текст:
        return None
    try:
        мс = int(текст)
    except ValueError:
        мс = 0
    if мс <= 0:
        raise ValueError(
            f"{ПЕРЕМЕННАЯ_ПРЕДЕЛА_ВЫЗОВА} должна быть целым положительным числом миллисекунд"
        )
    return мс / 1000


def _текст_результата(итог: Any) -> str:
    """Текстовые части ответа сервера; прочие — пометкой; нет текста — структура JSON."""
    части: list[str] = []
    for часть in итог.content or []:
        вид = getattr(часть, "type", "")
        if вид == "text":
            части.append(часть.text)
        else:
            подробно = getattr(часть, "mime_type", None) or getattr(часть, "uri", None) or ""
            части.append(f"[{вид} {подробно}]".replace(" ]", "]"))
    if not any(части) and итог.structured_content is not None:
        return json.dumps(итог.structured_content, ensure_ascii=False)
    return "\n".join(части)


# --- соединения сеанса ---------------------------------------------------------------------


class Соединения:
    """Соединения с серверами MCP на весь сеанс терминала и набор инструментов обмена.

    Владеет держателями; кроме него к сессиям никто не обращается. Исключений из `набор` не
    выпускает: сбой сервера или файла — жалоба набора, обмен идёт без этого сервера.
    """

    def __init__(self) -> None:
        self._держатели: dict[str, _Держатель] = {}
        self._замок = asyncio.Lock()

    async def набор(self, каталог: Path) -> Инструменты | None:
        async with self._замок:
            return await self._набор(каталог)

    async def _набор(self, каталог: Path) -> Инструменты | None:
        жалобы: list[str] = []
        разрешения, жалоба = прочитать_разрешения(каталог)
        if жалоба:
            жалобы.append(жалоба)
        путь = каталог / mcp_client.ИМЯ_ФАЙЛА
        try:
            записи = mcp_client.прочитать(путь)
        except mcp_client.ОшибкаФайла as exc:
            записи = []
            жалобы.append(f"MCP: {exc}")
        нужные: dict[str, Сервер] = {}
        for запись in записи:
            if not разрешения.сервер_возможен(запись.имя):
                continue
            if isinstance(запись, НегоднаяЗапись):
                жалобы.append(f"MCP {запись.имя}: {запись.причина}")
                continue
            нужные[запись.имя] = запись

        срок = 0.0
        if нужные:
            try:
                срок = mcp_client.предел()
            except ValueError as exc:
                жалобы.append(f"MCP: {exc}")
                нужные = {}
        # Лишние, изменённые в файле и умершие — закрыть; умершие откроются ниже заново.
        # Проверки живости — одновременно: молчащие серверы задерживают обмен на один срок, а
        # не на сумму сроков.
        прежние = list(self._держатели.items())
        живые = await asyncio.gather(
            *(
                д.отвечает(срок) if нужные.get(имя) == д.сервер else asyncio.sleep(0, False)
                for имя, д in прежние
            )
        )
        for (имя, держатель), жив in zip(прежние, живые):
            if not жив:
                del self._держатели[имя]
                await держатель.остановить()
        новые = []
        for имя, сервер in нужные.items():
            if имя not in self._держатели:
                держатель = _Держатель(сервер)
                держатель.запустить()
                self._держатели[имя] = держатель
                новые.append(держатель)
        if новые:
            ожидания = {asyncio.create_task(д.готово.wait()): д for д in новые}
            await asyncio.wait(ожидания, timeout=срок)
            for задача, держатель in ожидания.items():
                задача.cancel()
                if not держатель.готово.is_set():
                    держатель.сбой = f"сервер не ответил за {mcp_client.секунды(срок)} с"
                    await держатель.остановить()

        схемы: list[dict] = []
        карта: dict[str, tuple[_Держатель, Инструмент]] = {}
        for имя in нужные:
            держатель = self._держатели.get(имя)
            if держатель is None:
                continue
            if держатель.сбой is not None or держатель.сессия is None:
                жалобы.append(f"MCP {имя}: {держатель.сбой or 'соединение закрыто'}")
                self._держатели.pop(имя, None)
                continue
            for инструмент in держатель.инструменты:
                if not разрешения.разрешён(инструмент.полное_имя):
                    continue
                функция = _НЕ_ЗНАК_ИМЕНИ.sub("_", инструмент.полное_имя)
                if len(функция) > ПРЕДЕЛ_ИМЕНИ_API:
                    жалобы.append(
                        f"MCP {имя}: имя {функция} длиннее {ПРЕДЕЛ_ИМЕНИ_API} знаков — "
                        "API DeepSeek его не примет, инструмент не предложен"
                    )
                    continue
                if функция in карта:
                    жалобы.append(
                        f"MCP {имя}: имя {функция} уже занято другим инструментом — "
                        "инструмент не предложен"
                    )
                    continue
                карта[функция] = (держатель, инструмент)
                схемы.append(
                    {
                        "type": "function",
                        "function": {
                            "name": функция,
                            "description": инструмент.описание,
                            "parameters": инструмент.схема,
                        },
                    }
                )

        async def исполнить(имя: str, доводы: str) -> str:
            пара = карта.get(имя)
            if пара is None:
                raise PermissionError(f"инструмент {имя} не разрешён")
            держатель, инструмент = пара
            # Разрешение — ещё раз на миг вызова: человек мог снять его посреди круга.
            if not прочитать_разрешения(каталог)[0].разрешён(инструмент.полное_имя):
                raise PermissionError(f"инструмент {инструмент.полное_имя} не разрешён")
            try:
                разобранные = json.loads(доводы) if доводы.strip() else {}
            except json.JSONDecodeError as exc:
                raise ValueError(f"доводы не JSON: {exc}") from None
            if not isinstance(разобранные, dict):
                raise ValueError("доводы инструмента — не объект JSON")
            сессия = держатель.сессия
            if сессия is None:
                raise ConnectionError(f"соединение с сервером {держатель.сервер.имя} потеряно")
            предел_вызова = _предел_вызова()
            try:
                if предел_вызова is None:
                    итог = await сессия.call_tool(инструмент.имя, разобранные)
                else:
                    async with asyncio.timeout(предел_вызова) as рамка:
                        итог = await сессия.call_tool(инструмент.имя, разобранные)
            except TimeoutError:
                # Истечение узнаётся по своей рамке: `TimeoutError` из глубины библиотеки
                # пределом `MCP_TOOL_TIMEOUT` не является, и сказать так значило бы соврать.
                if предел_вызова is None or not рамка.expired():
                    raise RuntimeError(
                        mcp_client.вычистить("TimeoutError в библиотеке MCP", держатель.сервер)
                    ) from None
                raise TimeoutError(
                    f"сервер не ответил за {mcp_client.секунды(предел_вызова)} с "
                    f"({ПЕРЕМЕННАЯ_ПРЕДЕЛА_ВЫЗОВА})"
                ) from None
            except Exception as exc:  # noqa: BLE001 — сообщение библиотеки может цитировать тайну
                raise RuntimeError(mcp_client.описать_сбой(exc, держатель.сервер, {})) from None
            текст = _текст_результата(итог)
            if итог.is_error:
                # Ошибка сервера могла процитировать подставленный токен — он заменяется. Строки
                # и буквальные значения не трогаются: это текст для модели, а не строка сбоя.
                for тайна in mcp_client.значения_переменных(держатель.сервер):
                    текст = текст.replace(тайна, "***")
                raise RuntimeError(текст or "сервер вернул ошибку без текста")
            # Успешный результат — данные сервера, и модель получает их как есть: вычистка
            # строки сбоя склеила бы строки и вырезала бы из чисел короткие значения `env`.
            return текст

        жалоба_набора = "; ".join(жалобы) or None
        if not схемы and жалоба_набора is None:
            return None
        return Инструменты(схемы, исполнить, жалоба=жалоба_набора)

    async def закрыть(self) -> None:
        """Закрыть все соединения; процессов местных серверов после него не остаётся."""
        держатели = list(self._держатели.values())
        self._держатели.clear()
        await asyncio.gather(*(д.остановить() for д in держатели), return_exceptions=True)
