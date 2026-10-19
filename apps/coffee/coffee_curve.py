"""One brew's curve, the way the xBloom app draws it: grams in the cup and millilitres of water the
machine reports dispensing, one sample a second from the first bloom (index 0) to ENJOY. Pure.

Each second takes the latest reading of each (sample and hold), but only while the link is fresh: a
frame or heartbeat within FRESH_S. Otherwise the second is a gap (None), and a gap is never filled in.
Seconds the sampler missed take what the last sample held while that was still fresh, and are gaps
after it; a curve restored from a checkpoint holds nothing, so a restart's downtime is a gap.
A cup reading more than SPIKE_G away from the last one kept is dropped, unless it comes SPIKE_REPEATS
times in a row, as the app does. Values are kept in tenths (grams x 10, ml x 10).

Totals: total_s is first bloom to ENJOY, drawdown_s is BREW_END to ENJOY (the last rise of the water
when BREW_END went unheard), coverage is the share of fresh seconds, and cup_g and water_ml are the
last valid samples.
"""

FRESH_S = 10.0
SPIKE_G = 300.0
SPIKE_REPEATS = 4
WATER_RISE_ML = 0.5


def tenths(value):
    return None if value is None else int(round(value * 10))


class CupFilter:
    def __init__(self):
        self.last = None
        self.rejected = 0

    def accept(self, grams):
        """`grams` when it is kept, None when it is dropped as a spike."""
        if self.last is None or abs(grams - self.last) <= SPIKE_G:
            self.last, self.rejected = grams, 0
            return grams
        self.rejected += 1
        if self.rejected >= SPIKE_REPEATS:
            self.last, self.rejected = grams, 0
            return grams
        return None


class Curve:
    def __init__(self, t0):
        self.t0 = t0
        self.cup = []
        self.water = []
        self.fresh = []
        self.pours = []
        self.pauses = []
        self.end_s = None
        self.hold = None

    def to_dict(self):
        return {"t0": self.t0, "cup": self.cup, "water": self.water, "fresh": self.fresh, "pours": self.pours,
                "pauses": self.pauses, "end_s": self.end_s}

    @classmethod
    def from_dict(cls, data):
        curve = cls(float(data["t0"]))
        for key in ("cup", "water", "fresh", "pours", "pauses"):
            value = data.get(key)
            setattr(curve, key, list(value) if isinstance(value, list) else [])
        curve.end_s = data.get("end_s")
        return curve

    def second(self, ts):
        return max(0, int(round(ts - self.t0)))

    def _append(self, cup, water, fresh):
        self.cup.append(cup if fresh else None)
        self.water.append(water if fresh else None)
        self.fresh.append(1 if fresh else 0)

    def sample(self, now, cup_g, water_ml, fresh, fresh_until=None):
        """Appends one sample for every second from the last one up to `now`. `fresh_until`: until when the
        latest frame keeps the link fresh, now + FRESH_S by default."""
        if self.end_s is not None:
            return
        index = int(now - self.t0)
        cup, water = tenths(cup_g), tenths(water_ml)
        first = not self.cup
        while len(self.cup) < index:
            if first:
                self._append(cup, water, fresh)
            elif self.hold is not None and self.t0 + len(self.cup) <= self.hold[2]:
                self._append(self.hold[0], self.hold[1], True)
            else:
                self._append(None, None, False)
        if len(self.cup) == index:
            self._append(cup, water, fresh)
        if fresh:
            self.hold = (cup, water, fresh_until if fresh_until is not None else now + FRESH_S)
        else:
            self.hold = None

    def pour(self, ts, index):
        if any(known == index for _, known in self.pours):
            return False
        self.pours.append([self.second(ts), index])
        return True

    def pause(self, ts):
        if not self.pauses or self.pauses[-1][1] is not None:
            self.pauses.append([self.second(ts), None])

    def resume(self, ts):
        if self.pauses and self.pauses[-1][1] is None:
            self.pauses[-1][1] = self.second(ts)

    def paused_s(self, now=None):
        total = 0
        for start, end in self.pauses:
            if end is None:
                end = self.second(now) if now is not None else start
            total += max(0, end - start)
        return total

    def end(self, ts, cup_g, water_ml, fresh, fresh_until=None):
        self.sample(ts, cup_g, water_ml, fresh, fresh_until)
        self.resume(ts)
        self.end_s = self.second(ts)

    def trimmed(self, series):
        """`series` up to its last valid sample."""
        last = max((i for i, v in enumerate(series) if v is not None), default=-1)
        return series[:last + 1]

    def last_valid(self, series):
        values = [v for v in series if v is not None]
        return values[-1] / 10 if values else None

    def last_water_rise(self):
        """Seconds from t0 of the last rise of the water, None without one."""
        found, prev = None, None
        for i, value in enumerate(self.water):
            if value is None:
                continue
            if prev is not None and value - prev >= WATER_RISE_ML * 10:
                found = i
            prev = value
        return found

    def totals(self, enjoy_ts, brew_end_ts=None):
        """{total_s, drawdown_s, coverage, cup_g, water_ml}; a value that cannot be told is left out."""
        out = {}
        total = int(round(enjoy_ts - self.t0))
        if total > 0:
            out["total_s"] = total
        if brew_end_ts is not None and enjoy_ts >= brew_end_ts:
            out["drawdown_s"] = int(round(enjoy_ts - brew_end_ts))
        else:
            rise = self.last_water_rise()
            if rise is not None and total >= rise:
                out["drawdown_s"] = total - rise
        upto = self.fresh[:total + 1] if total > 0 else self.fresh
        if upto:
            out["coverage"] = round(sum(upto) / len(upto), 2)
        cup, water = self.last_valid(self.cup), self.last_valid(self.water)
        if cup is not None:
            out["cup_g"] = round(cup, 1)
        if water is not None:
            out["water_ml"] = round(water, 1)
        return out

    def file_data(self, brew_id):
        """The curve file /media/coffee/brews/<id>.json."""
        return {"id": brew_id, "t0": self.t0, "step_s": 1, "cup": self.trimmed(self.cup),
                "water": self.trimmed(self.water), "pours": self.pours,
                "pauses": [[s, e if e is not None else s] for s, e in self.pauses], "end_s": self.end_s}


def every(series, step):
    """Every `step`-th sample of a 1 s series (sample and hold), None kept as a gap."""
    return list(series[::step]) if step > 1 else list(series)


def csv(series):
    return ",".join("" if v is None else str(int(v)) for v in series)


def pairs_csv(pairs, sep):
    return ",".join(f"{a}{sep}{b}" for a, b in pairs)
