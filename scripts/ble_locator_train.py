"""Fits the room model that BleLocator runs on and scores it held-out. Reads the recorder database
read-only; the only write is the model file, and only on request.

SAMPLES. The beacon stuck on Rober2 (sensor.mp1_142377_distance_to_*) is heard by every scanner that
feeds Bermuda. Every 5 s of the windows (one or more, each a start and an end) in which the vacuum is
`cleaning` is a grid point carrying what each scanner heard: its last recorded distance, when that is a
number and under 120 s old, as dB = -55 - 30 * log10(metres), at least -100. The room is the robot's
(sensor.rober2_current_room, with Kitchen1 and Claudias room renamed to the area names), and a point is
a sample once the robot has been in that room for 30 s. A room is modelled when a scanner in its own
area heard the beacon in at least MIN_ROOM_SAMPLES of its samples: a room whose own scanner is deaf
would teach the model what a deaf scanner looks like, and Bermuda's own area stays the answer for it.
A scanner is the Bermuda scanner device named by the distance entity's unique id; its address is the
first of that MAC + 0..3 with a bluetooth connection in the device registry, and its area that
device's (else the base MAC's).
OFFSET_HISTORY replays the scanner offsets set in Bermuda after a distance was recorded, so every
sample reads as the offsets of today would have made it.

MODEL. Per (room, scanner) the median and the sample standard deviation of the dB readings, the
deviation at least 6. A pair read fewer than MIN_READINGS times in the room is not stored, like a pair
never read, and reads as mean -95, sd 10. A room's log-likelihood is the sum over the scanners heard of
the Gaussian log density of the reading minus the offset that fits the readings best to the room (the
1 / sd^2 weighted mean of reading minus the room's median over the scanners it has statistics for); a
scanner not heard adds nothing. The filter keeps a belief over the rooms: it stays put with probability
0.98 ** (dt / 5), leaves for the other rooms in equal parts, and each reading's posterior (uniform prior,
at least 1e-12) raised to the power 0.3 multiplies in. ble_locator.py does the same arithmetic on the
model file's `constants`, reading the stored median as the room's mean level.

TRAINING HYGIENE. Besides the median and the MIN_READINGS rule above, the fit learns only from the
samples that pass two rules, taken in time order. A sample that heard fewer than MIN_SCANNERS scanners
is left out: the model rests on how the levels differ between scanners, which one reading says nothing
about. At most SPOT_MAX_S seconds of samples are taken from any one spot, so that a stretch where the
robot stands still (at its dock, say) is not counted as many independent samples. A spot is a stretch
of consecutive grid points in one room whose readings all stay within SPOT_DB dB of the stretch's first
sample (the mean absolute difference over the scanners both heard); a gap, another room or a reading
further away starts a new spot. These rules shape the fit only, in every held-out refit as well: all
settled points of a modelled room are still scored.

HELD-OUT. Each 2 min block of the grid is left out in turn with 30 s on each side, the model is
refitted without it, and the block's samples are answered three ways: the nearest scanner (the
strongest reading, right when its area is the room), the model's most likely room, and the filter,
started anew for every block on the 30 s before it and read at every sample in it. The blocks are
laid out ALIGNMENTS times, each shifted by a share of a block, and the counts added up: where a
boundary falls in a short visit moves the filter's score by several points. The table gives the share
right per room, the mean over rooms and the share of all samples.

OUTPUT. The windows, the rooms and scanners and the table are printed. With --write PATH the model is
written there, through a temporary file and a rename, as {version, rooms, scanners: {address: {name,
area}}, stats: {room: {address: [median, sd, n]}}, constants, offset_history, trained_window (the first
start and the last end), trained_windows (each window), trained_at}; but only when the model beats the
nearest scanner by MIN_GAIN points over the rooms. If it does not, nothing is written and the exit
status is 1.

Run on the box:
  ssh 10.21.0.5 "sudo python3 - <UTC start> <UTC end> [<UTC start> <UTC end> ...] [--write PATH]" < scripts/ble_locator_train.py
"""

