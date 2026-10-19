"""The beans container: which beans are in it and about how many grams are left. Pure.

The household tops up a vacuum container, so the container is the one inventory. An add puts grams
in; adding other beans switches the container to them, with what was left counted into the new beans.
Every brew that ground debits its dose once. The remaining grams may go below zero, which is shown as
zero, and are unknown until the first add. An add can be undone for UNDO_S: the container goes back
to exactly what it was before it, with the debits made since applied again.
"""

from datetime import datetime, timedelta

UNDO_S = 600
LOW_BREWS = 2
MAX_OPS = 40
MAX_ADD_G = 2000
NAME_MAX = 40
DEFAULT_NAME = "Coffee beans"


def _parse(value):
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None


def _clean_name(value):
    text = " ".join(str(value or "").encode("ascii", "ignore").decode("ascii").split())
    return text[:NAME_MAX].strip()


class Beans:
    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        container = data.get("container")
        self.container = dict(container) if isinstance(container, dict) else {}
        self.known = [dict(b) for b in data.get("known") or [] if isinstance(b, dict) and b.get("id")]
        self.ops = [dict(op) for op in data.get("ops") or [] if isinstance(op, dict) and op.get("id")]
        self.seq = int(data.get("seq") or 0)

    def to_dict(self):
        return {"container": self.container, "known": self.known, "ops": self.ops, "seq": self.seq}

    def _op_id(self, now):
        self.seq += 1
        return f"o{int(now.timestamp())}{self.seq % 100:02d}"

    def _name(self, bean_id):
        return next((b["name"] for b in self.known if b["id"] == bean_id), None)

    def _new_bean(self, name):
        number = 1 + max((int(b["id"][1:]) for b in self.known if b["id"][1:].isdigit()), default=0)
        bean = {"id": f"b{number}", "name": name}
        self.known.append(bean)
        return bean["id"]

    def _snapshot(self):
        return {"container": dict(self.container), "known": [dict(b) for b in self.known]}

    def _keep(self, op):
        self.ops = (self.ops + [op])[-MAX_OPS:]

    @property
    def remaining(self):
        value = self.container.get("remaining_g")
        return float(value) if isinstance(value, (int, float)) else None

    @property
    def bean_id(self):
        return self.container.get("bean_id")

    def bean(self):
        return {"id": self.bean_id, "name": self._name(self.bean_id)} if self.bean_id else None

    def add(self, grams, now, bean_id=None, new_name=None):
        """Puts `grams` in and returns the op id; None when the request is no valid add."""
        try:
            grams = float(grams)
        except (TypeError, ValueError):
            return None
        if not 0 < grams <= MAX_ADD_G:
            return None
        before = self._snapshot()
        name = _clean_name(new_name)
        if name:
            target = next((b["id"] for b in self.known if b["name"].lower() == name.lower()), None)
            target = target or self._new_bean(name)
        elif bean_id:
            if self._name(bean_id) is None:
                return None
            target = bean_id
        else:
            target = self.bean_id or self._new_bean(DEFAULT_NAME)
        leftover = max(0.0, self.remaining or 0.0)
        self.container = {"bean_id": target, "remaining_g": round(leftover + grams, 1),
                          "added_at": now.isoformat(timespec="seconds")}
        op_id = self._op_id(now)
        self._keep({"id": op_id, "kind": "add", "at": now.isoformat(timespec="seconds"), "grams": grams,
                    "before": before})
        return op_id

    def debit(self, brew_id, grams, now):
        """Takes a brew's dose out once; False when the brew was debited already."""
        if any(op.get("kind") == "debit" and op.get("brew_id") == brew_id for op in self.ops):
            return False
        applied = self.remaining is not None and grams is not None
        if applied:
            self.container["remaining_g"] = round(self.remaining - float(grams), 1)
        self._keep({"id": self._op_id(now), "kind": "debit", "at": now.isoformat(timespec="seconds"),
                    "brew_id": brew_id, "grams": grams, "applied": applied})
        return True

    def undoable(self, now):
        """The newest add that can still be undone, with its deadline: (op, until) or (None, None)."""
        for op in reversed(self.ops):
            if op.get("kind") != "add":
                continue
            at = _parse(op.get("at"))
            until = at + timedelta(seconds=UNDO_S) if at else None
            return (op, until) if until and now <= until else (None, None)
        return None, None

    def undo(self, op_id, now):
        op, _ = self.undoable(now)
        if op is None or op["id"] != op_id:
            return False
        index = self.ops.index(op)
        later = [o for o in self.ops[index + 1:] if o.get("kind") == "debit"]
        self.container = dict(op["before"].get("container") or {})
        self.known = [dict(b) for b in op["before"].get("known") or []]
        self.ops = self.ops[:index]
        for debit in later:
            applied = self.remaining is not None and debit.get("grams") is not None
            if applied:
                self.container["remaining_g"] = round(self.remaining - float(debit["grams"]), 1)
            self._keep({**debit, "applied": applied})
        return True

    def view(self, dose_g, now):
        """What sensor.coffee_beans shows, in numbers."""
        out = {"known_beans": [{"id": b["id"], "name": b["name"]} for b in self.known], "dose_g": dose_g}
        remaining = self.remaining
        if self.bean_id:
            out.update(bean_id=self.bean_id, bean_name=self._name(self.bean_id) or "")
        if self.container.get("added_at"):
            out["added_at"] = self.container["added_at"]
        if remaining is None:
            out["remaining"] = None
        else:
            shown = max(0.0, remaining)
            out["remaining"] = int(round(shown))
            if dose_g:
                left = int(shown // dose_g)
                out["brews_left"] = left
                out["low"] = left <= LOW_BREWS
        op, until = self.undoable(now)
        if op is not None:
            out["last_op_id"], out["undo_until"] = op["id"], until.isoformat(timespec="seconds")
        return out
