"""
BleLocator - MEASUREMENT ONLY: which room is a tracked Bluetooth device in, from what every scanner
hears of it, by the model scripts/ble_locator_train.py fits on the beacon stuck on Rober2. Per device
it publishes sensor.<name>_ble_room and logs the answer next to Bermuda's own area, to judge the
model before anything relies on it. Nothing consumes these sensors yet, and the app decides nothing
and sends no notification.

TICK. Every interval_s the app calls bermuda.dump_devices once for all devices (the addresses
separated by spaces, which is how the service reads them), cut off after CALL_TIMEOUT_S. A tick that
finds the previous one still running is skipped. A dump that fails, times out or holds none of the
devices skips the tick: nothing is published or logged and no state changes. A device missing from a
dump that holds others has no readings that tick, warned about once until it is back.

READINGS. A device's fresh readings are its adverts from scanners the model file lists whose stamp is
0 to fresh_s seconds old on HA's time.monotonic() clock (HA and AppDaemon share the host, so it is
AppDaemon's too) and whose rssi_distance is a number. Bermuda blanks rssi_distance 30 s after a
scanner's last advert, as the recorded distance sensors the model was fitted on go unknown, so a
scanner is fresh for 30 s whatever fresh_s says. One reading per scanner, its newest advert, as
dB = -55 - 30 * log10(metres), at least -100 (the model file's constants).

ANSWER. The model answers for a device when the model file is usable, Bermuda's own area (the state
of bermuda_area) is one of the model's rooms or one of unreachable_areas (rooms nobody can be in,
which Bermuda still names when their scanner is the nearest) and at least min_scanners readings are
fresh. In every
other case the answer is Bermuda's area as it is, source "bermuda": no model file, one that cannot be
read or was trained more than max_model_age_days ago (each warned about once until the model is
usable), an area the model has no room for (Landing, unknown, not_home, any other), too few readings.

FILTER. Per device a belief over the model's rooms starts uniform. Each model answer updates it once:
it stays in its room with probability p_stay ** (dt / step), dt the seconds since the last update,
and leaves for the other rooms in equal parts; then each room's likelihood of the readings (the sum
over the fresh readings of the Gaussian log density of the reading minus the device offset under the
room's mean and sd for that scanner, mean -95 and sd 10 where the room has none) is made into
probabilities, floored, raised to the emission power and multiplied in. An update more than fresh_s
after the last one, or after the model's rooms changed, starts from uniform again. The room is the
most likely one, confidence its probability, runner_up the next room and runner_up_p its probability.

OFFSET. A phone does not transmit like the beacon, so a per-device offset in dB is subtracted from its
readings. Each model answer with at least OFFSET_MIN_SCANNERS fresh readings gives the offset that
fits them best to the answered room (mean of reading minus the room's mean, weighted by 1 / sd^2).
The offset is the average of those: the mean so far until it spans offset_tau_s, then an exponential
average with that time constant. Offsets are saved to state_file every minute when they changed and
at stop, and read back at start.

PUBLISH. sensor.<name>_ble_room is the room, or Bermuda's area as it is (unknown when it has none),
with attributes confidence, runner_up, runner_up_p (null on a Bermuda answer), source (model or
bermuda), fresh_scanners, offset_db and model_date (null without a usable model).

LOG. log_file gets one JSON line {t, device, room, confidence, runner_up, source, bermuda_area,
fresh_scanners, offset_db} whenever a device's room changes and once a minute per device, appended on
a worker thread; nothing prunes it. model_file is read again when its mtime changes.
"""

import asyncio
import json
import math
import os
import time
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

DUMP_SERVICE = "bermuda/dump_devices"
CALL_TIMEOUT_S = 10
MODEL_VERSION = 1
STATE_VERSION = 1
OFFSET_MIN_SCANNERS = 4
WRITE_EVERY_S = 60
DAY_S = 86400
CONSTANTS = ("ref_power_db", "db_per_decade", "floor_db", "unseen_mean_db", "unseen_sd_db", "p_stay",
             "p_stay_step_s", "emission_power", "emission_floor")


def _number(value):
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
    return value if ok else None


def _finite(value, what):
    number = _number(value)
    if number is None:
        raise ValueError(f"{what} is {value!r}, not a number")
    return float(number)


