#!/usr/bin/env bash
# Move the doorbell and Rober2 map archives from HA's public www/ to its media
# folder, which HA serves to signed-in users only. Runs from the laptop over ssh.
#
# Usage:
#   scripts/cutover_private_archives.sh --dry-run    every check, every action printed, nothing changed
#   scripts/cutover_private_archives.sh              the cutover (idempotent: a rerun resumes or re-verifies)
#   scripts/cutover_private_archives.sh --set-ring-clip-dir DIR   only switch the ABB option (rollback aid)
# Options:
#   --dashboard DIR     dashboard checkout to deploy from (default: the main checkout)
#   --skip-dashboard    leave the dashboard deploy to someone else
#   --no-test-push      do not send the photo test push to Mikkel's Android
#
# Order: prepare (HA shell_command block + restart, AppDaemon /media mount), wait
# until no ring for QUIET_S, stop AppDaemon, point the integration's ring clips at
# /media, final-sync and validate both archives, put the new AppDaemon files in
# place, prove signed-in access works and unsigned access is refused, move both
# www/ directories out, check the old URLs 404, start AppDaemon, check again,
# deploy the dashboard. Any failure before the move undoes the option, the code
# and the stop; nothing after the move is undone automatically.
set -euo pipefail

HOST="mke@10.21.0.5"
PUBLIC="https://ha.me2go.dk"
HA_DIR="/data/homeassistant"
AD_DIR="/data/appdaemon"
COMPOSE="/data/docker-compose.yaml"
NEW_CLIP_DIR="/media/abb_doorbell"
RING_SENSORS=(binary_sensor.abb_welcome_gateway_intercom_ringing
              binary_sensor.intercomproxy_doorbell_front_door
              binary_sensor.intercomproxy_doorbell_back_door)
VACUUM="vacuum.rober2"
QUIET_S=120              # longest ring clip finalizes ~40 s after the ring; episodes close at 75 s
QUIET_MAX_WAIT_S=900     # with AppDaemon still running
QUIET_STOPPED_MAX_S=180  # with AppDaemon stopped: the doorbell has no auto-open meanwhile
TEST_PUSH_SERVICE="mobile_app_mikkels_ofx9p"
AD_FILES=(apps/intercom/abb_welcome_bridge.py apps/intercom/abb_welcome_bridge.yaml apps/rober2/control.py apps/rober2/rober2.yaml)

DRY=0
DASH="/home/mke/repositories/home-assistant-hakit-dashboard"
SKIP_DASH=0
TEST_PUSH=1
SET_CLIP_DIR=""
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1 ;;
    --dashboard) DASH="${2:?--dashboard needs a directory}"; shift ;;
    --skip-dashboard) SKIP_DASH=1 ;;
    --no-test-push) TEST_PUSH=0 ;;
    --set-ring-clip-dir) SET_CLIP_DIR="${2:?--set-ring-clip-dir needs a directory}"; shift ;;
    -h|--help) sed -n '2,13p' "$0"; exit 0 ;;
    *) echo "unknown argument: $1" >&2; exit 2 ;;
  esac
  shift
done

REPO="$(cd "$(dirname "$0")/.." && git rev-parse --show-toplevel)"
DAY="$(TZ=Europe/Copenhagen date +%Y%m%d)"
BACKUP_DIR="$HA_DIR/archive-www-backup-$DAY"
AD_BACKUP="$AD_DIR/cutover-backup-$DAY"
CFG_BAK="$HA_DIR/configuration.yaml.bak-$DAY-archives"
COMPOSE_BAK="$COMPOSE.bak-$DAY-archives"

# --- output ------------------------------------------------------------------
PROBLEMS=()
step() { printf '\n== %s\n' "$*"; }
ok() { printf '  OK: %s\n' "$*"; }
info() { printf '  %s\n' "$*"; }
would() { printf '  WOULD: %s\n' "$*"; }
die() { printf '  FAIL: %s\n' "$*"; exit 1; }
# A failed check: fatal for real, collected in a dry run so it shows every problem.
problem() {
  if [ "$DRY" = 1 ]; then printf '  FAIL: %s\n' "$*"; PROBLEMS+=("$*"); else die "$*"; fi
}

box() { ssh -o BatchMode=yes -o ConnectTimeout=15 "$HOST" "$@"; }

# Every HA API call runs inside the homeassistant container (aiohttp is there).
# The token is read on the box from the ha-mcp container's environment and handed
# over on stdin: it never reaches argv, a file, or this script's output.
AGENT_BOOT='import sys; t = sys.stdin.readline().strip(); src = sys.stdin.read(); exec(compile(src, "agent", "exec"), {"TOKEN": t, "__name__": "agent"})'
IFS= read -r -d '' HA_AGENT <<'PY' || true
import asyncio, json, os, subprocess, sys, time, urllib.error, urllib.request
from datetime import datetime

BASE = "http://localhost:8123"
STORE = "/config/.storage/core.config_entries"


