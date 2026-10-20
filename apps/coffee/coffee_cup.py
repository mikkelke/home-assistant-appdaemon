"""Whether a cup stands on the xBloom's scale, from how its reading changed since the scale was last zeroed. Pure.

The machine zeroes the scale with whatever stands on it when it wakes and when a brew starts, so a reading is the
change since that zero and a cup that stood there all along reads 0. A step of at least STEP_G away from the level of
the last decision, held for HOLD_S, decides: up is a cup put on (`present`), down is one lifted (`absent`). Lifting
the cup again after putting it on, or putting it back after lifting it, is another such step. After a zero nothing is
known (`unknown`) until a step. A completed brew leaves its cup on the scale (`present`) until it is picked up
(`absent`). The magnet grinder cup (40 g) stays under STEP_G.

Frames come only while the app holds the link, so a decision holds only while it watches: once the link goes or the
app restarts the state is `unknown` again, and the level stays to judge the next step against. While the machine
sleeps the scale sends nothing but SLEEP_G once, which is no reading. Times are epoch seconds.
"""

STEP_G = 100.0
HOLD_S = 2.0
SLEEP_G = 50.0
STATES = ("present", "absent", "unknown")


class Cup:
    def __init__(self, level=0.0):
        self.state = "unknown"
        self.level = float(level)
        self.grams = None
        self.step = None

    def to_dict(self):
        return {"level": self.level}

    @classmethod
    def from_dict(cls, data):
        level = data.get("level") if isinstance(data, dict) else None
        return cls(level if isinstance(level, (int, float)) and not isinstance(level, bool) else 0.0)

    def _set(self, state, level):
        changed = state != self.state
        self.state, self.level, self.step = state, level, None
        return changed

    def reading(self, now, grams):
        """A frame from the scale. True when the state changed."""
        if abs(grams - SLEEP_G) < 0.005:
            return False
        self.grams = grams
        rise = grams - self.level
        way = "present" if rise >= STEP_G else "absent" if rise <= -STEP_G else None
        if way is None:
            self.step = None
        elif self.step is None or self.step[0] != way:
            self.step = (way, now)
        return self.settle(now)

    def settle(self, now):
        """Decides a step that has held for HOLD_S. True when the state changed."""
        if self.step is None or now - self.step[1] < HOLD_S:
            return False
        return self._set(self.step[0], self.grams)

    def due(self):
        """When the step under way decides, None without one."""
        return None if self.step is None else self.step[1] + HOLD_S

    def zero(self):
        """The machine zeroed the scale: on waking and at the start of a brew."""
        self.grams = 0.0
        return self._set("unknown", 0.0)

    def asleep(self):
        """The machine sleeps or is off; it zeroes the scale when it wakes."""
        self.grams = None
        return self._set("unknown", 0.0)

    def unseen(self):
        """The link went: changes from now on go unseen."""
        changed = self.state != "unknown"
        self.state, self.step = "unknown", None
        return changed

    def put_on(self):
        """A cup stands on the scale: a brew completed on it, or someone said so."""
        return self._set("present", self.level if self.grams is None else self.grams)

    def lifted(self):
        """The brew was picked up."""
        return self._set("absent", self.level if self.grams is None else self.grams)
