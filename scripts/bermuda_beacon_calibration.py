"""Scores Bermuda's room choice for the beacon on Rober2's lidar cap against the room the robot reports,
and fits per-scanner RSSI offsets that would have scored better. Read-only from the recorder database.

The replay picks the scanner with the shortest recorded distance, which matches Bermuda's own area about
90 % of the time. An offset of +X dB shortens that scanner's distance by 10^(X/30) (attenuation 3).
Distances are recorded with the offsets active at the time; OFFSET_HISTORY replays earlier samples as if
today's offsets had applied, so the fitted offsets are changes on top of the current Bermuda options.
Rooms are weighted equally. "held-out" refits once per robot room visit without that visit, so a room
visited only once scores how a fit generalises to it, not how well it fits it.

Run on the box: ssh rooftop "sudo python3 - 2026-11-16T09:00:00 2026-11-16T13:00:00" < scripts/bermuda_beacon_calibration.py
"""
import bisect, collections, json, sqlite3, sys
from datetime import datetime, timezone

BEACON = "sensor.mp1_142377"
ROBOT_ROOM = {"Kitchen1": "Kitchen", "Claudias room": "Claudias Room"}
# (UTC time, distance entity suffix, dB added to that scanner's offset)
OFFSET_HISTORY = [("2026-11-16T15:20:00", "bathroom_display", 8)]
ATTENUATION = 3.0
STORAGE = "/data/homeassistant/.storage/"


def ts(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


START, END = ts(sys.argv[1]), ts(sys.argv[2])
db = sqlite3.connect("file:/data/homeassistant/home-assistant_v2.db?mode=ro", uri=True)


def load(name):
    return json.load(open(STORAGE + name))["data"]


areas = {a["id"]: a["name"] for a in load("core.area_registry")["areas"]}
mac_area = {}
for d in load("core.device_registry")["devices"]:
    for kind, mac in d.get("connections") or []:
        if d.get("area_id"):
            mac_area[(kind, mac.lower())] = areas[d["area_id"]]


def area_of(mac):
    # Bermuda names an ESPHome/Shelly scanner by its base MAC; the radio is base + 0..3, and that bluetooth
    # device's area is where the scanner sits (the TV lift's controller is not where the lift is).
    head, last = mac[:-2], int(mac[-2:], 16)
    for step in range(4):
        found = mac_area.get(("bluetooth", f"{head}{last + step:02x}"))
        if found:
            return found
    return mac_area.get(("mac", mac))


prefix = BEACON + "_distance_to_"
scanner_area = {}
for e in load("core.entity_registry")["entities"]:
    uid = e.get("unique_id") or ""
    if e["entity_id"].startswith(prefix) and uid.endswith("_range"):
        scanner_area[e["entity_id"][len(prefix):]] = area_of(uid[: -len("_range")].split("_")[-1])


def history(entity_ids):
    q = ("select m.entity_id, s.state, s.last_updated_ts from states s join states_meta m on s.metadata_id = m.metadata_id"
         " where m.entity_id in (%s) and s.last_updated_ts between ? and ? order by s.last_updated_ts")
    out = collections.defaultdict(list)
    ids = list(entity_ids)
    for e, s, t in db.execute(q % ",".join("?" * len(ids)), ids + [START - 3600, END]):
        out[e].append((t, s))
    return out


rows = history([prefix + k for k in scanner_area] + ["sensor.rober2_current_room", "vacuum.rober2", BEACON + "_area"])


def at(series, t):
    i = bisect.bisect_right([x[0] for x in series], t) - 1
    return series[i] if i >= 0 else (None, None)


later = [(ts(t), k, db_) for t, k, db_ in OFFSET_HISTORY]
dist = {}
for k in scanner_area:
    pts = []
    for t, s in rows[prefix + k]:
        try:
            v = float(s)
        except ValueError:
            v = None
        if v is not None:
            v *= 10 ** (-sum(x for when, kk, x in later if kk == k and t < when) / (10 * ATTENUATION))
        pts.append((t, v))
    dist[k] = ([p[0] for p in pts], pts)

samples = []
truth = rows["sensor.rober2_current_room"]
t = START
while t < END:
    since, room = at(truth, t)
    room = ROBOT_ROOM.get(room, room)
    if at(rows["vacuum.rober2"], t)[1] == "cleaning" and room not in (None, "unknown") and t - since >= 30:
        heard = {}
        for k, (times, pts) in dist.items():
            i = bisect.bisect_right(times, t) - 1
            if i >= 0 and pts[i][1] is not None and t - pts[i][0] < 120 and scanner_area[k]:
                heard[k] = pts[i][1]
        samples.append((room, heard, at(rows[BEACON + "_area"], t)[1], since))
    t += 5
if not samples:
    sys.exit("no cleaning samples in the window")


def predict(heard, off):
    return min(heard.items(), key=lambda kv: kv[1] * 10 ** (-off.get(kv[0], 0) / (10 * ATTENUATION)), default=(None,))[0]


def score(off, ss):
    per = collections.defaultdict(lambda: [0, 0])
    for room, heard, _, _ in ss:
        k = predict(heard, off)
        per[room][0] += k is not None and scanner_area[k] == room
        per[room][1] += 1
    return sum(a / n for a, n in per.values()) / len(per), per


def fit(ss, lam=0.002, cap=8):
    off = {}
    for _ in range(4):
        changed = False
        for k in scanner_area:
            def cost(v):
                o = dict(off, **{k: v})
                return score(o, ss)[0] - lam * sum(abs(x) for x in o.values())
            best = max(range(-cap, cap + 1), key=lambda v: (cost(v), -abs(v)))
            if best != off.get(k, 0) and cost(best) > cost(off.get(k, 0)) + 1e-9:
                off[k] = best
                changed = True
        if not changed:
            break
    return {k: v for k, v in off.items() if v}


now_score, now_per = score({}, samples)
off = fit(samples)
fit_score, fit_per = score(off, samples)
held = collections.defaultdict(lambda: [0, 0])
for visit in sorted(set(s[3] for s in samples)):
    o = fit([s for s in samples if s[3] != visit])
    for room, heard, _, v in samples:
        if v == visit:
            k = predict(heard, o)
            held[room][0] += k is not None and scanner_area[k] == room
            held[room][1] += 1
bermuda = collections.defaultdict(lambda: [0, 0])
for room, _, area, _ in samples:
    bermuda[room][0] += area == room
    bermuda[room][1] += 1

print(f"{len(samples)} samples of 5 s; replay agrees with Bermuda's own area "
      f"{sum(predict(h, {}) is not None and scanner_area[predict(h, {})] == a for _, h, a, _ in samples) / len(samples):.0%}")
print("fitted offset changes (dB, on top of today's options):", off or "none")
print(f"{'room':16} {'min':>5} {'bermuda':>8} {'replay':>7} {'fitted':>7} {'held-out':>9}")
for r in sorted(now_per):
    n = now_per[r][1]
    print(f"{r:16} {n / 12:5.1f} {bermuda[r][0] / n:8.0%} {now_per[r][0] / n:7.0%} {fit_per[r][0] / n:7.0%} {held[r][0] / n:9.0%}")
print(f"{'rooms averaged':16} {'':5} {sum(a / n for a, n in bermuda.values()) / len(bermuda):8.0%} {now_score:7.0%} "
      f"{fit_score:7.0%} {sum(a / n for a, n in held.values()) / len(held):9.0%}")
