import pytest

from calories import cli
from calories.core import Блюдо
from calories.ollama import МоделиНет, ОтветНегоден, СлужбаНедоступна, ЧужойАдрес


@pytest.fixture
def вызовы(monkeypatch):
    журнал = []
    monkeypatch.setattr(cli, "оценить", lambda н: журнал.append(("оценить", н)) or Блюдо(н, 65, 300, 45))
    monkeypatch.setattr(
        cli, "подобрать",
        lambda к, ч, ход=None: журнал.append(("подобрать", к, ч)) or [Блюдо("каша", 100, 440, 50)],
    )
    return журнал


@pytest.mark.parametrize("строка, вызов", [
    ("450", ("подобрать", 450.0, 5)),
    ("450,5", ("подобрать", 450.5, 5)),
    (" 450.5 ", ("подобрать", 450.5, 5)),
    ("450 ккал", ("подобрать", 450.0, 5)),
    ("450Ккал", ("подобрать", 450.0, 5)),
    ("борщ", ("оценить", "борщ")),
    ("7 злаков", ("оценить", "7 злаков")),
    ("-5", ("оценить", "-5")),
    ("nan", ("оценить", "nan")),
])
def test_направление_по_строке(вызовы, строка, вызов):
    cli.ответить(строка, 5)
    assert вызовы == [вызов]


def test_ноль_килокалорий_без_запроса(вызовы):
    assert "больше нуля" in cli.ответить("0", 5)
    assert вызовы == []


def test_пустая_строка_без_запроса(вызовы):
    assert "Назови блюдо" in cli.ответить("   ", 5)
    assert вызовы == []


def test_недостача_блюд_названа(вызовы, capsys):
    cli.main(["450", "--блюд", "3"])
    assert "меньше просимого: 1 из 3" in capsys.readouterr().out
    cli.main(["450", "--блюд", "1"])
    assert "меньше просимого" not in capsys.readouterr().out


def test_замер_через_команду(tmp_path, monkeypatch, capsys):
    from calories import measure

    набор = tmp_path / "набор.jsonl"
    набор.write_text('{"блюдо": "а", "ккал_100": 100, "ги": 50}\n', encoding="utf-8")
    monkeypatch.setattr(measure, "оценить", lambda н: Блюдо(н, 110, 200, 50))
    assert cli.main([f"--замер={набор}"]) == 0
    assert "Средняя ошибка калорийности: 10.0 %" in capsys.readouterr().out
    assert cli.main([f"--замер={tmp_path / 'нет.jsonl'}"]) == 1
    assert "Контрольный набор не принят" in capsys.readouterr().err


def test_замер_вместе_с_блюдом_отвергнут(вызовы):
    with pytest.raises(SystemExit):
        cli.main(["борщ", "--замер"])
    assert вызовы == []


def test_прерывание_даёт_код_130(monkeypatch, capsys):
    def прервать(н):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "оценить", прервать)
    assert cli.main(["борщ"]) == 130


def test_вид_прямого_ответа(вызовы, capsys):
    assert cli.main(["борщ", "со", "свининой"]) == 0
    вывод = capsys.readouterr().out
    assert "Борщ со свининой — оценка модели" in вывод
    assert "65 ккал на 100 г" in вывод and "300 г → 195 ккал" in вывод and "45 (низкий)" in вывод
    assert cli.ОГОВОРКА in вывод


def test_вид_обратного_ответа(вызовы, capsys):
    assert cli.main(["450", "--блюд", "3"]) == 0
    вывод = capsys.readouterr().out
    assert вызовы == [("подобрать", 450.0, 3)]
    assert "440 ккал (-10)" in вывод and cli.ОГОВОРКА in вывод


def test_не_еда(monkeypatch, capsys):
    monkeypatch.setattr(cli, "оценить", lambda н: None)
    assert cli.main(["табуретка"]) == 0
    assert "Не знаю такого блюда: табуретка" in capsys.readouterr().out


@pytest.mark.parametrize("ошибка, слова", [
    (СлужбаНедоступна("x"), ["http://127.0.0.1:11434", "ollama serve"]),
    (СлужбаНедоступна("ReadError: обрыв"), ["ReadError: обрыв"]),
    (ЧужойАдрес("https://example.org"), ["https://example.org", "запрос не отправлен"]),
    (МоделиНет("model not found"), ["qwen3.5:9b-q4_K_M-32k", "CALORIES_MODEL", "model not found"]),
    (ОтветНегоден("калорийность 950"), ["калорийность 950"]),
])
def test_отказ_даёт_код_1_и_причину(monkeypatch, capsys, ошибка, слова):
    def упасть(н):
        raise ошибка

    monkeypatch.setattr(cli, "оценить", упасть)
    assert cli.main(["борщ"]) == 1
    сообщение = capsys.readouterr().err
    assert all(слово in сообщение for слово in слова)


def test_разговор_переживает_отказ(monkeypatch, capsys):
    ответы = iter([ОтветНегоден("плохо"), Блюдо("арбуз", 30, 200, 72)])

    def оценить(н):
        значение = next(ответы)
        if isinstance(значение, Exception):
            raise значение
        return значение

    monkeypatch.setattr(cli, "оценить", оценить)
    строки = iter(["борщ", "арбуз", ""])
    monkeypatch.setattr("builtins.input", lambda приглашение: next(строки))
    assert cli.main([]) == 0
    вывод = capsys.readouterr().out
    assert "Ответ модели не принят: плохо" in вывод and "Арбуз — оценка модели" in вывод


def test_негодный_адрес_даёт_отказ_а_не_трассировку(monkeypatch, capsys):
    """Настоящий путь: `оценить` не подменён, адрес не разбирается, запрос не уходит."""
    monkeypatch.setenv("OLLAMA_HOST", "localhost:abc")
    assert cli.main(["борщ"]) == 1
    assert "localhost:abc" in capsys.readouterr().err


def test_чужой_адрес_через_команду(monkeypatch, capsys):
    monkeypatch.setenv("OLLAMA_HOST", "192.168.1.5")
    assert cli.main(["борщ"]) == 1
    assert "запрос не отправлен" in capsys.readouterr().err
