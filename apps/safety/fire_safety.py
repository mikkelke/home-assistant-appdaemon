"""
Fire Safety - Bosch Twinguard (kitchen ceiling, zigbee2mqtt) phase machine.

Publishes ``sensor.fire_safety`` (state = phase). Six phases: clear, pre_alarm, alarm,
hushed, cooldown, offline - see the transition table in the module's design doc. Health
(battery/self-test/offline) rides as attributes and a throttled daily push; it never
becomes a phase of its own except "offline" (device itself unreachable > 30 min).

Owner constraints (hard): never touches locks/burglar features, never writes
sensitivity/pre_alarm on the device's own select - only ever writes "stop". False alarms
and night wake-ups are the worst outcome, so pre_alarm NEVER pushes (kitchen-only Sonos
chime, suppressed entirely while input_boolean.kitchen_cooking_mode is active) and hush is
bounded (hush_minutes, max_hushes_per_episode).

dry_run (default True) is a full gate: while on, this app calls precisely nothing on the
outside world (no call_service, no MobileNotifier/SonosNotifier notify) - every action is
logged with a "[dry-run]" prefix instead, mirroring this codebase's shadow-mode apps
(dryer_shadow.py). test_audience (default ["mikkel"]) narrows real pushes once dry_run is
off, independent of dry_run.

Restart survival: phase/since/episode fields persist to a JSON state file (tmp + os.replace,
see house_events.py / climate_alarm.py). No special "resume alarm on init" code exists - the
ordinary tick evaluates persisted phase against the live smoke reading and re-derives alarm
from scratch if smoke is already on, which also means a crash-and-restart mid-alarm resumes
the repeat cadence from the persisted last_push_at/last_announce_at instead of re-spamming.

Async convention (matches climate_alarm.py/entry_truth.py/lock_health.py): listen_state/
listen_event/run_daily callbacks are plain sync methods that create_task() the real work;
every AppDaemon API call made from inside an async method is awaited, including run_in
(lock_health.py's documented gotcha - an unawaited call from the event loop is a dead
coroutine that never fires).
"""

import json
import os
from datetime import datetime, timedelta, timezone, time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - stdlib always has it on supported Pythons
    ZoneInfo = None

UNAVAILABLE_STATES = (None, "unknown", "unavailable")

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


