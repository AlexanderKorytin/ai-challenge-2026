"""Память инструмента между запусками: где она лежит и как читается обратно.

Каталог выбран не тот, где настройки, и не тот, где кэш. По соглашениям Unix `~/.config`
держит то, что человек правит руками, `~/.cache` — то, что не жалко потерять и что
восстановится само, а `~/.local/state` — накопленное между запусками, которое терять жалко,
но и в руках вертеть незачем. Разговор и факты о пользователе — ровно третий случай:
удаление файла настроек не должно уносить с собой переписку, а чистка кэша — тем более.

Модуль намеренно не знает ни про сеть, ни про терминал: это чистая логика, и проверяется она
целиком без ключа API и без настоящего терминала. Из пакета ему нужен ровно один сосед —
`compact`, чтобы собрать прочитанную выжимку обратно в её тип, — и берётся он отложенно, уже
в теле функции: наверху этот импорт замкнул бы круг `memory` → `compact` → `profiles` →
`agent` → `memory`.

Хранилище — вспомогательный механизм. Обмен с моделью, дошедший до ответа, уже оплачен, и
уронить его отказом диска нельзя: поэтому запись возвращает ошибку текстом, а чтение
испорченного файла возвращает предупреждения, но не исключение.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from stat import S_ISREG
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:  # только подсказка типов: на выполнении этого импорта не происходит
    from .compact import Выжимка


def state_dir() -> Path:
    """Каталог состояния. Переопределяется `$MYHARNESS_STATE_DIR` — по тому же правилу, что
    каталог настроек: всё, что пишет в домашний каталог, обязано иметь переопределение пути,
    иначе проверки работают по живым файлам пользователя."""
    override = os.environ.get("MYHARNESS_STATE_DIR")
    return Path(override).expanduser() if override else Path.home() / ".local" / "state" / "myharness"


def _safe_name(text: str) -> str:
    """Кодирует текст в безопасное имя файла — и делает это **обратимо**.

    Буквы и цифры любого алфавита и `_` остаются как есть: имя читается глазами, и кириллица
    в нём остаётся кириллицей. Разделитель пути `/` даёт `-` — так имя папки проекта выглядит
    как путь. Всё прочее, включая сам знак `-` и пробел, уходит в `%XX` по байтам UTF-8.

    Обратимость тут не украшение, а сохранность разговоров. Замена всего небезопасного на один
    `-` сваливала профили `моя схема`, `моя/схема`, `моя.схема` и `моя-схема` в одно имя файла:
    четыре разных собеседника читали один разговор. С путями каталогов то же — `/a/b c` и
    `/a/b-c` делили переписку."""
    куски: list[str] = []
    for знак in text:
        if знак.isalnum() or знак == "_":
            куски.append(знак)
        elif знак == "/":
            куски.append("-")
        else:
            куски.append("".join(f"%{байт:02X}" for байт in знак.encode("utf-8")))
    return "".join(куски)


def project_key(cwd: Path) -> str:
    """Имя папки, в которой лежат разговоры этого рабочего каталога.

    Кодируется **абсолютный** путь: запуск из `./test` и из `test` — один и тот же каталог,
    и разговор у них обязан быть один. Вид `-Users-имя-проект` выбран за то, что читается
    глазами, — человек находит свой разговор в каталоге состояния без подсказок."""
    return _safe_name(str(cwd.resolve()))


def sessions_dir(cwd: Path) -> Path:
    return state_dir() / "projects" / project_key(cwd)


def facts_dir() -> Path:
    """Каталог глобальных фактов — один на все проекты, потому что человек один и тот же в
    любой папке. Внутри — файл на факт.

    Оглавления рядом с фактами намеренно нет, хотя у образца — памяти Claude Code — оно есть:
    общий изменяемый файл был бы единственным местом, где две запущенные копии инструмента
    сталкиваются. Факты короткие, прочитать весь каталог при запуске дёшево, и каталог сам
    себе оглавление."""
    return state_dir() / "memory"


def make_private_dir(directory: Path) -> None:
    """Создаёт каталог и закрывает правами 700 каждый уровень внутри каталога состояния.

    Имя без подчёркивания: тем же способом заводят свои каталоги рабочая память задачи и
    карточка проекта — они лежат в том же каталоге состояния и закрываться обязаны так же.

    Одного `mkdir(mode=0o700)` мало: родительские каталоги создаются с правами по умолчанию,
    и файл 600 оказался бы в папке 755. Закрывать есть что — имя папки проекта само по себе
    рассказывает соседям по машине, над чем человек работает."""
    directory.mkdir(parents=True, exist_ok=True)
    корень = state_dir()
    for уровень in (directory, *directory.parents):
        if уровень == корень or корень in уровень.parents:
            os.chmod(уровень, 0o700)


def write_private(path: Path, content: str, *, subject: str = "файл") -> str | None:
    """Кладёт текст в файл каталога состояния целиком; ошибку возвращает строкой, не бросает.

    `subject` — то, чем файл является для человека, В ВИНИТЕЛЬНОМ ПАДЕЖЕ: «факт», «карточку
    проекта», «рабочую память». Жалоба «не удалось записать файл» верна, но бесполезна:
    человек, набравший `/remember`, должен узнать, что не записался именно факт.

    Появление файла одним движением: черновик рядом, права ему, затем подмена имени. Читатель
    видит либо целый файл, либо прежний — половины документа не бывает никогда. Права
    выставляются черновику, а не итогу: подмена имени их сохраняет, и окна, в котором файл
    лежит с правами по умолчанию, не возникает вовсе.

    Номер процесса в имени черновика — чтобы две запущенные копии инструмента не писали в
    один черновик. Имя начинается с точки: перечисление каталогов идёт по «*.md», и черновик
    в него не попадает.

    Сообщением, а не исключением, по общему правилу хранилища: обмен с моделью, дошедший до
    ответа, уже оплачен, и уронить его отказом диска нельзя.
    """
    черновик = path.parent / f".{path.stem}.{os.getpid()}.tmp"
    try:
        make_private_dir(path.parent)
        черновик.write_text(content, encoding="utf-8")
        os.chmod(черновик, 0o600)
        os.replace(черновик, path)
    except OSError as exc:
        with contextlib.suppress(OSError):
            черновик.unlink()
        return f"не удалось записать {subject} ({path}): {exc}"
    return None


class SessionStore:
    """Один разговор — один файл, куда строки только дописываются.

    Дописывание, а не перезапись: файл растёт по строке на реплику, и обрыв процесса портит
    в худшем случае последнюю строку, а не весь разговор."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def touch(self) -> str | None:
        """Завести пустой файл разговора. Возвращает текст ошибки — и никогда не бросает.

        Нужно там, где «последний разговор» обязан смениться немедленно, до первой реплики:
        иначе очистка памяти не переживает перезапуск, потому что самым свежим на диске
        остаётся прежний файл."""
        try:
            make_private_dir(self.path.parent)
            self.path.touch(exist_ok=True)
            os.chmod(self.path, 0o600)
        except OSError as exc:
            return f"не удалось завести файл разговора ({self.path}): {exc}"
        return None

    def append(self, role: str, content: str, meta: dict[str, Any] | None = None) -> str | None:
        """Дописывает одну строку JSON. Возвращает текст ошибки — и никогда не бросает."""
        record: dict[str, Any] = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "role": role,
            "content": content,
        }
        for ключ, значение in (meta or {}).items():
            # Поля meta не перекрывают служебные. Роль задаёт чередование сообщений в запросе
            # к API: подменённая ролью из meta, она собрала бы разговор задом наперёд.
            if ключ not in record:
                record[ключ] = значение
        try:
            # Сериализация — до открытия файла: содержимое meta приходит от вызывающего кода,
            # и несериализуемое значение обязано остаться текстом ошибки, а не исключением
            # посреди записи. Гарантия «не бросает» дана безусловно — TypeError и ValueError
            # от json.dumps ловим наравне с отказом диска.
            строка = json.dumps(record, ensure_ascii=False)
            make_private_dir(self.path.parent)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(строка + "\n")
            # Права выставляем при каждой записи, а не только при создании: файл разговора —
            # личная переписка, и попасть на диск с правами по умолчанию он не должен.
            os.chmod(self.path, 0o600)
        except (OSError, TypeError, ValueError) as exc:
            return f"не удалось записать разговор ({self.path}): {exc}"
        return None

    def append_pair(
        self,
        user: str,
        assistant: str,
        meta: dict[str, Any] | None = None,
        *,
        приложение: dict[str, Any] | None = None,
    ) -> str | None:
        """Дописывает завершённую пару за одно открытие файла.

        Обе записи сериализуются до первого обращения к диску. Поэтому непригодная пометка
        ответа не может оставить в разговоре одинокий вопрос.

        `приложение` — то, что круг вызовов инструментов оставил внутри пары: рассуждения
        итогового ответа (`reasoning_content`) и промежуточные сообщения (`звенья`). Оба
        ложатся полями записи ответа, а не отдельными строками: правило «файл — это пары»
        держат восстановление, окно, сжатие и стратегии, и отдельные строки сломали бы
        чередование во всех этих местах сразу. Прежний читатель лишние поля пропускает.
        """
        try:
            отметка = datetime.now(UTC).isoformat(timespec="seconds")
            вопрос: dict[str, Any] = {"ts": отметка, "role": "user", "content": user}
            ответ: dict[str, Any] = {"ts": отметка, "role": "assistant", "content": assistant}
            for ключ, значение in (meta or {}).items():
                if ключ not in ответ:
                    ответ[ключ] = значение
            приложение = приложение or {}
            рассуждения = приложение.get("reasoning_content")
            звенья = приложение.get("звенья")
            # Поля приложения пишутся после meta и перекрывают её одноимённые ключи: это данные
            # самой пары, а не пометка о ней. Служебные поля (время, роль, текст) неприкосновенны
            # — их здесь нет среди перечисленных ключей по построению.
            if isinstance(рассуждения, str) and рассуждения:
                ответ["reasoning_content"] = рассуждения
            if isinstance(звенья, list) and звенья:
                ответ["звенья"] = звенья
            payload = (
                json.dumps(вопрос, ensure_ascii=False)
                + "\n"
                + json.dumps(ответ, ensure_ascii=False)
                + "\n"
            ).encode("utf-8")
        except (AttributeError, TypeError, ValueError) as exc:
            return f"не удалось записать разговор ({self.path}): {exc}"
        return _append_bytes(self.path, payload, "разговор")


