"""The xBloom Studio as its Shelly plug sees it (sensor.pour_over_plug_power). Pure.

A reading holds from its time until the next one. The machine is `asleep` below ASLEEP_BELOW_W (standby
reads 0.8-1.0 W), `working` from WORKING_FROM_W until it has stayed under that for QUIET_S, `awake`
otherwise, and `off` while the plug is off or reports nothing.

PowerBrew finds a brew in the readings alone, for brews Bluetooth does not see and for the backfill. A
brew is a grinder phase (GRINDER_MIN_W to GRINDER_MAX_W for at least GRINDER_MIN_S) followed within
HEATER_WITHIN_S by heater pulses (HEATER_FROM_W and up). It ends once the power has stayed under
WORKING_FROM_W for QUIET_S, and is complete with at least COMPLETE_PULSES pulses. A grind with no heat
after it is a grind on its own, and heat with no grind before it is no brew either. Measured on the
plug's history: the grinder draws 17-55 W for 12-17 s, the first pulse comes 6-8 s after it, and a
4-pour brew makes about seven pulses, some of them only 300-500 W.
"""

ASLEEP_BELOW_W = 1.4
WORKING_FROM_W = 10.0
QUIET_S = 60.0
GRINDER_MIN_W = 12.0
GRINDER_MAX_W = 100.0
GRINDER_MIN_S = 8.0
HEATER_FROM_W = 150.0
HEATER_WITHIN_S = 90.0
COMPLETE_PULSES = 3


def watts(value):
    """A power state as a float, None when it is no number."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


class MachineMeter:
    """The machine's class from its readings, with the working hold."""

    def __init__(self):
        self.watts = None
        self.plug_on = True
        self.last_working = None
        self.cool_since = None

    def update(self, ts, value):
        self.watts = watts(value)
        if self.watts is None:
            return
        if self.watts >= WORKING_FROM_W:
            self.last_working = ts
            self.cool_since = None
        elif self.last_working is not None and self.cool_since is None:
            self.cool_since = ts

    def state(self, now):
        if not self.plug_on or self.watts is None:
            return "off"
        if self.watts >= WORKING_FROM_W:
            return "working"
        if self.cool_since is not None and now - self.cool_since < QUIET_S:
            return "working"
        return "asleep" if self.watts < ASLEEP_BELOW_W else "awake"


class PowerBrew:
    """Streams readings into brew events: ("grinding", start), ("heating", start, first pulse),
    ("grind_only", start), ("ended", start, end, pulses, complete). Times are the readings' own, so a
    late tick or a late reading never moves them."""

    def __init__(self):
        self._idle()

    def _idle(self):
        self.phase = "idle"
        self.start = None
        self.grind_end = None
        self.heat_first = None
        self.pulses = 0
        self.pulse_on = False
        self.cool_since = None
        self.reported = False

    def snapshot(self):
        return {key: getattr(self, key) for key in ("phase", "start", "grind_end", "heat_first", "pulses",
                                                    "pulse_on", "cool_since", "reported")}

    def restore(self, saved):
        self._idle()
        if isinstance(saved, dict) and saved.get("phase") in ("idle", "candidate", "grinding", "ground", "heating"):
            for key, value in saved.items():
                if hasattr(self, key):
                    setattr(self, key, value)

    def feed(self, ts, value):
        w = watts(value)
        events = self.tick(ts)
        if w is None:
            return events
        events += self._reading(ts, w)
        return events

    def tick(self, now):
        events = []
        if self.phase == "candidate" and not self.reported and now - self.start >= GRINDER_MIN_S:
            self.reported = True
            events.append(("grinding", self.start))
        elif self.phase == "ground" and now - self.grind_end > HEATER_WITHIN_S:
            events.append(("grind_only", self.start))
            self._idle()
        elif self.phase == "heating" and self.cool_since is not None and now - self.cool_since >= QUIET_S:
            events.append(("ended", self.start, self.cool_since, self.pulses, self.pulses >= COMPLETE_PULSES))
            self._idle()
        return events

    def _reading(self, ts, w):
        in_band = GRINDER_MIN_W <= w < GRINDER_MAX_W
        if self.phase == "idle":
            if in_band:
                self.phase, self.start = "candidate", ts
            return []
        if self.phase == "candidate":
            if in_band:
                if not self.reported and ts - self.start >= GRINDER_MIN_S:
                    self.reported = True
                    return [("grinding", self.start)]
                return []
            if ts - self.start < GRINDER_MIN_S:
                self._idle()
                return []
            events = [] if self.reported else [("grinding", self.start)]
            self.reported = True
            self.phase, self.grind_end = "ground", ts
            return events + self._reading(ts, w)
        if self.phase == "ground":
            if w >= HEATER_FROM_W:
                self.phase, self.heat_first, self.pulses, self.pulse_on = "heating", ts, 1, True
                return [("heating", self.start, ts)]
            return []
        if w >= HEATER_FROM_W:
            if not self.pulse_on:
                self.pulses += 1
            self.pulse_on = True
        else:
            self.pulse_on = False
        if w >= WORKING_FROM_W:
            self.cool_since = None
        elif self.cool_since is None:
            self.cool_since = ts
        return []


def detect(rows):
    """[(start, end, pulses, complete)] of the brews in `rows` [(epoch s, value)], oldest first."""
    brew, found = PowerBrew(), []
    for ts, value in rows:
        for event in brew.feed(ts, value):
            if event[0] == "ended":
                found.append(event[1:])
    if rows:
        for event in brew.tick(rows[-1][0] + HEATER_WITHIN_S + QUIET_S + 1):
            if event[0] == "ended":
                found.append(event[1:])
    return found
