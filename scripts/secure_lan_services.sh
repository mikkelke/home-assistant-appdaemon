#!/usr/bin/env bash
# Adds authentication to three LAN services on the rooftop box (10.21.0.5):
#   mqtt          Mosquitto: password file with one user per client, then no anonymous access
#   z2m-frontend  zigbee2mqtt frontend: frontend.auth_token
#   zwave-ui      Z-Wave JS UI: gateway.authEnabled plus a users.json login
#
# Run on the box as root:
#   secure_lan_services.sh [--dry-run]       inspect, validate and report; changes nothing
#   secure_lan_services.sh --apply           apply zwave-ui, z2m-frontend, mqtt in that order;
#                                            verify each and roll back a service that fails
#   secure_lan_services.sh --rollback NAME   undo one service (zwave-ui, z2m-frontend, mqtt) from the
#                                            newest --apply copies of its files; needs no Home
#                                            Assistant, which only verifies afterwards. mqtt and
#                                            z2m-frontend share configuration.yaml: roll back mqtt first.
#   --ignore-presence                        do not require zone.home == 0
#
# Secrets are generated here and never printed. Each is written, before it is used, to
# /root/new-service-credentials.json (mode 600) as {service, username, password, url, status}
# with status pending, active or rolled_back. Every run keeps its log in
# /root/secure-lan-services/<time>-<mode>/; an --apply run also keeps there the pre-change baseline
# and, per service, a copy of each file taken before that service first changed it.
#
# SIGINT and SIGTERM roll back the service in progress; during a rollback both are ignored.
# Exit codes:
#   0  dry-run: ready, or everything already secured and verified. --apply: every service secured
#      and verified. --rollback: restored and verified against the saved baseline.
#   1  --apply stopped in a service and rolled that service back, verified: it failed, was
#      interrupted, or someone came home in the mqtt stage before anonymous access was turned off.
#      Or a service secured by an earlier run failed its check; nothing was changed then.
#   2  usage or environment error; nothing changed.
#   3  stopped before a service started changing: a blocker, someone home (or someone came home
#      during the previous service), or interrupted between services. For the dry-run: not ready.
#   4  a rollback failed or the script crashed: the service in progress may be half-changed. See
#      run.log; the copies of its original files are in the run dir.
#   5  --rollback restored the files and restarted the containers but is UNVERIFIED: no saved
#      baseline, or Home Assistant unreachable or not back to the baseline.
# With 1 and 3, services logged "done and verified" earlier in the same run stay secured.
set -euo pipefail
if [[ ${EUID} -ne 0 ]]; then
    echo "must run as root" >&2
    exit 2
fi
exec python3 - "$@" <<'PY'
import argparse
import base64
import datetime as dt
import difflib
import fcntl
import glob
import hashlib
import json
import os
import re
import secrets
import shutil
import signal
import socket
import stat
import string
import struct
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

BOX = "10.21.0.5"
STATE_DIR = "/root/secure-lan-services"
CREDS_FILE = "/root/new-service-credentials.json"
LOCK_FILE = "/run/secure-lan-services.lock"
COMPOSE_FILE = "/data/docker-compose.yaml"
HA_URL = "http://127.0.0.1:8123"
HA_STORAGE = "/data/homeassistant/.storage"
AD_CONF = "/data/appdaemon/appdaemon.yaml"
ESPHOME_DIR = "/data/esphome"

MQ_CONF = "/data/mqtt/mosquitto.conf"
MQ_PASSWD = "/data/mqtt/passwd"
MQ_PASSWD_IN = "/mosquitto/config/passwd"
MQ_URL = f"mqtt://{BOX}:1883"
MQ_USERS = ("zigbee2mqtt", "homeassistant", "admin")
PROBE = "seclan-"

Z2M_CONF = "/data/zigbee2mqtt/configuration.yaml"
Z2M_URL = f"http://{BOX}:8085"
Z2M_PORT = 8085

ZW_SETTINGS = "/data/zwavejs2mqtt/settings.json"
ZW_USERS = "/data/zwavejs2mqtt/users.json"
ZW_URL = f"http://{BOX}:8091"
ZW_LOCAL = "http://127.0.0.1:8091"
ZW_USER = "mke"

IN_CONTAINER = {
    MQ_CONF: ("mqtt", "/mosquitto/config/mosquitto.conf"),
    MQ_PASSWD: ("mqtt", MQ_PASSWD_IN),
    Z2M_CONF: ("zigbee2mqtt", "/app/data/configuration.yaml"),
    ZW_SETTINGS: ("zwavejs2mqtt", "/usr/src/app/store/settings.json"),
    ZW_USERS: ("zwavejs2mqtt", "/usr/src/app/store/users.json"),
}

OBSERVE_S = 300
REPORT_WINDOW_S = 60
SECRET_KEY = re.compile(r"key|pass|token|secret", re.I)
UNACCOUNTED = ("the broker log since mosquitto started shows clients other than zigbee2mqtt, Home Assistant "
               "and this script that never reconnected with a user: ")

MODE = "dry-run"
DRY = True
RUN_DIR = None
LOG_FH = None
MQ_CHECK = (None, None)
STAGE = None
SECRETS = set()


class Fail(Exception):
    pass


class Fatal(Fail):
    """A failure that makes waiting pointless."""


class Blocked(Fail):
    """A precondition failed before anything was changed."""


class Interrupted(BaseException):
    """SIGINT or SIGTERM."""


def on_signal(signum, frame):
    raise Interrupted(signal.Signals(signum).name)


def hold_signals():
    """A rollback, once started, runs to its end."""
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)


def log(msg):
    for s in sorted(SECRETS, key=len, reverse=True):
        msg = msg.replace(s, "<redacted>")
    line = f"{dt.datetime.now(dt.timezone.utc):%H:%M:%SZ} {msg}"
    print(line, flush=True)
    if LOG_FH:
        LOG_FH.write(line + "\n")
        LOG_FH.flush()


def secret(value):
    """Registers a value that log() must never print."""
    if isinstance(value, str) and len(value) >= 8:
        SECRETS.add(value)
    return value


def secret_fields(obj, hide=False):
    """Registers every string under a secret-looking key of parsed config data; returns obj."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            secret_fields(v, hide or bool(SECRET_KEY.search(str(k))))
    elif isinstance(obj, list):
        for v in obj:
            secret_fields(v, hide)
    elif hide:
        secret(obj)
    return obj


def new_secret(n=32):
    alphabet = string.ascii_letters + string.digits
    return secret("".join(secrets.choice(alphabet) for _ in range(n)))


KEY_LINE = re.compile(r"(\s*(?:#\s*)?(?:-\s+)?)([\"']?)([^\s\"':#]+)\2(\s*:|\s+)(.*)")


def redact_lines(lines):
    """Lines of a YAML or mosquitto config with the value of every secret-looking key replaced,
    including a value that continues on the more-indented lines after it."""
    out, hide = [], None
    for line in lines:
        ind = len(line) - len(line.lstrip())
        if hide is not None and (not line.strip() or ind > hide
                                 or (ind == hide and line.lstrip().startswith("-"))):
            out.append(line[:ind] + "<redacted>" if line.strip() else line)
            continue
        hide = None
        m = KEY_LINE.fullmatch(line)
        if m and SECRET_KEY.search(m.group(3)):
            out.append(line[:m.start(4)] + m.group(4).strip() + " <redacted>")
            hide = ind
        else:
            out.append(line)
    return out


def show_diff(path, old, new, label=""):
    """Logs a unified diff, compared on the real lines, printed with secret values redacted."""
    a, b = old.splitlines(), new.splitlines()
    ra, rb = redact_lines(a), redact_lines(b)
    log(f"  {label + ': ' if label else ''}diff {path}")
    for group in difflib.SequenceMatcher(None, a, b, autojunk=False).get_grouped_opcodes(1):
        log(f"    @@ -{group[0][1] + 1} +{group[0][3] + 1} @@")
        for tag, i1, i2, j1, j2 in group:
            for line in ra[i1:i2]:
                log(f"    {' ' if tag == 'equal' else '-'}{line}")
            if tag != "equal":
                for line in rb[j1:j2]:
                    log(f"    +{line}")


def sh(args, input=None, check=True, timeout=180):
    p = subprocess.run(args, input=input, capture_output=True, text=True, timeout=timeout)
    if check and p.returncode != 0:
        out = (p.stderr or p.stdout).strip()[-300:]
        raise Fail(f"{' '.join(args[:5])} exited {p.returncode}: {out}")
    return p


def iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def docker_logs(name, since=None):
    args = ["docker", "logs"] + (["--since", iso(since)] if since else []) + [name]
    p = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, timeout=120)
    return re.sub(r"\x1b\[[0-9;]*m", "", p.stdout)


def running(name):
    p = sh(["docker", "inspect", "-f", "{{.State.Running}}", name], check=False)
    return p.stdout.strip() == "true"


def wait_for(desc, fn, timeout, interval=5):
    end = time.monotonic() + timeout
    last = None
    while True:
        try:
            result = fn()
            if result:
                return result
            last = result
        except Fatal:
            raise
        except Exception as e:
            last = f"{type(e).__name__}: {e}"
        if time.monotonic() >= end:
            raise Fail(f"timed out after {timeout}s waiting for {desc} (last: {last})")
        time.sleep(interval)


def require(cond, msg):
    if not cond:
        raise Fail(msg)


# ---------- files, backups, credentials ----------

def backup(path):
    """Keeps a copy of path as it was before the current stage first changed it in this run."""
    if DRY or not os.path.exists(path):
        return
    require(STAGE, f"internal error: {path} changed outside a stage")
    dest = os.path.join(RUN_DIR, "files", STAGE, path.lstrip("/"))
    if os.path.exists(dest):
        return
    os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
    shutil.copy2(path, dest)
    st = os.stat(path)
    os.chown(dest, st.st_uid, st.st_gid)
    log(f"backup {path} -> {dest}")


def snapshot(stage, path):
    """The copy of path taken before `stage` first changed it: from this run, or for --rollback from
    the newest --apply run that has one."""
    runs = [RUN_DIR] if MODE == "apply" else sorted(glob.glob(os.path.join(STATE_DIR, "*-apply")))
    hits = [p for p in (os.path.join(r, "files", stage, path.lstrip("/")) for r in runs) if os.path.exists(p)]
    return hits[-1] if hits else None


def file_meta(path):
    st = os.stat(path)
    return stat.S_IMODE(st.st_mode), st.st_uid, st.st_gid


def write_atomic(path, content, meta=None):
    """Replaces path with content (str or bytes); meta is (mode, uid, gid), default the current file's."""
    if DRY:
        raise Fail(f"internal error: write to {path} in dry-run")
    meta = meta or file_meta(path)
    backup(path)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".seclan-")
    try:
        with os.fdopen(fd, "wb" if isinstance(content, bytes) else "w") as f:
            f.write(content)
        os.chmod(tmp, meta[0])
        os.chown(tmp, meta[1], meta[2])
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def run_ids(container):
    """The uid:gid the container's processes run as; for mqtt the mosquitto process's own."""
    rows = [l.split(None, 3) for l in sh(["docker", "top", container, "-eo", "pid,uid,gid,comm"]).stdout.splitlines()[1:]]
    ids = {(int(r[1]), int(r[2])) for r in rows if len(r) == 4 and (container != "mqtt" or r[3] == "mosquitto")}
    require(len(ids) == 1, f"{container}: expected its processes under one uid:gid, found {sorted(ids)}")
    return ids.pop()


