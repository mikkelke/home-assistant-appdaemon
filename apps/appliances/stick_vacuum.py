"""
StickVacuum - sessions, cleaner visits and maintenance facts for the LG CordZero A9K.

Cleaning opens a session. Twenty minutes without Cleaning off the mount, or five minutes
continuously Charging/Fully charged, closes it. Only Cleaning intervals count as suction,
bucketed by the power mode at the time; an unknown status suspends the clocks and makes an
open session partial. A session belongs to the cleaner if she was present at any point.

The feedback file holds one cycles record per session and one visits record per cleaner
visit, including visits without suction. Visit totals count only suction during that visit.
An admin house-events receipt reports a visit that used the stick. Recorder history rebuilds
the session and visit at restart; starts already in the ledger are never appended twice.
The file also keeps a checkpoint for when recorder history cannot be read. Writes replace
the file through a tmp file, as the other appliance ledgers do.

Publishes sensor.stick_vacuum (in_use | off_mount | charging | charged | unknown), with
string attributes describing its current state, the last session and visit, and problems.
binary_sensor.stick_vacuum_in_use is on during Cleaning or Ready in an open session, off
otherwise, and unknown with an unknown status. The mount's five-minute closing grace does
not hold a cleaner visit open. HouseUpcoming owns maintenance rows and all fault pushes.
"""

from __future__ import annotations

import copy
import json
import threading
from datetime import datetime, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import feedback_io
import run_ledger
import washer_history as whist

STATUSES = ("Ready", "Cleaning", "Charging", "Fully charged")
MOUNTED = ("Charging", "Fully charged")
MODES = ("Normal", "High", "Turbo", "unknown")
IDLE_S = 20 * 60
DOCK_S = 5 * 60
CLOG_S = 2 * 60
NOT_CHARGING_S = 30 * 60
TICK_S = 30
REPUBLISH_S = 300
HISTORY_S = 7 * 24 * 3600


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat(timespec="seconds") if value is not None else ""


def epoch(value):
    if isinstance(value, datetime):
        return (value if value.tzinfo else value.replace(tzinfo=timezone.utc)).timestamp()
    parsed = run_ledger.parse_time(value)
    return parsed.timestamp() if parsed is not None else None


def suction():
    return dict.fromkeys(MODES, 0.0)


def receipt(record):
    seconds = record["suction_seconds"]
    modes = ", ".join(f"{mode} {seconds.get(mode, 0) / 60:.0f}" for mode in MODES if seconds.get(mode, 0))
    text = f"Vacuumed {record['duration_min']:.0f} min: {modes}, {record['trigger_starts']} starts"
    return text + (" (partial)" if record["coverage"] == "partial" else "")


def used(record):
    return record.get("duration_min", 0) > 0 or record.get("trigger_starts", 0) > 0


def session_record(session, end):
    seconds = dict(session["suction_seconds"])
    return {"ts": stamp(end), "start": stamp(session["start"]), "end": stamp(end),
            "duration_min": sum(seconds.values()) / 60, "suction_seconds": seconds,
            "trigger_starts": session["trigger_starts"], "docked_at": stamp(session["docked_at"]),
            "coverage": session["coverage"], "visit": session["visit"]}


