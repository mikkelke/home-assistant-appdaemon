"""The water tank: about how many millilitres are left in it. Pure.

The tank is filled by hand and the machine only says when it has run dry, so the level is the capacity minus the
water the machine measured dispensing since the last fill, and unknown until the first one. Every brew debits its
water once. A fill or a run-dry in the middle of a brew moves `offset_ml` to that brew's reading, so only the
water it pours after the event is debited. The water used may pass the capacity, which shows as zero left.
`empties` keeps the water used at each of the last EMPTIES_MAX run-dry moments.
"""

DEFAULT_ML = 946
EMPTIES_MAX = 10


def _ml(value):
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


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

    def to_dict(self):
        return {"filled_at": self.filled_at, "used_ml": self.used_ml, "offset_ml": self.offset_ml,
                "empties": self.empties, "debited_id": self.debited_id}

    @property
    def left(self):
        return None if self.filled_at is None else max(0.0, self.capacity - self.used_ml)

    def fill(self, now, brew_water_ml=None):
        """The tank is full. `brew_water_ml`: what the running brew has dispensed so far, if one runs."""
        self.used_ml = 0.0
        self.filled_at = now.isoformat(timespec="seconds")
        self.offset_ml = _ml(brew_water_ml)

    def empty(self, now, brew_water_ml=None):
        """The machine ran dry: the water used since the fill, None while the level is unknown."""
        if self.filled_at is None:
            return None
        reading = _ml(brew_water_ml)
        used = self.used_ml + (max(0.0, reading - (self.offset_ml or 0.0)) if reading is not None else 0.0)
        self.empties = (self.empties + [{"at": now.isoformat(timespec="seconds"), "used_ml": round(used, 1)}])[
            -EMPTIES_MAX:]
        self.used_ml = self.capacity
        self.offset_ml = reading
        return round(used, 1)

    def brew_done(self, water_ml, brew_id=None):
        """Takes a brew's water out once; False when `brew_id` was debited already."""
        if brew_id is not None and brew_id == self.debited_id:
            return False
        water = _ml(water_ml)
        if self.filled_at is not None and water is not None:
            self.used_ml = round(self.used_ml + max(0.0, water - (self.offset_ml or 0.0)), 1)
        self.offset_ml = None
        if brew_id is not None:
            self.debited_id = brew_id
        return True

    def view(self):
        """What sensor.coffee_state shows, in numbers."""
        return {"capacity": self.capacity, "left": self.left, "filled_at": self.filled_at}
