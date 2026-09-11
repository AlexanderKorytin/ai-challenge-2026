"""Полноэкранное приложение целиком: клавиши, меню, панель параметров, ответ модели.

Настоящий терминал не нужен: prompt_toolkit умеет работать на трубе и пустом выводе,
а вместо DeepSeek подставлен поддельный клиент — проверки ничего не стоят и не жгут ключ.
"""

import ast
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

tmp = Path(tempfile.mkdtemp())
os.environ["MYHARNESS_PROFILES"] = str(tmp / "profiles")
# Настройки — во временный каталог: иначе проверки пишут в настоящий config.json
# пользователя и затирают его ключ. Так уже случилось однажды.
os.environ["MYHARNESS_CONFIG_DIR"] = str(tmp / "config")
os.environ["MYHARNESS_JOURNAL"] = str(tmp / "journal.jsonl")
# Каталог состояния — туда же: разговоры и глобальные факты пишутся между запусками,
# и без переопределения проверки замусорили бы настоящий ~/.local/state пользователя.
os.environ["MYHARNESS_STATE_DIR"] = str(tmp / "state")
(tmp / "profiles").mkdir(parents=True)

from prompt_toolkit.application import create_app_session  # noqa: E402
from prompt_toolkit.key_binding.key_processor import KeyPress  # noqa: E402
from prompt_toolkit.keys import Keys  # noqa: E402
from prompt_toolkit.input import create_pipe_input  # noqa: E402
from prompt_toolkit.layout.containers import Window  # noqa: E402
from prompt_toolkit.output import DummyOutput  # noqa: E402

from prompt_toolkit.data_structures import Point  # noqa: E402
from prompt_toolkit.mouse_events import MouseButton, MouseEvent, MouseEventType  # noqa: E402

from myharness import api, archivist, cli, memory, output, picker as picker_mod, profiles, ui  # noqa: E402
from myharness import compact, screens, tokens  # noqa: E402
from myharness.config import Config  # noqa: E402

failures = []
DOWN, ENTER, ESC = "\x1b[B", "\r", "\x1b"


def check(name, condition, detail=""):
    print(f"  [{'OK ' if condition else 'СБОЙ'}] {name}" + (f" — {detail}" if detail and not condition else ""))
    if not condition:
        failures.append(name)


class FakeClient:
    """Отвечает заранее заданным потоком, запоминая, с чем его позвали."""

    def __init__(self):
        self.calls = []
        self.closed = False
        # Черновик задаётся полем, а не строкой в коде: разделу про свёртывание нужен
        # многострочный, иначе прятать нечего — число видимых строк не изменится.
        self.reasoning = "прикидываю…"

    async def stream_chat(self, model, messages, params=None):
        self.calls.append({"model": model, "messages": messages, "params": dict(params or {})})
        yield api.StreamEvent("reasoning", self.reasoning)
        yield api.StreamEvent("content", '{"status": "ok", "name": "щука"}')
        # Разбивка входа сходится с `prompt_tokens` (8 + 4 = 12) намеренно: не сойдись она,
        # счёт денег считает весь вход промахом, и проверка «из кэша N» показывала бы не то,
        # что показывает живой обмен.
        yield api.StreamEvent(
            "meta",
            finish_reason="length",
            usage={
                "prompt_tokens": 12,
                "completion_tokens": 34,
                "prompt_cache_hit_tokens": 8,
                "prompt_cache_miss_tokens": 4,
                "completion_tokens_details": {"reasoning_tokens": 7},
            },
        )

    async def list_models(self):
        return ["deepseek-v4-flash", "deepseek-v4-pro", "deepseek-v4-flash-vision-exp"]

    async def aclose(self):
        self.closed = True