import argparse
import bisect
import collections
import json
import math
import os
import sqlite3
import statistics
import sys
from datetime import datetime, timezone

BEACON = "sensor.mp1_142377"
ROBOT_ROOM = {"Kitchen1": "Kitchen", "Claudias room": "Claudias Room"}
# (UTC time, distance entity suffix, dB added to that scanner's offset)
OFFSET_HISTORY = [("2026-11-16T15:20:00", "bathroom_display", 8)]
STORAGE = "/data/homeassistant/.storage/"
DATABASE = "/data/homeassistant/home-assistant_v2.db"
GRID_S = 5
SETTLE_S = 30
FRESH_S = 120
MIN_ROOM_SAMPLES = 24
MIN_READINGS = 3
MIN_SCANNERS = 2
SPOT_DB = 3.0
SPOT_MAX_S = 60
BLOCK_S, GAP_S, LEAD_S, ALIGNMENTS = 120, 30, 30, 4
MIN_GAIN = 10.0
MODEL_VERSION = 1
CONSTANTS = {
    "ref_power_db": -55.0,
    "db_per_decade": 30.0,
    "floor_db": -100.0,
    "sd_floor_db": 6.0,
    "unseen_mean_db": -95.0,
    "unseen_sd_db": 10.0,
    "p_stay": 0.98,
    "p_stay_step_s": 5.0,
    "emission_power": 0.3,
    "emission_floor": 1e-12,
}


def ts(iso):
    return datetime.fromisoformat(iso).replace(tzinfo=timezone.utc).timestamp()


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_db(metres, c=CONSTANTS):
    return max(c["ref_power_db"] - c["db_per_decade"] * math.log10(metres), c["floor_db"])


# ---------- the model: likelihood and filter ----------

def best_offset(per, readings):
    """The offset in dB that fits `readings` best to a room's statistics `per`: the 1 / sd^2 weighted mean of
    reading minus median over the scanners the room has statistics for, 0 when it has none of them."""
    weighted = weights = 0.0
    for address, db in readings.items():
        if address in per:
            mean, sd = per[address][:2]
            weighted += (db - mean) / (sd * sd)
            weights += 1.0 / (sd * sd)
    return weighted / weights if weights else 0.0


def log_likelihoods(stats, rooms, readings, c=CONSTANTS):
    """{room: log-likelihood} of `readings` ({address: dB}), each room scored after subtracting the offset that
    fits the readings best to it, so how loud a device is never decides the room. A room with no statistics
    at all is impossible (-inf)."""
    out = {}
    for room in rooms:
        per = stats.get(room)
        if not per:
            out[room] = -math.inf
            continue
        offset_db = best_offset(per, readings)
        total = 0.0
        for address, db in readings.items():
            mean, sd = per[address][:2] if address in per else (c["unseen_mean_db"], c["unseen_sd_db"])
            z = (db - offset_db - mean) / sd
            total -= 0.5 * z * z + math.log(sd)
        out[room] = total
    return out


def posterior(lls, rooms):
    """The likelihoods as probabilities over `rooms`, in that order, uniform prior."""
    top = max(lls[room] for room in rooms)
    if top == -math.inf:
        return [1.0 / len(rooms)] * len(rooms)
    weights = [math.exp(lls[room] - top) for room in rooms]
    total = sum(weights)
    return [w / total for w in weights]


def advance(belief, probs, dt, c=CONSTANTS):
    """The belief after dt seconds and one reading whose posterior is `probs`."""
    stay = c["p_stay"] ** (dt / c["p_stay_step_s"])
    move = (1.0 - stay) / (len(belief) - 1)
    new = [(b * stay + (1.0 - b) * move) * max(p, c["emission_floor"]) ** c["emission_power"]
           for b, p in zip(belief, probs)]
    total = sum(new)
    return [x / total for x in new]


