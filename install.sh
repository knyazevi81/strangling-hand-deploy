#!/usr/bin/env bash
# Установщик узла Savebit VPN.
#
#   curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/install.sh | sudo bash
#
# Скрипт спросит ключ узла из админки и домен API (по умолчанию api.savebit.ru).
# Роль узла (вход или выход) берётся из API по ключу.
#
# Параметры (можно не указывать, тогда скрипт спросит):
#   --key КЛЮЧ          ключ узла из админки net.savebit.ru
#   --api ДОМЕН         адрес API, по умолчанию api.savebit.ru
#   --role entry|exit   проверить, что ключ выдан именно для этой роли
#   --via ХОСТ:ПОРТ     ходить в API через туннель на входе (если API с этого сервера
#                       недоступен напрямую), например --via 146.185.211.176:9443
#   --yes               не задавать вопросов, если всё указано
#   --uninstall         удалить агента и Xray (конфиги и ключ остаются)
#   --purge             вместе с --uninstall удалить и /etc/savebit
set -euo pipefail

REPO_RAW="${SAVEBIT_REPO_RAW:-https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main}"
DEFAULT_API="api.savebit.ru"

AGENT_DIR=/opt/savebit/agent
AGENT_PY="$AGENT_DIR/savebit_agent.py"
ETC=/etc/savebit
ENV_FILE="$ETC/agent.env"
SERVICE_USER=savebit

KEY="" API="" ROLE="" VIA="" YES=0 UNINSTALL=0 PURGE=0

c_ok()   { printf '\033[1;32m==>\033[0m %s\n' "$*"; }
c_warn() { printf '\033[1;33m!!\033[0m %s\n' "$*"; }
die()    { printf '\033[1;31mОшибка:\033[0m %s\n' "$*" >&2; exit 1; }

while [ $# -gt 0 ]; do
  case "$1" in
    --key) KEY="${2:-}"; shift ;;
    --api) API="${2:-}"; shift ;;
    --role) ROLE="${2:-}"; shift ;;
    --via) VIA="${2:-}"; shift ;;
    --yes|-y) YES=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --purge) PURGE=1 ;;
    -h|--help) sed -n '2,20p' "$0" 2>/dev/null || true; exit 0 ;;
    *) die "непонятный параметр: $1" ;;
  esac
  shift
done

[ "$(id -u)" = 0 ] || die "запускай от root (через sudo)"
command -v systemctl >/dev/null || die "нужен systemd"

# Вопросы задаём через терминал: при запуске через «curl | bash» stdin занят самим скриптом.
ask() { # ask "Вопрос" ПЕРЕМЕННАЯ [скрыть=1]
  local prompt="$1" var="$2" hide="${3:-0}" answer=""
  [ -r /dev/tty ] || die "нет терминала для вопроса «$prompt». Передай значение параметром (см. --help)"
  if [ "$hide" = 1 ]; then
    read -r -s -p "$prompt" answer </dev/tty; echo >/dev/tty
  else
    read -r -p "$prompt" answer </dev/tty
  fi
  printf -v "$var" '%s' "$answer"
}

confirm() { # confirm "Вопрос?" — по умолчанию «да»
  [ "$YES" = 1 ] && return 0
  local a=""; ask "$1 [Д/н]: " a
  case "${a,,}" in ""|д|да|y|yes) return 0 ;; *) return 1 ;; esac
}

# ── Удаление ──────────────────────────────────────────────────────────────────
if [ "$UNINSTALL" = 1 ]; then
  c_ok "Останавливаю службы"
  systemctl disable --now savebit-agent.service savebit-xray.service 2>/dev/null || true
  rm -f /etc/systemd/system/savebit-agent.service /etc/systemd/system/savebit-xray.service
  systemctl daemon-reload
  rm -rf /opt/savebit /usr/local/bin/savebit-agent /var/lib/savebit /etc/sysctl.d/99-savebit.conf
  if [ "$PURGE" = 1 ]; then rm -rf "$ETC"; c_ok "Удалён и $ETC (ключ, конфиги, сертификаты)"; fi
  c_ok "Готово. Правила ufw не трогал: если открывали порты, закрой их вручную."
  exit 0
