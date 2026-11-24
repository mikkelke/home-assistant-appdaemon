"""The water tank: about how many millilitres are left in it. Pure.

The tank is filled by hand and the machine only says when it has run dry, so the level is the capacity minus the
water the machine measured dispensing since the last fill, and unknown until the first one. Every brew debits its
water once. A fill or a run-dry in the middle of a brew moves `offset_ml` to that brew's reading, so only the
water it pours after the event is debited. The water used may pass the capacity, which shows as zero left.
A run-dry is a brew the machine stopped for want of water; its low-water warning during a brew that pours on is
no run-dry. After a run-dry the tank is `dry`: a later brew that pours more than REFILL_MIN_ML means it was refilled, and
the tank counts as full from that brew on. `empties` keeps the water used at each of the last EMPTIES_MAX run-dry
moments. `fills` keeps every fill of the last FILLS_KEEP_S: a tap on "Filled" with the resident whose account it was
(None for a shared one), or a refill seen from the water poured, with the id of the brew that showed it. Who filled
is worked out from them and the ledger (coffee_who.fill_by), never stored.
"""

from coffee_fsm import epoch

DEFAULT_ML = 946
EMPTIES_MAX = 10
REFILL_MIN_ML = 30
FILLS_KEEP_S = 90 * 86400
FILL_SOURCES = ("tap", "inferred")


def _ml(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _fill(value):
    """A saved fill as it is kept, None when it is no fill."""
    if not isinstance(value, dict) or value.get("source") not in FILL_SOURCES:
        return None
    at = value.get("at")
    if not isinstance(at, str) or epoch(at) is None:
        return None
    user = value.get("user")
    fill = {"at": at, "source": value["source"], "user": user if isinstance(user, str) else None}
    if isinstance(value.get("brew"), str):
        fill["brew"] = value["brew"]
    return fill


class Tank:
    def __init__(self, data=None, capacity=DEFAULT_ML):
        data = data if isinstance(data, dict) else {}
        self.capacity = float(capacity)
        self.filled_at = data.get("filled_at") if isinstance(data.get("filled_at"), str) else None
        self.used_ml = _ml(data.get("used_ml")) or 0.0
        self.offset_ml = _ml(data.get("offset_ml"))
        self.empties = [{"at": e["at"], "used_ml": e["used_ml"]} for e in data.get("empties") or []
                        if isinstance(e, dict) and isinstance(e.get("at"), str) and _ml(e.get("used_ml")) is not None
                        ][-EMPTIES_MAX:]
        self.debited_id = data.get("debited_id") if isinstance(data.get("debited_id"), str) else None
        self.dry = data.get("dry") is True
        saved = data.get("fills") if isinstance(data.get("fills"), list) else []
        self.fills = [fill for fill in map(_fill, saved) if fill]

    def to_dict(self):
        return {"filled_at": self.filled_at, "used_ml": self.used_ml, "offset_ml": self.offset_ml,
                "empties": self.empties, "debited_id": self.debited_id, "dry": self.dry, "fills": self.fills}

    @property
    def left(self):
        return None if self.filled_at is None else max(0.0, self.capacity - self.used_ml)

    def fill(self, now, brew_water_ml=None, user=None):
        """The tank is full. `brew_water_ml`: what the running brew has dispensed so far, if one runs; `user`: the
        resident whose account tapped."""
        self.used_ml = 0.0
        self.filled_at = now.isoformat(timespec="seconds")
        self.offset_ml = _ml(brew_water_ml)
        self.dry = False
        self._log_fill(now, "tap", user)

    def _log_fill(self, now, source, user=None, brew=None):
        fill = {"at": now.isoformat(timespec="seconds"), "source": source, "user": user}
        if brew is not None:
            fill["brew"] = brew
        cutoff = now.timestamp() - FILLS_KEEP_S
        self.fills = [f for f in self.fills + [fill] if epoch(f["at"]) >= cutoff]

    def used(self, brew_water_ml=None):
        """The water used since the fill, counting the running brew's reading; None while the level is unknown."""
        if self.filled_at is None:
            return None
        reading = _ml(brew_water_ml)
        return self.used_ml + (max(0.0, reading - (self.offset_ml or 0.0)) if reading is not None else 0.0)

    def empty(self, now, brew_water_ml=None):
        """The machine ran dry: the water used since the fill, None while the level is unknown."""
        used = self.used(brew_water_ml)
        if used is None:
            return None
        reading = _ml(brew_water_ml)
        self.empties = (self.empties + [{"at": now.isoformat(timespec="seconds"), "used_ml": round(used, 1)}])[
            -EMPTIES_MAX:]
        self.used_ml = self.capacity
        self.offset_ml = reading
        self.dry = True
        return round(used, 1)

    def brew_done(self, water_ml, brew_id=None, now=None):
        """Takes a brew's water out once; False when `brew_id` was debited already."""
        if brew_id is not None and brew_id == self.debited_id:
            return False
        water = _ml(water_ml)
        if self.filled_at is not None and water is not None:
            poured = max(0.0, water - (self.offset_ml or 0.0))
            if self.dry and self.offset_ml is None and poured > REFILL_MIN_ML and now is not None:
                self.used_ml = round(poured, 1)
                self.filled_at = now.isoformat(timespec="seconds")
                self.dry = False
                self._log_fill(now, "inferred", brew=brew_id)
            else:
                self.used_ml = round(self.used_ml + poured, 1)
        self.offset_ml = None
        if brew_id is not None:
            self.debited_id = brew_id
        return True

    def view(self):
        """What sensor.coffee_state shows, in numbers."""
        return {"capacity": self.capacity, "left": self.left, "filled_at": self.filled_at}
