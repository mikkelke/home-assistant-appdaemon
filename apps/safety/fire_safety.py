"""
Fire Safety - Bosch Twinguard (kitchen ceiling, zigbee2mqtt) phase machine.

Publishes ``sensor.fire_safety`` (state = phase). Six phases: clear, pre_alarm, alarm,
hushed, cooldown, offline. Health (battery/self-test/offline) rides as attributes and a
throttled daily push; it never becomes a phase of its own except "offline" (device itself
unreachable > 30 min).

Never touches locks or burglar features, and never writes anything to the device's own
alarm select besides "stop". pre_alarm never pushes - only a kitchen-only Sonos chime,
suppressed while input_boolean.kitchen_cooking_mode is active. dry_run is a full gate:
while on, no call_service or notifier call reaches the outside world; every action is
logged with a "[dry-run]" prefix instead. test_audience narrows real pushes once dry_run
is off.

Phase/episode state persists to a JSON file (tmp + os.replace), so an ordinary tick
re-derives the phase from the live smoke/siren reading rather than needing special
resume-on-restart logic.

listen_state/listen_event/run_daily callbacks are plain sync methods that create_task()
the real work; every AppDaemon API call made from inside an async method must be awaited,
including run_in - an unawaited call from the event loop is a dead coroutine that never
fires.
"""

import asyncio
import json
import os
from datetime import datetime, timedelta, timezone, time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - stdlib always has it on supported Pythons
    ZoneInfo = None

UNAVAILABLE_STATES = (None, "unknown", "unavailable")
SIREN_BURGLAR = "burglar"

PHASE_ICONS = {
    "clear": "mdi:smoke-detector-variant",
    "pre_alarm": "mdi:smoke-detector-variant-alert",
    "alarm": "mdi:fire-alert",
    "hushed": "mdi:volume-off",
    "cooldown": "mdi:timer-sand",
    "offline": "mdi:cloud-alert",
}

IAQ_BREAKPOINTS = [(50, "fresh"), (100, "good"), (200, "stuffy")]
ECO2_BREAKPOINTS = [(800, "fresh"), (1200, "good"), (2000, "stuffy")]

# How many consecutive failed light-restore attempts (one per cooldown/clear tick) before
# giving up and dropping the snapshot rather than retrying forever.
LIGHT_RESTORE_MAX_ATTEMPTS = 5

# While a hush's siren/smoke reading is unknown (not a known negative), retry the
# confirmation check this often instead of a single now-or-never decision.
HUSH_UNKNOWN_RETRY_S = 5


def _normalize_siren(value):
    if value is None:
        return None
    text = str(value).strip().lower()
    return text or None


def _siren_matches(value, values):
    """Case-insensitive, trimmed, exact match - siren_state is a fixed vocabulary
    (clear/pre_alarm/fire/silenced/self_test/burglar), not free text to substring-scan."""
    norm = _normalize_siren(value)
    if norm is None or not values:
        return False
    return any(norm == str(v).strip().lower() for v in values)


def _band(value, breakpoints):
    """value <= each breakpoint's threshold in ascending order -> its label; else the worst
    band; None (sensor not reporting) -> "unknown", never "poor" (that would overstate it)."""
    if value is None:
        return "unknown"
    for threshold, label in breakpoints:
        if value <= threshold:
            return label
    return "poor"


