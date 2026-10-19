"""The lifecycle of a brew, from what the integration, the scale and the plug report. Pure.

A brew is `starting` from our start op until the machine accepts it (`grinding`) or refuses it
(`start_failed`, or `no_answer` after START_TIMEOUT_S). A brew nobody here started begins at
`grinding` when the brew status or a machine-start event says so, or at `brewing` when the plug shows
a grind followed by heat (source `power`: a grind alone is not a brew, so the plug cannot tell one any
earlier). The first bloom makes it `brewing`; the pause sensor makes it `paused` and back. ENJOY or the
integration's completion makes it `ready`; the cup lifted off the scale (under max(PICKUP_MIN_G,
PICKUP_SHARE of the final weight) for PICKUP_HOLD_S on a fresh link) makes it `picked_up`. A stop, a
failure after the start, the brew status back at idle without ENJOY, or a fault the machine stops for
makes it `stopped`. A plug-only brew is ready when the plug says it ended complete, else stopped. A
Bluetooth brew whose link went quiet is closed by the plug's ending after POWER_GRACE_S. Each finished
state lingers for LIFETIME_S, then the state is `idle` or `asleep` again.

Every input returns the effects the app acts on: "started", "bloom", "ready", "closed",
"start_failed", "picked_up", "expired", "fault", "source", "state" (publish sensor.coffee_state) and
"live" (publish sensor.coffee_live). Times are epoch seconds.
"""

from datetime import datetime, timezone

from coffee_curve import FRESH_S, CupFilter, Curve

START_TIMEOUT_S = 45
LIFETIME_S = {"start_failed": 60, "stopped": 300, "ready": 1800, "picked_up": 300}
ACTIVE = ("starting", "grinding", "brewing", "paused")
RUNNING = ("grinding", "brewing", "paused")
BUSY = ACTIVE + ("ready",)
PICKUP_MIN_G = 20.0
PICKUP_SHARE = 0.25
PICKUP_HOLD_S = 3.0
RESUME_MAX_S = 1200
POWER_GRACE_S = 120
STOP_ASKED_S = 30
STOP_FAULTS = ("no_beans",)
FAULTS = ("no_water", "no_beans", "dose_water_error", "gear_position_error")


def iso(ts):
    return None if ts is None else datetime.fromtimestamp(ts, timezone.utc).isoformat(timespec="seconds")


def epoch(value):
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()


def brew_id_for(ts):
    return f"c{int(ts)}"