def same_spot(a, b):
    """True when the readings `a` and `b` ({address: dB}) are within SPOT_DB of each other: the mean absolute
    difference over the scanners both heard."""
    common = a.keys() & b.keys()
    return bool(common) and sum(abs(a[address] - b[address]) for address in common) / len(common) <= SPOT_DB


def training_samples(samples):
    """The samples of `samples` (in time order) that the fit learns from: those that heard at least MIN_SCANNERS
    scanners, and of each spot only the first SPOT_MAX_S seconds' worth. A spot is a stretch of samples on
    consecutive grid points in one room whose readings all stay within SPOT_DB of the stretch's first sample."""
    kept, anchor, taken, previous = [], None, 0, None
    for sample in samples:
        if len(sample["db"]) < MIN_SCANNERS:
            continue
        if (previous is not None and sample["room"] == previous["room"] and sample["t"] - previous["t"] <= GRID_S * 1.1
                and same_spot(anchor["db"], sample["db"])):
            taken += 1
        else:
            anchor, taken = sample, 1
        previous = sample
        if taken * GRID_S <= SPOT_MAX_S:
            kept.append(sample)
    return kept


def fit(samples, c=CONSTANTS):
    """{room: {address: (median, sd, n)}} of the dB readings of training_samples(samples), `samples` in time order: a
    triple per room and scanner read at least MIN_READINGS times, the sd the sample deviation about the mean."""
    values = collections.defaultdict(lambda: collections.defaultdict(list))
    for sample in training_samples(samples):
        for address, db in sample["db"].items():
            values[sample["room"]][address].append(db)
    stats = {}
    for room, per in values.items():
        stats[room] = {}
        for address, xs in per.items():
            n = len(xs)
            if n < MIN_READINGS:
                continue
            mean = sum(xs) / n
            sd = max(math.sqrt(sum((x - mean) ** 2 for x in xs) / (n - 1)), c["sd_floor_db"])
            stats[room][address] = (statistics.median(xs), sd, n)
    return stats


# ---------- reading the recorder and the registries ----------

def read_storage(name):
    with open(STORAGE + name, encoding="utf-8") as f:
        return json.load(f)["data"]


def scanner_table(entities, devices, areas):
    """{distance entity suffix: {address, name, area}} for each of the beacon's distance sensors whose
    scanner has a Bluetooth address and an area."""
    bluetooth, named, area_of = {}, {}, {}
    for d in devices:
        area = areas.get(d.get("area_id"))
        name = d.get("name_by_user") or d.get("name")
        for kind, mac in d.get("connections") or []:
            mac = mac.lower()
            if kind == "bluetooth":
                bluetooth[mac] = (name, area)
            elif kind == "mac":
                named[mac], area_of[mac] = name, area
    prefix = BEACON + "_distance_to_"
    table = {}
    for e in entities:
        uid = e.get("unique_id") or ""
        if not (e["entity_id"].startswith(prefix) and uid.endswith("_range")):
            continue
        base = uid[: -len("_range")].split("_")[-1].lower()
        head, last = base[:-2], int(base[-2:], 16)
        for step in range(4):
            address = f"{head}{last + step:02x}"
            if address in bluetooth:
                name, area = bluetooth[address]
                table[e["entity_id"][len(prefix):]] = {"address": address, "name": named.get(base) or name,
                                                       "area": area or area_of.get(base)}
                break
    return table


class Series:
    """(time, value) pairs, oldest first, read at a point in time."""

    def __init__(self, pairs):
        self.times = [t for t, _ in pairs]
        self.values = [v for _, v in pairs]

    def at(self, t):
        i = bisect.bisect_right(self.times, t) - 1
        return (self.times[i], self.values[i]) if i >= 0 else (None, None)


