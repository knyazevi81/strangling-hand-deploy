#!/usr/bin/env python3
"""Тестовый API для агента: отдаёт конфиги узлов и принимает отчёты.

Нужен, чтобы проверить агента и установщик до того, как в бэкенде появятся
настоящие эндпоинты. Реализует тот же контракт, что описан в docs/agent-api.md.

    python3 tools/mock_api.py --nodes tools/mock_nodes.example.json --port 18765

Файл с узлами перечитывается при каждом запросе, так что его можно править на ходу.
"""
import argparse
import hashlib
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PREFIX = "/api/v1/agent"


def load_nodes(path):
    with open(path) as f:
        data = json.load(f)
    base = os.path.dirname(os.path.abspath(path))
    nodes = {}
    for n in data["nodes"]:
        cfg_path = n["config_file"]
        if not os.path.isabs(cfg_path):
            cfg_path = os.path.join(base, cfg_path)
        with open(cfg_path) as f:
            cfg = json.load(f)
        for key in n["keys"]:
            nodes[key] = dict(n, config=cfg)
    return nodes, data.get("files_dir")


def payload_for(node):
    body = {
        "node": {"id": node["id"], "name": node["name"], "role": node["role"]},
        "interval": node.get("interval", 60),
        "xray": node["xray"],
        "tls": node.get("tls", []),
        "config": node["config"],
    }
    rev = hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()[:12]
    body["revision"] = rev
    return body


class Handler(BaseHTTPRequestHandler):
    nodes_file = None
    reports_dir = None

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock-api] %s\n" % (fmt % args))

    def _node(self):
        auth = self.headers.get("Authorization", "")
        key = auth[7:] if auth.startswith("Bearer ") else ""
        nodes, files_dir = load_nodes(self.nodes_file)
        return nodes.get(key), files_dir

    def _json(self, code, obj, headers=None):
        raw = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        node, files_dir = self._node()
        if self.path.startswith("/files/"):
            if not node:
                return self._json(401, {"detail": "invalid node key"})
            name = os.path.basename(self.path)
            path = os.path.join(files_dir or ".", name)
            if not os.path.exists(path):
                return self._json(404, {"detail": "no such file"})
            with open(path, "rb") as f:
                data = f.read()
            self.send_response(200)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
            return
        if self.path != PREFIX + "/config":
            return self._json(404, {"detail": "not found"})
        if not node:
            return self._json(401, {"detail": "invalid node key"})
        body = payload_for(node)
        etag = '"%s"' % body["revision"]
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.end_headers()
            return
        self._json(200, body, {"ETag": etag})

    def do_POST(self):
        node, _ = self._node()
        if self.path != PREFIX + "/report":
            return self._json(404, {"detail": "not found"})
        if not node:
            return self._json(401, {"detail": "invalid node key"})
        length = int(self.headers.get("Content-Length", 0))
        report = json.loads(self.rfile.read(length) or b"{}")
        report["_received_at"] = int(time.time())
        if self.reports_dir:
            os.makedirs(self.reports_dir, exist_ok=True)
            with open(os.path.join(self.reports_dir, node["id"] + ".json"), "w") as f:
                json.dump(report, f, indent=2, ensure_ascii=False)
        x = report.get("xray") or {}
        sys.stderr.write("[mock-api] отчёт от %s: ревизия %s, xray %s %s, ошибка: %s\n" % (
            node["name"], report.get("revision"), x.get("version"),
            "работает" if x.get("active") else "НЕ работает", report.get("error") or "нет"))
        self._json(200, {"ok": True})


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--nodes", required=True)
    ap.add_argument("--port", type=int, default=18765)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--reports-dir")
    args = ap.parse_args()
    Handler.nodes_file = args.nodes
    Handler.reports_dir = args.reports_dir
    srv = ThreadingHTTPServer((args.bind, args.port), Handler)
    sys.stderr.write("[mock-api] слушаю http://%s:%d\n" % (args.bind, args.port))
    srv.serve_forever()


if __name__ == "__main__":
    main()
