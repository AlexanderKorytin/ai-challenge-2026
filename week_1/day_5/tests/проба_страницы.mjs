// Проба страницы опыта: поведение `models/page.html` без браузера и без единого
// обращения к API.
//
// Браузера в работе нет, а проверять надо именно поведение — что нарисовано в клетке,
// когда замера не было, закрывается ли поток по концу прогона, останавливается ли опрос
// сводки на время прогона. Поэтому здесь заведена подмена того немногого, что страница
// требует от браузера: `document` на десяток методов, `fetch` с заготовленными ответами
// и `EventSource`, в который события кладутся руками. Скрипт страницы вынимается из
// разметки как есть и исполняется в отдельном окружении (`node:vm`) поверх этой подмены —
// проверяется тот самый код, который уйдёт в браузер, а не его пересказ.
//
// Запуск (из каталога дня):  node tests/проба_страницы.mjs
//
// Ни одного обращения к DeepSeek здесь нет и быть не может: маршрут `/run` подменён,
// платных вызовов проба не делает.
import fs from "node:fs";
import vm from "node:vm";
import { fileURLToPath } from "node:url";
import path from "node:path";

// Путь считается от самого файла пробы, а не от рабочего каталога: проба обязана
// проходить и из каталога дня, и из корня хранилища, и из любого другого места.
const каталогПробы = path.dirname(fileURLToPath(import.meta.url));
const путь = path.join(каталогПробы, "..", "models", "page.html");
const разметка = fs.readFileSync(путь, "utf-8");
const код = разметка.match(/<script>([\s\S]*)<\/script>/)[1];
const идентификаторы = [...разметка.matchAll(/id="([^"]+)"/g)].map((м) => м[1]);

class Текст {
  constructor(т) { this.value = String(т); }
  get textContent() { return this.value; }
}

class Узел {
  constructor(тег) {
    this.tag = тег;
    this.children = [];
    this.className = "";
    this.hidden = false;
    this.disabled = false;
    this.listeners = {};
  }
  append(...узлы) {
    for (const у of узлы) this.children.push(typeof у === "string" ? new Текст(у) : у);
  }
  set textContent(v) {
    this.children = [];
    if (v !== "" && v !== null && v !== undefined) this.children.push(new Текст(v));
  }
  get textContent() { return this.children.map((c) => c.textContent).join(""); }
  addEventListener(имя, обработчик) {
    (this.listeners[имя] = this.listeners[имя] || []).push(обработчик);
  }
  // Поиск по классу вглубь — только для проверок.
  найти(класс) {
    const итог = [];
    const обойти = (у) => {
      if (у instanceof Узел) {
        if ((" " + у.className + " ").includes(" " + класс + " ")) итог.push(у);
        у.children.forEach(обойти);
      }
    };
    this.children.forEach(обойти);
    return итог;
  }
}

const реестр = new Map(идентификаторы.map((id) => [id, new Узел("div")]));
const document = {
  createElement: (т) => new Узел(т),
  createTextNode: (т) => new Текст(т),
  getElementById: (id) => реестр.get(id) || null,
};

// --- подставные ответы сервера ------------------------------------------------
const настройки = {
  cells: [
    { id: "flash_plain", title: "flash без рассуждений", role: "слабая", model: "deepseek-v4-flash", thinking: false },
    { id: "flash_think", title: "flash с рассуждениями", role: "средняя", model: "deepseek-v4-flash", thinking: true },
    { id: "pro_plain", title: "pro без рассуждений", role: "контрольная", model: "deepseek-v4-pro", thinking: false },
    { id: "pro_think", title: "pro с рассуждениями", role: "сильная", model: "deepseek-v4-pro", thinking: true },
  ],
  repeats: 5,
  // Настоящий текст вопроса дня, дословно: страница обязана показывать длинный вопрос
  // целиком, а не обрезать его.
  question: "Я сижу дома на 19 этаже мне нужно помыть машину. Она на подземной парковке дома. "
    + "Автомойка находится в 100 метрах от дома. Погода отличная, а на дорогах большие пробки "
    + "и ехать нужно через развязку 4 километра. Как лучше добраться — пешком или на машине",
};