@dataclass
class Restored:
    """Итог чтения файла разговора. `saved_pairs` — сколько пар лежит в файле, `pairs` — что
    из них поместилось в окно: человеку называют оба числа, иначе молча подставленный кусок
    прошлого разговора он отлаживает как «модель отвечает не на то»."""

    pairs: list[tuple[str, str]] = field(default_factory=list)
    saved_pairs: int = 0
    last_ts: str = ""
    warnings: list[str] = field(default_factory=list)
    fingerprint_changed: bool = False
    # Той же длины и в том же порядке, что `pairs`: окно режет оба списка одинаково, и
    # приложение пары находится по её номеру. У пары без приложения — пустой словарь.
    приложения: list[dict[str, Any]] = field(default_factory=list)


def звенья_годны(значение: Any) -> bool:
    """Звенья — список словарей с ролью ответа модели или результата инструмента.

    Смысл звеньев здесь не проверяется: это делает сервер. Проверяется только то, без чего
    раскрытая пара нарушила бы чередование ролей в запросе — роль `user` или `system` внутри
    пары собрала бы разговор не в том порядке."""
    return isinstance(значение, list) and all(
        isinstance(звено, dict) and звено.get("role") in ("assistant", "tool")
        for звено in значение
    )


def _приложение_записи(запись: dict[str, Any], номер: int, предупреждения: list[str]) -> dict[str, Any]:
    """Достаёт приложение из записи ответа. Порча поля теряет только поле, а не пару:
    ответ сам по себе цел, и выбросить его из-за вспомогательных данных значило бы потерять
    разговор ради того, что без него прекрасно обходится."""
    приложение: dict[str, Any] = {}
    if "reasoning_content" in запись:
        рассуждения = запись["reasoning_content"]
        if isinstance(рассуждения, str):
            if рассуждения:
                приложение["reasoning_content"] = рассуждения
        else:
            предупреждения.append(f"строка {номер}: рассуждения испорчены — пара без них")
    if "звенья" in запись:
        звенья = запись["звенья"]
        if звенья_годны(звенья):
            if звенья:
                приложение["звенья"] = звенья
        else:
            предупреждения.append(f"строка {номер}: звенья испорчены — пара без них")
    return приложение


def read_session(path: Path, *, window: int, system_fp: str) -> Restored:
    """Читает файл разговора в пары «вопрос, ответ».

    Разбор не имеет права ни упасть, ни оборвать сбор: испорченная строка пропускается,
    незнакомая роль отбрасывается, нарушенный порядок **пересинхронизирует** чтение —
    повисший вопрос и ответ без вопроса отбрасываются, а следующая строка читается как ни в
    чём не бывало. Обрыв сбора здесь был бы катастрофой: испорченная строка ответа оставляет
    вопрос повисшим, и первый же следующий вопрос унёс бы весь остаток разговора — ровно то,
    ради сохранности чего и выбрано дописывание вместо перезаписи.

    Роль при этом не угадывается: API требует строгого чередования `user`/`assistant`, и
    догадка упала бы позже, уже на стороне сервера, без видимой причины. `window` равен нулю —
    окно выключено, берутся все пары."""
    итог = Restored()
    if not path.exists():
        return итог
    try:
        сырые = path.read_bytes()
    except OSError as exc:
        итог.warnings.append(f"не удалось прочитать разговор ({path}): {exc}")
        return итог
    try:
        содержимое_файла = сырые.decode("utf-8")
    except UnicodeDecodeError:
        # Обрыв процесса посреди многобайтового знака — самый вероятный вид порчи: в кириллице
        # знак занимает два байта. Читаем с заменой: потерять весь разговор из-за половины
        # буквы хуже, чем показать её вопросительным ромбом.
        содержимое_файла = сырые.decode("utf-8", errors="replace")
        итог.warnings.append("часть знаков в файле разговора испорчена — заменены, остальное прочитано")

    последний_отпечаток: str | None = None
    вопрос: str | None = None
    строка_вопроса = 0
    for номер, сырая in enumerate(содержимое_файла.splitlines(), 1):
        if not сырая.strip():
            continue
        try:
            запись = json.loads(сырая)
        except json.JSONDecodeError:
            итог.warnings.append(f"строка {номер} испорчена — пропущена")
            continue
        if not isinstance(запись, dict):
            итог.warnings.append(f"строка {номер} испорчена — пропущена")
            continue

        роль = запись.get("role")
        if роль not in ("user", "assistant"):
            итог.warnings.append(f"строка {номер}: неизвестная роль — запись отброшена")
            continue
        содержимое = запись.get("content")
        if not isinstance(содержимое, str):
            # Отдельная ветка не ради красоты: назвать оборванную сериализацию «неизвестной
            # ролью» — послать человека искать роль, которой в файле нет.
            итог.warnings.append(f"строка {номер}: содержимое не текст — запись отброшена")
            continue

        # Отметку времени и отпечаток берём только с принятых записей: иначе судьбу строки
        # «инструкция изменилась» решала бы запись, которую разбор сам же и выбросил.
        отметка = запись.get("ts")
        if isinstance(отметка, str) and отметка:
            итог.last_ts = отметка
        отпечаток = запись.get("system_fp")
        if isinstance(отпечаток, str):
            последний_отпечаток = отпечаток

        if роль == "user":
            if вопрос is not None:
                итог.warnings.append(f"строка {строка_вопроса}: вопрос остался без ответа — отброшен")
            вопрос, строка_вопроса = содержимое, номер
        else:
            if вопрос is None:
                итог.warnings.append(f"строка {номер}: ответ без вопроса — отброшен")
                continue
            итог.pairs.append((вопрос, содержимое))
            итог.приложения.append(_приложение_записи(запись, номер, итог.warnings))
            вопрос = None

    if вопрос is not None:
        # Нечётный хвост: память обязана идти парами, иначе сервер откажет в запросе целиком.
        итог.warnings.append(f"строка {строка_вопроса}: вопрос остался без ответа — отброшен")

    итог.saved_pairs = len(итог.pairs)
    if window > 0:
        итог.pairs = итог.pairs[-window:]
        итог.приложения = итог.приложения[-window:]
    итог.fingerprint_changed = последний_отпечаток is not None and последний_отпечаток != system_fp
    return итог