class FireSafety(hass.Hass):
    def initialize(self):
        a = self.args.get

        self.smoke_entity = a("smoke_entity", "binary_sensor.kitchen_smoke_alarm_smoke")
        self.siren_state_entity = a("siren_state_entity", "sensor.kitchen_smoke_alarm_siren_state")
        self.battery_entity = a("battery_entity", "sensor.kitchen_smoke_alarm_battery")
        self.eco2_entity = a("eco2_entity", "sensor.kitchen_smoke_alarm_eco2")
        self.aqi_entity = a("aqi_entity", "sensor.kitchen_smoke_alarm_aqi")
        self.alarm_select_entity = a("alarm_select_entity", "select.kitchen_smoke_alarm_alarm")
        self.self_test_switch_entity = a("self_test_switch_entity", "switch.kitchen_smoke_alarm_self_test")

        self.siren_alarm_values = a("siren_alarm_values", ["fire"])
        self.siren_pre_alarm_values = a("siren_pre_alarm_values", ["pre_alarm"])
        self.siren_silenced_values = a("siren_silenced_values", ["silenced"])
        self.siren_self_test_values = a("siren_self_test_values", ["self_test"])

        self.hush_button_entity = a("hush_button_entity", "input_button.fire_safety_hush")
        self.clear_button_entity = a("clear_button_entity", "input_button.fire_safety_clear")
        self.test_button_entity = a("test_button_entity", "input_button.fire_safety_test")
        self.cooking_mode_entity = a("cooking_mode_entity", "input_boolean.kitchen_cooking_mode")

        # Button-press attribution (mirrors manual_override_timeout.py: context.user_id ->
        # person entity -> this fallback map); unresolved ids keep "the dashboard".
        self.user_name_fallback = {
            str(uid).replace("-", "").strip().lower(): name
            for uid, name in (a("user_name_fallback") or {}).items()
            if isinstance(name, str) and name.strip()
        }
        self._person_by_user_id = {}

        self.publish_entity = a("publish_entity", "sensor.fire_safety")
        self.state_file = a("state_file", "/conf/apps/safety/fire_safety_state.json")

        self.dry_run = bool(a("dry_run", True))
        self.test_audience = a("test_audience", ["mikkel"])

        self.hush_minutes = int(a("hush_minutes", 10))
        self.max_hushes_per_episode = int(a("max_hushes_per_episode", 2))
        # Remote hush sends "stop" then waits this long before checking whether the siren/
        # smoke actually corroborate it worked - a failed/ignored stop must not be reported
        # to the household as a successful hush.
        self.hush_confirm_s = int(a("hush_confirm_s", 20))
        # Total time since the press an unknown/unavailable siren or smoke reading may
        # keep rescheduling the confirmation check before giving up and reporting
        # "couldn't silence" - unknown must never itself confirm a hush.
        self.hush_confirm_max_s = int(a("hush_confirm_max_s", 120))
        self.pre_alarm_timeout_min = int(a("pre_alarm_timeout_min", 10))
        self.cooldown_confirm_s = int(a("cooldown_confirm_s", 60))
        self.cooldown_clear_min = int(a("cooldown_clear_min", 15))
        self.offline_after_min = int(a("offline_after_min", 30))
        self.cooking_mode_minutes = int(a("cooking_mode_minutes", 45))
        # Floor under _run_self_test's own trigger, for the gap before the device's live
        # siren_state actually reports self_test - seconds, not minutes, since a stuck or
        # accidental self-test must only mask a real fire for a short bounded window.
        self.self_test_floor_s = int(a("self_test_floor_s", 60))
        # If the siren has continuously reported self_test for longer than this, treat it as
        # NOT self-test for smoke_fallback purposes - a device stuck/faulty in self_test must
        # not suppress smoke_fallback forever.
        self.self_test_max_min = int(a("self_test_max_min", 10))
        self.reannounce_interval_s = int(a("reannounce_interval_s", 45))
        self.repush_interval_s = int(a("repush_interval_s", 120))
        self.repush_interval_acked_s = int(a("repush_interval_acked_s", 180))
        self.relights_interval_s = int(a("relights_interval_s", 60))
        self.tick_interval_s = int(a("tick_interval_s", 5))
        # Device unavailable/unknown mid-episode (alarm/hushed) longer than this -> one
        # non-critical push+announce, then Clear can end the episode without smoke=="off".
        self.stale_grace_s = int(a("stale_grace_s", 120))
        # smoke_fallback (smoke on, siren not corroborating) must hold continuously this
        # long before it alone raises an alarm; siren=="fire" always bypasses this.
        self.smoke_fallback_s = int(a("smoke_fallback_s", 30))
        # A new alarm starting within this many minutes of the previous clear reuses that
        # episode's id/hush-count/audience/light-snapshot instead of starting fresh.
        self.episode_reuse_min = int(a("episode_reuse_min", 30))

        self.battery_low_pct = float(a("battery_low_pct", 20))
        self.test_overdue_days = int(a("test_overdue_days", 35))
        self.fault_push_throttle_hours = int(a("fault_push_throttle_hours", 48))
        self.fault_check_time = a("fault_check_time", "09:00:00")
        self.monthly_test_time = a("monthly_test_time", "11:00:00")

        self.push_tag = a("push_tag", "fire_alarm")
        self.push_category = a("push_category", "fire_alarm")
        self.health_category = a("health_category", "fire_health")
        # A specific person list, not "home" - target="home" resolves to whoever is
        # physically home right now and would silently drop the push if everyone is away.
        self.health_notify_target = a("health_notify_target", ["mikkel"])

        self.alarm_always_notify = list(a("alarm_always_notify", ["mikkel"]))
        self.alarm_notify_if_home = dict(a("alarm_notify_if_home", {"kristine": "person.kristine", "claudia": "person.claudia"}) or {})
        self.alarm_nobody_home = a("alarm_nobody_home", "always_only")

        # Who counts as "someone home" for the automatic monthly self-test's auto-skip.
        self.monthly_test_person_entities = list(
            a("monthly_test_person_entities", ["person.mikkel", "person.kristine", "person.claudia"])
        )
        self.monthly_test_max_retry_days = int(a("monthly_test_max_retry_days", 7))

        self.sonos_kitchen_entity = a("sonos_kitchen_entity", "media_player.kitchen")
        self.sonos_all_entity = a("sonos_all_entity", "media_player.sonos_tts_all")
        self.sonos_kitchen_volume = float(a("sonos_kitchen_volume", 0.15))
        self.sonos_alarm_volume = float(a("sonos_alarm_volume", 0.7))

        self.alarm_lights = list(a("alarm_lights", []))
        self.alarm_light_manual_booleans = list(a("alarm_light_manual_booleans", []))
        self.media_pause_players = list(a("media_pause_players", []))

        self._source_entities = [
            self.smoke_entity, self.siren_state_entity, self.battery_entity,
            self.eco2_entity, self.aqi_entity, self.self_test_switch_entity,
        ]

        tz_name = a("timezone", "Europe/Copenhagen")
        self._tz = None
        if ZoneInfo is not None:
            try:
                self._tz = ZoneInfo(tz_name)
            except Exception:
                self._tz = None

        self._load_state()

        # Single lock shared by evaluate() and every external entry point (hush/clear/ack/
        # self-test/cooking) so a slow push can never block a concurrent button press.
        # Created here (not lazily) so it exists before any listener can possibly fire.
        self._eval_lock = asyncio.Lock()

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")
        self.sonos_notifier = None
        try:
            self.sonos_notifier = self.get_app("SonosNotifier")
        except Exception as e:
            self.log(f"SonosNotifier not available: {e}", level="WARNING")

        self.listen_state(self._on_smoke_change, self.smoke_entity)
        self.listen_state(self._on_siren_change, self.siren_state_entity)
        # One listener per button entity (AD filters on event data) instead of a single
        # house-wide state_changed subscription that fires for every entity in HA.
        self.listen_event(self._on_button_state_changed, "state_changed", entity_id=self.hush_button_entity)
        self.listen_event(self._on_button_state_changed, "state_changed", entity_id=self.clear_button_entity)
        self.listen_state(self._on_test_button, self.test_button_entity)
        self.listen_state(self._on_cooking_on, self.cooking_mode_entity, new="on")
        self.listen_state(self._on_cooking_off, self.cooking_mode_entity, new="off")
        self.listen_event(self._on_notification_action, "mobile_app_notification_action")

        self.run_daily(self._on_fault_check_tick, time(*self._parse_hms(self.fault_check_time)))
        self.run_daily(self._on_monthly_test_tick, time(*self._parse_hms(self.monthly_test_time)))

        # First tick at +2s (covers smoke already being on at boot), then every
        # tick_interval_s afterward for the time-based transitions/repeats.
        self.run_every(self._tick_cb, "now+2", self.tick_interval_s)

        self._maybe_resume_pending_hush()

        self.log(f"FireSafety initialized - phase={self.phase}, dry_run={self.dry_run}", level="INFO")

    def _maybe_resume_pending_hush(self):
        """A pending remote hush survives an AppDaemon restart (persisted) - if we're still
        in the same alarm episode, resume its confirmation loop instead of losing it."""
        pending = self.pending_hush
        if pending and self.phase == "alarm" and pending.get("episode") == self.episode_id:
            self.create_task(self._resume_pending_hush())

    async def _resume_pending_hush(self):
        pending = self.pending_hush
        if not pending:
            return
        await self.run_in(
            self._delayed_hush_confirm, 2, episode_id=pending.get("episode"), by_text=pending.get("by")
        )

    # ---------- scheduling glue (sync callbacks -> create_task) ----------

    def _tick_cb(self, kwargs):
        self.create_task(self._evaluate())

    def _on_smoke_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._evaluate())

    def _on_siren_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._evaluate())

    def _on_button_state_changed(self, event_name, data, kwargs):
        """Raw state_changed (not listen_state) so HA's context - and with it
        context.user_id, the human behind the tap - survives; see _resolve_actor.

        After an HA restart, input_button.* goes unavailable then restores its last press
        timestamp - that restore is a state_changed event too (old="unavailable",
        new=<old timestamp>), and must never be read as a fresh press."""
        data = data or {}
        entity = data.get("entity_id")
        if entity not in (self.hush_button_entity, self.clear_button_entity):
            return
        new_state = data.get("new_state") or {}
        old_state_raw = data.get("old_state")
        old_state = old_state_raw or {}
        new = new_state.get("state")
        old = old_state.get("state")
        if new == old or new in UNAVAILABLE_STATES:
            return
        if old_state_raw is None or old in UNAVAILABLE_STATES:
            return
        user_id = (new_state.get("context") or {}).get("user_id")
        self.create_task(self._handle_button_press(entity, user_id))

    async def _handle_button_press(self, entity, user_id):
        by_text = await self._resolve_actor(user_id) or "the dashboard"
        if entity == self.hush_button_entity:
            await self._hush(self._now(), by_text)
        else:
            await self._on_clear_pressed(by_text)

    async def _resolve_actor(self, user_id):
        if not user_id:
            return None
        if user_id not in self._person_by_user_id:
            await self._refresh_person_map()
        resolved = self._person_by_user_id.get(user_id)
        if resolved:
            return resolved
        return self.user_name_fallback.get(str(user_id).replace("-", "").strip().lower())

    async def _refresh_person_map(self):
        try:
            persons = await self.get_state("person") or {}
            for ent in persons:
                try:
                    obj = await self.get_state(ent, attribute="all") or {}
                except Exception:
                    continue
                attrs = obj.get("attributes") or {}
                uid = attrs.get("user_id")
                if uid:
                    self._person_by_user_id[uid] = attrs.get("friendly_name") or ent.split(".", 1)[-1].capitalize()
        except Exception as e:
            self.log(f"person map refresh failed: {e}", level="WARNING")

    def _on_test_button(self, entity, attribute, old, new, kwargs):
        # Same restored-press guard as _on_button_state_changed, adapted to listen_state's
        # old/new pair instead of a raw state_changed event. No freshness window - a
        # legitimate press delayed past one must still run; this old/new-unavailable check
        # is the only restart-restore protection needed.
        if new in UNAVAILABLE_STATES or old in UNAVAILABLE_STATES:
            return
        self.create_task(self._run_self_test(self._now()))

    def _on_cooking_on(self, entity, attribute, old, new, kwargs):
        self.create_task(self._start_cooking())

    def _on_cooking_off(self, entity, attribute, old, new, kwargs):
        self.create_task(self._stop_cooking())

    def _on_fault_check_tick(self, kwargs):
        self.create_task(self._check_faults(self._now()))

    def _on_monthly_test_tick(self, kwargs):
        self.create_task(self._maybe_run_monthly_test(self._now()))

    def _on_notification_action(self, event_name, data, kwargs):
        data = data or {}
        action = data.get("action", "") if isinstance(data, dict) else ""
        if action.startswith("FIRE_HUSH_"):
            parsed = self._parse_action(action, "FIRE_HUSH_")
            if not parsed:
                return
            episode, person = parsed
            if episode != self.episode_id:
                self.log(f"Stale hush action for episode {episode} (current {self.episode_id}) ignored", level="DEBUG")
                return
            # expected_episode is revalidated again once the lock is actually acquired (the
            # episode can change between this check and that point) - see _hush_locked.
            self.create_task(self._hush(self._now(), person.capitalize(), expected_episode=episode))
        elif action.startswith("FIRE_ACK_"):
            parsed = self._parse_action(action, "FIRE_ACK_")
            if not parsed:
                return
            episode, person = parsed
            if episode == self.episode_id:
                self.create_task(self._ack(person.capitalize()))

    @staticmethod
    def _parse_action(action, prefix):
        """FIRE_HUSH_<episode>_<person> -> (episode, person); episode_id is a plain digit
        timestamp (no underscores) so rsplit on the last "_" cleanly isolates the person."""
        remainder = action[len(prefix):]
        if "_" not in remainder:
            return None
        episode, person = remainder.rsplit("_", 1)
        if not episode or not person:
            return None
        return episode, person

    @staticmethod
    def _parse_hms(text):
        parts = [int(p) for p in str(text).split(":")]
        while len(parts) < 3:
            parts.append(0)
        return tuple(parts[:3])

    @staticmethod
    def _now():
        return datetime.now(timezone.utc)

    # ---------- main evaluation loop ----------

    async def _evaluate(self):
        # smoke and siren_state update in the same instant, so two listener tasks can race
        # here; without serialising, both would see the old phase and each enter alarm.
        async with self._eval_lock:
            await self._evaluate_locked()

    async def _evaluate_locked(self):
        try:
            now = self._now()
            # Read siren BEFORE the smoke-unknown early return - an explicit siren "fire" is
            # independent evidence and must raise/keep an alarm even while the smoke entity
            # itself is unavailable, instead of being swallowed by the outage branch below.
            smoke = await self._read_state(self.smoke_entity)
            siren = await self._read_state(self.siren_state_entity)
            siren_fire = _siren_matches(siren, self.siren_alarm_values)

            if smoke is None:
                # A device outage on the smoke entity alone must not let the smoke_fallback/
                # off debounce span it - a timer started before the outage could otherwise
                # satisfy its dwell purely from clock time, without a continuous reading.
                self.smoke_fallback_since = None
                self.off_since = None
                if not siren_fire:
                    # An unknown reading must never linger as a stale prior value -
                    # otherwise a later return to "fire" can look like "still fire" (no
                    # edge) instead of the fresh evidence it actually is.
                    if siren is None:
                        self.last_siren = None
                    self.last_smoke = None
                    await self._handle_unavailable(now)
                    await self._publish(now, None, siren)
                    return
                # else: fall through - the ordinary phase table below raises/keeps the alarm
                # from siren_fire alone even though smoke itself is unknown this tick.

            if self.unavailable_since is not None:
                self.unavailable_since = None
                self.stale_alarm_notified = False

            prev_smoke = self.last_smoke
            prev_siren = self.last_siren

            await self._maybe_expire_self_test(now)
            await self._maybe_expire_cooking_mode(now)
            self._maybe_log_burglar(prev_siren, siren)

            in_pre_alarm = _siren_matches(siren, self.siren_pre_alarm_values)
            in_alarm = _siren_matches(siren, self.siren_alarm_values)
            in_silenced = _siren_matches(siren, self.siren_silenced_values)
            in_self_test = _siren_matches(siren, self.siren_self_test_values)
            prev_in_alarm = _siren_matches(prev_siren, self.siren_alarm_values)
            prev_in_self_test = _siren_matches(prev_siren, self.siren_self_test_values)

            if in_self_test and not prev_in_self_test:
                self.last_self_test_at = now

            if not in_pre_alarm:
                # The siren has left pre_alarm at least once - lifts the stuck-pre_alarm
                # loop guard.
                self.pre_alarm_stuck = False

            # self_test_until is a floor under _run_self_test's own trigger, for the gap
            # before the device's live siren_state actually reports self_test; only applies
            # when smoke positively confirms "off" - smoke=="on" OR unknown must never be
            # masked, so a real fire (or one coinciding with a smoke-entity outage) during
            # that window is never suppressed by a recent self-test.
            floor_suppressed = (
                self.self_test_until is not None and now < self.self_test_until and smoke == "off"
            )
            # Bound how long a continuously-reported self_test can suppress smoke_fallback -
            # a device stuck/faulty in self_test must eventually let a real, uncorroborated
            # smoke reading raise an alarm again.
            self_test_expired = (
                in_self_test and self.last_self_test_at is not None
                and (now - self.last_self_test_at) >= timedelta(minutes=self.self_test_max_min)
            )
            effective_self_test = in_self_test and not self_test_expired
            siren_suppressed = effective_self_test or floor_suppressed

            # smoke_fallback: smoke on but the siren doesn't corroborate any known state
            # (not pre_alarm/silenced/self_test/fire - fire alarms immediately via in_alarm
            # below). Debounced: must hold continuously for smoke_fallback_s before it alone
            # raises an alarm, so a momentary cross-entity read race doesn't false-alarm.
            smoke_fallback_raw = (
                smoke == "on" and not in_pre_alarm and not in_silenced and not effective_self_test and not in_alarm
            )
            if smoke_fallback_raw:
                if self.smoke_fallback_since is None:
                    self.smoke_fallback_since = now
            else:
                self.smoke_fallback_since = None
            smoke_fallback = (
                self.smoke_fallback_since is not None
                and (now - self.smoke_fallback_since) >= timedelta(seconds=self.smoke_fallback_s)
            )

            pre_alarm_condition = (
                in_pre_alarm and not siren_suppressed and not self._cooking_active(now)
                and not self.pre_alarm_stuck
            )
            alarm_condition = (in_alarm and not siren_suppressed) or smoke_fallback

            if self.phase == "clear":
                if alarm_condition:
                    await self._enter_alarm(now)
                elif pre_alarm_condition:
                    await self._enter_pre_alarm(now)
                else:
                    await self._maybe_retry_light_restore(now)

            elif self.phase == "pre_alarm":
                if alarm_condition:
                    await self._enter_alarm(now)
                elif not in_pre_alarm:
                    await self._enter_clear(now)
                elif (now - self.since) >= timedelta(minutes=self.pre_alarm_timeout_min):
                    # Still reporting pre_alarm past our timeout - clear anyway, but remember
                    # it's stuck so we don't re-chime every tick until it truly clears.
                    self.pre_alarm_stuck = True
                    await self._enter_clear(now)

            elif self.phase == "alarm":
                if self._physical_hush_signal(prev_siren, siren):
                    # The device's own button already silenced it - confirmed by definition,
                    # commit immediately without the remote send-stop-and-wait dance. A
                    # matching in-flight remote hush is credited to its requester, not the
                    # button that merely echoed it.
                    pending = self.pending_hush
                    if (
                        pending is not None
                        and pending.get("episode") == self.episode_id
                        and pending.get("generation") == self.generation
                    ):
                        by_text = pending["by"]
                    else:
                        by_text = "the button on the alarm"
                    await self._hush_locked(now, by_text, physically_confirmed=True)
                elif smoke == "off":
                    self.off_since = self.off_since or now
                    if (now - self.off_since) >= timedelta(seconds=self.cooldown_confirm_s):
                        await self._enter_cooldown(now)
                    else:
                        await self._maybe_repeat_alarm_actions(now)
                else:
                    self.off_since = None
                    await self._maybe_repeat_alarm_actions(now)

            elif self.phase == "hushed":
                # Regardless of the smoke bit (silenced/self_test clear it on the device
                # itself, so it's not evidence of anything while hushed) - hold until
                # hushed_until, leaving early only on a real re-alarm signal.
                siren_realarm_edge = in_alarm and not prev_in_alarm
                if siren_realarm_edge or smoke_fallback:
                    await self._enter_alarm(now)
                elif self.hushed_until is not None and now >= self.hushed_until:
                    # Qualified predicate - smoke=="on" alone must NOT re-arm into a full
                    # critical alarm, since it's also the steady state of pre_alarm (the
                    # device's own smoke bit is set in both pre_alarm and fire).
                    if in_alarm or smoke_fallback:
                        await self._enter_alarm(now)
                    elif in_pre_alarm:
                        await self._enter_pre_alarm(now)
                    else:
                        await self._enter_cooldown(now)

            elif self.phase == "cooldown":
                if alarm_condition:
                    await self._enter_alarm(now)
                elif pre_alarm_condition:
                    await self._enter_pre_alarm(now)
                elif (now - self.since) >= timedelta(minutes=self.cooldown_clear_min):
                    await self._enter_clear(now)
                else:
                    await self._maybe_retry_light_restore(now)

            elif self.phase == "offline":
                if alarm_condition:
                    await self._enter_alarm(now)
                elif pre_alarm_condition:
                    await self._enter_pre_alarm(now)
                else:
                    await self._enter_clear(now)

            self.last_smoke = smoke
            self.last_siren = siren
            self._save_state()
            await self._publish(now, smoke, siren)
        except Exception as e:
            self.log(f"evaluate failed: {e}", level="ERROR")

    def _maybe_log_burglar(self, prev_siren, siren):
        is_burglar = _normalize_siren(siren) == SIREN_BURGLAR
        was_burglar = _normalize_siren(prev_siren) == SIREN_BURGLAR
        if is_burglar and not was_burglar:
            self.log(f"{self.siren_state_entity} reports burglar - ignored for fire phases", level="INFO")

    def _physical_hush_signal(self, prev_siren, siren):
        """The device's own mute button - detected as an EDGE into the silenced set (not a
        level check), so a siren stuck reporting silenced doesn't repeatedly re-fire."""
        now_silenced = _siren_matches(siren, self.siren_silenced_values)
        was_silenced = _siren_matches(prev_siren, self.siren_silenced_values)
        return now_silenced and not was_silenced

    def _clear_pending_hush(self):
        """Any phase change invalidates an in-flight remote hush confirmation."""
        self.pending_hush = None

    def _push_still_valid(self, episode, generation, expected_phase=None):
        """A detached push (dispatched with a snapshot of episode_id/generation) must
        re-check right before actually notifying - the episode may have moved on
        (hushed/re-alarmed/cleared) while it was in flight."""
        if self.episode_id != episode or self.generation != generation:
            return False
        return expected_phase is None or self.phase == expected_phase

    def _all_clear_still_valid(self, generation):
        """All-clear's episode_id is intentionally None by the time this fires, so validity
        is generation+phase only, not an episode_id match."""
        return self.phase == "clear" and self.generation == generation

    async def _handle_unavailable(self, now):
        if self.unavailable_since is None:
            self.unavailable_since = now
        stale_duration = now - self.unavailable_since
        if (
            self.phase in ("alarm", "hushed")
            and not self.stale_alarm_notified
            and stale_duration >= timedelta(seconds=self.stale_grace_s)
        ):
            await self._stale_mid_alarm(now)
        if self.phase != "offline" and stale_duration >= timedelta(minutes=self.offline_after_min):
            await self._enter_offline(now)
        self._save_state()

    async def _stale_mid_alarm(self, now):
        """Device stopped reporting mid-episode - stop repeats (already implicit,
        _evaluate_locked returns before the repeat cadence whenever smoke reads None),
        send exactly one non-critical notice, and mark it so Clear can end the episode
        (see _on_clear_pressed) even though smoke never reported "off".

        stale_alarm_notified is persisted, so this only ever fires once per episode even
        across an AppDaemon restart. Set it before dispatching the push itself (which must
        not hold _eval_lock) so a slow send can't cause a duplicate on the next tick."""
        self.stale_alarm_notified = True
        self._save_state()
        self.create_task(self._send_stale_notice(now, self.episode_id, self.generation))

    async def _send_stale_notice(self, now, episode_id, generation):
        message = "Kitchen smoke alarm stopped reporting during the alarm — check the kitchen"
        audience = await self._episode_audience(now, include_history=True)
        if not self._push_still_valid(episode_id, generation):
            self.log(f"Dropping stale mid-alarm notice for episode {episode_id}", level="DEBUG")
            return
        await self._notify(
            "stale-during-alarm notice",
            title="Fire alarm",
            message=message,
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )
        self._announce(
            "stale-during-alarm over Sonos",
            message=message,
            target_speakers=[self.sonos_all_entity],
            override_quiet_hours=True,
            volume_level=self.sonos_alarm_volume,
        )
        if self.episode_id == episode_id:
            self.episode_notified = audience

    async def _read_state(self, entity_id):
        try:
            state = await self.get_state(entity_id)
        except Exception as e:
            self.log(f"get_state({entity_id}) failed: {e}", level="WARNING")
            return None
        return None if state in UNAVAILABLE_STATES else state

    async def _read_float(self, entity_id):
        state = await self._read_state(entity_id)
        if state is None:
            return None
        try:
            return float(state)
        except (TypeError, ValueError):
            return None

    # ---------- phase transitions ----------

    async def _enter_pre_alarm(self, now):
        self._clear_pending_hush()
        self.phase = "pre_alarm"
        self.generation += 1
        self.since = now
        self._save_state()
        await self._report_feed("pre_alarm")
        await self._chime_pre_alarm(now)

    async def _enter_alarm(self, now):
        self._clear_pending_hush()
        # Continuation (not a new episode) whenever an episode is already open (offline
        # entered from alarm/hushed/cooldown keeps episode_id), or a fresh alarm lands
        # within episode_reuse_min of the last clear.
        resumed_continuous = self.episode_id is not None
        reused_recent = False
        if not resumed_continuous:
            reused_recent = (
                self.last_episode is not None
                and self.last_clear_at is not None
                and (now - self.last_clear_at) <= timedelta(minutes=self.episode_reuse_min)
            )
        fresh_start = not resumed_continuous and not reused_recent

        if fresh_start:
            self.episode_id = now.strftime("%Y%m%d%H%M%S")
            self.episode_started_at = now
            self.hush_count = 0
            self.ack_by = None
            self.episode_notified = None
            self.hush_limit_notified = False
        elif reused_recent:
            # Episode reuse carries over hush accounting only - NOT the audience list
            # (episode_notified) or a light snapshot, both of which must start fresh for
            # this new alarm rather than resurrecting stale history/state.
            prev = self.last_episode or {}
            self.episode_id = prev.get("episode_id") or now.strftime("%Y%m%d%H%M%S")
            self.episode_started_at = now
            self.hush_count = int(prev.get("hush_count") or 0)
            self.ack_by = None
            self.episode_notified = None
            self.hush_limit_notified = False

        self.phase = "alarm"
        self.generation += 1
        self.since = now
        self.off_since = None
        is_new_cycle = fresh_start or reused_recent
        if is_new_cycle:
            # The t+2s delayed announce is the first one; stop the tick cadence pre-empting it.
            self.last_announce_at = now
        self._save_state()

        # Lights/media/announcement must not wait on the push - a hanging notifier would
        # otherwise hold _eval_lock and block a concurrent hush/clear press.
        await self._report_feed("alarm")
        await self._assert_lights(now)
        self.last_lights_assert_at = now

        if is_new_cycle:
            self._save_state()
            await self.run_in(self._delayed_pause_media, 1)
            await self.run_in(self._delayed_announce, 2, episode_id=self.episode_id)
        else:
            # Escalation from hushed/cooldown/offline within the same episode: re-announce
            # now instead of waiting out the ordinary reannounce_interval_s cadence.
            await self._announce_alarm(now)
            self.last_announce_at = now
            self._save_state()

        # Snapshot episode_id/generation synchronously - the push itself runs detached from
        # _eval_lock, so it must not blindly trust live self.* once it resumes.
        self.create_task(self._push_alarm(now, self.episode_id, self.generation))
        self.last_push_at = now
        self._save_state()

    def _delayed_pause_media(self, kwargs):
        self.create_task(self._pause_media())

    def _delayed_announce(self, kwargs):
        self.create_task(self._announce_after_delay(kwargs.get("episode_id")))

    async def _announce_after_delay(self, episode_id):
        async with self._eval_lock:
            if self.phase != "alarm" or self.episode_id != episode_id:
                return  # superseded (hushed/cleared) before the t+2s announce fired
            now = self._now()
            await self._announce_alarm(now)
            self.last_announce_at = now
            self._save_state()

    async def _hush(self, now, by_text, expected_episode=None):
        async with self._eval_lock:
            await self._hush_locked(now, by_text, expected_episode=expected_episode)

    async def _hush_locked(self, now, by_text, expected_episode=None, physically_confirmed=False):
        """Core hush logic, lock-free - called both by _hush() (acquires _eval_lock) and
        directly from _evaluate_locked (the physical-hush-button path), which already
        holds the lock; asyncio.Lock isn't reentrant so that path must not re-acquire it.

        A remote hush is NOT trusted just because the select call was accepted - it sends
        "stop", then waits hush_confirm_s before checking the siren/smoke actually
        corroborate it worked. hush_count/phase are only committed on confirmation, so a
        silently-ignored remote hush never reports success or eats a hush attempt. The
        physical button (physically_confirmed=True) is confirmed by definition already."""
        if self.phase != "alarm":
            self.log(f"Hush ignored - phase is {self.phase}, not alarm", level="DEBUG")
            return
        if expected_episode is not None and expected_episode != self.episode_id:
            self.log(f"Stale hush action for episode {expected_episode} (current {self.episode_id}) ignored", level="DEBUG")
            return
        if self.hush_count >= self.max_hushes_per_episode:
            self.log(f"Hush limit ({self.max_hushes_per_episode}) reached for episode {self.episode_id}", level="WARNING")
            if not self.hush_limit_notified:
                self.hush_limit_notified = True
                self._save_state()
                self.create_task(self._push_hush_limit(now, self.episode_id, self.generation))
            return
        if physically_confirmed:
            await self._commit_hush(now, by_text)
            return
        # Only one remote hush attempt in flight at a time - a second press while one is
        # already awaiting confirmation is ignored, not queued.
        if self.pending_hush is not None:
            self.log("Hush already pending confirmation - second press ignored", level="DEBUG")
            return
        ok = await self._write_alarm_stop(now)
        if not ok:
            # The service call itself failed - treat as unconfirmed immediately, no need to
            # wait out hush_confirm_s for a call we already know didn't go through.
            self.create_task(self._push_hush_unconfirmed(now, self.episode_id, self.generation))
            return
        self.pending_hush = {
            "requested_at": now, "by": by_text, "episode": self.episode_id, "generation": self.generation,
        }
        self._save_state()
        await self.run_in(self._delayed_hush_confirm, self.hush_confirm_s, episode_id=self.episode_id, by_text=by_text)

    def _delayed_hush_confirm(self, kwargs):
        self.create_task(self._confirm_hush(kwargs.get("episode_id"), kwargs.get("by_text")))

    async def _confirm_hush(self, episode_id, by_text):
        async with self._eval_lock:
            await self._confirm_hush_locked(episode_id, by_text)

    async def _confirm_hush_locked(self, episode_id, by_text):
        """Confirmed only on POSITIVE evidence - siren known and in {clear, silenced}, or
        smoke known "off". An unknown/unavailable reading (e.g. the ~65s post-restart gap)
        must never itself confirm a hush - reschedule instead, up to hush_confirm_max_s
        total since the original press."""
        pending = self.pending_hush
        if (
            self.phase != "alarm"
            or self.episode_id != episode_id
            or pending is None
            or pending.get("episode") != episode_id
            or pending.get("generation") != self.generation
        ):
            return  # superseded (hushed some other way, a newer press, escalated, or cleared)
        now = self._now()
        siren = await self._read_state(self.siren_state_entity)
        smoke = await self._read_state(self.smoke_entity)
        siren_positive = _normalize_siren(siren) == "clear" or _siren_matches(siren, self.siren_silenced_values)
        smoke_positive = smoke == "off"
        if siren_positive or smoke_positive:
            await self._commit_hush(now, by_text)
            return
        requested_at = pending.get("requested_at") or now
        if (siren is None or smoke is None) and (now - requested_at) < timedelta(seconds=self.hush_confirm_max_s):
            await self.run_in(self._delayed_hush_confirm, HUSH_UNKNOWN_RETRY_S, episode_id=episode_id, by_text=by_text)
            return
        self.pending_hush = None
        self._save_state()
        self.create_task(self._push_hush_unconfirmed(now, episode_id, self.generation))

    async def _commit_hush(self, now, by_text):
        self.pending_hush = None
        if self.hush_count >= self.max_hushes_per_episode:
            # Re-check at commit time too - a confirmation that took a while (unknown
            # retries) could otherwise land after some other path already used up the limit.
            self.log(f"Hush limit reached at commit time for episode {self.episode_id} - not hushing", level="WARNING")
            if not self.hush_limit_notified:
                self.hush_limit_notified = True
                self._save_state()
                self.create_task(self._push_hush_limit(now, self.episode_id, self.generation))
            else:
                self._save_state()
            return
        self.hush_count += 1
        self.hushed_by = by_text
        self.hushed_until = now + timedelta(minutes=self.hush_minutes)
        self.phase = "hushed"
        self.generation += 1
        self.since = now
        self.off_since = None
        self._save_state()
        await self._report_feed("hushed", by=by_text)
        # Push detached from _eval_lock; announce stays awaited (ordering unchanged).
        self.create_task(self._push_hushed(now, self.episode_id, self.generation, self.hushed_by, self.hushed_until))
        await self._announce_hushed(now)

    async def _ack(self, person):
        async with self._eval_lock:
            if self.phase not in ("alarm", "hushed"):
                return
            self.ack_by = person
            self._save_state()
            self.log(f"{person} acknowledged the fire alarm", level="INFO")

    async def _enter_cooldown(self, now, by=None):
        self._clear_pending_hush()
        self.phase = "cooldown"
        self.generation += 1
        self.since = now
        self.off_since = None
        self._save_state()
        await self._report_feed("cooldown", by=by)
        await self._clear_lights()

    async def _enter_clear(self, now):
        self._clear_pending_hush()
        was_active = self.episode_id is not None
        duration_min = None
        if was_active and self.episode_started_at:
            duration_min = max(0, int((now - self.episode_started_at).total_seconds() // 60))
        if was_active:
            # Stashed for episode reuse before the fields below are wiped.
            self.last_clear_at = now
            self.last_episode = {
                "episode_id": self.episode_id,
                "hush_count": self.hush_count,
                "episode_notified": self.episode_notified,
                "light_snapshot": self.light_snapshot,
                "light_snapshot_episode": self.light_snapshot_episode,
            }
        # The all-clear push runs detached, so it must not rely on live
        # self.episode_id/episode_notified once it actually fires - both are about to be
        # wiped below. Snapshot them (and the new generation) synchronously instead, so
        # anyone recorded on this episode still receives the all-clear.
        cleared_episode_id = self.episode_id
        notified_snapshot = self.episode_notified
        self.phase = "clear"
        self.generation += 1
        self.since = now
        self.episode_id = None
        self.episode_started_at = None
        self.hush_count = 0
        self.hushed_by = None
        self.hushed_until = None
        self.ack_by = None
        self.off_since = None
        self.hush_limit_notified = False
        self.stale_alarm_notified = False
        self.episode_notified = None
        self._save_state()
        await self._report_feed("clear")
        if was_active:
            await self._clear_lights()
            self.create_task(
                self._push_all_clear(now, cleared_episode_id, self.generation, duration_min or 0, notified_snapshot)
            )
            await self._announce_all_clear(now)

    async def _enter_offline(self, now):
        self._clear_pending_hush()
        self.phase = "offline"
        self.generation += 1
        self.since = now
        self._save_state()
        await self._report_feed("offline")

    async def _on_clear_pressed(self, by_text=None):
        async with self._eval_lock:
            now = self._now()
            smoke = await self._read_state(self.smoke_entity)
            if smoke == "off":
                if self.phase == "clear":
                    return
                await self._enter_cooldown(now, by=by_text)
                return
            stale_mid_episode = (
                smoke is None
                and self.phase in ("alarm", "hushed", "offline")
                and self.unavailable_since is not None
                and (now - self.unavailable_since) >= timedelta(seconds=self.stale_grace_s)
            )
            if stale_mid_episode:
                await self._enter_clear(now)
                return
            self.log("Clear button pressed but smoke is still on - ignoring", level="WARNING")

    # ---------- self-test ----------

    async def _maybe_expire_self_test(self, now):
        if self.self_test_until and now >= self.self_test_until:
            self.self_test_until = None
            self._save_state()

    async def _run_self_test(self, now, automatic=False):
        """Refuse unless the alarm is idle and the device itself is clear - a self-test
        sounds the siren, so it must never fire mid-episode or onto ambiguous device state.
        The automatic monthly run additionally refuses unless everyone tracked is CONFIRMED
        not_home (unknown/unavailable presence counts as home)."""
        async with self._eval_lock:
            if not await self._self_test_ready(automatic):
                self.log("Self-test refused - alarm not idle, device not clear, or presence unclear", level="WARNING")
                return False
            await self._call(
                f"switch/turn_on {self.self_test_switch_entity}",
                "switch/turn_on", entity_id=self.self_test_switch_entity,
            )
            # last_self_test_at is stamped only from the observed siren edge into self_test
            # (see _evaluate_locked), not from triggering the switch here - the switch call
            # can fail or the device can ignore it, so only the confirmed edge counts.
            self.self_test_until = now + timedelta(seconds=self.self_test_floor_s)
            self._save_state()
            # Detached - this runs under _eval_lock, and a slow health push must not block
            # a concurrent hush/clear press.
            self.create_task(self._push_health(now, "Kitchen smoke alarm self-test ran."))
            return True

    async def _self_test_ready(self, automatic):
        if self.phase != "clear":
            return False
        smoke = await self._read_state(self.smoke_entity)
        if smoke != "off":
            return False
        siren = await self._read_state(self.siren_state_entity)
        if _normalize_siren(siren) != "clear":
            return False
        if automatic and await self._anyone_home():
            return False
        return True

    async def _anyone_home(self):
        """Unknown/unavailable presence counts as "home" for this gate - a self-test
        sounds the siren, so an unclear reading must never read as "away"."""
        for entity in self.monthly_test_person_entities:
            if await self._read_state(entity) != "not_home":
                return True
        return False

    async def _maybe_run_monthly_test(self, now):
        """Automatic monthly self-test: only while no one is home (a self-test sounds the
        siren). Retries daily at monthly_test_time through day monthly_test_max_retry_days,
        then gives up for the month and sends a health push instead."""
        if now.day > self.monthly_test_max_retry_days:
            return
        month_key = now.strftime("%Y-%m")
        if self.monthly_test_month_key != month_key:
            self.monthly_test_month_key = month_key
            self.monthly_test_resolved = False
            self._save_state()
        if self.monthly_test_resolved:
            return
        if await self._run_self_test(now, automatic=True):
            self.monthly_test_resolved = True
            self._save_state()
            return
        if now.day >= self.monthly_test_max_retry_days:
            self.monthly_test_resolved = True
            self._save_state()
            self.create_task(self._push_health(
                now, "Monthly smoke alarm test skipped — someone was home; run it from the dashboard"
            ))

    # ---------- cooking mode ----------

    async def _start_cooking(self):
        async with self._eval_lock:
            now = self._now()
            self.cooking_until = now + timedelta(minutes=self.cooking_mode_minutes)
            self._save_state()

    async def _stop_cooking(self):
        async with self._eval_lock:
            self.cooking_until = None
            self._save_state()

    async def _maybe_expire_cooking_mode(self, now):
        if not self.cooking_until or now < self.cooking_until:
            return
        self.cooking_until = None
        await self._call(
            f"input_boolean/turn_off {self.cooking_mode_entity}",
            "input_boolean/turn_off", entity_id=self.cooking_mode_entity,
        )
        self._save_state()

    def _cooking_active(self, now):
        return self.cooking_until is not None and now < self.cooking_until

    # ---------- faults (battery/self-test/offline) ----------

    async def _check_faults(self, now):
        battery = await self._read_float(self.battery_entity)
        battery_low = battery is not None and battery < self.battery_low_pct
        test_overdue = self.last_self_test_at is None or (now - self.last_self_test_at) > timedelta(days=self.test_overdue_days)

        faults = {}
        if battery_low:
            faults["battery_low"] = f"Kitchen smoke alarm battery is low ({battery:.0f}%)."
        if test_overdue:
            faults["test_overdue"] = f"Kitchen smoke alarm hasn't self-tested in over {self.test_overdue_days} days."
        if self.phase == "offline":
            faults["offline"] = "Kitchen smoke alarm has stopped reporting."

        throttle = timedelta(hours=self.fault_push_throttle_hours)
        for key, message in faults.items():
            last = self.last_fault_push_at.get(key)
            if last and (now - last) < throttle:
                continue
            # Detached - the throttle stamp is applied inside the task itself (only on a
            # real send), since a create_task() here can't be awaited for its result.
            self.create_task(self._send_fault_push(now, key, message))

    async def _send_fault_push(self, now, key, message):
        sent = await self._push_health(now, message)
        if sent:
            self.last_fault_push_at[key] = now
            self._save_state()

    # ---------- repeat cadence (alarm phase only) ----------

    async def _maybe_repeat_alarm_actions(self, now):
        if self.last_push_at is None or (now - self.last_push_at) >= self._repush_interval():
            # Detached from _eval_lock - a hanging repeat push must not block a concurrent
            # hush/clear press. A repeat's own targeting still ignores history
            # (include_history=False inside _push_alarm), but every recipient it actually
            # reaches is unioned into episode_notified afterward.
            self.create_task(self._push_alarm(now, self.episode_id, self.generation))
            self.last_push_at = now
            self._save_state()
        if self.last_announce_at is None or (now - self.last_announce_at) >= timedelta(seconds=self.reannounce_interval_s):
            await self._announce_alarm(now)
            self.last_announce_at = now
            self._save_state()
        if self.last_lights_assert_at is None or (now - self.last_lights_assert_at) >= timedelta(seconds=self.relights_interval_s):
            await self._assert_lights(now)
            self.last_lights_assert_at = now
            self._save_state()

    def _repush_interval(self):
        secs = self.repush_interval_acked_s if self.ack_by else self.repush_interval_s
        return timedelta(seconds=secs)

    # ---------- outward actions (all dry_run-gated - see _call/_notify/_announce) ----------

    async def _call(self, dry_run_desc, service, **kwargs):
        """Shared dry_run gate + error handling for a single call_service - see module
        docstring on dry_run being a full gate, never partial. Returns True/False so a
        caller like the remote hush path knows whether the call itself failed."""
        if self.dry_run:
            self.log(f"[dry-run] would {dry_run_desc}")
            return True
        try:
            await self.call_service(service, **kwargs)
            return True
        except Exception as e:
            self.log(f"{dry_run_desc} failed: {e}", level="WARNING")
            return False

    def _get_notifier(self):
        if self.mobile_notifier is None:
            self.log("MobileNotifier unavailable - push skipped", level="WARNING")
        return self.mobile_notifier

    def _get_sonos_notifier(self):
        if self.sonos_notifier is None:
            self.log("SonosNotifier unavailable - announce skipped", level="WARNING")
        return self.sonos_notifier

    async def _notify(self, dry_run_desc, **kwargs):
        """Returns True on success (or a simulated dry-run success) so callers that must
        only stamp bookkeeping on a real send (e.g. the fault throttle) can check it.

        MobileNotifier.notify returns the number of services it actually delivered to (0
        when none) - success here means that count is a positive int, not merely that the
        coroutine didn't raise, since a notifier that resolves zero services must not be
        read as a successful send."""
        if self.dry_run:
            self.log(f"[dry-run] would push: {dry_run_desc}")
            return True
        notifier = self._get_notifier()
        if notifier is None:
            return False
        try:
            result = await notifier.notify(**kwargs)
            return isinstance(result, int) and result > 0
        except Exception as e:
            self.log(f"push failed ({dry_run_desc}): {e}", level="WARNING")
            return False

    def _announce(self, dry_run_desc, **kwargs):
        if self.dry_run:
            self.log(f"[dry-run] would announce: {dry_run_desc}")
            return
        sonos = self._get_sonos_notifier()
        if sonos is None:
            return
        self.submit_to_executor(sonos.notify, **kwargs)

    async def _write_alarm_stop(self, now):
        return await self._call(
            f"select/select_option stop on {self.alarm_select_entity}",
            "select/select_option", entity_id=self.alarm_select_entity, option="stop",
        )

    async def _assert_lights(self, now):
        if self.dry_run:
            self.log(f"[dry-run] would turn on alarm lights: {self.alarm_lights}")
            return
        # Never take a new snapshot while a previous one is still un-restored - otherwise
        # a failed restore's target values would be lost forever.
        if self.light_snapshot_episode != self.episode_id and not self.light_snapshot:
            await self._snapshot_lights()
        try:
            for boolean in self.alarm_light_manual_booleans:
                await self.call_service("input_boolean/turn_on", entity_id=boolean)
            if self.alarm_lights:
                await self.call_service("light/turn_on", entity_id=self.alarm_lights, brightness_pct=100)
        except Exception as e:
            self.log(f"alarm lights on failed: {e}", level="WARNING")

    async def _snapshot_lights(self):
        snapshot = {}
        for entity in self.alarm_lights:
            try:
                obj = await self.get_state(entity, attribute="all")
            except Exception as e:
                self.log(f"light snapshot read failed for {entity}: {e}", level="WARNING")
                obj = None
            obj = obj or {}
            state = obj.get("state")
            attrs = obj.get("attributes") or {}
            if state == "on":
                snapshot[entity] = {"state": "on", "brightness": attrs.get("brightness")}
            else:
                snapshot[entity] = {"state": "off", "brightness": None}
        self.light_snapshot = snapshot
        self.light_snapshot_episode = self.episode_id
        self.light_restore_attempts = 0
        self._save_state()

    async def _clear_lights(self):
        """Only drop the snapshot once every restore call in it actually succeeded;
        otherwise keep it and let the caller retry on a later tick (see
        _maybe_retry_light_restore), giving up after LIGHT_RESTORE_MAX_ATTEMPTS."""
        if self.dry_run:
            self.log("[dry-run] would restore alarm lights and release manual overrides")
            return
        if not self.light_snapshot:
            if self.alarm_lights and self.episode_id is not None:
                self.log("No light snapshot for this episode - falling back to booleans-off only", level="WARNING")
            await self._release_manual_booleans()
            return
        ok = False
        try:
            ok = await self._restore_lights(self.light_snapshot)
        except Exception as e:
            self.log(f"alarm lights release failed: {e}", level="WARNING")
        await self._release_manual_booleans()
        if ok:
            self.light_snapshot = {}
            self.light_snapshot_episode = None
            self.light_restore_attempts = 0
            self._save_state()
            return
        self.light_restore_attempts += 1
        if self.light_restore_attempts >= LIGHT_RESTORE_MAX_ATTEMPTS:
            self.log(
                f"Light restore failed {self.light_restore_attempts} times - giving up and dropping the snapshot",
                level="WARNING",
            )
            self.light_snapshot = {}
            self.light_snapshot_episode = None
            self.light_restore_attempts = 0
        self._save_state()

    async def _maybe_retry_light_restore(self, now):
        if self.light_snapshot:
            await self._clear_lights()

    async def _release_manual_booleans(self):
        for boolean in self.alarm_light_manual_booleans:
            await self.call_service("input_boolean/turn_off", entity_id=boolean)

    async def _restore_lights(self, snapshot):
        """Returns True only if every call_service in the restore succeeded - a partial
        failure must not be reported as a completed restore."""
        ok = True
        off_entities = [entity for entity, v in snapshot.items() if v.get("state") != "on"]
        if off_entities:
            try:
                await self.call_service("light/turn_off", entity_id=off_entities)
            except Exception as e:
                self.log(f"light restore (off) failed: {e}", level="WARNING")
                ok = False
        groups = {}
        for entity, v in snapshot.items():
            if v.get("state") != "on":
                continue
            groups.setdefault(v.get("brightness"), []).append(entity)
        for brightness, entities in groups.items():
            kwargs = {"entity_id": entities}
            if brightness is not None:
                kwargs["brightness"] = brightness
            try:
                await self.call_service("light/turn_on", **kwargs)
            except Exception as e:
                self.log(f"light restore (on) failed: {e}", level="WARNING")
                ok = False
        return ok

    async def _pause_media(self):
        if not self.media_pause_players:
            return
        await self._call(
            f"pause media: {self.media_pause_players}",
            "media_player/media_pause", entity_id=self.media_pause_players,
        )

    async def _episode_audience(self, now, include_history, history_override=None):
        """alarm_always_notify is always included; each entry in alarm_notify_if_home only
        while home.

        The "nobody home -> everyone" fallback only applies once every tracked person is
        CONFIRMED not_home - an unknown/unavailable presence is ambiguous, not evidence of
        absence, so that person is simply left out rather than triggering "everyone".

        include_history=False (initial alarm + repeats) computes "always ∪ home now" only;
        include_history=True (hush/hush-unconfirmed/hush-limit/stale/all-clear) also ∪'s in
        episode_notified - anyone already recorded for this episode keeps getting THOSE
        pushes even after they leave. history_override lets a caller pass a pre-reset
        snapshot once self.episode_notified has already been wiped (see _enter_clear)."""
        home = set()
        all_confirmed_away = True
        for person, entity in self.alarm_notify_if_home.items():
            state = await self._read_state(entity)
            if state == "home":
                home.add(person)
            if state != "not_home":
                all_confirmed_away = False
        if not home and all_confirmed_away and self.alarm_nobody_home == "everyone":
            home = set(self.alarm_notify_if_home.keys())
        audience = set(self.alarm_always_notify) | home
        if include_history:
            history = self.episode_notified if history_override is None else history_override
            audience |= set(history or [])
        return sorted(audience)

    async def _push_alarm(self, now, episode, generation):
        """episode/generation are a snapshot taken synchronously by the caller - this push
        runs detached from _eval_lock, so it must not trust a live self.* read mid-flight.
        Every recipient of ANY alarm push (initial or repeat) is unioned into
        episode_notified - only the push's OWN targeting ignores history."""
        audience = await self._episode_audience(now, include_history=False)
        self.log(f"Fire alarm push audience: {audience}", level="INFO")

        def actions_for(person):
            return [
                {"action": f"FIRE_HUSH_{episode}_{person}", "title": "Silence – false alarm"},
                {"action": f"FIRE_ACK_{episode}_{person}", "title": "I'm checking"},
                {"action": "URI", "title": "Call 112", "uri": "tel:112"},
            ]

        if not self._push_still_valid(episode, generation, expected_phase="alarm"):
            self.log(f"Dropping stale alarm push for episode {episode}", level="DEBUG")
            return
        await self._notify(
            f"alarm push (episode={episode}) tag={self.push_tag}",
            title="Smoke in the kitchen",
            message="The alarm is sounding. Check the kitchen.",
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            critical=True,
            channel="Fire alarm",
            per_person_actions=actions_for,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )
        if self.episode_id == episode:
            self.episode_notified = sorted(set(self.episode_notified or []) | set(audience))

    async def _push_hushed(self, now, episode, generation, hushed_by, hushed_until):
        rearm = self._fmt(hushed_until)
        audience = await self._episode_audience(now, include_history=True)
        if not self._push_still_valid(episode, generation, expected_phase="hushed"):
            self.log(f"Dropping stale hush-confirmation push for episode {episode}", level="DEBUG")
            return
        await self._notify(
            "hush confirmation",
            title="Smoke in the kitchen",
            message=f"{hushed_by} silenced the kitchen alarm. It re-arms at {rearm}.",
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )
        if self.episode_id == episode:
            self.episode_notified = audience

    async def _push_hush_unconfirmed(self, now, episode, generation):
        """Sent both when the remote "stop" call itself failed and when it was accepted
        but the device never corroborated it within hush_confirm_max_s - from the
        household's perspective these are the same outcome (the alarm is not silenced)."""
        audience = await self._episode_audience(now, include_history=True)
        if not self._push_still_valid(episode, generation):
            self.log(f"Dropping stale hush-unconfirmed push for episode {episode}", level="DEBUG")
            return
        await self._notify(
            "hush-unconfirmed notice",
            title="Fire alarm",
            message="Couldn't silence the kitchen alarm remotely — press the button on the alarm",
            target="all",
            data={"data": {"tag": f"{self.push_tag}_hush_unconfirmed"}},
            category=self.push_category,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )
        if self.episode_id == episode:
            self.episode_notified = audience

    async def _push_hush_limit(self, now, episode, generation):
        audience = await self._episode_audience(now, include_history=True)
        if not self._push_still_valid(episode, generation):
            self.log(f"Dropping stale hush-limit push for episode {episode}", level="DEBUG")
            return
        await self._notify(
            "hush-limit-reached notice",
            title="Fire alarm",
            message="Hush limit reached - the kitchen alarm can't be silenced again this episode.",
            target="all",
            data={"data": {"tag": f"{self.push_tag}_hush_limit"}},
            category=self.push_category,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )

    async def _push_all_clear(self, now, episode, generation, duration_min, notified_snapshot):
        audience = await self._episode_audience(now, include_history=True, history_override=notified_snapshot)
        if not self._all_clear_still_valid(generation):
            self.log(f"Dropping stale all-clear push for episode {episode}", level="DEBUG")
            return
        await self._notify(
            "all-clear",
            title="Fire alarm",
            message=f"All clear — the kitchen alarm has reset. It was sounding for {duration_min} minutes.",
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            test_audience=self.test_audience if self.test_audience is not None else audience,
        )

    async def _push_health(self, now, message):
        return await self._notify(
            f"fire_health: {message}",
            title="Fire safety",
            message=message,
            target=self.health_notify_target,
            category=self.health_category,
            test_audience=self.test_audience,
        )

    async def _chime_pre_alarm(self, now):
        self._announce(
            "kitchen pre-alarm chime",
            message="Smoke is building in the kitchen",
            target_speakers=[self.sonos_kitchen_entity],
            volume_level=self.sonos_kitchen_volume,
        )

    async def _announce_alarm(self, now):
        self._announce(
            "alarm over house-wide Sonos",
            message="Smoke has been detected in the kitchen. Please check the kitchen now.",
            target_speakers=[self.sonos_all_entity],
            override_quiet_hours=True,
            volume_level=self.sonos_alarm_volume,
        )

    async def _announce_hushed(self, now):
        self._announce(
            "hush over Sonos",
            message="The kitchen alarm has been silenced.",
            target_speakers=[self.sonos_all_entity],
            volume_level=self.sonos_alarm_volume,
        )

    async def _announce_all_clear(self, now):
        self._announce(
            "all-clear over Sonos",
            message="The kitchen alarm has reset. All clear.",
            target_speakers=[self.sonos_all_entity],
        )

    # ---------- house_events feed (only on real transitions - see house_events.py) ----------

    async def _report_feed(self, phase, by=None):
        icon = PHASE_ICONS.get(phase, "mdi:smoke-detector-variant")
        if phase == "pre_alarm":
            cause, effect = "Kitchen smoke alarm sensing early smoke", "Kitchen chime warning played"
        elif phase == "alarm":
            cause, effect = "Kitchen smoke alarm triggered", "Push, lights and house-wide Sonos alarm"
        elif phase == "hushed":
            cause, effect = f"{by} silenced the kitchen alarm", f"Re-arms at {self._fmt(self.hushed_until)}"
        elif phase == "cooldown":
            cause = f"{by} confirmed the kitchen alarm is clear" if by else "Kitchen smoke cleared"
            effect = "Confirming the kitchen alarm is over"
        elif phase == "clear":
            cause, effect = "Kitchen alarm confirmed clear", "Lights and overrides released"
        elif phase == "offline":
            cause, effect = "Kitchen smoke alarm stopped reporting", f"Marked offline after {self.offline_after_min} minutes"
        else:
            return
        if self.dry_run:
            self.log(f"[dry-run] would report to feed: {cause} -> {effect}")
            return
        try:
            await self.fire_event("house_events_report", cause=cause, effect=effect, icon=icon, by=by)
        except Exception as e:
            self.log(f"house_events report failed: {e}", level="DEBUG")

    # ---------- publish (middle-layer convention: reason/source_entities/computed_at) ----------

    def _fmt(self, dt):
        if dt is None:
            return "unknown"
        local = dt.astimezone(self._tz) if self._tz else dt
        return local.strftime("%H:%M")

    def _headline_detail(self):
        if self.phase == "clear":
            return "All clear", "Kitchen alarm normal"
        if self.phase == "pre_alarm":
            return "Smoke building", f"Kitchen ceiling · {self._fmt(self.since)}"
        if self.phase == "alarm":
            return "Smoke detected", f"Kitchen ceiling · {self._fmt(self.since)}"
        if self.phase == "hushed":
            return f"Silenced by {self.hushed_by or 'someone'}", f"Re-arms at {self._fmt(self.hushed_until)}"
        if self.phase == "cooldown":
            return "Clearing", f"Smoke off since {self._fmt(self.off_since or self.since)}"
        if self.phase == "offline":
            return "Alarm offline", f"No data since {self._fmt(self.unavailable_since)}"
        return "Unknown", ""

    def _reason(self, smoke, siren):
        if smoke is None:
            return "device unavailable"
        if self.phase == "alarm":
            return "smoke sensor on" if smoke == "on" else f"siren reports {siren}"
        if self.phase == "hushed":
            return f"hushed by {self.hushed_by}, smoke {smoke}"
        if self.phase == "pre_alarm":
            return f"siren reports {siren}"
        if self.phase == "cooldown":
            return "smoke off, confirming clear"
        if self.phase == "offline":
            return "device unavailable"
        return "smoke off, siren normal"

    async def _publish(self, now, smoke, siren):
        battery = await self._read_float(self.battery_entity)
        eco2 = await self._read_float(self.eco2_entity)
        iaq = await self._read_float(self.aqi_entity)
        battery_low = battery is not None and battery < self.battery_low_pct
        test_overdue = self.last_self_test_at is None or (now - self.last_self_test_at) > timedelta(days=self.test_overdue_days)
        headline, detail = self._headline_detail()
        attributes = {
            "friendly_name": "Fire safety",
            "icon": PHASE_ICONS.get(self.phase, "mdi:smoke-detector-variant"),
            "since": self.since.isoformat() if self.since else None,
            "episode_id": self.episode_id,
            "hushed_until": self.hushed_until.isoformat() if self.hushed_until else None,
            "hushed_by": self.hushed_by,
            "hush_count": self.hush_count,
            "ack_by": self.ack_by,
            "last_smoke": self.last_smoke,
            "battery": battery,
            "battery_low": battery_low,
            "last_test": self.last_self_test_at.isoformat() if self.last_self_test_at else None,
            "test_overdue": test_overdue,
            "device_available": smoke is not None,
            "cooking_until": self.cooking_until.isoformat() if self.cooking_until else None,
            "iaq": iaq,
            "iaq_band": _band(iaq, IAQ_BREAKPOINTS),
            "eco2": eco2,
            "eco2_band": _band(eco2, ECO2_BREAKPOINTS),
            "headline": headline,
            "detail": detail,
            "reason": self._reason(smoke, siren),
            "source_entities": self._source_entities,
            "computed_at": now.isoformat(timespec="seconds"),
            "dry_run": self.dry_run,
        }
        # Recorder-spam guard: only write when something besides computed_at actually
        # changed, plus a 5-minute heartbeat (and always on the first call after init,
        # since _last_published_state starts None) so the entity still looks alive.
        comparable = {k: v for k, v in attributes.items() if k != "computed_at"}
        unchanged = self._last_published_state == self.phase and self._last_published_attrs == comparable
        heartbeat_due = (
            self._last_published_at is None or (now - self._last_published_at) >= timedelta(minutes=5)
        )
        if unchanged and not heartbeat_due:
            # HA may have lost our published entity (e.g. an HA-core restart wiped it
            # while AppDaemon kept running) - don't wait out the 5-minute heartbeat to
            # notice, republish immediately once we see it's gone.
            live = await self._read_state(self.publish_entity)
            if live is not None:
                return
        try:
            await self.set_state(self.publish_entity, state=self.phase, replace=True, attributes=attributes)
            self._last_published_state = self.phase
            self._last_published_attrs = comparable
            self._last_published_at = now
        except Exception as e:
            self.log(f"publish failed: {e}", level="WARNING")

    # ---------- persistence (tmp + os.replace, see house_events.py / climate_alarm.py) ----------

    @staticmethod
    def _parse_dt(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(value)
        except (TypeError, ValueError):
            return None

    @classmethod
    def _parse_pending_hush(cls, value):
        if not value:
            return None
        requested_at = cls._parse_dt(value.get("requested_at"))
        if requested_at is None:
            return None
        return {
            "requested_at": requested_at,
            "by": value.get("by"),
            "episode": value.get("episode"),
            "generation": int(value.get("generation") or 0),
        }

    @staticmethod
    def _dump_pending_hush(value):
        if not value:
            return None
        requested_at = value.get("requested_at")
        return {
            "requested_at": requested_at.isoformat() if requested_at else None,
            "by": value.get("by"),
            "episode": value.get("episode"),
            "generation": value.get("generation"),
        }

    def _load_state(self):
        try:
            with open(self.state_file) as f:
                data = json.load(f)
        except Exception:
            data = {}
        self.phase = data.get("phase", "clear")
        self.since = self._parse_dt(data.get("since")) or self._now()
        self.episode_id = data.get("episode_id")
        self.episode_started_at = self._parse_dt(data.get("episode_started_at"))
        self.hushed_until = self._parse_dt(data.get("hushed_until"))
        self.hushed_by = data.get("hushed_by")
        self.hush_count = int(data.get("hush_count") or 0)
        self.ack_by = data.get("ack_by")
        self.last_push_at = self._parse_dt(data.get("last_push_at"))
        self.last_announce_at = self._parse_dt(data.get("last_announce_at"))
        self.last_lights_assert_at = self._parse_dt(data.get("last_lights_assert_at"))
        self.last_self_test_at = self._parse_dt(data.get("last_self_test_at"))
        self.last_smoke = data.get("last_smoke")
        self.last_siren = data.get("last_siren")
        self.self_test_until = self._parse_dt(data.get("self_test_until"))
        self.cooking_until = self._parse_dt(data.get("cooking_until"))
        self.unavailable_since = self._parse_dt(data.get("unavailable_since"))
        self.off_since = self._parse_dt(data.get("off_since"))
        self.last_fault_push_at = {k: self._parse_dt(v) for k, v in (data.get("last_fault_push_at") or {}).items()}
        self.light_snapshot = data.get("light_snapshot") or {}
        self.light_snapshot_episode = data.get("light_snapshot_episode")
        self.light_restore_attempts = int(data.get("light_restore_attempts") or 0)
        self.episode_notified = data.get("episode_notified") or None
        self.last_clear_at = self._parse_dt(data.get("last_clear_at"))
        self.last_episode = data.get("last_episode") or None
        self.hush_limit_notified = bool(data.get("hush_limit_notified") or False)
        self.monthly_test_month_key = data.get("monthly_test_month_key")
        self.monthly_test_resolved = bool(data.get("monthly_test_resolved") or False)
        # Persisted (not transient) so an AppDaemon restart mid-alarm doesn't repeat the
        # once-only house-wide stale announcement; _enter_clear resets it per episode.
        self.stale_alarm_notified = bool(data.get("stale_alarm_notified") or False)
        # Bumped on every phase change so a detached push can tell whether the episode has
        # moved on since it was dispatched.
        self.generation = int(data.get("generation") or 0)
        self.pending_hush = self._parse_pending_hush(data.get("pending_hush"))

        # Transient (never persisted): safe/desirable to reset every process start
        # (fallback debounce, loop guard, heartbeat).
        self.smoke_fallback_since = None
        self.pre_alarm_stuck = False
        self._last_published_state = None
        self._last_published_attrs = None
        self._last_published_at = None

    def _save_state(self):
        data = {
            "phase": self.phase,
            "since": self.since.isoformat() if self.since else None,
            "episode_id": self.episode_id,
            "episode_started_at": self.episode_started_at.isoformat() if self.episode_started_at else None,
            "hushed_until": self.hushed_until.isoformat() if self.hushed_until else None,
            "hushed_by": self.hushed_by,
            "hush_count": self.hush_count,
            "ack_by": self.ack_by,
            "last_push_at": self.last_push_at.isoformat() if self.last_push_at else None,
            "last_announce_at": self.last_announce_at.isoformat() if self.last_announce_at else None,
            "last_lights_assert_at": self.last_lights_assert_at.isoformat() if self.last_lights_assert_at else None,
            "last_self_test_at": self.last_self_test_at.isoformat() if self.last_self_test_at else None,
            "last_smoke": self.last_smoke,
            "last_siren": self.last_siren,
            "self_test_until": self.self_test_until.isoformat() if self.self_test_until else None,
            "cooking_until": self.cooking_until.isoformat() if self.cooking_until else None,
            "unavailable_since": self.unavailable_since.isoformat() if self.unavailable_since else None,
            "off_since": self.off_since.isoformat() if self.off_since else None,
            "last_fault_push_at": {k: v.isoformat() for k, v in self.last_fault_push_at.items() if v},
            "light_snapshot": self.light_snapshot,
            "light_snapshot_episode": self.light_snapshot_episode,
            "light_restore_attempts": self.light_restore_attempts,
            "episode_notified": self.episode_notified,
            "last_clear_at": self.last_clear_at.isoformat() if self.last_clear_at else None,
            "last_episode": self.last_episode,
            "hush_limit_notified": self.hush_limit_notified,
            "monthly_test_month_key": self.monthly_test_month_key,
            "monthly_test_resolved": self.monthly_test_resolved,
            "stale_alarm_notified": self.stale_alarm_notified,
            "generation": self.generation,
            "pending_hush": self._dump_pending_hush(self.pending_hush),
        }
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")