def history(db, entity_ids, start, end):
    q = ("select m.entity_id, s.state, s.last_updated_ts from states s join states_meta m on s.metadata_id = m.metadata_id"
         " where m.entity_id in (%s) and s.last_updated_ts between ? and ? order by s.last_updated_ts")
    out = collections.defaultdict(list)
    ids = list(entity_ids)
    for e, s, t in db.execute(q % ",".join("?" * len(ids)), ids + [start - 3600, end]):
        out[e].append((t, s))
    return out


def distance_series(rows, table, offset_history=OFFSET_HISTORY, c=CONSTANTS):
    """{suffix: Series of metres or None}, each value replayed with the Bermuda offsets set after it was recorded."""
    later = [(ts(when), suffix, db) for when, suffix, db in offset_history]
    prefix = BEACON + "_distance_to_"
    out = {}
    for suffix in table:
        pairs = []
        for t, s in rows.get(prefix + suffix, []):
            try:
                v = float(s)
            except ValueError:
                v = None
            if v is not None and math.isfinite(v) and v > 0:
                v *= 10 ** (-sum(db for when, k, db in later if k == suffix and t < when) / c["db_per_decade"])
            else:
                v = None
            pairs.append((t, v))
        out[suffix] = Series(pairs)
    return out


def grid(series, table, truth, vacuum, start, end, first_run=0):
    """The 5 s grid points while the vacuum cleans: {t, room (the robot's, mapped, or None), settled, db, run}.
    Points more than one grid step apart start a new run, the first being number `first_run`."""
    points, run, previous = [], first_run, None
    t = start
    while t < end:
        if vacuum.at(t)[1] == "cleaning":
            since, raw = truth.at(t)
            room = ROBOT_ROOM.get(raw, raw)
            readings = {}
            for suffix, serie in series.items():
                recorded, metres = serie.at(t)
                if metres is not None and t - recorded < FRESH_S:
                    readings[table[suffix]["address"]] = to_db(metres)
            if previous is not None and t - previous > GRID_S * 1.1:
                run += 1
            previous = t
            points.append({"t": t, "room": None if room in (None, "unknown") else room,
                           "settled": since is not None and t - since >= SETTLE_S, "db": readings, "run": run})
        t += GRID_S
    return points


def mark_samples(points, by_address):
    """Sets `scored` on every point: a settled point of a modelled room. A room is modelled when a scanner in
    its own area heard the beacon in at least MIN_ROOM_SAMPLES of its settled points. Returns {room: (settled
    points, points a scanner of its own area heard)} for every room seen."""
    counts = collections.defaultdict(lambda: [0, 0])
    for p in points:
        if p["room"] and p["settled"]:
            counts[p["room"]][0] += 1
            counts[p["room"]][1] += any(by_address[a]["area"] == p["room"] for a in p["db"])
    modelled = {room for room, (_, own) in counts.items() if own >= MIN_ROOM_SAMPLES}
    for p in points:
        p["scored"] = bool(p["room"] in modelled and p["settled"])
    return counts


# ---------- held-out scoring ----------

def nearest(readings, table_by_address):
    placed = {a: db for a, db in readings.items() if table_by_address[a]["area"]}
    if not placed:
        return None
    return table_by_address[max(placed, key=placed.get)]["area"]


def best(probs, rooms, readings):
    return rooms[max(range(len(rooms)), key=probs.__getitem__)] if readings else None


