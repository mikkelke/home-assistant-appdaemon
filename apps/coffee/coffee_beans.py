"""The open bags and about how many grams of each bean are left. Pure.

An add opens or tops up a bag of that bean and closes other bags without a full dose left. A brew
that ground debits its bag once. Negative estimates are shown as zero. Undo restores the bags before
the add and reapplies subsequent debits and manual closures.
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


def _load_bags(data):
    bags = data.get("bags", [data.get("container")])
    return [dict(bag) for bag in bags or [] if isinstance(bag, dict) and bag.get("bean_id")]


def _remaining(bag):
    value = (bag or {}).get("remaining_g")
    return float(value) if isinstance(value, (int, float)) else None


class Beans:
    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        self.bags = _load_bags(data)
        self.known = [dict(b) for b in data.get("known") or [] if isinstance(b, dict) and b.get("id")]
        self.ops = [dict(op) for op in data.get("ops") or [] if isinstance(op, dict) and op.get("id")]
        if "bags" not in data:
            self._load_legacy_ops()
        self.seq = int(data.get("seq") or 0)
        self.bean_seq = max([int(data.get("bean_seq") or 0)] +
                            [int(b["id"][1:]) for b in self.known if b["id"][1:].isdigit()])

    def _load_legacy_ops(self):
        """Recover each old debit's bean from the container in use at that point."""
        bean_id = self.bags[0]["bean_id"] if self.bags else None
        for op in reversed(self.ops):
            if op.get("kind") == "debit" and op.get("applied"):
                op["bean_id"] = bean_id
            elif op.get("kind") == "add":
                op["bean_id"] = bean_id
                before = op.get("before") or {}
                bean_id = (before.get("container") or {}).get("bean_id")

    def to_dict(self):
        return {"bags": self.bags, "known": self.known, "ops": self.ops, "seq": self.seq, "bean_seq": self.bean_seq}

    def _op_id(self, now):
        self.seq += 1
        return f"o{int(now.timestamp())}{self.seq % 100:02d}"

    def _name(self, bean_id):
        return next((b["name"] for b in self.known if b["id"] == bean_id), None)

    def _named(self, name):
        name = _clean_name(name).lower()
        return next((b["id"] for b in self.known if _clean_name(b["name"]).lower() == name), None) if name else None

    def _new_bean(self, name):
        """Give the bean an id that cannot be reused by a later add."""
        self.bean_seq += 1
        bean = {"id": f"b{self.bean_seq}", "name": name}
        self.known.append(bean)
        return bean["id"]

    def _snapshot(self):
        return {"bags": [dict(bag) for bag in self.bags], "known": [dict(b) for b in self.known]}

    def _keep(self, op):
        self.ops = (self.ops + [op])[-MAX_OPS:]

    def _record(self, kind, now, **fields):
        op_id = self._op_id(now)
        self._keep({"id": op_id, "kind": kind, "at": now.isoformat(timespec="seconds"), **fields})
        return op_id

    def bag(self, bean_id):
        return next((bag for bag in self.bags if bag["bean_id"] == bean_id), None)

    def bean(self, bean_id):
        return {"id": bean_id, "name": self._name(bean_id)} if self.bag(bean_id) is not None else None

    def for_recipe(self, recipe_name):
        """The recipe's named bean, or the only open bag when its name does not match."""
        match = self.bean(self._named(recipe_name))
        return match or (self.bean(self.bags[0]["bean_id"]) if len(self.bags) == 1 else None)

    def primary(self, recipe_name):
        """The usual recipe's named bean, or the most recently added bag."""
        return self.for_recipe(recipe_name) or (self.bean(self.bags[0]["bean_id"]) if self.bags else None)

    def recipe_for(self, bean_id, recipes):
        """The recipe named for this open bag, if there is one."""
        bean = self.bean(bean_id)
        name = _clean_name(bean["name"]).lower() if bean else ""
        return next((r for r in recipes if _clean_name(r["name"]).lower() == name), None) if name else None

    def _has_dose(self, bag, dose_g, recipes):
        recipe = self.recipe_for(bag["bean_id"], recipes)
        dose = (recipe or {}).get("dose_g") or dose_g
        remaining = _remaining(bag)
        if remaining is None:
            return True
        return remaining >= dose if dose else remaining > 0

    def add(self, grams, now, bean_id=None, new_name=None, dose_g=None, recipes=(), default_bean_id=None):
        """Puts `grams` in the chosen bag and returns the op id, or None for an invalid add."""
        try:
            grams = float(grams)
        except (TypeError, ValueError):
            return None
        if not 0 < grams <= MAX_ADD_G:
            return None
        before = self._snapshot()
        name = _clean_name(new_name)
        if name:
            target = self._named(name) or self._new_bean(name)
        elif bean_id:
            if self._name(bean_id) is None:
                return None
            target = bean_id
        else:
            target = default_bean_id or (self.primary(None) or {}).get("id")
            target = target or self._named(DEFAULT_NAME) or self._new_bean(DEFAULT_NAME)
        leftover = max(0.0, _remaining(self.bag(target)) or 0.0)
        bag = {"bean_id": target, "remaining_g": round(leftover + grams, 1),
               "added_at": now.isoformat(timespec="seconds")}
        self.bags = [bag] + [other for other in self.bags
                             if other["bean_id"] != target and self._has_dose(other, dose_g, recipes)]
        return self._record("add", now, grams=grams, bean_id=target, before=before)

    def empty(self, bean_id, now):
        """Closes an open bag and records the correction."""
        bag = self.bag(bean_id)
        if bag is None:
            return False
        self.bags.remove(bag)
        self._record("empty", now, bean_id=bean_id)
        return True

    def _apply_debit(self, bean_id, grams):
        bag = self.bag(bean_id)
        remaining = _remaining(bag)
        if remaining is None or grams is None:
            return False
        bag["remaining_g"] = round(remaining - float(grams), 1)
        return True

    def debit(self, brew_id, grams, now, bean_id=None):
        """Takes a brew's dose out of its bag once. True when grams were taken."""
        if any(op.get("kind") == "debit" and op.get("brew_id") == brew_id for op in self.ops):
            return False
        applied = self._apply_debit(bean_id, grams)
        self._record("debit", now, brew_id=brew_id, grams=grams, bean_id=bean_id, applied=applied)
        return applied

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
        later = self.ops[index + 1:]
        before = op["before"]
        self.bags = _load_bags(before)
        self.known = [dict(b) for b in before.get("known") or []]
        self.ops = self.ops[:index]
        for change in later:
            bean_id = change.get("bean_id")
            if change.get("kind") == "debit":
                change = {**change, "applied": self._apply_debit(bean_id, change.get("grams"))}
            elif change.get("kind") == "empty":
                bag = self.bag(bean_id)
                if bag is not None:
                    self.bags.remove(bag)
            self._keep(change)
        return True

    def _bag_view(self, bag, dose_g):
        out = {"remaining": None}
        if bag is not None:
            out.update(bean_id=bag["bean_id"], bean_name=self._name(bag["bean_id"]) or "", added_at=bag.get("added_at"))
        remaining = _remaining(bag)
        if remaining is not None:
            shown = max(0.0, remaining)
            out["remaining"] = int(round(shown))
            if dose_g:
                left = int(shown // dose_g)
                out["brews_left"] = left
                out["low"] = left <= LOW_BREWS
        return out

    def view(self, dose_g, now, usual_name=None, recipes=()):
        """What sensor.coffee_beans shows, in numbers."""
        primary = self.primary(usual_name)
        out = self._bag_view(self.bag((primary or {}).get("id")), dose_g)
        out["dose_g"] = dose_g
        out["known_beans"] = [{"id": b["id"], "name": b["name"]} for b in self.known]
        out["bags"] = []
        for bag in self.bags:
            recipe = self.recipe_for(bag["bean_id"], recipes) or {}
            row = self._bag_view(bag, recipe.get("dose_g") or dose_g)
            row["recipe_id"] = recipe.get("id") or ""
            out["bags"].append(row)
        op, until = self.undoable(now)
        if op is not None:
            out["last_op_id"], out["undo_until"] = op["id"], until.isoformat(timespec="seconds")
        return out
