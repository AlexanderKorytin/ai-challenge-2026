"""Ключи командной строки — отдельным лёгким модулем.

Из зависимостей здесь только `argparse`, и это условие, а не совпадение. Точка входа пакета
обязана понять, какой режим просят, ДО того как загружено что-то тяжёлое: у пакетного режима
нет ни окна, ни клавиатуры, и тянуть ради него `prompt_toolkit` со всем интерфейсом незачем.
Раньше разбор ключей жил в `cli.py`, то есть узнать режим можно было только загрузив весь
интерфейс, — узел, который этот модуль и развязывает.
"""

from __future__ import annotations

import argparse


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="myharness", description="Терминальный harness для DeepSeek API")
    parser.add_argument("--profile", help="профиль генерации, применяемый при старте")
    parser.add_argument("--model", help="модель DeepSeek")
    parser.add_argument(
        "--batch",
        metavar="PATH",
        help="выполнить наряд без интерфейса и не спрашивая человека",
    )
    parser.add_argument(
        "--concurrency",
        type=int,
        metavar="N",
        help="сколько заданий наряда выполнять одновременно; основное значение задаёт сам наряд, "
        "этот ключ его перекрывает",
    )
    return parser.parse_args(argv)
