"""Файл тайн: права, атомарная запись, испорченный файл, сокрытие ключей."""

import os
import stat

import pytest

import secrets_store as ss


@pytest.fixture(autouse=True)
def свой_каталог(tmp_path, monkeypatch):
    # Настоящий `~/.config/games-agent` проверки не трогают никогда (`CLAUDE.md`, «Грабли»).
    monkeypatch.setenv("GAMES_CONFIG_DIR", str(tmp_path / "games-agent"))
    return tmp_path / "games-agent"


def права(путь) -> int:
    return stat.S_IMODE(os.stat(путь).st_mode)


ТАЙНЫ = ss.Тайны(steam_ключ="ABCDEF0123456789ABCDEF0123456789", steamid="76561197960287930",
                 itad_ключ="itad-ключ-123", deepseek_ключ="sk-deepseek-секрет")


def test_нет_файла_пустые_тайны():
    assert ss.загрузить() == ss.Тайны()


def test_права_и_круг(свой_каталог):
    ss.сохранить(ТАЙНЫ)
    assert права(свой_каталог) == 0o700
    assert права(ss.путь()) == 0o600
    assert ss.загрузить() == ТАЙНЫ


def test_каталог_с_широкими_правами_сужается(свой_каталог):
    свой_каталог.mkdir(mode=0o755)
    os.chmod(свой_каталог, 0o755)
    ss.сохранить(ТАЙНЫ)
    assert права(свой_каталог) == 0o700


def test_временный_файл_рождается_с_600(monkeypatch, свой_каталог):
    """Права ставятся до записи: в миг записи содержимого файл уже 600."""
    увидено = []
    настоящий = os.fdopen

    def подсмотреть(дескриптор, *a, **k):
        увидено.append(stat.S_IMODE(os.fstat(дескриптор).st_mode))
        return настоящий(дескриптор, *a, **k)

    monkeypatch.setattr(ss.os, "fdopen", подсмотреть)
    ss.сохранить(ТАЙНЫ)
    assert увидено == [0o600]


def test_повторная_запись_атомарна(monkeypatch, свой_каталог):
    ss.сохранить(ТАЙНЫ)
    прежний = ss.путь().read_text("utf-8")

    def сломать(*_):
        raise OSError("диск отказал")

    monkeypatch.setattr(ss.os, "replace", сломать)
    with pytest.raises(OSError):
        ss.сохранить(ss.Тайны(steam_ключ="новый"))
    # Прежний файл цел, временного не осталось.
    assert ss.путь().read_text("utf-8") == прежний
    assert [п.name for п in свой_каталог.iterdir()] == ["config.json"]


def test_повторная_запись_заменяет_узел(свой_каталог):
    ss.сохранить(ТАЙНЫ)
    узел = os.stat(ss.путь()).st_ino
    ss.сохранить(ss.Тайны(steam_ключ="другой"))
    assert os.stat(ss.путь()).st_ino != узел
    assert ss.загрузить().steam_ключ == "другой"
    assert права(ss.путь()) == 0o600


def test_испорченный_файл_путь_без_содержимого(свой_каталог):
    свой_каталог.mkdir()
    ss.путь().write_text('{"steam_ключ": "ТАЙНА-В-ФАЙЛЕ", обрыв', "utf-8")
    with pytest.raises(ss.ОшибкаТайн) as беда:
        ss.загрузить()
    assert str(ss.путь()) in str(беда.value)
    assert "ТАЙНА-В-ФАЙЛЕ" not in str(беда.value)
    # Содержимое не держит и цепочка исключений.
    assert беда.value.__context__ is None and беда.value.__cause__ is None


def test_не_объект_испорчен(свой_каталог):
    свой_каталог.mkdir()
    ss.путь().write_text('["ключ"]', "utf-8")
    with pytest.raises(ss.ОшибкаТайн):
        ss.загрузить()


def test_поле_не_строкой_пустое(свой_каталог):
    свой_каталог.mkdir()
    ss.путь().write_text('{"steam_ключ": 5, "itad_ключ": "x"}', "utf-8")
    assert ss.загрузить() == ss.Тайны(itad_ключ="x")


def test_скрыть_все_ключи():
    текст = (f"GET ?key={ТАЙНЫ.steam_ключ} itad={ТАЙНЫ.itad_ключ} "
             f"deepseek={ТАЙНЫ.deepseek_ключ} id={ТАЙНЫ.steamid}")
    скрыто = ss.скрыть(текст, ТАЙНЫ)
    for ключ in (ТАЙНЫ.steam_ключ, ТАЙНЫ.itad_ключ, ТАЙНЫ.deepseek_ключ):
        assert ключ not in скрыто
    assert скрыто.count("***") == 3
    # SteamID тайной не считается.
    assert ТАЙНЫ.steamid in скрыто


def test_скрыть_пустые_ключи_не_трогает_текст():
    assert ss.скрыть("ничего тайного", ss.Тайны()) == "ничего тайного"


def test_скрыть_вложенный_ключ():
    т = ss.Тайны(steam_ключ="abc", itad_ключ="abcdef")
    assert ss.скрыть("abcdef", т) == "***"
