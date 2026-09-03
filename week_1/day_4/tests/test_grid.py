"""Проверки ключа клетки, записи прогона и возобновления (`temperature.grid`).

Ни одна проверка не трогает настоящий `data/grid.jsonl`: запись и чтение идут во временный
каталог `tmp_path`, который pytest заводит на каждую проверку заново.
"""

import json
import re

from temperature.grid import (
    CellResult,
    append_record,
    build_record,
    collected_repeats,
    run_key,
)
from temperature.questions import by_id

# Имена ключей, которых в записи прогона быть не должно ни на одном уровне вложенности.
ЗАПРЕЩЁННЫЕ_КЛЮЧИ = {"api_key", "apikey", "api-key", "token", "secret", "authorization"}

# Ключ доступа DeepSeek выглядит как `sk-` и хвост из букв и цифр.
ПОХОЖЕ_НА_КЛЮЧ = re.compile(r"sk-[A-Za-z0-9]{8,}")


def обойти(значение, путь=""):
    """Обходит вложенные словари и списки, отдавая пары (путь, узел) для каждого узла."""
    yield путь, значение
    if isinstance(значение, dict):
        for ключ, вложенное in значение.items():
            yield from обойти(вложенное, f"{путь}.{ключ}" if путь else str(ключ))
    elif isinstance(значение, (list, tuple)):
        for номер, вложенное in enumerate(значение):
            yield from обойти(вложенное, f"{путь}[{номер}]")


def строка_записи(question_id, temperature, repetition, status):
    """Минимальная запись прогона — ровно те поля, по которым считается возобновление."""
    return json.dumps(
        {
            "run_key": f"{question_id}_t{temperature:.1f}_r{repetition}",
            "question_id": question_id,
            "temperature": temperature,
            "repetition": repetition,
            "status": status,
        },
        ensure_ascii=False,
    )


def файл_с_записями(путь, строки):
    """Кладёт готовые строки в файл журнала и возвращает путь к нему."""
    путь.write_text("\n".join(строки) + "\n", encoding="utf-8")
    return путь


# --- ключ клетки ---------------------------------------------------------------------------


def test_run_key_обычный_вопрос():
    assert run_key("poet", 0.7, 2) == "poet_t0.7_r2"


def test_run_key_нулевая_температура_печатается_с_одним_знаком():
    assert run_key("apples", 0.0, 1) == "apples_t0.0_r1"


def test_run_key_идентификатор_с_подчёркиванием():
    assert run_key("apples_hard", 1.2, 3) == "apples_hard_t1.2_r3"


# --- запись --------------------------------------------------------------------------------


def test_append_record_создаёт_каталог_и_пишет_строку(tmp_path):
    путь = tmp_path / "нет" / "такого" / "grid.jsonl"

    append_record({"question_id": "apples", "repetition": 1}, путь)

    assert путь.exists()
    строки = путь.read_text(encoding="utf-8").splitlines()
    assert len(строки) == 1
    assert json.loads(строки[0]) == {"question_id": "apples", "repetition": 1}


def test_append_record_дописывает_не_трогая_прежнюю_строку(tmp_path):
    путь = tmp_path / "grid.jsonl"
    первая = {"question_id": "apples", "repetition": 1}
    вторая = {"question_id": "apples", "repetition": 2}

    append_record(первая, путь)
    append_record(вторая, путь)

    строки = путь.read_text(encoding="utf-8").splitlines()
    assert len(строки) == 2
    assert json.loads(строки[0]) == первая
    assert json.loads(строки[1]) == вторая


def test_append_record_сохраняет_русский_текст_как_есть(tmp_path):
    путь = tmp_path / "grid.jsonl"

    append_record({"answer": "ответ: 3"}, путь)

    содержимое = путь.read_text(encoding="utf-8")
    assert "ответ: 3" in содержимое
    assert "\\u" not in содержимое


# --- возобновление -------------------------------------------------------------------------
#
# `collected_repeats` отвечает на вопрос «с какого номера продолжать», и ответ — наибольший
# номер прогона среди ВСЕХ записей вопроса, независимо от статуса. Номер, по которому в журнал
# уже что-то писалось, занят навсегда: переиспользуй его — и в `data/grid.jsonl` появятся две
# записи с одним `run_key`. Ключ перестанет быть именем клетки, а разбор молча потеряет одну
# из двух записей.
#
# Отсюда осознанный размен: прогон, провалившийся целиком, тоже занимает номер, и строка
# таблицы останется неполной навсегда. Дырка в таблице видна человеку, а две записи с одним
# ключом не видны никому.


def test_collected_repeats_провалившийся_прогон_тоже_занимает_номер(tmp_path):
    """У `poet` единственный прогон провален целиком, но номер 1 занят — следующий второй."""
    путь = файл_с_записями(
        tmp_path / "grid.jsonl",
        [
            строка_записи("apples", температура, прогон, "ok")
            for прогон in (1, 2)
            for температура in (0.0, 0.7, 1.2)
        ]
        + [
            строка_записи("poet", температура, 1, "error")
            for температура in (0.0, 0.7, 1.2)
        ],
    )

    assert collected_repeats(путь) == {"apples": 2, "poet": 1}