def req(method, path, body=None, headers=None, auth=True, timeout=30):
    h = dict(headers or {})
    if auth:
        h["Authorization"] = f"Bearer {TOKEN}"
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        h["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def die(msg):
    print(f"  FAIL: {msg}")
    sys.exit(1)


def ws(messages):
    import aiohttp

    async def run():
        async with aiohttp.ClientSession() as s:
            async with s.ws_connect(BASE + "/api/websocket", max_msg_size=0) as w:
                await w.receive_json()
                await w.send_json({"type": "auth", "access_token": TOKEN})
                if (await w.receive_json()).get("type") != "auth_ok":
                    die("websocket auth refused")
                out = []
                for i, m in enumerate(messages, 1):
                    await w.send_json({"id": i, **m})
                    while True:
                        r = await w.receive_json()
                        if r.get("id") == i:
                            break
                    out.append(r)
                return out

    return asyncio.run(asyncio.wait_for(run(), 120))


def abb_entry():
    with open(STORE) as f:
        found = [e for e in json.load(f)["data"]["entries"] if e["domain"] == "abb_welcome"]
    if len(found) != 1:
        die(f"expected one abb_welcome config entry, found {len(found)}")
    return found[0]


def ping():
    try:
        st, _ = req("GET", "/api/", timeout=5)
    except Exception as e:
        die(f"HA API unreachable: {e}")
    if st != 200:
        die(f"HA API answered {st}")
    print("  OK: HA API answers")


def quiet(min_s, *entities):
    min_s, wait, now = float(min_s), 0.0, time.time()
    for e in entities:
        st, body = req("GET", f"/api/states/{e}")
        if st != 200:
            die(f"{e}: HTTP {st}")
        d = json.loads(body)
        age = now - datetime.fromisoformat(d["last_changed"]).timestamp()
        print(f"  {e}: {d['state']} for {int(age)} s")
        if d["state"] == "on":
            wait = max(wait, min_s)
        elif age < min_s:
            wait = max(wait, min_s - age)
    if wait > 0:
        print(f"WAIT {int(wait) + 1}")
        sys.exit(10)
    print(f"  OK: no ring edge in the last {int(min_s)} s")


def vacuum(entity):
    st, body = req("GET", f"/api/states/{entity}")
    if st != 200:
        die(f"{entity}: HTTP {st}")
    state = json.loads(body)["state"]
    if state not in ("docked", "idle", "unavailable", "unknown"):
        print(f"  BUSY: {entity} is {state}: a run saves maps and needs AppDaemon; rerun once it has docked")
        sys.exit(10)
    print(f"  OK: {entity} is {state}")


def services():
    st, body = req("GET", "/api/services")
    if st != 200:
        die(f"/api/services: HTTP {st}")
    have = {s for d in json.loads(body) if d["domain"] == "shell_command" for s in d["services"]}
    print("present" if {"doorbell_archive_index", "rober2_maps_index"} <= have else "absent")


def check_config():
    st, body = req("POST", "/api/config/core/check_config", timeout=120)
    if st != 200:
        die(f"check_config: HTTP {st}")
    d = json.loads(body)
    if d.get("result") != "valid":
        die(f"HA config check: {d.get('result')}: {d.get('errors')}")
    print("  OK: HA config check passes")


def validate_block(b64):
    import base64
    import yaml
    text = base64.b64decode(b64).decode()
    cmds = yaml.safe_load(text)["shell_command"]
    for name in ("doorbell_archive_index", "rober2_maps_index"):
        r = subprocess.run(["sh", "-n", "-c", cmds[name]], capture_output=True, text=True)
        if r.returncode:
            die(f"{name}: sh -n: {r.stderr.strip()}")
    print("  OK: the shell_command block parses and both commands pass sh -n")


def restart():
    try:
        req("POST", "/api/services/homeassistant/restart", {}, timeout=10)
    except Exception:
        pass
    print("  OK: HA restart requested")


def option_get():
    e = abb_entry()
    print(e["options"].get("ring_clip_dir", ""))


def option_set(target, probe="no"):
    e = abb_entry()
    opts = e["options"]
    if probe != "probe" and opts.get("ring_clip_dir") == target:
        print(f"  OK: ring_clip_dir already {target!r}")
        return
    st, body = req("POST", "/api/config/config_entries/options/flow", {"handler": e["entry_id"]})
    if st != 200:
        die(f"options flow start: HTTP {st}")
    form = json.loads(body)
    flow = f"/api/config/config_entries/options/flow/{form.get('flow_id')}"
    fields = {f["name"] for f in form.get("data_schema", [])}
    dropped = sorted(set(opts) - fields)
    if form.get("type") != "form" or form.get("step_id") != "init" or dropped or "ring_clip_dir" not in fields:
        req("DELETE", flow)
        die(f"options form unusable (step {form.get('step_id')}, options it would drop: {dropped})")
    if probe == "probe":
        req("DELETE", flow)
        print(f"  OK: the options form carries all {len(opts)} current options; ring_clip_dir is {opts.get('ring_clip_dir')!r}")
        return
    user_input = {k: v for k, v in opts.items() if k in fields}
    user_input["ring_clip_dir"] = target
    st, body = req("POST", flow, user_input)
    if st != 200 or json.loads(body).get("type") != "create_entry":
        req("DELETE", flow)
        die(f"options flow refused the change: HTTP {st} {body[:300]!r}")
    for _ in range(60):
        new = abb_entry()["options"]
        if new.get("ring_clip_dir") == target:
            break
        time.sleep(0.5)
    else:
        die("ring_clip_dir did not reach .storage within 30 s")
    other = sorted(k for k in set(opts) | set(new) if k != "ring_clip_dir" and opts.get(k) != new.get(k))
    if other:
        die(f"other options changed too: {other}")
    print(f"  OK: ring_clip_dir {opts.get('ring_clip_dir')!r} -> {target!r}")


def index(service, expect):
    r = ws([{"type": "call_service", "domain": "shell_command", "service": service,
             "service_data": {}, "return_response": True}])[0]
    if not r.get("success"):
        die(f"{service}: {r.get('error')}")
    resp = (r.get("result") or {}).get("response") or {}
    rc, out, err = resp.get("returncode"), resp.get("stdout"), resp.get("stderr")
    if rc != 0 or err:
        die(f"{service}: returncode {rc}, stderr {err!r}")
    try:
        data = json.loads(out)
    except ValueError as e:
        die(f"{service}: stdout is not JSON: {e}")
    if data.get("absent") is True:
        if expect not in ("absent", "-"):
            die(f"{service}: reports no archive")
        print(f"  OK: {service} answers and reports no archive yet")
        return
    n = len(data["images" if "doorbell" in service else "maps"])
    if expect not in ("-", str(n)):
        die(f"{service}: {n} entries, expected {expect}")
    print(f"  OK: {service} returned {n} entries over the websocket (returncode 0, no stderr)")


def tamper(signed):
    head, _, sig = signed.rpartition("authSig=")
    return head + "authSig=" + sig[:-1] + ("A" if sig[-1:] != "A" else "B")


def signcheck(*paths):
    replies = ws([{"type": "auth/sign_path", "path": p, "expires": 60} for p in paths])
    for p, r in zip(paths, replies):
        if not r.get("success"):
            die(f"auth/sign_path refused {p}: {r.get('error')}")
    signed = [r["result"]["path"] for r in replies]
    bad = 0
    for i, (p, sp) in enumerate(zip(paths, signed)):
        query = sp.split("?", 1)[1]
        other = paths[(i + 1) % len(paths)]
        checks = [
            ("unsigned", req("GET", p, auth=False)[0], 401),
            ("bearer", req("GET", p)[0], 200),
            ("signed", req("GET", sp, auth=False)[0], 200),
            ("signed range", req("GET", sp, auth=False, headers={"Range": "bytes=0-99"})[0], 206),
            ("tampered", req("GET", tamper(sp), auth=False)[0], 401),
        ]
        if other != p:
            checks.append(("signature of another file", req("GET", f"{other}?{query}", auth=False)[0], 401))
        line = ", ".join(f"{name} {got}" for name, got, _ in checks)
        miss = [name for name, got, want in checks if got != want]
        bad += len(miss)
        print(f"  {'OK' if not miss else 'FAIL'}: {os.path.basename(p)}: {line}")
    if bad:
        die(f"{bad} access check(s) gave the wrong status")


def media_probe():
    found = []
    for root, _, files in sorted(os.walk("/media")):
        found += [os.path.relpath(os.path.join(root, f), "/media") for f in sorted(files)
                  if f.endswith((".mp3", ".jpg", ".png", ".mp4"))]
    if not found:
        die("no media file to probe")
    print(" ".join(f"/media/local/{rel}" for rel in found[:2]))


def notify(service, image):
    st, body = req("POST", f"/api/services/notify/{service}", {
        "title": "Archive check",
        "message": "This photo comes from the private doorbell archive.",
        "data": {"image": image},
    })
    if st != 200:
        die(f"notify.{service}: HTTP {st}")
    print(f"  OK: test push sent via notify.{service}")


cmd, *args = sys.argv[1:]
{"ping": ping, "quiet": quiet, "vacuum": vacuum, "services": services, "check_config": check_config,
 "validate_block": validate_block, "restart": restart, "option_get": option_get, "option_set": option_set,
 "index": index, "signcheck": signcheck, "media_probe": media_probe, "notify": notify}[cmd](*args)
PY

ha() {
  local q
  q=$(printf '%q ' "$@")
  box "set -o pipefail; T=\$(sudo docker inspect ha-mcp --format '{{range .Config.Env}}{{println .}}{{end}}' | sed -n 's/^HOMEASSISTANT_TOKEN=//p'); [ -n \"\$T\" ] || { echo 'FAIL: no HOMEASSISTANT_TOKEN in the ha-mcp container'; exit 3; }; { printf '%s\n' \"\$T\"; cat; } | sudo docker exec -i homeassistant python3 -c '$AGENT_BOOT' $q" <<<"$HA_AGENT"
}

# Both archives: copy (apply) or simulate (plan) the index rewrite, then check
# every entry, every URL and every referenced file. Runs as root on the box.
IFS= read -r -d '' ARCHIVES_PY <<'PY' || true
import json, os, sys

mode = sys.argv[1]  # plan: read www only | apply: write the media indexes | media: check media only
HA = "/data/homeassistant"
SPECS = {"abb_doorbell": ("images", (("url", "filename"), ("clip_url", "clip_filename")), None),
         "rober2_maps": ("maps", (("url", "filename"),), 2)}
bad = []
for name, (key, pairs, indent) in SPECS.items():
    www, media = f"{HA}/www/{name}", f"{HA}/media/{name}"
    src = media if mode == "media" else www
    try:
        with open(f"{src}/index.json") as f:
            data = json.load(f)
        entries = data[key]
        assert isinstance(entries, list)
    except Exception as e:
        bad.append(f"{src}/index.json unusable: {e!r}")
        continue
    refs = set()
    for e in entries:
        for url_key, file_key in pairs:
            url, fname = e.get(url_key) or "", e.get(file_key) or ""
            if not url and not fname:
                continue
            new = f"/media/local/{name}/{fname}"
            if not fname or os.path.basename(fname) != fname or url not in (f"/local/{name}/{fname}", new):
                bad.append(f"{name}: unexpected {url_key}={url!r} {file_key}={fname!r}")
                continue
            if mode == "media" and url != new:
                bad.append(f"{name}: {url_key} still public: {url!r}")
            e[url_key] = new
            refs.add(fname)
    target = www if mode == "plan" else media
    missing = [f for f in sorted(refs) if not os.path.isfile(f"{target}/{f}") or os.path.getsize(f"{target}/{f}") == 0]
    bad += [f"{name}: {f} missing or empty in {target}" for f in missing]
    if mode == "apply":
        bad += [f"{name}: {f} differs from the www copy" for f in sorted(refs)
                if os.path.isfile(f"{media}/{f}") and os.path.getsize(f"{media}/{f}") != os.path.getsize(f"{www}/{f}")]
    on_disk = {f for f in os.listdir(target) if f.endswith((".jpg", ".mp4", ".png"))}
    if mode == "apply" and not missing:
        st = os.stat(f"{www}/index.json")
        tmp = f"{media}/index.json.tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=indent)
        os.chown(tmp, st.st_uid, st.st_gid)
        os.chmod(tmp, st.st_mode & 0o777)
        os.replace(tmp, f"{media}/index.json")
        with open(f"{media}/index.json") as f:
            if json.load(f) != data:
                bad.append(f"{name}: media index.json did not read back")
    mb = sum(os.path.getsize(f"{target}/{f}") for f in os.listdir(target) if os.path.isfile(f"{target}/{f}")) / 1e6
    print(f"  {name}: {len(entries)} entries, {len(refs)} referenced files all present in {target}"
          if not missing else f"  {name}: {len(entries)} entries, {len(missing)} of {len(refs)} referenced files missing",
          f"({len(on_disk - refs)} unreferenced, {mb:.1f} MB)")
for b in bad:
    print(f"  FAIL: {b}")
sys.exit(1 if bad else 0)
PY

archives() { box "sudo python3 - $1" <<<"$ARCHIVES_PY"; }

# The appdaemon service gains the /media mount, inserted under its www/ mount.
IFS= read -r -d '' COMPOSE_PY <<'PY' || true
import os, re, sys

path = "/data/docker-compose.yaml"
with open(path) as f:
    lines = f.read().splitlines(keepends=True)
start = next(i for i, l in enumerate(lines) if l.rstrip() == "  appdaemon:")
end = next((i for i in range(start + 1, len(lines)) if re.match(r"^  \S.*:\s*$", lines[i])), len(lines))
if any(l.strip() == "- /data/homeassistant/media:/media" for l in lines[start:end]):
    print("present")
    sys.exit(0)
www = [i for i in range(start, end) if lines[i].strip() == "- /data/homeassistant/www:/www"]
if len(www) != 1:
    print("FAIL: no single www mount line in the appdaemon service")
    sys.exit(1)
line = lines[www[0]]
lines.insert(www[0] + 1, line[: len(line) - len(line.lstrip())] + "- /data/homeassistant/media:/media\n")
if sys.argv[1] == "print":
    sys.stdout.write("".join(lines))
else:
    st = os.stat(path)
    with open(path + ".tmp", "w") as f:
        f.write("".join(lines))
    os.chown(path + ".tmp", st.st_uid, st.st_gid)
    os.chmod(path + ".tmp", st.st_mode & 0o777)
    os.replace(path + ".tmp", path)
    print("added")
PY

IFS= read -r -d '' SHELL_BLOCK <<'YAML' || true
# Private archives: the dashboard reads each index through these commands; the
# files themselves are served at /media/local/ to signed-in users only. A missing
# index prints {"absent": true}; any other failure exits non-zero.
shell_command:
  doorbell_archive_index: "if [ -e /media/abb_doorbell/index.json ]; then cat /media/abb_doorbell/index.json; else echo '{\"absent\": true}'; fi"
  rober2_maps_index: "if [ -e /media/rober2_maps/index.json ]; then cat /media/rober2_maps/index.json; else echo '{\"absent\": true}'; fi"
YAML
SHELL_BLOCK=${SHELL_BLOCK%$'\n'}
SHELL_BLOCK_B64=$(printf '%s\n' "$SHELL_BLOCK" | base64 -w0)

# Old public URLs: every one must be refused once the directories are gone.
old_url_paths() {
  local jpg="$1" png="$2"
  printf '%s\n' /local/abb_doorbell/index.json /local/abb_doorbell/index.json.bak_retag \
    /local/rober2_maps/index.json '/local/abb%5Fdoorbell/index.json' '//local/abb_doorbell/index.json' \
    /local/./abb_doorbell/index.json /local/x/../abb_doorbell/index.json '/local/abb_doorbell/index.json?x=1' \
    "/local/abb_doorbell/$jpg" "/local/rober2_maps/$png"
}

# expect: gone (every path 4xx, the plain ones 404) | report (just print the codes)
check_old_urls() {
  local where="$1" expect="$2" jpg="$3" png="$4" out code path fails=0
  if [ "$where" = box ]; then info "on the box, straight to HA (localhost:8123, no Traefik):"; else info "public, through Traefik ($PUBLIC):"; fi
  if [ "$where" = box ]; then
    out=$(old_url_paths "$jpg" "$png" | box 'while IFS= read -r p; do printf "%s %s\n" "$(curl -s --path-as-is -o /dev/null -w "%{http_code}" "http://localhost:8123$p")" "$p"; done; printf "%s %s\n" "$(curl -s -o /dev/null -w "%{http_code}" http://localhost:8123/local/ha-dashboard/version.json)" "/local/ha-dashboard/version.json"')
  else
    out=$(old_url_paths "$jpg" "$png" | while IFS= read -r p; do printf '%s %s\n' "$(curl -4 -s --max-time 15 --path-as-is -o /dev/null -w '%{http_code}' "$PUBLIC$p" || true)" "$p"; done
          printf '%s %s\n' "$(curl -4 -s --max-time 15 -o /dev/null -w '%{http_code}' "$PUBLIC/local/ha-dashboard/version.json" || true)" /local/ha-dashboard/version.json)
  fi
  while read -r code path; do
    case "$path" in
      */ha-dashboard/version.json) [ "$code" = 200 ] || { [ "$expect" = gone ] && fails=$((fails + 1)); } ;;
      *%5F*|//*|*/./*|*/../*|*\?*) case "$code" in 4??) ;; *) [ "$expect" = gone ] && fails=$((fails + 1)) ;; esac ;;
      *) [ "$code" = 404 ] || { [ "$expect" = gone ] && fails=$((fails + 1)); } ;;
    esac
    info "  $code $path"
  done <<<"$out"
  if [ "$expect" = gone ]; then
    [ "$fails" = 0 ] || return 1
    ok "$where: every old URL is refused, the dashboard still loads"
  fi
}