def profile_dir(cwd: Path, profile: str, strategy: str = "standard") -> Path:
    """Каталог разговоров ключа «рабочий каталог, профиль, стратегия».

    Для `standard` возвращает прежний каталог профиля без дополнительного уровня. Остальные
    стратегии лежат ниже него и кодируются тем же правилом, что профиль: даже значение `..`
    не может вывести запись наружу.
    """
    каталог = sessions_dir(cwd) / (_safe_name(profile) or "-")
    if strategy == "standard":
        return каталог
    return каталог / (_safe_name(strategy) or "-")


def latest_session(cwd: Path, profile: str, strategy: str = "standard") -> Path | None:
    """Самая свежая сессия соответствующего ключа — или `None`, если её ещё не было.

    Свежесть считается по времени изменения файла, а не по имени. Посторонние расширения и
    вложенные каталоги с окончанием `.jsonl` сессиями не считаются.
    """
    свежайшая: Path | None = None
    # Ключ сравнения — пара «время, имя»: наносекундный st_mtime совпадает редко, но после
    # копирования каталога состояния или rsync — запросто, и тогда выбор разговора решался бы
    # порядком, в котором каталог отдала файловая система, то есть жребием.
    ключ_свежайшей: tuple[float, str] = (float("-inf"), "")
    try:
        содержимое = list(profile_dir(cwd, profile, strategy).iterdir())
    except OSError:
        # Каталога ещё нет — это первый запуск с этим профилем, обычное дело, а не беда.
        return None
    for файл in содержимое:
        if файл.suffix != ".jsonl":
            continue
        try:
            # is_file() и stat() одним обращением: файл мог исчезнуть между перечислением
            # каталога и проверкой, и это не повод падать.
            сведения = файл.stat()
        except OSError:
            continue
        if not S_ISREG(сведения.st_mode):
            # Вложенный каталог с подходящим именем разговором не является.
            continue
        ключ = (сведения.st_mtime, файл.name)
        if ключ > ключ_свежайшей:
            свежайшая, ключ_свежайшей = файл, ключ
    return свежайшая


def new_session(cwd: Path, profile: str, strategy: str = "standard") -> Path:
    """Возвращает путь новой сессии, не создавая файл.

    Случайное имя исключает гонку счётчика и ничего не сообщает о времени разговора.
    Дополнительный уровень появляется только у новых стратегий; старый вызов из двух
    аргументов остаётся на прежнем пути.
    """
    return profile_dir(cwd, profile, strategy) / f"{uuid4().hex}.jsonl"

РАСШИРЕНИЕ_ФАКТОВ_РАЗГОВОРА = ".facts.json"


def _is_utc_timestamp(value: Any) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return False
    return parsed.tzinfo is not None and parsed.utcoffset() == UTC.utcoffset(parsed)


def _append_bytes(path: Path, payload: bytes, subject: str) -> str | None:
    """Дописывает готовые строки, отделяя их от оборванного хвоста без `\n`."""
    try:
        make_private_dir(path.parent)
        with path.open("a+b") as handle:
            handle.seek(0, os.SEEK_END)
            if handle.tell():
                handle.seek(-1, os.SEEK_END)
                if handle.read(1) != b"\n":
                    payload = b"\n" + payload
                handle.seek(0, os.SEEK_END)
            handle.write(payload)
        os.chmod(path, 0o600)
    except OSError as exc:
        return f"не удалось записать {subject} ({path}): {exc}"
    return None


def _append_serialized(path: Path, line: str, subject: str) -> str | None:
    """Дописывает одну уже сериализованную строку в защищённый файл."""
    return _append_bytes(path, (line + "\n").encode("utf-8"), subject)


def _decode_lines(raw: bytes, subject: str) -> tuple[list[tuple[int, str]], list[str]]:
    """Строго декодирует строки: один битый знак не заражает соседние записи."""
    decoded: list[tuple[int, str]] = []
    warnings: list[str] = []
    for number, raw_line in enumerate(raw.splitlines(), 1):
        try:
            decoded.append((number, raw_line.decode("utf-8")))
        except UnicodeDecodeError:
            warnings.append(f"строка {number} {subject} содержит непригодные байты — пропущена")
    return decoded, warnings


@dataclass
class ConversationFacts:
    """Последняя целая редакция фактов текущего разговора."""

    values: dict[str, str] = field(default_factory=dict)
    revision: int = 0


def _fact_values(
    set_values: Mapping[str, str],
    forget_keys: Iterable[str],
) -> tuple[dict[str, str], list[str]] | str:
    if not isinstance(set_values, Mapping):
        return "set должен быть объектом"
    if isinstance(forget_keys, (str, bytes)) or not isinstance(forget_keys, Iterable):
        return "forget должен быть списком ключей"

    очищенные: dict[str, str] = {}
    try:
        for key, value in set_values.items():
            if not isinstance(key, str) or not key.strip():
                return "ключ set должен быть непустым текстом"
            if not isinstance(value, str) or not value.strip():
                return f"значение set для {key!r} должно быть непустым текстом"
            очищенные[key.strip()] = value.strip()

        забываемые: list[str] = []
        for key in forget_keys:
            if not isinstance(key, str) or not key.strip():
                return "ключ forget должен быть непустым текстом"
            забываемые.append(key.strip())
    except (TypeError, ValueError) as exc:
        return f"не удалось разобрать операцию фактов: {exc}"
    return очищенные, забываемые


