"""Проверка хранилища: запись и чтение дают те же задачи."""

from tasks.models import Task
from tasks.storage import load_tasks, save_tasks


def test_roundtrip(tmp_path, monkeypatch):
    """Записанные задачи читаются обратно без изменений."""
    monkeypatch.setenv("TASKS_FILE", str(tmp_path / "t.json"))
    save_tasks([Task(1, "купить хлеб")])
    assert load_tasks() == [Task(1, "купить хлеб")]