const пустаяСводка = {
  summary: {
    base: "flash_plain",
    cells: настройки.cells.map((к) => ({
      cell_id: к.id, title: к.title, role: к.role, model: к.model, thinking: к.thinking,
      n: 0, errors: 0,
      ttft_ms: { median: null, min: null, max: null },
      ttfa_ms: { median: null, min: null, max: null },
      total_ms: { median: null, min: null, max: null },
      tokens_per_sec: { median: null, min: null, max: null },
      tokens: { cache_hit: 0, cache_miss: 0, output: 0, reasoning: 0 },
      cost: 0, cost_ratio: null, time_ratio: null,
    })),
  },
  verdict: null,
};

const полнаяСводка = JSON.parse(JSON.stringify(пустаяСводка));
полнаяСводка.summary.cells[0].n = 5;
полнаяСводка.summary.cells[0].ttft_ms = { median: 310, min: 290, max: 360 };
полнаяСводка.summary.cells[0].ttfa_ms = { median: 420, min: 390, max: 480 };
полнаяСводка.summary.cells[0].total_ms = { median: 1900, min: 1700, max: 2400 };
полнаяСводка.summary.cells[0].tokens_per_sec = { median: 168.9, min: 150, max: 180 };
полнаяСводка.summary.cells[0].tokens = { cache_hit: 0, cache_miss: 200, output: 1250, reasoning: 0 };
полнаяСводка.summary.cells[0].cost = 0.0026;
полнаяСводка.summary.cells[0].cost_ratio = 1;
полнаяСводка.summary.cells[0].time_ratio = 1;
полнаяСводка.summary.cells[3].n = 4;
полнаяСводка.summary.cells[3].errors = 1;
// Клетка с рассуждениями: первый токен приходит быстро, а первое слово ответа — только
// после всего рассуждения. Ради этой разницы столбцы и стоят рядом.
полнаяСводка.summary.cells[3].ttft_ms = { median: 1400, min: 1200, max: 1900 };
полнаяСводка.summary.cells[3].ttfa_ms = { median: 7200, min: 6100, max: 9300 };
полнаяСводка.summary.cells[3].total_ms = { median: 9800, min: 8000, max: 12000 };
полнаяСводка.summary.cells[3].tokens = { cache_hit: 40, cache_miss: 160, output: 4200, reasoning: 3100 };
полнаяСводка.summary.cells[3].cost = 0.041;
полнаяСводка.summary.cells[3].cost_ratio = 15.77;
полнаяСводка.summary.cells[3].time_ratio = 5.16;

let сводкаСейчас = пустаяСводка;
const обращения = [];
function fetch(адрес) {
  обращения.push(адрес);
  const тело = адрес === "/settings" ? настройки : сводкаСейчас;
  return Promise.resolve({ ok: true, status: 200, json: () => Promise.resolve(тело) });
}

const потоки = [];
class EventSource {
  constructor(адрес) { this.url = адрес; this.закрыт = false; потоки.push(this); }
  close() { this.закрыт = true; }
}

const контекст = vm.createContext({
  document, fetch, EventSource, setInterval, clearInterval, console, Number, Math, isFinite,
});
vm.runInContext(код, контекст, { filename: "page.js" });

const дать = () => new Promise((r) => setImmediate(r));
const прочитать = (выражение) => vm.runInContext(выражение, контекст);
const провалы = [];
function проверить(имя, условие, подсказка) {
  if (условие) console.log("  ок   " + имя);
  else { провалы.push(имя); console.log("  СБОЙ " + имя + (подсказка ? " — " + подсказка : "")); }
}

const тело = реестр.get("тело");
const вывод = реестр.get("вывод");
const примечание = реестр.get("примечание-сводки");
const кнопка = реестр.get("запустить");

await дать(); await дать(); await дать();

console.log("\n1. пустой журнал, разбора нет");
проверить("вывод: «прогон ещё не делался»", вывод.textContent === "прогон ещё не делался", вывод.textContent);
проверить("примечание о пустом журнале видно", примечание.hidden === false);
проверить("вопрос взят от сервера", реестр.get("текст-вопроса").textContent === настройки.question);
проверить("шапка: роль и клетка", реестр.get("шапка").найти("роль")[0].textContent === "слабая"
  && реестр.get("шапка").найти("клетка")[0].textContent === "flash без рассуждений");
проверить("четыре переключателя вида", реестр.get("виды").children.length === 4);
проверить("кнопка доступна", кнопка.disabled === false);

