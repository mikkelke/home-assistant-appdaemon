"""
BeaconPositionLog - MEASUREMENT ONLY: where is the robot vacuum on the floor, and what does every
Bluetooth scanner hear of the beacon stuck on it? While the robot drives, samples of both go to
log_file, to train an RSSI-fingerprint locator offline (position and room from the RSSI vector).
It decides nothing, creates no entity and sends no notification.

WINDOW. Opens when `vacuum` is in one of active_states when the app starts or changes into one,
and closes tail_s after it leaves them. Going back into one inside that tail keeps the window and
cancels the close. Nothing is sampled or written while no window is open.

SAMPLE. Every interval_s from the open (the first at once) the app reads the robot's position
(roborock.get_vacuum_current_position for `vacuum`) and the beacon's adverts (bermuda.dump_devices
for `beacon`, not redacted) at the same time, each call cut off after CALL_TIMEOUT_S. A tick that
finds the previous sample still running is skipped. A sample appends one JSON line:
{t, x, y, room, vacuum, bermuda_area, adv}
  t             epoch seconds when the sample started, to 0.1 s
  x, y          the robot's position in Roborock map units (mm), null when the call failed or
                answered no usable position
  room          the state of `room` (sensor.rober2_current_room), null without one
  vacuum        the state of `vacuum`
  bermuda_area  the area_name Bermuda itself chose for the beacon, null without a dump or an area
  adv           {scanner address: [[epoch, rssi], ...]}, oldest first: the adverts that scanner
                heard since the last sample written, {} when none or when the dump failed

ADVERTS. Bermuda keeps each scanner's last 10 adverts (hist_stamp, hist_rssi, newest first) on HA's
time.monotonic() clock. HA and AppDaemon share the host, so that clock is AppDaemon's too, and a
stamp becomes epoch as now_epoch - (now_monotonic - stamp), to 0.1 s. A scanner's advert is
written once: only stamps above the newest already written for that scanner and above the window's
open count, so overlapping histories repeat nothing and a failed sample loses nothing, unless more
than 10 adverts of one scanner arrived since the last one written. If no stamp in a dump lies
within a day before now or SLACK_S after it, the stamps are not on this clock: adv stays empty and
that is warned about once per window.

LINES. A window also writes {t, event: start, scanners: {address: name}} before its first sample,
t being the open and scanners every scanner in that sample's dump ({} when it failed), and
{t, event: end} when it closes, also when the app stops. A window that closes before its first
sample reached the file leaves no line, and a sample still waiting for its calls when the window
closes is dropped. A failure is logged once per kind per window; the close message counts samples
without a position or a dump, skipped ticks and samples lost to write errors. Lines are appended
on worker threads under the app's lock, never on the event loop, and nothing prunes log_file.
"""

import asyncio
import collections
import itertools
import json
import math
import threading
import time
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

POSITION_SERVICE = "roborock/get_vacuum_current_position"
DUMP_SERVICE = "bermuda/dump_devices"
CALL_TIMEOUT_S = 10
SLACK_S = 5
DAY_S = 86400


def _number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def response_from(resp):
    """The service's own response out of AppDaemon's envelope. A refused or timed-out call has no
    result, so what comes back is the envelope itself; anything but a dict gives {}."""
    node = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    return node if isinstance(node, dict) else {}


def position_from(resp, entity):
    """(x, y) out of the position service's answer, (None, None) when it holds none for entity."""
    position = response_from(resp).get(entity)
    if isinstance(position, dict):
        x, y = _number(position.get("x")), _number(position.get("y"))
        if x is not None and y is not None:
            return x, y
    return None, None


def beacon_from(resp, address):
    """The beacon's device dict out of bermuda.dump_devices' answer, None when it holds none.
    `address` is lower case."""
    for key, device in response_from(resp).items():
        if str(key).lower() == address and isinstance(device, dict):
            return device
    return None


def read_adverts(device, now, last, since):
    """(fresh, names, mismatch) for one dump of the beacon. `now` is the monotonic clock, `last` the
    newest stamp already written per scanner address and `since` the window's open on that clock.
    fresh maps a scanner address to its new (stamp, rssi) pairs, oldest first; names maps every
    scanner in the dump to its name. mismatch is true when no stamp is on this clock, and fresh is
    then empty."""
    adverts = device.get("adverts") if device else None
    names, heard = {}, {}
    for key, advert in (adverts.items() if isinstance(adverts, dict) else ()):
        if not isinstance(advert, dict):
            continue
        scanner = str(advert.get("scanner_address") or str(key).rpartition("__")[2]).lower()
        names[scanner] = advert.get("name")
        stamps, rssis = advert.get("hist_stamp"), advert.get("hist_rssi")
        if isinstance(stamps, list) and isinstance(rssis, list):
            heard[scanner] = sorted((s, r) for s, r in zip(stamps, rssis)
                                    if _number(s) is not None and _number(r) is not None)
    stamps = [s for pairs in heard.values() for s, _ in pairs]
    mismatch = bool(stamps) and not any(-SLACK_S <= now - s <= DAY_S for s in stamps)
    fresh = {}
    for scanner, pairs in ({} if mismatch else heard).items():
        new = [(s, r) for s, r in pairs if s > last.get(scanner, since)]
        if new:
            fresh[scanner] = new
    return fresh, names, mismatch


