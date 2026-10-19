"""Who made a brew, and who is told it is ready. Pure.

In order: the person whose start it was (their HA user, through ActorAttribution's dashboard_action),
a person picked on the notify chip, Mikkel's phone by Bermuda when that is confident, the sole
occupant when sole occupancy is on, else nobody. Nobody named means no push.

Bermuda is confident (provisional rule, its evidence kept with the brew for calibration) when
person.mikkel is home, the phone's distance to the pour-over plug read CONFIDENT_M or less at an update
from BEFORE_S before the start to AFTER_S after it, and the phone moved in the MOVED_S before the start
(its area changed, or that distance changed by MOVED_M or more). A phone lying still is never evidence.
"""

CONFIDENT_M = 2.0
BEFORE_S = 180
AFTER_S = 30
MOVED_S = 900
MOVED_M = 1.0
KEEP_S = 1200
PEOPLE = ("mikkel", "kristine", "claudia")


def display_name(person):
    return person.capitalize() if person in PEOPLE else None


def as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Track:
    """The recent updates of one entity: [(epoch s, state)], the last KEEP_S, plus the one before."""

    def __init__(self):
        self.points = []

    def add(self, ts, state):
        self.points.append((ts, state))
        cutoff = ts - KEEP_S
        while len(self.points) > 1 and self.points[1][0] < cutoff:
            self.points.pop(0)

    def window(self, start, end):
        return [(t, s) for t, s in self.points if start <= t <= end]

    def at(self, ts):
        before = [s for t, s in self.points if t <= ts]
        return before[-1] if before else None


def bermuda(person_state, distance, area, t0):
    """(confident, evidence) for Mikkel's phone at a start at `t0`."""
    near = [(t, as_float(s)) for t, s in distance.window(t0 - BEFORE_S, t0 + AFTER_S)]
    near = [(t, m) for t, m in near if m is not None]
    closest = min((m for _, m in near), default=None)
    areas = [s for _, s in area.window(t0 - MOVED_S, t0)]
    before = area.at(t0 - MOVED_S)
    area_moved = len(set(areas + ([before] if before is not None else []))) > 1
    metres = [m for m in (as_float(s) for _, s in distance.window(t0 - MOVED_S, t0)) if m is not None]
    distance_moved = bool(metres) and max(metres) - min(metres) >= MOVED_M
    evidence = {"rule": "v1", "person": str(person_state or ""), "updates": str(len(near)),
                "moved": "area" if area_moved else "distance" if distance_moved else "no"}
    if closest is not None:
        evidence["closest_m"] = f"{closest:.1f}"
    confident = person_state == "home" and closest is not None and closest <= CONFIDENT_M and (
        area_moved or distance_moved)
    return confident, evidence


def resolve(start_person=None, chip_person=None, bermuda_person=None, sole_person=None, sole_enabled=True):
    """(who, method) by the order in the module docstring; ("none", None) when nobody is named."""
    if start_person in PEOPLE:
        return start_person, "dashboard_action"
    if chip_person in PEOPLE:
        return chip_person, "chip"
    if bermuda_person in PEOPLE:
        return bermuda_person, "bermuda"
    if sole_enabled and sole_person in PEOPLE:
        return sole_person, "sole_occupancy"
    return "none", None


def notify_target(who, chip):
    """Who gets the ready push: a pick on the chip (nobody included), else whoever made it."""
    if chip in PEOPLE or chip == "none":
        return chip
    return who if who in PEOPLE else "none"
