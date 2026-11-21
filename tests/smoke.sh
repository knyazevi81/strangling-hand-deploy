#!/usr/bin/env bash
# Смоук-тест агента против тестового API, без systemd и без root-путей.
#
#   XRAY_ZIP=/путь/к/Xray-linux-64.zip bash tests/smoke.sh
#
# XRAY_ZIP: архив релиза Xray (xray + geoip.dat + geosite.dat), как на GitHub.
# Проверяет: проверку ключа, установку Xray, применение конфига, повторную
# синхронизацию без перезапуска, плохой конфиг, откат при ошибке запуска,
# замену ключа, отказ в ключе и отчёты.
set -uo pipefail
HERE="$(cd "$(dirname "$0")/.." && pwd)"
XRAY_ZIP="${XRAY_ZIP:?укажи XRAY_ZIP}"
PORT="${MOCK_PORT:-18765}"
API="http://127.0.0.1:$PORT"

WORK="$(mktemp -d)"; export SAVEBIT_ROOT="$WORK/root"
export SAVEBIT_SYSTEMCTL="$HERE/tests/fake-systemctl.sh"
export SAVEBIT_SERVICE_GROUP="nogroup-savebit-test"
AGENT=(python3 "$HERE/agent/savebit_agent.py")
FILES="$WORK/files"; mkdir -p "$FILES" "$SAVEBIT_ROOT/etc/savebit"
cp "$XRAY_ZIP" "$FILES/Xray-linux-64.zip"
SHA="$(sha256sum "$FILES/Xray-linux-64.zip" | cut -d' ' -f1)"
VER="$(python3 - "$XRAY_ZIP" <<'PY'
import sys, zipfile, re, subprocess, tempfile, os
d = tempfile.mkdtemp(); z = zipfile.ZipFile(sys.argv[1]); z.extract("xray", d)
os.chmod(d + "/xray", 0o755)
print(re.search(r"Xray (\d+\.\d+\.\d+)", subprocess.run([d + "/xray", "version"], capture_output=True, text=True).stdout).group(1))
PY
)"

PASS=0; FAIL=0
ok()   { echo "  ✔ $*"; PASS=$((PASS+1)); }
bad()  { echo "  ✘ $*"; FAIL=$((FAIL+1)); }
step() { echo; echo "── $*"; }
xray_pid() { cat "$SAVEBIT_ROOT/run/xray.pid" 2>/dev/null; }
state() { python3 -c "import json,sys; print(json.load(open('$SAVEBIT_ROOT/var/lib/savebit/state.json')).get('$1'))"; }

# Конфиг выхода на высоких портах, чтобы не мешать системе
make_config() { # make_config ИМЯ_ФАЙЛА [ЛИШНИЙ_КЛИЕНТ]
  python3 - "$HERE/examples/exit.json" "$1" "${2:-}" <<'PY'
import json, sys
c = json.load(open(sys.argv[1]))
for i in c["inbounds"]:
    i["port"] = 24443
if sys.argv[3]:
    c["inbounds"][0]["settings"]["clients"].append({"id": "f4a45582-ba2a-4590-89c5-8eed88395832", "email": sys.argv[3]})
c["api"]["listen"] = "127.0.0.1:24085"
json.dump(c, open(sys.argv[2], "w"), indent=2)
PY
}
write_nodes() { # write_nodes КОНФИГ КЛЮЧ...
  local cfg="$1"; shift
  python3 - "$WORK/nodes.json" "$cfg" "$FILES" "$VER" "$SHA" "$API" "$@" <<'PY'
import json, sys
out, cfg, files, ver, sha, api, *keys = sys.argv[1:]
json.dump({"files_dir": files, "nodes": [{
    "id": "node-test-1", "name": "out-test-1", "role": "exit", "keys": keys, "interval": 15,
    "xray": {"version": ver, "url": api + "/files/Xray-linux-64.zip", "sha256": sha},
    "tls": [{"name": "hy2", "cn": "www.example.com"}], "config_file": cfg}]}, open(out, "w"), indent=2)
PY
}