class FactsStore:
    """Дописываемый журнал операций Sticky Facts рядом с линейной сессией."""

    def __init__(self, session: Path) -> None:
        self.session = session
        self.path = session.with_suffix(РАСШИРЕНИЕ_ФАКТОВ_РАЗГОВОРА)

    def load(self) -> tuple[ConversationFacts, list[str]]:
        facts = ConversationFacts()
        warnings: list[str] = []
        if not self.path.exists():
            return facts, warnings
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            return facts, [f"не удалось прочитать журнал фактов ({self.path}): {exc}"]
        decoded, decode_warnings = _decode_lines(raw, "журнала фактов")
        warnings.extend(decode_warnings)

        for number, line in decoded:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                warnings.append(f"строка {number} журнала фактов испорчена — пропущена")
                continue
            if not isinstance(record, dict):
                warnings.append(f"строка {number} журнала фактов испорчена — пропущена")
                continue

            revision = record.get("revision")
            source = record.get("source")
            turn_id = record.get("turn_id")
            operation = _fact_values(record.get("set"), record.get("forget"))
            пригодна = (
                isinstance(revision, int)
                and not isinstance(revision, bool)
                and revision > facts.revision
                and source in ("extractor", "user")
                and isinstance(turn_id, str)
                and bool(turn_id.strip())
                and _is_utc_timestamp(record.get("ts"))
                and not isinstance(operation, str)
            )
            if not пригодна:
                warnings.append(f"строка {number} журнала фактов непригодна — пропущена")
                continue

            set_values, forget_keys = operation
            for key, value in set_values.items():
                facts.values[key] = value
            for key in forget_keys:
                facts.values.pop(key, None)
            facts.revision = revision
        return facts, warnings

    def append(
        self,
        set_values: Mapping[str, str],
        forget_keys: Iterable[str],
        *,
        turn_id: str,
        source: str,
    ) -> str | None:
        operation = _fact_values(set_values, forget_keys)
        if isinstance(operation, str):
            return operation
        if source not in ("extractor", "user"):
            return "источник фактов должен быть extractor или user"
        if not isinstance(turn_id, str) or not turn_id.strip():
            return "turn_id операции фактов должен быть непустым текстом"

        facts, _ = self.load()
        set_record, forget_record = operation
        record = {
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "revision": facts.revision + 1,
            "turn_id": turn_id.strip(),
            "source": source,
            "set": set_record,
            "forget": forget_record,
        }
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return f"не удалось сериализовать операцию фактов: {exc}"
        return _append_serialized(self.path, line, "журнал фактов")


@dataclass(frozen=True)
class BranchTurn:
    """Один неизменяемый узел графа Branching — завершённая пара целиком."""

    id: str
    parent_id: str | None
    branch: str | None
    user: str
    assistant: str
    ts: str
    profile: str
    model: str
    system_fp: str


@dataclass
class RestoredBranches:
    """Восстановленный граф с независимыми головами доступных ветвей."""

    turns: dict[str, BranchTurn] = field(default_factory=dict)
    heads: dict[str, str] = field(default_factory=dict)
    checkpoint_id: str | None = None
    branches: tuple[str, ...] = ()
    root_head: str | None = None
    warnings: list[str] = field(default_factory=list)
    unavailable: set[str] = field(default_factory=set)

    def path(self, branch: str | None) -> list[tuple[str, str]]:
        """Возвращает пары от корня до головы, не включая соседние ветви."""
        if self.checkpoint_id is None:
            if branch is not None:
                return []
            head = self.root_head
        else:
            if branch not in self.heads or branch in self.unavailable:
                return []
            head = self.heads[branch]

        reversed_path: list[tuple[str, str]] = []
        seen: set[str] = set()
        while head is not None:
            if head in seen:
                return []
            seen.add(head)
            turn = self.turns.get(head)
            if turn is None:
                return []
            reversed_path.append((turn.user, turn.assistant))
            head = turn.parent_id
        reversed_path.reverse()
        return reversed_path


@dataclass(frozen=True)
class _BranchSplit:
    checkpoint_id: str
    left: str
    right: str
    line: int


def _branch_name(value: Any) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_branch_turn(record: dict[str, Any]) -> BranchTurn | None:
    turn_id = _branch_name(record.get("id"))
    parent = record.get("parent_id")
    branch_value = record.get("branch")
    branch = _branch_name(branch_value)
    if parent is not None:
        parent = _branch_name(parent)
        if parent is None:
            return None
    if branch_value is not None and branch is None:
        return None
    if (
        turn_id is None
        or not isinstance(record.get("user"), str)
        or not isinstance(record.get("assistant"), str)
        or not isinstance(record.get("profile"), str)
        or not isinstance(record.get("model"), str)
        or not isinstance(record.get("system_fp"), str)
        or not _is_utc_timestamp(record.get("ts"))
    ):
        return None
    return BranchTurn(
        id=turn_id,
        parent_id=parent,
        branch=branch,
        user=record["user"],
        assistant=record["assistant"],
        ts=record["ts"],
        profile=record["profile"],
        model=record["model"],
        system_fp=record["system_fp"],
    )


def _parse_split(record: dict[str, Any], line: int) -> _BranchSplit | None:
    checkpoint = _branch_name(record.get("checkpoint_id"))
    left = _branch_name(record.get("left"))
    right = _branch_name(record.get("right"))
    if (
        checkpoint is None
        or left is None
        or right is None
        or left == right
        or not _is_utc_timestamp(record.get("ts"))
    ):
        return None
    return _BranchSplit(checkpoint, left, right, line)


def _cycle_in_branch(turns: dict[str, BranchTurn]) -> bool:
    for start in turns:
        seen: set[str] = set()
        current: str | None = start
        while current in turns:
            if current in seen:
                return True
            seen.add(current)
            current = turns[current].parent_id
    return False


