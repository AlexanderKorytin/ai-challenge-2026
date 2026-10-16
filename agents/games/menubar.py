"""Значок игрового агента в строке меню macOS: 🎮 со счётчиком, нажатие открывает веб.

Значок — отдельный процесс: строке меню нужен главный поток своего процесса (`NSApplication`),
а веб занят Starlette. Счётчик — открытые предложения плюс скидки и выходы игр в ленте, новее
последнего нажатия. Опрос веба раз в 60 с: задания идут раз в день и раз в неделю, чаще новому
взяться неоткуда.

Первый запуск (из «Игровой агент.app»): служб ещё нет — значок поднимает веб дочерним процессом
и открывает его в браузере. Как только опрос вкуса пройден, значок ставит службы
(`deploy/установить.sh службы`), гасит дочерний веб и выходит: его место занимает служба
значка, которую `launchd` поднимает сразу и при каждом входе в систему.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import objc
from AppKit import (
    NSApplication,
    NSApplicationActivationPolicyAccessory,
    NSStatusBar,
    NSVariableStatusItemLength,
)
from Foundation import NSObject, NSTimer

ЗДЕСЬ = Path(__file__).resolve().parent
АДРЕС = f"http://127.0.0.1:{os.environ.get('GAMES_WEB_PORT') or 8791}"
ОПРОС_С = 60.0
СЛУЖБА_ЗНАЧКА = Path.home() / "Library" / "LaunchAgents" / "local.games-agent.menubar.plist"
ДАННЫЕ = Path(os.environ.get("GAMES_DATA_DIR") or Path.home() / ".local" / "share" / "games-agent")
ОТМЕТКА = ДАННЫЕ / "menubar.json"


def службы_стоят() -> bool:
    return СЛУЖБА_ЗНАЧКА.exists()


def состояние() -> dict | None:
    try:
        with urllib.request.urlopen(f"{АДРЕС}/api/state", timeout=3) as ответ:
            return json.load(ответ)
    except (OSError, ValueError):
        return None


def последнее_нажатие() -> str:
    try:
        return json.loads(ОТМЕТКА.read_text("utf-8")).get("нажато", "")
    except (OSError, ValueError):
        return ""


def запомнить_нажатие() -> None:
    try:
        ДАННЫЕ.mkdir(parents=True, exist_ok=True)
        ОТМЕТКА.write_text(json.dumps({"нажато": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}), "utf-8")
    except OSError:
        pass


def счётчик(с: dict) -> int:
    нажато = последнее_нажатие()
    новое = [
        з for з in с.get("лента", [])
        if з.get("род") in ("скидка", "выход", "сверка") and str(з.get("время", "")) > нажато
    ]
    return len(с.get("предложения", [])) + len(новое)


# Имена класса и его сообщений — латиницей: Objective-C не принимает иных (`objc.error`).
class GamesMenuBar(NSObject):
    def init(self):
        self = objc.super(GamesMenuBar, self).init()
        if self is None:
            return None
        self.дочерний = None
        self.пункт = NSStatusBar.systemStatusBar().statusItemWithLength_(NSVariableStatusItemLength)
        кнопка = self.пункт.button()
        кнопка.setTitle_("🎮")
        кнопка.setToolTip_("Игровой агент — открыть")
        кнопка.setTarget_(self)
        кнопка.setAction_("openWeb:")
        return self

    def openWeb_(self, _отправитель) -> None:
        запомнить_нажатие()
        subprocess.run(["open", АДРЕС], check=False)
        self.refresh_(None)

    def поднять_веб(self) -> None:
        """Веб дочерним процессом — только пока служб нет."""
        if self.дочерний is not None and self.дочерний.poll() is None:
            return
        # На чистом Маке каталога данных ещё нет, а `Popen(cwd=…)` без него падает (просмотр).
        ДАННЫЕ.mkdir(parents=True, exist_ok=True)
        self.дочерний = subprocess.Popen(
            ["uv", "run", "--project", str(ЗДЕСЬ), "python", str(ЗДЕСЬ / "web.py")],
            cwd=ДАННЫЕ, env={**os.environ, "GAMES_DATA_DIR": str(ДАННЫЕ)},
        )
        for _ in range(60):
            if состояние() is not None:
                return
            time.sleep(0.5)

    def поставить_службы(self) -> None:
        """Опрос пройден: службы ставятся, дочерний веб гасится, значок уступает место службе."""
        итог = subprocess.run([str(ЗДЕСЬ / "deploy" / "установить.sh"), "службы"], check=False)
        if итог.returncode != 0:
            self.пункт.button().setToolTip_("Игровой агент: службы не поставились, см. ~/Library/Logs/games-agent")
            return
        if self.дочерний is not None:
            self.дочерний.terminate()
        NSApplication.sharedApplication().terminate_(None)

    def refresh_(self, _таймер) -> None:
        с = состояние()
        if с is None:
            self.пункт.button().setTitle_("🎮 ⚠")
            self.пункт.button().setToolTip_("Игровой агент: веб не отвечает")
            if not службы_стоят():
                self.поднять_веб()
            return
        if с.get("опрос_пройден") and not службы_стоят():
            self.поставить_службы()
            return
        число = счётчик(с)
        self.пункт.button().setTitle_(f"🎮 {число}" if число else "🎮")
        self.пункт.button().setToolTip_("Игровой агент — открыть")


def main(доводы: list[str]) -> int:
    приложение = NSApplication.sharedApplication()
    # Только значок в строке меню: без окна и без места в Dock.
    приложение.setActivationPolicy_(NSApplicationActivationPolicyAccessory)
    значок = GamesMenuBar.alloc().init()
    if состояние() is None and not службы_стоят():
        значок.поднять_веб()
    if "--open" in доводы:
        subprocess.run(["open", АДРЕС], check=False)
    значок.refresh_(None)
    NSTimer.scheduledTimerWithTimeInterval_target_selector_userInfo_repeats_(
        ОПРОС_С, значок, "refresh:", None, True
    )
    приложение.run()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