make_config "$WORK/exit-v1.json"
write_nodes "$WORK/exit-v1.json" key-one
python3 "$HERE/tools/mock_api.py" --nodes "$WORK/nodes.json" --port "$PORT" --reports-dir "$WORK/reports" 2>"$WORK/mock.log" &
MOCK=$!
cleanup() { SAVEBIT_ROOT="$SAVEBIT_ROOT" "$SAVEBIT_SYSTEMCTL" stop savebit-xray; kill $MOCK 2>/dev/null; [ -n "${BLOCKER:-}" ] && kill "$BLOCKER" 2>/dev/null; }
trap cleanup EXIT
sleep 1
echo "Xray $VER, рабочий каталог $WORK"

step "1. Проверка ключа"
"${AGENT[@]}" check --api "$API" --key wrong-key >/dev/null; rc=$?
[ $rc = 2 ] && ok "чужой ключ отклонён (код 2)" || bad "чужой ключ: код $rc"
out="$("${AGENT[@]}" check --api "$API" --key key-one)"; rc=$?
[ $rc = 0 ] && echo "$out" | grep -q out-test-1 && ok "правильный ключ принят: $out" || bad "правильный ключ: код $rc $out"
"${AGENT[@]}" check --api "http://127.0.0.1:1" --key key-one >/dev/null; rc=$?
[ $rc = 3 ] && ok "недоступный API распознан (код 3)" || bad "недоступный API: код $rc"

printf 'SAVEBIT_API=%s\nSAVEBIT_NODE_KEY=key-one\n' "$API" >"$SAVEBIT_ROOT/etc/savebit/agent.env"

step "2. Первая синхронизация: скачать Xray, создать сертификат, применить конфиг"
"${AGENT[@]}" sync && ok "синхронизация прошла" || bad "синхронизация упала"
v="$("$SAVEBIT_ROOT/opt/savebit/xray/current/xray" version 2>/dev/null | head -1)"
echo "$v" | grep -q "$VER" && ok "Xray установлен: $v" || bad "Xray не установлен"
[ -n "$(xray_pid)" ] && kill -0 "$(xray_pid)" 2>/dev/null && ok "Xray запущен" || bad "Xray не запущен"
[ -f "$SAVEBIT_ROOT/etc/savebit/tls/hy2.crt" ] && ok "сертификат hy2 создан" || bad "сертификата нет"
grep -q "$SAVEBIT_ROOT/etc/savebit/tls/hy2.crt" "$SAVEBIT_ROOT/etc/savebit/xray/config.json" && ok "\${TLS_DIR} подставлен в конфиг" || bad "подстановка \${TLS_DIR} не сработала"
REV1="$(state revision)"; PID1="$(xray_pid)"

step "3. Повторная синхронизация без изменений"
"${AGENT[@]}" sync >/dev/null && ok "прошла" || bad "упала"
[ "$(xray_pid)" = "$PID1" ] && ok "Xray не перезапускался (API ответил 304)" || bad "Xray перезапустился зря"

step "4. В админке добавили друга: новая ревизия"
make_config "$WORK/exit-v2.json" friend-2
write_nodes "$WORK/exit-v2.json" key-one
"${AGENT[@]}" sync >/dev/null && ok "прошла" || bad "упала"
[ "$(state revision)" != "$REV1" ] && ok "ревизия обновилась: $(state revision)" || bad "ревизия не поменялась"
grep -q friend-2 "$SAVEBIT_ROOT/etc/savebit/xray/config.json" && ok "новый друг в конфиге" || bad "друга нет в конфиге"
[ "$(xray_pid)" != "$PID1" ] && kill -0 "$(xray_pid)" 2>/dev/null && ok "Xray перезапущен и работает" || bad "Xray не перезапущен"
REV2="$(state revision)"; PID2="$(xray_pid)"

