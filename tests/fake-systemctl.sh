#!/usr/bin/env bash
# Заменитель systemctl для тестов без systemd: запускает Xray как обычный процесс.
# Пути берёт из SAVEBIT_ROOT, как и агент.
ROOT="${SAVEBIT_ROOT:-}"
RUN="$ROOT/run"; mkdir -p "$RUN"
XRAY="$ROOT/opt/savebit/xray/current/xray"
CFG="$ROOT/etc/savebit/xray/config.json"
PID="$RUN/xray.pid"

alive() { [ -f "$PID" ] && kill -0 "$(cat "$PID")" 2>/dev/null; }
start() {
  XRAY_LOCATION_ASSET="$ROOT/opt/savebit/xray/current" nohup "$XRAY" run -config "$CFG" >"$RUN/xray.log" 2>&1 &
  echo $! >"$PID"
}
stop() { if alive; then kill "$(cat "$PID")"; sleep 0.3; fi; rm -f "$PID"; }

cmd="$1"; unit="${2%.service}"
if [ "$unit" != "savebit-xray" ]; then
  # агент в тестах не служба: делаем вид, что всё хорошо
  [ "$cmd" = is-active ] && echo active
  exit 0
fi
case "$cmd" in
  is-active) if alive; then echo active; exit 0; else echo inactive; exit 3; fi ;;
  restart) stop; start ;;
  start) alive || start ;;
  stop) stop ;;
  enable|disable|daemon-reload) ;;
esac
exit 0