# --- rollback ----------------------------------------------------------------
AD_STOPPED=0; OPTION_SWITCHED=0; CODE_DEPLOYED=0; MOVED=0; ORIG_CLIP_DIR=""
print_rollback() {
  cat <<EOF

== Rollback (only to undo a finished cutover; it republishes the archives on the internet)
  1. ssh $HOST 'sudo docker stop appdaemon'
  2. $REPO/scripts/cutover_private_archives.sh --set-ring-clip-dir ${ORIG_CLIP_DIR:-www/abb_doorbell}
  3. ssh $HOST 'cd $HA_DIR && sudo mv $BACKUP_DIR/abb_doorbell $BACKUP_DIR/rober2_maps www/'
  4. ssh $HOST 'cd $AD_BACKUP && sudo cp -p --parents ${AD_FILES[*]} $AD_DIR/'
  5. ssh $HOST 'sudo docker start appdaemon'
  6. Dashboard: build and deploy the commit before the archive change (the new build reads /media only).
  Rings and maps recorded after the cutover stay in $HA_DIR/media and are missing from the restored www indexes.
  The shell_command block, the AppDaemon /media mount and the backups ($CFG_BAK, $COMPOSE_BAK) can stay.
EOF
}

on_exit() {
  local rc=$?
  { [ "$rc" = 0 ] || [ "$DRY" = 1 ]; } && return
  trap - EXIT
  set +e
  if [ "$MOVED" = 0 ]; then
    step "Undoing (nothing was moved out of www/)"
    if [ "$OPTION_SWITCHED" = 1 ]; then ha option_set "$ORIG_CLIP_DIR"; fi
    if [ "$CODE_DEPLOYED" = 1 ]; then
      box "cd $AD_BACKUP && sudo cp -p --parents ${AD_FILES[*]} $AD_DIR/" && ok "AppDaemon files restored from $AD_BACKUP"
    fi
    if [ "$AD_STOPPED" = 1 ]; then box 'sudo docker start appdaemon' >/dev/null && ok "AppDaemon started"; fi
    info "www/ still serves both archives, exactly as before. The copies under media/ are private and get re-synced by a rerun."
  else
    step "Stopped after the move"
    if [ "$AD_STOPPED" = 1 ]; then box 'sudo docker start appdaemon' >/dev/null && ok "AppDaemon started (new code, archives in media/)"; fi
    info "The archives are in media/ and the old www/ directories in $BACKUP_DIR. Fix the failure above and rerun."
    print_rollback
  fi
  exit "$rc"
}

