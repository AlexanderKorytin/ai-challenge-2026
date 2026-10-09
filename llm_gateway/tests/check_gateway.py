"""Проверки шлюза без сети: служба Ollama — подменный транспорт, приложение зовётся как ASGI.

Запуск: `uv run python tests/check_gateway.py` из каталога `llm_gateway`.
Журнал шлюза пишется во временный каталог; в домашний каталог проверки не пишут.
"""

from __future__ import annotations

import asyncio
import json
import sys
import tempfile
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import gateway  # noqa: E402

ПРОВАЛЫ: list[str] = []


def check(имя: str, условие: bool, подробности: str = "") -> None:
    print(f"  [{'OK ' if условие else 'НЕТ'}] {имя}" + (f" — {подробности}" if not условие and подробности else ""))
    if not условие:
        ПРОВАЛЫ.append(имя)


МОДЕЛЬ = "малая:latest"
ЧУЖАЯ = "большая:27b"
# Малый словарь в виде, в каком его отдаёт Ollama: каждая «a» — один токен.
СВЕДЕНИЯ = {
    "tokenizer.ggml.model": "gpt2",
    "tokenizer.ggml.pre": "qwen35",
    "tokenizer.ggml.tokens": ["a", "b", "c", "ab", "abc", "Ġ", "<|метка|>"],
    "tokenizer.ggml.merges": ["a b", "ab c"],
    "tokenizer.ggml.token_type": [1, 1, 1, 1, 1, 1, 3],
}
ОКНО = 200
КУСКИ = [
    b'data: {"choices":[{"delta":{"content":"\xd0\xb4\xd0\xb0"}}]}\n\n',
    b'data: {"choices":[],"usage":{"prompt_tokens":7,"completion_tokens":3,"total_tokens":10}}\n\n',
    b"data: [DONE]\n\n",
]


class Поток(httpx2.AsyncByteStream):
    def __init__(self, куски: list[bytes], пауза: float = 0) -> None:
        self.куски = куски
        self.пауза = пауза
        self.закрыт = False

    async def __aiter__(self):
        for кусок in self.куски:
            yield кусок
            if self.пауза:
                await asyncio.sleep(self.пауза)

    async def aclose(self) -> None:
        self.закрыт = True


class Служба(httpx2.AsyncBaseTransport):
    """Подменная Ollama: запоминает запросы, чат отдаёт потоком из трёх кусков.

    Свой транспорт, а не `MockTransport`: тот читает ответ целиком, и поток перестаёт быть потоком."""

    def __init__(self, отказ: bool = False, словарь: bool = True, код_чата: int = 200, пауза: float = 0) -> None:
        self.код_чата = код_чата
        self.пауза = пауза
        self.потоки: list[Поток] = []
        self.запросы: list[httpx2.Request] = []
        self.отказ = отказ
        self.словарь = словарь

    async def handle_async_request(self, запрос: httpx2.Request) -> httpx2.Response:
        # Как у настоящей сети: тело ответа приходит потоком и читается один раз.
        готовый = self._ответ(запрос)
        куски = КУСКИ if запрос.url.path == "/v1/chat/completions" and готовый.status_code == 200 else [готовый.content]
        заголовки = {имя: значение for имя, значение in готовый.headers.items() if имя != "content-length"}
        self.потоки.append(Поток(куски, self.пауза))
        return httpx2.Response(готовый.status_code, headers=заголовки, stream=self.потоки[-1])

    def _ответ(self, запрос: httpx2.Request) -> httpx2.Response:
        if self.отказ:
            raise httpx2.ConnectError("отказ", request=запрос)
        self.запросы.append(запрос)
        путь = запрос.url.path
        if путь == "/v1/chat/completions":
            if self.код_чата != 200:
                return httpx2.Response(self.код_чата, json={"error": {"message": "сбой службы"}})
            return httpx2.Response(200, content=b"", headers={"content-type": "text/event-stream"})
        if путь == "/api/show":
            ответ = {"parameters": f"num_ctx {ОКНО}", "capabilities": ["completion"]}
            if self.словарь and json.loads(запрос.content).get("verbose"):
                ответ["model_info"] = СВЕДЕНИЯ
            return httpx2.Response(200, json=ответ)
        if путь in ("/api/tags", "/api/ps"):
            return httpx2.Response(200, json={"models": [{"name": МОДЕЛЬ}, {"name": ЧУЖАЯ}]})
        if путь == "/v1/models":
            return httpx2.Response(200, json={"object": "list", "data": [{"id": МОДЕЛЬ}, {"id": ЧУЖАЯ}]})
        return httpx2.Response(404)

    def чаты(self) -> list[httpx2.Request]:
        return [запрос for запрос in self.запросы if запрос.url.path == "/v1/chat/completions"]