# ---------- the model ----------

class Model:
    """The model file's content: rooms, scanner addresses, per room and scanner (mean, sd) and the constants."""

    def __init__(self, doc):
        try:
            self._parse(doc)
        except (KeyError, TypeError, AttributeError, IndexError) as e:
            raise ValueError(f"malformed: {e!r}") from e

    def _parse(self, doc):
        if doc["version"] != MODEL_VERSION:
            raise ValueError(f"version {doc['version']!r}, not {MODEL_VERSION}")
        self.c = {key: _finite(doc["constants"][key], f"constant {key}") for key in CONSTANTS}
        c = self.c
        if not (0 < c["p_stay"] < 1 and c["p_stay_step_s"] > 0 and c["db_per_decade"] > 0
                and c["emission_power"] > 0 and c["emission_floor"] > 0 and c["unseen_sd_db"] > 0):
            raise ValueError("constants out of range")
        self.rooms = [str(room) for room in doc["rooms"]]
        if len(self.rooms) < 2 or len(set(self.rooms)) != len(self.rooms):
            raise ValueError("rooms must be two or more, each once")
        self.scanners = {str(address).lower() for address in doc["scanners"]}
        self.stats = {}
        for room in self.rooms:
            self.stats[room] = {}
            for address, (mean, sd, _) in doc["stats"][room].items():
                if _finite(sd, "sd") <= 0:
                    raise ValueError(f"sd of {room} at {address} is not positive")
                self.stats[room][str(address).lower()] = (_finite(mean, "mean"), float(sd))
        stamp = str(doc["trained_at"])
        self.trained_at = datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp()
        self.date = stamp[:10]

    def to_db(self, metres):
        return max(self.c["ref_power_db"] - self.c["db_per_decade"] * math.log10(metres), self.c["floor_db"])

    def log_likelihoods(self, readings, offset_db=0.0):
        """{room: log-likelihood} of `readings` ({scanner address: dB}) after subtracting offset_db. A scanner
        with no reading adds nothing to any room."""
        unseen = (self.c["unseen_mean_db"], self.c["unseen_sd_db"])
        out = {}
        for room in self.rooms:
            per, total = self.stats[room], 0.0
            for address, db in readings.items():
                mean, sd = per.get(address, unseen)
                z = (db - offset_db - mean) / sd
                total -= 0.5 * z * z + math.log(sd)
            out[room] = total
        return out

    def posterior(self, lls):
        """The likelihoods as probabilities over the rooms, in their order, under a uniform prior."""
        top = max(lls.values())
        weights = [math.exp(lls[room] - top) for room in self.rooms]
        total = sum(weights)
        return [w / total for w in weights]

    def advance(self, belief, probs, dt):
        """The belief after dt seconds and one reading whose posterior is `probs`."""
        c = self.c
        stay = c["p_stay"] ** (dt / c["p_stay_step_s"])
        move = (1.0 - stay) / (len(belief) - 1)
        new = [(b * stay + (1.0 - b) * move) * max(p, c["emission_floor"]) ** c["emission_power"]
               for b, p in zip(belief, probs)]
        total = sum(new)
        return [x / total for x in new]

    def best_offset(self, room, readings):
        """The offset in dB that fits `readings` best to `room`: the 1 / sd^2 weighted mean of reading minus mean."""
        unseen = (self.c["unseen_mean_db"], self.c["unseen_sd_db"])
        weighted = weights = 0.0
        for address, db in readings.items():
            mean, sd = self.stats[room].get(address, unseen)
            weighted += (db - mean) / (sd * sd)
            weights += 1.0 / (sd * sd)
        return weighted / weights


def read_model(path, stamp):
    """None when the file is unchanged since `stamp`, its mtime in ns as read before. Else (stamp, model,
    problem): the file's mtime, and its model or None and what is wrong with the file."""
    try:
        mtime = os.stat(path).st_mtime_ns
    except OSError as e:
        what = "does not exist" if isinstance(e, FileNotFoundError) else f"cannot be read: {e!a}"
        return None, None, f"model file {path} {what}"
    if mtime == stamp:
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return mtime, Model(json.load(f)), None
    except (OSError, ValueError) as e:
        return mtime, None, f"model file {path} is unusable: {e}"