step "5. API прислал битый конфиг"
python3 - "$WORK/exit-v2.json" "$WORK/exit-bad.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); c["inbounds"][0]["protocol"] = "no-such-protocol"
json.dump(c, open(sys.argv[2], "w"))
PY
write_nodes "$WORK/exit-bad.json" key-one
"${AGENT[@]}" sync >/dev/null && bad "синхронизация не должна была пройти" || ok "синхронизация сообщила об ошибке"
[ "$(state revision)" = "$REV2" ] && ok "осталась прежняя ревизия" || bad "ревизия сменилась на битую"
[ "$(xray_pid)" = "$PID2" ] && kill -0 "$PID2" 2>/dev/null && ok "Xray продолжает работать на старом конфиге" || bad "Xray остановился"
state last_error | grep -q "xray -test" && ok "ошибка записана: проверка xray -test" || bad "ошибка не записана"

step "6. Конфиг проходит проверку, но Xray с ним не стартует (порт занят): откат"
python3 -c "import socket,time; s=socket.socket(); s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1); s.bind(('0.0.0.0', 24444)); s.listen(); time.sleep(600)" &
BLOCKER=$!; sleep 0.5
python3 - "$WORK/exit-v2.json" "$WORK/exit-busy.json" <<'PY'
import json, sys
c = json.load(open(sys.argv[1])); c["inbounds"][0]["port"] = 24444
json.dump(c, open(sys.argv[2], "w"))
PY
write_nodes "$WORK/exit-busy.json" key-one
"${AGENT[@]}" sync >/dev/null && bad "синхронизация не должна была пройти" || ok "синхронизация сообщила об ошибке"
state last_error | grep -q "вернул прежний" && ok "агент вернул прежний конфиг" || bad "отката не было: $(state last_error)"
grep -q '"port": 24443' "$SAVEBIT_ROOT/etc/savebit/xray/config.json" && ok "на диске снова рабочий конфиг" || bad "на диске остался сломанный конфиг"
kill -0 "$(xray_pid)" 2>/dev/null && ok "Xray снова работает" || bad "Xray не работает после отката"
kill $BLOCKER 2>/dev/null; BLOCKER=""

step "7. Ключ перевыпустили в админке"
write_nodes "$WORK/exit-v2.json" key-two
"${AGENT[@]}" sync >/dev/null && bad "старый ключ не должен работать" || ok "старый ключ отклонён"
state last_error | grep -q "rekey" && ok "в ошибке подсказка про rekey" || bad "нет подсказки"
kill -0 "$(xray_pid)" 2>/dev/null && ok "Xray работает, пока ключ не заменили" || bad "Xray упал"
"${AGENT[@]}" rekey wrong-key >/dev/null 2>&1 && bad "неверный новый ключ не должен приниматься" || ok "неверный новый ключ не принят, старый оставлен"
"${AGENT[@]}" rekey key-two >/dev/null && ok "ключ заменён" || bad "rekey не сработал"
grep -q "SAVEBIT_NODE_KEY=key-two" "$SAVEBIT_ROOT/etc/savebit/agent.env" && ok "новый ключ в agent.env" || bad "agent.env не обновлён"
[ "$(stat -c %a "$SAVEBIT_ROOT/etc/savebit/agent.env")" = 600 ] && ok "права agent.env 600" || bad "права agent.env не 600"
"${AGENT[@]}" sync >/dev/null && ok "синхронизация с новым ключом прошла" || bad "с новым ключом не работает"

step "8. Отчёт в API"
R="$WORK/reports/node-test-1.json"
[ -f "$R" ] && ok "отчёт получен" || bad "отчёта нет"
python3 - "$R" "$VER" <<'PY' && ok "в отчёте версия, состояние Xray, отпечаток сертификата, трафик и данные хоста" || bad "в отчёте не хватает полей"
import json, sys
r = json.load(open(sys.argv[1]))
assert r["xray"]["version"] == sys.argv[2] and r["xray"]["active"] is True
assert len(r["tls"]["hy2"]["sha256"]) == 95
assert r["host"]["hostname"] and "load1" in r["host"]
assert "24443/tcp" in r["ports"] and "24443/udp" in r["ports"]
assert r["traffic"] is not None
print("     порты:", r["ports"], "| онлайн:", r["traffic"]["online"])
PY

step "9. Статус"
"${AGENT[@]}" status | sed 's/^/     /'

echo; echo "Итого: пройдено $PASS, провалено $FAIL"
[ "$FAIL" = 0 ]
