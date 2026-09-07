"""Ведёт myharness через псевдотерминал: задаёт вопросы, ждёт ответы, выходит.

Нужен, чтобы проверить живой разговор без участия человека. Проверяем не картинку на
экране, а след в журнале прогонов: что ушло в модель на каждом ходу.
"""
import os, pty, select, sys, time

КОМАНДЫ = [
    ("/profile разговор", 3),
    ("функция сортировки пузырьком на Kotlin", 45),
    ("а короче?", 45),          # бессмысленно без памяти — агент обязан понять, о чём речь
    ("/clear", 3),
    ("а короче?", 45),          # память пуста — агент не поймёт, о чём речь
    ("/exit", 3),
]

окружение = dict(os.environ)
окружение["MYHARNESS_PROFILES"] = os.path.expanduser("~/challenge/w2d1/week_2/day_1/profiles")
окружение["MYHARNESS_JOURNAL"] = os.path.abspath("разговор-журнал.jsonl")
окружение["TERM"] = "xterm-256color"

главный, подчинённый = pty.openpty()
os.set_blocking(главный, False)
процесс = os.fork()
if процесс == 0:
    os.setsid(); os.dup2(подчинённый, 0); os.dup2(подчинённый, 1); os.dup2(подчинённый, 2)
    os.close(главный); os.close(подчинённый)
    os.execvpe("uv", ["uv", "run", "--project",
                      os.path.expanduser("~/challenge/w2d1/week_1/day_1"), "myharness"], окружение)

os.close(подчинённый)

def выкачать(секунд):
    конец = time.time() + секунд
    while time.time() < конец:
        готово, _, _ = select.select([главный], [], [], 0.2)
        if готово:
            try:
                if not os.read(главный, 65536):
                    return
            except OSError:
                return

выкачать(8)  # дать приложению подняться
for текст, пауза in КОМАНДЫ:
    print(f"→ {текст}", flush=True)
    os.write(главный, (текст + "\r").encode())
    выкачать(пауза)
os.waitpid(процесс, 0)
print("прогон завершён")