class Часы:
    def __init__(self) -> None:
        self.сейчас = 1000.0

    def __call__(self) -> float:
        return self.сейчас


class Стенд:
    def __init__(self, каталог: Path, частота: int = 100, служба: Служба | None = None) -> None:
        self.служба = служба or Служба()
        self.часы = Часы()
        self.журнал = каталог / f"журнал-{id(self)}.jsonl"
        настройки = gateway.Settings(
            upstream="http://ollama.test",
            models=frozenset({МОДЕЛЬ}),
            rate_per_minute=частота,
            tokens={"токен-маши": "маша", "токен-пети": "петя"},
            log_path=self.журнал,
        )
        self.приложение = gateway.build_app(настройки, self.служба, self.часы)

    async def запрос(
        self, метод: str, путь: str, тело: object = None, токен: str | None = "токен-маши",
        схема: str = "Bearer", бросить_после: int | None = None,
    ):  # fmt: skip
        """Вызов приложения по ASGI. Возвращает код, заголовки и куски тела по отдельности:
        так видно, что поток не склеен."""
        if isinstance(тело, bytes):
            содержимое = тело
        else:
            содержимое = b"" if тело is None else json.dumps(тело, ensure_ascii=False).encode()
        заголовки = [(b"content-type", b"application/json")]
        if токен is not None:
            заголовки.append((b"authorization", f"{схема} {токен}".encode()))
        область = {
            "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1", "method": метод,
            "scheme": "https", "path": путь, "raw_path": путь.encode(), "query_string": b"",
            "headers": заголовки, "server": ("шлюз", 443), "client": ("10.0.0.1", 5000),
        }  # fmt: skip
        отдано = False
        брошено = asyncio.Event()

        async def receive():
            nonlocal отдано
            if отдано:
                # Клиент бросил чтение: приложение узнаёт об этом сообщением `http.disconnect`.
                await брошено.wait()
                return {"type": "http.disconnect"}
            отдано = True
            return {"type": "http.request", "body": содержимое, "more_body": False}

        код, шапка, куски = 0, {}, []

        async def send(сообщение):
            nonlocal код, шапка
            if сообщение["type"] == "http.response.start":
                код = сообщение["status"]
                шапка = {имя.decode(): значение.decode() for имя, значение in сообщение["headers"]}
            elif сообщение["type"] == "http.response.body" and сообщение.get("body"):
                куски.append(сообщение["body"])
                if бросить_после is not None and len(куски) >= бросить_после:
                    брошено.set()

        await self.приложение(область, receive, send)
        return код, шапка, куски

    def строки(self) -> list[dict]:
        if not self.журнал.exists():
            return []
        return [json.loads(строка) for строка in self.журнал.read_text(encoding="utf-8").splitlines()]


def чат(текст: str = "aaa", **поля: object) -> dict:
    return {"model": МОДЕЛЬ, "messages": [{"role": "user", "content": текст}], "stream": True, **поля}


def признак(куски: list[bytes]) -> str:
    return json.loads(b"".join(куски))["error"]["code"]