class BranchStore:
    """Один дописываемый файл графа Branching."""

    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> RestoredBranches:
        restored = RestoredBranches()
        if not self.path.exists():
            return restored
        try:
            raw = self.path.read_bytes()
        except OSError as exc:
            restored.warnings.append(f"не удалось прочитать граф ветвей ({self.path}): {exc}")
            return restored
        decoded, decode_warnings = _decode_lines(raw, "графа ветвей")
        restored.warnings.extend(decode_warnings)

        entries: list[tuple[int, BranchTurn]] = []
        split: _BranchSplit | None = None
        invalid_branches: set[str] = set()
        for number, line in decoded:
            if not line.strip():
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                restored.warnings.append(f"строка {number} графа ветвей испорчена — пропущена")
                continue
            if not isinstance(record, dict):
                restored.warnings.append(f"строка {number} графа ветвей испорчена — пропущена")
                continue

            if record.get("type") == "turn":
                turn = _parse_branch_turn(record)
                if turn is None:
                    branch = _branch_name(record.get("branch"))
                    if branch is not None:
                        invalid_branches.add(branch)
                    restored.warnings.append(f"строка {number}: узел ветви непригоден — пропущен")
                    continue
                entries.append((number, turn))
            elif record.get("type") == "split":
                candidate = _parse_split(record, number)
                if candidate is None:
                    restored.warnings.append(f"строка {number}: разделение непригодно — пропущено")
                elif split is None:
                    split = candidate
                else:
                    restored.warnings.append(f"строка {number}: повторное разделение — пропущено")
            else:
                restored.warnings.append(f"строка {number}: неизвестное событие графа — пропущено")

        unique: dict[str, tuple[int, BranchTurn]] = {}
        for number, turn in entries:
            if turn.id in unique:
                if turn.branch is not None:
                    invalid_branches.add(turn.branch)
                restored.warnings.append(f"строка {number}: повтор id {turn.id!r} — узел пропущен")
                continue
            unique[turn.id] = (number, turn)

        root_head: str | None = None
        safe_turns: dict[str, BranchTurn] = {}
        split_line = split.line if split is not None else None
        for number, turn in entries:
            if unique.get(turn.id) != (number, turn) or turn.branch is not None:
                continue
            if split_line is not None and number > split_line:
                restored.warnings.append(f"строка {number}: корень продолжен после разделения — узел пропущен")
                continue
            if turn.parent_id != root_head:
                restored.warnings.append(f"строка {number}: нарушена цепочка корня — узел пропущен")
                continue
            safe_turns[turn.id] = turn
            root_head = turn.id
        restored.root_head = root_head

        if split is not None:
            if root_head is None or split.checkpoint_id != root_head:
                restored.warnings.append(
                    f"строка {split.line}: контрольная точка разделения не является головой корня"
                )
                split = None
            else:
                restored.checkpoint_id = split.checkpoint_id
                restored.branches = (split.left, split.right)

        if split is None:
            for number, turn in entries:
                if turn.branch is not None:
                    restored.warnings.append(f"строка {number}: ветвь задана до разделения — узел пропущен")
            restored.turns = safe_turns
            return restored

        declared = set(restored.branches)
        for number, turn in entries:
            if turn.branch is not None and turn.branch not in declared:
                invalid_branches.add(turn.branch)
                restored.warnings.append(
                    f"строка {number}: неизвестная ветвь {turn.branch!r} — узел пропущен"
                )
        restored.unavailable.update(invalid_branches - declared)

        for branch in restored.branches:
            branch_entries = [
                (number, turn)
                for number, turn in entries
                if turn.branch == branch and unique.get(turn.id) == (number, turn)
            ]
            branch_turns = {turn.id: turn for _, turn in branch_entries}
            broken = branch in invalid_branches

            for number, turn in branch_entries:
                if number < split.line:
                    restored.warnings.append(
                        f"строка {number}: ветвь {branch!r} записана до разделения"
                    )
                    broken = True
                    continue
                parent = unique.get(turn.parent_id) if turn.parent_id is not None else None
                if parent is None:
                    restored.warnings.append(
                        f"строка {number}: неизвестный родитель узла ветви {branch!r}"
                    )
                    broken = True
                elif turn.parent_id != restored.checkpoint_id and parent[1].branch != branch:
                    restored.warnings.append(
                        f"строка {number}: узел ветви {branch!r} ссылается на чужую ветвь"
                    )
                    broken = True

            if _cycle_in_branch(branch_turns):
                restored.warnings.append(f"в ветви {branch!r} обнаружен цикл")
                broken = True

            children: dict[str, int] = {}
            for turn in branch_turns.values():
                if turn.parent_id in branch_turns:
                    children[turn.parent_id] = children.get(turn.parent_id, 0) + 1
            if any(count > 1 for count in children.values()):
                restored.warnings.append(f"ветвь {branch!r} содержит развилку после разделения")
                broken = True

            if branch_turns:
                tips = [turn_id for turn_id in branch_turns if turn_id not in children]
                if len(tips) != 1:
                    restored.warnings.append(f"у ветви {branch!r} нет единственной головы")
                    broken = True
                    head = restored.checkpoint_id
                else:
                    head = tips[0]
                    reached: set[str] = set()
                    current: str | None = head
                    while current != restored.checkpoint_id and current in branch_turns:
                        if current in reached:
                            break
                        reached.add(current)
                        current = branch_turns[current].parent_id
                    if current != restored.checkpoint_id or reached != set(branch_turns):
                        restored.warnings.append(
                            f"ветвь {branch!r} не образует путь от контрольной точки"
                        )
                        broken = True
            else:
                head = restored.checkpoint_id

            if broken:
                restored.unavailable.add(branch)
                continue
            safe_turns.update(branch_turns)
            restored.heads[branch] = head

        restored.turns = safe_turns
        return restored

    def append_turn(
        self,
        branch: str | None,
        parent_id: str | None,
        user: str,
        assistant: str,
        *,
        profile: str,
        model: str,
        system_fp: str,
    ) -> tuple[BranchTurn | None, str | None]:
        if branch is not None:
            branch = _branch_name(branch)
            if branch is None:
                return None, "имя ветви должно быть непустым текстом"
        if parent_id is not None and _branch_name(parent_id) is None:
            return None, "parent_id должен быть непустым текстом или None"
        if not all(isinstance(value, str) for value in (user, assistant, profile, model, system_fp)):
            return None, "тексты пары и пометки узла должны быть строками"

        restored = self.load()
        if restored.checkpoint_id is None:
            if branch is not None:
                return None, "до разделения пару можно записать только в корень"
            expected_parent = restored.root_head
        else:
            if branch not in restored.branches:
                return None, f"неизвестная ветвь {branch!r}"
            if branch in restored.unavailable or branch not in restored.heads:
                return None, f"ветвь {branch!r} недоступна из-за повреждения графа"
            expected_parent = restored.heads[branch]
        if parent_id != expected_parent:
            return None, f"родитель не является головой: ожидался {expected_parent!r}"

        turn_id = uuid4().hex
        while turn_id in restored.turns:
            turn_id = uuid4().hex
        turn = BranchTurn(
            id=turn_id,
            parent_id=parent_id,
            branch=branch,
            user=user,
            assistant=assistant,
            ts=datetime.now(UTC).isoformat(timespec="seconds"),
            profile=profile,
            model=model,
            system_fp=system_fp,
        )
        record = {
            "type": "turn",
            "ts": turn.ts,
            "id": turn.id,
            "parent_id": turn.parent_id,
            "branch": turn.branch,
            "user": turn.user,
            "assistant": turn.assistant,
            "profile": turn.profile,
            "model": turn.model,
            "system_fp": turn.system_fp,
        }
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return None, f"не удалось сериализовать узел ветви: {exc}"
        error = _append_serialized(self.path, line, "граф ветвей")
        return (None, error) if error else (turn, None)

    def split(self, checkpoint_id: str | None, left: str, right: str) -> str | None:
        left_name = _branch_name(left)
        right_name = _branch_name(right)
        if left_name is None or right_name is None:
            return "имена двух ветвей должны быть непустым текстом"
        if left_name == right_name:
            return "имена двух ветвей должны различаться"

        restored = self.load()
        if restored.checkpoint_id is not None:
            return "разговор уже разделён"
        if restored.root_head is None:
            return "разделение возможно только после завершённой пары"
        if checkpoint_id != restored.root_head:
            return f"контрольная точка не является головой: ожидалась {restored.root_head!r}"

        record = {
            "type": "split",
            "ts": datetime.now(UTC).isoformat(timespec="seconds"),
            "checkpoint_id": checkpoint_id,
            "left": left_name,
            "right": right_name,
        }
        try:
            line = json.dumps(record, ensure_ascii=False)
        except (TypeError, ValueError) as exc:
            return f"не удалось сериализовать разделение: {exc}"
        return _append_serialized(self.path, line, "разделение ветвей")



# Расширение файла выжимки. Оно нарочно НЕ `.jsonl`: выбор последней сессии перебирает
# каталог профиля по расширению, а выжимка пишется позже разговора и потому всегда свежее
# его — совпади расширения, «продолжить последний разговор» поднимало бы пересказ вместо
# самого разговора, и всегда, а не изредка.
РАСШИРЕНИЕ_ВЫЖИМКИ = ".summary.json"


def summary_path(session: Path) -> Path:
    """Файл выжимки этой сессии: `<имя сессии>.summary.json` рядом с самим разговором.

    Рядом, а не в общем складе выжимок: имя сессии случайно и уникально, поэтому связь
    «эта выжимка от этого разговора» держится самим именем файла, без оглавления и без
    разбора содержимого. Удалили разговор — рядом лежит осиротевший файл, который видно
    глазами; унеси мы выжимку в отдельный каталог, сирота была бы не видна никому.

    Имя берётся без расширения сессии: `<uuid>.summary.json`, а не `<uuid>.jsonl.summary.json`.
    Два расширения подряд читаются как «файл, который переименовали и забыли», а пара
    «`<uuid>.jsonl` и `<uuid>.summary.json`» видна в перечислении каталога с одного взгляда."""
    return session.with_suffix(РАСШИРЕНИЕ_ВЫЖИМКИ)