console.log("\n2. нажатие: клетки ожидания до первых номеров");
кнопка.listeners.click[0]();
const обращенийДоПрогона = обращения.length;
проверить("поток открыт на /run без параметров", потоки.length === 1 && потоки[0].url === "/run");
проверить("пять строк ожидания", тело.children.length === 5);
проверить("клетки «ждём ответа…»", тело.найти("ожидание").length === 20);
проверить("кнопка заблокирована", кнопка.disabled === true && кнопка.textContent === "идёт запрос…");

console.log("\n3. опрос сводки на время прогона остановлен");
сводкаСейчас = полнаяСводка;
await new Promise((r) => setTimeout(r, 3300));
проверить("к /summary за 3.3 с не ходили", обращения.length === обращенийДоПрогона,
  "обращений стало " + обращения.length);

console.log("\n4. события клеток");
const событие = (о) => потоки[0].onmessage({ data: JSON.stringify(о) });
событие({
  kind: "cell", cell_id: "flash_plain", repetition: 1, answer: "Пешком — сто метров.",
  usage: { prompt_tokens: 40, completion_tokens: 250, total_tokens: 290,
    prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 40,
    completion_tokens_details: { reasoning_tokens: 0 } },
  ttft_ms: 310, ttfa_ms: 420, total_ms: 1900, tokens_per_sec: 168.9,
  cost: 0.000521, finish_reason: "stop", status: "ok", error: null,
});
событие({
  kind: "cell", cell_id: "flash_think", repetition: 1, answer: "Пешком.",
  usage: { prompt_tokens: 40, completion_tokens: 900, total_tokens: 940,
    prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 40, reasoning_tokens: 640 },
  ttft_ms: null, ttfa_ms: null, total_ms: 7100, tokens_per_sec: null,
  cost: 0.0000004, finish_reason: "stop", status: "ok", error: null,
});
событие({
  kind: "cell", cell_id: "pro_plain", repetition: 1, answer: "Пеш",
  usage: { prompt_tokens: 40, completion_tokens: 3, total_tokens: 43,
    prompt_cache_hit_tokens: 0, prompt_cache_miss_tokens: 40 },
  ttft_ms: 280, ttfa_ms: null, total_ms: 900, tokens_per_sec: null,
  cost: 0.00001, finish_reason: null, status: "error", error: "обрыв связи с API",
});
проверить("строка прогона получила номер", тело.children[0].children[0].textContent === "прогон 1");
проверить("недошедшая клетка строки 1 всё ещё ждёт", тело.children.length === 5
  && тело.найти("ожидание").length === 1 + 4 * 4);

const клеткаСбоя = тело.children[0].children[3];
проверить("клетка сбоя окрашена", клеткаСбоя.className === "сбой");
проверить("текст ошибки в виде «ответ»", клеткаСбоя.textContent.includes("обрыв связи с API"));
проверить("ответ показан и при сбое", клеткаСбоя.textContent.includes("Пеш"));
проверить("без повторов «попыток» не пишется",
  !тело.children[0].children[1].textContent.includes("попыток"));

console.log("\n5. повторная попытка и вовсе не пришедший ответ");
событие({
  kind: "cell", cell_id: "pro_think", repetition: 1, answer: "",
  usage: { prompt_tokens: 40, completion_tokens: 0, total_tokens: 40,
    prompt_cache_hit_tokens: 40, prompt_cache_miss_tokens: 0,
    completion_tokens_details: { reasoning_tokens: 0 } },
  ttft_ms: null, ttfa_ms: null, total_ms: 5200, tokens_per_sec: null,
  attempts: 2, cost: 0.0031, finish_reason: null, status: "error",
  error: "пустой ответ: модель не прислала ни одного токена",
});
const пустая = () => тело.children[0].children[4];
проверить("строка прогона заполнилась целиком", тело.найти("ожидание").length === 4 * 4);
проверить("клетка окрашена как сбой", пустая().className === "сбой");
проверить("текст про пустой ответ виден", пустая().textContent.includes("пустой ответ"));
проверить("«попыток: 2» в виде «ответ»", пустая().textContent.includes("попыток: 2"));