def test_collected_repeats_учитывает_последний_прогон_упавший_целиком(tmp_path):
    """Обрыв связи под конец: прогон 3 провален целиком и его номер нельзя выдать заново."""
    путь = файл_с_записями(
        tmp_path / "grid.jsonl",
        [
            строка_записи("apples", температура, прогон, "ok")
            for прогон in (1, 2)
            for температура in (0.0, 0.7, 1.2)
        ]
        + [
            строка_записи("apples", температура, 3, "error")
            for температура in (0.0, 0.7, 1.2)
        ],
    )

    assert collected_repeats(путь) == {"apples": 3}


def test_collected_repeats_считает_начатый_прогон_занятым(tmp_path):
    """Прогон 2 собран не полностью, но номер уже занят — продолжать надо с третьего."""
    путь = файл_с_записями(
        tmp_path / "grid.jsonl",
        [строка_записи("apples", температура, 1, "ok") for температура in (0.0, 0.7, 1.2)]
        + [
            строка_записи("apples", 0.0, 2, "ok"),
            строка_записи("apples", 0.7, 2, "ok"),
            строка_записи("apples", 1.2, 2, "error"),
        ],
    )

    assert collected_repeats(путь) == {"apples": 2}


def test_collected_repeats_берёт_наибольший_номер_а_не_число_прогонов(tmp_path):
    """Дырка в середине: прогон 2 провален целиком, но продолжать надо с четвёртого."""
    путь = файл_с_записями(
        tmp_path / "grid.jsonl",
        [
            строка_записи("apples", температура, прогон, "ok")
            for прогон in (1, 3)
            for температура in (0.0, 0.7, 1.2)
        ]
        + [
            строка_записи("apples", температура, 2, "error")
            for температура in (0.0, 0.7, 1.2)
        ],
    )

    assert collected_repeats(путь) == {"apples": 3}


def test_collected_repeats_на_несуществующем_файле_даёт_пустой_словарь(tmp_path):
    assert collected_repeats(tmp_path / "ничего-нет.jsonl") == {}


def test_collected_repeats_пропускает_испорченную_строку(tmp_path):
    путь = файл_с_записями(
        tmp_path / "grid.jsonl",
        [строка_записи("apples", температура, 1, "ok") for температура in (0.0, 0.7, 1.2)]
        + ["это не json, а обрывок строки"]
        + [строка_записи("apples", температура, 2, "ok") for температура in (0.0, 0.7, 1.2)],
    )

    assert collected_repeats(путь) == {"apples": 2}


# --- состав записи -------------------------------------------------------------------------


def результат_клетки():
    return CellResult(
        question_id="apples",
        temperature=0.7,
        repetition=2,
        answer="Шаг первый.\n\nответ: 3",
        value="3",
        finish_reason="stop",
        usage={"prompt_tokens": 120, "completion_tokens": 45},
        elapsed_ms=1830,
        status="ok",
        error=None,
    )


def test_build_record_содержит_все_поля():
    запись = build_record(
        результат_клетки(),
        by_id("apples"),
        "deepseek-v4-flash",
        {"max_tokens": 1024, "temperature": 0.7, "thinking": {"type": "disabled"}},
    )

    ожидаемые = {
        "run_key",
        "question_id",
        "question_text",
        "temperature",
        "repetition",
        "timestamp",
        "model",
        "params",
        "answer",
        "value",
        "finish_reason",
        "usage",
        "elapsed_ms",
        "status",
        "error",
        "code_version",
    }
    assert ожидаемые <= set(запись)


def test_build_record_переносит_params_как_есть():
    параметры = {"max_tokens": 1024, "temperature": 0.7, "thinking": {"type": "disabled"}}

    запись = build_record(результат_клетки(), by_id("apples"), "deepseek-v4-flash", параметры)

    assert запись["params"] == параметры


def test_build_record_не_содержит_ключа_доступа():
    запись = build_record(
        результат_клетки(),
        by_id("apples"),
        "deepseek-v4-flash",
        {"max_tokens": 1024, "temperature": 0.7, "thinking": {"type": "disabled"}},
    )

    for путь, узел in обойти(запись):
        if isinstance(узел, dict):
            встреченные = {str(ключ).lower() for ключ in узел}
            assert not (встреченные & ЗАПРЕЩЁННЫЕ_КЛЮЧИ), f"ключ доступа в записи: {путь}"
        if isinstance(узел, str):
            assert not ПОХОЖЕ_НА_КЛЮЧ.search(узел), f"значение похоже на ключ доступа: {путь}"


def test_build_record_привязывает_поля_к_источникам():
    """Каждое поле записи должно приходить из своего источника, а не быть заглушкой."""
    результат = результат_клетки()
    вопрос = by_id("apples")
    параметры = {"max_tokens": 1024, "temperature": 0.7, "thinking": {"type": "disabled"}}

    запись = build_record(результат, вопрос, "deepseek-v4-flash", параметры)

    assert запись["run_key"] == run_key(
        результат.question_id, результат.temperature, результат.repetition
    )
    assert запись["question_id"] == результат.question_id
    assert запись["temperature"] == результат.temperature
    assert запись["repetition"] == результат.repetition
    assert запись["answer"] == результат.answer
    assert запись["value"] == результат.value
    assert запись["status"] == результат.status
    assert запись["finish_reason"] == результат.finish_reason
    assert запись["elapsed_ms"] == результат.elapsed_ms
    assert запись["usage"] == результат.usage
    assert запись["model"] == "deepseek-v4-flash"
    # Полный текст вопроса, а не короткая подпись переключателя.
    assert запись["question_text"] == вопрос.text
    assert запись["question_text"] != вопрос.title