def readable(path, uid, gid):
    container, inside = IN_CONTAINER[path]
    p = sh(["docker", "exec", "-u", f"{uid}:{gid}", container, "test", "-r", inside], check=False)
    require(p.returncode == 0, f"{container} cannot read {inside} as {uid}:{gid}")


def write_secure(path, content):
    """Writes a file a container reads as mode 600 owned by the uid:gid the container runs as, then
    checks that the container can read it."""
    uid, gid = run_ids(IN_CONTAINER[path][0])
    write_atomic(path, content, (0o600, uid, gid))
    readable(path, uid, gid)


def perm_plan(paths):
    """Dry-run view of the permissions --apply sets, checked as the identity the container runs as."""
    for path in paths:
        container = IN_CONTAINER[path][0]
        mode, uid, gid = file_meta(path)
        ruid, rgid = run_ids(container)
        readable(path, ruid, rgid)
        log(f"  {path}: {mode:o} {uid}:{gid} now, 600 {ruid}:{rgid} after the change "
            f"({container} runs as {ruid}:{rgid} and can read it)")


def restore_file(stage, path, comparable=None):
    """Puts path back, byte for byte with its old mode and owner, from the copy taken before `stage`
    changed it. comparable(content) gives the data outside the stage's own keys, which must match
    between the copy and the current file; a current file it cannot parse is restored anyway.
    Returns False when there is no copy."""
    snap = snapshot(stage, path)
    if not snap:
        return False
    with open(snap, "rb") as f:
        old = f.read()
    with open(path, "rb") as f:
        cur = f.read()
    if cur != old:
        if comparable:
            try:
                now = comparable(cur)
            except (Fail, ValueError) as e:
                log(f"{path} does not parse ({e}); restoring the copy")
            else:
                require(now == comparable(old), f"{path} has other changes since {snap} was taken; "
                                                "restore it by hand")
        write_atomic(path, old, file_meta(snap))
        log(f"{path} restored from {snap}")
    return True


def creds_load():
    if not os.path.exists(CREDS_FILE):
        return []
    with open(CREDS_FILE) as f:
        return secret_fields(json.load(f))


def creds_save(entries):
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(CREDS_FILE), prefix=".seclan-creds-")
    with os.fdopen(fd, "w") as f:
        json.dump(entries, f, indent=2)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, CREDS_FILE)


def creds_put(service, url, logins):
    """Stores new {username: password} logins as pending, before they are used anywhere."""
    entries = creds_load()
    for e in entries:
        if e["service"] == service and e["username"] in logins and e.get("status") in ("pending", "active"):
            raise Blocked(f"{CREDS_FILE} already holds a {e['status']} {service}/{e['username'] or '-'} entry; "
                          "move it to Bitwarden or remove it first")
    entries = [e for e in entries if not (e["service"] == service and e["username"] in logins)]
    entries += [{"service": service, "username": u, "password": p, "url": url, "status": "pending"}
                for u, p in logins.items()]
    creds_save(entries)
    log(f"credentials {service}: {', '.join(u or '-' for u in logins)} generated and stored as pending")


def creds_mark(service, status, from_status=("pending",)):
    entries = creds_load()
    hit = [e for e in entries if e["service"] == service and e.get("status") in from_status]
    for e in hit:
        e["status"] = status
    if hit:
        creds_save(entries)
        log(f"credentials {service}: {', '.join(e['username'] or '-' for e in hit)} -> {status}")


def creds_get(service, username, status=("active",)):
    for e in creds_load():
        if e["service"] == service and e["username"] == username and e.get("status") in status:
            return e["password"]
    return None


# ---------- HTTP, websocket, MQTT probes ----------