console.log("\n6. вид «время»: null — «нет замера», а не ноль");
const переключить = (номер) => {
  const кружок = реестр.get("виды").children[номер].children[0];
  кружок.listeners.change[0]();
};
переключить(1);
const времяFlashThink = тело.children[0].children[2].textContent;
проверить("до первого токена — «нет замера»", времяFlashThink.includes("до первого токенанет замера"), времяFlashThink);
проверить("токенов в секунду — «нет замера»", времяFlashThink.includes("токенов в секундунет замера"));
проверить("нуля нет вовсе", !/нет замера/.test("") && !времяFlashThink.includes("0 мс"));
проверить("полное время в секундах", времяFlashThink.includes("7.1 с"), времяFlashThink);
проверить("до первого токена в миллисекундах", тело.children[0].children[1].textContent.includes("310 мс"));
проверить("ошибка видна и в виде «время»", тело.children[0].children[3].textContent.includes("обрыв связи с API"));
проверить("«попыток: 2» в виде «время»", пустая().textContent.includes("попыток: 2"));

console.log("\n7. вид «токены»: рассуждения отдельной строкой, обе формы поля");
переключить(2);
проверить("вложенная форма reasoning_tokens", тело.children[0].children[1].textContent.includes("из них рассуждения0"));
проверить("плоская форма reasoning_tokens", тело.children[0].children[2].textContent.includes("из них рассуждения640"));
проверить("разбивка входа", тело.children[0].children[1].textContent.includes("из промежуточного хранилища0")
  && тело.children[0].children[1].textContent.includes("мимо хранилища40"));
проверить("«попыток: 2» в виде «токены»", пустая().textContent.includes("попыток: 2"));

console.log("\n8. вид «деньги»: не ноль и не научная запись");
переключить(3);
// Берём именно узел цены, а не текст клетки целиком: рядом стоит служебная строка.
const деньгиОбычные = тело.children[0].children[1].найти("значение")[0].textContent;
const деньгиКрошечные = тело.children[0].children[2].найти("значение")[0].textContent;
проверить("обычная цена шестью знаками", деньгиОбычные === "0.000521 $", деньгиОбычные);
проверить("крошечная цена не ноль", деньгиКрошечные === "0.000000400 $", деньгиКрошечные);
проверить("научной записи нет", !деньгиОбычные.includes("e") && !деньгиКрошечные.includes("e"));
// Цена в журнале учитывает лишь удачную попытку, поэтому у клетки с повтором она
// занижена — «попыток: 2» рядом с ценой и есть предупреждение об этом.
проверить("«попыток: 2» рядом с ценой", пустая().textContent.includes("попыток: 2"));
проверить("клетка без повторов цену не оговаривает",
  !тело.children[0].children[1].textContent.includes("попыток"));

console.log("\n9. смена вида к серверу не ходит");
проверить("обращений не прибавилось", обращения.length === обращенийДоПрогона,
  обращения.slice(обращенийДоПрогона).join(", "));

console.log("\n10. конец прогона");
потоки[0].onmessage({ data: JSON.stringify({ kind: "done", collected: 5 }) });
проверить("поток закрыт со своей стороны", потоки[0].закрыт === true);
проверить("кнопка снова доступна", кнопка.disabled === false && кнопка.textContent === "сделать запрос");
проверить("строки ожидания убраны", тело.children.length === 1 && тело.найти("ожидание").length === 0);
await дать(); await дать(); await дать();
проверить("сводка запрошена сразу", обращения.length > обращенийДоПрогона);

console.log("\n11. сводка по журналу и разбор");
const телоСводки = реестр.get("тело-сводки");
проверить("четыре строки сводки", телоСводки.children.length === 4);
const базовая = телоСводки.children[0].textContent;
проверить("базовая клетка помечена", базовая.includes("(базовая)"), базовая);
проверить("медиана полного времени с разбросом", базовая.includes("1.9 с (1.7 с–2.4 с)"), базовая);
проверить("до первого токена в сводке", базовая.includes("310 мс (290 мс–360 мс)"), базовая);
проверить("до первого слова ответа в сводке", базовая.includes("420 мс (390 мс–480 мс)"), базовая);
const сильная = телоСводки.children[3].textContent;
проверить("отношения показаны", сильная.includes("15.77×") && сильная.includes("5.16×"), сильная);
проверить("рассуждения выделены", сильная.includes("3100"));
// Первый токен через 1.4 с, первое слово ответа только через 7.2 с — ради этой разницы
// два столбца и стоят рядом; одним «временем ответа» её не описать.
проверить("три времени клетки с рассуждениями различимы",
  сильная.includes("1.4 с (1.2 с–1.9 с)") && сильная.includes("7.2 с (6.1 с–9.3 с)")
  && сильная.includes("9.8 с (8.0 с–12.0 с)"), сильная);
