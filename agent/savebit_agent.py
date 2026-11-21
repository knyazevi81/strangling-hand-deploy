#!/usr/bin/env python3
"""savebit-agent: агент узла Savebit VPN.

Раз в N секунд забирает у api.savebit.ru конфиг своего узла и нужную версию Xray,
проверяет и применяет их, а затем отправляет отчёт о здоровье узла.

Соединения всегда идут от узла к API: панели не нужен доступ к серверу.
Если API недоступен или ключ не принят, агент продолжает работать на последнем
рабочем конфиге.

Только стандартная библиотека Python 3.8+.
"""

import argparse
import hashlib
import io
import json
import os
import platform
import random
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

AGENT_VERSION = "0.1.0"

# SAVEBIT_ROOT нужен только для тестов: все пути уходят в отдельный каталог.
ROOT = os.environ.get("SAVEBIT_ROOT", "").rstrip("/")


def _p(path):
    return ROOT + path


ENV_FILE = _p("/etc/savebit/agent.env")
STATE_FILE = _p("/var/lib/savebit/state.json")
XRAY_BASE = _p("/opt/savebit/xray")
XRAY_CURRENT = XRAY_BASE + "/current"
XRAY_BIN = XRAY_CURRENT + "/xray"
CONFIG_DIR = _p("/etc/savebit/xray")
CONFIG_FILE = CONFIG_DIR + "/config.json"
TLS_DIR = _p("/etc/savebit/tls")

XRAY_SERVICE = "savebit-xray"
AGENT_SERVICE = "savebit-agent"
SERVICE_GROUP = os.environ.get("SAVEBIT_SERVICE_GROUP", "savebit")
SYSTEMCTL = os.environ.get("SAVEBIT_SYSTEMCTL", "systemctl")

API_PREFIX = os.environ.get("SAVEBIT_API_PREFIX", "/api/v1/agent")
DEFAULT_INTERVAL = 60
MIN_INTERVAL, MAX_INTERVAL = 15, 3600

GITHUB_RELEASE = "https://github.com/XTLS/Xray-core/releases/download/v{ver}/Xray-linux-{arch}.zip"
ARCH_MAP = {
    "x86_64": "64", "amd64": "64",
    "aarch64": "arm64-v8a", "arm64": "arm64-v8a",
    "armv7l": "arm32-v7a",
}
TLS_PLACEHOLDER = "${TLS_DIR}"


class AgentError(Exception):
    """Ошибка, о которой нужно сообщить в API, не роняя агента."""


class ApiUnavailable(AgentError):
    pass


class KeyRejected(AgentError):
    pass


# ── мелочи ─────────────────────────────────────────────────────────────────────

def log(msg):
    print("[savebit-agent] " + msg, flush=True)


def read_env(path=ENV_FILE):
    env = {}
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    except FileNotFoundError:
        pass
    return env


