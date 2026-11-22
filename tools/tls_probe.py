#!/usr/bin/env python3
"""Тест «режет ли оператор этот IP»: HTTPS-сервер, который отдаёт 2 МБ и пишет, что дошло.

Запускать на входе (или на кандидате в новый адрес):

    curl -fsSL https://raw.githubusercontent.com/knyazevi81/strangling-hand-deploy/main/tools/tls_probe.py \
      | sudo python3 - --port 8444 --minutes 15

Потом с телефона БЕЗ VPN, через проверяемую сеть, открыть https://IP:8444, пропустить
предупреждение о сертификате и подождать до 30 секунд. Результат — в выводе и в /tmp/tls_probe.log:

    tcp connected               дошёл TCP
    TLS ok                      прошло TLS-рукопожатие
    OK 2MB sent in 0.1s         данные идут, IP для этой сети чистый
    TLS FAILED: ... timed out   оператор режет TLS к этому IP (заморозка на уровне IP)
    FROZEN after 16 KB          классическая заморозка ТСПУ после ~16 КБ

Строки «CERTIFICATE_UNKNOWN» до нажатия «Продолжить» в браузере — это нормально.
Порт (TCP) должен быть открыт в файрволе облака. Только стандартная библиотека и openssl.
"""

import argparse
import http.server
import os
import ssl
import subprocess
import sys
import threading
import time

CERT_DIR = "/tmp/tls_probe"
LOG_FILE = "/tmp/tls_probe.log"
BODY = ("<html><body><!--" + "a" * 2000000 + "--><h1 style=\"font-size:48px\">DONE: 2 MB OK</h1>"
        "</body></html>").encode()
_lock = threading.Lock()


def log(ip, text):
    line = "%s %s %s" % (time.strftime("%X"), ip, text)
    with _lock:
        print(line, flush=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")


def ensure_cert():
    crt, key = CERT_DIR + "/t.crt", CERT_DIR + "/t.key"
    if not (os.path.exists(crt) and os.path.exists(key)):
        os.makedirs(CERT_DIR, exist_ok=True)
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "7",
                        "-subj", "/CN=probe", "-keyout", key, "-out", crt],
                       check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return crt, key


class Handler(http.server.BaseHTTPRequestHandler):
    def setup(self):
        self.request.settimeout(20)
        ip = self.client_address[0]
        try:
            self.request.do_handshake()
            log(ip, "TLS ok")
        except Exception as e:  # noqa: BLE001 — нам важен любой исход
            log(ip, "TLS FAILED: %s" % e)
            raise
        super().setup()

    def do_GET(self):
        started, sent = time.time(), 0
        ip = self.client_address[0]
        try:
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.send_header("Content-Length", str(len(BODY)))
            self.end_headers()
            for i in range(0, len(BODY), 16384):
                self.wfile.write(BODY[i:i + 16384])
                sent = i + 16384
            self.wfile.flush()
            log(ip, "OK 2MB sent in %.1fs" % (time.time() - started))
        except Exception as e:  # noqa: BLE001
            log(ip, "FROZEN after %d KB, %.1fs: %s" % (sent // 1024, time.time() - started, e))

    def log_message(self, *args):
        pass


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--port", type=int, default=8444)
    ap.add_argument("--minutes", type=float, default=15, help="сколько работать, потом выйти")
    args = ap.parse_args()

    crt, key = ensure_cert()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(crt, key)

    class Server(http.server.ThreadingHTTPServer):
        daemon_threads = True
        allow_reuse_address = True

        def get_request(self):
            conn, addr = self.socket.accept()
            log(addr[0], "tcp connected")
            return ctx.wrap_socket(conn, server_side=True, do_handshake_on_connect=False), addr

        def handle_error(self, request, client_address):
            pass

    try:
        srv = Server(("0.0.0.0", args.port), Handler)
    except OSError as e:
        sys.exit("порт %d занят или недоступен: %s" % (args.port, e))
    threading.Timer(args.minutes * 60, srv.shutdown).start()
    print("Слушаю TCP %d, %g мин. Открой https://ЭТОТ_IP:%d с проверяемой сети без VPN. Журнал: %s"
          % (args.port, args.minutes, args.port, LOG_FILE), flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    print("Остановлен.")


if __name__ == "__main__":
    main()