def failure_text(service, answer, missing):
    """The warning for a service call whose answer held no `missing`."""
    name = service.replace("/", ".")
    if isinstance(answer, BaseException):
        return f"{name} failed: {answer!a}"
    return f"{name} returned no {missing}: {str(answer)[:100]!a}"


class BeaconPositionLog(hass.Hass):
    def initialize(self):
        # Guards _win. A sample is recorded and written on an executor thread that holds it, and
        # AppDaemon calls such as get_state, run_every and cancel_timer wait for the event loop,
        # so none may be made while this is held.
        self._lock = threading.RLock()
        self._win = None
        self._ids = itertools.count(1)
        a = self.args
        self.vacuum_entity = a.get("vacuum", "vacuum.rober2")
        self.room_entity = a.get("room", "sensor.rober2_current_room")
        self.beacon = str(a.get("beacon", "7c:d9:f4:14:23:77")).lower()
        states = a.get("active_states") or ["cleaning", "returning"]
        self.active_states = frozenset([states] if isinstance(states, str) else states)
        self.tail_s = float(a.get("tail_s", 60))
        self.interval_s = float(a.get("interval_s", 5))
        self.log_file = Path(a.get("log_file") or Path(__file__).resolve().parent / "beacon_position_log.jsonl")
        if self.interval_s <= 0 or self.tail_s < 0:
            raise ValueError("BeaconPositionLog: interval_s must be positive and tail_s not negative")

        self.listen_state(self._on_vacuum, self.vacuum_entity)
        state = self.get_state(self.vacuum_entity)
        if state in self.active_states:
            self._enter(state)
        self.log(f"BeaconPositionLog initialized: samples {self.beacon} and the position of {self.vacuum_entity} "
                 f"every {self.interval_s:g} s while it is {'/'.join(sorted(self.active_states))} and "
                 f"{self.tail_s:g} s after -> {self.log_file}", level="INFO")

    def terminate(self):
        self._close()

    def _on_vacuum(self, entity, attribute, old, new, kwargs):
        if new in self.active_states:
            self._enter(new)
        else:
            self._leave(new)

    def _enter(self, state):
        opened = resumed = False
        tail = None
        with self._lock:
            win = self._win
            if win is None:
                win = self._win = {"id": next(self._ids), "start": self._stamp(), "since": self._monotonic(),
                                   "tick": None, "leaving": None, "tail": None, "busy": False, "headed": False,
                                   "last": {}, "n": collections.Counter(), "warned": set()}
                opened = True
            else:
                resumed, tail = win["leaving"] is not None, win["tail"]
                win["leaving"] = win["tail"] = None
        if opened:
            tick = self.run_every(self._on_tick, "immediate", self.interval_s, wid=win["id"])
            with self._lock:
                win["tick"] = tick
            self.log(f"BeaconPositionLog window opened: {self.vacuum_entity} is {state}", level="INFO")
        elif resumed:
            self._cancel(tail)
            self.log(f"BeaconPositionLog window kept: {self.vacuum_entity} is {state} again", level="INFO")

    def _leave(self, state):
        with self._lock:
            win = self._win
            if win is None or win["leaving"] is not None:
                return
            win["leaving"] = token = next(self._ids)
            wid = win["id"]
        handle = self.run_in(self._on_tail, self.tail_s, wid=wid, token=token)
        with self._lock:
            pending = self._win is win and win["leaving"] == token
            if pending:
                win["tail"] = handle
        if not pending:
            self._cancel(handle)
        self.log(f"BeaconPositionLog: {self.vacuum_entity} is {state}, the window closes in {self.tail_s:g} s "
                 "unless it is active again", level="INFO")

    def _on_tail(self, kwargs):
        with self._lock:
            win = self._win
            if win is None or win["id"] != kwargs["wid"] or win["leaving"] != kwargs["token"]:
                return
            win["tail"] = None
        self._close(kwargs["wid"])

    def _on_tick(self, kwargs):
        with self._lock:
            win = self._win
            if win is None or win["id"] != kwargs["wid"]:
                return
            if win["busy"]:
                win["n"]["skipped"] += 1
                return
            win["busy"] = True
        try:
            t = self._stamp()
            vacuum = self.get_state(self.vacuum_entity)
            room = self.get_state(self.room_entity)
            self.create_task(self._sample(win, t, vacuum, room))
        except Exception:
            with self._lock:
                win["busy"] = False
            raise

    async def _sample(self, win, t, vacuum, room):
        try:
            position, dump = await asyncio.gather(
                self._call(POSITION_SERVICE, entity_id=self.vacuum_entity),
                self._call(DUMP_SERVICE, addresses=self.beacon.upper(), redact=False),
                return_exceptions=True)
            now_mono, now_epoch = self._monotonic(), self._epoch()
            await self.run_in_executor(self._record, win, t, vacuum, room, position, dump, now_mono, now_epoch)
        except Exception as e:
            self.log(f"BeaconPositionLog sample lost: {e!a}", level="WARNING")
        finally:
            with self._lock:
                win["busy"] = False

    async def _call(self, service, **data):
        return await asyncio.wait_for(
            self.call_service(service, return_response=True, suppress_log_messages=True, **data),
            timeout=CALL_TIMEOUT_S)

    def _record(self, win, t, vacuum, room, position, dump, now_mono, now_epoch):
        failed = {}
        x, y = (None, None) if isinstance(position, BaseException) else position_from(position, self.vacuum_entity)
        device = None if isinstance(dump, BaseException) else beacon_from(dump, self.beacon)
        if x is None:
            failed["position"] = failure_text(POSITION_SERVICE, position, "position")
        if device is None:
            failed["dump"] = failure_text(DUMP_SERVICE, dump, f"device for {self.beacon}")
        with self._lock:
            if self._win is not win:
                return
            fresh, names, mismatch = read_adverts(device, now_mono, win["last"], win["since"])
            if mismatch:
                failed["clock"] = "no advert stamp is on AppDaemon's monotonic clock, adverts are not logged"
            adv = {scanner: [[round(now_epoch - (now_mono - stamp), 1), rssi] for stamp, rssi in pairs]
                   for scanner, pairs in fresh.items()}
            lines = [{"t": t, "x": x, "y": y, "room": room, "vacuum": vacuum,
                      "bermuda_area": device.get("area_name") if device else None, "adv": adv}]
            if not win["headed"]:
                lines.insert(0, {"t": win["start"], "event": "start", "scanners": names})
            try:
                self._append(lines)
            except OSError as e:
                failed["write"] = f"could not write {self.log_file}: {e!a}"
                win["n"]["lost"] += 1
            else:
                win["headed"] = True
                win["last"].update({scanner: pairs[-1][0] for scanner, pairs in fresh.items()})
                win["n"]["samples"] += 1
                win["n"]["no_position"] += x is None
                win["n"]["no_dump"] += device is None
            reports = [message for kind, message in failed.items() if kind not in win["warned"]]
            win["warned"].update(failed)
        for message in reports:
            self.log(f"BeaconPositionLog: {message}", level="WARNING")

    def _close(self, wid=None):
        with self._lock:
            win = self._win
            if win is None or (wid is not None and win["id"] != wid):
                return
            self._win = None
            error = None
            if win["headed"]:
                try:
                    self._append([{"t": self._stamp(), "event": "end"}])
                except OSError as e:
                    error = e
            n = win["n"]
        self._cancel(win["tick"])
        self._cancel(win["tail"])
        if error is not None:
            self.log(f"BeaconPositionLog: end of window not written to {self.log_file}: {error!a}", level="WARNING")
        notes = [f"{n[key]} {label}" for key, label in (("no_position", "without a position"),
                                                        ("no_dump", "without a dump"),
                                                        ("skipped", "ticks skipped"),
                                                        ("lost", "lost to write errors")) if n[key]]
        detail = f" ({', '.join(notes)})" if notes else ""
        self.log(f"BeaconPositionLog window closed: {n['samples']} samples{detail} -> {self.log_file.name}",
                 level="INFO")

    def _append(self, lines):
        data = "".join(json.dumps(line, separators=(",", ":"), default=str) + "\n" for line in lines)
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(data)

    def _cancel(self, handle):
        if handle is not None:
            self.cancel_timer(handle, silent=True)

    def _epoch(self):
        return time.time()

    def _stamp(self):
        return round(self._epoch(), 1)

    def _monotonic(self):
        return time.monotonic()
