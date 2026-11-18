"""Compares BLE (Bermuda area) with the router wifi trackers as a home/away signal, read-only from the
recorder database: runs where BLE heard nobody while wifi said home (false away), runs where wifi said
away while BLE still heard the phone, and per wifi departure/arrival the door and BLE times. Times print
in the box clock (UTC). Run on the box: ssh rooftop "sudo python3 - 2026-11-16T07:20:00" < scripts/ble_home_away_report.py
"""
import sqlite3, sys, time
from datetime import datetime
db = sqlite3.connect("file:/data/homeassistant/home-assistant_v2.db?mode=ro", uri=True)
START = datetime.fromisoformat(sys.argv[1]).timestamp()
END = time.time()
PEOPLE = {
  "Mikkel": ("device_tracker.mikkels_ofx9p", "sensor.bermuda_d6462f88944242db9e31bdff598edc25_100_40004_area"),
  "Claudia": ("device_tracker.claudias_iphone_2", "sensor.claudias_iphone_ble_area"),
  "Kristine": ("device_tracker.kristines_iphone", "sensor.kristines_iphone_ble_area"),
}
DOOR = "binary_sensor.apartment_door_open"
def series(eid):
    mid = db.execute("select metadata_id from states_meta where entity_id=?", (eid,)).fetchone()[0]
    prev = db.execute("select state from states where metadata_id=? and last_updated_ts<? order by last_updated_ts desc limit 1", (mid, START)).fetchone()
    rows = db.execute("select last_updated_ts, state from states where metadata_id=? and last_updated_ts>=? and last_updated_ts<? order by last_updated_ts", (mid, START, END)).fetchall()
    out = [(START, prev[0] if prev else None)] + rows
    # collapse repeats
    c = []
    for t, s in out:
        if not c or c[-1][1] != s: c.append((t, s))
    return c
def at(ser, t):
    s = None
    for tt, ss in ser:
        if tt > t: break
        s = ss
    return s
def runs(ser, pred):
    res = []
    for i, (t, s) in enumerate(ser):
        e = ser[i+1][0] if i+1 < len(ser) else END
        if pred(s):
            if res and abs(res[-1][1]-t) < 1: res[-1][1] = e
            else: res.append([t, e])
    return res
fmt = lambda t: datetime.fromtimestamp(t).strftime("%d.%m %H:%M:%S")
door = series(DOOR)
door_opens = [t for t, s in door if s == "on"]
for name, (wifi_e, area_e) in PEOPLE.items():
    wifi, area = series(wifi_e), series(area_e)
    ble_gone = lambda s: s in (None, "unknown", "unavailable")
    print(f"=== {name}")
    # BLE gone while wifi home (false away candidates), merged runs, ignoring unavailable blips < 10 s
    gone = runs(area, ble_gone)
    fa = []
    for a, b in gone:
        # portion where wifi home
        wh = runs(wifi, lambda s: s == "home")
        for x, y in wh:
            lo, hi = max(a, x), min(b, y)
            if hi - lo > 0: fa.append((lo, hi))
    fa = [r for r in fa if r[1]-r[0] >= 60]
    print(f"BLE unheard >= 1 min while wifi home: {len(fa)} runs, total {sum(b-a for a,b in fa)/3600:.1f} h")
    for a, b in sorted(fa, key=lambda r: r[0]-r[1])[:8]:
        print(f"   {fmt(a)} -> {fmt(b)}  {(b-a)/60:.0f} min")
    # wifi away, BLE heard (wifi false away)
    wa = []
    for x, y in runs(wifi, lambda s: s != "home"):
        for a, b in runs(area, lambda s: not ble_gone(s)):
            lo, hi = max(a, x), min(b, y)
            if hi - lo >= 60: wa.append((lo, hi))
    print(f"wifi away while BLE heard >= 1 min: {len(wa)} runs, total {sum(b-a for a,b in wa)/60:.0f} min")
    for a, b in wa[:8]:
        print(f"   {fmt(a)} -> {fmt(b)}  {(b-a)/60:.0f} min  area at start {at(area, a)}")
    # departures / arrivals by wifi
    for t, s in wifi:
        if t == START: continue
        prevs = at(wifi, t-0.001)
        if s != "home" and prevs == "home":
            d = max([o for o in door_opens if t-1800 < o <= t], default=None)
            gone_t = next((a for a, b in gone if d and a >= d), None) if d else None
            print(f"  LEFT  wifi {fmt(t)}  door {fmt(d) if d else '-'}  ble gone {fmt(gone_t) if gone_t else '-'}  ble lead {(t-gone_t)/60:.1f} min" if gone_t else f"  LEFT  wifi {fmt(t)}  door {fmt(d) if d else '-'}  ble gone -")
        if s == "home" and prevs not in ("home", None):
            heard = next((a for a, b in runs(area, lambda s: not ble_gone(s)) if t-900 <= a <= t+900), None)
            d = min([o for o in door_opens if t-600 <= o <= t+600], key=lambda o: abs(o-t), default=None)
            print(f"  CAME  wifi {fmt(t)}  door {fmt(d) if d else '-'}  ble heard {fmt(heard) if heard else '-'}")