if [ -n "$SET_CLIP_DIR" ]; then
  step "Set the ABB Welcome ring_clip_dir option"
  if [ "$DRY" = 1 ]; then ha option_set "$SET_CLIP_DIR" probe; would "set ring_clip_dir to $SET_CLIP_DIR"; else ha option_set "$SET_CLIP_DIR"; fi
  exit 0
fi
trap on_exit EXIT

# Index files of one archive copy (www or media): first ring photo, first clip, first map.
pick_files() {
  box "sudo python3 -c '
import json
d = json.load(open(\"$HA_DIR/$1/abb_doorbell/index.json\"))[\"images\"]
m = json.load(open(\"$HA_DIR/$1/rober2_maps/index.json\"))[\"maps\"]
print(next(i[\"filename\"] for i in d if i.get(\"filename\")), next(i[\"clip_filename\"] for i in d if i.get(\"clip_filename\")), m[0][\"filename\"])'"
}

[ "$DRY" = 1 ] && printf 'DRY RUN: every check runs, nothing on the box changes.\n'

# --- 1. local preflight ----------------------------------------------------------
step "Local checkout"
cd "$REPO"
info "AppDaemon repo $REPO at $(git rev-parse --short HEAD) ($(git branch --show-current))"
git diff --quiet HEAD -- "${AD_FILES[@]}" || problem "uncommitted changes in ${AD_FILES[*]}"
if ! grep -q '"/media/abb_doorbell"' apps/intercom/abb_welcome_bridge.yaml \
  || ! grep -q 'MAP_URL_PREFIX = "/media/local/rober2_maps"' apps/rober2/control.py; then
  problem "this checkout does not carry the /media archive code"
fi
for t in apps/intercom/tests apps/rober2/tests; do
  if python3 -m unittest discover -s "$t" -q >/dev/null 2>&1; then ok "unit tests pass: $t"; else problem "unit tests fail: $t"; fi
done
if [ "$SKIP_DASH" = 0 ]; then
  head_sha=$(git -C "$DASH" rev-parse --short HEAD 2>/dev/null || echo none)
  dist_ver=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$DASH/dist/version.json" 2>/dev/null || echo none)
  info "dashboard $DASH at $head_sha, dist/ built from $dist_ver"
  git -C "$DASH" cat-file -e HEAD:src/utils/haArchive.ts 2>/dev/null || problem "dashboard HEAD lacks the archive change (merge private-archives-ui first)"
  [[ "$dist_ver" == "$head_sha"* ]] || problem "dashboard dist/ is not built from HEAD (run npm run build in $DASH)"
  if grep -lq 'doorbell_archive_index' "$DASH"/dist/assets/*.js 2>/dev/null && ! grep -lq '/local/abb_doorbell' "$DASH"/dist/assets/*.js; then
    ok "dashboard build reads the archives over the websocket"
  else
    problem "dashboard dist/ still fetches /local archives"
  fi
  grep -q '^VITE_SSH_HOSTNAME=.' "$DASH/.env" 2>/dev/null || problem "$DASH/.env lacks the deploy settings"
  grep -q '^VITE_HA_TOKEN=.' "$DASH/.env" 2>/dev/null && problem "$DASH/.env sets VITE_HA_TOKEN: npm run deploy would stop at a prompt"
fi

# --- 2. box preflight (read-only) ---------------------------------------------
step "Box access"
box 'sudo -n true' || die "no ssh or passwordless sudo on $HOST"
ha ping

step "State of the box"
declare -A S
while IFS='=' read -r k v; do [ -n "$k" ] && S[$k]="$v"; done < <(box "sudo bash -s" <<'EOF'
cd /data/homeassistant
b() { [ -e "$1" ] && echo 1 || echo 0; }
echo "WWW_DOOR=$(b www/abb_doorbell)"; echo "WWW_MAPS=$(b www/rober2_maps)"
echo "MEDIA_DOOR=$(b media/abb_doorbell/index.json)"; echo "MEDIA_MAPS=$(b media/rober2_maps/index.json)"
bk=""; for d in archive-www-backup-*; do [ -d "$d/abb_doorbell" ] && [ -d "$d/rober2_maps" ] && bk="$PWD/$d"; done; echo "BACKUP=$bk"
grep -q '^shell_command:' configuration.yaml && echo CFG_SHELL=1 || echo CFG_SHELL=0
grep -q '^  doorbell_archive_index: ' configuration.yaml && grep -q '^  rober2_maps_index: ' configuration.yaml && echo CFG_BLOCK=1 || echo CFG_BLOCK=0
docker inspect appdaemon --format '{{range .Mounts}}{{.Source}}={{.Destination}};{{end}}' | grep -q '/data/homeassistant/media=/media;' && echo AD_MOUNT=1 || echo AD_MOUNT=0
echo "AD_RUNNING=$(docker inspect -f '{{if .State.Running}}1{{else}}0{{end}}' appdaemon)"
newest=$(ls -t backups/*.tar 2>/dev/null | head -1)
echo "HA_BACKUP_AGE_H=$([ -n "$newest" ] && echo $(( ($(date +%s) - $(stat -c %Y "$newest")) / 3600 )) || echo none)"
echo "FREE_MB=$(df -Pm /data | awk 'NR==2 {print $4}')"
echo "ARCHIVE_MB=$(du -scm www/abb_doorbell www/rober2_maps 2>/dev/null | tail -1 | cut -f1)"
EOF
)
for k in WWW_DOOR WWW_MAPS MEDIA_DOOR MEDIA_MAPS BACKUP CFG_SHELL CFG_BLOCK AD_MOUNT AD_RUNNING HA_BACKUP_AGE_H FREE_MB ARCHIVE_MB; do
  [ -n "${S[$k]:-}" ] || [ "$k" = BACKUP ] || die "could not read $k from the box"
  info "$k=${S[$k]:-}"
done
ORIG_CLIP_DIR=$(ha option_get) || die "cannot read the ABB Welcome options: $ORIG_CLIP_DIR"
info "ABB Welcome ring_clip_dir=$ORIG_CLIP_DIR"

if [ "${S[WWW_DOOR]}" != "${S[WWW_MAPS]}" ]; then
  die "only one of www/abb_doorbell and www/rober2_maps exists: half-moved, sort it out by hand first"
fi
PHASE=pre
if [ "${S[WWW_DOOR]}" = 0 ]; then
  if [ -z "${S[BACKUP]:-}" ] || [ "${S[MEDIA_DOOR]}" = 0 ] || [ "${S[MEDIA_MAPS]}" = 0 ]; then
    die "both www/ archives are gone but there is no archive-www-backup-* with both, or no media indexes: look before rerunning"
  fi
  PHASE=moved
  BACKUP_DIR="${S[BACKUP]}"
  info "the archives were already moved (www copies in $BACKUP_DIR): verifying only"
fi
if [ "${S[HA_BACKUP_AGE_H]}" != none ] && [ "${S[HA_BACKUP_AGE_H]}" -le 48 ]; then
  ok "newest HA backup is ${S[HA_BACKUP_AGE_H]} h old"
else
  problem "no HA backup from the last 48 h in $HA_DIR/backups"
fi
[ "${S[AD_RUNNING]}" = 1 ] || problem "the appdaemon container is not running"
if [ "$PHASE" = pre ]; then
  if [ "${S[FREE_MB]}" -gt $(( 3 * ${S[ARCHIVE_MB]} + 500 )) ]; then
    ok "${S[FREE_MB]} MB free for ${S[ARCHIVE_MB]} MB of archives"
  else
    problem "not enough space on /data"
  fi
  if [ -e "$BACKUP_DIR/abb_doorbell" ] || [ -e "$BACKUP_DIR/rober2_maps" ]; then problem "$BACKUP_DIR already holds an archive"; fi
  case "$ORIG_CLIP_DIR" in
    "$NEW_CLIP_DIR") ORIG_CLIP_DIR="www/abb_doorbell" ;;
    "www/abb_doorbell"|"/config/www/abb_doorbell") ;;
    *) problem "unexpected ring_clip_dir $ORIG_CLIP_DIR" ;;
  esac
  read -r jpg mp4 png < <(pick_files www) || die "cannot read the www indexes"
else
  read -r jpg mp4 png < <(pick_files media) || die "cannot read the media indexes"
fi

step "Inventory of www/"
box "sudo bash -s" <<'EOF'
cd /data/homeassistant
for d in www/*; do printf '  %6s  %s\n' "$(du -sh "$d" | cut -f1)" "$d"; done
find www -type l -printf '  SYMLINK %p -> %l\n'
find www \( -path www/abb_doorbell -o -path www/rober2_maps \) -prune -o -type f \( -name 'abb_*' -o -name 'rober2_map*' -o -name 'index.json*' \) -printf '  STRAY %p\n'
for d in www/abb_doorbell www/rober2_maps; do [ -d "$d" ] && find "$d" -maxdepth 1 -type f ! -name '*.jpg' ! -name '*.mp4' ! -name '*.png' -printf '  moves with its directory: %p\n'; done
find /data -xdev \( -name abb_doorbell -o -name rober2_maps \) -type d -printf '  copy elsewhere on /data: %p\n' 2>/dev/null \
  | grep -v -E ' /data/homeassistant/(www|media|archive-www-backup-[0-9]+)/(abb_doorbell|rober2_maps)$' || true
EOF
inv=$(box "sudo find $HA_DIR/www -type l; sudo find $HA_DIR/www \( -path $HA_DIR/www/abb_doorbell -o -path $HA_DIR/www/rober2_maps \) -prune -o -type f \( -name 'abb_*' -o -name 'rober2_map*' \) -print")
if [ -z "$inv" ]; then ok "no symlinks and no stray archive copies elsewhere in www/"; else problem "symlinks or stray archive files in www/ (listed above)"; fi

step "AppDaemon files on the box"
for f in "${AD_FILES[@]}"; do
  boxsum=$(box "sha256sum $AD_DIR/$f 2>/dev/null | cut -d' ' -f1")
  if [ "$boxsum" = "$(sha256sum "$f" | cut -d' ' -f1)" ]; then
    info "$f: already the new version"
    continue
  fi
  known=""
  for c in $(git log -40 --format=%H -- "$f"); do
    [ "$(git show "$c:$f" 2>/dev/null | sha256sum | cut -d' ' -f1)" = "$boxsum" ] && { known=$(git rev-parse --short "$c"); break; }
  done
  if [ -n "$known" ]; then
    info "$f: box runs $known, will be replaced"
  else
    problem "$f on the box matches no recent commit: deploying would revert someone's change"
  fi
done

step "Archives (source check)"
if [ "$PHASE" = pre ]; then archives plan || problem "the www archives do not validate"; else archives media || problem "the media archives do not validate"; fi

step "Rober2 and the doorbell"
ha vacuum "$VACUUM" || problem "Rober2 is not docked"
ha quiet "$QUIET_S" "${RING_SENSORS[@]}" || info "(a ring is recent: the cutover waits for quiet before stopping AppDaemon)"

step "HA config"
ha check_config || problem "HA config check fails today: fix that before touching configuration.yaml"
ha validate_block "$SHELL_BLOCK_B64" || problem "the shell_command block does not validate"
ha option_set "$NEW_CLIP_DIR" probe || problem "the ABB Welcome options form cannot be driven safely"
have_cmds=$(ha services)
info "shell_command services: $have_cmds"

step "Signed access machinery (on an existing media file)"
probe=$(ha media_probe) || die "no media file to probe: $probe"
read -r -a probe_paths <<<"$probe"
ha signcheck "${probe_paths[@]}" || problem "HA does not enforce signed access on /media/local"

# --- 3. prepare --------------------------------------------------------------
step "Prepare: shell_command block in configuration.yaml"
if [ "${S[CFG_BLOCK]}" = 1 ]; then
  ok "already there"
elif [ "${S[CFG_SHELL]}" = 1 ]; then
  problem "configuration.yaml has another shell_command: block; merge these two commands into it by hand"
elif [ "$DRY" = 1 ]; then
  would "back up configuration.yaml to $CFG_BAK and append:"
  printf '%s\n' "$SHELL_BLOCK" | sed 's/^/      /'
else
  box "sudo bash -s -- $CFG_BAK $SHELL_BLOCK_B64" <<'EOF'
set -e
cd /data/homeassistant
[ -e "$1" ] || cp -p configuration.yaml "$1"
{ printf '\n'; printf '%s' "$2" | base64 -d; } >> configuration.yaml
EOF
  if ! ha check_config; then
    box "sudo cp -p $CFG_BAK $HA_DIR/configuration.yaml"
    die "HA config check failed with the block; configuration.yaml restored from $CFG_BAK"
  fi
  ok "appended (backup $CFG_BAK)"
fi
if [ "$have_cmds" = present ]; then
  ok "shell_command services are loaded"
elif [ "$DRY" = 1 ]; then
  would "restart HA (about a minute) so shell_command loads, then wait for both services"
else
  ha restart
  sleep 20
  for i in $(seq 1 60); do
    if ha ping >/dev/null 2>&1 && [ "$(ha services 2>/dev/null)" = present ]; then break; fi
    [ "$i" = 60 ] && die "HA did not come back with the shell_command services within 5 minutes"
    sleep 5
  done
  ok "HA restarted; shell_command services loaded"
fi
if [ "$DRY" = 0 ]; then ha index doorbell_archive_index -; ha index rober2_maps_index -; fi

step "Prepare: /media mount for AppDaemon"
if [ "${S[AD_MOUNT]}" = 1 ]; then
  ok "appdaemon already mounts /data/homeassistant/media at /media"
else
  # The compose file holds secrets, so the edit is diffed and validated on the box.
  planned=$(box "sudo bash -s -- $(printf '%s' "$COMPOSE_PY" | base64 -w0)" <<'EOF'
py=$(printf '%s' "$1" | base64 -d)
first=$(python3 -c "$py" print 2>/dev/null | head -1)
case "$first" in
  present) echo present; exit 0 ;;
  services:*) ;;
  *) echo "      $first"; echo invalid; exit 0 ;;
esac
python3 -c "$py" print | diff /data/docker-compose.yaml - | grep '^>' | sed 's/^/      adds: /'
removed=$(python3 -c "$py" print | diff /data/docker-compose.yaml - | grep -c '^<')
if [ "$removed" = 0 ] && python3 -c "$py" print | docker compose -f - --project-directory /data -p data config -q; then echo valid; else echo invalid; fi
EOF
  )
  edited=$(tail -1 <<<"$planned")
  if [ "$edited" = present ]; then
    info "the compose file already has the line; the container predates it"
  else
    sed '$d' <<<"$planned"
    if [ "$edited" = valid ]; then ok "the edited compose file validates"; else problem "the edited compose file does not validate"; fi
  fi
  if [ "$DRY" = 1 ]; then
    would "back up $COMPOSE to $COMPOSE_BAK, add the line above, then: cd /data && docker compose up -d --no-deps appdaemon"
  else
    box "sudo bash -s -- $COMPOSE_BAK" <<'EOF'
set -e
[ -e "$1" ] || cp -p /data/docker-compose.yaml "$1"
EOF
    [ "$edited" = present ] || box "sudo python3 - apply" <<<"$COMPOSE_PY"
    box "cd /data && sudo docker compose config -q" || { box "sudo cp -p $COMPOSE_BAK $COMPOSE"; die "compose config invalid; restored $COMPOSE_BAK"; }
    box "cd /data && sudo docker compose up -d --no-deps appdaemon"
    mounts=$(box "sudo docker inspect appdaemon --format '{{range .Mounts}}{{.Source}}={{.Destination}};{{end}}'")
    [[ "$mounts" == *'/data/homeassistant/media=/media;'* ]] || die "appdaemon still has no /media mount"
    ok "appdaemon recreated with /media mounted (backup $COMPOSE_BAK)"
  fi
fi

deploy_dashboard() {
  local built
  built=$(cat "$DASH/dist/version.json")
  if [ "$(box "cat $HA_DIR/www/ha-dashboard/version.json")" = "$built" ]; then
    ok "the box already serves this dashboard build"
    return
  fi
  (cd "$DASH" && npm run deploy)
  [ "$(box "cat $HA_DIR/www/ha-dashboard/version.json")" = "$built" ] || die "the box does not serve the new dashboard build"
  ok "the box serves dashboard $(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["version"])' "$DASH/dist/version.json")"
}

if [ "$PHASE" = moved ]; then
  step "Verify the finished cutover"
  if [ "$ORIG_CLIP_DIR" = "$NEW_CLIP_DIR" ]; then ok "ring_clip_dir is $NEW_CLIP_DIR"; else problem "ring_clip_dir is $ORIG_CLIP_DIR"; fi
  ha index doorbell_archive_index - || problem "doorbell index over the websocket"
  ha index rober2_maps_index - || problem "maps index over the websocket"
  ha signcheck "/media/local/abb_doorbell/$jpg" "/media/local/abb_doorbell/$mp4" "/media/local/rober2_maps/$png" \
    || problem "signed access to the media archives"
  check_old_urls box gone "$jpg" "$png" || problem "an old URL is still served on the box"
  check_old_urls public gone "$jpg" "$png" || problem "an old URL is still served publicly"
  if [ "${#PROBLEMS[@]}" -gt 0 ]; then printf '\n%s problem(s):\n' "${#PROBLEMS[@]}"; printf '  - %s\n' "${PROBLEMS[@]}"; exit 1; fi
  if [ "$SKIP_DASH" = 0 ] && [ "$DRY" = 0 ]; then step "Dashboard"; deploy_dashboard; fi
  printf '\nThe cutover is done and verified.\n'
  exit 0
fi

if [ "$DRY" = 1 ]; then
  step "Cutover (dry run: the actions, in order)"
  would "wait until no ring edge for ${QUIET_S} s (up to ${QUIET_MAX_WAIT_S} s) and no *.h264 in www/abb_doorbell, AppDaemon still running"
  would "docker stop appdaemon (no auto-open on the doorbell until AppDaemon starts again, about 2 minutes)"
  would "options flow: ring_clip_dir '$ORIG_CLIP_DIR' -> '$NEW_CLIP_DIR', then check quiet again (up to ${QUIET_STOPPED_MAX_S} s)"
  would "install -d -o root -g mke -m 2775 $HA_DIR/media/abb_doorbell $HA_DIR/media/rober2_maps"
  would "rsync -a --checksum www/abb_doorbell/ media/abb_doorbell/ and www/rober2_maps/ media/rober2_maps/"
  would "write both media index.json files with /media/local URLs (temp file, rename); validate every entry and file; diff -rq www media"
  would "copy the box's ${AD_FILES[*]} to $AD_BACKUP, rsync this checkout's versions, compare sha256"
  would "websocket: both shell_commands return the full indexes; signed, unsigned, tampered and cross-file access to $jpg, $mp4, $png"
  would "mv www/abb_doorbell www/rober2_maps $BACKUP_DIR/"
  would "old URLs on localhost:8123 and $PUBLIC: refused; the dashboard's version.json: 200"
  would "docker start appdaemon; wait for AbbWelcomeBridge and rober2_control, 'Map save directory: /media/rober2_maps', the map cleanup"
  would "www/ archives still absent, media indexes intact, old URLs refused again"
  if [ "$SKIP_DASH" = 0 ]; then would "npm run deploy in $DASH, then compare the box's version.json"; fi
  if [ "$TEST_PUSH" = 1 ]; then would "push /media/local/abb_doorbell/$jpg as the image to notify.$TEST_PUSH_SERVICE"; fi
  step "Old URLs today (expected 200 before the cutover)"
  check_old_urls box report "$jpg" "$png"
  check_old_urls public report "$jpg" "$png"
  print_rollback
  if [ "${#PROBLEMS[@]}" -gt 0 ]; then
    printf '\nDry run found %s problem(s):\n' "${#PROBLEMS[@]}"; printf '  - %s\n' "${PROBLEMS[@]}"; exit 1
  fi
  printf '\nDry run clean: nothing blocks the cutover.\n'
  exit 0
fi

# --- 4. cutover ----------------------------------------------------------------
wait_quiet() {  # wait_quiet <max seconds>
  local deadline=$(( SECONDS + $1 )) out
  while true; do
    if out=$(ha quiet "$QUIET_S" "${RING_SENSORS[@]}"); then printf '%s\n' "$out"; return 0; fi
    [ "$SECONDS" -ge "$deadline" ] && { printf '%s\n' "$out"; return 1; }
    sleep 10
  done
}
# The integration writes a .h264 only while it finalizes a clip.
clip_in_flight() { box "sudo find $HA_DIR/www/abb_doorbell $HA_DIR/media -maxdepth 2 -name '*.h264' -type f"; }
# A log's lines written after mark_logs ran (all of it if it rotated since).
declare -A MARK
mark_logs() { for log in appdaemon.log rober2.log error.log; do MARK[$log]=$(box "sudo cat $AD_DIR/logs/$log 2>/dev/null | wc -l"); done; }
since() { box "f=$AD_DIR/logs/$1; if [ \$(sudo cat \$f | wc -l) -lt ${MARK[$1]} ]; then sudo cat \$f; else sudo tail -n +$(( ${MARK[$1]} + 1 )) \$f; fi"; }

step "Wait for quiet (AppDaemon running)"
ha vacuum "$VACUUM"
wait_quiet "$QUIET_MAX_WAIT_S" || die "the doorbell never went quiet for ${QUIET_S} s"
[ -z "$(clip_in_flight)" ] || die "a ring clip is being finalized"

step "Stop AppDaemon"
box 'sudo docker stop appdaemon' >/dev/null
AD_STOPPED=1
ok "stopped"

step "Point the integration's ring clips at $NEW_CLIP_DIR"
OPTION_SWITCHED=1
ha option_set "$NEW_CLIP_DIR"
wait_quiet "$QUIET_STOPPED_MAX_S" || die "a ring arrived during the cutover"
[ -z "$(clip_in_flight)" ] || die "a ring clip is being finalized"

step "Final sync of both archives"
box "sudo bash -s" <<'EOF'
set -e
cd /data/homeassistant
install -d -o root -g mke -m 2775 media/abb_doorbell media/rober2_maps
rsync -a --checksum www/abb_doorbell/ media/abb_doorbell/
rsync -a --checksum www/rober2_maps/ media/rober2_maps/
EOF
archives apply || die "the media archives do not validate"
diffs=$(box "cd $HA_DIR && sudo diff -rq www/abb_doorbell media/abb_doorbell; sudo diff -rq www/rober2_maps media/rober2_maps" || true)
unexpected=$(grep -v -E '^Files www/(abb_doorbell|rober2_maps)/index.json and media/(abb_doorbell|rober2_maps)/index.json differ$|^Only in media/' <<<"$diffs" || true)
[ -z "$unexpected" ] || { sed 's/^/  /' <<<"$unexpected"; die "the media copies differ from www/ beyond index.json"; }
ok "media copies match www/ (only index.json differs)"
counts=$(box "sudo python3 -c 'import json; print(len(json.load(open(\"$HA_DIR/media/abb_doorbell/index.json\"))[\"images\"]), len(json.load(open(\"$HA_DIR/media/rober2_maps/index.json\"))[\"maps\"]))'")
read -r doorbell_n maps_n <<<"$counts"

step "AppDaemon code"
box "sudo bash -s -- $AD_BACKUP ${AD_FILES[*]}" <<'EOF'
set -e
dest="$1"; shift
cd /data/appdaemon
if [ ! -e "$dest" ]; then mkdir -p "$dest"; cp -p --parents "$@" "$dest/"; fi
EOF
CODE_DEPLOYED=1
printf '%s\n' "${AD_FILES[@]}" | rsync -rlz --checksum --no-perms --no-owner --no-group --files-from=- . "$HOST:$AD_DIR/"
for f in "${AD_FILES[@]}"; do
  [ "$(box "sha256sum $AD_DIR/$f" | cut -d' ' -f1)" = "$(sha256sum "$f" | cut -d' ' -f1)" ] || die "$f did not land intact"
done
ok "${#AD_FILES[@]} files in place (box copies kept in $AD_BACKUP)"

step "Signed-in access works, unsigned access is refused"
ha index doorbell_archive_index "$doorbell_n"
ha index rober2_maps_index "$maps_n"
ha signcheck "/media/local/abb_doorbell/$jpg" "/media/local/abb_doorbell/$mp4" "/media/local/rober2_maps/$png"

step "Move the public directories out of www/"
box "sudo bash -s -- $BACKUP_DIR" <<'EOF'
set -e
cd /data/homeassistant
mkdir -p "$1"
mv www/abb_doorbell "$1/"
mv www/rober2_maps "$1/" || { mv "$1/abb_doorbell" www/; exit 1; }
EOF
MOVED=1
ok "www/abb_doorbell and www/rober2_maps are in $BACKUP_DIR"

step "Old URLs right after the move"
check_old_urls box gone "$jpg" "$png" || die "an old URL is still served on the box"
check_old_urls public gone "$jpg" "$png" || die "an old URL is still served publicly"

step "Start AppDaemon"
mark_logs
box 'sudo docker start appdaemon' >/dev/null
AD_STOPPED=0
for i in $(seq 1 48); do
  log=$(since appdaemon.log)
  if grep -q -E 'Calling initialize\(\) for AbbWelcomeBridge$' <<<"$log" && grep -q -E 'Calling initialize\(\) for rober2_control$' <<<"$log"; then break; fi
  [ "$i" = 48 ] && die "AbbWelcomeBridge and rober2_control did not initialize within 4 minutes"
  sleep 5
done
grep -q 'Map save directory: /media/rober2_maps' <<<"$(since rober2.log)" || die "rober2_control does not report /media/rober2_maps"
! grep -E 'Archive dir .* unavailable' <<<"$(since appdaemon.log)" || die "the bridge cannot use its archive dir"
ok "both apps initialized on /media"
sleep 45  # rober2's startup map cleanup runs 30 s after init
! grep -E 'Map cleanup skipped|Error in cleanup_old_map_images|Error updating map index|Error creating map save directory' <<<"$(since rober2.log)" \
  || die "the map cleanup refused the index"
! grep -E 'AbbWelcomeBridge|rober2' <<<"$(since error.log)" || die "errors from the archive apps in error.log"
archives media || die "the media archives no longer validate after AppDaemon started"

step "Old URLs with the producers running"
[ -z "$(box "ls -d $HA_DIR/www/abb_doorbell $HA_DIR/www/rober2_maps 2>/dev/null || true")" ] || die "a www/ archive directory came back"
ok "www/abb_doorbell and www/rober2_maps stay absent"
check_old_urls box gone "$jpg" "$png" || die "an old URL is served again on the box"
check_old_urls public gone "$jpg" "$png" || die "an old URL is served again publicly"

if [ "$SKIP_DASH" = 0 ]; then
  step "Dashboard"
  deploy_dashboard
fi

if [ "$TEST_PUSH" = 1 ]; then
  step "Push photo test"
  ha notify "$TEST_PUSH_SERVICE" "/media/local/abb_doorbell/$jpg"
  info "Mikkel's Android should show a doorbell photo now; a push without it means relative /media/local images fail there."
fi

step "Done"
cat <<EOF
  Still to check by hand (plan section E):
  37. Mikkel's phone on 5G, Wi-Fi off: gallery thumbnails, a clip plays and seeks, a ring row on Now opens
      its photo and clip, the vacuum rooms list and the room-map sheet show maps.
  38. The same as a non-admin (Kristine's or Claudia's phone, or a temporary non-admin user).
  39. Next ring: the jpg and mp4 land in $HA_DIR/media/abb_doorbell and the push carries a photo.
      Next Rober2 run: the maps land in $HA_DIR/media/rober2_maps. www/ stays without either directory.
  40. After 24 h: no 200 on the old paths in the Traefik log, no shell_command errors in the HA log.
  The old www/ directories are in $BACKUP_DIR; deleting them is Mikkel's call.
EOF
print_rollback
