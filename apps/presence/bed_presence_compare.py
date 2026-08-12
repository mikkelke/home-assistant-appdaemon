"""
BedPresenceCompare - the promotion evidence for the ESPHome bed-presence strip.

The strip (device bed_presence_6b9c94, an Elevated-Sensors-style pressure strip,
installed and hand-calibrated 2026-08-12) went live with zero nights of track
record, so it was wired into consumers as an ADDITIONAL WITNESS only: the Withings
mats (binary_sensor.left_bedside / right_bedside) stay authoritative. This app
produces the evidence that decides whether the strip ever gets promoted: it watches
each mat/strip pair, logs every disagreement with timestamps and durations, and
publishes sensor.bed_presence_agreement with per-side counters, edge-lag stats and
per-night records. After ~5 nights the attributes should answer "is the strip at
least as good as the mat" directly:

- {side}_mat_only ~ 0        the strip never misses an edge the mat sees
                             (mat-only counts are the strip failing to witness);
- {side}_strip_only          strip edges the mat never confirmed - NOT automatically
                             bad: the mats are known to miss getting-up far too often
                             (see bedroom_lights.py), so each record needs eyeballing;
- lag stats                  the local strip should LEAD the cloud mat (strip_led
                             high, positive average lag = seconds the follower took);
- nights[*] unavailable_s    a strip that flatlines unavailable overnight is not
                             ready, whatever its counters say.

Mechanics: a divergence between a pair opens an "episode" (who moved first =
opener). If the other sensor catches up within lag_match_window_seconds, that is an
agreed edge - edges_agreeing increments and the episode duration is the lag sample,
credited to whichever sensor led. If the opener reverts first, it was a blip only
one sensor saw - strip_only/mat_only increments. An episode still diverged past the
window is a real disagreement: counted once (by whichever sensor is asserting "on"),
logged at WARNING, and its eventual end logged with the full duration. Effective
state is exact "on" vs everything else; unavailable/unknown time is accrued per
sensor per night so a dead witness shows up as unavailable seconds, not as sleep.

Second-sleeper bookkeeping (design data only, wired into no decisions): the strip's
_bed_occupied_both is the household's first honest two-people-in-bed signal, so
both-occupied episodes and minutes are counted per night.

A "night" is any overlap with 21:00-09:00 local; nights_observed counts nights the
app was actually watching (a night the whole stack was down does not count as
evidence). All counters, night records and disagreement records persist across
restarts via the usual atomic tmp+replace state file (five nights of evidence must
not reset on a deploy).

Publish contract: AppDaemon 4.5.13 drops any attribute whose value equals
None/False/0 - recursively, lists and nested dicts included (utils.remove_literals;
see smart_cooling._publish for chapter and verse). Counters here legitimately sit
at 0 for exactly the sensors that are behaving, so every number in the published
attributes is a STRING ("0" survives, 0 vanishes). The state file keeps real ints.
"""

import json
import os
from datetime import datetime, time as dtime, timedelta

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

STATE_VERSION = 1

# Per-side cumulative counter keys (state file keeps ints/floats; publish stringifies).
_SIDE_COUNTERS = (
    "edges_agreeing",
    "strip_only",
    "mat_only",
    "strip_led",
    "mat_led",
    "lag_count",
    "lag_sum_s",
    "lag_max_s",
)
_SIDE_NIGHT_EXTRAS = (
    "strip_unavailable_s",
    "mat_unavailable_s",
)