class StickVacuum(hass.Hass):
    def initialize(self):
        a = self.args
        self.entity = a.get("entity", "sensor.stick_vacuum")
        self.in_use_entity = a.get("in_use_entity", "binary_sensor.stick_vacuum_in_use")
        self.sources = {
            "status": a.get("status", "sensor.lg_stick_vacuum_status"),
            "power_mode": a.get("power_mode", "sensor.lg_stick_vacuum_power_mode"),
            "battery": a.get("battery", "sensor.lg_stick_vacuum_battery"),
            "filter": a.get("filter", "binary_sensor.lg_stick_vacuum_filter_needs_cleaning"),
            "clogged": a.get("clogged", "binary_sensor.lg_stick_vacuum_air_path_clogged"),
            "visitor": a.get("visitor", "sensor.untracked_visitor"),
            "door": a.get("door", "binary_sensor.apartment_door_open"),
        }
        self.feedback_file = Path(a.get("feedback_file") or Path(__file__).with_name("stick_vacuum_feedback.json"))
        self._lock = threading.RLock()
        self._data = feedback_io.load_for_append(str(self.feedback_file), self.log)
        self._writable = self._data is not None
        self._saved = None
        self._data = self._data or feedback_io.new_history()
        self._data.setdefault("visits", [])
        self._session = None
        self._visit = None
        self._status = None
        self._known_status = None
        self._power_mode = "unknown"
        self._battery = "unknown"
        self._filter = False
        self._clog_since = None
        self._idle_since = None
        self._mounted_since = None
        self._last_mount = None
        self._last_door_close = None
        self._candidate = None
        self._last_closed_at = None
        self._time = None
        self._published_key = None
        self._published_at = 0.0
        self._semantic = None
        self._since = None
        with self._lock:
            now = self._now()
            self._recover(now)
            self._refresh(now)
        for key, entity in self.sources.items():
            self.listen_state(self._on_source, entity, attribute="all", source=key)
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._tick, f"now+{TICK_S}", TICK_S)
        self.log(f"StickVacuum initialized: {self.entity}, {len(self._data['cycles'])} sessions", level="INFO")

    # ---------- observations ----------

    def _on_source(self, entity, attribute, old, new, kwargs):
        with self._lock:
            now = self._now()
            self._observe(now, kwargs["source"], new)
            self._refresh(now)

    def _on_plugin_started(self, event_name, data, kwargs):
        with self._lock:
            self._published_key = None
            self._refresh(self._now())

    def _tick(self, kwargs):
        with self._lock:
            now = self._now()
            self._advance(now)
            self._refresh(now)

    def _advance(self, now):
        if self._time is not None and now < self._time:
            return
        if self._time is not None and self._session is not None and self._status == "Cleaning":
            seconds = now - self._time
            mode = self._power_mode if self._power_mode in MODES else "unknown"
            self._session["suction_seconds"][mode] += seconds
            if self._visit is not None:
                self._visit["suction_seconds"][mode] += seconds
        self._time = now
        if self._session is None:
            return
        if self._status in MOUNTED and self._mounted_since is not None and now >= self._mounted_since + DOCK_S:
            self._close(self._mounted_since + DOCK_S)
        elif self._status == "Ready" and self._idle_since is not None and now >= self._idle_since + IDLE_S:
            self._close(self._idle_since + IDLE_S)

    def _observe(self, now, key, full):
        self._advance(now)
        full = full if isinstance(full, dict) else {"state": full}
        state = full.get("state")
        attrs = full.get("attributes") or {}
        if key == "status":
            status = state if state in STATUSES else None
            old, self._status = self._status, status
            previous = self._known_status
            if status is not None:
                self._known_status = status
            if status is None:
                self._idle_since = self._mounted_since = None
                if self._session is not None:
                    self._session["coverage"] = "partial"
                if self._visit is not None and self._session is not None:
                    self._visit["coverage"] = "partial"
            elif status == "Cleaning":
                if self._session is None:
                    self._session = {"start": now, "suction_seconds": suction(), "trigger_starts": 0,
                                     "docked_at": None, "coverage": "complete",
                                     "visit": "cleaner" if self._visit else "household"}
                    previous = None
                if previous != "Cleaning":
                    self._session["trigger_starts"] += 1
                    if self._visit is not None:
                        self._visit["trigger_starts"] += 1
                self._session["docked_at"] = None
                self._idle_since = self._mounted_since = None
            else:
                if self._session is not None and self._idle_since is None:
                    self._idle_since = now
                if status in MOUNTED:
                    if old not in MOUNTED:
                        self._mounted_since = now
                    self._last_mount = now
                    if self._candidate is not None and now >= epoch(self._candidate["end"]):
                        self._candidate = None
                    if status == "Charging" and self._session is not None and self._session["docked_at"] is None:
                        self._session["docked_at"] = now
                else:
                    self._mounted_since = None
            self._include_session()
        elif key == "power_mode":
            self._power_mode = state if state in ("Off", "Normal", "High", "Turbo") else "unknown"
        elif key == "battery":
            self._battery = state if state in ("High", "Medium", "Low", "Warning") else "unknown"
        elif key == "filter":
            self._filter = state == "on"
        elif key == "clogged":
            if state == "on":
                if self._clog_since is None:
                    changed = epoch(full.get("last_changed"))
                    self._clog_since = min(changed, now) if changed is not None else now
            else:
                self._clog_since = None
        elif key == "door":
            if state == "off":
                changed = epoch(full.get("last_changed"))
                self._last_door_close = min(changed, now) if changed is not None else now
        elif key == "visitor":
            if state == "cleaner":
                start = epoch(attrs.get("since"))
                if self._visit is None:
                    self._visit = {"start": min(start, now) if start is not None else now,
                                   "who": str(attrs.get("who") or ""),
                                   "suction_seconds": suction(), "trigger_starts": 0,
                                   "sessions": [], "coverage": "complete"}
                if self._session is not None:
                    self._session["visit"] = "cleaner"
                    self._include_session()
            elif state in ("none", "guest") and self._visit is not None:
                self._end_visit(now, attrs.get("end_reason") or "unknown")
        self._advance(now)
        self._mark_semantic(now)

    def _include_session(self):
        if self._session is not None and self._visit is not None:
            start = stamp(self._session["start"])
            if start not in self._visit["sessions"]:
                self._visit["sessions"].append(start)

    def _close(self, now):
        record = session_record(self._session, now)
        if not any(item.get("start") == record["start"] for item in self._data["cycles"]):
            self._data["cycles"].append(record)
        self._last_closed_at = now
        self._session = None
        self._idle_since = self._mounted_since = None
        self._mark_semantic(now)

    def _end_visit(self, now, why):
        visit, self._visit = self._visit, None
        records = [item for item in self._data["cycles"] if item.get("start") in visit["sessions"]]
        if self._session is not None and stamp(self._session["start"]) in visit["sessions"]:
            records.append(session_record(self._session, now))
        docked = [item["docked_at"] for item in records if item.get("docked_at")]
        seconds = visit["suction_seconds"]
        record = {"ts": stamp(now), "start": stamp(visit["start"]), "end": stamp(now),
                  "who": visit.get("who", ""), "end_reason": why, "sessions": visit["sessions"], "suction_seconds": seconds,
                  "duration_min": sum(seconds.values()) / 60, "trigger_starts": visit["trigger_starts"],
                  "docked_at": max(docked, key=epoch) if docked else "",
                  "door_closed_at": stamp(self._last_door_close), "coverage": visit["coverage"]}
        duplicate = any(item.get("start") == record["start"] for item in self._data["visits"])
        if not duplicate:
            self._data["visits"].append(record)
        if used(record) and self._status not in MOUNTED:
            self._candidate = record
        if not duplicate:
            self._save()
            if used(record):
                try:
                    self.fire_event("house_events_report", text=receipt(record), icon="mdi:vacuum", audience="admin",
                                    by=record["who"])
                except Exception as e:
                    self.log(f"StickVacuum: visit receipt failed: {e!r}", level="WARNING")

    # ---------- start-up recovery ----------

    def _recover(self, now):
        cycles, visits = self._data["cycles"], self._data["visits"]
        ends = [epoch(item.get("start")) for item in cycles[-1:]] + [epoch(item.get("end")) for item in visits[-1:]]
        start = min((value for value in ends if value is not None), default=now - HISTORY_S)
        checkpoint = copy.deepcopy(self._data.get("state") or {})
        self._candidate = copy.deepcopy(checkpoint.get("candidate"))
        for name in ("session", "visit"):
            saved = checkpoint.get(name)
            if isinstance(saved, dict) and isinstance(saved.get("start"), (int, float)):
                start = min(start, saved["start"])
        for record in reversed(cycles):
            began, ended = epoch(record.get("start")), epoch(record.get("end") or record.get("ts"))
            if began is not None and ended is not None and began < start < ended:
                start = began
        current = {key: self.get_state(entity, attribute="all") for key, entity in self.sources.items()}
        full = current.get("visitor")
        if isinstance(full, dict) and full.get("state") == "cleaner":
            since = epoch((full.get("attributes") or {}).get("since"))
            if since is not None:
                start = min(start, since)
        try:
            visitor = self._history("visitor", start, now)
            for full in visitor:
                if full.get("state") == "cleaner":
                    since = epoch((full.get("attributes") or {}).get("since"))
                    if since is not None:
                        start = min(start, since)
            for record in reversed(cycles):
                began, ended = epoch(record.get("start")), epoch(record.get("end") or record.get("ts"))
                if began is not None and ended is not None and began < start < ended:
                    start = began
            histories = {key: self._history(key, start, now) for key in self.sources}
            if checkpoint.get("session") and not histories["status"]:
                raise ValueError("no status history for the open session")
            points = []
            order = ("visitor", "power_mode", "battery", "filter", "clogged", "door", "status")
            for priority, key in enumerate(order):
                for full in histories[key]:
                    when = epoch(full.get("last_updated") or full.get("last_changed"))
                    if when is not None and when <= now:
                        points.append((max(start, when), priority, key, full))
            for when, _, key, full in sorted(points, key=lambda point: point[:2]):
                self._observe(when, key, full)
        except Exception as e:
            self.log(f"StickVacuum: history unreadable, using checkpoint: {e!r}", level="WARNING")
            for name in ("session", "visit", "candidate", "last_door_close", "last_closed_at", "last_mount", "clog_since"):
                if name in checkpoint:
                    setattr(self, "_" + name, copy.deepcopy(checkpoint[name]))
            self._time = now
            self._status = None
            self._known_status = checkpoint.get("known_status")
            if self._session is not None:
                self._session["coverage"] = "partial"
            if self._visit is not None:
                self._visit["coverage"] = "partial"
        if cycles:
            ends = [epoch(item.get("end") or item.get("ts")) for item in cycles]
            self._last_closed_at = max((value for value in ends if value is not None), default=None)
        for key in ("visitor", "power_mode", "battery", "filter", "clogged", "door", "status"):
            self._observe(now, key, current[key])

    def _history(self, key, start, now):
        raw = self.get_history(entity_id=self.sources[key], start_time=datetime.fromtimestamp(start, timezone.utc),
                               end_time=datetime.fromtimestamp(now, timezone.utc), minimal_response=False,
                               no_attributes=key != "visitor")
        return [full for full in whist.flatten_history(raw, self.sources[key]) if isinstance(full, dict)]

    # ---------- persistence and semantic entities ----------

    def _save(self):
        self._data["state"] = {key: copy.deepcopy(getattr(self, "_" + key)) for key in
                               ("session", "visit", "candidate", "last_door_close", "last_closed_at",
                                "last_mount", "known_status", "clog_since")}
        if not self._writable:
            return
        snapshot = json.dumps(self._data, sort_keys=True)
        if snapshot == self._saved:
            return
        try:
            feedback_io.write_json(str(self.feedback_file), self._data)
            self._saved = snapshot
        except OSError as e:
            self.log(f"StickVacuum: feedback write failed: {e!r}", level="WARNING")

    def _problems(self, now):
        problems = []
        if self._clog_since is not None and now - self._clog_since >= CLOG_S:
            problems.append("clogged")
        if self._filter:
            problems.append("filter")
        if self._candidate is not None and self._status == "Ready":
            if now - epoch(self._candidate["end"]) >= NOT_CHARGING_S:
                problems.append("not_charging")
        return problems

    def _state(self):
        if self._status == "Cleaning" or self._status == "Ready" and self._session is not None:
            state, reason = "in_use", "The stick vacuum is in an open cleaning session."
        elif self._status == "Ready":
            state, reason = "off_mount", "The stick vacuum is off the mount with the motor off."
        elif self._status == "Charging":
            state, reason = "charging", "The stick vacuum is charging on the mount."
        elif self._status == "Fully charged":
            state, reason = "charged", "The stick vacuum is fully charged on the mount."
        else:
            state, reason = "unknown", "The stick vacuum status is unknown."
        return state, reason

    def _mark_semantic(self, now):
        state, _ = self._state()
        if state != self._semantic:
            self._semantic, self._since = state, now

    def _snapshot(self, now):
        self._mark_semantic(now)
        state, reason = self._state()
        last = self._data["cycles"][-1] if self._data["cycles"] else None
        visit = self._data["visits"][-1] if self._data["visits"] else None
        problems = self._problems(now)
        coverage = self._session["coverage"] if self._session else last["coverage"] if last else "unknown"
        candidate = self._candidate or {}
        attrs = {"friendly_name": "Stick vacuum", "icon": "mdi:vacuum", "reason": reason,
                 "since": stamp(self._since), "power_mode": self._power_mode, "battery": self._battery,
                 "coverage": coverage, "problems": ",".join(problems),
                 "last_session": f"{last['duration_min']:.0f} min, {last['trigger_starts']} starts ({last['visit']})" if last else "",
                 "last_visit": receipt(visit) if visit and used(visit) else "",
                 "session_open": "yes" if self._session else "no",
                 "session_started": stamp(self._session["start"]) if self._session else "",
                 "session_closed_at": stamp(self._last_closed_at), "clogged_since": stamp(self._clog_since),
                 "not_charging_since": candidate.get("end", "") if "not_charging" in problems else "",
                 "not_charging_push": "yes" if candidate.get("end_reason") == "quiet" else "no"}
        return state, attrs

    def _refresh(self, now):
        self._save()
        state, attrs = self._snapshot(now)
        in_use = "unknown" if self._status is None else "on" if state == "in_use" else "off"
        key = (state, tuple(attrs.items()), in_use)
        if key == self._published_key and now - self._published_at < REPUBLISH_S:
            return
        try:
            self.set_state(self.entity, state=state, attributes=attrs, replace=True)
            self.set_state(self.in_use_entity, state=in_use, replace=True,
                           attributes={"friendly_name": "Stick vacuum in use", "icon": "mdi:vacuum",
                                       "reason": attrs["reason"]})
        except Exception as e:
            self.log(f"StickVacuum: publish failed: {e!r}", level="WARNING")
            return
        self._published_key, self._published_at = key, now

    def _now(self):
        return self.get_now().timestamp()
