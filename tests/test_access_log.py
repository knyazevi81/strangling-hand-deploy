"""Разбор журнала соединений Xray агентом: python3 tests/test_access_log.py"""
import importlib.util
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["SAVEBIT_ROOT"] = tempfile.mkdtemp()
os.environ["SAVEBIT_SERVICE_GROUP"] = "none"
spec = importlib.util.spec_from_file_location("agent", os.path.join(HERE, "..", "agent", "savebit_agent.py"))
a = importlib.util.module_from_spec(spec)
spec.loader.exec_module(a)

LINES = [
    # так пишет Xray 26: с email и без, с «tcp:» перед адресом и без, «>>» и «->»
    "2026/11/21 21:27:18.0 from 85.140.12.34:44654 accepted tcp:rr3---sn-abc.googlevideo.com:443 [in-reality >> exits] email: u1",
    "2026/11/21 21:27:18.0 from tcp:85.140.12.34:44655 accepted tcp:www.youtube.com:443 [in-xhttp -> exits] email: u1",
    "2026/11/21 21:27:18.0 from [2a00:1fa0::5]:5555 accepted udp:api.vk.com:443 [in-hy2 >> exits] email: u1",
    "2026/11/21 21:27:18.0 from 10.0.0.1:1 accepted tcp:bbc.co.uk:443 [in-xhttp >> direct] email: relay:in-msk-1",
    "2026/11/21 21:27:18.0 from 95.165.1.2:3 accepted tcp:[2001:db8::1]:443 [in-xhttp >> direct] email: u2",
    "2026/11/21 21:27:18.0 from 95.165.1.2:3 accepted tcp:news.bbc.co.uk:443 [in-xhttp >> direct] email: u2",
    "2026/11/21 21:27:18.0 from tcp:127.0.0.1:50014 accepted tcp:example.com:443 [in-test -> block]",
    "мусор",
]
fails = 0


def check(name, ok):
    global fails
    print(("  ok    " if ok else "  FAIL  ") + name)
    fails += 0 if ok else 1


r = a.parse_access_lines(LINES)
check("только друзья, без relay и без строк без email", set(r) == {"u1", "u2"})
check("IP и IPv6 с счётчиками", r["u1"]["ips"] == {"85.140.12.34": 2, "2a00:1fa0::5": 1})
check("домены схлопнуты до основного", r["u1"]["domains"] == {"googlevideo.com": 1, "youtube.com": 1, "vk.com": 1})
check("двухуровневые зоны и IP-адрес назначения", r["u2"]["domains"] == {"bbc.co.uk": 1})

# чтение с позиции, недописанная строка, обрезка
os.makedirs(a.LOG_DIR, exist_ok=True)
cfg = {"log": {"access": a.ACCESS_LOG}}
state = {}
with open(a.ACCESS_LOG, "w") as f:
    f.write(LINES[0] + "\n" + LINES[1][:20])
first = a.read_access_log(state, cfg)
check("читает только законченные строки", first["lines"] == 1)
with open(a.ACCESS_LOG, "a") as f:
    f.write(LINES[1][20:] + "\n")
second = a.read_access_log(state, cfg)
check("дочитывает строку в следующий раз", second["lines"] == 1 and second["users"]["u1"]["domains"] == {"youtube.com": 1})
check("второй раз без новых строк — пусто", a.read_access_log(state, cfg)["lines"] == 0)
a.ACCESS_LOG_MAX = 10
with open(a.ACCESS_LOG, "a") as f:
    f.write(LINES[2] + "\n")
a.read_access_log(state, cfg)
check("большой журнал обрезается", os.path.getsize(a.ACCESS_LOG) == 0 and state["access_offset"] == 0)
check("сбор выключен — журнал удаляется", a.read_access_log(state, {}) is None and not os.path.exists(a.ACCESS_LOG))
check("плейсхолдер папки журнала", a.substitute_placeholders({"log": {"access": "${LOG_DIR}/access.log"}})["log"]["access"] == a.ACCESS_LOG)

print("Итого: провалено %d" % fails)
sys.exit(1 if fails else 0)