const заголовкиСводки = реестр.get("шапка-сводки").children[0];
проверить("двенадцать столбцов сводки", заголовкиСводки.children.length === 12);
проверить("подписи времён не спутать",
  заголовкиСводки.children[3].textContent.startsWith("до первого токена")
  && заголовкиСводки.children[4].textContent.startsWith("до первого слова ответа")
  && заголовкиСводки.children[5].textContent.startsWith("полное время"));
проверить("у каждого времени пояснение «медиана (мин–макс)»",
  заголовкиСводки.найти("пояснение").length === 4);
const средняя = телоСводки.children[1].textContent;
проверить("пустая медиана — прочерк", средняя.includes("—"), средняя);
проверить("примечание о пустом журнале убрано", примечание.hidden === true);
проверить("вывод: «прогон сделан, вывод ещё не написан»",
  вывод.textContent === "прогон сделан, вывод ещё не написан", вывод.textContent);

console.log("\n12. разбор написан");
сводкаСейчас = JSON.parse(JSON.stringify(полнаяСводка));
сводкаСейчас.verdict = "## Вывод\n\nСлабая клетка справилась.";
await new Promise((r) => setTimeout(r, 3300));
проверить("текст разбора показан как есть",
  вывод.textContent === "## Вывод\n\nСлабая клетка справилась.", вывод.textContent);
проверить("опрос сам возобновился после прогона", обращения.length > обращенийДоПрогона + 1);

console.log("\n13. повторное нажатие дописывает прогоны, а не перезаписывает");
// Потеря собранного — это потеря уже оплаченных ответов: за прогоны 1–5 деньги списаны,
// и второе нажатие обязано дописать 6–10, а не начать таблицу заново.
переключить(0);
кнопка.listeners.click[0]();
проверить("строка «прогон 1» осталась на месте",
  тело.children[0].children[0].textContent === "прогон 1");
проверить("её ответ не стёрт",
  тело.children[0].children[1].textContent.includes("Пешком — сто метров."));
проверить("к ней дописались пять строк ожидания", тело.children.length === 6);

// Заодно случай, на котором страница расходилась с сервером: вложенное поле пришло
// пустым, а плоское заполнено. Выбирать надо по годности значения, а не по наличию
// ключа, иначе клетка покажет ноль, а сводка внизу той же страницы — настоящее число.
потоки[1].onmessage({ data: JSON.stringify({
  kind: "cell", cell_id: "flash_think", repetition: 6, answer: "На машине.",
  usage: { prompt_tokens: 40, completion_tokens: 800, total_tokens: 840,
    prompt_cache_hit_tokens: 40, prompt_cache_miss_tokens: 0,
    completion_tokens_details: { reasoning_tokens: null }, reasoning_tokens: 512 },
  ttft_ms: 1400, ttfa_ms: 7200, total_ms: 9800, tokens_per_sec: 81.6,
  attempts: 1, cost: 0.0021, finish_reason: "stop", status: "ok", error: null,
}) });
проверить("появилась строка «прогон 6»",
  тело.children[1].children[0].textContent === "прогон 6", тело.children[1].children[0].textContent);
проверить("прогон 1 по-прежнему первый и целый",
  тело.children[0].children[0].textContent === "прогон 1"
  && тело.children[0].children[1].textContent.includes("Пешком — сто метров."));
переключить(2);
проверить("негодное вложенное поле не затирает плоское",
  тело.children[1].children[2].textContent.includes("из них рассуждения512"),
  тело.children[1].children[2].textContent);
потоки[1].onmessage({ data: JSON.stringify({ kind: "done", collected: 10 }) });
проверить("второй поток закрыт", потоки[1].закрыт === true);
проверить("обе строки прогонов остались", тело.children.length === 2);

console.log("\n14. сбой прогона сообщением, а не клеткой");
кнопка.listeners.click[0]();
потоки[2].onmessage({ data: JSON.stringify({ kind: "error", message: "ключ не задан: запустите myharness и выполните /auth" }) });
проверить("сообщение показано", реестр.get("сообщение").hidden === false
  && реестр.get("сообщение").textContent.includes("ключ не задан"));
проверить("третий поток закрыт", потоки[2].закрыт === true);

console.log(провалы.length ? "\nПРОВАЛЕНО: " + провалы.length : "\nвсе проверки прошли");
process.exit(провалы.length ? 1 : 0);