def save_summary(session: Path, выжимка: Выжимка) -> str | None:
    """Кладёт выжимку рядом с разговором. Возвращает текст ошибки — и никогда не бросает.

    Текстом, а не исключением, по общему правилу хранилища: обмен, дошедший до ответа, уже
    оплачен у поставщика, и уронить его отказом диска нельзя. Выжимка тут ничем не отличается
    от журнала и файла разговора — вспомогательный механизм, который не имеет права утащить за
    собой оплаченную работу.

    Запись идёт подменой имени — тем же приёмом, что у глобальных фактов, и по той же
    причине: читатель обязан видеть либо целую выжимку, либо ничего. Прямая запись оставила бы
    окно, в котором файл наполовину старый, наполовину новый; разобрать такой нельзя, а
    случается это ровно тогда, когда процесс убили посреди работы, то есть когда пересказ
    длинного разговора и нужен больше всего.

    С отступами: файл выжимки читается ГЛАЗАМИ — это и есть его главное назначение, им
    отлаживают пересказ. Экономить на переводах строк там, где счёт идёт на килобайты раз в
    сжатие, значит менять читаемость на ничто."""
    путь = summary_path(session)
    # Черновик лежит рядом и начинается с точки, а расширение у него `.tmp`: в перечисление
    # сессий он не попадает ни при каком исходе. Номер процесса в имени — чтобы две копии
    # инструмента не писали в один черновик.
    черновик = путь.parent / f".{путь.name}.{os.getpid()}.tmp"
    try:
        # Сериализация — до открытия файла: пункты приходят от модели, и несериализуемое
        # значение обязано остаться текстом ошибки, а не исключением посреди записи.
        содержимое = json.dumps(выжимка.to_dict(), ensure_ascii=False, indent=2)
        make_private_dir(путь.parent)
        черновик.write_text(содержимое + "\n", encoding="utf-8")
        # Права выставляем черновику, а не итогу: подмена имени их сохраняет, и окна, в
        # котором пересказ разговора лежит с правами по умолчанию, не возникает вовсе.
        os.chmod(черновик, 0o600)
        os.replace(черновик, путь)
    except (OSError, TypeError, ValueError) as exc:
        with contextlib.suppress(OSError):
            черновик.unlink()
        return f"не удалось записать выжимку ({путь}): {exc}"
    return None


def load_summary(session: Path) -> tuple[Выжимка | None, list[str]]:
    """Читает выжимку сессии. Возвращает `(выжимка или None, предупреждения)` и не бросает.

    Отсутствие файла — молчание: у сессии, начатой до появления сжатия, и у любой новой
    выжимки нет, и это обычное состояние, а не беда. Предупреждай о нём — человек получал бы
    тревогу за штатную работу и перестал бы читать предупреждения вовсе.

    Испорченный файл — наоборот, назван вслух: разговор поднимается так, как если бы выжимки
    не было, и молчать об этом нельзя — иначе человек считает, что модель помнит
    пересказанное, а она его не видит.

    Границу с числом пар файла сессии здесь не сверяем: пары считает чтение разговора, и
    сверка живёт там, где оба числа уже на руках. Читать файл сессии второй раз ради одной
    проверки — платить за неё лишним проходом по всему разговору."""
    путь = summary_path(session)
    try:
        содержимое = путь.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, []
    except (OSError, UnicodeDecodeError) as exc:
        return None, [f"не удалось прочитать выжимку ({путь}): {exc}"]
    try:
        данные = json.loads(содержимое)
    # `RecursionError` перехватывается наравне с `ValueError` по воспроизведённой беде: файл из
    # двухсот тысяч вложенных скобок кладёт разбор переполнением стека, а не ошибкой разбора, —
    # и запуск инструмента умирает целиком. Требование обещает, что испорченная выжимка не
    # роняет запуск, без оговорок, а файл этот человеку разрешено править руками.
    except (ValueError, RecursionError):
        return None, [f"файл выжимки испорчен ({путь}) — разговор поднят без пересказа"]
    # Импорт внутри тела, а не наверху модуля, — не небрежность, а разрыв круга: `compact`
    # берёт `Profile` у `profiles`, `profiles` берёт умолчание окна у `agent`, а `agent`
    # берёт хранилище отсюда. Наверху этот круг замкнулся бы на самом первом импорте пакета,
    # причём с ошибкой «частично инициализированный модуль», по которой причину не видно.
    # Круг проверен прогоном, а не предположен: поднятый наверх импорт роняет пакет на входе
    # через `compact`, `profiles` и `agent`, и целым остаётся только вход через сам `memory`.
    try:
        from .compact import Выжимка as _Выжимка
    except ImportError as exc:  # круг разорван, но обещание «не роняем запуск» сильнее догадок
        return None, [f"не удалось разобрать выжимку ({путь}): {exc}"]

    выжимка = _Выжимка.from_dict(данные)
    if выжимка is None:
        # Разбор молчит о том, чего именно не хватило, и это осознанно: разбор — чистое
        # преобразование без слов для человека. Слова живут здесь.
        return None, [f"в файле выжимки нет обязательных полей ({путь}) — разговор поднят без пересказа"]
    return выжимка, []


def fingerprint(system: str | None) -> str:
    """Короткий отпечаток системной инструкции: по нему видно, что инструкцию правили между
    запусками и прежние ответы получены под другой.

    Шести знаков хватает: задача отпечатка — заметить правку, а не устоять против подделки.
    Отсутствие инструкции даёт отпечаток пустого текста, а не пустую строку: пустой строкой
    обозначаются записи старого формата, где отпечатка нет вовсе, и путать эти два случая
    нельзя."""
    return hashlib.sha256((system or "").encode("utf-8")).hexdigest()[:6]


# Потолок числа фактов. Он не про место на диске: факты уезжают в системную инструкцию каждого
# запроса, и без потолка она разбухала бы незаметно для человека и за его деньги.
# Отказ «строка уже лежит в этом месте» — один на все три хранилища. Он не беда, а обычный
# ход дела, и зовущие отличают его от настоящих отказов (нет прав, полон диск, память полна):
# на нём человеку нечего предложить сделать руками — строка и так записана. Сравнение по
# строковому литералу в чужом модуле разъехалось бы с самим отказом молча, поэтому слово
# живёт здесь, а `workspace` и `project_card` берут его отсюда.
УЖЕ_ЗАПИСАНО = "уже записано"

# Начала отказов, которые зависят от САМОЙ строки, а не от внешней беды. Предлагать после них
# готовую команду нельзя: нажатая, она даст тот же отказ, и две строки в ленте спорили бы друг
# с другом. Отличать их по подстроке в чужом модуле было бы гаданием, поэтому список живёт
# рядом с самими отказами — здесь, в `workspace` и в `project_card`.
ОТКАЗЫ_ПРО_СТРОКУ: tuple[str, ...] = (УЖЕ_ЗАПИСАНО, "факт пустой", "факт длиннее")

FACTS_MAX = 40

# Потолок длины одного факта — тот же, что у разбора ответа архивариуса. Потолок в штуках от
# факта в двенадцать тысяч знаков не спасает, а по замыслу факт и есть короткая
# самодостаточная строка, а не абзац.
FACT_CHARS_MAX = 200

_НАЧАЛО_ЗАГОЛОВКА = "---"


@dataclass
class _Факт:
    """Один файл памяти, разобранный: путь, время добавления и сам текст."""

    путь: Path
    добавлен: str
    текст: str
    # Пометка `инвариант: да` в заголовке: запись — правило, которое модель не вправе
    # нарушать (способность `invariants`). Ставит её только человек, см. `add_fact`.
    инвариант: bool = False


