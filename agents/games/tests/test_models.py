"""Выбор местной модели и переключение советника по облачному ключу."""

import pytest

import advisor
import games_db
import judge
import models
import secrets_store


def test_выбор_пишется_и_читается_отбором(monkeypatch):
    monkeypatch.delenv("GAMES_LOCAL_MODEL", raising=False)
    с = games_db.открыть()
    assert judge.модель() == "ollama/" + models.ПО_УМОЛЧАНИЮ
    models.выбрать(с, "qwen3.5:9b-q4_K_M")
    assert judge.модель() == "ollama/qwen3.5:9b-q4_K_M"
    with pytest.raises(ValueError):
        models.выбрать(с, "чужая:модель")


def test_советник_без_ключа_местный_с_ключом_облачный():
    assert advisor.модель() == "ollama/" + models.ВАРИАНТ
    т = secrets_store.загрузить()
    т.deepseek_ключ = "sk-проба"
    secrets_store.сохранить(т)
    assert not advisor.модель().startswith("ollama/")


def test_память_мака_и_сбой(monkeypatch):
    class Итог:
        stdout = str(18 * 2**30)

    monkeypatch.setattr(models.subprocess, "run", lambda *а, **к: Итог())
    assert models.память_мака() == 18

    def сбой(*а, **к):
        raise OSError("нет sysctl")

    monkeypatch.setattr(models.subprocess, "run", сбой)
    assert models.память_мака() == 0


def test_местный_советник_без_рассуждений(monkeypatch, tmp_path):
    from pathlib import Path
    monkeypatch.setenv("MYHARNESS_PROFILES", str(Path(advisor.__file__).parent / "profiles"))
    assert advisor.профиль().params.get("thinking") == {"type": "disabled"}
    т = secrets_store.загрузить()
    т.deepseek_ключ = "sk-проба"
    secrets_store.сохранить(т)
    assert advisor.профиль().params.get("thinking") is None
