import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import datetime
import time


class ManualOverrideTimeout(hass.Hass):
    """GLOBAL mechanism: auto-clear ``*_lights_manual`` overrides after ``timeout_hours``.

    One watcher for every room toggle (instead of per-app timers). Restart-safe:
    the deadline is computed from the boolean's HA ``last_changed``, so an
    AppDaemon reload/restart can never strand an override past its 12 hours -
    on init every ON toggle is rescheduled for its remaining time (or cleared
    immediately if already expired). Turning the boolean OFF is picked up by the
    room's lights app, which resumes automatic control.
    """

    def initialize(self):
        self.timeout_s = float(self.args.get("timeout_hours", 12)) * 3600.0
        self.booleans = list(self.args.get("booleans") or [])
        self._timers = {}
        # Entities whose OFF we caused ourselves (expiry) - suppresses the duplicate
        # "turned off" feed report in _on_change. See _expire.
        self._self_cleared = set()
        for ent in self.booleans:
            self.listen_state(self._on_change, ent)
        self.run_in(lambda _: self._resync_all(), 5)
        self.log(
            f"ManualOverrideTimeout: {len(self.booleans)} toggles, "
            f"auto-off after {self.timeout_s / 3600:.0f}h",
            level="INFO",
        )

    def terminate(self):
        for ent in list(self._timers):
            self._cancel(ent)

    # ─────────────────────────────────────────────────────────────

    def _resync_all(self):
        for ent in self.booleans:
            self._resync(ent)

    def _resync(self, ent):
        """Schedule (or fire) the auto-off from how long the toggle has been ON."""
        self._cancel(ent)
        try:
            if self.get_state(ent) != "on":
                return
            elapsed = 0.0
            last_changed = self.get_state(ent, attribute="last_changed")
            if last_changed:
                try:
                    lc = datetime.datetime.fromisoformat(str(last_changed))
                    elapsed = max(0.0, time.time() - lc.timestamp())
                except (ValueError, TypeError):
                    elapsed = 0.0
            remaining = max(1.0, self.timeout_s - elapsed)
            self._timers[ent] = self.run_in(lambda _, e=ent: self._expire(e), remaining)
            self.log(f"{ent}: ON - auto-off in {remaining / 3600:.1f}h", level="INFO")
        except Exception as e:
            self.log(f"resync failed for {ent}: {e}", level="ERROR")

    def _on_change(self, entity, attribute, old, new, kwargs):
        if new == "on":
            self._resync(entity)
            self._report(
                f"{self._room_label(entity)} lights switched to manual",
                f"Automation paused - resumes within {self.timeout_s / 3600:.0f} h",
            )
        elif new == "off":
            self._cancel(entity)
            # Our own _expire turn_off lands here too - it already reported the richer
            # "timed out" story, so only a human flipping the toggle reports this one.
            if entity in self._self_cleared:
                self._self_cleared.discard(entity)
            else:
                self._report(
                    f"{self._room_label(entity)} manual override turned off",
                    "Lights back to automatic",
                )

    def _expire(self, ent):
        try:
            if self.get_state(ent) == "on":
                self.log(
                    f"{ent}: manual override expired after {self.timeout_s / 3600:.0f}h - back to automatic",
                    level="INFO",
                )
                self._self_cleared.add(ent)
                self.turn_off(ent)
                self._report(
                    f"{self._room_label(ent)} manual override timed out after {self.timeout_s / 3600:.0f} h",
                    "Lights back to automatic",
                )
        except Exception as e:
            self.log(f"expire failed for {ent}: {e}", level="ERROR")

    @staticmethod
    def _room_label(ent):
        """input_boolean.living_room_lights_manual -> 'Living room'."""
        name = ent.split(".", 1)[-1]
        if name.endswith("_lights_manual"):
            name = name[: -len("_lights_manual")]
        return name.replace("_", " ").strip().capitalize()

    def _report(self, cause, effect):
        """Explain an override change to the dashboard's Home activity feed. Fire-and-forget:
        HouseEvents (apps/home_pulse) listens; if absent the event evaporates."""
        try:
            self.fire_event("house_events_report", cause=cause, effect=effect, icon="mdi:hand-back-right")
        except Exception:
            pass

    def _cancel(self, ent):
        h = self._timers.pop(ent, None)
        try:
            if h and self.timer_running(h):
                self.cancel_timer(h)
        except Exception:
            pass
