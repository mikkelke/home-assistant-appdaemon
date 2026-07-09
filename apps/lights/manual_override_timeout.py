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
        elif new == "off":
            self._cancel(entity)

    def _expire(self, ent):
        try:
            if self.get_state(ent) == "on":
                self.log(
                    f"{ent}: manual override expired after {self.timeout_s / 3600:.0f}h - back to automatic",
                    level="INFO",
                )
                self.turn_off(ent)
        except Exception as e:
            self.log(f"expire failed for {ent}: {e}", level="ERROR")

    def _cancel(self, ent):
        h = self._timers.pop(ent, None)
        try:
            if h and self.timer_running(h):
                self.cancel_timer(h)
        except Exception:
            pass
