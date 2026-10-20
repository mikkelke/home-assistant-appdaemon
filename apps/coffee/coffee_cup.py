"""Whether a cup stands on the xBloom's scale, from its reading. Pure.

The machine zeroes the scale with whatever stands on it when it wakes and when a brew starts, so a reading is the net
change since then. STEP_G or more held for HOLD_S says a cup was put on (`present`), STEP_G or more below says
something was lifted off since the zero (`absent`), and anything between says nothing (`unknown`), as right after a
zero. At rest the magnet grinder cup and the dripper stand on the scale: a machine woken at rest and readied without a
cup reads about -114 g, and with the cup about +160 g. A completed brew leaves its cup on the scale (`present`) until
it is picked up (`absent`).

Frames come only while the app holds the link, so the state holds only while it watches: once the link goes or the
app restarts it is `unknown` until the next frame. While the machine sleeps the scale sends nothing but SLEEP_G once,
which is no reading. Times are epoch seconds.
"""

STEP_G = 100.0
HOLD_S = 2.0
SLEEP_G = 50.0


def side(grams):
    return "present" if grams >= STEP_G else "absent" if grams <= -STEP_G else "unknown"


def sleep_frame(grams):
    """The frame the scale sends once as the machine falls asleep, which is no reading."""
    return abs(grams - SLEEP_G) < 0.005


class Cup:
    def __init__(self):
        self.state = "unknown"
        self.grams = None
        self.step = None

    def _set(self, state):
        changed = state != self.state
        self.state, self.step = state, None
        return changed

    def reading(self, now, grams):
        """A frame from the scale. True when the state changed."""
        if sleep_frame(grams):
            return False
        self.grams = grams
        way = side(grams)
        if way == self.state:
            self.step = None
        elif self.step is None or self.step[0] != way:
            self.step = (way, now)
        return self.settle(now)

    def settle(self, now):
        """Decides a reading that has stayed on its side for HOLD_S. True when the state changed."""
        if self.step is None or now - self.step[1] < HOLD_S:
            return False
        return self._set(self.step[0])

    def due(self):
        """When the reading under way decides, None without one."""
        return None if self.step is None else self.step[1] + HOLD_S

    def zero(self):
        """The machine zeroed the scale: on waking and at the start of a brew."""
        self.grams = 0.0
        return self._set("unknown")

    def asleep(self):
        """The machine sleeps or is off."""
        self.grams = None
        return self._set("unknown")

    def unseen(self):
        """The link went: changes from now on go unseen."""
        return self._set("unknown")

    def put_on(self):
        """A cup stands on the scale: a brew completed on it, or someone said so."""
        return self._set("present")

    def lifted(self):
        """The brew was picked up."""
        return self._set("absent")