class Tracker:
    def __init__(self):
        self.brew = None
        self.last = None
        self.curve = None
        self.filter = CupFilter()
        self.cup = None
        self.water = None
        self.frame_at = None
        self.below_since = None

    # -- persistence ------------------------------------------------------------------------

    def to_dict(self):
        return {"brew": self.brew, "last": self.last, "curve": self.curve.to_dict() if self.curve else None,
                "cup": self.cup, "water": self.water, "frame_at": self.frame_at}

    @classmethod
    def from_dict(cls, data):
        tracker = cls()
        if not isinstance(data, dict):
            return tracker
        tracker.brew = data.get("brew") if isinstance(data.get("brew"), dict) else None
        tracker.last = data.get("last") if isinstance(data.get("last"), dict) else None
        if isinstance(data.get("curve"), dict):
            try:
                tracker.curve = Curve.from_dict(data["curve"])
            except (KeyError, TypeError, ValueError):
                tracker.curve = None
        tracker.cup, tracker.water, tracker.frame_at = data.get("cup"), data.get("water"), data.get("frame_at")
        return tracker

    # -- reading ----------------------------------------------------------------------------

    def phase(self):
        return self.brew["phase"] if self.brew else None

    def active(self):
        return self.phase() in ACTIVE

    def busy(self):
        return self.phase() in BUSY

    def fresh(self, now):
        return self.frame_at is not None and now - self.frame_at <= FRESH_S

    def _fresh_until(self):
        return None if self.frame_at is None else self.frame_at + FRESH_S

    def reading_cup(self, now, grams):
        self.frame_at = now
        kept = self.filter.accept(grams)
        if kept is not None:
            self.cup = kept
        return self._pickup(now) if self.phase() == "ready" else []

    def reading_water(self, now, ml):
        self.frame_at = now
        self.water = ml

    def frame(self, now):
        self.frame_at = now

    def _mine(self, brew_id, run_id):
        if self.brew is None:
            return False
        if brew_id is not None:
            return brew_id == self.brew["id"]
        return run_id is not None and run_id == self.brew.get("run_id")

    # -- transitions --------------------------------------------------------------------------

    def _new(self, brew_id, phase, source, now, **fields):
        if self.brew is not None:
            self.last = self.brew
        self.brew = {"id": brew_id, "phase": phase, "phase_ts": now, "source": source, "faults": [], "pour": 0,
                     "ground": False, "requested_ts": now if phase == "starting" else None,
                     "started_ts": None if phase == "starting" else now, **fields}
        self.curve = None
        self.cup = self.water = None
        self.filter = CupFilter()
        self.below_since = None

    def _set(self, phase, now, **fields):
        self.brew.update(phase=phase, phase_ts=now, **fields)

    def request(self, brew_id, now, source, **fields):
        if self.active():
            return []
        self._new(brew_id, "starting", source, now, **fields)
        return ["state"]

    def _grinding(self, now):
        self._set("grinding", now, started_ts=now)
        self.cup = self.water = None
        self.filter = CupFilter()
        return ["started", "state", "live"]

    def _note(self, run_id, recipe_name):
        if run_id and not self.brew.get("run_id"):
            self.brew["run_id"] = run_id
        if recipe_name and not self.brew.get("recipe_name"):
            self.brew["recipe_name"] = recipe_name

    def accepted(self, brew_id, run_id, recipe_name, now):
        """xbloom_brew_started: the machine took a brew. Ours when it carries our brew id."""
        if brew_id is not None and self._mine(brew_id, None):
            self._note(run_id, recipe_name)
            return self._grinding(now) if self.phase() == "starting" else []
        if self.phase() in RUNNING:
            self._note(run_id, recipe_name)
            return []
        if self.phase() == "starting":
            return []
        self._new(brew_id_for(now), "grinding", "ha", now, run_id=run_id, recipe_name=recipe_name)
        return ["started", "state", "live"]

    def refused(self, brew_id, run_id, reason, now):
        """xbloom_brew_failed: before the start a refusal, after it a failure."""
        if not self._mine(brew_id, run_id):
            return []
        if self.phase() == "starting":
            self._set("start_failed", now, start_error=reason or "no_reply")
            return ["start_failed", "state"]
        if self.phase() in RUNNING:
            if reason in FAULTS and reason not in self.brew["faults"]:
                self.brew["faults"].append(reason)
            return self._stop(now, "fault" if reason in FAULTS else "machine", "failed")
        return []

    def grinding_seen(self, now, source="machine"):
        """The brew status at grinding, or a machine-start event."""
        if self.phase() == "starting":
            return self._grinding(now)
        if self.active():
            if source == "machine" and self.brew["source"] == "power":
                self.brew["source"] = "machine"
                return ["source", "state"]
            return []
        self._new(brew_id_for(now), "grinding", source, now)
        return ["started", "state", "live"]

    def bloom(self, now):
        effects = []
        if self.phase() == "starting":
            effects += self._grinding(now)
        if self.phase() != "grinding":
            return effects
        self._set("brewing", now, bloom_ts=now, ground=True, pour=1)
        self.curve = Curve(now)
        self.curve.pour(now, 0)
        return effects + ["bloom", "state", "live"]

    def pour(self, now, index):
        effects = self.bloom(now) if index == 0 else []
        if self.phase() in ("brewing", "paused") and self.curve is not None and self.curve.pour(now, index):
            self.brew["pour"] = index + 1
            if "state" not in effects:
                effects.append("state")
        return effects

    def paused(self, on, now):
        if on and self.phase() in ("grinding", "brewing"):
            self._set("paused", now, paused_since_ts=now, resume_phase=self.phase())
            if self.curve is not None:
                self.curve.pause(now)
            return ["state", "live"]
        if not on and self.phase() == "paused":
            self._set(self.brew.pop("resume_phase", None) or "brewing", now, paused_since_ts=None)
            if self.curve is not None:
                self.curve.resume(now)
            return ["state", "live"]
        return []

    def brew_end(self, now):
        if self.phase() in RUNNING and not self.brew.get("brew_end_ts"):
            self.brew["brew_end_ts"] = now

    def enjoy(self, now):
        if self.phase() in RUNNING:
            return self._ready(now, enjoy=True)
        return []

    def completed(self, data, now):
        """xbloom_brew_completed: the integration's word that the brew finished."""
        data = data if isinstance(data, dict) else {}
        if data.get("outcome") not in ("confirmed", "presumed") or self.brew is None:
            return []
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        if not (self._mine(context.get("coffee_brew_id"), data.get("run_id")) or self.phase() in RUNNING):
            return []
        self.brew["completed"] = {k: data[k] for k in ("outcome", "first_pour_at", "ended_at", "duration_s", "grind",
                                                     "water_ml", "ratio", "dose_g", "recipe_name")
                                  if data.get(k) is not None}
        if self.phase() not in RUNNING:
            return []
        if not self.brew.get("bloom_ts") and epoch(data.get("first_pour_at")):
            self.brew["bloom_ts"] = epoch(data.get("first_pour_at"))
        ended = min(epoch(data.get("ended_at")) or now, now)
        return self._ready(ended, enjoy=data.get("outcome") == "confirmed")

    def _ready(self, now, enjoy):
        if self.curve is not None:
            self.curve.end(now, self.cup, self.water, self.fresh(now), self._fresh_until())
        self._set("ready", now, ready_ts=now, outcome="complete", ground=True, paused_since_ts=None)
        if enjoy:
            self.brew["enjoy_ts"] = now
        self.below_since = None
        return ["ready", "state", "live"]

    def stop_asked(self, now):
        """Our stop op. A start not yet taken is over at once; a running brew waits for the machine."""
        if self.phase() == "starting":
            return self._stop(now, "home_assistant")
        if self.phase() in RUNNING:
            self.brew["stop_asked_ts"] = now
        return []

    def stopped(self, brew_id, run_id, by, ground, now):
        """xbloom_brew_stopped."""
        if self.phase() not in RUNNING:
            return []
        if (brew_id is not None or (run_id and self.brew.get("run_id"))) and not self._mine(brew_id, run_id):
            return []
        if ground:
            self.brew["ground"] = True
        return self._stop(now, "home_assistant" if by in ("home_assistant", "superseded") else "machine")

    def status_idle(self, now):
        """The brew status back at idle without ENJOY."""
        if self.phase() in RUNNING and self.brew["source"] != "power":
            return self._stop(now, "machine")
        return []

    def fault(self, fault, now):
        if not self.active() or fault not in FAULTS:
            return []
        new = fault not in self.brew["faults"]
        if new:
            self.brew["faults"].append(fault)
        if fault in STOP_FAULTS and self.phase() in RUNNING:
            return self._stop(now, "fault", "failed")
        return ["fault", "state"] if new else []

    def _stop(self, now, reason, outcome="stopped"):
        if self.curve is not None:
            self.curve.end(now, self.cup, self.water, self.fresh(now), self._fresh_until())
        asked = self.brew.get("stop_asked_ts")
        if reason == "machine" and asked is not None and now - asked <= STOP_ASKED_S:
            reason = "home_assistant"
        self._set("stopped", now, stopped_ts=now, stop_reason=reason, outcome=outcome, paused_since_ts=None)
        return ["closed", "state", "live"]

    def abandon(self, now):
        """A brew the app could not follow to its end (a restart too late to resume, a timeout)."""
        if self.phase() == "starting":
            self._set("start_failed", now, start_error="no_answer")
            return ["start_failed", "state"]
        if self.phase() in RUNNING:
            return self._stop(now, "abandoned")
        return []

    def power(self, event):
        """An event of coffee_power.PowerBrew."""
        kind = event[0]
        if kind == "heating":
            start, heat = event[1], event[2]
            if self.active():
                return []
            self._new(brew_id_for(start), "brewing", "power", start, bloom_ts=heat, ground=True, pour=1)
            self.brew["phase_ts"] = heat
            return ["started", "bloom", "state", "live"]
        if kind != "ended" or self.phase() not in RUNNING:
            return []
        end, complete = event[2], event[4]
        if self.brew["source"] != "power":
            self.brew.update(power_end_ts=end, power_complete=bool(complete))
            return []
        if not complete:
            return self._stop(end, "machine")
        effects = self._ready(end, enjoy=False)
        self.brew["outcome"] = "unobserved"
        return effects

    def picked_up(self, brew_id, now):
        if self.phase() == "ready" and (brew_id is None or brew_id == self.brew["id"]):
            self._set("picked_up", now, picked_up_ts=now)
            return ["picked_up", "state"]
        return []

    def _pickup(self, now):
        final = (self.brew.get("final") or {}).get("cup_g")
        if final is None or self.cup is None or not self.fresh(now):
            self.below_since = None
            return []
        if self.cup < max(PICKUP_MIN_G, PICKUP_SHARE * final):
            if self.below_since is None:
                self.below_since = now
            elif now - self.below_since >= PICKUP_HOLD_S:
                return self.picked_up(None, now)
        else:
            self.below_since = None
        return []

    def tick(self, now):
        phase = self.phase()
        if phase is None:
            return []
        if phase == "starting" and now - (self.brew.get("requested_ts") or now) >= START_TIMEOUT_S:
            self._set("start_failed", now, start_error="no_answer")
            return ["start_failed", "state"]
        effects = []
        if phase in ("brewing", "paused") and self.curve is not None:
            self.curve.sample(now, self.cup, self.water, self.fresh(now), self._fresh_until())
            effects.append("live")
        power_end = self.brew.get("power_end_ts")
        if phase in RUNNING and power_end and not self.fresh(now) and now - power_end >= POWER_GRACE_S:
            return self._ready(power_end, enjoy=False) if self.brew.get("power_complete") else \
                self._stop(power_end, "abandoned")
        if phase == "ready":
            effects += self._pickup(now)
        lifetime = LIFETIME_S.get(self.phase())
        if lifetime is not None and now - self.brew["phase_ts"] >= lifetime:
            self.last, self.brew, self.curve = self.brew, None, None
            effects += ["expired", "state"]
        return effects
