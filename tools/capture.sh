#!/usr/bin/env bash
# Запись всего, что вход видит от одного клиента: пакеты, журнал соединений Xray и журнал службы.
# Пишет в фоне, в файлы /tmp/cap.*, поэтому переживает обрыв SSH (он рвётся, когда клиент включает VPN).
#
#   curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/tools/capture.sh -o /tmp/capture.sh
#   sudo bash /tmp/capture.sh start 95.24.112.3 300    # IP клиента, секунд (по умолчанию 300)
#   ... клиент подключается и открывает сайты ...
#   sudo bash /tmp/capture.sh report                   # сводка
#   sudo bash /tmp/capture.sh stop                     # остановить раньше времени
#
# Внешний IP клиента: на его устройстве без VPN открыть ifconfig.me (или curl -4 ifconfig.me).
set -euo pipefail

ACCESS_LOG=/var/lib/savebit/xray-log/access.log
OUT=/tmp/cap

case "${1:-}" in
  start)
    IP="${2:?укажи IP клиента}"
    SEC="${3:-300}"
    command -v tcpdump >/dev/null || { apt-get update -qq && apt-get install -y -qq tcpdump >/dev/null; }
    rm -f "$OUT".*
    echo "$IP" > "$OUT.ip"
    [ -f "$ACCESS_LOG" ] || echo "!! журнал соединений выключен: включи в админке «IP и домены» → «Сбор данных», иначе cap.access будет пустым"
    setsid nohup bash -c "
      timeout $SEC tail -n0 -F '$ACCESS_LOG' 2>/dev/null | grep --line-buffered -F '$IP' > '$OUT.access' &
      timeout $SEC journalctl -u savebit-xray -f -n0 | grep --line-buffered -F '$IP' > '$OUT.journal' &
      timeout $SEC tcpdump -ni any 'host $IP and not port 22' -q -c 5000 > '$OUT.tcp' 2>&1 &
      wait" >/dev/null 2>&1 &
    echo "Пишу трафик $IP ещё $SEC с в $OUT.*. Можно отключаться от SSH."
    echo "Потом: sudo bash $0 report"
    ;;
  stop)
    pkill -f "tcpdump -ni any host" 2>/dev/null || true
    pkill -f "tail -n0 -F $ACCESS_LOG" 2>/dev/null || true
    pkill -f "journalctl -u savebit-xray -f -n0" 2>/dev/null || true
    echo "Остановлено."
    ;;
  report)
    [ -f "$OUT.tcp" ] || { echo "Записи нет: сначала start"; exit 1; }
    echo "== Клиент $(cat "$OUT.ip" 2>/dev/null)"
    wc -l "$OUT".access "$OUT".journal "$OUT".tcp
    echo
    echo "== Соединения (TCP): байт и пакетов в каждую сторону"
    awk '$4=="IP" && $8=="tcp" {
           if ($3=="In") { n=split($5,a,"."); p=a[n]; i[p]+=$9; ci[p]++ }
           else { d=$7; sub(":","",d); n=split(d,a,"."); p=a[n]; o[p]+=$9; co[p]++ }
           l[p]=$1 }
         END { for (p in l) printf "порт %-6s от клиента %7d Б/%4d пак.   к клиенту %8d Б/%4d пак.   последний %s\n",
                                    p, i[p], ci[p], o[p], co[p], l[p] }' "$OUT.tcp" | sort -k9 | tail -40
    udp=$(grep -c "UDP" "$OUT.tcp" || true)
    echo "UDP-пакетов (Hysteria2): $udp"
    echo
    echo "== Принятые соединения Xray (первые 20)"
    head -20 "$OUT.access"
    echo
    echo "== Ошибки Xray"
    cat "$OUT.journal"
    ;;
  *)
    sed -n '2,12p' "$0"; exit 1 ;;
esac