# Ключ заголовка записи-инварианта и его единственное значение «да». Любое другое значение —
# не инвариант: файл правят руками, и опечатка не должна тихо превращать мягкую запись в
# жёсткое правило. Отсутствие ключа — тоже не инвариант, прежние файлы остаются прежними.
КЛЮЧ_ИНВАРИАНТА = "инвариант"
ИНВАРИАНТ_ДА = "да"

# Отказ замка. Строкой рядом с самим замком — по той же причине, что `УЖЕ_ЗАПИСАНО`.
ИНВАРИАНТ_ТОЛЬКО_ЧЕЛОВЕК = "инвариант ставит только человек"


# Потолок слага в БАЙТАХ. Предел имени файла в ext4 — 255 байт, а знак кириллицы занимает два:
# слаг, обрезанный по знакам, дал бы из законного факта в 200 знаков имя в 410 байт, и на
# сервере проекта факт не записался бы вовсе (ENAMETOOLONG). На макбуке беда не видна — APFS
# считает знаки, — потому потолок и задан в байтах. Уникальность имени держит отпечаток, а не
# длина слага, так что резать можно смело.
СЛАГ_БАЙТ_MAX = 80


def _обрезать_по_байтам(текст: str, предел: int) -> str:
    """Обрезает текст до предела в байтах — по границе знака, а не посреди буквы."""
    закодированное = текст.encode("utf-8")
    if len(закодированное) <= предел:
        return текст
    # `errors="ignore"` отбрасывает недописанный хвостовой знак целиком: разрубленная пополам
    # буква оставила бы в имени файла битый байт.
    return закодированное[:предел].decode("utf-8", errors="ignore")


def слаг(текст: str) -> str:
    """Начало имени файла — первые слова текста. Имя человек читает глазами, поэтому кириллица
    остаётся кириллицей: `зовут-александр-a3f19c.md` говорит о себе сам, `a3f19c.md` — нет."""
    слова: list[str] = []
    for слово in текст.lower().split():
        оставленное = "".join(знак for знак in слово if знак.isalnum() or знак == "_")
        if not оставленное:
            continue
        слова.append(оставленное)
    # Хвостовой «-» после обрезки — след разрубленного слова, в имени он лишний.
    слаг = _обрезать_по_байтам("-".join(слова), СЛАГ_БАЙТ_MAX).rstrip("-")
    # Факт из одних знаков препинания слагом не описать — тогда имя несёт один отпечаток.
    return слаг or "факт"


def _нормализовать(текст: str) -> str:
    """Факт — одна строка. Перевод строки внутри разорвал бы тело файла надвое, и вторая
    половина при следующем чтении оказалась бы отдельным непонятным фактом."""
    return " ".join(текст.split())


def _разобрать_факт(содержимое: str) -> tuple[str, str, bool] | None:
    """Разбирает файл факта в `(добавлен, текст, инвариант)`; `None` — файл испорчен.

    Заголовок необязателен: человеку обещано, что файлы можно править руками, и файл без
    заголовка — не повод терять факт. Пустое тело — повод: сказать нечего."""
    добавлен = ""
    инвариант = False
    тело = содержимое
    if содержимое.startswith(_НАЧАЛО_ЗАГОЛОВКА):
        _, _, остаток = содержимое.partition("\n")
        заголовок, разделитель, тело = остаток.partition(f"\n{_НАЧАЛО_ЗАГОЛОВКА}")
        if not разделитель:
            return None
        for строка in заголовок.splitlines():
            ключ, _, значение = строка.partition(":")
            if ключ.strip() == "добавлен":
                добавлен = значение.strip()
            elif ключ.strip() == КЛЮЧ_ИНВАРИАНТА:
                инвариант = значение.strip().casefold() == ИНВАРИАНТ_ДА
        _, _, тело = тело.partition("\n")
    текст = _нормализовать(тело)
    return (добавлен, текст, инвариант) if текст else None


def _собрать_факты() -> tuple[list[_Факт], list[str]]:
    """Читает каталог памяти. Порядок — по времени добавления, при совпадении по имени файла.

    Порядок обязан быть одинаков в двух запущенных копиях инструмента: иначе `/forget 2` в
    одном окне убирает не то, что видно во втором."""
    каталог = facts_dir()
    предупреждения: list[str] = []
    if not каталог.exists():
        # Каталога ещё нет — фактов просто не заводили. Это не беда и говорить не о чем.
        return [], предупреждения
    try:
        # Обход через `iterdir`, а не `glob`: `glob` молча глотает отказ в правах и отдаёт
        # пустой список. Тогда недоступная память была бы неотличима от пустой, и человек
        # считал бы, что инструмент его просто забыл.
        файлы = sorted(п for п in каталог.iterdir() if п.suffix == ".md")
    except OSError as exc:
        предупреждения.append(f"глобальная память недоступна ({каталог}): {exc}")
        return [], предупреждения

    собранные: list[_Факт] = []
    for файл in файлы:
        try:
            содержимое = файл.read_text(encoding="utf-8")
        except FileNotFoundError:
            # Файл исчез между перечислением каталога и чтением: его только что убрала вторая
            # копия инструмента. Это норма устройства «файл на факт», и говорить тут не о чем.
            continue
        except (OSError, UnicodeDecodeError) as exc:
            предупреждения.append(f"факт {файл.name} не прочитан: {exc}")
            continue
        разобранное = _разобрать_факт(содержимое)
        if разобранное is None:
            предупреждения.append(f"факт {файл.name} испорчен — пропущен")
            continue
        добавлен, текст, инвариант = разобранное
        собранные.append(_Факт(путь=файл, добавлен=добавлен, текст=текст, инвариант=инвариант))

    собранные.sort(key=lambda факт: (факт.добавлен, факт.путь.name))
    return собранные, предупреждения


def load_facts() -> tuple[list[str], list[str]]:
    """Читает глобальные факты. Возвращает `(факты, предупреждения)` и никогда не бросает:
    испорченный файл памяти не имеет права мешать работе инструмента."""
    собранные, предупреждения = _собрать_факты()
    return [факт.текст for факт in собранные], предупреждения


def load_records() -> tuple[list[tuple[str, bool]], list[str]]:
    """Все записи о человеке парами `(текст, инвариант)` — в том же порядке, что `/memory`.

    Отдельно от `load_facts`, а не вместо него: `load_facts` отдаёт ВСЕ записи, и номера
    `/forget` держатся на нём. Кому нужно отличить инвариант от мягкой записи — берёт эту."""
    собранные, предупреждения = _собрать_факты()
    return [(факт.текст, факт.инвариант) for факт in собранные], предупреждения


def load_invariant_facts() -> tuple[list[str], list[str]]:
    """Тексты записей-инвариантов в порядке `/memory` — строки `Г` блока инвариантов."""
    записи, предупреждения = load_records()
    return [текст for текст, инвариант in записи if инвариант], предупреждения


