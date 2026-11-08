import copy

from cook_predict import add_sample, bath_eta, number, probe_eta, slope


PRESETS = [
    {"key": "beef", "serve": 54, "pull": 50, "rest": 15},
    {"key": "lamb", "serve": 58, "pull": 54, "rest": 15},
    {"key": "pork", "serve": 63, "pull": 60, "rest": 15},
    {"key": "chicken", "serve": 66, "pull": 64, "rest": 10},
    {"key": "duck", "serve": 54, "pull": 51, "rest": 10},
    {"key": "fish", "serve": 50, "pull": 48, "rest": 5},
    {"key": "custom", "serve": 54, "pull": 51, "rest": 15},
]
BATH_PHASES = ("off", "heating", "too_hot", "ready", "cooking", "done", "chilling", "chilled", "interrupted")
PROBE_PHASES = ("off", "tracking", "near", "pull", "resting", "carve")


def _bath():
    return {"phase": "off", "last_target": 58, "last_minutes": 150, "low_water": False,
            "_samples": [], "_pushed": [], "_suppressed": False}


def _probe():
    return {"phase": "off", "preset": "beef", "serve": 54, "pull": 50, "rest": 15,
            "in_meat": False, "lost": False, "_samples": [], "_rest_samples": [],
            "_pushed": [], "shadow": [], "_suppressed": False}


def _valid_saved(data, defaults, phases):
    if not isinstance(data, dict) or data.get("phase") not in phases:
        return defaults
    restored = copy.deepcopy(defaults)
    restored.update(copy.deepcopy(data))
    for key in ("_samples", "_rest_samples"):
        if key in restored:
            rows = restored[key] if isinstance(restored[key], list) else []
            restored[key] = [[number(value) for value in row] for row in rows if isinstance(row, (list, tuple))
                             and len(row) == 2 and all(number(value) is not None for value in row)][-100:]
    for key in ("_pushed", "shadow"):
        if key in restored and not isinstance(restored[key], list):
            restored[key] = []
    if restored["phase"] != "off" and (not isinstance(restored.get("id"), str)
                                       or number(restored.get("start")) is None):
        return defaults
    return restored


