"""Who made a brew, and who is told it is ready. Pure.

In order: the person whose start it was (their HA user, through ActorAttribution's dashboard_action),
a person picked on the notify chip, a phone Bermuda picks, the sole occupant when sole occupancy is on,
else nobody. Nobody named means no push.

A phone is confident (provisional rule, its evidence kept with the brew for calibration) when its
person is home, its distance to the coffee bar read CONFIDENT_M or less at an update from BEFORE_S
before the start to AFTER_S after it, and it moved in the MOVED_S before the start (its area changed,
or that distance changed by MOVED_M or more). A phone lying still is never evidence. Bermuda picks the
one confident phone; of several, the closest when the next is MARGIN_M or more farther, else nobody.

Who filled the water tank is told by the brew that follows (fill_by): the first to start within FILL_WINDOW_S
after a tap, or the one that showed an inferred refill. Its person fills; a brew that names nobody leaves it to
the resident whose account tapped, else nobody. A tap less than FILL_MERGE_S after the fill kept before it is a
double tap and counts with that fill (single_fills).
"""

from coffee_fsm import epoch

CONFIDENT_M = 2.0
BEFORE_S = 180
AFTER_S = 30
MOVED_S = 900
MOVED_M = 1.0
MARGIN_M = 1.0
KEEP_S = 1200
FILL_WINDOW_S = 900
FILL_MERGE_S = 1800
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
    """(confident, evidence) for one phone at a start at `t0`."""
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


def pick(phones):
    """(person or None, reason) from {person: (confident, evidence)} by the rule in the module docstring."""
    near = sorted((float(evidence["closest_m"]), person) for person, (confident, evidence) in phones.items()
                  if confident)
    if not near:
        return None, "none_near"
    if len(near) == 1 or near[1][0] - near[0][0] >= MARGIN_M:
        return near[0][1], "closest"
    return None, "tie"


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


def named(who):
    """`who` when it is a resident, else "none"."""
    return who if who in PEOPLE else "none"


def _started(record):
    return epoch(record.get("started_at")) or epoch(record.get("ts"))


def fill_by(fill, records, now):
    """Who filled the tank at `fill` (a coffee_tank fill), by the ledger `records`: a resident or "none", and None
    while it is pending: no brew yet and the fill under FILL_WINDOW_S old."""
    at = epoch(fill["at"])
    if fill.get("brew"):
        brew = next((r for r in records if r.get("id") == fill["brew"]), None)
    else:
        brew = min((r for r in records if at <= (_started(r) or -1) <= at + FILL_WINDOW_S), key=_started, default=None)
    if brew is None and now - at < FILL_WINDOW_S:
        return None
    who = named((brew or {}).get("who"))
    return who if who != "none" else named(fill.get("user"))


def single_fills(fills):
    """`fills` in time order without the double taps: a tap less than FILL_MERGE_S after the fill kept before it. A
    refill seen from a brew is never one: the tank ran dry in between."""
    kept = []
    for fill in sorted(fills, key=lambda f: epoch(f["at"])):
        if kept and fill.get("source") == "tap" and epoch(fill["at"]) - epoch(kept[-1]["at"]) < FILL_MERGE_S:
            continue
        kept.append(fill)
    return kept


def fill_counts(fills, records, now, since):
    """{who: fills} of the fills at `since` (epoch s) or later that are not pending, a double tap counted once."""
    counts = {}
    for fill in single_fills(fills):
        if epoch(fill["at"]) < since:
            continue
        by = fill_by(fill, records, now)
        if by is not None:
            counts[by] = counts.get(by, 0) + 1
    return counts


def counts_csv(counts):
    """`mikkel:18,claudia:2,none:15`: the most first, "none" last, nobody with no count."""
    ranked = sorted((who, n) for who, n in counts.items() if n > 0)
    ranked.sort(key=lambda item: (item[0] == "none", -item[1]))
    return ",".join(f"{who}:{n}" for who, n in ranked)