def held_out(points, rooms, by_address, c=CONSTANTS):
    """{method: {room: [right, total]}} for the scored points, methods nearest, model and filter, the counts of
    ALIGNMENTS layouts of the blocks added up."""
    result = {m: {r: [0, 0] for r in rooms} for m in ("nearest", "model", "filter")}
    scored = [i for i, p in enumerate(points) if p["scored"]]
    runs = collections.defaultdict(list)
    for i, p in enumerate(points):
        runs[p["run"]].append(i)
    for k in range(ALIGNMENTS):
        origin = points[0]["t"] - k * BLOCK_S / ALIGNMENTS
        blocks = collections.defaultdict(list)
        for i, p in enumerate(points):
            blocks[int((p["t"] - origin) // BLOCK_S)].append(i)
        for block, test in sorted(blocks.items()):
            low, high = origin + block * BLOCK_S, origin + (block + 1) * BLOCK_S
            train = {i for i in scored if points[i]["t"] < low - GAP_S or points[i]["t"] >= high + GAP_S}
            stats = fit([points[i] for i in sorted(train)], c)
            for run in sorted({points[i]["run"] for i in test}):
                mine = [i for i in test if points[i]["run"] == run]
                first, last = points[mine[0]]["t"], points[mine[-1]]["t"]
                sequence = [i for i in runs[run] if first - LEAD_S <= points[i]["t"] <= last and i not in train]
                belief, previous, inside = [1.0 / len(rooms)] * len(rooms), None, set(mine)
                for i in sequence:
                    p = points[i]
                    probs = posterior(log_likelihoods(stats, rooms, p["db"], c), rooms)
                    dt = GRID_S if previous is None else p["t"] - previous
                    belief, previous = advance(belief, probs, dt, c), p["t"]
                    if i in inside and p["scored"]:
                        answers = {"nearest": nearest(p["db"], by_address),
                                   "model": best(probs, rooms, p["db"]), "filter": best(belief, rooms, p["db"])}
                        for method, answer in answers.items():
                            result[method][p["room"]][0] += answer == p["room"]
                            result[method][p["room"]][1] += 1
    return result


def balanced(per_room):
    shares = [right / total for right, total in per_room.values() if total]
    return sum(shares) / len(shares)


def pooled(per_room):
    return sum(right for right, _ in per_room.values()) / sum(total for _, total in per_room.values())


def print_table(result, rooms):
    print(f"{'room':16} {'samples':>7} {'nearest':>8} {'model':>8} {'filter':>8}")
    for room in rooms:
        total = result["model"][room][1]
        if total:
            print(f"{room:16} {total // ALIGNMENTS:7d} " + " ".join(f"{result[m][room][0] / total:8.0%}"
                                                                    for m in ("nearest", "model", "filter")))
    print(f"{'rooms averaged':16} {'':7} " + " ".join(f"{balanced(result[m]):8.1%}" for m in ("nearest", "model", "filter")))
    samples = sum(total for _, total in result["model"].values()) // ALIGNMENTS
    print(f"{'samples pooled':16} {samples:7d} " + " ".join(f"{pooled(result[m]):8.1%}" for m in ("nearest", "model", "filter")))


# ---------- the model file ----------

def model_document(rooms, by_address, stats, window, by_suffix, c=CONSTANTS, offset_history=OFFSET_HISTORY,
                   windows=None):
    """The model file's content. Its scanners are those that heard the beacon in at least one room. `window` is
    the first start and the last end, `windows` each (start, end) trained on, `window` alone when not given."""
    heard = {a for per in stats.values() for a in per}
    return {
        "version": MODEL_VERSION,
        "rooms": list(rooms),
        "scanners": {a: {"name": by_address[a]["name"], "area": by_address[a]["area"]} for a in sorted(heard)},
        "stats": {room: {a: [round(m, 2), round(sd, 2), n] for a, (m, sd, n) in sorted(stats[room].items())}
                  for room in rooms},
        "constants": dict(c),
        "offset_history": [{"from": when + "Z", "scanner": by_suffix[suffix]["address"], "db": db}
                           for when, suffix, db in offset_history if suffix in by_suffix],
        "trained_window": {"start": iso(window[0]), "end": iso(window[1])},
        "trained_windows": [{"start": iso(a), "end": iso(b)} for a, b in (windows or [window])],
        "trained_at": iso(datetime.now(timezone.utc).timestamp()),
    }


def write_model(path, document):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(document, f, indent=1, sort_keys=True)
        f.write("\n")
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, 0o644)
    os.replace(tmp, path)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Fit and score the BleLocator room model from the recorder.")
    parser.add_argument("window", nargs="+", metavar="UTC",
                        help="a window's start and its end, ISO date and time, UTC; more pairs add more windows")
    parser.add_argument("--write", metavar="PATH", help="write the model JSON here if it beats the nearest scanner")
    args = parser.parse_args(argv)
    if len(args.window) % 2:
        parser.error("each window needs a start and an end")
    windows = sorted((ts(a), ts(b)) for a, b in zip(args.window[::2], args.window[1::2]))
    if any(b <= a for a, b in windows) or any(x[1] > y[0] for x, y in zip(windows, windows[1:])):
        parser.error("a window must end after it starts and windows must not overlap")

    db = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    areas = {a["id"]: a["name"] for a in read_storage("core.area_registry")["areas"]}
    table = scanner_table(read_storage("core.entity_registry")["entities"],
                          read_storage("core.device_registry")["devices"], areas)
    prefix = BEACON + "_distance_to_"
    points = []
    for start, end in windows:
        rows = history(db, [prefix + s for s in table] + ["sensor.rober2_current_room", "vacuum.rober2"], start, end)
        points += grid(distance_series(rows, table), table, Series(rows["sensor.rober2_current_room"]),
                       Series(rows["vacuum.rober2"]), start, end, points[-1]["run"] + 1 if points else 0)
    by_address = {s["address"]: s for s in table.values()}
    counts = mark_samples(points, by_address)
    rooms = sorted({p["room"] for p in points if p["scored"]})
    if not rooms:
        sys.exit(f"no room has {MIN_ROOM_SAMPLES} settled cleaning samples heard by a scanner of its own area")

    print(f"{'window' if len(windows) == 1 else 'windows'} " + ", ".join(f"{iso(a)} to {iso(b)}" for a, b in windows)
          + f": {len(points)} cleaning points of 5 s, {sum(p['scored'] for p in points)} samples in {len(rooms)} rooms")
    skipped = {r: n for r, n in counts.items() if r not in rooms}
    if skipped:
        print("not modelled (settled points / of them heard by a scanner of the room's own area): "
              + ", ".join(f"{r} {n}/{own}" for r, (n, own) in sorted(skipped.items())))
    unnamed = sorted(r for r in rooms if r not in areas.values())
    if unnamed:
        print("rooms that are no area name:", ", ".join(unnamed))
    scored = [p for p in points if p["scored"]]
    print(f"training samples: {len(training_samples(scored))} of {len(scored)} kept "
          f"(at least {MIN_SCANNERS} scanners heard, at most {SPOT_MAX_S} s from one spot)")
    stats = fit(scored)
    heard = {a for per in stats.values() for a in per}
    print(f"{len(heard)} scanners heard the beacon: " + ", ".join(
        f"{by_address[a]['name']} [{by_address[a]['area']}]" for a in sorted(heard, key=lambda a: by_address[a]["name"])))
    silent = sorted(s["name"] for a, s in by_address.items() if a not in heard)
    if silent:
        print(f"scanners read fewer than {MIN_READINGS} times in every room, left out:", ", ".join(silent))

    result = held_out(points, rooms, by_address)
    print(f"\nleave-one-block-out, {BLOCK_S} s blocks with {GAP_S} s left out on each side, filter restarted per "
          f"block, mean of {ALIGNMENTS} block alignments")
    print_table(result, rooms)
    gain = (balanced(result["model"]) - balanced(result["nearest"])) * 100

    document = model_document(rooms, by_address, stats, (windows[0][0], windows[-1][1]), table, windows=windows)
    print(f"\nthe model beats the nearest scanner by {gain:.1f} points over rooms (needs {MIN_GAIN:.0f})")
    if not args.write:
        print("not written: no --write")
        return 0
    if gain < MIN_GAIN:
        print(f"not written to {args.write}")
        return 1
    write_model(args.write, document)
    print(f"written to {args.write}: {len(rooms)} rooms, {len(document['scanners'])} scanners")
    return 0


if __name__ == "__main__":
    sys.exit(main())
