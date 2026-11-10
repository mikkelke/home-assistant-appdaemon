"""
BleVisitLog - MEASUREMENT ONLY: which Bluetooth addresses does Bermuda hear around a front-door
unlock? It decides nothing, creates no entity and sends no notification. The question is whether
the house cleaner carries a device with a stable address (watch, earbuds, a phone with a static
address) that could later notice her visits, also when someone lets her in by hand: compare her
windows with the residents' offline.

WINDOW. Opens when `lock` goes to unlocked or `visitor` changes to cleaner. A later unlock or
cleaner visit is added to its events; it neither restarts nor extends the window. persons_home
lists the person.* entities home when it opened.

SNAPSHOTS. first_delay_s after the open, then every interval_s until window_min after it, the app
calls bermuda.dump_devices and keeps per address name, manufacturer, address_type, local_name,
beacon (uuid, major, minor or null), first and last (first and last snapshot that saw it), n
(snapshots that saw it), areas (sorted area_ids) and best_rssi (highest area_rssi or null). A
failed or empty dump skips that snapshot.

SEEN. A device was seen when its last_seen, on HA's time.monotonic() clock (HA and AppDaemon share
the host, so the clock), is 0 to interval_s + 30 s old; last_seen 0 means never seen. If a dump has
devices but none qualifies and some ages are negative or over a day, the clocks do not match:
that is warned about once per window and a device then counts as seen when its last_seen has
grown since the previous snapshot of the window.

CLOSE. window_min after the open, or when the app stops, one JSON line is appended to log_file:
{start, end, trigger (unlock or visitor), changed_by, persons_home, events, snapshots, devices}.
snapshots counts the dumps kept. Nothing prunes log_file; the window lives in memory only.

CHANGED_BY. The lock updates changed_by a few seconds after its state, so the opening unlock's is
read at the first snapshot, not in the state callback. A later event's is read at the snapshot
after it (null when none follows), by which time it can be a lock label such as Auto Lock; the
visitor events carry who instead.
"""

import asyncio
import itertools
import json
import math
import threading
import time
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

DUMP_TIMEOUT_S = 30
SEEN_SLACK_S = 30
DAY_S = 86400


def _number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def devices_from(resp):
    """{address: device} out of AppDaemon's envelope for bermuda.dump_devices, {} when there is
    none. Only dicts with a last_seen are devices: an error envelope holds dicts that are not."""
    node = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    if not isinstance(node, dict):
        return {}
    return {str(addr): dev for addr, dev in node.items() if isinstance(dev, dict) and "last_seen" in dev}


def read_dump(devices, now, max_age_s, before):
    """(seen, stamps, mismatch) for one dump. `now` is the monotonic clock and `before` the stamps
    of the previous dump in the window. stamps holds every device's last_seen; mismatch is true
    when the clocks look unrelated, and seen is then the devices whose stamp grew."""
    stamps = {}
    for addr, dev in devices.items():
        last_seen = _number(dev.get("last_seen"))
        if last_seen is not None and last_seen > 0:
            stamps[addr] = last_seen
    seen = [addr for addr, last_seen in stamps.items() if 0 <= now - last_seen <= max_age_s]
    mismatch = not seen and any(now - last_seen < 0 or now - last_seen > DAY_S for last_seen in stamps.values())
    if mismatch:
        seen = [addr for addr, last_seen in stamps.items() if last_seen > before.get(addr, last_seen)]
    return seen, stamps, mismatch


def update_device(devices, addr, dev, stamp):
    """Adds one sighting of `addr` at `stamp` to the window's per-address record."""
    rec = devices.setdefault(addr, {"name": None, "manufacturer": None, "address_type": None,
                                    "local_name": None, "beacon": None, "first": stamp, "last": stamp,
                                    "n": 0, "areas": [], "best_rssi": None})
    rec["last"] = stamp
    rec["n"] += 1
    for key, source in (("name", "name"), ("manufacturer", "manufacturer"), ("address_type", "address_type"),
                        ("local_name", "name_bt_local_name")):
        rec[key] = dev.get(source) or rec[key]
    if dev.get("beacon_uuid"):
        rec["beacon"] = {"uuid": dev["beacon_uuid"], "major": dev.get("beacon_major"),
                         "minor": dev.get("beacon_minor")}
    area = str(dev["area_id"]) if dev.get("area_id") else None
    if area is not None and area not in rec["areas"]:
        rec["areas"] = sorted([*rec["areas"], area])
    rssi = _number(dev.get("area_rssi"))
    if rssi is not None and (rec["best_rssi"] is None or rssi > rec["best_rssi"]):
        rec["best_rssi"] = rssi


