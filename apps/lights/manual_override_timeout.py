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

    Feed attribution: toggles are watched via the raw ``state_changed`` event rather
    than ``listen_state`` because only the raw event keeps HA's context - and with it
    ``context.user_id``, the actual human behind a dashboard/app tap. The id resolves
    to a display name through person entities (``person.X`` carries ``user_id`` when
    that person has an HA account), so the activity feed can say "Mikkel set ... to
    manual". No user context (physical switch, another app's service call) or an id
    no person claims -> neutral wording, never a guessed name.
    """

    def initialize(self):
        self.timeout_s = float(self.args.get("timeout_hours", 12)) * 3600.0
        self.booleans = set(self.args.get("booleans") or [])
        self._timers = {}
        # Entities whose OFF we caused ourselves (expiry) - suppresses the duplicate
        # "turned off" feed report in _handle_change. See _expire.
        self._self_cleared = set()
        self._person_by_user_id = {}
        self.listen_event(self._on_state_changed, "state_changed")
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

    def _on_state_changed(self, event_name, data, kwargs):
        """Raw state_changed listener (fires for every entity; filtered here - cheap set
        lookup, same thing listen_state does internally) so context survives. See class doc."""
        try:
            entity = (data or {}).get("entity_id")
            if entity not in self.booleans:
                return
            new_state = data.get("new_state") or {}
            old_state = data.get("old_state") or {}
            new = new_state.get("state")
            old = old_state.get("state")
            if new == old:
                return
            user_id = (new_state.get("context") or {}).get("user_id")
            self._handle_change(entity, old, new, self._person_name(user_id))
        except Exception as e:
            self.log(f"state_changed handling failed: {e}", level="ERROR")

    def _handle_change(self, entity, old, new, person):
        # Wording stays neutral; the acting person travels in the report's separate `by`
        # field (feed v4) and the dashboard renders it as its own muted "By <name>" line.
        room = self._room_label(entity)
        if new == "on":
            self._resync(entity)
            self._report(
                f"{room} lights switched to manual",
                f"Automation paused - resumes within {self.timeout_s / 3600:.0f} h",
                by=person,
            )
        elif new == "off":
            self._cancel(entity)
            # Our own _expire turn_off lands here too - it already reported the richer
            # "timed out" story, so only a human flipping the toggle reports this one.
            if entity in self._self_cleared:
                self._self_cleared.discard(entity)
            else:
                self._report(
                    f"{room} manual override turned off",
                    "Lights back to automatic",
                    by=person,
                )

    def _person_name(self, user_id):
        """Display name for an HA user id via person entities, or None. The map refreshes
        lazily on an unseen id (covers accounts/persons added after app start)."""
        if not user_id:
            return None
        if user_id not in self._person_by_user_id:
            self._refresh_person_map()
        return self._person_by_user_id.get(user_id)

    def _refresh_person_map(self):
        # Domain query first (attribute= is per-entity only in AppDaemon), then each
        # person individually - a handful of entities, and refreshes are lazy/rare.
        try:
            persons = self.get_state("person") or {}
            for ent in persons:
                try:
                    obj = self.get_state(ent, attribute="all") or {}
                except Exception:
                    continue
                attrs = obj.get("attributes") or {}
                uid = attrs.get("user_id")
                if uid:
                    self._person_by_user_id[uid] = attrs.get("friendly_name") or ent.split(".", 1)[-1].capitalize()
        except Exception as e:
            self.log(f"person map refresh failed: {e}", level="WARNING")

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

    def _report(self, cause, effect, by=None):
        """Explain an override change to the dashboard's Home activity feed. Fire-and-forget:
        HouseEvents (apps/home_pulse) listens; if absent the event evaporates. audience=admin:
        override plumbing is Mikkel-facing - housemates' feeds skip it."""
        try:
            payload = {"cause": cause, "effect": effect, "icon": "mdi:hand-back-right", "audience": "admin"}
            if by:
                payload["by"] = by
            self.fire_event("house_events_report", **payload)
        except Exception:
            pass

    def _cancel(self, ent):
        h = self._timers.pop(ent, None)
        try:
            if h and self.timer_running(h):
                self.cancel_timer(h)
        except Exception:
            pass