class Tracker:
    def __init__(self, saved=None):
        saved = saved if isinstance(saved, dict) else {}
        self.bath = _valid_saved(saved.get("bath"), _bath(), BATH_PHASES)
        self.probe = _valid_saved(saved.get("probe"), _probe(), PROBE_PHASES)

    def to_dict(self):
        return copy.deepcopy({"bath": self.bath, "probe": self.probe})

    def _push(self, lane, kind, title, message="", actions=(), wake=False, repeat=False):
        if kind in lane["_pushed"] and not repeat:
            return []
        lane["_pushed"].append(kind)
        return [{"type": "push", "kind": kind, "id": lane.get("id", "cook_alarm"),
                 "title": title, "message": message,
                 "actions": [{"action": action, "title": label} for action, label in actions], "wake": wake}]

    def _new_bath(self, now):
        old = self.bath
        self.bath = _bath()
        for key in ("last_target", "last_minutes", "temp", "target", "low_water", "_mode", "_last_rate",
                    "_low_since", "_low_repeated"):
            if key in old:
                self.bath[key] = old[key]
        self.bath.update(id=f"bath{int(now * 1000)}", start=now, phase="heating")

    def _new_probe(self, now):
        old = self.probe
        self.probe = _probe()
        for key in ("preset", "serve", "pull", "rest"):
            self.probe[key] = old[key]
        self.probe.update(id=f"probe{int(now * 1000)}", start=now, phase="tracking")

    def _close(self, kind, now):
        lane = getattr(self, kind)
        if lane["phase"] == "off":
            return []
        record = {key: copy.deepcopy(value) for key, value in lane.items() if not key.startswith("_")}
        record.update(kind=kind, end=now, outcome=lane["phase"])
        if kind == "bath":
            old = lane
            if number(old.get("rate")) is not None and old["rate"] > 0.1:
                old["_last_rate"] = old["rate"]
            self.bath = _bath()
            for key in ("last_target", "last_minutes", "_last_rate", "temp", "target", "low_water", "_mode",
                        "_missing_since", "_low_since", "_low_repeated"):
                if key in old:
                    self.bath[key] = old[key]
            self.bath["_suppressed"] = True
        else:
            self.probe = _probe()
            for key in ("preset", "serve", "pull", "rest", "_missing_since"):
                if key in lane:
                    self.probe[key] = lane[key]
            self.probe["_suppressed"] = True
        return [{"type": "closed", "record": record}]

    def bath_reading(self, now, climate_state, temp, target, low_water):
        bath = self.bath
        effects = []
        was_low = bath["low_water"]
        bath["low_water"] = bool(low_water)
        if low_water and not was_low:
            bath.update(_low_since=now, _low_repeated=False)
            bath["_pushed"] = [kind for kind in bath["_pushed"] if kind != "low_water"]
            effects += self._push(bath, "low_water", "Anova low water", "Top up the bath.",
                                  (("COOK_SILENCE", "Silence"),), wake=True)
        elif not low_water:
            bath.pop("_low_since", None)
            bath["_low_repeated"] = False
        mode_before = bath.get("_mode")
        if climate_state not in ("heat", "off"):
            bath.setdefault("_missing_since", now)
            return effects + self.tick(now)
        missing_since = bath.pop("_missing_since", None)
        if (missing_since is not None and now - missing_since > 600 and bath["phase"] == "cooking"
                and missing_since < bath["done_at"]):
            bath.update(phase="interrupted", stopped_at=missing_since)
        bath["_mode"] = climate_state
        temp, target = number(temp), number(target)
        if temp is not None:
            bath["temp"] = temp
        if target is not None:
            bath.update(target=target, last_target=target)
        if climate_state == "heat":
            if bath["phase"] == "off" and (not bath["_suppressed"] or mode_before == "off"):
                self._new_bath(now)
                bath = self.bath
            if temp is not None and bath["phase"] != "off":
                add_sample(bath["_samples"], now, temp, 300)
            effects += self._bath_temperature(now)
        elif bath["phase"] == "cooking" and now < bath["done_at"]:
            bath.update(phase="interrupted", stopped_at=now)
        elif bath["phase"] in ("heating", "too_hot", "ready", "done"):
            effects += self._close("bath", now)
        return effects + self.tick(now)

    def _bath_temperature(self, now):
        bath = self.bath
        if bath["phase"] not in ("heating", "too_hot", "ready"):
            return []
        current, target = bath.get("temp"), bath.get("target")
        if current is None or target is None:
            return []
        previous = bath["phase"]
        difference = current - target
        if abs(difference) <= 0.5:
            bath.update(phase="ready")
            if previous != "ready":
                bath["ready_at"] = now
            measured = slope(bath["_samples"], now, 300, count=4)
            if measured is not None and measured > 0.1:
                bath["_last_rate"] = measured
                bath["rate"] = measured
            return self._push(bath, "bath_ready", f"Bath at {target:g}° · bag in",
                              actions=(("COOK_BAG_IN", "Bag in"),), wake=True)
        if difference > 1.5 or (previous == "too_hot" and difference > 0.5):
            bath.update(phase="too_hot")
            bath.pop("ready_at", None)
        elif difference < -0.5:
            bath["phase"] = "heating"
            eta, rate = bath_eta(bath["_samples"], now, current, target, bath.get("_last_rate"))
            bath["ready_at"], bath["rate"] = eta, rate
        elif previous == "heating":
            bath.pop("ready_at", None)
        return []

    def probe_reading(self, now, core, ambient, needles):
        probe = self.probe
        core, ambient = number(core), number(ambient)
        needles = [number(value) for value in (list(needles or []) + [None] * 4)[:4]]
        if probe["phase"] in ("resting", "carve"):
            index = probe.get("core_index")
            core = needles[index] if isinstance(index, int) and 0 <= index < 4 else None
        if core is None:
            probe.setdefault("_missing_since", now)
            if probe["phase"] != "off":
                probe.setdefault("stale_since", now)
                probe["_meat_since"] = None
                probe["_rest_samples"] = []
                probe["eta"] = None
            return self.tick(now)
        previous_reading = probe.get("_reading_at")
        if previous_reading is not None and now - previous_reading > 90:
            probe["_meat_since"] = None
            probe["_samples"] = []
            probe["_rest_samples"] = []
        missing_since = probe.pop("_missing_since", None)
        effects = []
        if missing_since is not None and now - missing_since >= 600:
            effects += self._close("probe", missing_since + 600)
            self.probe["_suppressed"] = False
            probe = self.probe
        if probe["phase"] == "off":
            if probe["_suppressed"]:
                return effects
            self._new_probe(now)
            probe = self.probe
        probe.pop("stale_since", None)
        probe["lost"] = False
        if missing_since is not None:
            probe["_meat_since"] = None
            probe["_samples"] = []
            probe["_rest_samples"] = []
        probe.update(core=core, ambient=ambient, needle=needles, _reading_at=now)
        if probe["phase"] in ("tracking", "near", "pull"):
            valid = [(value, index) for index, value in enumerate(needles) if value is not None]
            probe["core_index"] = min(valid)[1] if valid else None
            if not probe["in_meat"]:
                if ambient is not None and ambient - core >= 3:
                    if probe.get("_meat_since") is None:
                        probe["_meat_since"] = now
                    if now - probe["_meat_since"] >= 30:
                        probe["in_meat"] = True
                else:
                    probe["_meat_since"] = None
            add_sample(probe["_samples"], now, core, 600)
            probe["eta"], probe["slope"] = probe_eta(probe["_samples"], now, core, probe["pull"])
            if probe["phase"] != "pull":
                if core >= probe["pull"]:
                    probe.update(phase="pull", _pull_at=now)
                elif core >= probe["pull"] - 3 or (probe["eta"] is not None and probe["eta"] - now <= 300):
                    probe["phase"] = "near"
                else:
                    probe["phase"] = "tracking"
            if probe["phase"] == "pull" and probe["in_meat"]:
                effects += self._push(probe, "pull", f"Pull the roast now · {core:.1f}°",
                                      f"Rests to about {probe['serve']:g}°",
                                      (("COOK_PULLED", "Pulled"),), wake=True)
        elif probe["phase"] in ("resting", "carve"):
            probe["peak"] = max(probe.get("peak", core), core)
            add_sample(probe["_rest_samples"], now, core, 150)
        return effects + self.tick(now)

    def tick(self, now):
        effects = []
        bath, probe = self.bath, self.probe
        if (bath["low_water"] and bath.get("_low_since") is not None and not bath.get("_low_repeated")
                and now - bath["_low_since"] >= 600):
            bath["_low_repeated"] = True
            effects += self._push(bath, "low_water", "Anova still low on water", "Top up the bath.",
                                  (("COOK_SILENCE", "Silence"),), repeat=True)
        if bath["phase"] == "cooking":
            missing = bath.get("_missing_since")
            if missing is not None and now - missing > 600 and missing < bath["done_at"]:
                bath.update(phase="interrupted", stopped_at=missing)
            elif now >= bath["done_at"] and missing is None:
                bath["phase"] = "done"
                effects += self._push(bath, "bath_done", f"Sous vide done · holding {bath.get('target', bath['last_target']):g}°",
                                      actions=(("COOK_ICE", "Ice water"), ("COOK_STOP", "Stop")), wake=True)
        elif bath["phase"] == "chilling" and now >= bath["chill_until"]:
            bath["phase"] = "chilled"
            effects += self._push(bath, "chill_done", "Into the fridge")
        elif bath["phase"] == "heating" and bath.get("_missing_since") is None:
            effects += self._bath_temperature(now)
        missing = probe.get("_missing_since")
        if missing is not None and now - missing >= 600:
            effects += self._close("probe", missing + 600)
            self.probe["_suppressed"] = False
            probe = self.probe
        elif (missing is not None and now - missing > 90
              and probe["phase"] in ("tracking", "near", "pull")):
            probe["lost"] = True
            if probe["phase"] != "pull" and probe["in_meat"]:
                effects += self._push(probe, "lost", "Probe reading lost", "Check the probe connection.")
        if probe["phase"] == "resting":
            rest = probe["_rest_samples"]
            before = [row for row in rest if row[0] <= now - 120]
            plateau = (missing is None and before and rest[-1][0] >= now - 10
                       and rest[-1][1] - before[-1][1] < 0.05)
            if now >= probe["carve_at"] or (now - probe["pulled_at"] >= 120 and plateau):
                probe.update(phase="carve", _carved_at=now)
                if probe["in_meat"]:
                    effects += self._push(probe, "carve", f"Carve · peaked {probe['peak']:.1f}°", wake=True)
                if not probe.get("_reported"):
                    probe["_reported"] = True
                    effects.append({"type": "report", "text": f"Roast rested · peaked {probe['peak']:.1f}°"})
        if probe["phase"] != "off" and now - probe.get("_shadow_at", now - 60) >= 60:
            probe["shadow"].append({"at": now, "core": probe.get("core"), "eta": probe.get("eta")})
            probe["_shadow_at"] = now
        return effects

    def command(self, op, data, now):
        data = data if isinstance(data, dict) else {}
        bath, probe = self.bath, self.probe
        ignored = [{"type": "ignored", "op": op}]
        effects = [{"type": "state"}]
        if op == "bath_start":
            target = number(data.get("target", bath["last_target"]))
            if target is None or not 0 < target <= 100 or bath["phase"] not in ("off", "heating", "too_hot", "ready"):
                return ignored
            bath.update(last_target=target, _suppressed=False)
            return [{"type": "service", "op": op, "target": target}]
        if op == "bath_stop":
            if bath["phase"] == "off" and bath.get("_mode") != "heat":
                return ignored
            return [{"type": "service", "op": op}]
        if op == "silence":
            if not bath["low_water"]:
                return ignored
            return [{"type": "service", "op": op}]
        if op == "bag_in":
            minutes = number(data.get("minutes", bath["last_minutes"]))
            if bath["phase"] not in ("heating", "too_hot", "ready") or minutes is None or not 0 < minutes <= 2880:
                return ignored
            bath.update(phase="cooking", in_at=now, minutes=minutes, done_at=now + minutes * 60,
                        last_minutes=minutes, in_temp=bath.get("temp"), in_target=bath.get("target"))
            return effects
        if op == "bath_extend":
            minutes = number(data.get("minutes"))
            if bath["phase"] not in ("cooking", "done") or minutes is None or not 0 < minutes <= 2880:
                return ignored
            bath["done_at"] += minutes * 60
            bath["minutes"] += minutes
            if bath["done_at"] > now:
                bath["phase"] = "cooking"
            return effects + self.tick(now)
        if op == "chill":
            if bath["phase"] not in ("done", "interrupted"):
                return ignored
            bath.update(phase="chilling", chill_until=now + 900)
            return effects
        if op == "bath_clear":
            return self._close("bath", now) if bath["phase"] != "off" else ignored
        if op == "probe_preset":
            if probe["phase"] not in ("off", "tracking", "near"):
                return ignored
            preset = next((preset for preset in PRESETS if preset["key"] == data.get("key")), None)
            if preset is None:
                return ignored
            preset = dict(preset)
            if preset["key"] == "custom":
                serve = number(data.get("serve"))
                if serve is None or not 3 < serve <= 100:
                    return ignored
                preset.update(serve=serve, pull=serve - 3)
            probe.update(preset=preset["key"], serve=preset["serve"], pull=preset["pull"], rest=preset["rest"])
            return effects
        if op == "probe_pulled":
            if probe["phase"] != "pull" or probe.get("stale_since") is not None:
                return ignored
            index = probe.get("core_index")
            if not isinstance(index, int):
                return ignored
            core = probe["needle"][index]
            if core is None:
                return ignored
            probe.update(phase="resting", pulled_at=now, pulled_core=core, core=core, peak=core,
                         carve_at=now + probe["rest"] * 60, eta=None, _rest_samples=[[now, core]])
            return effects
        if op == "probe_done":
            return self._close("probe", now) if probe["phase"] != "off" else ignored
        return ignored