class BleVisitLog(hass.Hass):
    def initialize(self):
        # Guards _win. A snapshot is kept on the event loop, and AppDaemon calls such as get_state
        # and run_in wait for that loop, so none may be made while this is held.
        self._lock = threading.RLock()
        self._win = None
        self._ids = itertools.count(1)
        a = self.args
        self.lock_entity = a.get("lock", "lock.yale")
        self.visitor_entity = a.get("visitor", "sensor.untracked_visitor")
        self.window_s = float(a.get("window_min", 180)) * 60
        self.interval_s = float(a.get("interval_s", 300))
        self.first_delay_s = max(0.0, float(a.get("first_delay_s", 20)))
        self.log_file = Path(a.get("log_file") or Path(__file__).resolve().parent / "ble_visit_log.jsonl")
        if self.window_s <= 0 or self.interval_s <= 0:
            raise ValueError("BleVisitLog: window_min and interval_s must be positive")
        self._tz = self.get_now().tzinfo

        self.listen_state(self._on_unlock, self.lock_entity, new="unlocked")
        self.listen_state(self._on_cleaner, self.visitor_entity, new="cleaner")
        self.log(f"BleVisitLog initialized: {self.window_s / 60:g} min windows after {self.lock_entity} "
                 f"unlocks and {self.visitor_entity} cleaner visits, a snapshot every {self.interval_s:g} s "
                 f"-> {self.log_file}", level="INFO")

    def terminate(self):
        self._close()

    def _on_unlock(self, entity, attribute, old, new, kwargs):
        self._note("unlock")

    def _on_cleaner(self, entity, attribute, old, new, kwargs):
        self._note("visitor", {"state": new,
                               "who": self.get_state(self.visitor_entity, attribute="who"),
                               "reason": self.get_state(self.visitor_entity, attribute="reason")})

    def _note(self, kind, state=None):
        event = {"t": self._stamp(), "kind": kind, "changed_by": None, "state": state}
        persons = sorted(entity for entity, st in (self.get_state("person") or {}).items()
                         if isinstance(st, dict) and st.get("state") == "home")
        with self._lock:
            opened = self._win is None
            if opened:
                self._win = {"id": next(self._ids), "start": event["t"], "trigger": kind,
                             "persons_home": persons, "events": [event], "snapshots": 0,
                             "devices": {}, "before": {}, "warned": False}
            else:
                self._win["events"].append(event)
            wid = self._win["id"]
        if not opened:
            self.log(f"BLE {kind} noted in the open window", level="INFO")
            return
        offsets = [self.first_delay_s + i * self.interval_s
                   for i in range(math.ceil((self.window_s - self.first_delay_s) / self.interval_s))]
        for offset in offsets:
            self.run_in(self._on_due, offset, wid=wid)
        self.run_in(self._on_expired, self.window_s, wid=wid)
        self.log(f"BLE window opened by {kind}: {len(offsets)} snapshots planned, {len(persons)} persons home",
                 level="INFO")

    def _on_due(self, kwargs):
        by = self.get_state(self.lock_entity, attribute="changed_by")
        stamp = self._stamp()
        with self._lock:
            win = self._win
            if win is None or win["id"] != kwargs["wid"]:
                return
            for event in win["events"]:
                if event["changed_by"] is None:
                    event["changed_by"] = by
        self.create_task(self._snapshot(win, stamp))

    def _on_expired(self, kwargs):
        self._close(kwargs["wid"])

    async def _snapshot(self, win, stamp):
        try:
            resp = await asyncio.wait_for(self.call_service("bermuda/dump_devices", return_response=True),
                                          timeout=DUMP_TIMEOUT_S)
        except Exception as e:
            self.log(f"BLE snapshot skipped, bermuda.dump_devices failed: {e!a}", level="WARNING")
            return
        now = self._monotonic()
        devices = devices_from(resp)
        if not devices:
            self.log(f"BLE snapshot skipped, bermuda.dump_devices returned no devices: {str(resp)[:100]!a}",
                     level="WARNING")
            return
        with self._lock:
            if self._win is not win:
                return
            seen, stamps, mismatch = read_dump(devices, now, self.interval_s + SEEN_SLACK_S, win["before"])
            win["before"] = stamps
            win["snapshots"] += 1
            count = win["snapshots"]
            for addr in seen:
                update_device(win["devices"], addr, devices[addr], stamp)
            warn = mismatch and not win["warned"]
            win["warned"] = win["warned"] or mismatch
        if warn:
            self.log("BLE monotonic clock mismatch: no device is recent by Home Assistant's clock and some ages "
                     "are negative or over a day, counting devices whose last_seen grew since the previous "
                     "snapshot", level="WARNING")
        self.log(f"BLE snapshot {count}: {len(devices)} devices in the dump, {len(seen)} seen", level="DEBUG")

    def _close(self, wid=None):
        with self._lock:
            win = self._win
            if win is None or (wid is not None and win["id"] != wid):
                return
            self._win = None
        line = json.dumps({"start": win["start"], "end": self._stamp(), "trigger": win["trigger"],
                           "changed_by": win["events"][0]["changed_by"], "persons_home": win["persons_home"],
                           "events": win["events"], "snapshots": win["snapshots"], "devices": win["devices"]},
                          default=str)
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except OSError as e:
            self.log(f"BLE window lost, could not write {self.log_file}: {e!a}", level="WARNING")
            return
        self.log(f"BLE window closed: {win['snapshots']} snapshots, {len(win['devices'])} devices "
                 f"-> {self.log_file.name}", level="INFO")

    def _now(self):
        return datetime.now(self._tz)

    def _stamp(self):
        return self._now().isoformat(timespec="seconds")

    def _monotonic(self):
        return time.monotonic()