def write_env(api, key, path=ENV_FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write("# Создано savebit-agent. Ключ меняется командой: savebit-agent rekey НОВЫЙ_КЛЮЧ\n")
        f.write("SAVEBIT_API=%s\n" % api)
        f.write("SAVEBIT_NODE_KEY=%s\n" % key)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load_state():
    try:
        with open(STATE_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    tmp = STATE_FILE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    os.replace(tmp, STATE_FILE)


def normalize_api(value):
    value = (value or "").strip().rstrip("/")
    if not value:
        raise AgentError("не указан адрес API")
    if not re.match(r"^https?://", value):
        value = "https://" + value
    return value


def run(cmd, timeout=60, env=None):
    p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                       timeout=timeout, env=env, universal_newlines=True)
    return p.returncode, p.stdout


def systemctl(*args, timeout=60):
    try:
        return run([SYSTEMCTL] + list(args), timeout=timeout)
    except FileNotFoundError:
        return 127, "systemctl не найден"


def service_active(name):
    code, out = systemctl("is-active", name, timeout=15)
    return code == 0 and out.strip() == "active"


def chown_group(path, mode):
    """Файл читает root и группа службы Xray. Если группы нет (тесты), просто ставим права."""
    os.chmod(path, mode)
    try:
        shutil.chown(path, user="root" if os.geteuid() == 0 else None, group=SERVICE_GROUP)
    except (LookupError, PermissionError, OSError):
        pass


def tail(text, lines=15):
    return "\n".join((text or "").strip().splitlines()[-lines:])


# ── API ────────────────────────────────────────────────────────────────────────

def http_get_raw(url, headers=None, timeout=120):
    req = urllib.request.Request(url, headers=headers or {})
    req.add_header("User-Agent", "savebit-agent/" + AGENT_VERSION)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise AgentError("не удалось скачать %s: %s" % (url, e))


def api_request(api, key, method, path, body=None, etag=None, timeout=20):
    url = api.rstrip("/") + API_PREFIX + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("User-Agent", "savebit-agent/" + AGENT_VERSION)
    req.add_header("Accept", "application/json")
    if data is not None:
        req.add_header("Content-Type", "application/json")
    if etag:
        req.add_header("If-None-Match", '"%s"' % etag)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as e:
        if e.code == 304:
            return 304, None
        if e.code in (401, 403):
            raise KeyRejected("API не принял ключ узла (HTTP %d). Новый ключ: savebit-agent rekey КЛЮЧ" % e.code)
        try:
            detail = e.read().decode(errors="replace")[:300]
        except Exception:
            detail = ""
        raise AgentError("API ответил HTTP %d на %s %s %s" % (e.code, method, path, detail))
    except (urllib.error.URLError, socket.timeout, OSError) as e:
        raise ApiUnavailable("API недоступен (%s): %s" % (api, getattr(e, "reason", e)))
    except ValueError:
        raise AgentError("API вернул не JSON на %s %s" % (method, path))


def fetch_config(api, key, etag=None):
    code, payload = api_request(api, key, "GET", "/config", etag=etag)
    if code == 304:
        return None
    validate_payload(payload)
    return payload


def validate_payload(p):
    if not isinstance(p, dict):
        raise AgentError("ответ API на /config не объект")
    for field in ("node", "revision", "xray", "config"):
        if field not in p:
            raise AgentError("в ответе API нет поля %s" % field)
    if p["node"].get("role") not in ("entry", "exit"):
        raise AgentError("неизвестная роль узла: %r" % p["node"].get("role"))
    if not re.match(r"^\d+\.\d+\.\d+$", str(p["xray"].get("version", "")).lstrip("v")):
        raise AgentError("в ответе API нет корректной xray.version")
    if not isinstance(p["config"], dict) or "inbounds" not in p["config"]:
        raise AgentError("config в ответе API не похож на конфиг Xray")


# ── Xray: установка нужной версии ─────────────────────────────────────────────

def installed_xray_version(binary=XRAY_BIN):
    if not os.path.exists(binary):
        return None
    try:
        code, out = run([binary, "version"], timeout=20)
    except (OSError, subprocess.TimeoutExpired):
        return None
    m = re.search(r"Xray (\d+\.\d+\.\d+)", out)
    return m.group(1) if code == 0 and m else None


def expected_sha256(spec, zip_url, api, key):
    if spec.get("sha256"):
        return spec["sha256"].lower()
    if spec.get("url"):
        return None
    # У релизов на GitHub рядом лежит файл .dgst с контрольными суммами
    dgst = http_get_raw(zip_url + ".dgst", timeout=60).decode(errors="replace")
    m = re.search(r"SHA2-256=\s*([0-9a-fA-F]{64})", dgst)
    return m.group(1).lower() if m else None


def ensure_xray(spec, api, key):
    """Ставит нужную версию Xray рядом со старыми и переключает ссылку current.
    Возвращает True, если версия поменялась."""
    ver = str(spec["version"]).lstrip("v")
    if installed_xray_version() == ver:
        return False
    vdir = "%s/versions/%s" % (XRAY_BASE, ver)
    if installed_xray_version(vdir + "/xray") != ver:
        arch = ARCH_MAP.get(platform.machine().lower())
        if not arch:
            raise AgentError("неизвестная архитектура процессора: %s" % platform.machine())
        url = spec.get("url") or GITHUB_RELEASE.format(ver=ver, arch=arch)
        headers = {}
        if url.startswith(api):
            headers["Authorization"] = "Bearer " + key
        log("скачиваю Xray %s: %s" % (ver, url))
        data = http_get_raw(url, headers=headers, timeout=300)
        want = expected_sha256(spec, url, api, key)
        got = hashlib.sha256(data).hexdigest()
        if want and want != got:
            raise AgentError("контрольная сумма Xray не совпала: ожидали %s, получили %s" % (want, got))
        if not want:
            log("контрольная сумма для архива не указана, проверка пропущена")
        os.makedirs(XRAY_BASE, exist_ok=True)
        tmpdir = tempfile.mkdtemp(prefix=".download-", dir=XRAY_BASE)
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                for name in z.namelist():
                    base = os.path.basename(name)
                    if base in ("xray", "geoip.dat", "geosite.dat"):
                        with z.open(name) as src, open(os.path.join(tmpdir, base), "wb") as dst:
                            shutil.copyfileobj(src, dst)
            if not os.path.exists(os.path.join(tmpdir, "xray")):
                raise AgentError("в архиве нет файла xray")
            os.chmod(os.path.join(tmpdir, "xray"), 0o755)
            for dat in ("geoip.dat", "geosite.dat"):
                if os.path.exists(os.path.join(tmpdir, dat)):
                    os.chmod(os.path.join(tmpdir, dat), 0o644)
            if installed_xray_version(os.path.join(tmpdir, "xray")) != ver:
                raise AgentError("скачанный Xray не запускается или не той версии")
            os.makedirs(os.path.dirname(vdir), exist_ok=True)
            if os.path.exists(vdir):
                shutil.rmtree(vdir)
            os.rename(tmpdir, vdir)
            os.chmod(vdir, 0o755)
        finally:
            if os.path.exists(tmpdir):
                shutil.rmtree(tmpdir, ignore_errors=True)
    link_tmp = XRAY_CURRENT + ".new"
    if os.path.lexists(link_tmp):
        os.remove(link_tmp)
    os.symlink(vdir, link_tmp)
    os.replace(link_tmp, XRAY_CURRENT)
    log("Xray переключён на версию %s" % ver)
    cleanup_old_versions(keep=ver)
    return True


def cleanup_old_versions(keep, max_versions=3):
    root = XRAY_BASE + "/versions"
    try:
        versions = sorted(os.listdir(root), key=lambda v: [int(x) for x in v.split(".") if x.isdigit()])
    except FileNotFoundError:
        return
    old = [v for v in versions if v != keep]
    extra = old[:-(max_versions - 1)] if max_versions > 1 else old
    for v in extra:
        shutil.rmtree(os.path.join(root, v), ignore_errors=True)


# ── TLS для Hysteria2 ─────────────────────────────────────────────────────────

def ensure_tls(items):
    """Создаёт самоподписанные сертификаты по списку из API и возвращает их отпечатки.
    Отпечаток нужен бэкенду: клиенты и входы привязываются к сертификату по нему."""
    result = {}
    for item in items or []:
        name = re.sub(r"[^a-zA-Z0-9_-]", "", str(item.get("name", "")))
        if not name:
            continue
        cn = re.sub(r"[^a-zA-Z0-9.-]", "", str(item.get("cn") or "www.example.com"))
        os.makedirs(TLS_DIR, exist_ok=True)
        crt, key = "%s/%s.crt" % (TLS_DIR, name), "%s/%s.key" % (TLS_DIR, name)
        if not (os.path.exists(crt) and os.path.exists(key)):
            log("создаю сертификат %s (CN=%s)" % (name, cn))
            code, out = run(["openssl", "req", "-x509", "-newkey", "ec",
                             "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes",
                             "-days", "3650", "-subj", "/CN=" + cn,
                             "-keyout", key, "-out", crt], timeout=60)
            if code != 0:
                raise AgentError("openssl не смог создать сертификат: " + tail(out, 3))
        chown_group(crt, 0o644)
        chown_group(key, 0o640)
        code, out = run(["openssl", "x509", "-in", crt, "-noout", "-fingerprint", "-sha256"], timeout=20)
        m = re.search(r"=([0-9A-F:]{95})", out)
        if code != 0 or not m:
            raise AgentError("не удалось прочитать отпечаток сертификата " + name)
        result[name] = {"sha256": m.group(1), "cn": cn}
    return result


def substitute_placeholders(obj):
    if isinstance(obj, dict):
        return {k: substitute_placeholders(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [substitute_placeholders(v) for v in obj]
    if isinstance(obj, str) and TLS_PLACEHOLDER in obj:
        return obj.replace(TLS_PLACEHOLDER, TLS_DIR)
    return obj


# ── Применение конфига ─────────────────────────────────────────────────────────

def xray_env():
    env = dict(os.environ)
    env["XRAY_LOCATION_ASSET"] = XRAY_CURRENT
    return env


def test_config(path):
    code, out = run([XRAY_BIN, "run", "-test", "-c", path], timeout=60, env=xray_env())
    if code != 0 or "Configuration OK" not in out:
        raise AgentError("конфиг не прошёл проверку xray -test:\n" + tail(out, 8))


def restart_xray():
    systemctl("enable", XRAY_SERVICE, timeout=30)
    code, out = systemctl("restart", XRAY_SERVICE, timeout=60)
    for _ in range(10):
        time.sleep(0.5)
        if service_active(XRAY_SERVICE):
            time.sleep(1.5)
            if service_active(XRAY_SERVICE):
                return True, ""
    _, journal = run(["journalctl", "-u", XRAY_SERVICE, "-n", "15", "--no-pager"], timeout=20) \
        if shutil.which("journalctl") else (0, out)
    return False, tail(journal, 15)


def apply_config(cfg, force_restart=False):
    """Проверяет новый конфиг, подменяет старый и перезапускает Xray.
    Если Xray с новым конфигом не поднялся, возвращает прежний."""
    os.makedirs(CONFIG_DIR, exist_ok=True)
    body = json.dumps(cfg, indent=2, ensure_ascii=False, sort_keys=True)
    try:
        with open(CONFIG_FILE) as f:
            same = f.read() == body
    except FileNotFoundError:
        same = False
    if same and not force_restart and service_active(XRAY_SERVICE):
        return False

    new = CONFIG_DIR + "/config.new.json"  # Xray узнаёт формат по расширению
    with open(new, "w") as f:
        f.write(body)
    chown_group(new, 0o640)
    try:
        test_config(new)
    except AgentError:
        os.remove(new)
        raise

    prev = CONFIG_DIR + "/config.prev.json"
    had_old = os.path.exists(CONFIG_FILE)
    if had_old:
        shutil.copy2(CONFIG_FILE, prev)
    os.replace(new, CONFIG_FILE)

    ok, journal = restart_xray()
    if ok:
        return True
    if had_old:
        os.replace(prev, CONFIG_FILE)
        restart_xray()
        raise AgentError("Xray не запустился с новым конфигом, вернул прежний:\n" + journal)
    raise AgentError("Xray не запустился:\n" + journal)


# ── Порты и файрвол ───────────────────────────────────────────────────────────

def inbound_ports(cfg):
    """Список (порт_с, порт_по, tcp|udp) для входящих, которые слушают снаружи."""
    result = []
    for ib in cfg.get("inbounds", []):
        listen = str(ib.get("listen", "0.0.0.0"))
        if listen.startswith("127.") or listen in ("::1", "localhost") or ib.get("tag") == "api":
            continue
        ss = ib.get("streamSettings") or {}
        udp = ib.get("protocol") == "hysteria" or ss.get("network") in ("hysteria", "kcp", "mkcp")
        proto = "udp" if udp else "tcp"
        for part in str(ib.get("port", "")).split(","):
            part = part.strip()
            if not part:
                continue
            if "-" in part:
                a, b = part.split("-", 1)
            else:
                a = b = part
            if a.isdigit() and b.isdigit():
                result.append((int(a), int(b), proto))
    return sorted(set(result))


def open_firewall(ports):
    """Открывает порты в ufw, если он включён. Сам ничего не закрывает."""
    if not shutil.which("ufw"):
        return []
    code, out = run(["ufw", "status"], timeout=20)
    if code != 0 or "Status: active" not in out:
        return []
    opened = []
    for a, b, proto in ports:
        rule = ("%d:%d/%s" % (a, b, proto)) if a != b else ("%d/%s" % (a, proto))
        c, _ = run(["ufw", "allow", rule], timeout=20)
        if c == 0:
            opened.append(rule)
    return opened


# ── Отчёт о здоровье ──────────────────────────────────────────────────────────

def host_info():
    info = {"hostname": socket.gethostname(), "kernel": platform.release(), "cpus": os.cpu_count()}
    try:
        with open("/etc/os-release") as f:
            m = re.search(r'^PRETTY_NAME="?([^"\n]+)', f.read(), re.M)
            info["os"] = m.group(1) if m else None
    except OSError:
        pass
    try:
        with open("/proc/uptime") as f:
            info["uptime_sec"] = int(float(f.read().split()[0]))
        with open("/proc/loadavg") as f:
            info["load1"] = float(f.read().split()[0])
        mem = {}
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                mem[k] = int(v.split()[0])
        if mem.get("MemTotal"):
            info["mem_used_pct"] = round(100 * (1 - mem.get("MemAvailable", 0) / mem["MemTotal"]), 1)
    except (OSError, ValueError, IndexError):
        pass
    try:
        du = shutil.disk_usage("/")
        info["disk_used_pct"] = round(100 * du.used / du.total, 1)
    except OSError:
        pass
    return info


def tcp_probe(host, port, timeout=5.0):
    t0 = time.monotonic()
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True, round((time.monotonic() - t0) * 1000, 1)
    except OSError:
        return False, None


def outbound_targets(cfg):
    """Куда узел ходит дальше (для входа это выходы)."""
    targets = []
    for ob in cfg.get("outbounds", []):
        s = ob.get("settings") or {}
        ss = ob.get("streamSettings") or {}
        if ob.get("protocol") == "hysteria":
            host, port, proto = s.get("address"), s.get("port"), "udp"
        elif s.get("vnext"):
            host, port, proto = s["vnext"][0].get("address"), s["vnext"][0].get("port"), "tcp"
        elif s.get("servers"):
            host, port, proto = s["servers"][0].get("address"), s["servers"][0].get("port"), "tcp"
        else:
            continue
        if ss.get("network") in ("hysteria", "kcp", "mkcp"):
            proto = "udp"
        targets.append({"tag": ob.get("tag"), "address": host, "port": port, "proto": proto})
    return targets


def xray_api_addr(cfg):
    api = cfg.get("api") or {}
    return api.get("listen")


def xray_api(addr, *args):
    try:
        code, out = run([XRAY_BIN, "api"] + list(args) + ["--server=" + addr], timeout=15, env=xray_env())
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out if code == 0 else None


def traffic_and_online(cfg):
    addr = xray_api_addr(cfg)
    if not addr or not os.path.exists(XRAY_BIN):
        return None
    report = {"users": {}, "online": None}
    out = xray_api(addr, "statsquery", "-pattern", "user>>>", "-reset")
    if out:
        try:
            for s in json.loads(out).get("stat", []):
                parts = s.get("name", "").split(">>>")
                if len(parts) == 4 and parts[2] == "traffic":
                    u = report["users"].setdefault(parts[1], {"up": 0, "down": 0})
                    u["up" if parts[3] == "uplink" else "down"] += int(s.get("value", 0))
        except ValueError:
            pass
    out = xray_api(addr, "statsgetallonlineusers")
    if out:
        try:
            data = json.loads(out)
            users = data.get("users") if isinstance(data, dict) else data
            report["online"] = len(users or [])
        except ValueError:
            pass
    return report


def balancer_choice(cfg):
    addr = xray_api_addr(cfg)
    tags = [b.get("tag") for b in (cfg.get("routing") or {}).get("balancers", []) if b.get("tag")]
    if not addr or not tags:
        return None
    result = {}
    for tag in tags:
        out = xray_api(addr, "bi", tag)
        if not out:
            continue
        lines = out.splitlines()
        for i, line in enumerate(lines):
            if "Selects:" in line and i + 1 < len(lines):
                parts = lines[i + 1].split()
                result[tag] = parts[1] if len(parts) > 1 else None
    return result or None


def build_report(state, cfg, error=None):
    probes = []
    for t in outbound_targets(cfg or {}):
        if t["proto"] == "tcp" and t["address"] and t["port"]:
            ok, ms = tcp_probe(t["address"], int(t["port"]))
            t.update({"ok": ok, "ms": ms})
        else:
            t.update({"ok": None, "ms": None})
        probes.append(t)
    return {
        "agent_version": AGENT_VERSION,
        "revision": state.get("revision"),
        "applied_at": state.get("applied_at"),
        "error": error,
        "xray": {"version": installed_xray_version(), "active": service_active(XRAY_SERVICE)},
        "tls": state.get("tls", {}),
        "ports": ["%s-%s/%s" % p if p[0] != p[1] else "%s/%s" % (p[0], p[2]) for p in inbound_ports(cfg or {})],
        "probes": probes,
        "balancers": balancer_choice(cfg or {}),
        "traffic": traffic_and_online(cfg or {}),
        "host": host_info(),
    }


# ── Один цикл синхронизации ───────────────────────────────────────────────────

def sync_once(api, key, verbose=False):
    state = load_state()
    error = None
    try:
        have_config = os.path.exists(CONFIG_FILE) and installed_xray_version() is not None
        payload = fetch_config(api, key, etag=state.get("revision") if have_config else None)
        if payload is None:
            if verbose:
                log("конфиг не изменился (ревизия %s)" % state.get("revision"))
        else:
            node = payload["node"]
            if state.get("node", {}).get("id") not in (None, node.get("id")):
                log("внимание: ключ теперь принадлежит другому узлу (%s)" % node.get("name"))
            tls = ensure_tls(payload.get("tls"))
            version_changed = ensure_xray(payload["xray"], api, key)
            cfg = substitute_placeholders(payload["config"])
            changed = apply_config(cfg, force_restart=version_changed)
            opened = open_firewall(inbound_ports(cfg))
            state.update({
                "node": node,
                "revision": payload["revision"],
                "interval": payload.get("interval", DEFAULT_INTERVAL),
                "tls": tls,
                "applied_at": int(time.time()) if changed else state.get("applied_at"),
                "xray_version": installed_xray_version(),
            })
            log("узел %s (%s): ревизия %s %s%s" % (
                node.get("name"), node.get("role"), payload["revision"],
                "применена" if changed else "без изменений",
                (", открыты порты " + ", ".join(opened)) if opened else ""))
        state["last_error"] = None
    except KeyRejected as e:
        error = str(e)
    except AgentError as e:
        error = str(e)
    state["last_sync"] = int(time.time())
    if error:
        state["last_error"] = error
        log("ошибка: " + error)
    save_state(state)

    cfg = None
    try:
        with open(CONFIG_FILE) as f:
            cfg = json.load(f)
    except (FileNotFoundError, ValueError):
        pass
    try:
        api_request(api, key, "POST", "/report", body=build_report(state, cfg, error))
    except AgentError as e:
        if verbose or not error:
            log("отчёт не отправлен: %s" % e)
    interval = state.get("interval") or DEFAULT_INTERVAL
    return max(MIN_INTERVAL, min(MAX_INTERVAL, int(interval))), error


# ── Команды ───────────────────────────────────────────────────────────────────

def need_env():
    env = read_env()
    api, key = env.get("SAVEBIT_API"), env.get("SAVEBIT_NODE_KEY")
    if not api or not key:
        sys.exit("Агент не настроен: нет %s. Запусти установщик ещё раз." % ENV_FILE)
    return api, key


def cmd_run(args):
    api, key = need_env()
    stop = {"flag": False}

    def handler(signum, frame):
        stop["flag"] = True
    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)
    log("запущен, версия %s, API %s" % (AGENT_VERSION, api))
    while not stop["flag"]:
        try:
            interval, _ = sync_once(api, key)
        except Exception as e:  # агент не должен падать ни от чего
            log("непредвиденная ошибка: %r" % e)
            interval = DEFAULT_INTERVAL
        deadline = time.time() + interval + random.uniform(0, 5)
        while not stop["flag"] and time.time() < deadline:
            time.sleep(1)
        env = read_env()  # ключ могли поменять командой rekey
        api, key = env.get("SAVEBIT_API", api), env.get("SAVEBIT_NODE_KEY", key)
    log("остановлен")


def cmd_sync(args):
    api, key = need_env()
    _, error = sync_once(api, key, verbose=True)
    sys.exit(1 if error else 0)


def check_key(api, key):
    """Проверяет ключ и печатает, какой это узел. Код выхода: 0 ок, 2 ключ не принят, 3 API недоступен."""
    try:
        p = fetch_config(api, key)
    except KeyRejected as e:
        print(str(e))
        return 2, None
    except ApiUnavailable as e:
        print(str(e))
        return 3, None
    except AgentError as e:
        print(str(e))
        return 4, None
    print(json.dumps({"name": p["node"].get("name"), "role": p["node"].get("role"),
                      "id": p["node"].get("id"), "xray": p["xray"].get("version")}, ensure_ascii=False))
    return 0, p


def cmd_check(args):
    env = read_env()
    api = normalize_api(args.api or env.get("SAVEBIT_API"))
    key = args.key or env.get("SAVEBIT_NODE_KEY")
    if not key:
        sys.exit("нужен ключ: --key")
    code, _ = check_key(api, key)
    sys.exit(code)


def cmd_rekey(args):
    env = read_env()
    api = normalize_api(env.get("SAVEBIT_API") or args.api)
    code, p = check_key(api, args.key)
    if code != 0:
        sys.exit("Новый ключ не подошёл, оставляю прежний.")
    write_env(api, args.key)
    systemctl("restart", AGENT_SERVICE, timeout=30)
    print("Ключ заменён, агент перезапущен. Узел: %s (%s)" % (p["node"].get("name"), p["node"].get("role")))


def cmd_set_api(args):
    env = read_env()
    key = env.get("SAVEBIT_NODE_KEY")
    if not key:
        sys.exit("Агент не настроен: сначала установка.")
    api = normalize_api(args.api)
    code, _ = check_key(api, key)
    if code != 0:
        sys.exit("С новым адресом API ключ не проверился, оставляю прежний адрес.")
    write_env(api, key)
    systemctl("restart", AGENT_SERVICE, timeout=30)
    print("Адрес API заменён на %s, агент перезапущен." % api)


def cmd_status(args):
    env = read_env()
    st = load_state()
    node = st.get("node") or {}

    def ts(v):
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(v)) if v else "—"
    print("API:            %s" % (env.get("SAVEBIT_API") or "не настроен"))
    print("Узел:           %s (%s)" % (node.get("name", "—"), node.get("role", "—")))
    print("Ревизия:        %s, применена %s" % (st.get("revision", "—"), ts(st.get("applied_at"))))
    print("Xray:           %s, служба %s" % (installed_xray_version() or "не установлен",
                                             "работает" if service_active(XRAY_SERVICE) else "НЕ работает"))
    print("Агент:          служба %s" % ("работает" if service_active(AGENT_SERVICE) else "НЕ работает"))
    print("Синхронизация:  %s" % ts(st.get("last_sync")))
    print("Ошибка:         %s" % (st.get("last_error") or "нет"))
    for name, t in (st.get("tls") or {}).items():
        print("Сертификат %s: %s" % (name, t.get("sha256")))


def main():
    ap = argparse.ArgumentParser(prog="savebit-agent", description="Агент узла Savebit VPN")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("run", help="работать постоянно (так запускает systemd)")
    sub.add_parser("sync", help="одна синхронизация прямо сейчас")
    sub.add_parser("status", help="состояние узла")
    p = sub.add_parser("check", help="проверить ключ и адрес API, ничего не меняя")
    p.add_argument("--api")
    p.add_argument("--key")
    p = sub.add_parser("rekey", help="заменить ключ узла")
    p.add_argument("key")
    p.add_argument("--api")
    p = sub.add_parser("set-api", help="заменить адрес API")
    p.add_argument("api")
    sub.add_parser("version", help="версия агента")
    args = ap.parse_args()
    if args.cmd == "version":
        print(AGENT_VERSION)
        return
    handlers = {"run": cmd_run, "sync": cmd_sync, "status": cmd_status, "check": cmd_check,
                "rekey": cmd_rekey, "set-api": cmd_set_api}
    if args.cmd not in handlers:
        ap.print_help()
        sys.exit(1)
    handlers[args.cmd](args)


if __name__ == "__main__":
    main()