def devices_from(resp):
    """{address in lower case: device} out of AppDaemon's envelope for bermuda.dump_devices, {} when it
    holds none. Only dicts with an `adverts` dict are devices: an error envelope holds dicts that are not."""
    node = resp
    for key in ("result", "response"):
        if isinstance(node, dict) and isinstance(node.get(key), dict):
            node = node[key]
    if not isinstance(node, dict):
        return {}
    return {str(address).lower(): device for address, device in node.items()
            if isinstance(device, dict) and isinstance(device.get("adverts"), dict)}


def readings_from(device, model, now, fresh_s):
    """{scanner address: dB} of a device's fresh readings. `now` is the monotonic clock. Only scanners the
    model knows count, each by its newest advert, and only when that advert is 0 to fresh_s seconds old
    and carries a distance."""
    newest = {}
    for advert in (device.get("adverts") or {}).values() if device else ():
        if not isinstance(advert, dict):
            continue
        scanner = str(advert.get("scanner_address") or "").lower()
        stamp, metres = _number(advert.get("stamp")), _number(advert.get("rssi_distance"))
        if scanner not in model.scanners or stamp is None or metres is None or metres <= 0:
            continue
        if 0 <= now - stamp <= fresh_s and stamp > newest.get(scanner, (-math.inf, 0))[0]:
            newest[scanner] = (stamp, metres)
    return {scanner: model.to_db(metres) for scanner, (_, metres) in newest.items()}


def read_state(path):
    """{address: (offset dB, seconds of evidence)} out of the state file, {} when it holds none."""
    try:
        with open(path, encoding="utf-8") as f:
            doc = json.load(f)
        if doc["version"] != STATE_VERSION:
            return {}
        return {str(address).lower(): (_finite(v["db"], "db"), max(_finite(v["s"], "s"), 0.0))
                for address, v in doc["offsets"].items()}
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return {}


def write_json(path, doc):
    tmp = f"{path}.tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(doc, f, separators=(",", ":"))
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def append_lines(path, lines):
    data = "".join(json.dumps(line, separators=(",", ":"), default=str) + "\n" for line in lines)
    with open(path, "a", encoding="utf-8") as f:
        f.write(data)


class Track:
    """What the app keeps per device."""

    def __init__(self, name, address, area_entity, offset, offset_s):
        self.name, self.address, self.area_entity = name, address, area_entity
        self.entity = f"sensor.{name}_ble_room"
        self.offset, self.offset_s, self.offset_at = offset, offset_s, None
        self.rooms = self.belief = self.belief_at = None
        self.room = self.logged_at = None