def http(url, body=None, headers=None, timeout=10):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET",
                                 headers={"Content-Type": "application/json", **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def http_json(url, body=None, headers=None, timeout=10):
    status, raw = http(url, body, headers, timeout)
    return status, (json.loads(raw) if raw else None)


def ws_first_frame(port, path, timeout=10):
    """Opens a websocket on 127.0.0.1 and returns ('text', head) or ('close', code) or ('http', status)."""
    s = socket.create_connection(("127.0.0.1", port), timeout=timeout)
    try:
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nUpgrade: websocket\r\n"
                   f"Connection: Upgrade\r\nSec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            chunk = s.recv(65536)
            if not chunk:
                raise Fail("connection closed during websocket handshake")
            buf += chunk
        head, rest = buf.split(b"\r\n\r\n", 1)
        status = head.split(b"\r\n", 1)[0].decode(errors="replace")
        if status.split(" ")[1:2] != ["101"]:
            return ("http", status)

        def need(n):
            nonlocal rest
            while len(rest) < n:
                chunk = s.recv(65536)
                if not chunk:
                    raise Fail("connection closed before the first websocket frame")
                rest += chunk
            out, rest = rest[:n], rest[n:]
            return out

        b0, b1 = need(2)
        op, n = b0 & 0x0F, b1 & 0x7F
        if n == 126:
            n = struct.unpack("!H", need(2))[0]
        elif n == 127:
            n = struct.unpack("!Q", need(8))[0]
        payload = need(n)
        if op == 8:
            return ("close", struct.unpack("!H", payload[:2])[0] if len(payload) >= 2 else None)
        return ("text", payload[:100].decode(errors="replace"))
    finally:
        s.close()


def frame_desc(kind, info):
    """A z2m frontend frame for logs: its payload can carry config, so text frames show no content."""
    return kind if kind == "text" else f"{kind} {info}"


def _mq_str(x):
    b = x.encode()
    return struct.pack("!H", len(b)) + b


def _mq_len(n):
    out = bytearray()
    while True:
        d, n = n % 128, n // 128
        out.append(d | (0x80 if n else 0))
        if not n:
            return bytes(out)


class MqttProbe:
    """Minimal MQTT 3.1.1 client: connect, subscribe, collect. QoS 0 only."""

    def __init__(self, tag, user=None, password=None, timeout=10):
        self.sock = socket.create_connection(("127.0.0.1", 1883), timeout=timeout)
        flags = 0x02
        payload = _mq_str(f"{PROBE}{tag}-{os.getpid()}")
        if user is not None:
            flags |= 0x80
            payload += _mq_str(user)
            if password is not None:
                flags |= 0x40
                payload += _mq_str(password)
        body = _mq_str("MQTT") + bytes([4, flags]) + struct.pack("!H", 60) + payload
        self.sock.sendall(b"\x10" + _mq_len(len(body)) + body)
        h, b = self._packet()
        if h >> 4 != 2 or len(b) < 2:
            raise Fail("broker sent no CONNACK")
        self.rc = b[1]

    def _exact(self, n):
        buf = b""
        while len(buf) < n:
            chunk = self.sock.recv(n - len(buf))
            if not chunk:
                raise ConnectionError("broker closed the connection")
            buf += chunk
        return buf

    def _packet(self):
        h = self._exact(1)[0]
        mult, n = 1, 0
        while True:
            d = self._exact(1)[0]
            n += (d & 0x7F) * mult
            if not d & 0x80:
                break
            mult *= 128
        return h, (self._exact(n) if n else b"")

    def subscribe(self, topics):
        body = struct.pack("!H", 1) + b"".join(_mq_str(t) + b"\x00" for t in topics)
        self.sock.sendall(b"\x82" + _mq_len(len(body)) + body)

    def collect(self, seconds):
        msgs = []
        end = time.monotonic() + seconds
        while (left := end - time.monotonic()) > 0:
            self.sock.settimeout(left)
            try:
                h, b = self._packet()
            except socket.timeout:
                break
            if h >> 4 == 3:
                tl = struct.unpack("!H", b[:2])[0]
                off = 2 + tl + (2 if (h >> 1) & 3 else 0)
                msgs.append((b[2:2 + tl].decode(errors="replace"), b[off:], bool(h & 1)))
        return msgs

    def close(self):
        try:
            self.sock.sendall(b"\xe0\x00")
        except OSError:
            pass
        self.sock.close()


def mq_try(tag, user=None, password=None):
    m = MqttProbe(tag, user, password)
    rc = m.rc
    m.close()
    return rc


def mq_collect(topics, seconds):
    m = MqttProbe("check", *MQ_CHECK)
    try:
        if m.rc != 0:
            raise Fail(f"broker refused the check connection (CONNACK {m.rc})")
        m.subscribe(topics)
        return m.collect(seconds)
    finally:
        m.close()


def z2m_bridge_online():
    for _, payload, _ in mq_collect(["zigbee2mqtt/bridge/state"], 3):
        try:
            return json.loads(payload).get("state") == "online"
        except ValueError:
            return payload.decode(errors="replace") == "online"
    return False


def z2m_reporting(seconds):
    msgs = mq_collect(["zigbee2mqtt/+"], seconds)
    return len({t for t, _, retained in msgs if not retained and t != "zigbee2mqtt/bridge"})


def mq_sys_connected():
    vals = [p for t, p, _ in mq_collect(["$SYS/broker/clients/connected"], 12)]
    return int(vals[-1]) if vals else None


# ---------- broker log ----------

CONNECT_RE = re.compile(r"New client connected from (\S+) as (\S+) \(([^)]*)\)")
DISCONNECT_RE = re.compile(r"Client (\S+) \[([^\]]+)\] disconnected(.*)$")


def broker_events(since=None):
    events = []
    for line in docker_logs("mqtt", since).splitlines():
        if re.search(r"mosquitto version \S+ starting", line):
            events.append(("start",))
            continue
        m = CONNECT_RE.search(line)
        if m:
            addr, cid, opts = m.groups()
            user = re.search(r"u'([^']*)'", opts)
            events.append(("connect", addr, cid, opts.split(",")[0].strip(), user.group(1) if user else None))
            continue
        m = DISCONNECT_RE.search(line)
        if m:
            events.append(("disconnect", m.group(2), m.group(1), m.group(3).strip(" :.")))
    return events


def broker_connected():
    conns = {}
    for ev in broker_events():
        if ev[0] == "start":
            conns = {}
        elif ev[0] == "connect":
            _, addr, cid, proto, user = ev
            for a in [a for a, c in conns.items() if c["cid"] == cid]:
                del conns[a]
            conns[addr] = {"addr": addr, "ip": addr.rsplit(":", 1)[0], "cid": cid, "proto": proto, "user": user}
        else:
            conns.pop(ev[1], None)
    return conns


def broker_rejections(since):
    return [ev for ev in broker_events(since)
            if ev[0] == "disconnect" and "not authorised" in ev[3] and not ev[2].startswith(PROBE)]


def fmt_client(c):
    return f"{c['cid']} from {c['ip']} ({c['proto']}, {'user ' + c['user'] if c['user'] else 'anonymous'})"


def mq_unaccounted(ctx):
    """Clients in the broker log since mosquitto last started that are not zigbee2mqtt, Home Assistant
    or this script and whose latest connect under their client id was anonymous."""
    last = {}
    for ev in broker_events():
        if ev[0] == "start":
            last = {}
        elif ev[0] == "connect":
            _, addr, cid, proto, user = ev
            ip = addr.rsplit(":", 1)[0]
            if not (cid.startswith(PROBE) or ip == ctx.z2m_ip or (ip == ctx.gw_ip and proto == "p5")):
                last[cid] = (ip, proto, user)
    return [f"{cid} from {ip} ({proto})" for cid, (ip, proto, user) in last.items() if user is None]


# ---------- Home Assistant ----------

class HA:
    def __init__(self, token):
        self.token = token

    def call(self, method, path, body=None, timeout=30):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(HA_URL + path, data=data, method=method,
                                     headers={"Authorization": "Bearer " + self.token,
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                raw = r.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as e:
            raise Fail(f"HA {method} {path.split('?')[0]} -> HTTP {e.code}: "
                       f"{e.read()[:200].decode(errors='replace')}") from None


def ha_connect():
    candidates = []
    if os.environ.get("HA_TOKEN_FILE"):
        with open(os.environ["HA_TOKEN_FILE"]) as f:
            candidates.append(("HA_TOKEN_FILE", secret(f.read().strip())))
    compose = open(COMPOSE_FILE).read()
    m = re.search(r"HOMEASSISTANT_TOKEN:\s*[\"']?([A-Za-z0-9._-]{100,})", compose)
    if m:
        candidates.append(("ha-mcp token in docker-compose.yaml", secret(m.group(1))))
    m = re.search(r"^\s*-\s*TOKEN=([A-Za-z0-9._-]{100,})", compose, re.M)
    if m:
        candidates.append(("appdaemon token in docker-compose.yaml", secret(m.group(1))))
    for source, token in candidates:
        ha = HA(token)
        try:
            ha.call("GET", "/api/config/config_entries/entry?domain=mqtt")
        except (Fail, OSError):
            continue
        return ha, source
    raise Fail("no Home Assistant admin token works; set HA_TOKEN_FILE")


def ha_entry(ha, domain):
    entries = ha.call("GET", f"/api/config/config_entries/entry?domain={domain}")
    require(len(entries) == 1, f"expected one {domain} config entry, found {len(entries)}")
    return entries[0]


def ha_entry_data(domain):
    with open(f"{HA_STORAGE}/core.config_entries") as f:
        entries = [e for e in json.load(f)["data"]["entries"] if e["domain"] == domain]
    require(len(entries) == 1, f"expected one {domain} entry in core.config_entries, found {len(entries)}")
    return entries[0]["entry_id"], secret_fields(dict(entries[0]["data"]))


def platform_ids(platform):
    with open(f"{HA_STORAGE}/core.entity_registry") as f:
        reg = json.load(f)["data"]["entities"]
    return sorted(e["entity_id"] for e in reg if e["platform"] == platform and not e.get("disabled_by"))


def ha_counts(ha, ids):
    """Health of these entities in /api/states; an entity missing there counts as unavailable."""
    states = {s["entity_id"]: s["state"] for s in ha.call("GET", "/api/states")}
    got = [states.get(i) for i in ids]
    return {"total": len(ids), "missing": got.count(None),
            "unavailable": sum(s in (None, "unavailable") for s in got),
            "not_ok": sum(s in (None, "unknown", "unavailable") for s in got)}


def ha_state(ha, entity_id):
    try:
        return ha.call("GET", f"/api/states/{entity_id}").get("state")
    except Fail:
        return None


def ha_home_count(ha):
    return ha_state(ha, "zone.home")


def presence_gate(ctx, before, abort=False):
    """Rechecks zone.home right before a disruptive action. Someone home: with abort the stage fails
    and rolls back, otherwise it finishes and the run stops after it."""
    if ctx.ignore_presence:
        return
    n = ha_home_count(ctx.ha)
    if n == "0":
        return
    if abort:
        raise Fail(f"someone came home (zone.home {n}) before {before}")
    if not ctx.came_home:
        log(f"zone.home is {n} before {before}; finishing {STAGE}, then stopping")
    ctx.came_home = True


def ha_mqtt_reconfigure(ha, username, password, dry=False):
    """Runs the MQTT reconfigure flow. username None removes the credentials."""
    entry_id, data = ha_entry_data("mqtt")
    odd = [k for k in ("certificate", "client_cert", "client_key", "ws_path", "ws_headers") if data.get(k)]
    if odd or data.get("transport", "tcp") != "tcp":
        raise Fail(f"HA MQTT entry uses {odd or data.get('transport')}; reconfigure it by hand")
    flow = ha.call("POST", "/api/config/config_entries/flow", {"handler": "mqtt", "entry_id": entry_id})
    flow_id = flow.get("flow_id")
    try:
        fields = {f.get("name") for f in flow.get("data_schema") or []}
        need = {"broker", "port", "protocol", "username", "password", "other_settings"}
        if flow.get("type") != "form" or flow.get("step_id") != "broker" or not need <= fields:
            raise Fail(f"unexpected MQTT reconfigure form: type={flow.get('type')} "
                       f"step={flow.get('step_id')} fields={sorted(f for f in fields if f)}")
        if dry:
            return f"reconfigure form ok, fields {sorted(fields)}"
        other = {"set_client_cert": False, "set_ca_cert": "off", "transport": "tcp"}
        for k in ("client_id", "keepalive"):
            if k in data:
                other[k] = data[k]
        body = {"broker": data["broker"], "port": data["port"], "protocol": data.get("protocol", "5"),
                "other_settings": other}
        if username:
            body["username"] = username
            body["password"] = password
        else:
            body["password"] = ""
        result = ha.call("POST", f"/api/config/config_entries/flow/{flow_id}", body, timeout=60)
        if result.get("type") in ("abort", "create_entry"):
            flow_id = None
        if result.get("type") != "abort" or result.get("reason") != "reconfigure_successful":
            raise Fail(f"HA rejected the MQTT reconfigure: {result.get('type')} "
                       f"{result.get('reason') or result.get('errors')}")
        return "reconfigured"
    finally:
        if flow_id:
            try:
                ha.call("DELETE", f"/api/config/config_entries/flow/{flow_id}")
            except Fail:
                pass


# ---------- zigbee2mqtt ----------

def yaml_load(text):
    """Parses the zigbee2mqtt config; an error gives its position only, never the text around it."""
    import yaml
    try:
        return yaml.safe_load(text)
    except yaml.YAMLError as e:
        mark = getattr(e, "problem_mark", None) or getattr(e, "context_mark", None)
        at = f"line {mark.line + 1}, column {mark.column + 1}" if mark else "an unknown position"
        raise Fail(f"{Z2M_CONF}: YAML {type(e).__name__} at {at}") from None


def z2m_conf():
    with open(Z2M_CONF) as f:
        data = yaml_load(f.read())
    require(isinstance(data, dict), f"{Z2M_CONF} is not a YAML mapping")
    return secret_fields(data)


def yaml_key_at(line, key):
    """True when a YAML line names `key` as a mapping key: plain or quoted, with any value, or as an
    explicit '? key'."""
    if line.lstrip().startswith("#"):
        return False
    k = re.escape(key)
    return bool(re.search(rf"(?:^|[\s{{\[,?])(['\"]?){k}\1\s*:", line)
                or re.match(rf"\s*\?\s*(['\"]?){k}\1\s*(?:#.*)?$", line))


def yaml_block_insert(text, block, key, value):
    """Adds block.key as the last line of a top-level YAML mapping block; every other line stays as is."""
    lines = text.splitlines(keepends=True)
    heads = [i for i, l in enumerate(lines) if re.fullmatch(rf"{re.escape(block)}:[ \t]*(#.*)?", l.rstrip("\r\n"))]
    require(len(heads) == 1, f"{Z2M_CONF}: expected one '{block}:' block, found {len(heads)}")
    h = heads[0]
    end = len(lines)
    for j in range(h + 1, len(lines)):
        if lines[j].strip() and not lines[j][0].isspace() and not lines[j].lstrip().startswith("#"):
            end = j
            break
    found = [j + 1 for j in range(h + 1, end) if yaml_key_at(lines[j], key)]
    if found:
        raise Fail(f"{Z2M_CONF}: '{block}:' already names {key} on line {found[0]}; edit it by hand")
    body = [j for j in range(h + 1, end) if lines[j].strip() and not lines[j].lstrip().startswith("#")]
    require(body, f"{Z2M_CONF}: '{block}:' block is empty")
    m = re.match(r" +", lines[body[0]])
    require(m, f"{Z2M_CONF}: '{block}:' block is not indented with spaces")
    if not lines[body[-1]].endswith("\n"):
        lines[body[-1]] += "\n"
    lines.insert(body[-1] + 1, f"{m.group(0)}{key}: '{str(value).replace(chr(39), chr(39) * 2)}'\n")
    return "".join(lines)


def z2m_edit(changes, text):
    """configuration.yaml text with new block.key lines added. Changes nothing itself; Blocked when a
    key exists already, in any spelling, or the result would differ in anything else."""
    try:
        expected = yaml_load(text)
        new = text
        for block, key, value in changes:
            section = expected.get(block) if isinstance(expected, dict) else None
            require(isinstance(section, dict), f"{Z2M_CONF}: '{block}' is not a mapping")
            require(key not in section, f"{Z2M_CONF}: {block}.{key} exists already; edit it by hand")
            new = yaml_block_insert(new, block, key, value)
            section[key] = value
        require(yaml_load(new) == expected,
                f"{Z2M_CONF}: the edit would change more than {[f'{b}.{k}' for b, k, _ in changes]}")
    except Fail as e:
        raise Blocked(str(e)) from None
    return new


def z2m_without(text, keys):
    data = yaml_load(text)
    for block, key in keys:
        if isinstance(data, dict) and isinstance(data.get(block), dict):
            data[block].pop(key, None)
    return data


def z2m_restore(stage, keys):
    """configuration.yaml back to the copy taken before `stage` changed it. Without a copy the file
    must already lack the stage's keys."""
    if restore_file(stage, Z2M_CONF, lambda t: z2m_without(t, keys)):
        return
    data = z2m_conf()
    left = [f"{b}.{k}" for b, k in keys if k in (data.get(b) or {})]
    require(not left, f"{Z2M_CONF} has {left} and no copy from {stage} to restore; remove them by hand")


def z2m_restart():
    t0 = time.time()
    log("restarting zigbee2mqtt")
    sh(["docker", "restart", "-t", "30", "zigbee2mqtt"], timeout=120)

    def started():
        text = docker_logs("zigbee2mqtt", t0)
        bad = [l for l in text.splitlines() if re.search(r"not authori[sz]ed|MQTT failed to connect|Failed to start", l, re.I)]
        if bad:
            raise Fatal(f"zigbee2mqtt: {bad[0].strip()[:200]}")
        return "Zigbee2MQTT started!" in text

    wait_for("zigbee2mqtt to start", started, 150, 3)
    wait_for("zigbee2mqtt bridge/state online", z2m_bridge_online, 60, 5)
    log(f"zigbee2mqtt up after {time.time() - t0:.0f}s")
    return t0


def container_ip(name):
    ips = sh(["docker", "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", name]).stdout.split()
    require(len(ips) == 1, f"{name}: expected one container IP, got {ips}")
    return ips[0]


def mqtt_gateway_ip():
    nets = sh(["docker", "inspect", "-f", "{{range $k, $v := .NetworkSettings.Networks}}{{$k}} {{end}}", "mqtt"]).stdout.split()
    require(len(nets) == 1, f"mqtt: expected one network, got {nets}")
    gws = sh(["docker", "network", "inspect", "-f", "{{range .IPAM.Config}}{{.Gateway}} {{end}}", nets[0]]).stdout.split()
    require(len(gws) >= 1, f"network {nets[0]} has no gateway")
    return gws[0]


def mq_classify(ctx, conns):
    z2m = [c for c in conns.values() if c["ip"] == ctx.z2m_ip]
    ha = [c for c in conns.values() if c["ip"] == ctx.gw_ip and c["proto"] == "p5" and not c["cid"].startswith(PROBE)]
    other = [c for c in conns.values() if c not in z2m and c not in ha and not c["cid"].startswith(PROBE)]
    return z2m, ha, other


def health_check(ctx, desc, timeout=240):
    """Zigbee side after a change: bridge online, devices reporting, HA's MQTT entities back."""
    require(ctx.base, "no saved baseline from an --apply run")
    wait_for("zigbee2mqtt bridge online", z2m_bridge_online, 90, 5)
    need = max(1, ctx.base["reporting"] // 3)
    got = wait_for(f"{need}+ zigbee devices reporting",
                   lambda: (n := z2m_reporting(REPORT_WINDOW_S)) >= need and n, timeout, 1)
    log(f"{desc}: {got} zigbee devices reported within {REPORT_WINDOW_S}s "
        f"(baseline {ctx.base['reporting']}, need {need})")
    base = ctx.base["mqtt"]
    tol = max(10, base["total"] // 20)
    wait_for(f"HA MQTT entities back (unknown+unavailable+missing <= {base['not_ok'] + tol})",
             lambda: ha_counts(ctx.ha, base["ids"])["not_ok"] <= base["not_ok"] + tol, timeout, 10)
    wait_for("HA binary_sensor.zigbee2mqtt_bridge_connection_state on",
             lambda: ha_state(ctx.ha, "binary_sensor.zigbee2mqtt_bridge_connection_state") == "on", 120, 5)
    now = ha_counts(ctx.ha, base["ids"])
    log(f"{desc}: HA MQTT entities {now['total']}, unknown+unavailable {now['not_ok']} of which "
        f"{now['missing']} missing (baseline {base['not_ok']}), bridge entity on")


# ---------- Mosquitto ----------

PW_LINE = f"password_file {MQ_PASSWD_IN}"


def mq_conf_parse(text):
    directives = [l.split(None, 1) for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    keys = [d[0] for d in directives]
    odd = {"per_listener_settings", "acl_file", "plugin", "auth_plugin", "include_dir"} & set(keys)
    if keys.count("listener") != 1 or keys.count("allow_anonymous") != 1 or keys.count("password_file") > 1 or odd:
        raise Fail(f"{MQ_CONF} has a layout this script does not handle: {keys}")
    pwfile = [d[1].strip() for d in directives if d[0] == "password_file"]
    if pwfile and pwfile[0] != MQ_PASSWD_IN:
        raise Fail(f"{MQ_CONF} already uses another password file: {pwfile[0]}")
    anon = [d[1].strip() for d in directives if d[0] == "allow_anonymous"][0] == "true"
    return bool(pwfile), anon


def mq_conf_render(text, pwfile, anon):
    mq_conf_parse(text)
    out = []
    for line in text.splitlines():
        w = line.split(None, 1)
        if w and w[0] == "password_file":
            continue
        if w and w[0] == "allow_anonymous":
            line = f"allow_anonymous {'true' if anon else 'false'}"
        out.append(line)
    if pwfile:
        out.append(PW_LINE)
    return "\n".join(out) + "\n"


def mq_test_config(text):
    p = sh(["docker", "exec", "-i", "mqtt", "sh", "-c",
            'f=$(mktemp) && cat > "$f" && mosquitto --test-config -c "$f"; rc=$?; rm -f "$f"; exit $rc'],
           input=text, check=False)
    if p.returncode != 0:
        raise Fail(f"mosquitto --test-config rejected the config: {(p.stdout + p.stderr).strip()[-300:]}")


def mq_reload():
    t0 = time.time()
    sh(["docker", "kill", "-s", "HUP", "mqtt"])
    wait_for("mosquitto 'Reloading config.'", lambda: "Reloading config." in docker_logs("mqtt", t0 - 1), 20, 1)
    time.sleep(2)
    if not running("mqtt"):
        raise Fatal("mosquitto stopped after the reload")
    bad = [l for l in docker_logs("mqtt", t0 - 1).splitlines() if "Error" in l or "terminating" in l]
    if bad:
        raise Fatal(f"mosquitto after reload: {bad[0].strip()}")
    return t0


def mq_set_conf(pwfile, anon):
    """Writes mosquitto.conf for the wanted state and reloads the broker, or restarts it if it is down.

    The original form (no password file, anonymous allowed) is written even when the broker is down,
    so a rollback works after a failed reload.
    """
    current = open(MQ_CONF).read()
    wanted = mq_conf_render(current, pwfile, anon)
    original = not pwfile and anon
    if pwfile:
        uid, gid = run_ids("mqtt")
        st = os.stat(MQ_PASSWD)
        require((st.st_uid, st.st_gid, stat.S_IMODE(st.st_mode)) == (uid, gid, 0o600),
                f"{MQ_PASSWD} must be {uid}:{gid} mode 600")
        # mosquitto exits on reload when it cannot read the password file
        readable(MQ_PASSWD, uid, gid)
        mq_test_config(wanted)
        if wanted != current:
            write_secure(MQ_CONF, wanted)
    elif wanted != current:
        write_atomic(MQ_CONF, wanted)
    log(f"mosquitto.conf: password_file={'on' if pwfile else 'off'}, allow_anonymous={'true' if anon else 'false'}")
    if running("mqtt"):
        try:
            return mq_reload()
        except Fatal:
            if not original:
                raise
            log("reload failed; restarting mosquitto with the original config")
    t0 = time.time()
    sh(["docker", "restart", "-t", "10", "mqtt"])
    wait_for("mosquitto running", lambda: running("mqtt") and " running" in docker_logs("mqtt", t0 - 1), 60, 2)
    log("mosquitto restarted")
    return t0


def mq_build_passwd(pw):
    uid, gid = run_ids("mqtt")
    tmp_host, tmp_in = MQ_PASSWD + ".seclan-new", MQ_PASSWD_IN + ".seclan-new"
    if os.path.exists(tmp_host):
        os.unlink(tmp_host)
    try:
        for i, user in enumerate(MQ_USERS):
            sh(["docker", "exec", "-i", "-u", "0", "mqtt", "mosquitto_passwd"] + (["-c"] if i == 0 else [])
               + [tmp_in, user], input=f"{pw[user]}\n{pw[user]}\n")
        rows = [l.split(":", 1) for l in open(tmp_host).read().splitlines() if l.strip()]
        require([r[0] for r in rows] == list(MQ_USERS) and all(len(r) == 2 and r[1].startswith("$") for r in rows),
                "password file does not hold exactly the expected users")
        os.chown(tmp_host, uid, gid)
        os.chmod(tmp_host, 0o600)
        backup(MQ_PASSWD)
        os.replace(tmp_host, MQ_PASSWD)
    finally:
        if os.path.exists(tmp_host):
            os.unlink(tmp_host)
    log(f"{MQ_PASSWD}: {', '.join(MQ_USERS)} (owner {uid}:{gid}, mode 600)")


def mq_state():
    pwfile, anon = mq_conf_parse(open(MQ_CONF).read())
    z2m_user = (z2m_conf().get("mqtt") or {}).get("user")
    ha_user = ha_entry_data("mqtt")[1].get("username")
    if not pwfile and anon and not z2m_user and not ha_user:
        return "todo"
    if pwfile and not anon and z2m_user and ha_user:
        return "done"
    return (f"partial (password_file={pwfile}, allow_anonymous={anon}, z2m user={z2m_user!r}, "
            f"HA username={ha_user!r})")


# ---------- services ----------
#
# Each service has apply(ctx); restore(ctx), the local part of its rollback, which needs no Home
# Assistant and restarts its containers every time; and recheck(ctx), which verifies the rollback in
# Home Assistant against the baseline.

class ZWaveUI:
    name = "zwave-ui"
    service = "zwave-js-ui"

    def state(self):
        with open(ZW_SETTINGS) as f:
            return "done" if json.load(f).get("gateway", {}).get("authEnabled") is True else "todo"

    @staticmethod
    def hash(password, salt=None):
        # zwave-js-ui: crypto.scrypt(password, <16 hex chars as text>, 64) with node defaults N=16384 r=8 p=1
        salt = salt or secrets.token_hex(8)
        key = hashlib.scrypt(password.encode(), salt=salt.encode(), n=16384, r=8, p=1, dklen=64, maxmem=64 << 20)
        return f"{salt}:{key.hex()}"

    def plan(self, ctx):
        raw = open(ZW_SETTINGS).read()
        settings = secret_fields(json.loads(raw))
        users = json.load(open(ZW_USERS))
        default = [u["username"] for u in users if self.hash("zwave", u["passwordHash"].split(":")[0]) == u["passwordHash"]]
        log(f"  settings.json gateway.authEnabled={settings['gateway'].get('authEnabled')}, "
            f"mqtt.disabled={settings.get('mqtt', {}).get('disabled')}")
        log(f"  users.json users {[u['username'] for u in users]}; still on the public default password 'zwave': {default}")
        compact = json.dumps(settings, separators=(",", ":"), ensure_ascii=False) == raw.rstrip("\n")
        log(f"  settings.json is compact JSON, rewrite keeps the format: {compact}")
        probe = new_secret()
        h = self.hash(probe)
        require(len(h) == 145 and self.hash(probe, h.split(":")[0]) == h, "scrypt self-test failed")
        perm_plan([ZW_SETTINGS, ZW_USERS])
        _, r = http_json(ZW_LOCAL + "/api/auth-enabled")
        log(f"  UI /api/auth-enabled now: {r}")
        log(f"  Z-Wave JS server :3000 first frame: {ws_first_frame(3000, '/')}")
        log(f"  change: gateway.authEnabled -> true; users.json -> [{ZW_USER}] with a new scrypt hash; "
            "restart zwavejs2mqtt")

    def verify_done(self, ctx):
        _, r = http_json(ZW_LOCAL + "/api/auth-enabled")
        require(r and r.get("data") is True, f"/api/auth-enabled says {r}")
        _, r = http_json(ZW_LOCAL + "/api/settings")
        require(r and r.get("success") is False, "/api/settings answers without a login")
        kind, info = ws_first_frame(3000, "/")
        require(kind == "text" and '"version"' in info, f"port 3000 answered {kind} {info}")
        require(ha_entry(ctx.ha, "zwave_js")["state"] == "loaded", "HA zwave_js is not loaded")
        log("  verified: UI requires login, port 3000 serves HA without auth, HA zwave_js loaded")

    def zwave_ha_ok(self, ctx):
        require(ctx.base, "no saved baseline from an --apply run")
        base = ctx.base["zwave_js"]

        def ok():
            return (ha_entry(ctx.ha, "zwave_js")["state"] == "loaded"
                    and ha_counts(ctx.ha, base["ids"])["unavailable"] <= base["unavailable"])

        wait_for(f"HA zwave_js loaded with Z-Wave entities available (unavailable+missing <= {base['unavailable']})",
                 ok, 300, 10)
        time.sleep(15)
        require(ok(), "HA zwave_js did not stay connected")
        log(f"zwave: HA zwave_js loaded, unavailable {ha_counts(ctx.ha, base['ids'])['unavailable']} "
            f"(baseline {base['unavailable']})")

    @staticmethod
    def restart():
        t0 = time.time()
        log("restarting zwavejs2mqtt")
        sh(["docker", "restart", "-t", "30", "zwavejs2mqtt"], timeout=120)
        wait_for("Z-Wave JS server listening", lambda: "ZwaveJS server listening" in docker_logs("zwavejs2mqtt", t0), 180, 3)
        kind, info = wait_for("port 3000 version message", lambda: ws_first_frame(3000, "/"), 60, 3)
        require(kind == "text" and '"version"' in info, f"port 3000 answered {kind} {info}")
        log(f"zwave: up after {time.time() - t0:.0f}s, port 3000 sends its version message without auth")

    def apply(self, ctx):
        if ha_entry(ctx.ha, "zwave_js")["state"] != "loaded":
            raise Blocked("HA zwave_js is not loaded before the change")
        password = new_secret()
        settings = secret_fields(json.loads(open(ZW_SETTINGS).read()))
        settings["gateway"]["authEnabled"] = True
        creds_put(self.service, ZW_URL, {ZW_USER: password})
        write_secure(ZW_USERS, json.dumps([{"username": ZW_USER, "passwordHash": self.hash(password)}],
                                          separators=(",", ":")))
        write_secure(ZW_SETTINGS, json.dumps(settings, separators=(",", ":"), ensure_ascii=False))
        presence_gate(ctx, "the zwavejs2mqtt restart")
        self.restart()
        _, r = http_json(ZW_LOCAL + "/api/auth-enabled")
        require(r and r.get("data") is True, f"/api/auth-enabled says {r}")
        _, r = http_json(ZW_LOCAL + "/api/settings")
        require(r and r.get("success") is False, "/api/settings answers without a login")
        _, r = http_json(ZW_LOCAL + "/api/authenticate", {"username": ZW_USER, "password": "x" + new_secret(12)})
        require(r and r.get("success") is False, "a wrong password was accepted")
        _, r = http_json(ZW_LOCAL + "/api/authenticate", {"username": ZW_USER, "password": password})
        require(r and r.get("success") is True, "login with the new password failed")
        _, s = http_json(ZW_LOCAL + "/api/settings", headers={"x-access-token": r["user"]["token"]})
        require(s and s.get("success") is True, "the login token does not open /api/settings")
        log("zwave: UI requires login, wrong password refused, new login works")
        self.zwave_ha_ok(ctx)
        creds_mark(self.service, "active")

    def restore(self, ctx):
        try:
            settings = secret_fields(json.loads(open(ZW_SETTINGS).read()))
            settings["gateway"]["authEnabled"] = False
            wanted = json.dumps(settings, separators=(",", ":"), ensure_ascii=False)
        except (ValueError, KeyError, TypeError):
            require(restore_file(self.name, ZW_SETTINGS), f"{ZW_SETTINGS} does not parse and has no copy to restore")
        else:
            if wanted != open(ZW_SETTINGS).read():
                write_atomic(ZW_SETTINGS, wanted)
        restore_file(self.name, ZW_USERS)
        self.restart()
        _, r = http_json(ZW_LOCAL + "/api/auth-enabled")
        require(r and r.get("data") is False, f"/api/auth-enabled says {r} after rollback")

    def recheck(self, ctx):
        self.zwave_ha_ok(ctx)


class Z2MFrontend:
    name = "z2m-frontend"
    service = "zigbee2mqtt-frontend"
    keys = [("frontend", "auth_token")]

    def state(self):
        return "done" if (z2m_conf().get("frontend") or {}).get("auth_token") else "todo"

    def plan(self, ctx):
        text = open(Z2M_CONF).read()
        show_diff(Z2M_CONF, text, z2m_edit([("frontend", "auth_token", new_secret())], text))
        perm_plan([Z2M_CONF])
        log(f"  frontend websocket now: {frame_desc(*ws_first_frame(Z2M_PORT, '/api'))} (no token sent)")
        log("  change: add frontend.auth_token, restart zigbee2mqtt; the maintenance link stays "
            "http://10.21.0.5:8085 and the UI asks for the token once per browser")

    def verify_done(self, ctx):
        self.check_auth((z2m_conf().get("frontend") or {}).get("auth_token"))

    def check_auth(self, token):
        kind, info = ws_first_frame(Z2M_PORT, "/api")
        require((kind, info) == ("close", 4401), f"frontend without token answered {frame_desc(kind, info)}, "
                                                 "expected close 4401")
        kind, info = ws_first_frame(Z2M_PORT, f"/api?token={token}")
        require(kind == "text", f"frontend with token answered {frame_desc(kind, info)}")
        status, _ = http(f"http://127.0.0.1:{Z2M_PORT}/")
        require(status == 200, f"frontend page answered HTTP {status}")
        log("z2m-frontend: websocket refuses no token (4401), accepts the token, UI page serves")

    def apply(self, ctx):
        token = new_secret()
        new = z2m_edit([("frontend", "auth_token", token)], open(Z2M_CONF).read())
        creds_put(self.service, Z2M_URL, {"": token})
        write_secure(Z2M_CONF, new)
        presence_gate(ctx, "the zigbee2mqtt restart")
        z2m_restart()
        self.check_auth(token)
        health_check(ctx, "z2m-frontend")
        creds_mark(self.service, "active")

    def restore(self, ctx):
        z2m_restore(self.name, self.keys)
        z2m_restart()
        kind, info = ws_first_frame(Z2M_PORT, "/api")
        require(kind == "text", f"frontend after rollback answered {frame_desc(kind, info)}")

    def recheck(self, ctx):
        health_check(ctx, "z2m-frontend rollback")


class MqttAuth:
    name = "mqtt"
    service = "mqtt"
    keys = [("mqtt", "user"), ("mqtt", "password")]

    def state(self):
        return mq_state()

    def plan(self, ctx):
        text = open(MQ_CONF).read()
        stage1, stage2 = mq_conf_render(text, True, True), mq_conf_render(text, True, False)
        show_diff(MQ_CONF, text, stage1, "step 1")
        show_diff(MQ_CONF, stage1, stage2, "step 4")
        mq_test_config(stage1)
        mq_test_config(stage2)
        log("  mosquitto --test-config accepts both new configs")
        uid, gid = run_ids("mqtt")
        log(f"  password file {MQ_PASSWD} for {', '.join(MQ_USERS)}, built with mosquitto_passwd in the "
            f"container, owner {uid}:{gid} mode 600 (the mosquitto process runs as {uid}:{gid})")
        perm_plan([MQ_CONF])
        z2m = open(Z2M_CONF).read()
        show_diff(Z2M_CONF, z2m, z2m_edit([("mqtt", "user", "zigbee2mqtt"), ("mqtt", "password", new_secret())], z2m),
                  "step 2")
        log("  " + ha_mqtt_reconfigure(ctx.ha, None, None, dry=True)
            + " (flow started and deleted again, nothing saved)")
        entry_id, data = ha_entry_data("mqtt")
        log(f"  step 3: HA MQTT entry {entry_id} {({k: v for k, v in data.items() if not SECRET_KEY.search(k)})} "
            "gets username homeassistant + password via the reconfigure flow")

    def verify_done(self, ctx):
        require(mq_try("anon") != 0, "broker accepts anonymous clients")
        require(MQ_CHECK[0] and mq_try("check", *MQ_CHECK) == 0, "broker refused the check credentials")
        self.gate_authenticated(ctx)
        refused = {(e[2], e[1].rsplit(":", 1)[0]) for e in broker_rejections(time.time() - 86400)}
        require(not refused, f"clients refused in the last 24 h: {sorted(refused)}")
        log("  verified: anonymous refused, zigbee2mqtt and Home Assistant connected with their users, "
            "no client refused in the last 24 h")

    def gate_before(self, ctx):
        z2m, ha, other = mq_classify(ctx, broker_connected())
        if len(z2m) != 1 or len(ha) != 1 or other:
            raise Blocked(f"expected exactly zigbee2mqtt from {ctx.z2m_ip} and one Home Assistant p5 client from "
                          f"{ctx.gw_ip}; connected: {[fmt_client(c) for c in z2m + ha + other]}")
        unknown = mq_unaccounted(ctx)
        if unknown:
            raise Blocked(UNACCOUNTED + "; ".join(unknown))

    def gate_authenticated(self, ctx):
        conns = broker_connected()
        z2m, ha, other = mq_classify(ctx, conns)
        require(not other, "unknown MQTT clients connected: " + "; ".join(fmt_client(c) for c in other))
        require(len(z2m) == 1 and z2m[0]["user"] == "zigbee2mqtt", f"zigbee2mqtt connection: {[fmt_client(c) for c in z2m]}")
        require(len(ha) == 1 and ha[0]["user"] == "homeassistant", f"Home Assistant connection: {[fmt_client(c) for c in ha]}")
        return z2m[0], ha[0]

    def apply(self, ctx):
        global MQ_CHECK
        self.gate_before(ctx)
        pw = {u: new_secret() for u in MQ_USERS}
        z2m_old = open(Z2M_CONF).read()
        z2m_new = z2m_edit([("mqtt", "user", "zigbee2mqtt"), ("mqtt", "password", pw["zigbee2mqtt"])], z2m_old)
        creds_put(self.service, MQ_URL, pw)

        log("mqtt step 1/4: password file, anonymous still allowed")
        mq_build_passwd(pw)
        t1 = mq_set_conf(pwfile=True, anon=True)
        for u in MQ_USERS:
            require(mq_try(f"auth-{u}", u, pw[u]) == 0, f"broker refused the new {u} password")
        require(mq_try("badpw", "admin", "x" + new_secret(12)) != 0, "broker accepted a wrong password")
        require(mq_try("anon") == 0, "broker refused anonymous access too early")
        dropped = [e for e in broker_events(t1) if e[0] == "disconnect" and not e[2].startswith(PROBE)]
        require(not dropped, f"clients dropped at reload: {dropped}")
        MQ_CHECK = ("admin", pw["admin"])
        log("mqtt step 1/4 ok: all three users authenticate, wrong password refused, no client dropped")

        log("mqtt step 2/4: zigbee2mqtt connects as zigbee2mqtt")
        presence_gate(ctx, "the zigbee2mqtt restart", abort=True)
        require(open(Z2M_CONF).read() == z2m_old, f"{Z2M_CONF} changed during the mqtt stage")
        write_secure(Z2M_CONF, z2m_new)
        t2 = z2m_restart()
        ctx.z2m_ip = container_ip("zigbee2mqtt")
        mine = [e for e in broker_events(t2) if e[0] == "connect" and e[1].rsplit(":", 1)[0] == ctx.z2m_ip]
        require(mine and mine[-1][4] == "zigbee2mqtt", f"zigbee2mqtt reconnect not seen with user zigbee2mqtt: {mine}")
        health_check(ctx, "mqtt step 2/4")

        log("mqtt step 3/4: Home Assistant connects as homeassistant")
        presence_gate(ctx, "the Home Assistant MQTT reconfigure", abort=True)
        t3 = time.time()
        ha_mqtt_reconfigure(ctx.ha, "homeassistant", pw["homeassistant"])

        def ha_back():
            mine = [e for e in broker_events(t3) if e[0] == "connect" and e[1].rsplit(":", 1)[0] == ctx.gw_ip
                    and e[3] == "p5" and e[4] == "homeassistant"]
            live = broker_connected()
            return mine and mine[-1][1] in live and ha_entry(ctx.ha, "mqtt")["state"] == "loaded"

        wait_for("HA MQTT reconnect as homeassistant", ha_back, 120, 5)
        health_check(ctx, "mqtt step 3/4")

        log("mqtt step 4/4: anonymous access off")
        self.gate_authenticated(ctx)
        unknown = mq_unaccounted(ctx)
        require(not unknown, UNACCOUNTED + "; ".join(unknown))
        t4 = mq_set_conf(pwfile=True, anon=False)
        dropped = [e for e in broker_events(t4) if e[0] == "disconnect" and not e[2].startswith(PROBE)]
        require(not dropped, f"clients dropped at reload: {dropped}")
        require(mq_try("anon") != 0, "broker still accepts anonymous clients")
        require(mq_try("admin", "admin", pw["admin"]) == 0, "broker refused admin")
        log(f"mqtt: anonymous refused; watching {OBSERVE_S}s for refused clients")
        end = time.monotonic() + OBSERVE_S
        while time.monotonic() < end:
            time.sleep(30)
            rej = broker_rejections(t4)
            require(not rej, "client refused: " + "; ".join(f"{e[2]} [{e[1]}]" for e in rej))
            require(running("mqtt"), "mosquitto stopped")
            wait_for("zigbee2mqtt and HA connected with their users", lambda: self.gate_authenticated(ctx), 60, 5)
        health_check(ctx, "mqtt step 4/4")
        creds_mark(self.service, "active")

    def restore(self, ctx):
        global MQ_CHECK
        try:
            mq_set_conf(pwfile=False, anon=True)
            require(mq_try("anon") == 0, "broker refuses anonymous clients")
        except Exception as e:
            raise Fail(f"broker not restored, stopped there: {type(e).__name__}: {e}. {MQ_PASSWD} is kept, "
                       "zigbee2mqtt and Home Assistant are untouched; fix mosquitto.conf by hand") from None
        MQ_CHECK = (None, None)
        z2m_restore(self.name, self.keys)
        z2m_restart()
        if os.path.exists(MQ_PASSWD):
            backup(MQ_PASSWD)
            os.unlink(MQ_PASSWD)
            log(f"{MQ_PASSWD} removed (copy in the run dir)")

    def recheck(self, ctx):
        try:
            if ha_entry_data("mqtt")[1].get("username"):
                t0 = time.time()
                ha_mqtt_reconfigure(ctx.ha, None, None)
                wait_for("HA MQTT reconnect without credentials",
                         lambda: any(e[0] == "connect" and e[1].rsplit(":", 1)[0] == ctx.gw_ip and e[3] == "p5"
                                     and e[4] is None for e in broker_events(t0))
                         and ha_entry(ctx.ha, "mqtt")["state"] == "loaded", 120, 5)
        except Exception as e:
            log(f"warning: HA MQTT entry keeps its credentials, harmless with an anonymous broker: {e}")
        health_check(ctx, "mqtt rollback")


STAGES = [ZWaveUI(), Z2MFrontend(), MqttAuth()]


def rollback(st, ctx):
    st.restore(ctx)
    creds_mark(st.service, "rolled_back", ("pending", "active"))
    st.recheck(ctx)


# ---------- report and orchestration ----------

class Ctx:
    pass


def mq_check_creds_for_done():
    pw = creds_get("mqtt", "admin")
    if pw:
        return ("admin", pw)
    m = z2m_conf().get("mqtt") or {}
    return (m.get("user"), m.get("password"))


def preflight(ctx):
    blockers = []
    for c in ("mqtt", "zigbee2mqtt", "zwavejs2mqtt", "homeassistant"):
        if not running(c):
            blockers.append(f"container {c} is not running")
    for f in (MQ_CONF, Z2M_CONF, ZW_SETTINGS, ZW_USERS, COMPOSE_FILE):
        if not os.path.exists(f):
            blockers.append(f"missing {f}")
    try:
        import yaml  # noqa: F401
    except ImportError:
        blockers.append("python3-yaml is not installed on the host")
    ctx.ha, source = ha_connect()
    log(f"Home Assistant: admin API ok with the {source}")
    try:
        ctx.z2m_ip, ctx.gw_ip = container_ip("zigbee2mqtt"), mqtt_gateway_ip()
        log(f"zigbee2mqtt container IP {ctx.z2m_ip}, mqtt network gateway (host-side clients) {ctx.gw_ip}")
    except Fail as e:
        ctx.z2m_ip = ctx.gw_ip = None
        blockers.append(str(e))
    ver = re.findall(r"mosquitto version (\S+) running", docker_logs("mqtt"))
    log(f"mosquitto {ver[-1] if ver else '?'}; zone.home = {ha_home_count(ctx.ha)}")
    for st in STAGES:
        try:
            ctx.states[st.name] = st.state()
        except Exception as e:
            ctx.states[st.name] = f"error: {e}"
        log(f"state {st.name}: {ctx.states[st.name]}")
        if ctx.states[st.name] not in ("todo", "done"):
            blockers.append(f"{st.name}: {ctx.states[st.name]}")
    global MQ_CHECK
    if ctx.states.get("mqtt") == "done":
        MQ_CHECK = mq_check_creds_for_done()
    for domain in ("mqtt", "zwave_js"):
        try:
            state = ha_entry(ctx.ha, domain)["state"]
        except Fail as e:
            state = str(e)
        if state != "loaded":
            blockers.append(f"HA {domain} entry is {state}")
    try:
        if not z2m_bridge_online():
            blockers.append("zigbee2mqtt bridge is not online")
    except Exception as e:
        blockers.append(f"zigbee2mqtt bridge state unreadable: {e}")
    return blockers


def inventory(ctx):
    """Logs who uses the broker; returns the problems that keep the mqtt stage from starting."""
    log("== MQTT client inventory")
    conns = broker_connected()
    z2m, ha, other = mq_classify(ctx, conns)
    for c in conns.values():
        role = "zigbee2mqtt" if c in z2m else "Home Assistant" if c in ha else "this script" if c["cid"].startswith(PROBE) else "UNKNOWN"
        log(f"  connected: {fmt_client(c)} -> {role}")
    log(f"  $SYS/broker/clients/connected = {mq_sys_connected()} (includes this check)")
    seen = {}
    for ev in broker_events():
        if ev[0] == "connect":
            ip, cid = ev[1].rsplit(":", 1)[0], ev[2]
            kind = "mosquitto CLI/auto id" if cid.startswith("auto-") else "z2m (mqttjs)" if cid.startswith("mqttjs_") \
                else "this script" if cid.startswith(PROBE) else "random id" if re.fullmatch(r"[A-Za-z0-9]{22}", cid) else cid
            seen.setdefault((ip, ev[3], kind, ev[4]), 0)
            seen[(ip, ev[3], kind, ev[4])] += 1
    m = re.match(r"(\d+):", docker_logs("mqtt"))
    first = iso(int(m.group(1)))[:19] + "Z" if m else "?"
    log(f"  connects in the broker log since {first}:")
    for (ip, proto, kind, user), n in sorted(seen.items(), key=lambda x: -x[1]):
        log(f"    {n:3d}x {ip:<16} {proto} {kind}{' user ' + user if user else ''}")
    topics = {t.split("/")[0] for t, _, _ in mq_collect(["#"], 5)}
    log(f"  retained topic roots: {sorted(topics)}")
    zw = json.load(open(ZW_SETTINGS)).get("mqtt", {})
    log(f"  zwave-js-ui mqtt.disabled = {zw.get('disabled')}")
    ad = open(AD_CONF).read() if os.path.exists(AD_CONF) else ""
    ad_mqtt = len(re.findall(r"type:\s*mqtt", ad, re.I))
    log(f"  appdaemon plugins of type mqtt: {ad_mqtt}")
    esp = [os.path.basename(p) for p in glob.glob(f"{ESPHOME_DIR}/*.yaml")
           if not p.endswith("secrets.yaml") and re.search(r"^\s*mqtt:", open(p).read(), re.M)]
    log(f"  esphome configs with an mqtt: block: {esp or 'none'}")
    log(f"  z2m mqtt server: {(z2m_conf().get('mqtt') or {}).get('server')}; HA MQTT entry broker: "
        f"{ha_entry_data('mqtt')[1].get('broker')}")
    problems = [f"unknown client connected: {fmt_client(c)}" for c in other]
    if ctx.states.get("mqtt") == "todo":
        unknown = mq_unaccounted(ctx)
        if unknown:
            problems.append(UNACCOUNTED + "; ".join(unknown))
    for p in problems:
        log(f"  BLOCKER for the mqtt step: {p}")
    return problems


def baselines(ctx):
    base = {"reporting": z2m_reporting(REPORT_WINDOW_S)}
    for platform in ("mqtt", "zwave_js"):
        ids = platform_ids(platform)
        base[platform] = {"ids": ids, **ha_counts(ctx.ha, ids)}
    ctx.base = base
    log(f"baseline: {base['reporting']} zigbee devices reported in {REPORT_WINDOW_S}s; HA MQTT entities "
        f"{base['mqtt']['total']} (unknown+unavailable {base['mqtt']['not_ok']}, {base['mqtt']['missing']} missing); "
        f"Z-Wave entities {base['zwave_js']['total']} (unavailable {base['zwave_js']['unavailable']}, "
        f"{base['zwave_js']['missing']} missing)")
    if MODE == "apply":
        path = os.path.join(RUN_DIR, "baseline.json")
        with open(path, "w") as f:
            json.dump(base, f)
        os.chmod(path, 0o600)
        log(f"baseline saved to {path}")


def saved_baseline():
    hits = sorted(glob.glob(os.path.join(STATE_DIR, "*-apply", "baseline.json")))
    if not hits:
        return None
    log(f"baseline from {hits[-1]}")
    with open(hits[-1]) as f:
        return json.load(f)


def dry_run(ctx):
    blockers = list(ctx.blockers)
    blockers += [f"mqtt: {p}" for p in inventory(ctx)]
    if os.path.exists(CREDS_FILE):
        log(f"note: {CREDS_FILE} exists from an earlier run:")
        creds_summary()
    log("== Plan, in apply order")
    for st in STAGES:
        log(f"-- {st.name} ({ctx.states[st.name]})")
        if ctx.states[st.name] not in ("todo", "done"):
            continue
        try:
            if ctx.states[st.name] == "todo":
                st.plan(ctx)
            else:
                st.verify_done(ctx)
        except Exception as e:
            blockers.append(f"{st.name}: {e}")
            log(f"  {'BLOCKER' if ctx.states[st.name] == 'todo' else 'CHECK FAILED'}: {e}")
    log("== Baselines")
    try:
        baselines(ctx)
    except Exception as e:
        blockers.append(f"baseline: {type(e).__name__}: {e}")
        log(f"  BLOCKER: baseline unavailable: {type(e).__name__}: {e}")
    log("== Expected downtime: Z-Wave about 20-90 s (container restart, then HA reconnects); Zigbee about "
        "15-30 s per zigbee2mqtt restart, twice; HA MQTT entities about 30-60 s while the entry reloads; "
        "the broker itself never stops (config reloads with SIGHUP)")
    if ha_home_count(ctx.ha) != "0":
        log(f"note: zone.home is {ha_home_count(ctx.ha)}; --apply waits for an empty home unless --ignore-presence")
    if blockers:
        log("RESULT: not ready: " + "; ".join(blockers))
        return 3
    todo = [s.name for s in STAGES if ctx.states[s.name] == "todo"]
    log("RESULT: " + ("ready; --apply would change: " + ", ".join(todo) if todo else "all services secured and verified"))
    return 0


def creds_summary():
    for e in creds_load():
        log(f"  {CREDS_FILE}: {e['service']} / {e['username'] or '-'} / {e['url']} / {e['status']}")


def apply(ctx):
    global STAGE
    if ctx.blockers:
        log("not applying: " + "; ".join(ctx.blockers))
        return 3
    try:
        problems = inventory(ctx)
        baselines(ctx)
    except Exception as e:
        log(f"not applying: {type(e).__name__}: {e}")
        return 3
    if problems and ctx.states["mqtt"] == "todo":
        log("not applying: " + "; ".join(problems))
        return 3
    for st in STAGES:
        if not ctx.ignore_presence:
            home = ha_home_count(ctx.ha)
            if ctx.came_home or home != "0":
                log(f"zone.home is {home}{', someone came home during the run' if ctx.came_home else ''}; "
                    f"stopping before {st.name}")
                creds_summary()
                return 3
        if ctx.states[st.name] == "done":
            log(f"== {st.name}: already secured")
            try:
                st.verify_done(ctx)
            except Exception as e:
                log(f"== {st.name}: CHECK FAILED, stopping: {e}")
                return 1
            continue
        log(f"== {st.name}: applying")
        STAGE = st.name
        try:
            st.apply(ctx)
        except Blocked as e:
            log(f"== {st.name}: not started, nothing changed: {e}")
            creds_summary()
            return 3
        except (Exception, Interrupted) as e:
            hold_signals()
            log(f"== {st.name}: {'INTERRUPTED' if isinstance(e, Interrupted) else 'FAILED'}: {type(e).__name__}: {e}")
            log(f"== {st.name}: rolling back")
            try:
                rollback(st, ctx)
                log(f"== {st.name}: rolled back and verified; stopping here")
                creds_summary()
                return 1
            except Exception as e2:
                log(f"== {st.name}: ROLLBACK FAILED: {type(e2).__name__}: {e2}")
                log(f"   copies of the original files are in {RUN_DIR}/files/{st.name}")
                creds_summary()
                return 4
        STAGE = None
        log(f"== {st.name}: done and verified")
    log("all services secured; move the active entries into Bitwarden, then delete the file")
    creds_summary()
    return 0


def explicit_rollback(ctx, st):
    """--rollback NAME: restores locally first; Home Assistant only verifies afterwards."""
    global MQ_CHECK, STAGE
    hold_signals()
    STAGE = st.name
    try:
        ctx.z2m_ip, ctx.gw_ip = container_ip("zigbee2mqtt"), mqtt_gateway_ip()
    except Exception as e:
        log(f"container addresses unavailable: {e}")
    try:
        MQ_CHECK = mq_check_creds_for_done()
    except Exception as e:
        log(f"no MQTT check credentials: {e}")
    try:
        st.restore(ctx)
        creds_mark(st.service, "rolled_back", ("pending", "active"))
    except Exception as e:
        log(f"{st.name}: ROLLBACK FAILED: {type(e).__name__}: {e}")
        log(f"   copies of the original files are in {STATE_DIR}/*-apply/files/{st.name}")
        return 4
    log(f"{st.name}: files restored and containers restarted; verifying with Home Assistant")
    ctx.base = saved_baseline()
    try:
        ctx.ha, source = ha_connect()
        st.recheck(ctx)
    except Exception as e:
        log(f"{st.name}: UNVERIFIED: {type(e).__name__}: {e}")
        return 5
    log(f"{st.name}: rolled back and verified")
    return 0


def main():
    global MODE, DRY, RUN_DIR, LOG_FH
    ap = argparse.ArgumentParser(prog="secure_lan_services.sh")
    mode = ap.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    mode.add_argument("--rollback", choices=[s.name for s in STAGES])
    ap.add_argument("--ignore-presence", action="store_true")
    args = ap.parse_args()
    MODE = "apply" if args.apply else "rollback" if args.rollback else "dry-run"
    DRY = MODE == "dry-run"
    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)
    if not DRY:
        lock = open(LOCK_FILE, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            print("another run holds " + LOCK_FILE, file=sys.stderr)
            return 2
        stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        RUN_DIR = os.path.join(STATE_DIR, f"{stamp}-apply" if args.apply else f"{stamp}-rollback-{args.rollback}")
        os.makedirs(RUN_DIR, mode=0o700)
        os.chmod(STATE_DIR, 0o700)
        LOG_FH = open(os.path.join(RUN_DIR, "run.log"), "a")
        os.chmod(LOG_FH.name, 0o600)
    log(f"secure_lan_services {'rollback ' + args.rollback if args.rollback else MODE}"
        f"{'' if DRY else ', run dir ' + RUN_DIR}")
    ctx = Ctx()
    ctx.states = {}
    ctx.ha = ctx.base = ctx.z2m_ip = ctx.gw_ip = None
    ctx.ignore_presence = args.ignore_presence
    ctx.came_home = False
    if args.rollback:
        return explicit_rollback(ctx, next(s for s in STAGES if s.name == args.rollback))
    try:
        ctx.blockers = preflight(ctx)
    except Exception as e:
        log(f"preflight failed: {type(e).__name__}: {e}")
        return 2
    if DRY:
        return dry_run(ctx)
    return apply(ctx)


if __name__ == "__main__":
    try:
        code = main()
    except Interrupted as e:
        log(f"interrupted ({e}) with no service mid-change")
        code = 3
    except Exception as e:
        log(f"crashed: {type(e).__name__}: {e}")
        code = 4
    log(f"exit {code}")
    sys.exit(code)
PY
