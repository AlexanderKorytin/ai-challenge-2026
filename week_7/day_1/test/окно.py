"""Ведёт myharness в псевдотерминале: набирает строки, ждёт, пока журнал прирастёт записью."""
import os, pty, select, sys, time, json, re
журнал = os.environ["MYHARNESS_JOURNAL"]
модель, файл = sys.argv[1:]
строки = [с for с in open(файл).read().splitlines() if с.strip()]
def записей():
    try: return sum(1 for _ in open(журнал))
    except FileNotFoundError: return 0
pid, fd = pty.fork()
if pid == 0:
    os.execvp("uv", ["uv", "run", "--project", os.path.expanduser("~/challenge/main/myharness"), "myharness", "--model", модель])
экран = b""
def читать(секунд):
    global экран
    конец = time.time() + секунд
    while time.time() < конец:
        r, _, _ = select.select([fd], [], [], 0.2)
        if r:
            try: экран += os.read(fd, 65536)
            except OSError: return
читать(6)
for строка in строки:
    было = записей()
    os.write(fd, строка.encode()); читать(0.5); os.write(fd, b"\r")
    срок = time.time() + 240
    while записей() == было and time.time() < срок: читать(1)
    читать(3)
os.write(fd, b"/tokens\r"); читать(2)
os.write(fd, b"/exit\r"); читать(3)
текст = re.sub(rb"\x1b\[[0-9;?]*[a-zA-Z]", b"", экран).decode("utf-8", "replace")
open("экран-" + re.sub(r"[/:]", "-", модель) + ".txt", "w").write(текст)