class BleLocator(hass.Hass):
    def initialize(self):
        a = self.args
        here = Path(__file__).resolve().parent
        self.model_file = str(a.get("model_file") or here / "ble_locator_model.json")
        self.log_file = str(a.get("log_file") or here / "ble_locator_log.jsonl")
        self.state_file = str(a.get("state_file") or here / "ble_locator_state.json")
        self.interval_s = float(a.get("interval_s", 5))
        self.fresh_s = float(a.get("fresh_s", 120))
        self.min_scanners = int(a.get("min_scanners", 3))
        self.unreachable_areas = set(a.get("unreachable_areas") or ())
        self.offset_tau_s = float(a.get("offset_tau_s", 3600))
        self.max_model_age_days = float(a.get("max_model_age_days", 14))
        if min(self.interval_s, self.fresh_s, self.offset_tau_s, self.max_model_age_days) <= 0 or self.min_scanners < 1:
            raise ValueError("BleLocator: interval_s, fresh_s, offset_tau_s and max_model_age_days must be positive "
                             "and min_scanners at least 1")
        listed = a.get("devices") or []
        if not listed or not all(isinstance(d, dict) and d.get("name") and d.get("address") and d.get("bermuda_area")
                                 for d in listed):
            raise ValueError("BleLocator: devices must list at least one {name, address, bermuda_area}")
        saved = read_state(self.state_file)
        self._tracks = [Track(str(d["name"]), str(d["address"]).lower(), str(d["bermuda_area"]),
                              *saved.get(str(d["address"]).lower(), (0.0, 0.0))) for d in listed]
        if (len({t.address for t in self._tracks}) != len(self._tracks)
                or len({t.name for t in self._tracks}) != len(self._tracks)):
            raise ValueError("BleLocator: every device needs its own name and address")
        self._addresses = " ".join(track.address for track in self._tracks)

        self._model, self._model_stamp, self._active = None, None, None
        self._problems, self._missing = {}, set()
        self._busy = False
        self._saved, self._saved_at = self._offsets(), None

        self.run_every(self._on_tick, f"now+{self.interval_s:g}", self.interval_s)
        self.log(f"BleLocator initialized: shadow mode, {len(self._tracks)} device(s) every {self.interval_s:g} s -> "
                 f"sensor.<name>_ble_room, {self.log_file}", level="INFO")

    def terminate(self):
        try:
            write_json(self.state_file, self._state_document())
        except OSError as e:
            self.log(f"BleLocator: offsets not saved to {self.state_file}: {e!a}", level="WARNING")

    def _on_tick(self, kwargs):
        if self._busy:
            return
        areas = {track.name: self.get_state(track.area_entity) for track in self._tracks}
        self._busy = True
        try:
            self.create_task(self._step(areas))
        except Exception:
            self._busy = False
            raise

    # ---------- one tick ----------

    async def _step(self, areas):
        try:
            await self._refresh_model()
            devices = await self._dump()
            if devices is None:
                return
            now, epoch = self._monotonic(), self._epoch()
            self._check_model(epoch)
            lines = []
            for track in self._tracks:
                device = devices.get(track.address)
                if device is None and track.address not in self._missing:
                    self._missing.add(track.address)
                    self.log(f"BleLocator: bermuda.dump_devices holds no device {track.address} ({track.name})",
                             level="WARNING")
                elif device is not None:
                    self._missing.discard(track.address)
                area = areas[track.name]
                answer = self._answer(track, device, area, now)
                changed = answer["room"] != track.room
                track.room = answer["room"]
                await self._publish(track, answer)
                if changed or track.logged_at is None or epoch - track.logged_at >= WRITE_EVERY_S:
                    track.logged_at = epoch
                    lines.append({"t": round(epoch, 1), "device": track.name, "room": answer["room"],
                                  "confidence": answer["confidence"], "runner_up": answer["runner_up"],
                                  "source": answer["source"], "bermuda_area": area,
                                  "fresh_scanners": answer["fresh"], "offset_db": answer["offset"]})
            if lines:
                await self._write("log", append_lines, self.log_file, lines)
            state = self._offsets()
            if state != self._saved and (self._saved_at is None or now - self._saved_at >= WRITE_EVERY_S):
                if await self._write("state", write_json, self.state_file, self._state_document()):
                    self._saved, self._saved_at = state, now
        except Exception as e:
            self._problem("tick", f"tick failed: {e!a}")
        else:
            self._recovered("tick", "ticks run again")
        finally:
            self._busy = False

    async def _dump(self):
        try:
            resp = await asyncio.wait_for(
                self.call_service(DUMP_SERVICE, return_response=True, suppress_log_messages=True,
                                  addresses=self._addresses, redact=False), timeout=CALL_TIMEOUT_S)
        except Exception as e:
            self._problem("dump", f"{DUMP_SERVICE.replace('/', '.')} failed, tick skipped: {e!a}")
            return None
        devices = devices_from(resp)
        if not any(track.address in devices for track in self._tracks):
            self._problem("dump", f"{DUMP_SERVICE.replace('/', '.')} returned none of the devices, tick skipped: "
                                  f"{str(resp)[:100]!a}")
            return None
        self._recovered("dump", "bermuda.dump_devices answers again")
        return devices

    def _answer(self, track, device, area, now):
        """The device's answer this tick: {room, confidence, runner_up, runner_up_p, source, fresh, offset}."""
        model = self._active
        readings = readings_from(device, model, now, self.fresh_s) if model else {}
        base = {"confidence": None, "runner_up": None, "runner_up_p": None, "source": "bermuda",
                "fresh": len(readings), "offset": round(track.offset, 1)}
        modelled = area in model.rooms or area in self.unreachable_areas if model else False
        if not modelled or len(readings) < self.min_scanners:
            return {**base, "room": area if isinstance(area, str) and area else "unknown"}

        if track.belief is None or track.rooms != model.rooms or now - track.belief_at > self.fresh_s:
            track.belief, dt = [1.0 / len(model.rooms)] * len(model.rooms), model.c["p_stay_step_s"]
        else:
            dt = now - track.belief_at
        probs = model.posterior(model.log_likelihoods(readings, track.offset))
        track.belief = model.advance(track.belief, probs, dt)
        track.rooms, track.belief_at = model.rooms, now
        first, second = sorted(range(len(model.rooms)), key=lambda i: -track.belief[i])[:2]
        if len(readings) >= OFFSET_MIN_SCANNERS:
            self._learn(track, model.best_offset(model.rooms[first], readings), now)
        return {**base, "room": model.rooms[first], "confidence": round(track.belief[first], 3),
                "runner_up": model.rooms[second], "runner_up_p": round(track.belief[second], 3),
                "source": "model", "offset": round(track.offset, 1)}

    def _learn(self, track, observed, now):
        dt = self.interval_s if track.offset_at is None else min(now - track.offset_at, self.fresh_s)
        track.offset_at = now
        if dt <= 0:
            return
        track.offset_s = min(track.offset_s + dt, self.offset_tau_s)
        track.offset += (observed - track.offset) * min(dt / track.offset_s, 1.0)

    async def _publish(self, track, answer):
        name = track.name.replace("_", " ").capitalize()
        attributes = {"friendly_name": f"{name} BLE room", "icon": "mdi:map-marker-radius",
                      "confidence": answer["confidence"], "runner_up": answer["runner_up"],
                      "runner_up_p": answer["runner_up_p"], "source": answer["source"],
                      "fresh_scanners": answer["fresh"], "offset_db": answer["offset"],
                      "model_date": self._active.date if self._active else None}
        try:
            await self.set_state(track.entity, state=answer["room"], attributes=attributes, replace=True)
        except Exception as e:
            self._problem("publish", f"could not publish {track.entity}: {e!a}")
        else:
            self._recovered("publish", "publishing again")

    # ---------- the model file and the state file ----------

    async def _refresh_model(self):
        read = await self.run_in_executor(read_model, self.model_file, self._model_stamp)
        if read is None:
            return
        self._model_stamp, self._model, problem = read
        if self._model is not None:
            self.log(f"BleLocator: model of {self._model.date} loaded from {self.model_file}: "
                     f"{len(self._model.rooms)} rooms, {len(self._model.scanners)} scanners", level="INFO")
        elif problem:
            self._problem("model", problem)

    def _check_model(self, epoch):
        """Sets the model the tick answers with: the loaded one unless it is too old."""
        model = self._model
        if model is not None and epoch - model.trained_at > self.max_model_age_days * DAY_S:
            self._problem("model", f"the model of {model.date} is older than {self.max_model_age_days:g} days, "
                                   "answering with Bermuda's area until it is retrained")
            model = None
        elif model is not None:
            self._recovered("model", "the model is usable")
        self._active = model

    def _offsets(self):
        return {track.address: (round(track.offset, 3), round(track.offset_s, 1)) for track in self._tracks}

    def _state_document(self):
        return {"version": STATE_VERSION, "offsets": {address: {"db": db, "s": seconds}
                                                      for address, (db, seconds) in self._offsets().items()}}

    async def _write(self, kind, writer, path, content):
        try:
            await self.run_in_executor(writer, path, content)
        except OSError as e:
            self._problem(kind, f"could not write {path}: {e!a}")
            return False
        self._recovered(kind, f"writing {path} again")
        return True

    # ---------- misc ----------

    def _problem(self, kind, text):
        """Logs `text` at WARNING unless it is the same problem already logged for `kind`."""
        if self._problems.get(kind) != text:
            self._problems[kind] = text
            self.log(f"BleLocator: {text}", level="WARNING")

    def _recovered(self, kind, text):
        if self._problems.pop(kind, None) is not None:
            self.log(f"BleLocator: {text}", level="INFO")

    def _epoch(self):
        return time.time()

    def _monotonic(self):
        """HA's time.monotonic() clock, the one the dump's stamps are on; the host is shared, so this process
        reads the same clock."""
        return time.monotonic()