def _matches_any(text, values):
    """Case-insensitive substring match of `text` against any of `values` - the siren_state
    strings are unverified, so this is deliberately loose rather than an exact-value match."""
    if not text or not values:
        return False
    lowered = str(text).lower()
    return any(str(v).lower() in lowered for v in values)


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

        self.siren_alarm_values = a("siren_alarm_values", ["fire", "alarm", "smoke"])
        self.siren_pre_alarm_values = a("siren_pre_alarm_values", ["pre_alarm", "pre-alarm", "prealarm"])

        self.hush_button_entity = a("hush_button_entity", "input_button.fire_safety_hush")
        self.clear_button_entity = a("clear_button_entity", "input_button.fire_safety_clear")
        self.test_button_entity = a("test_button_entity", "input_button.fire_safety_test")
        self.cooking_mode_entity = a("cooking_mode_entity", "input_boolean.kitchen_cooking_mode")

        self.publish_entity = a("publish_entity", "sensor.fire_safety")
        self.state_file = a("state_file", "/conf/apps/safety/fire_safety_state.json")

        self.dry_run = bool(a("dry_run", True))
        self.test_audience = a("test_audience", ["mikkel"])

        self.hush_minutes = int(a("hush_minutes", 10))
        self.max_hushes_per_episode = int(a("max_hushes_per_episode", 2))
        self.pre_alarm_timeout_min = int(a("pre_alarm_timeout_min", 10))
        self.cooldown_confirm_s = int(a("cooldown_confirm_s", 60))
        self.cooldown_clear_min = int(a("cooldown_clear_min", 15))
        self.offline_after_min = int(a("offline_after_min", 30))
        self.cooking_mode_minutes = int(a("cooking_mode_minutes", 45))
        self.self_test_window_min = int(a("self_test_window_min", 3))
        self.reannounce_interval_s = int(a("reannounce_interval_s", 45))
        self.repush_interval_s = int(a("repush_interval_s", 120))
        self.repush_interval_acked_s = int(a("repush_interval_acked_s", 180))
        self.relights_interval_s = int(a("relights_interval_s", 60))
        self.tick_interval_s = int(a("tick_interval_s", 5))

        self.battery_low_pct = float(a("battery_low_pct", 20))
        self.test_overdue_days = int(a("test_overdue_days", 35))
        self.fault_push_throttle_hours = int(a("fault_push_throttle_hours", 48))
        self.fault_check_time = a("fault_check_time", "09:00:00")
        self.monthly_test_time = a("monthly_test_time", "11:00:00")

        self.push_tag = a("push_tag", "fire_alarm")
        self.push_category = a("push_category", "fire_alarm")
        self.health_category = a("health_category", "fire_health")
        self.health_notify_target = a("health_notify_target", "home")

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
        self.listen_state(self._on_hush_button, self.hush_button_entity)
        self.listen_state(self._on_clear_button, self.clear_button_entity)
        self.listen_state(self._on_test_button, self.test_button_entity)
        self.listen_state(self._on_cooking_on, self.cooking_mode_entity, new="on")
        self.listen_state(self._on_cooking_off, self.cooking_mode_entity, new="off")
        self.listen_event(self._on_notification_action, "mobile_app_notification_action")

        self.run_daily(self._on_fault_check_tick, time(*self._parse_hms(self.fault_check_time)))
        self.run_daily(self._on_monthly_test_tick, time(*self._parse_hms(self.monthly_test_time)))

        # Single schedule: first tick at +2s (covers "smoke already on at boot" - see module
        # docstring), then every tick_interval_s for the time-based transitions/repeats.
        self.run_every(self._tick_cb, "now+2", self.tick_interval_s)

        self.log(f"FireSafety initialized - phase={self.phase}, dry_run={self.dry_run}", level="INFO")

    # ---------- scheduling glue (sync callbacks -> create_task) ----------

    def _tick_cb(self, kwargs):
        self.create_task(self._evaluate())

    def _on_smoke_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._evaluate())

    def _on_siren_change(self, entity, attribute, old, new, kwargs):
        self.create_task(self._evaluate())

    def _on_hush_button(self, entity, attribute, old, new, kwargs):
        if new in UNAVAILABLE_STATES:
            return
        self.create_task(self._hush(self._now(), "the dashboard"))

    def _on_clear_button(self, entity, attribute, old, new, kwargs):
        if new in UNAVAILABLE_STATES:
            return
        self.create_task(self._on_clear_pressed())

    def _on_test_button(self, entity, attribute, old, new, kwargs):
        if new in UNAVAILABLE_STATES:
            return
        self.create_task(self._run_self_test(self._now()))

    def _on_cooking_on(self, entity, attribute, old, new, kwargs):
        self.create_task(self._start_cooking())

    def _on_cooking_off(self, entity, attribute, old, new, kwargs):
        self.create_task(self._stop_cooking())

    def _on_fault_check_tick(self, kwargs):
        self.create_task(self._check_faults(self._now()))

    def _on_monthly_test_tick(self, kwargs):
        if self._now().day != 1:
            return
        self.create_task(self._run_self_test(self._now()))

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
            self.create_task(self._hush(self._now(), person.capitalize()))
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
        try:
            now = self._now()
            smoke = await self._read_state(self.smoke_entity)
            if smoke is None:
                await self._handle_unavailable(now)
                await self._publish(now, None, None)
                return
            if self.unavailable_since is not None:
                self.unavailable_since = None

            siren = await self._read_state(self.siren_state_entity)
            prev_smoke = self.last_smoke
            prev_siren = self.last_siren

            await self._maybe_expire_self_test(now)
            await self._maybe_expire_cooking_mode(now)

            siren_suppressed = self.self_test_until is not None and now < self.self_test_until and smoke == "off"
            # "pre_alarm" contains the substring "alarm" - a pre-alarm match must win that
            # overlap, since siren_alarm_values/siren_pre_alarm_values are matched as
            # case-insensitive substrings (spec'd, not exact-value).
            pre_alarm_siren_match = _matches_any(siren, self.siren_pre_alarm_values)
            alarm_siren_match = _matches_any(siren, self.siren_alarm_values) and not pre_alarm_siren_match
            alarm_condition = smoke == "on" or (not siren_suppressed and alarm_siren_match)
            pre_alarm_condition = (
                smoke == "off"
                and not siren_suppressed
                and not self._cooking_active(now)
                and pre_alarm_siren_match
            )

            if self.phase == "clear":
                if alarm_condition:
                    await self._enter_alarm(now)
                elif pre_alarm_condition:
                    await self._enter_pre_alarm(now)

            elif self.phase == "pre_alarm":
                if smoke == "on":
                    await self._enter_alarm(now)
                elif (
                    not _matches_any(siren, self.siren_pre_alarm_values)
                    or (now - self.since) >= timedelta(minutes=self.pre_alarm_timeout_min)
                ):
                    await self._enter_clear(now)

            elif self.phase == "alarm":
                if self._physical_hush_signal(prev_siren, siren, smoke):
                    await self._hush(now, "the button on the alarm")
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
                new_edge = prev_smoke == "off" and smoke == "on"
                expired_still_on = self.hushed_until is not None and now >= self.hushed_until and smoke == "on"
                if new_edge or expired_still_on:
                    await self._enter_alarm(now)
                elif smoke == "off":
                    self.off_since = self.off_since or now
                    if (now - self.off_since) >= timedelta(seconds=self.cooldown_confirm_s):
                        await self._enter_cooldown(now)

            elif self.phase == "cooldown":
                if smoke == "on":
                    await self._enter_alarm(now)
                elif (now - self.since) >= timedelta(minutes=self.cooldown_clear_min):
                    await self._enter_clear(now)

            elif self.phase == "offline":
                if smoke == "on":
                    await self._enter_alarm(now)
                else:
                    await self._enter_clear(now)

            self.last_smoke = smoke
            self.last_siren = siren
            self._save_state()
            await self._publish(now, smoke, siren)
        except Exception as e:
            self.log(f"evaluate failed: {e}", level="ERROR")

    def _physical_hush_signal(self, prev_siren, siren, smoke):
        """The device's own mute button silences the siren without clearing smoke - infer
        that from an EDGE (siren_state WAS reading an alarm value and just became clear)
        rather than a level check. siren_state's alarm-value semantics are unverified (see
        module docstring / yaml comments), so a level check ("doesn't currently match")
        would misfire on every tick if the real device simply never reports an alarm-list
        value at all while genuinely blaring."""
        if smoke != "on" or siren is None:
            return False
        was_alarming = _matches_any(prev_siren, self.siren_alarm_values)
        now_clear = not _matches_any(siren, self.siren_alarm_values) and not _matches_any(siren, self.siren_pre_alarm_values)
        return was_alarming and now_clear

    async def _handle_unavailable(self, now):
        if self.unavailable_since is None:
            self.unavailable_since = now
        elif self.phase != "offline" and (now - self.unavailable_since) >= timedelta(minutes=self.offline_after_min):
            await self._enter_offline(now)
        self._save_state()

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
        self.phase = "pre_alarm"
        self.since = now
        self._save_state()
        await self._report_feed("pre_alarm")
        await self._chime_pre_alarm(now)

    async def _enter_alarm(self, now):
        new_episode = self.phase not in ("alarm", "hushed", "cooldown")
        if new_episode:
            self.episode_id = now.strftime("%Y%m%d%H%M%S")
            self.episode_started_at = now
            self.hush_count = 0
            self.ack_by = None
        self.phase = "alarm"
        self.since = now
        self.off_since = None
        await self._report_feed("alarm")
        await self._push_alarm(now)
        await self._assert_lights(now)
        self.last_push_at = now
        self.last_lights_assert_at = now
        self._save_state()
        if new_episode:
            await self.run_in(self._delayed_pause_media, 1)
            await self.run_in(self._delayed_announce, 2, episode_id=self.episode_id)
        else:
            # Escalation from hushed/cooldown within the same episode: re-announce now
            # instead of waiting out the ordinary 45s cadence.
            await self._announce_alarm(now)
            self.last_announce_at = now
            self._save_state()

    def _delayed_pause_media(self, kwargs):
        self.create_task(self._pause_media())

    def _delayed_announce(self, kwargs):
        self.create_task(self._announce_after_delay(kwargs.get("episode_id")))

    async def _announce_after_delay(self, episode_id):
        if self.phase != "alarm" or self.episode_id != episode_id:
            return  # superseded (hushed/cleared) before the t+2s announce fired
        now = self._now()
        await self._announce_alarm(now)
        self.last_announce_at = now
        self._save_state()

    async def _hush(self, now, by_text):
        if self.phase != "alarm":
            self.log(f"Hush ignored - phase is {self.phase}, not alarm", level="DEBUG")
            return
        if self.hush_count >= self.max_hushes_per_episode:
            self.log(f"Hush limit ({self.max_hushes_per_episode}) reached for episode {self.episode_id}", level="WARNING")
            await self._push_hush_limit(now)
            return
        self.hush_count += 1
        self.hushed_by = by_text
        self.hushed_until = now + timedelta(minutes=self.hush_minutes)
        self.phase = "hushed"
        self.since = now
        self.off_since = None
        await self._write_alarm_stop(now)
        await self._report_feed("hushed", by=by_text)
        await self._push_hushed(now)
        await self._announce_hushed(now)
        self._save_state()

    async def _ack(self, person):
        if self.phase not in ("alarm", "hushed"):
            return
        self.ack_by = person
        self._save_state()
        self.log(f"{person} acknowledged the fire alarm", level="INFO")

    async def _enter_cooldown(self, now):
        self.phase = "cooldown"
        self.since = now
        self.off_since = None
        self._save_state()
        await self._report_feed("cooldown")

    async def _enter_clear(self, now):
        was_active = self.episode_id is not None
        duration_min = None
        if was_active and self.episode_started_at:
            duration_min = max(0, int((now - self.episode_started_at).total_seconds() // 60))
        self.phase = "clear"
        self.since = now
        self.episode_id = None
        self.episode_started_at = None
        self.hush_count = 0
        self.hushed_by = None
        self.hushed_until = None
        self.ack_by = None
        self.off_since = None
        await self._report_feed("clear")
        if was_active:
            await self._clear_lights()
            await self._push_all_clear(now, duration_min or 0)
            await self._announce_all_clear(now)
        self._save_state()

    async def _enter_offline(self, now):
        self.phase = "offline"
        self.since = now
        self._save_state()
        await self._report_feed("offline")

    async def _on_clear_pressed(self):
        now = self._now()
        smoke = await self._read_state(self.smoke_entity)
        if smoke != "off":
            self.log("Clear button pressed but smoke is still on - ignoring", level="WARNING")
            return
        if self.phase == "clear":
            return
        await self._enter_cooldown(now)

    # ---------- self-test ----------

    async def _maybe_expire_self_test(self, now):
        if self.self_test_until and now >= self.self_test_until:
            self.self_test_until = None
            self._save_state()

    async def _run_self_test(self, now):
        await self._call(
            f"switch/turn_on {self.self_test_switch_entity}",
            "switch/turn_on", entity_id=self.self_test_switch_entity,
        )
        self.self_test_until = now + timedelta(minutes=self.self_test_window_min)
        self.last_self_test_at = now
        self._save_state()
        await self._push_health(now, "Kitchen smoke alarm self-test ran.")

    # ---------- cooking mode ----------

    async def _start_cooking(self):
        now = self._now()
        self.cooking_until = now + timedelta(minutes=self.cooking_mode_minutes)
        self._save_state()

    async def _stop_cooking(self):
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
        changed = False
        for key, message in faults.items():
            last = self.last_fault_push_at.get(key)
            if last and (now - last) < throttle:
                continue
            await self._push_health(now, message)
            self.last_fault_push_at[key] = now
            changed = True
        if changed:
            self._save_state()

    # ---------- repeat cadence (alarm phase only) ----------

    async def _maybe_repeat_alarm_actions(self, now):
        if self.last_push_at is None or (now - self.last_push_at) >= self._repush_interval():
            await self._push_alarm(now)
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
        docstring on dry_run being a full gate, never partial."""
        if self.dry_run:
            self.log(f"[dry-run] would {dry_run_desc}")
            return
        try:
            await self.call_service(service, **kwargs)
        except Exception as e:
            self.log(f"{dry_run_desc} failed: {e}", level="WARNING")

    def _get_notifier(self):
        if self.mobile_notifier is None:
            self.log("MobileNotifier unavailable - push skipped", level="WARNING")
        return self.mobile_notifier

    def _get_sonos_notifier(self):
        if self.sonos_notifier is None:
            self.log("SonosNotifier unavailable - announce skipped", level="WARNING")
        return self.sonos_notifier

    async def _notify(self, dry_run_desc, **kwargs):
        if self.dry_run:
            self.log(f"[dry-run] would push: {dry_run_desc}")
            return
        notifier = self._get_notifier()
        if notifier is None:
            return
        try:
            await notifier.notify(**kwargs)
        except Exception as e:
            self.log(f"push failed ({dry_run_desc}): {e}", level="WARNING")

    def _announce(self, dry_run_desc, **kwargs):
        if self.dry_run:
            self.log(f"[dry-run] would announce: {dry_run_desc}")
            return
        sonos = self._get_sonos_notifier()
        if sonos is None:
            return
        self.submit_to_executor(sonos.notify, **kwargs)

    async def _write_alarm_stop(self, now):
        await self._call(
            f"select/select_option stop on {self.alarm_select_entity}",
            "select/select_option", entity_id=self.alarm_select_entity, option="stop",
        )

    async def _assert_lights(self, now):
        if self.dry_run:
            self.log(f"[dry-run] would turn on alarm lights: {self.alarm_lights}")
            return
        try:
            for boolean in self.alarm_light_manual_booleans:
                await self.call_service("input_boolean/turn_on", entity_id=boolean)
            if self.alarm_lights:
                await self.call_service("light/turn_on", entity_id=self.alarm_lights, brightness_pct=100, kelvin=4000)
        except Exception as e:
            self.log(f"alarm lights on failed: {e}", level="WARNING")

    async def _clear_lights(self):
        if self.dry_run:
            self.log("[dry-run] would release alarm-light manual overrides")
            return
        try:
            for boolean in self.alarm_light_manual_booleans:
                await self.call_service("input_boolean/turn_off", entity_id=boolean)
        except Exception as e:
            self.log(f"alarm lights release failed: {e}", level="WARNING")

    async def _pause_media(self):
        if not self.media_pause_players:
            return
        await self._call(
            f"pause media: {self.media_pause_players}",
            "media_player/media_pause", entity_id=self.media_pause_players,
        )

    async def _push_alarm(self, now):
        episode = self.episode_id

        def actions_for(person):
            return [
                {"action": f"FIRE_HUSH_{episode}_{person}", "title": "Silence – false alarm"},
                {"action": f"FIRE_ACK_{episode}_{person}", "title": "I'm checking"},
                {"action": "URI", "title": "Call 112", "uri": "tel:112"},
            ]

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
            test_audience=self.test_audience,
        )

    async def _push_hushed(self, now):
        rearm = self._fmt(self.hushed_until)
        await self._notify(
            "hush confirmation",
            title="Smoke in the kitchen",
            message=f"{self.hushed_by} silenced the kitchen alarm. It re-arms at {rearm}.",
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            test_audience=self.test_audience,
        )

    async def _push_hush_limit(self, now):
        await self._notify(
            "hush-limit-reached notice",
            title="Fire alarm",
            message="Hush limit reached - the kitchen alarm can't be silenced again this episode.",
            target="all",
            data={"data": {"tag": f"{self.push_tag}_hush_limit"}},
            category=self.push_category,
            test_audience=self.test_audience,
        )

    async def _push_all_clear(self, now, duration_min):
        await self._notify(
            "all-clear",
            title="Fire alarm",
            message=f"All clear — the kitchen alarm has reset. It was sounding for {duration_min} minutes.",
            target="all",
            data={"data": {"tag": self.push_tag}},
            category=self.push_category,
            test_audience=self.test_audience,
        )

    async def _push_health(self, now, message):
        await self._notify(
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
            cause, effect = "Kitchen smoke cleared", "Confirming the kitchen alarm is over"
        elif phase == "clear":
            cause, effect = "Kitchen alarm confirmed clear", "Lights and overrides released"
        elif phase == "offline":
            cause, effect = "Kitchen smoke alarm stopped reporting", f"Marked offline after {self.offline_after_min} minutes"
        else:
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
        try:
            await self.set_state(
                self.publish_entity,
                state=self.phase,
                replace=True,
                attributes={
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
                },
            )
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
        }
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(data, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"state save failed: {e}", level="WARNING")