def add_fact(text: str, *, source: str = "человек", инвариант: bool = False) -> tuple[bool, str]:
    """Кладёт факт в глобальную память. Возвращает `(добавлен ли, сообщение человеку)`.

    Добавление — создание НОВОГО файла. Общего изменяемого состояния нет, поэтому нет и гонки:
    две запущенные копии инструмента пишут разные файлы и не встречаются. Замок здесь не забыт,
    он не нужен — это следствие устройства.

    `source` отвечает на вопрос «откуда взялась эта строка»: `человек` сказал её сам или её
    вывел `архивариус`. Через месяц при разборе памяти другого способа это узнать нет.

    `инвариант` ставит в заголовок пометку `инвариант: да`. Замок стоит ПЕРВЫМ делом, до
    любых проверок текста: правило, по которому программа судит ответы модели, модель себе
    не пишет — ни архивариусом, ни естественной записью. Отказ не пишет ничего."""
    if инвариант and source != "человек":
        return False, ИНВАРИАНТ_ТОЛЬКО_ЧЕЛОВЕК
    очищенный = _нормализовать(text)
    if not очищенный:
        return False, "факт пустой — нужен текст"
    if len(очищенный) > FACT_CHARS_MAX:
        return False, f"факт длиннее {FACT_CHARS_MAX} знаков (в нём {len(очищенный)}) — нужна короткая строка, а не пересказ"

    собранные, _ = _собрать_факты()
    # Повтор ищем по тексту, а не по имени файла: имя человек мог сменить, текст внутри —
    # поправить, и отбор повторов не должен от этого разваливаться.
    if any(факт.текст.casefold() == очищенный.casefold() for факт in собранные):
        return False, УЖЕ_ЗАПИСАНО
    if len(собранные) >= FACTS_MAX:
        # Молча вытеснить самое старое нельзя: человек не узнает, что потерял, и будет считать
        # память полной, пока она втихую забывает. Пусть выбирает сам — и знает, чем чистить.
        return False, f"глобальная память полна ({FACTS_MAX} фактов) — уберите лишнее командой /forget"

    каталог = facts_dir()
    путь = каталог / f"{слаг(очищенный)}-{fingerprint(очищенный)}.md"
    содержимое = (
        f"{_НАЧАЛО_ЗАГОЛОВКА}\n"
        f"добавлен: {datetime.now(UTC):%Y-%m-%dT%H:%M:%SZ}\n"
        f"источник: {_нормализовать(source) or 'человек'}\n"
        + (f"{КЛЮЧ_ИНВАРИАНТА}: {ИНВАРИАНТ_ДА}\n" if инвариант else "")
        + f"{_НАЧАЛО_ЗАГОЛОВКА}\n\n"
        f"{очищенный}\n"
    )
    ошибка = write_private(путь, содержимое, subject="факт")
    if ошибка:
        return False, ошибка
    # Наружу отдаём приведённый текст: объявлять человеку надо ровно то, что легло в файл.
    return True, очищенный


def remove_fact(index: int) -> tuple[bool, str]:
    """Убирает факт по номеру, каким он показан человеку в `/memory`, — то есть с единицы."""
    собранные, _ = _собрать_факты()
    if not собранные:
        return False, "глобальная память пуста — удалять нечего"
    if not 1 <= index <= len(собранные):
        # Номер приходит из набранной вручную команды. Промах — не поломка инструмента, и
        # отвечать на него исключением значит наказывать за опечатку.
        return False, f"нет факта с номером {index} — сейчас их {len(собранные)}"
    факт = собранные[index - 1]
    try:
        факт.путь.unlink()
    except FileNotFoundError:
        # Вторая копия инструмента убрала этот файл первой. Цель достигнута — факта нет.
        return True, факт.текст
    except OSError as exc:
        return False, f"не удалось убрать факт ({факт.путь}): {exc}"
    return True, факт.текст


# Строка употребления над списком записей. Записи уходят ВСЕМ персонам целиком — охвата у
# записи нет и заводить его признано убыточным, — и без этой строки преподаватель испанского,
# увидев запись про Kotlin, тянет программирование в разговор о языке. Лечится словами, а не
# полем в сорока файлах: десяток токенов против разметки, промах которой к тому же тихий.
УПОТРЕБЛЕНИЕ_ЗАПИСЕЙ = (
    "Справка о собеседнике: верна всегда и везде. "
    "Пользуйся ею, только если она относится к вопросу, и не упоминай без надобности. "
    "При противоречии с инструкцией профиля, с карточкой проекта или с состоянием текущей "
    "задачи верны они: область у них уже. "
    "Блок «Инварианты» старше справки."
)

# Чем помечается запись, которую перекрыл действующий профиль. Запись остаётся в блоке
# намеренно: выбрось её — и в профиле французского модель не узнает, что человек
# русскоязычный, и объяснит грамматику на языке, которого он ещё не знает. Старшинство надо
# ОБЪЯВИТЬ, а не заменить умолчанием.
#
# Слова выбраны про СТАРШИНСТВО, а не про отмену. «Не действует» модель прочтёт как «этого про
# него знать не надо» — и перестанет учитывать запись вовсе; тогда цена за оставленную в блоке
# запись уплачена, а польза не получена. Здесь тот же ключ, что у строки употребления выше.
ПОМЕТКА_ПЕРЕКРЫТИЯ = " — здесь старше профиль: помни эту запись, но следуй профилю"


def ключ_записи(строка: str) -> str:
    """Чем запись о человеке опознаётся при сравнении: нормализованный текст без регистра.

    Правило общее для всего инструмента, поэтому и живёт открыто: по нему `facts_block` ищет
    перекрытую запись, а мастер профиля — ту, которую назвала модель. Сверяй они по-разному —
    строка с лишним пробелом внутри была бы отброшена мастером как «такой записи нет», хотя
    пометка на ней сработала бы.

    Внутренние пробелы схлопываются потому, что от нормализованного текста считаются слаг и
    отпечаток в имени файла записи. Регистр отбрасывается по другой причине: `add_fact` отбивает
    повтор по `casefold`, поэтому двух записей, различных только регистром, в памяти не бывает, —
    и сравнивать их без регистра безопасно.
    """
    return _нормализовать(строка).casefold()


def facts_block(facts: list[str], *, перекрытые: Iterable[str] = ()) -> str:
    """Собирает то, что подмешивается в системную инструкцию.

    `перекрытые` — записи, с которыми расходится действующий профиль (его поле `overrides`).
    Совпавшая запись остаётся в блоке и получает `ПОМЕТКА_ПЕРЕКРЫТИЯ`; не совпавшая ни с чем
    строка на текст не влияет вовсе. Молчаливый промах здесь — правильное поведение, а не
    упущение: запись могли удалить или переписать уже после того, как профиль её назвал, и
    отказываться работать из-за этого профиль не должен.

    Сравнение идёт по НОРМАЛИЗОВАННОМУ тексту — схлопнутые внутренние пробелы, срезанные
    краевые, без учёта регистра, — потому что нормализованный текст и есть опознание записи:
    `add_fact` считает от него и слаг, и отпечаток в имени файла. Строка в профиле приходит из
    ответа модели, и лишний пробел внутри неё иначе означал бы промах пометки — молчаливый, в
    точности там, ради чего пометка и заведена.
    """
    строки = [факт.strip() for факт in facts if факт and факт.strip()]
    if not строки:
        # Пустая строка, а не заголовок с пустым списком: пустой заголовок сообщил бы модели,
        # что о человеке известно, что о нём ничего не известно, и она стала бы это обыгрывать.
        return ""
    # Голую строку в доводе разбирать по знакам нельзя: каждый знак стал бы ключом, и
    # пометка налипла бы куда попало. В этом же файле от такой ошибки уже защищаются явно.
    источник = () if isinstance(перекрытые, (str, bytes)) else перекрытые
    ключи = {
        ключ_записи(строка) for строка in источник if isinstance(строка, str) and строка.strip()
    }
    return "\n".join(
        [
            УПОТРЕБЛЕНИЕ_ЗАПИСЕЙ,
            "Что известно о пользователе:",
            *(
                f"- {факт}"
                + (ПОМЕТКА_ПЕРЕКРЫТИЯ if ключ_записи(факт) in ключи else "")
                for факт in строки
            ),
        ]
    )