async def проверки(каталог: Path) -> None:
    print("\n# Шаг 2: токен, путь, модель, поток, журнал")
    стенд = Стенд(каталог)
    код_без, _, куски_без = await стенд.запрос("POST", "/v1/chat/completions", чат(), токен=None)
    check("без токена — 401, служба не тронута", код_без == 401 and not стенд.служба.запросы, f"{код_без}")
    код_чужой, _, _ = await стенд.запрос("POST", "/v1/chat/completions", чат(), токен="токен-васи")
    check("чужой токен — 401", код_чужой == 401 and not стенд.служба.запросы, str(код_чужой))
    код_начало, _, _ = await стенд.запрос("POST", "/v1/chat/completions", чат(), токен="токен-маш")
    check("начало верного токена — 401", код_начало == 401, str(код_начало))
    код, шапка, куски = await стенд.запрос("POST", "/v1/chat/completions", чат("aaa секрет-реплики"))
    check("парная: с токеном — 200 и ответ службы", код == 200 and b"".join(куски) == b"".join(КУСКИ), str(код))
    check("поток из трёх кусков дошёл тремя кусками", куски == КУСКИ, str(len(куски)))
    check("вид потока передан клиенту", шапка.get("content-type", "").startswith("text/event-stream"), str(шапка))
    ушедший = стенд.служба.чаты()[-1]
    check("токен клиента службе не ушёл: в заголовке заглушка myharness",
          all("токен-маши" not in значение for значение in ушедший.headers.values()), str(dict(ушедший.headers)))
    check("тело запроса ушло службе как есть", json.loads(ушедший.content) == чат("aaa секрет-реплики"))
    строка = стенд.строки()[-1]
    check("журнал: клиент, модель, код и токены ответа",
          (строка["клиент"], строка["модель"], строка["код"], строка["вход"], строка["выход"]) == ("маша", МОДЕЛЬ, 200, 7, 3),
          str(строка))
    сырой_журнал = стенд.журнал.read_text(encoding="utf-8")
    check("журнал не несёт ни текста реплики, ни токена",
          "секрет-реплики" not in сырой_журнал and "токен-маши" not in сырой_журнал and "токен-васи" not in сырой_журнал)
    check("отказ 401 записан без имени клиента", стенд.строки()[0]["клиент"] is None and стенд.строки()[0]["код"] == 401)

    до = len(стенд.служба.запросы)
    for метод, путь in [("POST", "/api/pull"), ("DELETE", "/api/delete"), ("POST", "/api/generate"),
                        ("POST", "/api/create"), ("POST", "/v1/embeddings"), ("GET", "/"),
                        ("POST", "/v1/chat/completions/"), ("GET", "/v1/chat/completions")]:  # fmt: skip
        код_пути, _, _ = await стенд.запрос(метод, путь, {"model": МОДЕЛЬ})
        check(f"путь вне списка: {метод} {путь} — 404", код_пути == 404, str(код_пути))
    check("ни один запрос вне списка до службы не дошёл", len(стенд.служба.запросы) == до)

    код_модели, _, куски_модели = await стенд.запрос("POST", "/v1/chat/completions", {**чат(), "model": ЧУЖАЯ})
    check("чат с моделью вне списка — 404 model_not_found",
          код_модели == 404 and признак(куски_модели) == "model_not_found" and len(стенд.служба.запросы) == до)
    код_показа, _, _ = await стенд.запрос("POST", "/api/show", {"model": ЧУЖАЯ})
    код_своей, _, _ = await стенд.запрос("POST", "/api/show", {"model": МОДЕЛЬ})
    check("показ модели вне списка — 404, своей — 200 (пара)", (код_показа, код_своей) == (404, 200), f"{код_показа} {код_своей}")
    код_без_модели, _, _ = await стенд.запрос("POST", "/v1/chat/completions", {"messages": []})
    check("чат без имени модели — 404", код_без_модели == 404, str(код_без_модели))
    код_мусора, _, куски_мусора = await стенд.запрос("POST", "/v1/chat/completions", [1, 2])
    check("тело не объектом — 400", код_мусора == 400 and признак(куски_мусора) == "invalid_json", str(код_мусора))

    print("\n# Проверено одно тело — службе ушло оно же")
    до = len(стенд.служба.запросы)
    for имя_случая, тело_случая, ждём in [
        ("ключ Model рядом с model", {**чат(), "Model": ЧУЖАЯ}, (400, "unknown_field")),
        ("ключ Messages рядом с messages", {**чат(), "Messages": [{"role": "user", "content": "a" * 999}]}, (400, "unknown_field")),
        ("поле options (окно и время жизни модели)", {**чат(), "options": {"num_ctx": 999999}}, (400, "unknown_field")),
        ("поле keep_alive", {**чат(), "keep_alive": -1}, (400, "unknown_field")),
        ("имя модели списком", {**чат(), "model": [МОДЕЛЬ]}, (404, "model_not_found")),
        ("имя модели объектом", {**чат(), "model": {"a": 1}}, (404, "model_not_found")),
        ("ключ Content в реплике", {**чат(), "messages": [{"role": "user", "content": "a", "Content": "a" * 999}]}, (400, "invalid_request")),
        ("длинный текст списком частей взвешен, а не пропущен", {**чат(), "messages": [{"role": "user", "content": [{"type": "text", "text": "a" * 999}]}]}, (400, "context_length_exceeded")),
        ("реплика не объектом", {**чат(), "messages": ["aaa"]}, (400, "invalid_request")),
        ("max_tokens логическим значением", чат(max_tokens=True), (400, "invalid_request")),
        ("max_tokens дробным числом", чат(max_tokens=10.5), (400, "invalid_request")),
        ("max_tokens отрицательным", чат(max_tokens=-1), (400, "invalid_request")),
    ]:  # fmt: skip
        код_случая, _, куски_случая = await стенд.запрос("POST", "/v1/chat/completions", тело_случая)
        check(f"{имя_случая} — {ждём[0]} {ждём[1]}", (код_случая, признак(куски_случая)) == ждём, f"{код_случая} {куски_случая}")
    код_частей, _, _ = await стенд.запрос("POST", "/v1/chat/completions", {**чат(), "messages": [{"role": "user", "content": [{"type": "text", "text": "aaa"}]}]})
    вес_частей = стенд.строки()[-1].get("вес", 0)
    код_строки, _, _ = await стенд.запрос("POST", "/v1/chat/completions", чат("aaa"))
    check("текст списком частей принят и весит не меньше того же текста строкой",
          (код_частей, код_строки) == (200, 200) and вес_частей >= стенд.строки()[-1]["вес"] > 3, f"{код_частей} {вес_частей}")
    код_суррогата, _, _ = await стенд.запрос("POST", "/v1/chat/completions", json.dumps(чат("\ud800")).encode())
    check("одиночный суррогат в тексте не роняет шлюз", код_суррогата in (200, 400), str(код_суррогата))
    до = len(стенд.служба.запросы)
    код_картинки, _, куски_картинки = await стенд.запрос("POST", "/v1/chat/completions", {**чат(), "messages": [{"role": "user", "content": [{"type": "image_url", "image_url": {"url": "x"}}]}]})
    check("часть-картинка — 400 invalid_request", (код_картинки, признак(куски_картинки)) == (400, "invalid_request"), str(код_картинки))
    check("ни один такой запрос до службы не дошёл", len(стенд.служба.запросы) == до, str(len(стенд.служба.запросы) - до))
    check("чужой текст из поля model в журнал не попал", ЧУЖАЯ not in стенд.журнал.read_text(encoding="utf-8"))
    код_имени, _, _ = await стенд.запрос("POST", "/api/show", {"name": МОДЕЛЬ, "Model": ЧУЖАЯ, "verbose": True})
    ушло = json.loads(стенд.служба.запросы[-1].content)
    check("показ: службе ушли только model и verbose, лишний ключ отброшен",
          код_имени == 200 and ушло == {"model": МОДЕЛЬ, "verbose": True}, str(ушло))
    код_пары, _, _ = await стенд.запрос("POST", "/api/show", {"model": ЧУЖАЯ, "name": МОДЕЛЬ})
    check("показ: поле model старше name, чужая модель — 404", код_пары == 404, str(код_пары))
    код_схемы, _, _ = await стенд.запрос("GET", "/v1/models", схема="bearer ")
    check("схема bearer строчными и с двумя пробелами принята", код_схемы == 200, str(код_схемы))
    код_иной, _, _ = await стенд.запрос("GET", "/v1/models", схема="Basic")
    check("парная: схема Basic с верным токеном — 401", код_иной == 401, str(код_иной))

    print("\n# Обрыв потока и сбой службы")
    check("дочитанный поток: признака обрыва нет, соединение со службой закрыто",
          next(строка for строка in стенд.строки() if строка["код"] == 200 and строка["путь"] == "/v1/chat/completions")["оборван"] is False
          and all(поток.закрыт for поток in стенд.служба.потоки))
    медленный = Стенд(каталог, служба=Служба(пауза=0.05))
    _, _, куски_обрыва = await медленный.запрос("POST", "/v1/chat/completions", чат(), бросить_после=1)
    await asyncio.sleep(0.2)
    чаты_обрыва = [строка for строка in медленный.строки() if строка["путь"] == "/v1/chat/completions"]
    check("предусловие: клиент бросил чтение, не дочитав три куска", len(куски_обрыва) < len(КУСКИ), str(len(куски_обрыва)))
    check("клиент бросил чтение: соединение со службой закрыто", медленный.служба.потоки[-1].закрыт)
    check("клиент бросил чтение: строка журнала одна и несёт признак обрыва",
          len(чаты_обрыва) == 1 and чаты_обрыва[0]["оборван"] is True, str(чаты_обрыва))
    сбойный = Стенд(каталог, служба=Служба(код_чата=500))
    код_сбоя, _, куски_сбоя = await сбойный.запрос("POST", "/v1/chat/completions", чат())
    check("код ошибки службы уходит клиенту как есть и пишется в журнал",
          код_сбоя == 500 and "сбой службы" in b"".join(куски_сбоя).decode() and сбойный.строки()[-1]["код"] == 500, str(код_сбоя))

    for путь, список, поле in [("/v1/models", "data", "id"), ("/api/tags", "models", "name"), ("/api/ps", "models", "name")]:
        код_списка, _, куски_списка = await стенд.запрос("GET", путь)
        имена = [запись[поле] for запись in json.loads(b"".join(куски_списка))[список]]
        check(f"{путь} сужен до разрешённых моделей", код_списка == 200 and имена == [МОДЕЛЬ], str(имена))

    лежит = Стенд(каталог, служба=Служба(отказ=True))
    код_503, _, куски_503 = await лежит.запрос("POST", "/v1/chat/completions", чат())
    check("служба не принимает соединение — 503 upstream_unavailable",
          код_503 == 503 and признак(куски_503) == "upstream_unavailable", str(код_503))
    check("при молчащей службе окно неизвестно, строка журнала это называет",
          лежит.строки()[-1]["окно"] is None and лежит.строки()[-1]["код"] == 503, str(лежит.строки()[-1]))

    print("\n# Шаг 3: частота")
    частый = Стенд(каталог, частота=2)
    коды = [(await частый.запрос("POST", "/v1/chat/completions", чат()))[0] for _ in range(2)]
    код_429, шапка_429, куски_429 = await частый.запрос("POST", "/v1/chat/completions", чат())
    check("предусловие: два запроса при частоте 2 прошли", коды == [200, 200], str(коды))
    check("третий подряд — 429 с Retry-After 30",
          код_429 == 429 and шапка_429.get("retry-after") == "30" and признак(куски_429) == "rate_limit_exceeded",
          f"{код_429} {шапка_429}")
    check("отказ по частоте до службы не дошёл", len(частый.служба.чаты()) == 2, str(len(частый.служба.чаты())))
    код_пети, _, _ = await частый.запрос("POST", "/v1/chat/completions", чат(), токен="токен-пети")
    check("ведро другого клиента полно", код_пети == 200, str(код_пети))
    коды_чтения = [(await частый.запрос("GET", "/v1/models"))[0] for _ in range(4)]
    check("чтение списка моделей жетон не тратит и частотой не держится", коды_чтения == [200] * 4, str(коды_чтения))
    частый.часы.сейчас += 29
    код_рано, шапка_рано, _ = await частый.запрос("POST", "/v1/chat/completions", чат())
    check("через 29 с жетона ещё нет, ждать 1 с", (код_рано, шапка_рано.get("retry-after")) == (429, "1"), f"{код_рано} {шапка_рано}")
    частый.часы.сейчас += 1
    код_после, _, _ = await частый.запрос("POST", "/v1/chat/completions", чат())
    check("парная: через 30 с запрос проходит", код_после == 200, str(код_после))
    код_отказа, _, _ = await частый.запрос("POST", "/v1/chat/completions", чат(), токен="токен-васи")
    частый.часы.сейчас += 30
    код_жетона, _, _ = await частый.запрос("POST", "/v1/chat/completions", чат())
    check("запрос с чужим токеном жетонов не тратит", (код_отказа, код_жетона) == (401, 200), f"{код_отказа} {код_жетона}")

    print("\n# Шаг 3: окно")
    оконный = Стенд(каталог)
    код_малого, _, _ = await оконный.запрос("POST", "/v1/chat/completions", чат("aaa"))
    вес_малого = оконный.строки()[-1]["вес"]
    check("малый запрос проходит, окно и вес в журнале",
          код_малого == 200 and оконный.строки()[-1]["окно"] == ОКНО and 3 < вес_малого < ОКНО, str(оконный.строки()[-1]))
    код_десяти, _, _ = await оконный.запрос("POST", "/v1/chat/completions", чат("a" * 13))
    check("словарь модели в деле: десять лишних «a» — десять токенов", оконный.строки()[-1]["вес"] - вес_малого == 10,
          str(оконный.строки()[-1]["вес"] - вес_малого))
    запас = ОКНО - вес_малого
    код_впритык, _, _ = await оконный.запрос("POST", "/v1/chat/completions", чат("aaa", max_tokens=запас))
    код_сверх, _, куски_сверх = await оконный.запрос("POST", "/v1/chat/completions", чат("aaa", max_tokens=запас + 1))
    check("вход + выход ровно в окно — проходит", код_впритык == 200, str(код_впритык))
    check("парная: на один токен больше — 400 context_length_exceeded",
          код_сверх == 400 and признак(куски_сверх) == "context_length_exceeded", str(код_сверх))
    код_иного, _, _ = await оконный.запрос("POST", "/v1/chat/completions", чат("aaa", max_completion_tokens=запас + 1))
    check("выход под именем max_completion_tokens считается так же", код_иного == 400, str(код_иного))
    до_длинного = len(оконный.служба.чаты())
    код_длинного, _, куски_длинного = await оконный.запрос("POST", "/v1/chat/completions", чат("a" * (запас + 4)))
    check("вход длиннее окна — 400, до службы не дошёл",
          код_длинного == 400 and признак(куски_длинного) == "context_length_exceeded"
          and len(оконный.служба.чаты()) == до_длинного, str(код_длинного))
    код_вида, _, куски_вида = await оконный.запрос("POST", "/v1/chat/completions", {"model": МОДЕЛЬ, "messages": "aaa"})
    check("реплики не списком — 400 invalid_request", код_вида == 400 and признак(куски_вида) == "invalid_request", str(код_вида))
    показы = [запрос for запрос in оконный.служба.запросы if запрос.url.path == "/api/show"]
    check("окно и словарь спрошены у службы один раз на все запросы", len(показы) == 2, str(len(показы)))

    без_словаря = Стенд(каталог, служба=Служба(словарь=False))
    код_без_словаря, _, _ = await без_словаря.запрос("POST", "/v1/chat/completions", чат("a" * 1000))
    check("служба словаря не дала: проверка окна пропущена, запрос ушёл службе",
          код_без_словаря == 200 and без_словаря.строки()[-1]["окно"] is None, str(без_словаря.строки()[-1]))

    print("\n# Настройки из окружения")
    основа = {"LLM_TOKEN_mac": "t1", "LLM_MODELS": "m1, m2", "LLM_RATE_PER_MINUTE": "12",
              "LLM_GATEWAY_LOG": str(каталог / "ж.jsonl")}  # fmt: skip
    настройки = gateway.Settings.from_env(основа)
    check("настройки читаются: токен → клиент, модели, частота",
          настройки.tokens == {"t1": "mac"} and настройки.models == {"m1", "m2"} and настройки.rate_per_minute == 12)
    for имя in основа:
        try:
            gateway.Settings.from_env({ключ: значение for ключ, значение in основа.items() if ключ != имя})
            отказ = ""
        except SystemExit as выход:
            отказ = str(выход)
        check(f"без {имя} шлюз не стартует и называет причину", "шлюз не стартует" in отказ, отказ)


def main() -> int:
    with tempfile.TemporaryDirectory() as каталог:
        asyncio.run(проверки(Path(каталог)))
    if ПРОВАЛЫ:
        print(f"\nПровалено: {len(ПРОВАЛЫ)}")
        for имя in ПРОВАЛЫ:
            print(f"  - {имя}")
        return 1
    print("\nВсе проверки пройдены")
    return 0


if __name__ == "__main__":
    sys.exit(main())