fi

# ── Проверка системы ──────────────────────────────────────────────────────────
. /etc/os-release 2>/dev/null || true
case "${ID:-}${ID_LIKE:-}" in
  *debian*|*ubuntu*) ;;
  *) c_warn "Проверено на Ubuntu и Debian, а здесь ${PRETTY_NAME:-неизвестная система}. Продолжаю." ;;
esac
case "$(uname -m)" in x86_64|amd64|aarch64|arm64|armv7l) ;; *) die "неподдерживаемый процессор: $(uname -m)" ;; esac

# ── Ключ и домен ──────────────────────────────────────────────────────────────
echo
echo "  Установка узла Savebit VPN"
echo "  Ключ узла берётся в админке: net.savebit.ru → Узлы → Добавить узел."
echo
[ -n "$KEY" ] || ask "Ключ узла: " KEY 1
KEY="$(printf '%s' "$KEY" | tr -d '[:space:]')"
[ -n "$KEY" ] || die "ключ не указан"
if [ -z "$API" ]; then
  ask "Домен API [$DEFAULT_API]: " API
  API="${API:-$DEFAULT_API}"
fi
API="$(printf '%s' "$API" | tr -d '[:space:]')"
API="${API%/}"
case "$API" in http://*|https://*) ;; *) API="https://$API" ;; esac

# ── Зависимости ───────────────────────────────────────────────────────────────
need=()
for b in python3 curl openssl; do command -v "$b" >/dev/null || need+=("$b"); done
if [ ${#need[@]} -gt 0 ]; then
  c_ok "Ставлю пакеты: ${need[*]}"
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq && apt-get install -y -qq "${need[@]}" ca-certificates >/dev/null
fi
python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' || die "нужен Python 3.8 или новее"

# ── Агент ─────────────────────────────────────────────────────────────────────
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" 2>/dev/null && pwd || echo /nonexistent)"
mkdir -p "$AGENT_DIR"
if [ -f "$SRC_DIR/agent/savebit_agent.py" ]; then
  install -m 755 "$SRC_DIR/agent/savebit_agent.py" "$AGENT_PY.new"
else
  curl -fsSL "$REPO_RAW/agent/savebit_agent.py" -o "$AGENT_PY.new" || die "не удалось скачать агента с $REPO_RAW"
  chmod 755 "$AGENT_PY.new"
fi
python3 -m py_compile "$AGENT_PY.new" || die "скачанный агент повреждён"
mv -f "$AGENT_PY.new" "$AGENT_PY"
ln -sf "$AGENT_PY" /usr/local/bin/savebit-agent

# ── Проверка ключа ────────────────────────────────────────────────────────────
if [ -n "$VIA" ]; then c_ok "Проверяю ключ на $API (через вход $VIA)"; else c_ok "Проверяю ключ на $API"; fi
set +e
NODE_JSON="$(python3 "$AGENT_PY" check --api "$API" --key "$KEY" --via "${VIA:-off}")"
rc=$?
set -e
case $rc in
  0) ;;
  2) die "API не принял ключ. Проверь его в админке или выпусти новый." ;;
  3) if [ -z "$VIA" ]; then
       die "API $API недоступен с этого сервера: $NODE_JSON
   Если это выход, а напрямую до API не достучаться, укажи туннель на входе: --via АДРЕС_ВХОДА:9443"
     fi
     die "API недоступен и через вход: $NODE_JSON
   Проверь, что вход онлайн и в группе безопасности открыт TCP-порт туннеля." ;;
  *) die "не удалось получить настройки узла: $NODE_JSON" ;;
esac
NODE_NAME="$(printf '%s' "$NODE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["name"])')"
NODE_ROLE="$(printf '%s' "$NODE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["role"])')"
XRAY_VER="$(printf '%s' "$NODE_JSON" | python3 -c 'import json,sys; print(json.load(sys.stdin)["xray"])')"
ROLE_RU="вход"; [ "$NODE_ROLE" = exit ] && ROLE_RU="выход"
c_ok "Ключ подошёл: узел «$NODE_NAME», роль: $ROLE_RU, Xray $XRAY_VER"
if [ -n "$ROLE" ] && [ "$ROLE" != "$NODE_ROLE" ]; then
  die "этот ключ выдан для роли «$NODE_ROLE», а скрипт запущен для «$ROLE». Проверь ключ."
