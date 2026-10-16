import pytest

import judge


def test_разбор_ответа():
    assert judge.разобрать('{"подходит": true, "причина": "  пошаговые  бои "}') == judge.Вердикт(True, "пошаговые бои")
    with pytest.raises(ValueError, match="не JSON"):
        judge.разобрать("да, подходит")
    with pytest.raises(ValueError, match="подходит"):
        judge.разобрать('{"причина": "x"}')