def _in_window(t, start, end):
    """True when time-of-day t lies in [start, end), handling windows that wrap
    midnight (21:00-09:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _parse_hhmm(value, fallback):
    try:
        parts = [int(p) for p in str(value).split(":")]
        return dtime(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError, TypeError):
        return fallback


def _empty_side_counters():
    return {k: 0.0 if k.endswith("_s") else 0 for k in _SIDE_COUNTERS}


class BedPresenceCompare(hass.Hass):
    # Class-level defaults so bare __new__() test instances are well-defined -
    # same pattern as HouseNightMode/ActorAttribution in this directory.
    pairs = {
        "left": {
            "mat": "binary_sensor.left_bedside",
            "strip": "binary_sensor.bed_presence_6b9c94_bed_occupied_left",
        },
        "right": {
            "mat": "binary_sensor.right_bedside",
            "strip": "binary_sensor.bed_presence_6b9c94_bed_occupied_right",
        },
    }
    both_entity = "binary_sensor.bed_presence_6b9c94_bed_occupied_both"
    publish_entity = "sensor.bed_presence_agreement"
    lag_match_window_seconds = 600
    night_window = (dtime(21, 0), dtime(9, 0))
    state_file = "/conf/apps/presence/bed_presence_compare_state.json"
    max_night_records = 21
    max_disagreement_records = 20
    tick_seconds = 60

    def initialize(self):
        a = self.args.get
        raw_pairs = a("pairs")
        if raw_pairs:
            self.pairs = {
                str(side): {"mat": cfg["mat"], "strip": cfg["strip"]}
                for side, cfg in raw_pairs.items()
            }
        self.both_entity = a(
            "both_entity", "binary_sensor.bed_presence_6b9c94_bed_occupied_both"
        )
        self.publish_entity = a("publish_entity", "sensor.bed_presence_agreement")
        self.lag_match_window_seconds = int(a("lag_match_window_seconds", 600))
        self.night_window = (
            _parse_hhmm(a("night_window_start", "21:00"), dtime(21, 0)),
            _parse_hhmm(a("night_window_end", "09:00"), dtime(9, 0)),
        )
        self.state_file = a(
            "state_file", "/conf/apps/presence/bed_presence_compare_state.json"
        )
        self.max_night_records = int(a("max_night_records", 21))
        self.max_disagreement_records = int(a("max_disagreement_records", 20))
        self.tick_seconds = int(a("tick_seconds", 60))

        self._state = self._load_state()
        self._episodes = {side: None for side in self.pairs}
        self._last_accrual = None

        now = self._now_local()
        # Restart mid-divergence: open an opener-unknown episode so an existing
        # mismatch is still tracked (its lag is unknowable - no sample at close).
        for side in self.pairs:
            if self._effective(self._entity(side, "strip")) != self._effective(
                self._entity(side, "mat")
            ):
                self._open_episode(side, now, opener=None)

        for side, cfg in self.pairs.items():
            for role in ("mat", "strip"):
                self.listen_state(
                    self._on_pair_change, cfg[role], side=side, role=role
                )
        if self.both_entity:
            self.listen_state(self._on_both_change, self.both_entity)
        self.run_every(self._tick, f"now+{self.tick_seconds}", self.tick_seconds)

        self._publish(now)
        self.log(
            f"BedPresenceCompare: watching {self.pairs} + both={self.both_entity}, "
            f"nights_observed={self._state['nights_observed']}",
            level="INFO",
        )

    # ---------- config/state helpers ----------

    def _entity(self, side, role):
        return self.pairs[side][role]

    def _effective(self, entity):
        """Exact "on" is occupied; off/unavailable/unknown/None/read-error is not."""
        try:
            return self.get_state(entity) == "on"
        except Exception:
            return False

    def _raw(self, entity):
        try:
            return self.get_state(entity)
        except Exception:
            return None

    def _unreadable(self, entity):
        return self._raw(entity) in ("unavailable", "unknown", None)

    def _totals(self, side):
        return self._state["totals"][side]

    def _night(self):
        """The current (mutable) night record, or None when no night is open."""
        nights = self._state["nights"]
        key = self._state.get("current_night")
        if key and nights and nights[-1]["night"] == key:
            return nights[-1]
        return None

    # ---------- callbacks ----------

    def _on_pair_change(self, entity, attribute, old, new, kwargs):
        try:
            side, role = kwargs["side"], kwargs["role"]
            now = self._now_local()
            if new in ("unavailable", "unknown", None) or old in (
                "unavailable",
                "unknown",
                None,
            ):
                self.log(
                    f"BEDCMP {side} {role} {entity} availability change: "
                    f"{old!r} -> {new!r}",
                    level="INFO",
                )
            self._roll_night(now)
            self._compare(side, now, changed_role=role)
            self._save_state()
            self._publish(now)
        except Exception as e:
            self.log(f"BEDCMP pair-change handler failed: {e}", level="ERROR")

    def _on_both_change(self, entity, attribute, old, new, kwargs):
        try:
            now = self._now_local()
            self._roll_night(now)
            if new == "on":
                self._state["both"]["episodes_total"] += 1
                night = self._night()
                if night is not None:
                    night["both_episodes"] += 1
                self.log("BEDCMP both sides occupied (episode start)", level="INFO")
            elif old == "on":
                self.log("BEDCMP both-occupied episode ended", level="INFO")
            self._save_state()
            self._publish(now)
        except Exception as e:
            self.log(f"BEDCMP both-change handler failed: {e}", level="ERROR")

    def _tick(self, kwargs):
        try:
            now = self._now_local()
            changed = self._roll_night(now)
            changed |= self._accrue(now)
            for side in self.pairs:
                changed |= self._flag_stale_episode(side, now)
                # Belt and braces: a lost callback must not leave a phantom episode
                # open (or a real divergence untracked) forever.
                changed |= self._compare(side, now, changed_role=None)
            if changed:
                self._save_state()
            self._publish(now)
        except Exception as e:
            self.log(f"BEDCMP tick failed: {e}", level="ERROR")

    # ---------- night bookkeeping ----------

    def _night_key(self, now):
        """The date the night STARTED on, or None outside 21:00-09:00. A window
        wrapping midnight means times past the start belong to today's date and
        times before the end belong to yesterday's."""
        start, end = self.night_window
        t = now.time()
        if not _in_window(t, start, end):
            return None
        if start <= end or t >= start:
            return now.date().isoformat()
        return (now - timedelta(days=1)).date().isoformat()

    def _roll_night(self, now):
        """Open a night record when we first see activity inside a night window,
        and close it again at the first activity outside one (daytime events must
        not be credited to last night). nights_observed counts nights the
        comparator was actually watching."""
        key = self._night_key(now)
        if key is None:
            if self._state.get("current_night") is None:
                return False
            self._state["current_night"] = None
            self.log("BEDCMP night closed (window ended)", level="INFO")
            return True
        if key == self._state.get("current_night"):
            return False
        self._state["current_night"] = key
        self._state["nights_observed"] += 1
        record = {"night": key, "both_episodes": 0, "both_seconds": 0.0}
        for side in self.pairs:
            for counter in _SIDE_COUNTERS:
                record[f"{side}_{counter}"] = 0.0 if counter.endswith("_s") else 0
            for extra in _SIDE_NIGHT_EXTRAS:
                record[f"{side}_{extra}"] = 0.0
        self._state["nights"].append(record)
        self._state["nights"] = self._state["nights"][-self.max_night_records:]
        self.log(f"BEDCMP night {key} opened (nights_observed="
                 f"{self._state['nights_observed']})", level="INFO")
        return True

    def _accrue(self, now):
        """Tick-based accrual: both-occupied seconds, and unavailable seconds per
        sensor per night (a dead witness must show up as downtime, not as absence)."""
        last = self._last_accrual
        self._last_accrual = now
        if last is None:
            return False
        elapsed = (now - last).total_seconds()
        if elapsed <= 0:
            return False
        # A long scheduler gap (AD paused/suspended) must not credit hours at once.
        elapsed = min(elapsed, self.tick_seconds * 5)
        changed = False
        if self.both_entity and self._effective(self.both_entity):
            self._state["both"]["seconds_total"] += elapsed
            night = self._night()
            if night is not None:
                night["both_seconds"] += elapsed
            changed = True
        night = self._night()
        if night is not None:
            for side in self.pairs:
                for role in ("mat", "strip"):
                    if self._unreadable(self._entity(side, role)):
                        night[f"{side}_{role}_unavailable_s"] += elapsed
                        changed = True
        return changed

    # ---------- episode machinery ----------

    def _open_episode(self, side, now, opener):
        strip_raw = self._raw(self._entity(side, "strip"))
        mat_raw = self._raw(self._entity(side, "mat"))
        # opener_to: the effective state the opener diverged INTO. Classification
        # at close compares the final agreed state against it, so a lost callback
        # (tick-detected close) still classifies correctly.
        opener_to = None
        if opener is not None:
            opener_to = self._effective(self._entity(side, opener))
        self._episodes[side] = {
            "opened_at": now,
            "opener": opener,  # "strip" | "mat" | None (restart / missed edge)
            "opener_to": opener_to,
            "flagged": False,
        }
        self.log(
            f"BEDCMP {side} diverged: strip={strip_raw!r} mat={mat_raw!r} "
            f"(opener={opener}, at {now.isoformat()})",
            level="INFO",
        )

    def _compare(self, side, now, changed_role):
        """Reconcile one pair against its episode. Returns True when state-file
        content may have changed (an episode closed and counted something)."""
        strip_on = self._effective(self._entity(side, "strip"))
        mat_on = self._effective(self._entity(side, "mat"))
        ep = self._episodes.get(side)
        if strip_on == mat_on:
            if ep is not None:
                self._close_episode(side, ep, now, agreed_state=strip_on)
                self._episodes[side] = None
                return True
        else:
            if ep is None:
                self._open_episode(side, now, opener=changed_role)
        return False

    def _close_episode(self, side, ep, now, agreed_state):
        duration = (now - ep["opened_at"]).total_seconds()
        opened_iso = ep["opened_at"].isoformat()
        state_word = "on" if agreed_state else "off"
        if ep["flagged"]:
            # Already counted as a real disagreement when it outlived the lag
            # window - just close the story in the log and the record.
            self._amend_last_record_duration(side, duration)
            self.log(
                f"BEDCMP {side} disagreement ended after {duration:.0f}s "
                f"(now both {state_word}, opened {opened_iso})",
                level="INFO",
            )
            return
        opener = ep["opener"]
        if opener is not None and agreed_state != ep["opener_to"]:
            # The pair re-agreed on the ORIGINAL state: the opener went back (or
            # we lost its reverting edge and a tick caught it) - a blip only one
            # witness saw. strip_only/mat_only by who asserted it.
            self._count_disagreement(
                side,
                kind=f"{opener}_only",
                at=ep["opened_at"],
                duration_s=duration,
                detail=(
                    f"{opener}-only blip: unconfirmed by the other sensor, "
                    f"reverted after {duration:.0f}s"
                ),
            )
            return
        # The other sensor caught up (or an opener-unknown episode resolved):
        # an agreed edge. Lag samples only when we saw who led.
        self._totals(side)["edges_agreeing"] += 1
        night = self._night()
        if night is not None:
            night[f"{side}_edges_agreeing"] += 1
        if opener is not None:
            led_key = "strip_led" if opener == "strip" else "mat_led"
            self._totals(side)[led_key] += 1
            self._add_lag_sample(side, duration)
            if night is not None:
                night[f"{side}_{led_key}"] += 1
            self.log(
                f"BEDCMP {side} edge agreed: {state_word}, {opener} led by "
                f"{duration:.0f}s (opened {opened_iso})",
                level="INFO",
            )
        else:
            self.log(
                f"BEDCMP {side} edge agreed: {state_word} after an opener-unknown "
                f"divergence of {duration:.0f}s (no lag sample, opened {opened_iso})",
                level="INFO",
            )

    def _flag_stale_episode(self, side, now):
        """An episode diverged past the lag window is a real disagreement: count it
        once, by whichever sensor is asserting "on"."""
        ep = self._episodes.get(side)
        if ep is None or ep["flagged"]:
            return False
        age = (now - ep["opened_at"]).total_seconds()
        if age < self.lag_match_window_seconds:
            return False
        ep["flagged"] = True
        strip_on = self._effective(self._entity(side, "strip"))
        kind = "strip_only" if strip_on else "mat_only"
        asserter = "strip" if strip_on else "mat"
        other = "mat" if strip_on else "strip"
        other_raw = self._raw(self._entity(side, other))
        self._count_disagreement(
            side,
            kind=kind,
            at=ep["opened_at"],
            duration_s=age,
            detail=(
                f"{asserter} says on, {other} says {other_raw!r} - unresolved past "
                f"the {self.lag_match_window_seconds}s lag window, still ongoing"
            ),
        )
        return True

    def _add_lag_sample(self, side, lag_s):
        totals = self._totals(side)
        totals["lag_count"] += 1
        totals["lag_sum_s"] += lag_s
        totals["lag_max_s"] = max(totals["lag_max_s"], lag_s)
        night = self._night()
        if night is not None:
            night[f"{side}_lag_count"] += 1
            night[f"{side}_lag_sum_s"] += lag_s
            night[f"{side}_lag_max_s"] = max(night[f"{side}_lag_max_s"], lag_s)

    def _count_disagreement(self, side, kind, at, duration_s, detail):
        self._totals(side)[kind] += 1
        night = self._night()
        if night is not None:
            night[f"{side}_{kind}"] += 1
        desc = f"{side} {kind} at {at.isoformat()}: {detail}"
        self._state["last_disagreement"] = desc
        self._state["last_disagreement_at"] = at.isoformat()
        records = self._state["recent_disagreements"]
        records.append(
            {
                "at": at.isoformat(),
                "side": side,
                "kind": kind,
                "duration_s": f"{duration_s:.0f}",
                "detail": detail,
            }
        )
        self._state["recent_disagreements"] = records[-self.max_disagreement_records:]
        self.log(
            f"BEDCMP {side} DISAGREEMENT {kind}: {detail} "
            f"(started {at.isoformat()}, {duration_s:.0f}s)",
            level="WARNING",
        )

    def _amend_last_record_duration(self, side, duration_s):
        for record in reversed(self._state["recent_disagreements"]):
            if record["side"] == side:
                record["duration_s"] = f"{duration_s:.0f}"
                record["detail"] = record["detail"].replace(
                    ", still ongoing", f", ended after {duration_s:.0f}s"
                )
                break

    # ---------- publishing ----------

    def _publish(self, now):
        """sensor.bed_presence_agreement. Every numeric is a string: AppDaemon
        4.5.13's set_state drops attribute values equal to None/False/0 recursively
        (see module docstring), and 0 is this sensor's most meaningful value."""
        try:
            s = self._state
            attributes = {
                "friendly_name": "Bed presence agreement",
                "icon": "mdi:bed-double-outline",
                "schema": str(STATE_VERSION),
                "nights_observed": str(s["nights_observed"]),
                "tonight": s.get("current_night") or "",
                "last_disagreement": s.get("last_disagreement") or "",
                "last_disagreement_at": s.get("last_disagreement_at") or "",
                "recent_disagreements": [dict(r) for r in s["recent_disagreements"]],
                "nights": [self._stringify(n) for n in s["nights"]],
                "both_episodes_total": str(s["both"]["episodes_total"]),
                "both_minutes_total": f"{s['both']['seconds_total'] / 60.0:.1f}",
                "computed_at": now.isoformat(),
            }
            for side, cfg in self.pairs.items():
                totals = self._totals(side)
                for counter in ("edges_agreeing", "strip_only", "mat_only",
                                "strip_led", "mat_led"):
                    attributes[f"{side}_{counter}"] = str(totals[counter])
                lag_count = totals["lag_count"]
                attributes[f"{side}_lag_avg_s"] = (
                    f"{totals['lag_sum_s'] / lag_count:.1f}" if lag_count else ""
                )
                attributes[f"{side}_lag_max_s"] = f"{totals['lag_max_s']:.1f}"
                ep = self._episodes.get(side)
                attributes[f"{side}_open_disagreement_since"] = (
                    ep["opened_at"].isoformat() if ep else ""
                )
                attributes[f"{side}_entities"] = f"{cfg['strip']} vs {cfg['mat']}"
            self.set_state(
                self.publish_entity,
                state=str(s["nights_observed"]),
                replace=True,
                attributes=attributes,
            )
        except Exception as e:
            self.log(f"BEDCMP publish failed: {e}", level="WARNING")

    @staticmethod
    def _stringify(record):
        return {
            k: (f"{v:.0f}" if isinstance(v, float) else str(v))
            for k, v in record.items()
        }

    # ---------- time + restart survival ----------

    def _now_local(self):
        """Naive local datetime (repo idiom). Falls back to datetime.now() on a
        bare test instance."""
        try:
            return self.datetime()
        except Exception:
            return datetime.now()

    def _fresh_state(self):
        return {
            "version": STATE_VERSION,
            "nights_observed": 0,
            "current_night": None,
            "totals": {side: _empty_side_counters() for side in self.pairs},
            "both": {"episodes_total": 0, "seconds_total": 0.0},
            "nights": [],
            "recent_disagreements": [],
            "last_disagreement": "",
            "last_disagreement_at": "",
        }

    def _load_state(self):
        fresh = self._fresh_state()
        try:
            with open(self.state_file) as f:
                loaded = json.load(f)
        except Exception:
            return fresh
        if not isinstance(loaded, dict) or loaded.get("version") != STATE_VERSION:
            return fresh
        # Config may have gained/lost sides since the file was written.
        for key, value in fresh.items():
            loaded.setdefault(key, value)
        for side in self.pairs:
            loaded["totals"].setdefault(side, _empty_side_counters())
            for counter in _SIDE_COUNTERS:
                loaded["totals"][side].setdefault(
                    counter, 0.0 if counter.endswith("_s") else 0
                )
        return loaded

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"BEDCMP state save failed: {e}", level="WARNING")