fi

# Предупреждаем о том, что может занять те же порты
for s in x-ui xray; do
  if systemctl is-active --quiet "$s" 2>/dev/null; then
    c_warn "На сервере уже работает служба $s. Она может занимать нужные порты (443, 8443 и др.)."
    confirm "Продолжить установку?" || die "отменено"
  fi
done

# ── Файлы и службы ────────────────────────────────────────────────────────────
c_ok "Настраиваю систему"
id "$SERVICE_USER" >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
mkdir -p "$ETC/xray" "$ETC/tls" /var/lib/savebit /opt/savebit/xray
chmod 750 "$ETC/xray" "$ETC/tls"; chgrp "$SERVICE_USER" "$ETC/xray" "$ETC/tls"
chmod 755 "$ETC"

umask 077
cat > "$ENV_FILE" <<EOF
# Создано установщиком Savebit. Ключ меняется командой: savebit-agent rekey НОВЫЙ_КЛЮЧ
SAVEBIT_API=$API
SAVEBIT_NODE_KEY=$KEY
EOF
[ -n "$VIA" ] && printf 'SAVEBIT_API_VIA=%s\n' "$VIA" >> "$ENV_FILE"
umask 022

cat > /etc/sysctl.d/99-savebit.conf <<'EOF'
# Savebit: BBR для TCP и большие буферы для QUIC (Hysteria2)
net.core.default_qdisc = fq
net.ipv4.tcp_congestion_control = bbr
net.core.rmem_max = 16777216
net.core.wmem_max = 16777216
net.ipv4.tcp_fastopen = 3
fs.file-max = 1048576
EOF
sysctl --system >/dev/null 2>&1 || c_warn "не все настройки ядра применились (это не критично)"

cat > /etc/systemd/system/savebit-xray.service <<EOF
[Unit]
Description=Savebit Xray
After=network-online.target
Wants=network-online.target

[Service]
User=$SERVICE_USER
Group=$SERVICE_USER
Environment=XRAY_LOCATION_ASSET=/opt/savebit/xray/current
ExecStart=/opt/savebit/xray/current/xray run -config $ETC/xray/config.json
Restart=on-failure
RestartSec=3
LimitNOFILE=1048576
AmbientCapabilities=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
CapabilityBoundingSet=CAP_NET_BIND_SERVICE CAP_NET_ADMIN
NoNewPrivileges=true

[Install]
WantedBy=multi-user.target
EOF

cat > /etc/systemd/system/savebit-agent.service <<EOF
[Unit]
Description=Savebit node agent
After=network-online.target
Wants=network-online.target

[Service]
ExecStart=/usr/bin/env python3 $AGENT_PY run
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload

# ── Первая синхронизация ──────────────────────────────────────────────────────
c_ok "Скачиваю Xray и применяю конфиг узла (может занять минуту)"
if python3 "$AGENT_PY" sync; then
  SYNC_OK=1
else
  SYNC_OK=0
  c_warn "Первая синхронизация не удалась. Агент будет пробовать сам раз в минуту."
fi
systemctl enable --now savebit-agent.service >/dev/null 2>&1

echo
python3 "$AGENT_PY" status || true
echo
if [ "$SYNC_OK" = 1 ]; then
  c_ok "Узел «$NODE_NAME» готов. Через минуту он появится в админке как работающий."
else
  c_warn "Агент установлен, но узел пока не работает. Причина выше, подробности: journalctl -u savebit-agent -f"
fi
cat <<EOF

  Полезные команды:
    savebit-agent status            состояние узла
    savebit-agent sync              синхронизироваться прямо сейчас
    savebit-agent rekey НОВЫЙ_КЛЮЧ  заменить ключ после перевыпуска в админке
    journalctl -u savebit-agent -f  журнал агента
    journalctl -u savebit-xray -f   журнал Xray
EOF