class УправляемыйКлиент:
    """Удерживает каждый обмен отдельно и помечает его вопросом из настоящего Enter."""

    def __init__(self):
        self.calls = []
        self.started = {}
        self.releases = {}
        self.cancelled = {}
        self.closed = False

    def _event(self, collection, content):
        return collection.setdefault(content, asyncio.Event())

    async def wait_started(self, content):
        await self._event(self.started, content).wait()

    def release(self, content):
        self._event(self.releases, content).set()

    async def wait_cancelled(self, content):
        await self._event(self.cancelled, content).wait()

    async def stream_chat(self, model, messages, params=None):
        content = messages[-1]["content"]
        self.calls.append({"model": model, "messages": messages, "params": dict(params or {})})
        self._event(self.started, content).set()
        try:
            await self._event(self.releases, content).wait()
        except asyncio.CancelledError:
            self._event(self.cancelled, content).set()
            raise
        yield api.StreamEvent("content", f"ответ:{content}")
        yield api.StreamEvent(
            "meta",
            finish_reason="stop",
            usage={"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5},
        )

    async def list_models(self):
        return list(api.FALLBACK_MODELS)

    async def aclose(self):
        self.closed = True


def fragments_text(fragments):
    """Фрагмент — (стиль, текст) либо (стиль, текст, обработчик мыши): вкладки кликабельны."""
    return "".join(fragment[1] for fragment in fragments)


def log_text(state, target=None):
    """Лента экрана (его первой панели) или конкретной панели."""
    target = target or state.main
    pane = target.first if hasattr(target, "first") else target
    return fragments_text(pane.log)


def строки_ожидания(панель, начало=0):
    """Строки ожидания из ленты панели — по знакам колонок, а не по их порядку в ленте.

    Отбираем по «↑ ↓ Σ» вместе: поодиночке любой из знаков может встретиться в ответе модели,
    а все три в одной строке — почерк ровно этой строки.
    """
    текст = fragments_text(панель.log[начало:])
    return [строка for строка in текст.splitlines() if all(знак in строка for знак in ("↑", "↓", "Σ"))]


def _число_после(строка, знак, следующий):
    """Цифры между двумя знаками колонок. Разряды разделены неразрывным пробелом, поэтому
    склеиваем одни цифры — сравнивать надо число, а не его оформление."""
    хвост = строка.split(знак, 1)[1].split(следующий, 1)[0]
    return "".join(з for з in хвост if з.isdigit())


def итог_из(строка):
    """Колонка «Σ» строки ожидания как текст. Именно текст, а не число: итог печатается
    сокращением («12.4k»), и сравнивать его надо с тем же сокращением, а не с цифрами."""
    return строка.split("Σ", 1)[1].strip()


def исходящие_из(строка):
    return _число_после(строка, "↑", "↓")


def входящие_из(строка):
    return _число_после(строка, "↓", "Σ")


def panel_fragments(app):
    """Фрагменты списка агентов прямо из раскладки — с обработчиками щелчка.

    Ищем по строке подсказок над списком: собственного имени у окна нет, а подделывать
    фрагменты рядом с приложением значило бы проверять не то, что видит пользователь."""
    for window in app.layout.walk():
        if not isinstance(window, Window):
            continue
        getter = getattr(window.content, "text", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, list) and any("↑/↓" in item[1] for item in value if len(item) >= 2):
            return value
    return []


def panel_text(app):
    return "".join(item[1] for item in panel_fragments(app))


def видимых_строк(панель):
    """Сколько строк на экране на самом деле — счётом по видимой ленте, а не по счётчику.

    Считать по `visible_lines()` нельзя: это и есть проверяемое, и сравнение вышло бы
    тождеством, которое проходит при любом сломанном счёте."""
    return sum(текст.count("\n") for _, текст in панель.visible_log())


def pane_control(app, панель):
    """Управляющий элемент панели прямо из раскладки: по нему видно, куда встаёт курсор,
    а по курсору окно доматывает ленту вниз."""
    видимое = панель.visible_log()
    for window in app.layout.walk():
        if not isinstance(window, Window):
            continue
        getter = getattr(window.content, "text", None)
        if not callable(getter):
            continue
        try:
            значение = getter()
        except Exception:
            continue
        if значение == видимое:
            return window.content
    return None


def press(app, key, sequence):
    app.key_processor.feed(KeyPress(key, sequence))
    app.key_processor.process_keys()


def screen_texts(app):
    """Тексты всех окон текущей раскладки — так видно, что показывает строка состояния."""
    out = []
    for window in app.layout.walk():
        if not isinstance(window, Window):
            continue
        getter = getattr(window.content, "text", None)
        if not callable(getter):
            continue
        try:
            value = getter()
        except Exception:
            continue
        if isinstance(value, list):
            out.append(fragments_text(value))
    return out


async def main():
    fake = FakeClient()
    state = cli.State(
        # Сбор фактов выключен на время разделов 1–11: архивариус — лишний запрос к
        # подставному клиенту, и он сбил бы счёт вызовов там, где вызовы считают поимённо.
        # Включают его обратно в разделе 12в, где он и проверяется.
        config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
        client=fake,
        model="deepseek-v4-flash",
        profile=profiles.builtin_default(),
    )

    with create_pipe_input() as pipe, create_app_session(input=pipe, output=DummyOutput()):
        # Запускаем настоящий `repl`, а не отдельно собранное приложение: только так выход
        # проходит тот же жизненный цикл отмены исполнителей, что и у пользователя.
        run = asyncio.create_task(cli.repl(state))
        while state.app is None:
            await asyncio.sleep(0)
        app = state.app
        await asyncio.sleep(0.1)

        async def send(text, pause=0.12):
            pipe.send_text(text)
            await asyncio.sleep(pause)

        print("\n1. Меню команд по «/»")
        await send("/")
        buffer = app.layout.get_buffer_by_name("text-area") or app.current_buffer
        check("список команд открылся без Enter", buffer.complete_state is not None)
        count = len(buffer.complete_state.completions) if buffer.complete_state else 0
        # Число сверяем с самим списком команд, а не с записанным от руки: список растёт от
        # задания к заданию, и записанное число ловило бы каждый его рост как поломку. Смысл
        # проверки в другом: авторизованному не показывают /auth, и меню не пустует.
        видимые = ui.visible_commands(True)
        имена = [имя for имя, *_ in видимые]
        check("в списке команды авторизованного, без /auth", count == len(видимые), f"их {count}, в наборе {len(видимые)}")
        check("/auth из меню авторизованного убран", "/auth" not in имена, str(имена))
        # Команды сжатия обязаны быть в меню и в справке: без них выжимку нечем ни увидеть,
        # ни собрать по требованию, а работает она молча и в фоне.
        check("команды выжимки есть в меню", {"/context", "/compact"} <= set(имена), str(имена))
        справка = fragments_text(ui.help_fragments())
        check("команды выжимки описаны в справке", "/context" in справка and "/compact" in справка)
        check(
            "команды стратегий есть в меню и справке",
            {"/strategy", "/facts", "/branch"} <= set(имена)
            and all(command in справка for command in ("/strategy", "/facts", "/branch")),
            str(имена),
        )
        check(
            "до авторизации команды стратегий не предлагаются",
            not ({"/strategy", "/facts", "/branch"} & {
                name for name, *_ in ui.visible_commands(False)
            }),
        )
        check("пока агент один, списка агентов нет", not cli.show_agent_panel(state))

        await send(DOWN)
        check("стрелка выбирает пункт", buffer.complete_state.current_completion is not None)
        await send(ENTER)
        check("Enter подставляет команду, а не отправляет её", buffer.text.startswith("/"), repr(buffer.text))
        check("сообщение в очередь не ушло", state.queue.qsize() == 0)

        await send(ESC + "\x7f" * 40)  # закрыть меню и очистить строку

        print("\n2. Панель параметров")
        await send("/set temperature" + ENTER)
        check("панель открылась", state.picker is not None)
        await send("щука")  # печать при открытой панели не должна проходить
        check("панель модальна — текст в строку не попал", state.picker is not None and "щука" not in buffer.text)
        await send(DOWN + ENTER)
        check("значение выбрано стрелкой и применено", "temperature" in state.profile.params, str(state.profile.params))
        check("панель закрылась", state.picker is None)
        await send("/set max_tokens" + ENTER)
        # Одиночный Esc через трубу не доходит: \x1b — префикс escape-последовательностей,
        # и парсер ждёт продолжения, которого в тесте не будет (в живом терминале его
        # выпускает таймаут). Поэтому клавишу подаём прямо обработчику.
        app.key_processor.feed(KeyPress(Keys.Escape, "\x1b"))
        app.key_processor.process_keys()
        await asyncio.sleep(0.5)  # Esc выпускается по таймауту ожидания продолжения (app.timeoutlen)
        check(
            "Esc закрывает без изменений",
            state.picker is None and "max_tokens" not in state.profile.params,
            f"panel={state.picker is not None}, params={state.profile.params}",
        )
        await send("/set max_tokens" + ENTER)
        await send("\x03")  # Ctrl+C — второй путь выхода из панели
        check("Ctrl+C тоже закрывает панель", state.picker is None and "max_tokens" not in state.profile.params)

        print("\n3. Списки — панель выбора, а не текст в логе")
        await send("/model" + ENTER, pause=0.25)
        check("/model открыл панель", state.picker is not None and len(state.picker.items) == 3, str(state.picker))
        check("текущая модель помечена", state.picker.marked == 0)
        await send(DOWN + ENTER)
        check("модель выбрана стрелкой", state.model == "deepseek-v4-pro", state.model)

        await send("/profile" + ENTER, pause=0.2)
        check("/profile открыл панель", state.picker is not None)
        titles = [i.label for i in state.picker.items]
        check("в панели есть профили", "s3" in titles or "default" in titles, str(titles))
        await send(ESC, pause=0.5)

        await send("/params" + ENTER, pause=0.2)
        check("/params открыл панель параметров", state.picker is not None and len(state.picker.items) >= 7)
        await send(ESC, pause=0.5)
        check("панель закрыта перед следующим шагом", state.picker is None)

        print("\n4. Строка состояния")
        check("показывает, что ключ есть", any("● авторизован" in s for s in screen_texts(app)))
        state.config.api_key = None
        check("сразу отражает потерю ключа", any("не авторизован" in s for s in screen_texts(app)))
        state.config.api_key = "sk-test"
        check("и возвращается обратно без перезапуска", any("● авторизован" in s for s in screen_texts(app)))

        print("\n5. Ответ модели")
        отметка_5 = len(state.main.first.log)
        await send("щука" + ENTER, pause=0.4)
        text = log_text(state)
        check("вопрос показан", "› щука" in text)
        check("рассуждения показаны", "прикидываю…" in text)
        check("ответ показан", '"status": "ok"' in text)
        ожидание_5 = строки_ожидания(state.main.first, отметка_5)
        check("строка ожидания осталась в ленте", len(ожидание_5) == 1, str(ожидание_5))
        check(
            "в ней серверные числа расхода, а не живые",
            ожидание_5 and исходящие_из(ожидание_5[0]) == "12" and входящие_из(ожидание_5[0]) == "34",
            str(ожидание_5),
        )
        check("строка замерла: слова «думаю» в ней нет", ожидание_5 and "думаю" not in ожидание_5[0], str(ожидание_5))
        check("обрыв по лимиту назван прямо", "упёрлось в max_tokens" in text)
        check("подсказка, что делать с обрывом", "/set max_tokens" in text)
        # Строка итога говорит то, чего нет в замершей строке ожидания, и не повторяет её.
        итог_5 = fragments_text(state.main.first.log[отметка_5:])
        check("строка итога называет попадания в кэш", "из кэша 8" in итог_5, итог_5[-200:])
        check("строка итога называет деньги", "¢" in итог_5 or "$" in итог_5, итог_5[-200:])
        check("строка итога называет профиль", "профиль: default" in итог_5, итог_5[-200:])
        check("токены из строки итога убраны — они уже в строке ожидания", "токены: вход" not in итог_5)
        check("заголовок размышлений получил число токенов", "▸ размышления · 7 токенов" in итог_5, итог_5[:200])
        check("temperature ушла в запрос", fake.calls[0]["params"].get("temperature") is not None)
        check("запрос ушёл выбранной моделью", fake.calls[0]["model"] == "deepseek-v4-pro", fake.calls[0]["model"])

        print("\n5а. Строка ожидания живёт весь обмен, замирает и остаётся в ленте")
        # Три свойства сразу: живые числа во время потока, замирание с серверными числами и
        # то, что следующий вопрос заводит СВОЮ строку, а старая больше не меняется. Без
        # последнего лента не становится таблицей роста расхода, ради которой всё и затеяно.

        class МедленныйКлиент:
            """Отдаёт поток кусками с паузой между ними.

            Подставной клиент раздела 5 успевает кончиться раньше первого кадра вращения —
            на нём живых чисел не увидеть вовсе, и проверять было бы нечего."""

            def __init__(self):
                self.calls = []

            async def stream_chat(self, model, messages, params=None):
                self.calls.append({"model": model, "messages": messages})
                for кусок in ("мысль раз ", "мысль два ", "мысль три "):
                    await asyncio.sleep(0.12)
                    yield api.StreamEvent("reasoning", кусок)
                await asyncio.sleep(0.12)
                yield api.StreamEvent("content", "готово")
                yield api.StreamEvent(
                    "meta",
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 100,
                        "completion_tokens": 40,
                        "total_tokens": 140,
                        "prompt_cache_hit_tokens": 64,
                        "prompt_cache_miss_tokens": 36,
                        "completion_tokens_details": {"reasoning_tokens": 9},
                    },
                )

            async def list_models(self):
                return list(api.FALLBACK_MODELS)

            async def aclose(self):
                pass

        медленный = МедленныйКлиент()
        state.client = медленный
        отметка = len(state.main.first.log)
        pipe.send_text("медленный вопрос" + ENTER)
        await asyncio.sleep(0.2)
        живые_1 = строки_ожидания(state.main.first, отметка)
        await asyncio.sleep(0.25)
        живые_2 = строки_ожидания(state.main.first, отметка)
        check("во время обмена строка говорит «думаю…»", bool(живые_1) and "думаю…" in живые_1[0], str(живые_1))
        check(
            "строка несёт живой расход: вход, выход, итог сеанса",
            bool(живые_1) and all(знак in живые_1[0] for знак in ("↑", "↓", "Σ")),
            str(живые_1),
        )
        check(
            "вес запроса известен, не дожидаясь ответа сервера",
            bool(живые_1) and исходящие_из(живые_1[0]) not in ("", "0"),
            str(живые_1),
        )
        check(
            "входящее число растёт по ходу потока",
            bool(живые_1)
            and bool(живые_2)
            and int(входящие_из(живые_2[0])) > int(входящие_из(живые_1[0])),
            f"{живые_1} → {живые_2}",
        )
        check(
            "строка на весь обмен одна — печатью её не размножает ни один кусок потока",
            len(живые_1) == 1 and len(живые_2) == 1,
            f"{len(живые_1)} и {len(живые_2)}",
        )
        await asyncio.sleep(0.7)  # дать обмену дойти до конца
        первая = строки_ожидания(state.main.first, отметка)
        check("после ответа строка замерла со знаком исхода", bool(первая) and первая[0].startswith("✓"), str(первая))
        check(
            "в замершей строке серверные числа, а не живые",
            bool(первая) and исходящие_из(первая[0]) == "100" and входящие_из(первая[0]) == "40",
            str(первая),
        )
        снимок = первая[0] if первая else ""
        await send("второй медленный вопрос" + ENTER, pause=1.0)
        обе = строки_ожидания(state.main.first, отметка)
        check("у второго вопроса своя строка ожидания", len(обе) == 2, str(обе))
        check("числа в первой строке больше не меняются", bool(обе) and обе[0] == снимок, f"{снимок!r} → {обе[:1]}")
        # Итог сеанса здесь ещё трёхзначный, поэтому в строке он напечатан целиком — сравнение
        # цифр законно. Дорасти он до тысяч, и строка показала бы «1.2k»: тогда сравнивать
        # пришлось бы иначе, но и день, когда это случится, наступит не в этой проверке.

        def итог(строка):
            return int("".join(з for з in строка.split("Σ", 1)[1] if з.isdigit()))

        check(
            "итог сеанса во второй строке вырос — лента стала таблицей роста",
            len(обе) == 2 and итог(обе[1]) > итог(обе[0]),
            str(обе),
        )
        # Отмена — третий исход, и знак у неё свой. Стирать строку нельзя и здесь: обмен шёл,
        # время потрачено, часть токенов у поставщика списана, и делать вид, будто запроса не
        # было, значило бы прятать израсходованное.
        отметка_отмены = len(state.main.first.log)
        pipe.send_text("отменяемый вопрос" + ENTER)
        await asyncio.sleep(0.2)
        await send("\x03", pause=0.4)
        отменённая = строки_ожидания(state.main.first, отметка_отмены)
        check("отменённый обмен оставил свою строку в ленте", len(отменённая) == 1, str(отменённая))
        check("у отмены свой знак исхода", bool(отменённая) and отменённая[0].startswith("⊘"), str(отменённая))
        check(
            "и числа помечены неточными — серверных не дождались",
            bool(отменённая) and "~" in отменённая[0],
            str(отменённая),
        )
        state.client = fake

        print("\n5б. Два тихих пути строки итога: предел веса и незнакомый тариф")
        # Оба — по одной строке кода, и оба молчаливые: сломайся они, ни одна другая проверка
        # этого не заметит. Предел, о превышении которого не сказано, хуже отсутствующего:
        # на отсутствующий человек не рассчитывает. Цена незнакомой модели, показанная нулём,
        # читается как «бесплатно», и правду человек узнает из счёта в конце месяца.
        (tmp / "profiles" / "tight.json").write_text(
            json.dumps(
                # Предел выше веса пустого запроса (83), иначе разбор профиля ругается сам и
                # проверка ловила бы его предупреждение вместо нашего. Вопрос длинный — в
                # такой предел он не влезает при любой надбавке обёртки.
                {"name": "tight", "system": "коротко", "keep_history": True, "budget_tokens": 90},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        await send("/profile tight" + ENTER, pause=0.25)
        отметка_предела = len(state.main.first.log)
        await send("вопрос, который заведомо не влезает в предел: " + "щука " * 60 + ENTER, pause=0.5)
        предел = fragments_text(state.main.first.log[отметка_предела:])
        check("о превышении предела веса сказано вслух", "тяжелее заданного предела" in предел, предел[-300:])
        check("и сказано, что с этим делать", "/budget" in предел, предел[-300:])

        прежняя_модель = state.model
        state.model = "модель-без-тарифа"
        отметка_тарифа = len(state.main.first.log)
        await send("вопрос незнакомой модели" + ENTER, pause=0.5)
        тариф = fragments_text(state.main.first.log[отметка_тарифа:])
        check("у модели без тарифа цена не выдумывается", "тариф неизвестен" in тариф, тариф[-300:])
        check("и нулём она не притворяется", "0.00" not in тариф, тариф[-300:])
        state.model = прежняя_модель

        print("\n6. Журнал")
        record = json.loads(Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").strip().splitlines()[0])
        check("прогон записан", record["status"] == "ok" and record["query"] == "щука")
        check("в записи слепок профиля с параметрами", "temperature" in record["profile"]["params"])
        check("в записи причина остановки и токены", record["finish_reason"] == "length" and record["usage"]["completion_tokens"] == 34)
        check("ключ в журнал не попал", "sk-test" not in json.dumps(record, ensure_ascii=False))

        print("\n6a. История разговора")
        # Профиль с накоплением истории: два вопроса подряд обязаны попасть в один разговор.
        profiles_dir = tmp / "profiles"
        (profiles_dir / "talky.json").write_text(
            json.dumps({"name": "talky", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile talky" + ENTER, pause=0.25)
        before = len(fake.calls)
        await send("первый вопрос" + ENTER, pause=0.4)
        await send("второй вопрос" + ENTER, pause=0.4)
        first, second = fake.calls[before], fake.calls[before + 1]
        # Подставной клиент всегда отдаёт содержимое ответа, значит после первого обмена
        # в истории лежит ровно одна пара «вопрос — ответ» = 2 сообщения. Системная
        # инструкция кладётся поверх истории и в саму историю не входит.
        check(
            "в первый запрос ушли только инструкция и вопрос",
            len(first["messages"]) == 2,
            str([m["role"] for m in first["messages"]]),
        )
        check(
            "во второй запрос ушла история предыдущего обмена",
            len(second["messages"]) == 4,
            str([m["role"] for m in second["messages"]]),
        )
        check(
            "роли второго запроса идут по порядку",
            [m["role"] for m in second["messages"]] == ["system", "user", "assistant", "user"],
            str([m["role"] for m in second["messages"]]),
        )
        check(
            "в истории лежит первый вопрос, а не второй",
            second["messages"][1]["content"] == "первый вопрос",
            repr(second["messages"][1]["content"]),
        )

        print("\n6b. Обрезка памяти видна пользователю")
        # Молчаливая обрезка недопустима: первая же потерянная отсылка («сделай короче», а
        # сокращать уже нечего) будет отлажена пользователем как «модель поглупела». Поле
        # `dropped_pairs` само по себе ничего не значит — важно, что о нём СКАЗАНО в ленте.
        # ОТКУДА ЧИСЛА: окно — 1 пара, запас обрезки — 5, значит порог 1 + 5 = 6 пар. Кладём
        # в память ровно шесть пар и задаём вопрос: обрезка выбрасывает весь запас — 5 пар.
        (profiles_dir / "forgetful.json").write_text(
            json.dumps(
                {"name": "forgetful", "system": "помни немного", "keep_history": True, "history_window": 1},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        await send("/profile forgetful" + ENTER, pause=0.25)
        for номер in range(6):
            state.main_agent.remember(f"старый вопрос {номер}", f"старый ответ {номер}")
        mark = len(state.main.first.log)
        await send("свежий вопрос" + ENTER, pause=0.4)
        свежее = fragments_text(state.main.first.log[mark:])
        check("про обрезку памяти сказано в ленте", "память обрезана" in свежее, свежее[-200:])
        check("названо, сколько пар выброшено", "выброшено 5 пар" in свежее, свежее[-200:])

        print("\n7. Группа агентов")
        profiles_dir = tmp / "profiles"
        for name, system in (("analyst", "ты аналитик"), ("critic", "ты критик")):
            (profiles_dir / f"{name}.json").write_text(
                json.dumps({"name": name, "system": system, "keep_history": False}, ensure_ascii=False),
                encoding="utf-8",
            )
        (profiles_dir / "lead.json").write_text(
            json.dumps(
                {"name": "lead", "system": "сведи ответы", "keep_history": False, "agents": ["analyst", "critic"]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "meta.json").write_text(
            json.dumps({"name": "meta", "prefill": "составь промпт для задачи"}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile meta" + ENTER, pause=0.25)
        check("заготовка профиля подставлена в строку ввода", buffer.text == "составь промпт для задачи", repr(buffer.text))
        check("но сама не отправлена — ждём Enter", state.queue.qsize() == 0 and not state.busy)
        await send("\x7f" * 40)

        await send("/team" + ENTER, pause=0.2)
        check("без группы /team объясняет, чего не хватает", "группа не задана" in log_text(state))

        before = len(fake.calls)
        await send("/profile lead" + ENTER, pause=0.25)
        check("состав группы показан при выборе профиля", "● analyst" in log_text(state) and "● critic" in log_text(state))

        # Разогрев главного экрана перед группой. Без него проверка Σ на экране агента ничего
        # не значит: смена профиля заводит НОВОГО собеседника главного экрана, его расход
        # обнуляется, и к приходу экспертов «весь сеанс» равен нулю — то есть совпадает с
        # расходом самого эксперта. Подмена одного другим при таком совпадении ничем себя не
        # выдаёт, и проверка проходила бы при сломанном правиле (проверено порчей С1).
        #
        # Обмен заводим тем же вызовом, каким его заводит очередь: ввод с клавиатуры при
        # профиле `lead` поднял бы ещё одну группу.
        class РазогревныйКлиент:
            """Расход, заметно отличный от расхода основного подставного клиента: равные
            числа снова слили бы «своё» с «общим» и обесценили проверку."""

            async def stream_chat(self, model, messages, params=None):
                yield api.StreamEvent("content", "разогрев")
                yield api.StreamEvent(
                    "meta",
                    finish_reason="stop",
                    usage={"prompt_tokens": 100, "completion_tokens": 40, "total_tokens": 140},
                )

            async def list_models(self):
                return list(api.FALLBACK_MODELS)

            async def aclose(self):
                pass

        # Меряем ПРИРОСТ, а не итог: к этому месту сценарий уже сменил профиль, и в итоге
        # сеанса законно лежит расход выбывших собеседников. Проверка на круглое число
        # сломалась бы от любой правки выше по сценарию, ничего при этом не поймав.
        до_разогрева = state.session_usage_total()["total_tokens"]
        state.client = РазогревныйКлиент()
        await output.run_turn(state, state.main_agent, "разогрев перед группой", pane=state.main.first)
        state.client = fake
        разогрев = state.session_usage_total()["total_tokens"]
        check(
            "перед группой на главном экране уже потрачено",
            разогрев - до_разогрева == 140,
            f"прирост {разогрев - до_разогрева}",
        )
        # Счёт вызовов подставного клиента ведём с этого места: разогрев шёл мимо него, а
        # проверки ниже разбирают запросы группы поимённо.
        before = len(fake.calls)

        await send("почему так?" + ENTER, pause=0.8)
        check("экраны группы заведены", [s.key for s in state.screens] == ["main", "lead", "lead:summary"], str([s.key for s in state.screens]))
        board = state.screens[1]
        summary_screen = state.screens[2]
        check("у каждого эксперта своя панель", [p.key for p in board.panes] == ["analyst", "critic"], str([p.key for p in board.panes]))
        analyst_pane = board.panes[0]
        check("в панели эксперта его постановка задачи", "● analyst" in log_text(state, analyst_pane) and "ты аналитик" in log_text(state, analyst_pane))
        check("инструкция свёрнута, но доступна целиком", "(/system — целиком)" in log_text(state, analyst_pane))
        check("в панели эксперта его рассуждения и ответ", "прикидываю…" in log_text(state, analyst_pane) and '"status": "ok"' in log_text(state, analyst_pane))
        check("ответ соседа в чужую панель не попадает", "ты критик" not in log_text(state, analyst_pane))
        check("вывод агентов в главный экран не льётся", "ты аналитик" not in log_text(state))
        check("в главном экране сказано, где смотреть", "группа поднята: analyst, critic" in log_text(state))
        check("сводка ведущего — на своей вкладке", "свожу ответы агентов (2)" in log_text(state, summary_screen))

        # Σ на экране агента — расход ЭТОГО эксперта, и только его. У эксперта одна задача и
        # никакой переписки: общий итог сеанса стоял бы в его строке чужим числом, и сравнить
        # экспертов между собой стало бы нечем — у всех одно и то же большое число.
        свой = analyst_pane.agent.session_usage["total_tokens"]
        общий = state.session_usage_total()["total_tokens"]
        ожидание_эксперта = строки_ожидания(analyst_pane)
        check("расход эксперта и расход сеанса — разные числа", свой != разогрев and общий > свой, f"свой {свой}, разогрев {разогрев}, всего {общий}")
        check("в панели эксперта есть замершая строка обмена", len(ожидание_эксперта) == 1, str(ожидание_эксперта))
        check(
            "Σ на экране агента — расход самого агента",
            bool(ожидание_эксперта) and итог_из(ожидание_эксперта[0]) == ui.format_tokens(свой),
            f"{ожидание_эксперта} против {ui.format_tokens(свой)}",
        )
        check(
            "и это не общий итог сеанса",
            bool(ожидание_эксперта) and итог_из(ожидание_эксперта[0]) != ui.format_tokens(общий),
            f"{ожидание_эксперта} против {ui.format_tokens(общий)}",
        )

        # В списке агентов расход помечен знаком итога, а не стрелкой входящих: в колонке
        # стоит весь расход агента, и «↓» врало бы про смысл числа.
        фрагменты_списка = panel_fragments(app)
        check("расход в списке помечен знаком итога", "Σ" in panel_text(app), panel_text(app))
        check(
            "и набран стилем итога, как в строке ожидания",
            any(ф[0] == "class:tokens.sum" for ф in фрагменты_списка),
            str(sorted({ф[0] for ф in фрагменты_списка})),
        )
        check(
            "стрелкой входящих расход больше не помечен",
            f"↓ {ui.format_tokens(свой)}" not in panel_text(app),
            panel_text(app),
        )

        agent_calls = fake.calls[before:]
        systems = [c["messages"][0]["content"] for c in agent_calls]
        check("каждому агенту ушла своя инструкция", "ты аналитик" in systems and "ты критик" in systems, str(systems))
        check("агенты не видели ответов друг друга", all(len(c["messages"]) == 2 for c in agent_calls[:2]))
        check("группа запущена ровно один раз", len(agent_calls) == 3, str(len(agent_calls)))
        check("главная панель не получила отдельного исполнителя", id(state.main.first) not in state.pane_workers)
        summary_call = agent_calls[-1]
        check("ведущему ушли ответы всех агентов", summary_call["messages"][0]["content"] == "сведи ответы" and "Ответ эксперта «critic»" in summary_call["messages"][1]["content"])

        check("список агентов появился", cli.show_agent_panel(state))
        check("в списке видны агенты группы", "analyst" in panel_text(app) and "critic" in panel_text(app), panel_text(app))
        handler = next(f[2] for f in panel_fragments(app) if len(f) == 3 and "analyst" in f[1])
        buffer.text = "черновик главной"
        handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        check("клик по строке списка открывает экран агента", state.active == 1 and state.screen is board)
        check("экран агента показывает главный черновик", buffer.text == "черновик главной", repr(buffer.text))
        # Строка под экраном агента всё равно принадлежит главной панели: экран только для
        # чтения. Набранное при просмотре агента не должно попасть в его панель или исчезнуть.
        buffer.text = "черновик главной, дополненный у агента"
        cli.switch_pane(state, 1)
        check(
            "переключение панелей агента не меняет главный черновик",
            buffer.text == "черновик главной, дополненный у агента",
            repr(buffer.text),
        )
        cli.switch_screen(state, 0)
        check(
            "текст, набранный у агента, пережил возврат на главный экран",
            buffer.text == "черновик главной, дополненный у агента",
            repr(buffer.text),
        )
        buffer.text = ""
        cli.switch_screen(state, 1)
        cli.switch_pane(state, 0)

        # Итог оркестратора возвращается в главный экран: человеку, ведущему разговор, не
        # приходится идти на чужую вкладку и смотреть, чем всё кончилось.
        check("сводка ведущего вернулась в главный экран", "сводка группы «lead»" in log_text(state), log_text(state)[-300:])
        хвост = log_text(state).split("сводка группы «lead»")[-1]
        check("в главном экране лежит сам ответ, а не ссылка на вкладку", '"status": "ok"' in хвост[:200], хвост[:200])
        память = state.main_agent.history()
        check("итог попал и в память главного агента", len(память) == 2 and память[0]["content"] == "почему так?", str(память))
        check("в памяти лежит текст итога", '"status": "ok"' in память[1]["content"], str(память))
        await send("/system" + ENTER, pause=0.25)
        check("/system на панели эксперта показывает его инструкцию", "ты аналитик" in log_text(state, board.panes[0]))
        cli.switch_screen(state, 0)
        check("возврат на главный экран", state.active == 0)

        # А на главном экране Σ — весь сеанс, вместе с уже отработавшими экспертами: человек
        # платит за окно целиком, и итог без экспертов занижал бы счёт ровно в тот день,
        # когда он вырос.
        #
        # Обмен заводим тем же вызовом, каким его заводит очередь (`cli.worker`), а не вводом
        # с клавиатуры: действующий профиль — `lead`, и ввод поднял бы ещё одну группу. Она
        # завела бы второй прогон и сбила бы проверку журнала «вся группа помечена одним
        # прогоном», проверяя при этом не то, ради чего сюда пришли.
        отметка_главного = len(state.main.first.log)
        await output.run_turn(state, state.main_agent, "вопрос после группы", pane=state.main.first)
        главная_строка = строки_ожидания(state.main.first, отметка_главного)
        всего = state.session_usage_total()["total_tokens"]
        свой_главный = state.main_agent.session_usage["total_tokens"]
        check("на главном экране обмен оставил свою строку", len(главная_строка) == 1, str(главная_строка))
        check(
            "Σ на главном экране — весь сеанс",
            bool(главная_строка) and итог_из(главная_строка[0]) == ui.format_tokens(всего),
            f"{главная_строка} против {ui.format_tokens(всего)}",
        )
        check(
            "и расход экспертов в него входит",
            всего > свой_главный,
            f"главный {свой_главный}, всего {всего}",
        )

        records = [json.loads(line) for line in Path(os.environ["MYHARNESS_JOURNAL"]).read_text(encoding="utf-8").splitlines()]
        team_records = [r for r in records if r.get("run_id")]
        check("в журнале отмечено, кто отвечал", {r.get("agent") for r in team_records} == {"analyst", "critic", "lead"}, str([r.get("agent") for r in team_records]))
        check("вся группа помечена одним прогоном", len({r["run_id"] for r in team_records}) == 1)

        print("\n8. Рабочие экраны и мышь")
        (profiles_dir / "step_ask.json").write_text(
            json.dumps(
                {
                    "name": "step_ask",
                    "description": "вставьте логическую задачу",
                    "system": "составь промпт",
                    "prefill": "вставьте логическую задачу",
                    "keep_history": False,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "step_solve.json").write_text(
            json.dumps({"name": "step_solve", "prefill": "вставьте промпт", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "two_steps.json").write_text(
            json.dumps({"name": "two_steps", "screens": ["step_ask", "step_solve"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile two_steps" + ENTER, pause=0.3)
        check("рабочие экраны открыты вместо экранов группы", [s.key for s in state.screens] == ["main", "step_ask", "step_solve"], str([s.key for s in state.screens]))
        check("сразу открыт первый шаг", state.active == 1)
        check("заготовка первого шага в строке ввода", buffer.text == "вставьте логическую задачу", repr(buffer.text))
        check("вводная шага написана в его ленте", "вставьте логическую задачу" in log_text(state, state.screens[1]))

        before = len(fake.calls)
        await send("\x7f" * 40 + "задача про шофёров" + ENTER, pause=0.5)
        step_screen = state.screens[1]
        check("вопрос и ответ остались на своём экране", "задача про шофёров" in log_text(state, step_screen) and '"status": "ok"' in log_text(state, step_screen))
        check("главный экран не тронут", "задача про шофёров" not in log_text(state))
        check("экран остался открытым — не перескочили на главный", state.active == 1)
        check("ушла инструкция этого экрана", fake.calls[before]["messages"][0]["content"] == "составь промпт")

        cli.switch_screen(state, 2)
        check("заготовка второго шага подставилась при переходе", buffer.text == "вставьте промпт", repr(buffer.text))
        buffer.text = "своё, исправленное человеком"
        cli.switch_screen(state, 1)
        check("черновик не перенесён на соседний экран", buffer.text == "", repr(buffer.text))
        cli.switch_screen(state, 2)
        check(
            "исправленный черновик пережил уход и возврат",
            buffer.text == "своё, исправленное человеком",
            repr(buffer.text),
        )
        buffer.text = ""
        cli.switch_screen(state, 1)

        # Поведение изменено осознанно: мышь у harness всегда. Прежний переключатель «или клики,
        # или выделение» опирался на ложный выбор — губило выделение отслеживание перетаскивания
        # (1003h), а не сами клики. Приложение просит у терминала только нажатия, поэтому клики
        # и выделение текста уживаются без команд.
        check("мышь у harness сразу — клики работают без команд", state.mouse_enabled is True)
        check("приложение перехватывает мышь с самого начала", app.mouse_support() is True)
        check("строка состояния молчит, пока всё в порядке", not any("отдана терминалу" in text for text in screen_texts(app)))
        app.key_processor.feed(KeyPress(Keys.F2, "\x1bOQ"))
        app.key_processor.process_keys()
        await asyncio.sleep(0.15)
        check("F2 — аварийный выход: мышь целиком терминалу", state.mouse_enabled is False and app.mouse_support() is False)
        check("о потере кликов сказано в строке состояния", any("отдана терминалу" in text for text in screen_texts(app)))
        await send("/mouse" + ENTER, pause=0.2)
        check("/mouse возвращает мышь harness", state.mouse_enabled is True and app.mouse_support() is True)

        print("\n9. Набор способов: один вопрос — все подходы сразу")
        (profiles_dir / "plain.json").write_text(
            json.dumps({"name": "plain", "system": "отвечай прямо", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "task.json").write_text(
            json.dumps({"name": "task", "methods": ["plain", "two_steps", "lead"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile task" + ENTER, pause=0.3)
        keys = [s.key for s in state.screens]
        check("вкладки способов открыты сразу, до вопроса", keys == ["main", "plain", "two_steps", "lead", "lead:summary"], str(keys))
        check("остались на главном экране — вопрос вводится здесь", state.active == 0)
        check("в главном сказано, что задача уйдёт во все способы", "способы: plain, two_steps, lead" in log_text(state))

        before = len(fake.calls)
        await send("как из рубашки сделать птицу?" + ENTER, pause=1.2)
        plain_screen, chain_screen, board = state.screens[1], state.screens[2], state.screens[3]
        check("простой способ ответил на своей вкладке", '"status": "ok"' in log_text(state, plain_screen))
        check("у цепочки панель на каждый шаг", [p.key for p in chain_screen.panes] == ["step_ask", "step_solve"], str([p.key for p in chain_screen.panes]))
        solve_pane = chain_screen.panes[1]
        check("второй шаг получил ответ первого автоматически", '"status": "ok"' in log_text(state, solve_pane) and "как из рубашки" in log_text(state, solve_pane))
        solve_calls = [c for c in fake.calls[before:] if c["messages"][-1]["content"].count("как из рубашки") == 1 and "{" in c["messages"][-1]["content"]]
        check("в запрос второго шага вошёл текст первого", bool(solve_calls), "промпт первого шага во второй запрос не попал")
        check("у группы панели по экспертам", [p.key for p in board.panes] == ["analyst", "critic"], str([p.key for p in board.panes]))
        check("эксперты отвечали в свои панели", all('"status": "ok"' in log_text(state, pane) for pane in board.panes))
        check(
            "набор способов запущен ровно один раз",
            len(fake.calls[before:]) == 6,
            str(len(fake.calls[before:])),
        )
        check("сводка ведущего на своей вкладке", "свожу ответы агентов" in log_text(state, state.screens[4]))

        # Итоги всех способов возвращаются в главный экран — и в порядке набора, а не в
        # порядке, в каком способы управились: сравнивают их по столбцам набора.
        главный = log_text(state)
        итоги = [
            главный.find("ответ способа «plain»"),
            главный.find("итог цепочки «two_steps»"),
            главный.find("сводка группы «lead»", главный.find("способы: plain")),
        ]
        check("итог каждого способа вернулся в главный экран", all(место > 0 for место in итоги), str(итоги))
        # Предмет работы не теряется: в итоге цепочки есть ответ КАЖДОГО шага, а не только
        # последнего. На дне 6 последним шагом стоял проверяющий, и наверх уезжал вердикт без кода.
        хвост_цепочки = главный.split("итог цепочки «two_steps»")[-1]
        check(
            "в итоге цепочки подписаны оба шага, а не только последний",
            "[step_ask]" in хвост_цепочки[:600] and "[step_solve]" in хвост_цепочки[:600],
            хвост_цепочки[:300],
        )
        check("итоги идут в порядке набора, а не готовности", итоги == sorted(итоги), str(итоги))
        память = state.main_agent.history()
        check("на весь прогон одна пара в памяти, а не по паре на способ", len(память) == 2, str(len(память)))
        подписи = ["[ответ способа «plain»]", "[итог цепочки «two_steps»]", "[сводка группы «lead»]"]
        check(
            "в памяти итоги подписаны, иначе это склейка из трёх ответов",
            all(подпись in память[1]["content"] for подпись in подписи),
            память[1]["content"][:200],
        )

        cli.switch_screen(state, 3)
        check("панель по умолчанию первая", state.screen.active_pane == 0)
        cli.switch_pane(state, 1)
        check("Alt+стрелка переводит на соседнюю панель", state.screen.pane.key == "critic")
        cli.switch_pane(state, 2)
        check("панели перебираются по кругу", state.screen.pane.key == "analyst")
        cli.toggle_zoom(state)
        check("F3 разворачивает панель на весь экран", state.screen.zoomed is True)
        cli.toggle_zoom(state)
        check("и возвращает сетку", state.screen.zoomed is False)
        cli.switch_screen(state, 0)

        print("\n10. Список агентов под строкой ввода")
        # Группа поднимается заново, поверх набора способов: так в списке заведомо есть и
        # отвечавшие агенты, и один не отвечавший — собеседник главного экрана. Смена
        # профиля заводит его заново, поэтому прежние обмены на него не переносятся.
        await send("/profile lead" + ENTER, pause=0.3)
        await send("кто из вас прав?" + ENTER, pause=0.9)
        rows = cli.collect_agents(state)
        # Агентов ровно четыре: собеседник главного экрана, два эксперта и ведущий на сводке.
        check(
            "порядок строк повторяет порядок экранов и панелей",
            [row.agent.name for row in rows] == ["main", "analyst", "critic", "lead:summary"],
            str([row.agent.name for row in rows]),
        )
        строки = [строка for строка in panel_text(app).split("\n") if строка.strip()]
        check("список стоит под строкой ввода без всяких команд", cli.show_agent_panel(state))
        check("над списком строка подсказок", "↑/↓" in строки[0] and "Ctrl+R" in строки[0], строки[0])
        check("главный разговор — первой строкой", строки[1].strip().startswith("○ main") or строки[1].strip().startswith("● main"), строки[1])
        check(
            "у отвечавшего агента посчитан расход",
            # Знак итога, а не стрелка входящих: в колонке весь расход агента. Пометка «↓»
            # означала бы «столько пришло от модели» и врала бы про смысл числа.
            any("Σ 46" in строка and "critic" in строка for строка in строки),
            str(строки),
        )
        check(
            "агент, который ещё не отвечал, показан прочерками",
            next(строка for строка in строки if "main" in строка).count("—") == 2,
            next(строка for строка in строки if "main" in строка),
        )

        # Стрелки водят по строкам списка: тот же путь, что и щелчок мышью.
        cli.switch_screen(state, 0)
        cli.switch_pane(state, 0)
        press(app, Keys.Down, "\x1b[B")
        await asyncio.sleep(0.15)
        check(
            "↓ переводит на следующего агента списка",
            state.screen.key == "lead" and state.screen.pane.key == "analyst",
            f"{state.screen.key} / {state.screen.pane.key}",
        )
        check("закрашен тот, на кого перешли", any(строка.strip().startswith("● analyst") for строка in panel_text(app).split("\n")), panel_text(app))
        press(app, Keys.Up, "\x1b[A")
        await asyncio.sleep(0.15)
        check("↑ возвращает на предыдущего", state.active == 0 and state.screen.key == "main")

        # Меню команд ходит теми же стрелками, и отнимать их у него нельзя.
        await send("/")
        press(app, Keys.Down, "\x1b[B")
        await asyncio.sleep(0.15)
        check("при открытом меню команд стрелка выбирает команду, а не агента", state.active == 0 and buffer.complete_state is not None)
        await send(ESC + "\x7f" * 40)

        # Клик мышью по строке: обработчик висит на самой строке, включая отбивку справа.
        row_handler = next(f[2] for f in panel_fragments(app) if len(f) == 3 and "critic" in f[1])
        row_handler(MouseEvent(position=Point(0, 0), event_type=MouseEventType.MOUSE_UP, button=MouseButton.LEFT, modifiers=frozenset()))
        check(
            "клик по строке переводит на экран и панель агента",
            state.screen.key == "lead" and state.screen.pane.key == "critic",
            f"{state.screen.key} / {state.screen.pane.key}",
        )
        cli.switch_screen(state, 0)

        print("\n11. Цепочка вкладками: каждый шаг на своей вкладке")
        # Раскладка панелями (раздел 9) остаётся умолчанием — здесь профиль цепочки просит
        # «tabs», и те же самые шаги должны разъехаться по отдельным вкладкам, не потеряв
        # передачу работы между собой.
        (profiles_dir / "tab_ask.json").write_text(
            json.dumps(
                {"name": "tab_ask", "title": "постановка", "system": "составь промпт", "keep_history": False},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "tab_solve.json").write_text(
            json.dumps({"name": "tab_solve", "title": "решение", "keep_history": False}, ensure_ascii=False),
            encoding="utf-8",
        )
        (profiles_dir / "two_tabs.json").write_text(
            json.dumps(
                {"name": "two_tabs", "title": "по вкладкам", "screens": ["tab_ask", "tab_solve"], "layout": "tabs"},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "task_tabs.json").write_text(
            json.dumps({"name": "task_tabs", "methods": ["plain", "two_tabs"]}, ensure_ascii=False),
            encoding="utf-8",
        )

        await send("/profile task_tabs" + ENTER, pause=0.3)
        keys = [s.key for s in state.screens]
        check(
            "у цепочки с layout=tabs по вкладке на шаг, ключ несёт имя цепочки",
            keys == ["main", "plain", "two_tabs:tab_ask", "two_tabs:tab_solve"],
            str(keys),
        )
        titles = [s.title for s in state.screens[2:]]
        check("заголовки вкладок различимы и взяты у профилей шагов", titles == ["постановка", "решение"], str(titles))
        check(
            "вкладки шагов только на просмотр — ввод уходит в главный экран",
            all(not s.interactive for s in state.screens[2:]) and state.active == 0,
        )

        before = len(fake.calls)
        await send("как из рубашки сделать птицу?" + ENTER, pause=1.2)
        ask_tab, solve_tab = state.screens[2], state.screens[3]
        check(
            "каждый шаг ответил в свою вкладку",
            '"status": "ok"' in log_text(state, ask_tab) and '"status": "ok"' in log_text(state, solve_tab),
            log_text(state, solve_tab),
        )
        solve_calls = [
            c
            for c in fake.calls[before:]
            if c["messages"][-1]["content"].count("как из рубашки") == 1 and "{" in c["messages"][-1]["content"]
        ]
        check("в запрос второго шага вошёл ответ первого", bool(solve_calls), "ответ первого шага во второй запрос не попал")

        await send("а обратно?" + ENTER, pause=1.2)
        check(
            "повторный вопрос новых вкладок не заводит",
            [s.key for s in state.screens] == keys,
            str([s.key for s in state.screens]),
        )
        cli.switch_screen(state, 0)

        print("\n12. Память между запусками")
        # Сбор фактов на время остального прогона выключен намеренно: архивариус — лишний
        # запрос к подставному клиенту, и он сбил бы счёт вызовов в разделах выше. Здесь его
        # включают обратно и проверяют отдельно.
        cli.switch_screen(state, 0)
        await send("/profile talky" + ENTER, pause=0.3)
        каталог_запуска = Path.cwd()
        файл_разговора = state.store.path if state.store else None
        check("разговор пишется в файл сессии профиля", файл_разговора is not None and файл_разговора.exists(), str(файл_разговора))
        check(
            "файл разговора лежит в каталоге профиля, а не проекта",
            файл_разговора.parent == memory.profile_dir(каталог_запуска, "talky"),
            str(файл_разговора.parent),
        )
        check("права файла разговора 600", oct(файл_разговора.stat().st_mode & 0o777) == "0o600", oct(файл_разговора.stat().st_mode & 0o777))

        # Перезапуск без второго процесса: собираем состояние заново в том же каталоге и с
        # тем же профилем — ровно то, что делает `_main` при следующем запуске.
        сохранённых_пар = len(memory.read_session(файл_разговора, window=0, system_fp="").pairs)
        второй_запуск = cli.State(
            config=Config(api_key="sk-test", model="deepseek-v4-flash", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        check("новое состояние пустое до восстановления", второй_запуск.main_agent.history() == [])
        cli.restore_conversation(второй_запуск)
        поднятое = второй_запуск.main_agent.history()
        check(
            "второй запуск в той же папке поднял разговор",
            len(поднятое) == сохранённых_пар * 2 and поднятое[0]["content"] == "первый вопрос",
            str(поднятое),
        )
        отчёт = log_text(второй_запуск)
        check("в ленте строка отчёта о восстановлении", "восстановлен разговор:" in отчёт, отчёт)
        check("в отчёте оба числа — сколько поднято и сколько сохранено", f"из {сохранённых_пар} сохранённых" in отчёт, отчёт)
        check("в отчёте время последней записи", "последняя " in отчёт, отчёт)
        # Решение пользователя: экран остаётся чистым, память полной. Печатай мы реплики,
        # каждый запуск начинался бы с простыни прошлого разговора.
        check("сами реплики в ленту не печатаются", "первый вопрос" not in отчёт, отчёт)
        check("восстановление продолжает найденную сессию, а не заводит новую", второй_запуск.store.path == файл_разговора, str(второй_запуск.store.path))
        # Восстановление идёт единственным методом пополнения памяти и не пишет на диск:
        # иначе чтение файла тут же удваивало бы его самим собой.
        размер_после_восстановления = файл_разговора.stat().st_size
        cli.restore_conversation(второй_запуск)
        check(
            "восстановление на диск ничего не пишет",
            файл_разговора.stat().st_size == размер_после_восстановления,
            str(файл_разговора.stat().st_size),
        )

        # Чистый запуск не сообщает ничего: разговора не было, и говорить не о чем.
        чистый_каталог = tmp / "чистая-папка"
        чистый_каталог.mkdir()
        прежний_каталог = os.getcwd()
        os.chdir(чистый_каталог)
        чистый_запуск = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        cli.restore_conversation(чистый_запуск)
        check("в новой папке восстанавливать нечего", чистый_запуск.main_agent.history() == [])
        check("и в ленте об этом ни строки", log_text(чистый_запуск) == "", log_text(чистый_запуск))
        check("хранилище всё равно открыто — обмену есть куда писать", чистый_запуск.store is not None)
        os.chdir(прежний_каталог)

        # Правку инструкции между запусками называют вслух: молча продолженный разговор
        # выглядел бы цельным, не будучи им.
        правленый = profiles.load("talky")[0]
        правленый.system = "болтай иначе"
        запуск_с_правкой = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=правленый,
        )
        cli.restore_conversation(запуск_с_правкой)
        check("о правке инструкции сказано", "инструкция профиля изменилась" in log_text(запуск_с_правкой), log_text(запуск_с_правкой))

        # Три места, куда встроена память, поведением из этой проверки не достаются:
        # `_main` и `repl` поднимают настоящее приложение, а проверка собирает состояние
        # сама. Смотрим дерево разбора — тем же приёмом, каким в check_units проверяется
        # единственность точки записи в память.
        def вызовы(имя_функции):
            дерево = ast.parse(Path(cli.__file__).read_text(encoding="utf-8"))
            for узел in ast.walk(дерево):
                if isinstance(узел, (ast.FunctionDef, ast.AsyncFunctionDef)) and узел.name == имя_функции:
                    return {ast.unparse(вызов.func) for вызов in ast.walk(узел) if isinstance(вызов, ast.Call)}
            return set()

        check("запуск поднимает прежний разговор", "restore_conversation" in вызовы("_main"), str(sorted(вызовы("_main"))))
        # В конструкторе состояния восстановлению не место: его зовут проверки напрямую, и
        # любое собранное состояние начало бы читать и писать в каталог состояния человека.
        check("конструктор состояния на диск не ходит", "restore_conversation" not in вызовы("__post_init__"))
        check("смена профиля подхватывает разговор нового профиля", "restore_conversation" in вызовы("switch_profile"))
        check("очередь запросов заводит заход архивариуса", "archivist.start" in вызовы("worker"), str(sorted(вызовы("worker"))))
        check("выход зовёт архивариуса последний раз", "archivist.finish" in вызовы("repl"), str(sorted(вызовы("repl"))))

        # Испорченную строку в файле разговора не проглатываем молча: восстановление
        # продолжается, но человеку сказано, что часть переписки не прочлась.
        (profiles_dir / "битый.json").write_text(
            json.dumps({"name": "битый", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        битая_сессия = memory.new_session(каталог_запуска, "битый")
        хранилище_битой = memory.SessionStore(битая_сессия)
        хранилище_битой.append("user", "целый вопрос")
        хранилище_битой.append("assistant", "целый ответ")
        with битая_сессия.open("a", encoding="utf-8") as файл:
            файл.write("{это не json\n")
        запуск_с_битой = cli.State(
            config=Config(api_key="sk-test", profile="битый", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("битый")[0],
        )
        cli.restore_conversation(запуск_с_битой)
        check("испорченная строка названа вслух", "испорчена" in log_text(запуск_с_битой), log_text(запуск_с_битой))
        check("и остальной разговор всё равно поднят", len(запуск_с_битой.main_agent.history()) == 2, str(запуск_с_битой.main_agent.history()))

        print("\n12a. /clear открывает новую сессию")
        прежний_файл = state.store.path
        пар_до_очистки = len(memory.read_session(прежний_файл, window=0, system_fp="").pairs)
        await send("/clear" + ENTER, pause=0.25)
        check("сказано, что начат новый разговор", "начат новый разговор" in log_text(state), log_text(state)[-200:])
        check("память главного агента пуста", state.main_agent.history() == [])
        # Стереть файл значило бы издевательство наоборот: ошибочный /clear был бы
        # необратим. Файл остаётся, но следующий запуск его уже не поднимет.
        check("прежний файл разговора цел", прежний_файл.exists() and len(memory.read_session(прежний_файл, window=0, system_fp="").pairs) == пар_до_очистки)
        check("пишем уже в новый файл", state.store.path != прежний_файл, str(state.store.path))
        await send("после очистки" + ENTER, pause=0.4)
        запуск_после_очистки = cli.State(
            config=Config(api_key="sk-test", profile="talky", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        cli.restore_conversation(запуск_после_очистки)
        поднятое_после_очистки = запуск_после_очистки.main_agent.history()
        check(
            "перезапуск после /clear поднимает только новый разговор",
            [сообщение["content"] for сообщение in поднятое_после_очистки[:1]] == ["после очистки"],
            str(поднятое_после_очистки),
        )
        check("стёртое обратно не возвращается", not any("первый вопрос" == сообщение["content"] for сообщение in поднятое_после_очистки))

        # Очистка обязана пережить перезапуск ДАЖЕ БЕЗ единого обмена после неё. Заводись
        # файл при первой записи, а не сразу, — очистил, вышел молча, и следующий запуск
        # нашёл бы самым свежим прежний файл и поднял ровно то, что человек стёр.
        # Ведём на своём профиле: пустая сессия, оставленная у `talky`, сбила бы соседние
        # проверки, которым нужен его разговор.
        (profiles_dir / "забывчивый.json").write_text(
            json.dumps({"name": "забывчивый", "system": "болтай", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile забывчивый" + ENTER, pause=0.3)
        await send("сказано до очистки" + ENTER, pause=0.4)
        await send("/clear" + ENTER, pause=0.3)
        check("новый файл разговора заведён сразу, до первой реплики", state.store.path.exists(), str(state.store.path))
        молчаливый_запуск = cli.State(
            config=Config(api_key="sk-test", profile="забывчивый", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("забывчивый")[0],
        )
        cli.restore_conversation(молчаливый_запуск)
        check(
            "очистка без единого обмена переживает перезапуск",
            молчаливый_запуск.main_agent.history() == [],
            str(молчаливый_запуск.main_agent.history()),
        )
        await send("/profile talky" + ENTER, pause=0.3)


        # Смена профиля — переход в другой разговор, потому что ключ пары изменился.
        (profiles_dir / "talky2.json").write_text(
            json.dumps({"name": "talky2", "system": "болтай иначе", "keep_history": True}, ensure_ascii=False),
            encoding="utf-8",
        )
        await send("/profile talky2" + ENTER, pause=0.3)
        check("у другого профиля свой каталог разговоров", state.store.path.parent == memory.profile_dir(каталог_запуска, "talky2"), str(state.store.path.parent))
        check("чужой разговор не подхвачен", state.main_agent.history() == [], str(state.main_agent.history()))
        await send("вопрос второму профилю" + ENTER, pause=0.4)
        # Шапка приложения печатается один раз за сеанс. Она уехала было в смену профиля
        # вместе с правкой порядка печати при запуске, и каждый /profile рисовал рамку заново.
        шапок_до_возврата = log_text(state).count("myharness  ·  DeepSeek API")
        await send("/profile talky" + ENTER, pause=0.35)
        check(
            "смена профиля не печатает шапку заново",
            log_text(state).count("myharness  ·  DeepSeek API") == шапок_до_возврата,
            str((шапок_до_возврата, log_text(state).count("myharness  ·  DeepSeek API"))),
        )
        вернулись = state.main_agent.history()
        check(
            "возврат к прежнему профилю поднимает ЕГО разговор",
            bool(вернулись) and вернулись[0]["content"] == "после очистки",
            str(вернулись),
        )
        check("отчёт о восстановлении показан при смене профиля", "восстановлен разговор:" in log_text(state), log_text(state)[-300:])

        print("\n12b. Команды глобальной памяти")
        for путь in memory.facts_dir().glob("*.md"):
            путь.unlink()
        await send("/memory" + ENTER, pause=0.2)
        check("на пустой памяти сказано, что она пуста", "глобальная память пуста" in log_text(state), log_text(state)[-200:])
        await send("/remember" + ENTER, pause=0.2)
        check("без текста /remember объясняет, чего не хватает", "нужен текст факта" in log_text(state), log_text(state)[-200:])
        check("и панель выбора не открывает — факт пишут словами", state.picker is None)
        await send("/remember зовут Александр" + ENTER, pause=0.2)
        check("факт объявлен в ленте", "запомнил: зовут Александр" in log_text(state), log_text(state)[-200:])
        check("факт лёг в глобальную память", memory.load_facts()[0] == ["зовут Александр"], str(memory.load_facts()))
        await send("/remember любимый цвет — синий" + ENTER, pause=0.2)
        await send("/memory" + ENTER, pause=0.2)
        память_в_ленте = log_text(state)[-400:]
        check("список фактов пронумерован", "1. зовут Александр" in память_в_ленте and "2. любимый цвет — синий" in память_в_ленте, память_в_ленте)
        check("над списком сказано, собираются ли факты", "сбор фактов" in память_в_ленте, память_в_ленте)

        # Факты уезжают в системную инструкцию главного разговора — оттуда «как меня зовут»
        # отвечается в любой папке, даже в новой, где разговора ещё не было.
        before = len(fake.calls)
        await send("как меня зовут?" + ENTER, pause=0.4)
        системное = fake.calls[before]["messages"][0]["content"]
        check("факты ушли в системную инструкцию", "зовут Александр" in системное, системное)
        check("инструкция профиля при этом на месте", "болтай" in системное, системное)

        await send("/forget 1" + ENTER, pause=0.2)
        check("удаление названо вслух", "забыто: зовут Александр" in log_text(state), log_text(state)[-200:])
        check("факт убран, нумерация сдвинулась", memory.load_facts()[0] == ["любимый цвет — синий"], str(memory.load_facts()))
        await send("/forget" + ENTER, pause=0.2)
        check("без номера /forget объясняет, чего не хватает", "нужен номер" in log_text(state), log_text(state)[-200:])
        await send("/forget семь" + ENTER, pause=0.2)
        check("не-номер даёт внятное сообщение, а не падение", "не номер" in log_text(state), log_text(state)[-200:])
        await send("/forget 99" + ENTER, pause=0.2)
        check("промах по номеру объяснён", "нет факта с номером 99" in log_text(state), log_text(state)[-200:])

        # Собеседник, собранный конструктором состояния, обязан знать факты сразу — до
        # первой смены профиля. Проверка выше идёт после `/profile`, и одна она пропустила бы
        # потерю фактов у главного агента при запуске.
        свежее_состояние = cli.State(
            config=Config(api_key="sk-test", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        системное_свежего = свежее_состояние.main_agent.build_messages("привет")[0]["content"]
        check("собеседник главного экрана знает факты с самого запуска", "любимый цвет — синий" in системное_свежего, системное_свежего)

        видимые = [имя for имя, _, _ in ui.visible_commands(True)]
        check("все три команды памяти есть в меню", {"/remember", "/memory", "/forget"} <= set(видимые), str(видимые))
        справка = fragments_text(ui.help_fragments())
        check("и в справке", all(команда in справка for команда in ("/remember", "/memory", "/forget")), справка[:200])

        print("\n12c. Архивариус: раз в пять обменов, фоном")
        for путь in memory.facts_dir().glob("*.md"):
            путь.unlink()
        # Подставной клиент отвечает архивариусу тем же, чем всем: это не JSON со списком
        # фактов, поэтому сам по себе он памяти не пополняет. Здесь проверяется, КОГДА
        # архивариус заводится, а что он делает с ответом — в check_units.
        await send("/memory off" + ENTER, pause=0.2)
        check("выключатель сохранён в настройках инструмента", state.config.remember is False)
        state.since_archive = 0
        before = len(fake.calls)
        for номер in range(archivist.EVERY_N):
            await send(f"вопрос при выключенном сборе {номер}" + ENTER, pause=0.35)
        check("при выключенном сборе архивариус не заводится ни разу", state.архивариус.задача is None)
        check("и лишних запросов не делает", len(fake.calls) - before == archivist.EVERY_N, str(len(fake.calls) - before))

        await send("/memory on" + ENTER, pause=0.2)
        check("сбор включён обратно", state.config.remember is True)
        state.since_archive = 0
        before = len(fake.calls)
        for номер in range(archivist.EVERY_N - 1):
            await send(f"вопрос до срока {номер}" + ENTER, pause=0.35)
        check("до пятого обмена архивариус не заводится", state.архивариус.задача is None, str(state.since_archive))
        await send("пятый вопрос" + ENTER, pause=0.35)
        check("на пятом обмене заход заведён", state.архивариус.задача is not None)
        check("счётчик обнулён — следующий заход не раньше чем через пять", state.since_archive == 0, str(state.since_archive))
        # Ввод не блокируется: строка ввода принимает текст, пока архивариус ходит к модели.
        buffer.text = ""
        await send("набрано, пока архивариус работает")
        check("ввод при работающем архивариусе не заблокирован", buffer.text == "набрано, пока архивариус работает", repr(buffer.text))
        await send("\x7f" * 60)
        check("пока заход идёт, второго не заводим", not archivist.due(state))
        await asyncio.wait({state.архивариус.задача}, timeout=5)
        check("заход завершился", state.архивариус.задача.done())
        # Считаем запросы ПОИМЁННО, по системной инструкции, а не по их общему числу. С
        # появлением сжимателя фоновых запросов стало двое, и они ходят к тому же клиенту:
        # общий счёт ловил бы заход сжимателя как лишний заход архивариуса и обвинял бы не
        # того. Проверяется здесь именно архивариус — что он один на пять обменов.
        запросы = fake.calls[before:]
        архивариусом = [
            вызов for вызов in запросы if вызов["messages"][0]["content"] == archivist.ARCHIVIST_INSTRUCTION
        ]
        check("на пять обменов пришёлся ровно один запрос архивариуса", len(архивариусом) == 1, str(len(архивариусом)))
        разговором = [вызов for вызов in запросы if вызов["messages"][0]["content"] not in (
            archivist.ARCHIVIST_INSTRUCTION,
            compact.ИНСТРУКЦИЯ,
        )]
        check("обменов человека ровно столько, сколько вопросов", len(разговором) == archivist.EVERY_N, str(len(разговором)))
        запрос_архивариуса = архивариусом[0]
        check(
            "архивариус получил кусок разговора со своей инструкцией",
            запрос_архивариуса["messages"][0]["content"] == archivist.ARCHIVIST_INSTRUCTION,
            запрос_архивариуса["messages"][0]["content"][:80],
        )
        check(
            "пока архивариус работал, в строке состояния было сказано",
            "запоминаю…" in fragments_text(ui.status_fragments("m", True, "talky", False, True, True)),
        )
        check(
            "когда не работает — не сказано",
            "запоминаю…" not in fragments_text(ui.status_fragments("m", True, "talky", False, True, False)),
        )
        print("\n12d. Размышления свёрнуты, пока их не развернули")
        # Черновик модели длиннее ответа в разы: показанный целиком, он вытесняет с экрана
        # то, ради чего запрос делался. Число видимых строк сверяем СЧЁТОМ по видимой ленте:
        # сравнение с `line_count - reasoning_lines` было бы повторением определения и
        # проходило бы даже при счётчике, который вообще не растёт.
        # Падение отрисовки prompt_toolkit не роняет приложение: оно печатает след и живёт
        # дальше, поэтому «приложение живо» ничего не доказывает. Ловим сбои цикла событий.
        сбои_отрисовки = []
        asyncio.get_running_loop().set_exception_handler(
            lambda _цикл, контекст: сбои_отрисовки.append(контекст)
        )
        fake.reasoning = "первая мысль\nвторая мысль\nтретья мысль\n"
        панель = state.main.first
        await send("вопрос со свёрнутыми размышлениями" + ENTER, pause=0.4)
        свёрнуто = fragments_text(панель.visible_log())
        check("текста размышлений на экране нет", "вторая мысль" not in свёрнуто, свёрнуто[-200:])
        check("но заголовок области виден", "▸ размышления" in свёрнуто, свёрнуто[-200:])
        check("ответ на месте", '"status": "ok"' in свёрнуто)
        check(
            "под свёрнутым заголовком нет пустой строки",
            "развернуть\nmyharness › " in свёрнуто,
            repr(свёрнуто[-160:]),
        )
        check(
            "счёт видимых строк сходится с тем, что на экране",
            панель.visible_lines() == видимых_строк(панель),
            f"счётчик {панель.visible_lines()}, на экране {видимых_строк(панель)}",
        )
        строк_свёрнуто = панель.visible_lines()

        # Панель, прокрученную вверх, переключение обязано вернуть к живому выводу: объём
        # ленты выше точки просмотра меняется разом на все размышления.
        панель.autoscroll = False
        press(app, Keys.ControlR, "\x12")
        await asyncio.sleep(0.05)
        развёрнуто = fragments_text(панель.visible_log())
        check("Ctrl+R разворачивает", "вторая мысль" in развёрнуто, развёрнуто[-200:])
        check("виден весь черновик, а не его начало", "первая мысль" in развёрнуто and "третья мысль" in развёрнуто)
        check("заголовок области никуда не делся", "▸ размышления" in развёрнуто)
        check("ответ развёрнутыми размышлениями не потерян", '"status": "ok"' in развёрнуто)
        check("переключение вернуло панель к живому выводу", панель.autoscroll is True)
        check(
            "счёт видимых строк сходится и в развёрнутом виде",
            панель.visible_lines() == видимых_строк(панель),
            f"счётчик {панель.visible_lines()}, на экране {видимых_строк(панель)}",
        )
        строк_развёрнуто = панель.visible_lines()
        check(
            "развёрнутых строк на экране больше, чем свёрнутых",
            строк_развёрнуто > строк_свёрнуто,
            f"{строк_свёрнуто} → {строк_развёрнуто}",
        )

        # Путь, на котором расхождение счётчика перестаёт быть косметикой: по положению
        # курсора окно доматывает ленту вниз, и завышение уводит его за последнюю строку.
        панель.autoscroll = False
        press(app, Keys.ControlEnd, "\x1b[1;5F")
        app.invalidate()
        await asyncio.sleep(0.1)
        курсор = pane_control(app, панель)
        check("окно панели найдено в раскладке", курсор is not None)
        check(
            "прокрутка вниз встаёт на последнюю видимую строку",
            курсор is not None and курсор.get_cursor_position().y == видимых_строк(панель),
            f"курсор {курсор.get_cursor_position().y if курсор else '—'}, строк {видимых_строк(панель)}",
        )

        press(app, Keys.ControlR, "\x12")
        await asyncio.sleep(0.05)
        снова_свёрнуто = fragments_text(панель.visible_log())
        check("повторное нажатие снова прячет", "вторая мысль" not in снова_свёрнуто, снова_свёрнуто[-200:])
        check(
            "и счёт видимых строк снова сходится",
            панель.visible_lines() == видимых_строк(панель),
            f"счётчик {панель.visible_lines()}, на экране {видимых_строк(панель)}",
        )
        check(
            "видимых строк снова меньше, чем в развёрнутом виде",
            панель.visible_lines() < строк_развёрнуто,
            f"{строк_развёрнуто} → {панель.visible_lines()}",
        )

        # То же со свёрнутыми размышлениями: здесь курсор и лента расходятся, если счётчик
        # завышен, и окно лезет за последнюю строку.
        панель.autoscroll = False
        press(app, Keys.ControlEnd, "\x1b[1;5F")
        app.invalidate()
        await asyncio.sleep(0.1)
        курсор = pane_control(app, панель)
        check(
            "и со свёрнутыми размышлениями прокрутка встаёт на последнюю видимую строку",
            курсор is not None and курсор.get_cursor_position().y == видимых_строк(панель),
            f"курсор {курсор.get_cursor_position().y if курсор else '—'}, строк {видимых_строк(панель)}",
        )
        check("отрисовка за весь раздел ни разу не упала", not сбои_отрисовки, str(сбои_отрисовки[:1])[:160])
        asyncio.get_running_loop().set_exception_handler(None)
        fake.reasoning = "прикидываю…"

        print("\n12e. Подсказка над списком набирается по ширине окна")
        # Строка не переносится, поэтому не влезшее окно срезало бы посреди слова. Части
        # отбрасываются целиком и с конца — проверяем оба края: широкое окно и очень узкое.
        части = set(ui.PANEL_HINT_PARTS)
        широкая = ui.panel_hint(200)
        check(
            "в широком окне видны все части",
            широкая.split(" · ") == list(ui.PANEL_HINT_PARTS),
            широкая,
        )
        check("клик упомянут там, где для него есть место", "клик" in широкая)
        узкая = ui.panel_hint(80)
        check("при ширине 80 подсказка помещается в строку", len(узкая) + 1 <= 80, f"{len(узкая) + 1} знаков")
        check("и ни одна часть не оборвана", set(узкая.split(" · ")) <= части, узкая)
        check("про Ctrl+R сказано даже в окне на 80 знаков", "Ctrl+R" in узкая, узкая)
        тесная = ui.panel_hint(30)
        check("в очень узком окне остаётся хотя бы одна целая часть", тесная in части, repr(тесная))
        check(
            "в тесном окне отброшено то, о чём догадаются сами",
            "клик" not in тесная and "Ctrl+R" not in тесная,
            тесная,
        )

        print("\n12f. Расход токенов и предел веса запроса")
        # Снимок «до единого обмена» на живом состоянии не снять: к этому месту проверок
        # обменов сделаны десятки. Заводим отдельное состояние — ровно то, что видит человек
        # сразу после запуска в папке, где с прошлого раза уже лежит разговор.
        чистое = cli.State(
            config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.load("talky")[0],
        )
        чистое.main_agent.restore([("прошлый вопрос", "прошлый ответ")])
        cli.cmd_tokens(чистое)
        снимок = log_text(чистое)
        check("до единого обмена итог сеанса нулевой", "сеанс: 0 обменов" in снимок, снимок)
        строка_истории = next((с for с in снимок.splitlines() if "история:" in с), "")
        вес_истории = "".join(з for з in строка_истории.split("история:")[1].split(" в ")[0] if з.isdigit())
        check(
            "а вес истории не нулевой — разговор поднят с диска",
            вес_истории.isdigit() and int(вес_истории) > 0,
            строка_истории,
        )
        check("поднятые пары названы отдельной строкой", "с диска поднято 1 пара" in снимок, снимок)
        check("и сказано, что в итог сеанса они не входят", "в итог сеанса не входят" in снимок, снимок)
        check("окно модели названо вместе с долей", "окно «deepseek-v4-flash»" in снимок and "%" in снимок, снимок)

        # Незнакомая модель: цена, показанная нулём, читается как «бесплатно», и правду
        # человек узнаёт из счёта в конце месяца.
        #
        # Признак ставится не выбором модели, а состоявшимся обменом по неизвестному тарифу:
        # деньги копятся по ходу, тем тарифом, что действовал на обмен. Пересчёт итога по
        # ТЕКУЩЕЙ модели врал бы втрое — расход, сделанный на «pro», дешевел бы от одной лишь
        # команды /model.
        чистое.model = "модель-без-тарифа"
        чистое.session_cost_known = False
        отметка = len(чистое.main.first.log)
        cli.cmd_tokens(чистое)
        без_тарифа = fragments_text(чистое.main.first.log[отметка:])
        check("у модели без тарифа деньги не выдуманы", "тариф неизвестен" in без_тарифа, без_тарифа)
        check("и нулём цена не притворяется", "0.00" not in без_тарифа, без_тарифа)

        отметка = len(state.main.first.log)
        await send("/tokens" + ENTER, pause=0.2)
        живой = fragments_text(state.main.first.log[отметка:])
        строка_сеанса = next((с for с in живой.splitlines() if "сеанс:" in с), "")
        вход_сеанса = "".join(з for з in строка_сеанса.split("вход")[1].split(",")[0] if з.isdigit())
        check(
            "после обменов итог сеанса не нулевой",
            вход_сеанса.isdigit() and int(вход_сеанса) > 0,
            строка_сеанса,
        )
        check("рассуждения в итоге названы отдельно", "из них рассуждения" in живой, живой)

        # Признак несохранённого профиля сбрасываем: к этому месту его подняли параметры
        # из разделов выше, и проверка ниже прошла бы, ничего не проверив.
        state.profile_dirty = False
        await send("/budget 3000" + ENTER, pause=0.2)
        check("предел принят", state.profile.budget_tokens == 3000, str(state.profile.budget_tokens))
        check("и назван вслух", ui.format_exact(3000) in log_text(state)[-400:], log_text(state)[-400:])
        check("профиль помечен изменённым, но не сохранён", state.profile_dirty)
        отметка = len(state.main.first.log)
        await send("/tokens" + ENTER, pause=0.2)
        с_пределом = fragments_text(state.main.first.log[отметка:])
        check("снимок показывает действующий предел", ui.format_exact(3000) in с_пределом, с_пределом)

        отметка = len(state.main.first.log)
        await send("/budget" + ENTER, pause=0.2)
        без_довода = fragments_text(state.main.first.log[отметка:])
        check("без довода показано действующее значение", ui.format_exact(3000) in без_довода, без_довода)

        await send("/budget 0" + ENTER, pause=0.2)
        check("нулём предел выключается", state.profile.budget_tokens == 0)
        check("и это сказано вслух", "предел веса запроса снят" in log_text(state)[-300:], log_text(state)[-300:])

        await send("/budget три тысячи" + ENTER, pause=0.2)
        check("нечисловой довод даёт сообщение, а не падение", "не число токенов" in log_text(state)[-300:], log_text(state)[-300:])
        check("и предел от него не меняется", state.profile.budget_tokens == 0)
        await send("/budget -5" + ENTER, pause=0.2)
        check("отрицательный предел отвергнут", "не бывает отрицательным" in log_text(state)[-300:], log_text(state)[-300:])
        check("и тоже ничего не изменил", state.profile.budget_tokens == 0)

        отметка = len(state.main.first.log)
        await send("/budget 10" + ENTER, pause=0.2)
        тесный = fragments_text(state.main.first.log[отметка:])
        check("о заведомо тесном пределе предупреждают", "меньше веса пустого запроса" in тесный, тесный)
        check("но заданное значение не подменяют своим", state.profile.budget_tokens == 10, str(state.profile.budget_tokens))
        # Возвращаем как было: тесный предел резал бы память у всего, что идёт следом.
        await send("/budget 0" + ENTER, pause=0.2)

        видимые = [имя for имя, _, _ in ui.visible_commands(True)]
        check("обе команды есть в меню", {"/tokens", "/budget"} <= set(видимые), str(видимые))
        справка = fragments_text(ui.help_fragments())
        check("и в справке", all(команда in справка for команда in ("/tokens", "/budget")), справка[:200])

        print("\n12к. Числа не врут: отказ и вес инструкции")

        # Оба изъяна найдены живым прогоном в настоящем терминале, когда 998 проверок были
        # зелёными: числа считались верно по отдельности и врали на экране.

        # 1. Итог сеанса за несостоявшийся обмен не растёт: отвергнутый сервером запрос
        # не оплачен ни на токен, а живой Σ успел прибавить к нему предсказанный вход.
        class ОтказныйКлиент:
            async def stream_chat(self, model, messages, params=None):
                raise RuntimeError("400 - maximum context length")
                yield  # noqa: PLE0101 — делает функцию генератором, до неё не доходит

            async def list_models(self):
                return list(api.FALLBACK_MODELS)

            async def aclose(self):
                pass

        отказный_экран = screens.Screen(key="отказ", title="отказ", profile=profiles.builtin_default())
        state.screens.append(отказный_экран)
        отказный_экран.first.agent.session_usage = tokens.add_usage(
            {}, {"prompt_tokens": 900, "completion_tokens": 100, "total_tokens": 1000}
        )
        state.client = ОтказныйКлиент()
        await output.run_turn(state, отказный_экран.first.agent, "вопрос в пустоту", pane=отказный_экран.first)
        state.client = fake
        строки_отказа = строки_ожидания(отказный_экран.first)
        было = ui.format_tokens(1000)
        check("у отказавшего обмена своя замершая строка", len(строки_отказа) == 1, str(строки_отказа))
        check(
            "итог сеанса за несостоявшийся обмен не вырос",
            bool(строки_отказа) and итог_из(строки_отказа[0]) == было,
            f"{строки_отказа} против {было}",
        )
        state.screens.remove(отказный_экран)

        # 2. Снимок /tokens считает системную инструкцию: профиль дня кладёт в неё документ
        # на миллион токенов, и без этого «занято» выходило смехотворно малым.
        снимок_фрагменты = ui.tokens_report_fragments(
            "deepseek-v4-flash",
            history=100,
            pairs=1,
            overhead=83,
            system=500_000,
            restored=0,
            runs=1,
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            budget=0,
            cost="0.01 ¢",
        )
        снимок_текст = fragments_text(снимок_фрагменты)
        check("вес инструкции назван отдельной строкой", "системная инструкция" in снимок_текст, снимок_текст[:200])
        check("сказано, что /clear её не уберёт", "/clear её не трогает" in снимок_текст, снимок_текст[:300])
        check(
            "занятое окно считает инструкцию",
            ui.format_exact(500_183) in снимок_текст,
            снимок_текст[:200],
        )

        print("\n12и. Вопросы профиля идут отдельными очередями панелей")

        # Три панели намеренно доводим до трёх разных мест очереди. Так одна общая очередь
        # вместо трёх собственных не сможет случайно пройти проверку на одинаковых данных.
        профиль_a = profiles.Profile(name="очередь-a", prefills=["A1", "A2", "A3"])
        профиль_b = profiles.Profile(name="очередь-b", prefills=["B1", "B2", "B3"])
        # Одиночная `prefill` остаётся тем же путём: очередью из одного вопроса, после
        # подстановки которого ожидающий хвост пуст.
        профиль_c = profiles.Profile(name="очередь-c", prefill="C1")
        панели_очередей = [
            screens.Pane(key="queue-a", profile=профиль_a),
            screens.Pane(key="queue-b", profile=профиль_b),
            screens.Pane(key="queue-c", profile=профиль_c),
        ]
        check(
            "списки очередей панелей не разделяют объект",
            len({id(панель.prefill_queue) for панель in панели_очередей}) == 3,
        )
        экран_очередей = screens.Screen(
            key="очереди",
            title="очереди",
            panes=панели_очередей,
            interactive=True,
        )
        state.screens.append(экран_очередей)
        индекс_очередей = len(state.screens) - 1
        cli.switch_screen(state, индекс_очередей)
        панель_a, панель_b, панель_c = панели_очередей
        check("первая заготовка A встала в её черновик", buffer.text == "A1", repr(buffer.text))
        check("у A ждут следующие две", панель_a.prefill_queue == ["A2", "A3"], str(панель_a.prefill_queue))

        cli.switch_pane(state, 1)
        check("первая заготовка B независима от A", buffer.text == "B1", repr(buffer.text))
        buffer.text = "B1, исправленный человеком"
        cli.switch_pane(state, 2)
        check("одиночная заготовка подставлена", buffer.text == "C1", repr(buffer.text))
        check(
            "одиночная заготовка прошла очередь длиной один",
            панель_c.prefill_initialized and панель_c.prefill_queue == [],
            str(панель_c.prefill_queue),
        )
        cli.switch_pane(state, 1)
        check(
            "исправленный черновик панели пережил уход и возврат",
            buffer.text == "B1, исправленный человеком",
            repr(buffer.text),
        )

        cli.switch_pane(state, 0)
        # Настоящий обработчик Enter запоминает A синхронно, но обработка текста начнётся
        # задачей позже. До передачи управления переходим на B: выбор открытой панели уже
        # не вправе поменять адрес очереди отправленного вопроса.
        press(app, Keys.ControlM, "\r")
        cli.switch_pane(state, 1)
        buffer.text = ""
        await asyncio.sleep(0.15)
        check("Enter на A не сдвинул очередь B", панель_b.prefill_queue == ["B2", "B3"], str(панель_b.prefill_queue))
        check("чужая очередь не подставилась в открытую B", buffer.text == "", repr(buffer.text))
        check(
            "три панели стоят на разных местах очередей",
            [len(панель.prefill_queue) for панель in панели_очередей] == [1, 2, 0],
            str([панель.prefill_queue for панель in панели_очередей]),
        )
        cli.switch_pane(state, 0)
        check("Enter с первой A подготовил вторую A", buffer.text == "A2", repr(buffer.text))
        buffer.text = ""
        cli.switch_screen(state, 0)
        state.screens.remove(экран_очередей)

        print("\n12о. Независимые последовательные исполнители панелей")
        управляемый = УправляемыйКлиент()
        state.client = управляемый
        # Фоновые службы здесь не проверяются и не должны добавлять свои запросы в
        # управляемый обмен панелей.
        state.config.remember = False
        state.since_archive = 0
        панели_исполнителей = [
            screens.Pane(key="worker-a", profile=profiles.Profile(name="worker-a")),
            screens.Pane(key="worker-b", profile=profiles.Profile(name="worker-b")),
            screens.Pane(key="worker-c", profile=profiles.Profile(name="worker-c")),
        ]
        экран_исполнителей = screens.Screen(
            key="исполнители",
            title="исполнители",
            panes=панели_исполнителей,
            interactive=True,
        )
        state.screens.append(экран_исполнителей)
        индекс_исполнителей = len(state.screens) - 1

        def отправить_в_панель(index, text):
            cli.switch_screen(state, индекс_исполнителей)
            cli.switch_pane(state, index)
            buffer.text = text
            press(app, Keys.ControlM, "\r")

        отметки = {id(панель): len(панель.log) for панель in панели_исполнителей}
        отправить_в_панель(0, "первый-a")
        # Переключение сделано в тот же оборот цикла, до запуска асинхронной обработки Enter.
        cli.switch_pane(state, 1)
        отправить_в_панель(1, "первый-b")
        cli.switch_pane(state, 2)
        отправить_в_панель(2, "первый-c")
        cli.switch_pane(state, 0)
        await asyncio.gather(
            *(управляемый.wait_started(text) for text in ("первый-a", "первый-b", "первый-c"))
        )
        исполнители = [state.pane_workers[id(панель)] for панель in панели_исполнителей]
        check("у каждой панели свой исполнитель", len({id(item) for item in исполнители}) == 3)
        check("три панели начали обмены до первого ответа", all(item.busy for item in исполнители))

        # C отпускаем первой. A и B должны остаться удержанными, а её результат — попасть
        # в захваченную C, хотя открыта уже A.
        управляемый.release("первый-c")
        await исполнители[2].queue.join()
        check("первая завершилась только отпущенная C", "ответ:первый-c" in log_text(state, панели_исполнителей[2]))
        check(
            "удержанные ответы A и B не появились раньше времени",
            "ответ:первый-a" not in log_text(state, панели_исполнителей[0])
            and "ответ:первый-b" not in log_text(state, панели_исполнителей[1]),
        )
        управляемый.release("первый-a")
        управляемый.release("первый-b")
        await asyncio.gather(*(item.queue.join() for item in исполнители[:2]))
        for index, name in enumerate(("первый-a", "первый-b", "первый-c")):
            свой_текст = log_text(state, панели_исполнителей[index])
            чужие = [other for other in ("первый-a", "первый-b", "первый-c") if other != name]
            check(
                f"ответ {name[-1].upper()} адресован исходной панели",
                f"ответ:{name}" in свой_текст and all(f"ответ:{other}" not in свой_текст for other in чужие),
                свой_текст[-200:],
            )
            check(
                f"строка ожидания {name[-1].upper()} осталась в исходной панели",
                len(строки_ожидания(панели_исполнителей[index], отметки[id(панели_исполнителей[index])])) == 1,
            )

        # Два Enter подряд в A попадают в одну очередь. Второй сетевой обмен не имеет права
        # начаться, пока первый удерживается.
        отправить_в_панель(0, "порядок-a-1")
        отправить_в_панель(0, "порядок-a-2")
        await управляемый.wait_started("порядок-a-1")
        await asyncio.sleep(0)
        check(
            "второй вопрос A ждёт завершения первого",
            not управляемый._event(управляемый.started, "порядок-a-2").is_set(),
        )
        управляемый.release("порядок-a-1")
        await управляемый.wait_started("порядок-a-2")
        check(
            "второй обмен A начался после ответа первого",
            "ответ:порядок-a-1" in log_text(state, панели_исполнителей[0]),
        )
        управляемый.release("порядок-a-2")
        await исполнители[0].queue.join()
        текст_a = log_text(state, панели_исполнителей[0])
        check(
            "два ответа A завершились в порядке отправки",
            0 <= текст_a.find("ответ:порядок-a-1") < текст_a.find("ответ:порядок-a-2"),
            текст_a[-300:],
        )

        # B и C работают одновременно. Открываем B и жмём настоящий Ctrl+C: соседняя C
        # остаётся живой и получает свой ответ после отдельного разрешения.
        отправить_в_панель(1, "отмена-b")
        отправить_в_панель(2, "сосед-c")
        await asyncio.gather(
            управляемый.wait_started("отмена-b"),
            управляемый.wait_started("сосед-c"),
        )
        cli.switch_pane(state, 1)
        press(app, Keys.ControlC, "\x03")
        await управляемый.wait_cancelled("отмена-b")
        управляемый.release("сосед-c")
        await asyncio.gather(исполнители[1].queue.join(), исполнители[2].queue.join())
        check(
            "Ctrl+C отменил только открытую B",
            "запрос отменён" in log_text(state, панели_исполнителей[1])
            and "ответ:отмена-b" not in log_text(state, панели_исполнителей[1]),
        )
        check(
            "соседняя C завершилась после отмены B",
            "ответ:сосед-c" in log_text(state, панели_исполнителей[2])
            and "запрос отменён" not in log_text(state, панели_исполнителей[2]),
        )
        check(
            "состояния отмены и успеха принадлежат своим панелям",
            панели_исполнителей[1].status == screens.ERROR
            and панели_исполнителей[2].status == screens.DONE,
            str([панель.status for панель in панели_исполнителей]),
        )
        state.client = fake
        cli.switch_screen(state, 0)
        state.screens.remove(экран_исполнителей)

        print("\n12п. Экраны стратегий, факты и ветви")
        (profiles_dir / "strategy-ui.json").write_text(
            json.dumps(
                {
                    "name": "strategy-ui",
                    "system": "единая инструкция стратегий",
                    "keep_history": True,
                    "compact_at": 0,
                    "branch_prefills": {
                        "A": ["A1", "A2"],
                        "B": ["B1", "B2"],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        вызовов_до_профиля = len(fake.calls)
        await cli.switch_profile(state, "strategy-ui")
        check(
            "открытие исходного профиля не делает сетевой запрос",
            len(fake.calls) == вызовов_до_профиля,
        )

        async def отправить_стратегии(screen, pane_index, text):
            cli.switch_screen(state, state.screens.index(screen))
            cli.switch_pane(state, pane_index)
            pane = screen.panes[pane_index]
            было_прогонов = pane.agent.runs
            buffer.text = text
            press(app, Keys.ControlM, "\r")
            while pane.agent.runs == было_прогонов:
                await asyncio.sleep(0)
            await state.pane_workers[id(pane)].queue.join()

        def вызов_с_вопросом(client, text):
            return next(
                call
                for call in reversed(client.calls)
                if call["messages"][-1]["content"] == text
            )

        cli.cmd_strategy(state, "use sliding")
        sliding_screen = state.screen
        sliding_pane = sliding_screen.first
        sliding_agent = sliding_pane.agent
        await отправить_стратегии(sliding_screen, 0, "sliding-one")
        sliding_request = вызов_с_вопросом(fake, "sliding-one")
        check(
            "первый Sliding Window получил только свой вопрос",
            [message["content"] for message in sliding_request["messages"] if message["role"] == "user"]
            == ["sliding-one"],
            str(sliding_request["messages"]),
        )
        buffer.text = "черновик Sliding"
        sliding_history = sliding_agent.history()
        main_screen = state.main
        main_agent = state.main_agent
        cli.cmd_strategy(state, "use standard")
        standard_window = state.profile.strategy_window
        cli.cmd_strategy(state, "window 9")
        check(
            "standard остаётся прежним main, а строгое окно к нему неприменимо",
            state.screen is main_screen
            and state.main_agent is main_agent
            and state.profile.strategy_window == standard_window,
        )
        дочерний_профиль_стратегии = profiles.Profile(
            name="strategy-child",
            system="инструкция дочернего профиля",
            context_strategy="sliding",
            keep_history=True,
            compact_at=0,
            prefills=["дочерняя заготовка", "дочерняя очередь"],
        )
        дочерний_экран_стратегии = screens.Screen(
            key="strategy-child-source",
            title="дочерний",
            profile=дочерний_профиль_стратегии,
            interactive=True,
        )
        state.screens.append(дочерний_экран_стратегии)
        cli.switch_screen(
            state, state.screens.index(дочерний_экран_стратегии)
        )
        дочерний_agent = дочерний_экран_стратегии.first.agent
        дочерний_agent.remember(
            "память дочернего профиля",
            "ответ дочернего профиля",
            model=state.model,
        )
        buffer.text = "черновик дочернего профиля"
        cli.cmd_strategy(state, "use standard")
        дочерний_standard = state.screen
        check(
            "/strategy use standard дочерней панели сохраняет отдельный Agent её профиля",
            дочерний_standard is not main_screen
            and дочерний_standard.strategy_identity
            == ("strategy-child", "standard")
            and дочерний_standard.first.agent is not main_agent
            and дочерний_standard.first.agent is not дочерний_agent
            and дочерний_standard.profile.name == "strategy-child"
            and дочерний_standard.profile.system
            == "инструкция дочернего профиля"
            and дочерний_standard.profile.context_strategy == "standard"
            and дочерний_standard.first.agent.history() == []
            and дочерний_agent.history()[0]["content"]
            == "память дочернего профиля"
            and дочерний_экран_стратегии.first.draft
            == "черновик дочернего профиля"
            and дочерний_standard.first.draft == "дочерняя заготовка"
            and дочерний_standard.first.prefill_queue
            == ["дочерняя очередь"]
            and дочерний_standard.first.prefill_queue
            is not дочерний_экран_стратегии.first.prefill_queue,
            str(
                (
                    дочерний_standard.strategy_identity,
                    дочерний_standard.profile,
                    дочерний_standard.first.agent.history(),
                )
            ),
        )
        cli.switch_screen(state, 0)
        cli.cmd_strategy(state, "use sliding")
        cli.cmd_strategy(state, "")
        check(
            "/strategy показывает режим и окно и открывает выбор",
            state.picker is not None
            and "стратегия: sliding" in log_text(state, sliding_pane)
            and "строгое окно" in log_text(state, sliding_pane),
        )
        state.picker = None
        отметка_чужих_фактов = len(sliding_pane.log)
        cli.cmd_facts(state, "")
        check(
            "/facts на Sliding Window отказала с причиной",
            "только в Sticky Facts"
            in fragments_text(sliding_pane.log[отметка_чужих_фактов:]),
        )

        cli.cmd_strategy(state, "use facts")
        facts_screen = state.screen
        facts_pane = facts_screen.first
        facts_agent = facts_pane.agent
        check(
            "переключение создало другой Agent и сохранило Sliding",
            facts_agent is not sliding_agent and sliding_agent.history() == sliding_history,
        )
        отметка_пустых = len(facts_pane.log)
        cli.cmd_facts(state, "")
        check(
            "пустой словарь Sticky Facts назван с редакцией",
            "редакция 0" in fragments_text(facts_pane.log[отметка_пустых:])
            and "пусты" in fragments_text(facts_pane.log[отметка_пустых:]),
        )
        cli.cmd_facts(state, "set beta второе")
        cli.cmd_facts(state, "set alpha первое")
        cli.cmd_facts(state, "set alpha исправленное")
        отметка_фактов = len(facts_pane.log)
        cli.cmd_facts(state, "")
        показ_фактов = fragments_text(facts_pane.log[отметка_фактов:])
        check(
            "ручные факты показаны в устойчивом порядке с редакцией",
            "редакция 3" in показ_фактов
            and 0 <= показ_фактов.find("alpha → исправленное") < показ_фактов.find("beta → второе"),
            показ_фактов,
        )
        файл_фактов = facts_screen.session_store.path.with_suffix(
            memory.РАСШИРЕНИЕ_ФАКТОВ_РАЗГОВОРА
        )
        журнал_до_отказа = файл_фактов.read_bytes()
        редакция_до_отказа = facts_agent.conversation_facts()[1]
        cli.cmd_facts(state, "forget missing")
        check(
            "отсутствующий ключ отклонён без записи операции",
            facts_agent.conversation_facts()[1] == редакция_до_отказа
            and файл_фактов.read_bytes() == журнал_до_отказа
            and "нет ключа «missing»" in log_text(state, facts_pane),
        )
        cli.cmd_facts(state, "forget beta")
        check(
            "ручное удаление стало новой редакцией",
            facts_agent.conversation_facts() == ({"alpha": "исправленное"}, 4),
            str(facts_agent.conversation_facts()),
        )

        class ПозднийИзвлекатель:
            """Основной ответ готов, но извлечение фактов остаётся в работе."""

            def __init__(self):
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def stream_chat(self, model, messages, params=None):
                system = messages[0]["content"] if messages else ""
                if "обновляешь словарь фактов" in system:
                    self.started.set()
                    await self.release.wait()
                    yield api.StreamEvent(
                        "content",
                        '{"set":{"late":"extractor"},"forget":["manual"]}',
                    )
                else:
                    yield api.StreamEvent("content", "основной ответ готов")
                yield api.StreamEvent(
                    "meta",
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                )

            async def aclose(self):
                pass

        поздний = ПозднийИзвлекатель()
        state.client = поздний
        await cli.handle_submit(
            "позднее извлечение",
            state,
            destination_screen=facts_screen,
            destination_pane=facts_pane,
        )
        await поздний.started.wait()
        facts_worker = state.pane_workers[id(facts_pane)]
        редакция_до_гонки = facts_agent.conversation_facts()[1]
        отметка_гонки = len(facts_pane.log)
        cli.cmd_facts(state, "set manual нельзя")
        check(
            "ручная правка фактов явно отклонена, пока извлекатель занят",
            facts_worker.busy
            and facts_agent.conversation_facts()[1] == редакция_до_гонки
            and "панель выполняет или ожидает обмен"
            in fragments_text(facts_pane.log[отметка_гонки:]),
        )
        поздний.release.set()
        await facts_worker.queue.join()
        check(
            "поздний извлекатель применил свой итог без гонки с ручной правкой",
            facts_agent.conversation_facts()[0].get("late") == "extractor"
            and "manual" not in facts_agent.conversation_facts()[0],
            str(facts_agent.conversation_facts()),
        )
        state.client = fake
        await отправить_стратегии(facts_screen, 0, "facts-one")
        facts_request = вызов_с_вопросом(fake, "facts-one")
        check(
            "основной запрос Sticky Facts видит ручную редакцию",
            "alpha" in facts_request["messages"][0]["content"]
            and "исправленное" in facts_request["messages"][0]["content"],
            str(facts_request["messages"]),
        )

        class КлиентРасходаFacts:
            def __init__(self, extractor_usage, extractor_text='{"set":{},"forget":[]}'):
                self.extractor_usage = extractor_usage
                self.extractor_text = extractor_text

            async def stream_chat(self, model, messages, params=None):
                system = messages[0]["content"] if messages else ""
                if "обновляешь словарь фактов" in system:
                    yield api.StreamEvent("content", self.extractor_text)
                    yield api.StreamEvent(
                        "meta",
                        finish_reason="stop",
                        usage=self.extractor_usage,
                    )
                    return
                yield api.StreamEvent("content", "ответ основного обмена сохранён")
                yield api.StreamEvent(
                    "meta",
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 60,
                        "completion_tokens": 40,
                        "total_tokens": 100,
                    },
                )

            async def aclose(self):
                pass

        редакция_перед_точным = facts_agent.conversation_facts()[1]
        расход_до_agent = facts_agent.session_usage["total_tokens"]
        расход_до_сеанса = state.session_usage_total()["total_tokens"]
        отметка_точного_расхода = len(facts_pane.log)
        state.client = КлиентРасходаFacts(
            {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            }
        )
        await отправить_стратегии(facts_screen, 0, "точный расход facts")
        точный_вывод = fragments_text(
            facts_pane.log[отметка_точного_расхода:]
        )
        точные_строки_ожидания = строки_ожидания(
            facts_pane, отметка_точного_расхода
        )
        ожидаемый_итог_стратегии = ui.format_tokens(расход_до_agent + 130)
        ошибочный_двойной_итог = ui.format_tokens(расход_до_agent + 230)
        check(
            "основной ответ Sticky Facts сохранён при отдельном выводе расхода",
            "ответ основного обмена сохранён" in точный_вывод,
            точный_вывод,
        )
        check(
            "100 основных и 30 извлекателя дали 130 ровно один раз",
            f"редакция Sticky Facts: {редакция_перед_точным} → "
            f"{редакция_перед_точным + 1}" in точный_вывод
            and "основной обмен 100" in точный_вывод
            and "извлекатель 30" in точный_вывод
            and точный_вывод.count("расход стратегии 130") == 1
            and len(точные_строки_ожидания) == 1
            and итог_из(точные_строки_ожидания[0])
            == ожидаемый_итог_стратегии
            and итог_из(точные_строки_ожидания[0])
            != ошибочный_двойной_итог
            and facts_agent.session_usage["total_tokens"] - расход_до_agent
            == 130
            and state.session_usage_total()["total_tokens"]
            - расход_до_сеанса
            == 130,
            точный_вывод,
        )

        отметка_ошибки_facts = len(facts_pane.log)
        state.client = КлиентРасходаFacts(
            {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
            extractor_text="не json",
        )
        await отправить_стратегии(facts_screen, 0, "ошибка извлечения facts")
        вывод_ошибки_facts = fragments_text(
            facts_pane.log[отметка_ошибки_facts:]
        )
        check(
            "facts_error виден, но основной ответ не потерян",
            "Sticky Facts не обновлены" in вывод_ошибки_facts
            and "ответ основного обмена сохранён" in вывод_ошибки_facts,
            вывод_ошибки_facts,
        )

        отметка_неизвестного_facts = len(facts_pane.log)
        state.client = КлиентРасходаFacts({})
        await отправить_стратегии(facts_screen, 0, "неизвестный расход facts")
        вывод_неизвестного_facts = fragments_text(
            facts_pane.log[отметка_неизвестного_facts:]
        )
        check(
            "неизвестный usage извлекателя не стал нулём",
            "извлекатель неизвестен" in вывод_неизвестного_facts
            and "расход стратегии неизвестен" in вывод_неизвестного_facts,
            вывод_неизвестного_facts,
        )

        cli.switch_screen(state, state.screens.index(facts_screen))
        state.main_agent.profile.system = "система main " * 7
        facts_agent.profile.system = "система активного facts " * 13
        state.main_agent.remember(
            "история только main " * 11,
            "ответ только main " * 11,
            model=state.model,
        )
        cli.cmd_facts(
            state,
            "set вес_активной_панели " + "отдельный факт facts " * 17,
        )
        история_main = state.main_agent.history_tokens()
        история_facts = facts_agent.history_tokens()
        система_main = tokens.count_text(state.main_agent.system_text())
        система_facts = tokens.count_text(facts_agent.system_text())
        следующий_main = (
            история_main
            + система_main
            + state.main_agent.overhead(state.model)
        )
        следующий_facts = (
            история_facts
            + система_facts
            + facts_agent.overhead(state.model)
        )
        отметка_активного_расхода = len(facts_pane.log)
        cli.cmd_tokens(state)
        вывод_активного_расхода = fragments_text(
            facts_pane.log[отметка_активного_расхода:]
        )
        строка_окна_активного = next(
            (
                line
                for line in вывод_активного_расхода.splitlines()
                if "следующий запрос займёт" in line
            ),
            "",
        )
        строка_истории_активного = next(
            (
                line
                for line in вывод_активного_расхода.splitlines()
                if "история:" in line
            ),
            "",
        )
        строка_системы_активного = next(
            (
                line
                for line in вывод_активного_расхода.splitlines()
                if "системная инструкция:" in line
            ),
            "",
        )
        check(
            "/tokens показывает вес активного Sticky Facts Agent вместо main",
            история_facts != история_main
            and система_facts != система_main
            and следующий_facts != следующий_main
            and f"активный Agent «{facts_agent.name}» · Sticky Facts"
            in вывод_активного_расхода
            and f"займёт {ui.format_exact(следующий_facts)} "
            in строка_окна_активного
            and f"займёт {ui.format_exact(следующий_main)} "
            not in строка_окна_активного
            and f"история: {ui.format_exact(история_facts)} "
            in строка_истории_активного
            and f"история: {ui.format_exact(история_main)} "
            not in строка_истории_активного
            and f"системная инструкция: {ui.format_exact(система_facts)} "
            in строка_системы_активного
            and f"системная инструкция: {ui.format_exact(система_main)} "
            not in строка_системы_активного
            and "полный серверный расход неизвестен"
            in вывод_активного_расхода,
            вывод_активного_расхода,
        )

        основной_прогноз = output._predict_outgoing(
            state, state.main_agent, "проверка предупреждения"
        )
        прогноз_facts = output._predict_outgoing(
            state, facts_agent, "проверка предупреждения"
        )
        check(
            "вес активного Sticky Facts учитывает его факты",
            прогноз_facts > основной_прогноз,
            f"main={основной_прогноз}, facts={прогноз_facts}",
        )
        прежнее_окно_модели = tokens.CONTEXT_WINDOW
        отметка_предупреждения = len(facts_pane.log)
        try:
            tokens.CONTEXT_WINDOW = основной_прогноз
            state.client = КлиентРасходаFacts(
                {
                    "prompt_tokens": 20,
                    "completion_tokens": 10,
                    "total_tokens": 30,
                }
            )
            await отправить_стратегии(
                facts_screen, 0, "проверка предупреждения"
            )
        finally:
            tokens.CONTEXT_WINDOW = прежнее_окно_модели
        check(
            "предупреждение переполнения относится к активной панели с её facts",
            "запрос не влезет в окно модели"
            in fragments_text(facts_pane.log[отметка_предупреждения:]),
            fragments_text(facts_pane.log[отметка_предупреждения:]),
        )
        state.client = fake

        cli.cmd_strategy(state, "use sliding")
        check(
            "/strategy use вернул тот же экран, Agent, историю и черновик",
            state.screen is sliding_screen
            and state.screen.first.agent is sliding_agent
            and sliding_agent.history() == sliding_history
            and buffer.text == "черновик Sliding",
            repr(buffer.text),
        )
        старое_окно = sliding_screen.profile.strategy_window
        for bad_window in ("0", "-1", "1.5", "четыре", "4 лишнее"):
            cli.cmd_strategy(state, f"window {bad_window}")
        check(
            "неположительные, дробные, нетекстовые и лишние значения окна отвергнуты",
            sliding_screen.profile.strategy_window == старое_окно,
            str(sliding_screen.profile.strategy_window),
        )
        state.profile_dirty = False
        cli.cmd_strategy(state, "window 2")
        check(
            "положительное окно меняет только активный профиль и помечает его",
            sliding_screen.profile.strategy_window == 2
            and facts_screen.profile.strategy_window == старое_окно
            and state.profile_dirty,
        )
        await отправить_стратегии(sliding_screen, 0, "sliding-two")
        await отправить_стратегии(sliding_screen, 0, "sliding-three")
        отметка_строгого_окна = len(sliding_pane.log)
        await отправить_стратегии(sliding_screen, 0, "sliding-four")
        вывод_строгого_окна = fragments_text(
            sliding_pane.log[отметка_строгого_окна:]
        )
        check(
            "завершённый Sliding показывает фактический выбор и сохранённый хвост",
            "режим: Sliding Window; выбрано 2 пары" in вывод_строгого_окна
            and "сохранено, но не отправлено: 1 пара"
            in вывод_строгого_окна
            and "забыт" not in вывод_строгого_окна,
            вывод_строгого_окна,
        )
        await cli.cmd_profile(state, "save")
        saved_strategy = json.loads(
            (profiles.user_profiles_dir() / "strategy-ui.json").read_text(encoding="utf-8")
        )
        check(
            "/profile save сохранил выбранную стратегию и её окно",
            saved_strategy["context_strategy"] == "sliding"
            and saved_strategy["strategy_window"] == 2,
            str(saved_strategy),
        )

        cli.cmd_strategy(state, "use branching")
        branch_screen = state.screen
        branch_store = branch_screen.branch_store
        окно_branching = branch_screen.profile.strategy_window
        cli.cmd_strategy(state, "window 9")
        check(
            "строгое окно к Branching неприменимо",
            branch_screen.profile.strategy_window == окно_branching,
        )
        branch_root = branch_screen.first.agent
        отметка_раннего_split = len(branch_screen.first.log)
        await cli.cmd_branch(state, "split A B")
        check(
            "разделение до первой пары отклонено без изменения графа",
            branch_store.load().checkpoint_id is None
            and len(branch_screen.panes) == 1
            and "после завершённой пары"
            in fragments_text(branch_screen.first.log[отметка_раннего_split:]),
        )
        await отправить_стратегии(branch_screen, 0, "общая точка")
        branch_root_pane_id = id(branch_screen.first)
        общий_путь = branch_root.history()
        расход_корня = branch_root.session_usage["total_tokens"]
        расход_выбывших = state.retired_usage["total_tokens"]
        прогонов_выбывших = state.retired_runs
        await cli.cmd_branch(state, "split A B")
        branch_a, branch_b = branch_screen.panes
        check(
            "родитель заменён двумя ветвями с разными Agent",
            [pane.key for pane in branch_screen.panes] == ["A", "B"]
            and branch_a.agent is not branch_b.agent
            and branch_a.agent is not branch_root
            and branch_b.agent is not branch_root
            and branch_root_pane_id not in state.pane_workers,
        )
        check(
            "обе ветви получили одно общее прошлое",
            branch_a.agent.history() == общий_путь
            and branch_b.agent.history() == общий_путь,
            str([branch_a.agent.history(), branch_b.agent.history()]),
        )
        check(
            "расход и обмен родителя учтены в сеансе ровно один раз",
            state.retired_usage["total_tokens"]
            == расход_выбывших + расход_корня
            and state.retired_runs
            == прогонов_выбывших + branch_root.runs,
            f"{state.retired_usage}; runs={state.retired_runs}",
        )
        отметка_расхода_после_split = len(branch_a.log)
        cli.cmd_tokens(state)
        расход_после_split = fragments_text(
            branch_a.log[отметка_расхода_после_split:]
        )
        check(
            "/tokens сохраняет расход завершённого корня ветвей ровно один раз",
            f"сеанс: {state.retired_runs + sum(agent.runs for agent in state.agents())} "
            in расход_после_split
            and f"всего {ui.format_exact(state.session_usage_total()['total_tokens'])}"
            in расход_после_split,
            расход_после_split,
        )
        check(
            "заготовки ветвей назначены своим панелям",
            branch_a.draft == "A1"
            and branch_a.prefill_queue == ["A2"]
            and branch_b.draft == "B1"
            and branch_b.prefill_queue == ["B2"]
            and branch_a.prefill_queue is not branch_b.prefill_queue,
            str([(pane.draft, pane.prefill_queue) for pane in branch_screen.panes]),
        )
        buffer.text = "черновик A"
        await cli.cmd_branch(state, "B")
        check("переход к B открыл её собственную заготовку", buffer.text == "B1", repr(buffer.text))
        buffer.text = "черновик B"
        await cli.cmd_branch(state, "A")
        check("возврат к A восстановил её черновик", buffer.text == "черновик A", repr(buffer.text))
        await cli.cmd_branch(state, "B")
        check("возврат к B восстановил её черновик", buffer.text == "черновик B", repr(buffer.text))

        await отправить_стратегии(branch_screen, 0, "только A")
        check(
            "отправка в A продвинула только очередь и черновик A",
            branch_a.draft == "A2"
            and branch_a.prefill_queue == []
            and branch_b.draft == "черновик B"
            and branch_b.prefill_queue == ["B2"],
            str([(pane.draft, pane.prefill_queue) for pane in branch_screen.panes]),
        )
        await отправить_стратегии(branch_screen, 1, "только B")
        check(
            "отправка в B продвинула только очередь и черновик B",
            branch_b.draft == "B2"
            and branch_b.prefill_queue == []
            and branch_a.draft == "A2"
            and branch_a.prefill_queue == [],
            str([(pane.draft, pane.prefill_queue) for pane in branch_screen.panes]),
        )
        отметка_ветви = len(branch_a.log)
        await отправить_стратегии(branch_screen, 0, "следующий A")
        request_a = вызов_с_вопросом(fake, "следующий A")
        request_b = вызов_с_вопросом(fake, "только B")
        request_a_text = [message["content"] for message in request_a["messages"]]
        request_b_text = [message["content"] for message in request_b["messages"]]
        check(
            "A получила общее прошлое и своё продолжение без B",
            "общая точка" in request_a_text
            and "только A" in request_a_text
            and "только B" not in request_a_text,
            str(request_a_text),
        )
        check(
            "B получила общее прошлое и своё продолжение без A",
            "общая точка" in request_b_text
            and "только B" in request_b_text
            and "только A" not in request_b_text,
            str(request_b_text),
        )
        вывод_ветви = fragments_text(branch_a.log[отметка_ветви:])
        checkpoint = branch_store.load().checkpoint_id
        check(
            "итог Branching различает активную ветвь и контрольную точку",
            "активная ветвь: A" in вывод_ветви
            and f"контрольная точка: {checkpoint}" in вывод_ветви,
            вывод_ветви,
        )
        граф_до_отказов = branch_store.path.read_bytes()
        агенты_до_отказов = [pane.agent for pane in branch_screen.panes]
        активная_до_отказов = branch_screen.active_pane
        await cli.cmd_branch(state, "split C D")
        await cli.cmd_branch(state, "unknown")
        check(
            "повторный split и неизвестная ветвь не меняют граф и панели",
            branch_store.path.read_bytes() == граф_до_отказов
            and [pane.agent for pane in branch_screen.panes] == агенты_до_отказов
            and branch_screen.active_pane == активная_до_отказов,
        )
        await cli.cmd_branch(state, "")
        check(
            "/branch показывает точку, обе ветви и активную и открывает выбор",
            state.picker is not None
            and all(
                text in log_text(state, branch_screen.pane)
                for text in ("контрольная точка", "A, B", "активная")
            ),
        )
        state.picker = None

        strategy_screens = tuple(state.screens[1:])
        histories_before_clear = {
            id(pane.agent): pane.agent.history()
            for screen in strategy_screens
            for pane in screen.panes
        }
        facts_before_clear = facts_agent.conversation_facts()
        graph_before_clear = branch_store.path.read_bytes()
        state.main_agent.remember("главное до clear", "ответ главного", model=state.model)
        await cli.handle_command("/clear", state)
        check(
            "/clear очистил только main и назвал это на активной ветви",
            state.main_agent.history() == []
            and "главный разговор очищен" in log_text(state, branch_screen.pane),
        )
        check(
            "/clear сохранил все экраны, истории, факты и граф стратегий",
            tuple(state.screens[1:]) == strategy_screens
            and facts_agent.conversation_facts() == facts_before_clear
            and branch_store.path.read_bytes() == graph_before_clear
            and all(
                pane.agent.history() == histories_before_clear[id(pane.agent)]
                for screen in strategy_screens
                for pane in screen.panes
            ),
        )

        restored_source, _ = profiles.load("strategy-ui")
        restored_state = cli.State(
            config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=restored_source,
        )
        restored_sliding = restored_state.screens[
            cli.ensure_strategy_screen(restored_state, "sliding")
        ]
        restored_facts = restored_state.screens[
            cli.ensure_strategy_screen(restored_state, "facts")
        ]
        restored_branch = restored_state.screens[
            cli.ensure_strategy_screen(restored_state, "branching")
        ]
        check(
            "перезапуск поднял полную линейную историю каждой стратегии отдельно",
            restored_sliding.first.agent.history() == sliding_agent.history()
            and restored_facts.first.agent.history() == facts_agent.history(),
            str(
                [
                    restored_sliding.first.agent.history(),
                    restored_facts.first.agent.history(),
                ]
            ),
        )
        check(
            "перезапуск поднял ручную редакцию Sticky Facts",
            restored_facts.first.agent.conversation_facts()
            == facts_agent.conversation_facts(),
            str(restored_facts.first.agent.conversation_facts()),
        )

        class FactsOnlyClient:
            """Извлекатель успевает записать факт, основной запрос падает."""

            def __init__(self):
                self.calls = []

            async def stream_chat(self, model, messages, params=None):
                self.calls.append({"model": model, "messages": messages})
                system = messages[0]["content"] if messages else ""
                if "обновляешь словарь фактов" not in system:
                    raise RuntimeError("основной запрос не удался")
                yield api.StreamEvent(
                    "content",
                    '{"set":{"survived":"да"},"forget":[]}',
                )
                yield api.StreamEvent(
                    "meta",
                    finish_reason="stop",
                    usage={
                        "prompt_tokens": 3,
                        "completion_tokens": 2,
                        "total_tokens": 5,
                    },
                )

            async def aclose(self):
                pass

        facts_only_client = FactsOnlyClient()
        facts_only_source = profiles.Profile(
            name="facts-survive-main-failure",
            system="основной разговор",
            compact_at=0,
        )
        facts_only_state = cli.State(
            config=Config(api_key="sk-test", remember=False),
            client=facts_only_client,
            model="deepseek-v4-flash",
            profile=facts_only_source,
        )
        facts_only_screen = facts_only_state.screens[
            cli.ensure_strategy_screen(facts_only_state, "facts")
        ]
        await cli.handle_submit(
            "сохрани факт при отказе",
            facts_only_state,
            destination_screen=facts_only_screen,
            destination_pane=facts_only_screen.first,
        )
        facts_only_pane_id = id(facts_only_screen.first)
        await cli.switch_profile(facts_only_state, "default")
        facts_only_restored = cli.State(
            config=Config(api_key="sk-test", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=facts_only_source,
        )
        facts_only_restored_screen = facts_only_restored.screens[
            cli.ensure_strategy_screen(facts_only_restored, "facts")
        ]
        check(
            "смена профиля дождалась отказа main и сохранила успешные facts",
            facts_only_screen.first.agent.history() == []
            and facts_only_restored_screen.first.agent.conversation_facts()
            == ({"survived": "да"}, 1)
            and facts_only_pane_id not in facts_only_state.pane_workers
            and len(facts_only_state.screens) == 1,
            str(facts_only_restored_screen.first.agent.conversation_facts()),
        )
        await cli.close_pane_workers(facts_only_state)

        print("\n12т. Смена профиля дожидается старых очередей")
        (profiles_dir / "lifecycle-target.json").write_text(
            json.dumps(
                {
                    "name": "lifecycle-target",
                    "system": "новый профиль",
                    "compact_at": 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        lifecycle_client = УправляемыйКлиент()
        lifecycle_state = cli.State(
            config=Config(
                api_key="sk-test",
                model="deepseek-v4-flash",
                remember=False,
            ),
            client=lifecycle_client,
            model="deepseek-v4-flash",
            profile=profiles.Profile(
                name="lifecycle-source",
                system="старый профиль",
                compact_at=0,
            ),
        )
        lifecycle_screen = lifecycle_state.screens[
            cli.ensure_strategy_screen(lifecycle_state, "sliding")
        ]
        lifecycle_pane = lifecycle_screen.first
        lifecycle_agent = lifecycle_pane.agent
        old_main_agent = lifecycle_state.main_agent
        main_runner = asyncio.create_task(cli.worker(lifecycle_state))
        await cli.handle_submit(
            "старый main",
            lifecycle_state,
            destination_screen=lifecycle_state.main,
            destination_pane=lifecycle_state.main.first,
        )
        await cli.handle_submit(
            "старая панель 1",
            lifecycle_state,
            destination_screen=lifecycle_screen,
            destination_pane=lifecycle_pane,
        )
        await cli.handle_submit(
            "старая панель 2",
            lifecycle_state,
            destination_screen=lifecycle_screen,
            destination_pane=lifecycle_pane,
        )
        await asyncio.gather(
            lifecycle_client.wait_started("старый main"),
            lifecycle_client.wait_started("старая панель 1"),
        )
        old_pane_id = id(lifecycle_pane)
        switch_task = asyncio.create_task(
            cli.switch_profile(lifecycle_state, "lifecycle-target")
        )
        while not lifecycle_state.switching_profile:
            await asyncio.sleep(0)
        log_before_rejected = len(lifecycle_pane.log)
        await cli.handle_submit(
            "не принимать при смене",
            lifecycle_state,
            destination_screen=lifecycle_screen,
            destination_pane=lifecycle_pane,
        )
        check(
            "при начавшейся смене новый вопрос явно отклонён",
            "выполняется смена профиля"
            in fragments_text(lifecycle_pane.log[log_before_rejected:])
            and "не принимать при смене"
            not in fragments_text(lifecycle_pane.log[log_before_rejected:]),
        )
        lifecycle_client.release("старый main")
        lifecycle_client.release("старая панель 1")
        await lifecycle_client.wait_started("старая панель 2")
        lifecycle_client.release("старая панель 2")
        await switch_task
        check(
            "смена профиля дождалась текущего и очередного ответа старой панели",
            "ответ:старая панель 1" in log_text(lifecycle_state, lifecycle_pane)
            and "ответ:старая панель 2"
            in log_text(lifecycle_state, lifecycle_pane)
            and lifecycle_agent.history()[-4:]
            == [
                {"role": "user", "content": "старая панель 1"},
                {"role": "assistant", "content": "ответ:старая панель 1"},
                {"role": "user", "content": "старая панель 2"},
                {"role": "assistant", "content": "ответ:старая панель 2"},
            ],
            str(lifecycle_agent.history()),
        )
        old_main_call = вызов_с_вопросом(lifecycle_client, "старый main")
        check(
            "старый main завершился со старым профилем и не попал в новый Agent",
            old_main_call["messages"][0]
            == {"role": "system", "content": "старый профиль"}
            and lifecycle_state.main_agent is not old_main_agent
            and "старый main"
            not in [
                message["content"]
                for message in lifecycle_state.main_agent.history()
            ],
            str(
                (
                    old_main_call["messages"],
                    lifecycle_state.main_agent.history(),
                )
            ),
        )
        check(
            "окончательный расход перенесён, а ссылка на старый PaneWorker удалена",
            lifecycle_state.retired_usage["total_tokens"] == 15
            and lifecycle_state.session_usage_total()["total_tokens"] == 15
            and old_pane_id not in lifecycle_state.pane_workers
            and len(lifecycle_state.screens) == 1,
            str(
                (
                    lifecycle_state.retired_usage,
                    lifecycle_state.pane_workers,
                )
            ),
        )
        main_runner.cancel()
        await asyncio.gather(main_runner, return_exceptions=True)
        await cli.close_pane_workers(lifecycle_state)

        restored_a = restored_branch.pane_by_key("A")
        restored_b = restored_branch.pane_by_key("B")
        check(
            "перезапуск поднял две независимые ветви с общим прошлым",
            restored_a is not None
            and restored_b is not None
            and restored_a.agent is not restored_b.agent
            and "общая точка" in [
                message["content"] for message in restored_a.agent.history()
            ]
            and "только A" in [
                message["content"] for message in restored_a.agent.history()
            ]
            and "только B" not in [
                message["content"] for message in restored_a.agent.history()
            ]
            and "только B" in [
                message["content"] for message in restored_b.agent.history()
            ]
            and "только A" not in [
                message["content"] for message in restored_b.agent.history()
            ],
        )

        print("\n12р. Три рабочих экрана стратегий начинают одновременно")
        for name, strategy, prefill in (
            ("screen-sliding", "sliding", "первый Sliding"),
            ("screen-facts", "facts", "первый Facts"),
            ("screen-branch", "branching", "первый Branching"),
        ):
            (profiles_dir / f"{name}.json").write_text(
                json.dumps(
                    {
                        "name": name,
                        "context_strategy": strategy,
                        "strategy_window": 2,
                        "compact_at": 0,
                        "prefill": prefill,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        (profiles_dir / "three-strategies.json").write_text(
            json.dumps(
                {
                    "name": "three-strategies",
                    "compact_at": 0,
                    "screens": ["screen-sliding", "screen-facts", "screen-branch"],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        вызовов_до_трёх = len(fake.calls)
        await cli.switch_profile(state, "three-strategies")
        strategy_work_screens = state.screens[1:]
        check(
            "три рабочих экрана и три Agent созданы до первого вопроса",
            [screen.key for screen in strategy_work_screens]
            == ["screen-sliding", "screen-facts", "screen-branch"]
            and len({id(screen.first.agent) for screen in strategy_work_screens}) == 3
            and len(fake.calls) == вызовов_до_трёх,
        )
        check(
            "на каждом экране заранее стоит его первая заготовка",
            [screen.first.draft for screen in strategy_work_screens]
            == ["первый Sliding", "первый Facts", "первый Branching"],
            str([screen.first.draft for screen in strategy_work_screens]),
        )

        concurrent_client = УправляемыйКлиент()
        state.client = concurrent_client
        concurrent_questions = ("одновременно Sliding", "одновременно Facts", "одновременно Branching")
        for screen, question in zip(strategy_work_screens, concurrent_questions, strict=True):
            cli.switch_screen(state, state.screens.index(screen))
            buffer.text = question
            press(app, Keys.ControlM, "\r")
        await asyncio.gather(
            *(concurrent_client.wait_started(question) for question in concurrent_questions)
        )
        concurrent_workers = [
            state.pane_workers[id(screen.first)] for screen in strategy_work_screens
        ]
        check(
            "три стратегии начали запросы до первого ответа",
            all(worker.busy for worker in concurrent_workers),
        )
        check(
            "каждый вопрос захвачен Agent своего режима",
            {
                call["messages"][-1]["content"]
                for call in concurrent_client.calls
                if call["messages"][-1]["content"] in concurrent_questions
            }
            == set(concurrent_questions)
            and [
                screen.first.agent.profile.context_strategy
                for screen in strategy_work_screens
            ]
            == ["sliding", "facts", "branching"],
        )
        # Sticky Facts делает четвёртое, вспомогательное обращение. Отпускаем его вместе с
        # тремя основными: все четыре уже начались, но ни один основной ответ ещё не получен.
        while len(concurrent_client.calls) < 4:
            await asyncio.sleep(0)
        for call in concurrent_client.calls:
            concurrent_client.release(call["messages"][-1]["content"])
        await asyncio.gather(*(worker.queue.join() for worker in concurrent_workers))
        check(
            "после одновременного старта ответы остались в своих панелях",
            all(
                f"ответ:{question}" in log_text(state, screen.first)
                for screen, question in zip(
                    strategy_work_screens, concurrent_questions, strict=True
                )
            ),
        )
        state.client = fake

        print("\n12с. Источник стратегии и тип экрана не зависят от key")
        collision_name = "strategy:x:sliding"
        (profiles_dir / f"{collision_name}.json").write_text(
            json.dumps(
                {
                    "name": collision_name,
                    "system": "инструкция дочернего экрана",
                    "compact_at": 0,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        (profiles_dir / "x.json").write_text(
            json.dumps(
                {
                    "name": "x",
                    "system": "инструкция родителя",
                    "compact_at": 0,
                    "screens": [collision_name],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        await cli.switch_profile(state, "x")
        collision_work_screen = state.screen
        check(
            "произвольный key рабочего экрана допустимо похож на служебный",
            collision_work_screen.key
            == screens.strategy_screen_key("x", "sliding")
            and collision_work_screen.strategy_identity
            == (collision_name, "standard"),
            str(
                (
                    collision_work_screen.key,
                    collision_work_screen.strategy_identity,
                )
            ),
        )
        cli.switch_screen(state, 0)
        cli.cmd_strategy(state, "use sliding")
        parent_sliding = state.screen
        check(
            "служебная личность не дала рабочему key подменить стратегию родителя",
            parent_sliding is not collision_work_screen
            and parent_sliding.strategy_identity == ("x", "sliding")
            and parent_sliding.first.agent.profile.name == "x",
            str(
                (
                    parent_sliding.strategy_identity,
                    parent_sliding.first.agent.profile.name,
                )
            ),
        )
        cli.switch_screen(state, state.screens.index(collision_work_screen))
        cli.cmd_strategy(state, "use facts")
        child_facts = state.screen
        check(
            "/strategy use взял профиль активной дочерней панели",
            child_facts.strategy_identity == (collision_name, "facts")
            and child_facts.first.agent.profile.name == collision_name
            and child_facts.first.agent.profile.system
            == "инструкция дочернего экрана",
            str(
                (
                    child_facts.strategy_identity,
                    child_facts.first.agent.profile.name,
                )
            ),
        )

        print("\n12з. О переполнении окна сказано до отправки")

        # Ради этого и заведён собственный счёт: на живом прогоне 2026-09-09 предсказание
        # разошлось с сервером на один токен из миллиона — значит предупреждать честно.
        # Запрос всё равно уходит: своя оценка не должна отменять ответ сервера.
        огромный = profiles.Profile(
            name="огромный",
            system="Р" * (tokens.CONTEXT_WINDOW * 4),  # заведомо больше окна
            params={"max_tokens": 2000},
        )
        экран_окна = screens.Screen(key="окно", title="окно", profile=огромный)
        state.screens.append(экран_окна)
        было_вызовов = len(fake.calls)
        await output.run_turn(state, экран_окна.first.agent, "вопрос", pane=экран_окна.first)
        текст_окна = log_text(state, экран_окна.first)
        check("о переполнении окна сказано", "не влезет в окно модели" in текст_окна, текст_окна[:200])
        check("названо место под ответ", "на ответ" in текст_окна, текст_окна[:200])
        check("сказано, что делать", "/clear" in текст_окна, текст_окна[:200])
        check("запрос всё равно ушёл — решает сервер", len(fake.calls) > было_вызовов, str(len(fake.calls)))
        state.screens.remove(экран_окна)

        print("\n12ж. Расход сеанса переживает смену профиля")

        # Сеанс — это запуск процесса, а не жизнь одного собеседника. Смена профиля заводит нового
        # агента и закрывает экраны группы; без копилки выбывших итог падал бы почти до нуля посреди
        # работы, хотя деньги списаны. На записи ролика профиль переключают трижды — увидели бы сразу.
        состояние_смены = cli.State(
            config=Config(api_key="sk-test", model="deepseek-v4-flash", remember=False),
            client=fake,
            model="deepseek-v4-flash",
            profile=profiles.builtin_default(),
        )
        состояние_смены.main.first.agent.session_usage = tokens.add_usage(
            {}, {"prompt_tokens": 700, "completion_tokens": 300, "total_tokens": 1000}
        )
        эксперт = screens.Screen(key="эксперт", title="эксперт", profile=состояние_смены.profile)
        эксперт.first.agent.session_usage = tokens.add_usage(
            {}, {"prompt_tokens": 200, "completion_tokens": 100, "total_tokens": 300}
        )
        состояние_смены.screens.append(эксперт)
        check(
            "до смены итог сеанса — сумма главного и эксперта",
            состояние_смены.session_usage_total()["total_tokens"] == 1300,
            str(состояние_смены.session_usage_total()["total_tokens"]),
        )

        await cli.switch_profile(состояние_смены, "default")
        итог_после = состояние_смены.session_usage_total()["total_tokens"]
        check("расход прежнего собеседника не пропал при смене профиля", итог_после == 1300, str(итог_после))
        check(
            "новый собеседник начинает с нуля",
            состояние_смены.main_agent.session_usage["total_tokens"] == 0,
            str(состояние_смены.main_agent.session_usage["total_tokens"]),
        )
        check("экраны прежней группы закрыты", len(состояние_смены.screens) == 1, str(len(состояние_смены.screens)))

        # Двойной счёт — главная опасность копилки: провожать живого агента нельзя.
        await cli.switch_profile(состояние_смены, "default")
        check(
            "вторая смена профиля не удвоила расход",
            состояние_смены.session_usage_total()["total_tokens"] == 1300,
            str(состояние_смены.session_usage_total()["total_tokens"]),
        )
        состояние_смены.main.first.agent.session_usage = tokens.add_usage(
            {}, {"prompt_tokens": 40, "completion_tokens": 10, "total_tokens": 50}
        )
        check(
            "расход нового собеседника прибавляется к копилке",
            состояние_смены.session_usage_total()["total_tokens"] == 1350,
            str(состояние_смены.session_usage_total()["total_tokens"]),
        )
        print("\n12л. Строки о сжатии памяти")
        # Три исхода обрезки говорятся тремя РАЗНЫМИ строками, и это требование, а не вкус:
        # замена дословного текста пересказом и потеря без пересказа стоят разного, а
        # отвергнутая выжимка — вообще не потеря памяти, а пропавший блок запроса.
        сжато = fragments_text(ui.compacted_fragments(5, 12))
        check("строка о сжатии называет заменённые пары", "5 пар заменены пересказом" in сжато, сжато)
        check("строка о сжатии называет пройденное разговором", "пройдено 12 пар" in сжато, сжато)
        check("сам пересказ в ленту не печатается", len(сжато) < 120, сжато)
        забыто = fragments_text(ui.trimmed_fragments(3, сжатие=True))
        check("забытое дословно названо своими словами", "забыты дословно" in забыто, забыто)
        check("сказано, почему забыто", "выжимка не поспела" in забыто, забыто)
        check("забытое не выдано за сжатие", "сжата" not in забыто, забыто)
        без_сжатия = fragments_text(ui.trimmed_fragments(3, сжатие=False))
        check(
            "при выключенном сжатии про выжимку не поминают",
            "выжимка" not in без_сжатия and "выброшено 3 пары" in без_сжатия,
            без_сжатия,
        )
        # Отчёт о восстановлении читают один раз, в первую секунду запуска: разбитый на три
        # строки, он читается как три события, хотя событие одно.
        восстановление = fragments_text(
            ui.restored_fragments(3, 12, "", за_выжимкой=8, забыто_дословно=2)
        )
        check("о восстановлении сказано одной строкой", восстановление.count("\n") == 1, восстановление)
        check("названы поднятые пары и все сохранённые", "3 пары из 12 сохранённых" in восстановление, восстановление)
        check("названо, сколько пар стоит за выжимкой", "за выжимкой 8 пар" in восстановление, восстановление)
        check("названо забытое дословно", "из них 2 забыты дословно" in восстановление, восстановление)
        check("названа команда показа выжимки", "/context" in восстановление, восстановление)
        без_выжимки = fragments_text(ui.restored_fragments(3, 12, ""))
        check(
            "без выжимки о ней не поминают",
            "выжимк" not in без_выжимки and без_выжимки.count("\n") == 1,
            без_выжимки,
        )
        check(
            "пока сжиматель работает, в строке состояния сказано",
            "сжимаю разговор…" in fragments_text(ui.status_fragments("m", True, "talky", False, True, False, True)),
        )
        check(
            "когда не работает — не сказано",
            "сжимаю" not in fragments_text(ui.status_fragments("m", True, "talky", False, True, False, False)),
        )
        отвергнута = fragments_text(ui.summary_rejected_fragments())
        check(
            "об отвергнутой выжимке сказано инструкцией",
            "другой инструкцией" in отвергнута and "не идёт" in отвергнута,
            отвергнута,
        )

        print("\n12м. Полоска занятости мерится ближайшим ограничителем")
        # ОТКУДА ЧИСЛА: окно — 10 пар, запас обрезки — 5, значит обрезка на 15-й паре. Девять
        # пар в памяти дают ровно девять пятнадцатых — то самое «9 из 15» из требования.
        профиль_полоски = profiles.builtin_default()
        профиль_полоски.history_window = 10
        собеседник = cli.Agent("полоска", профиль_полоски)
        for номер in range(9):
            собеседник.remember(f"вопрос {номер}", f"ответ {номер}")
        счёт = ui.СчётЗанятости()
        ближайший = счёт.ближайший(собеседник, "", model=state.model)
        check("ближайшим назван не порог сжатия, а окно по парам", ближайший.имя == "окно по парам", str(ближайший))
        check("доля мерится своими единицами ограничителя", abs(ближайший.доля - 9 / 15) < 1e-9, str(ближайший.доля))
        полоска = fragments_text(ui.context_bar_fragments(ближайший, 80))
        строки = полоска.rstrip("\n").split("\n")
        check("полоска занимает две строки", len(строки) == 2, полоска)
        check("на восьми десятых стоит отметка", "┊" in строки[0], строки[0])
        check("жёлоб и заполнение видны оба", "▓" in строки[0] and "░" in строки[0], строки[0])
        check("подпись называет долю", строки[1].startswith("60 % до сжатия"), строки[1])
        check("подпись называет ограничитель и его числа", "окно по парам, 9 из 15" in строки[1], строки[1])

        # Порог сжатия ближайшим: окно по парам выключено, мерить остаётся вес.
        профиль_веса = profiles.builtin_default()
        профиль_веса.history_window = 0
        весовой = cli.Agent("вес", профиль_веса)
        подпись_порога = fragments_text(ui.context_bar_fragments(счёт.ближайший(весовой, "", model=state.model), 80))
        # Форма у всех ограничителей одна: «сколько есть из скольких сработает». Прежняя
        # сборка называла здесь сам порог и окно («5 242 из 1 048 576»), и рядом с «9 из 15»
        # это читалось одинаково, а значило противоположное — человек решил бы, что набрал
        # пять тысяч из миллиона и до сжатия далеко, ровно когда до него один ход. Окно
        # модели осталось, но отдельно и в скобках: порог задан его долей, и без окна
        # непонятно, откуда взялось число.
        порог_весового = int(профиль_веса.compact_at * tokens.CONTEXT_WINDOW)
        check(
            "подпись порога называет набранное и сам порог, а окно — отдельно",
            "порог сжатия" in подпись_порога
            and f"из {ui.format_exact(порог_весового)} токенов" in подпись_порога
            and "% окна модели" in подпись_порога,
            подпись_порога,
        )
        # Перебор — отдельный вид подписи: доля выше единицы не поднимается, и «на сколько
        # ушли сверх» ей не сказать. Предел веса задан заведомо меньше веса инструкции.
        профиль_предела = profiles.builtin_default()
        профиль_предела.history_window = 0
        профиль_предела.budget_tokens = 10
        тяжёлый = cli.Agent("предел", профиль_предела)
        перебор = fragments_text(
            ui.context_bar_fragments(ui.СчётЗанятости().ближайший(тяжёлый, "", model=state.model), 80)
        )
        check("при переборе сказано, на сколько ушли сверх", "сверх порога на" in перебор, перебор)
        check("полоска при переборе залита целиком", "░" not in перебор.split("\n")[0], перебор)
        check("при переборе назван и сам ограничитель", "предел веса, " in перебор and "из 10 токенов" in перебор, перебор)

        # Узкое окно: три десятка знаков на шкалу дают шаг в три процента — полоска врёт
        # больше, чем сообщает. Подпись при этом и точнее, и полезнее.
        узкая = fragments_text(ui.context_bar_fragments(ближайший, 39))
        check("в узком окне полоски нет", "▓" not in узкая and "░" not in узкая, узкая)
        check("в узком окне подпись осталась", "окно по парам, 9 из 15" in узкая, узкая)

        # Сжатие выключено — полоски нет вовсе: она заведена как счёт до сжатия, а без него
        # мерила бы дорогу к обычному забыванию, о котором и так говорит строка в ленте.
        профиль_без_сжатия = profiles.builtin_default()
        профиль_без_сжатия.compact_at = 0
        без_полоски = cli.Agent("без сжатия", профиль_без_сжатия)
        check(
            "при выключенном сжатии ближайшего не называют",
            ui.СчётЗанятости().ближайший(без_полоски, "вопрос", model=state.model) is None,
        )
        check("полоски нет вовсе, а не пустая", ui.context_bar_fragments(None, 80) == [])

        print("\n12н. Полоска не считает всё заново на каждое нажатие")
        # Постоянная часть веса считается при изменении памяти и запоминается. Пересчёт всего
        # на каждое нажатие означал бы проход словарём токенов по всей памяти между двумя
        # буквами, а `system_text()` вдобавок ходит на диск за фактами.
        обращений = {"факты": 0, "система": 0}

        def факты_со_счётом():
            обращений["факты"] += 1
            return ["любит щуку"]

        счёт_фактов = ui.СчётЗанятости(факты_со_счётом)
        # Системная часть агента подменена считающей, а не бросающей: лишний поход на диск
        # должен назваться числом в отчёте, а не уронить прогон посреди раздела — упавший
        # прогон не покажет, сколько именно раз туда сходили.
        настоящая_системная = весовой.system_text
        весовой.system_text = lambda: (обращений.__setitem__("система", обращений["система"] + 1), настоящая_системная())[1]
        основа = счёт_фактов.ближайший(весовой, "", model=state.model).текущее
        for длина in range(1, 8):
            последний = счёт_фактов.ближайший(весовой, "а" * длина, model=state.model)
        check("факты прочитаны один раз на весь набор", обращений["факты"] == 1, str(обращений["факты"]))
        check("за системной частью полоска не ходит вовсе", обращений["система"] == 0, str(обращений["система"]))
        check("набранный вопрос двигает заполнение", последний.текущее > основа, f"{основа} → {последний.текущее}")
        check(
            "добавка вопроса — ровно его вес",
            последний.текущее == основа + tokens.count_text("а" * 7),
            f"{последний.текущее} против {основа + tokens.count_text('а' * 7)}",
        )
        # На шкале в парах считать нечего вовсе: вопрос парой ещё не стал.
        with_пары = счёт.ближайший(собеседник, "очень длинный вопрос" * 50, model=state.model)
        check("на шкале в парах набранное не считается", with_пары.текущее == 9, str(with_пары.текущее))
        # Память изменилась — постоянная часть обязана пересчитаться, иначе полоска после
        # сжатия осталась бы красной и сообщала бы, что сжатие не сработало.
        до_очистки = счёт.ближайший(собеседник, "", model=state.model).доля
        собеседник.forget()
        check(
            "после очистки памяти заполнение упало",
            счёт.ближайший(собеседник, "", model=state.model).доля < до_очистки,
            str(до_очистки),
        )

        print("\n13. Выход")
        клиент_выхода = УправляемыйКлиент()
        state.client = клиент_выхода
        state.config.remember = False
        панель_выхода = screens.Pane(
            key="worker-exit",
            profile=profiles.Profile(name="worker-exit"),
        )
        экран_выхода = screens.Screen(
            key="worker-exit",
            title="worker-exit",
            panes=[панель_выхода],
            interactive=True,
        )
        state.screens.append(экран_выхода)
        cli.switch_screen(state, len(state.screens) - 1)
        buffer.text = "незавершённый при выходе"
        press(app, Keys.ControlM, "\r")
        await клиент_выхода.wait_started("незавершённый при выходе")
        созданные_исполнители = tuple(state.pane_workers.values())
        check("перед выходом панельный обмен действительно идёт", state.pane_workers[id(панель_выхода)].busy)

        await send("/exit" + ENTER, pause=0)
        await run
        check("приложение завершилось", run.done())
        check(
            "незавершённый обмен отменён при выходе",
            клиент_выхода._event(клиент_выхода.cancelled, "незавершённый при выходе").is_set(),
        )
        check(
            "все созданные исполнители завершены",
            all(item.runner is not None and item.runner.done() for item in созданные_исполнители),
        )
        check("задачи Enter не оставлены в фоне", not state.submission_tasks)
        check("клиент закрыт после исполнителей", клиент_выхода.closed)


asyncio.run(main())
print()
if failures:
    print(f"ПРОВАЛЕНО: {len(failures)} — " + "; ".join(failures))
    sys.exit(1)
print("Все проверки пройдены")
