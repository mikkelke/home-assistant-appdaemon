import appdaemon.plugins.hass.hassapi as hass # type: ignore
import asyncio
import json
import os
from datetime import date, time, timedelta

import cover_util
import room_state_darkness
import solar_window

# Shared layers this routine ORCHESTRATES rather than reimplements: who owns the
# bedroom blind (apps/blinds/blind_arbiter.py) and how a room is faded up
# (apps/lights/light_ramp.py). Imported defensively on purpose - this is the alarm
# that wakes a real person, so a missing or broken shared module must cost the house
# a layer, never the wake-up. Every use site below falls back to the direct service
# call this app made before the split; see _set_cover_position / _ramp_tick.
try:
    import blind_arbiter
except Exception:  # pragma: no cover - exercised via the None-module tests
    blind_arbiter = None
try:
    import light_ramp
except Exception:  # pragma: no cover - exercised via the None-module tests
    light_ramp = None

REQUIRED_TOP_LEVEL = [
    "alarm_time_entity", "alarm_enabled_entity",
    "presence_persons",
    "media_master", "media_join", "media_content_id", "media_content_type",
    "volume_entities", "volume_start", "volume_target", "volume_step", "volume_interval_sec",
    "covers", "closed_is_100", "bedroom_cover_target", "bathroom_cover_target",
    "bedroom_state_entity",
    "light_entity",
    "adaptive_main_switch", "adaptive_brightness_switch",
    "ramp_start_pct", "ramp_step_pct", "ramp_interval_sec", "ramp_max_pct",
    "log", "log_level",
]


class WakeupRoutine(hass.Hass):
    def initialize(self):
        # Ensure all required args are present
        missing = [k for k in REQUIRED_TOP_LEVEL if k not in self.args]
        if missing:
            self.log(f"[wake] Missing required config keys: {missing}", level="ERROR")
            return

        # Fast access + typed values (no defaults!)
        A = self.args
        self.user_log = A["log"]
        self.log_level = A["log_level"]

        self.alarm_time_entity = A["alarm_time_entity"]
        self.alarm_enabled_entity = A["alarm_enabled_entity"]
        self.persons = list(A["presence_persons"])

        self.master_player = A["media_master"]
        self.join_players = list(A["media_join"])
        self.stream_id = A["media_content_id"]
        self.stream_type = A["media_content_type"]
        # Optional fallback URL for failover
        self.fallback = A.get("media_fallback")
        # Phone target for the total-media-failure page (see _verify_media_playback)
        self.notify_target = A.get("notify_target", ["mikkel"])
        # Playback verification timing. The primary is a DR HLS stream: playlist +
        # segment buffering routinely takes 3-8 s before the player reports "playing".
        # The old fixed 2 s check produced 18 false failovers Mar-Jul 2026 (~15 % of
        # mornings, every one "state=idle" that was really just still buffering) and
        # on 16 of those 18 mornings the primary was audibly playing by the +4 s
        # recheck. First check late, and recheck once before ever switching streams.
        self.media_verify_delay_sec = int(A.get("media_verify_delay_sec", 12))
        self.media_verify_recheck_sec = int(A.get("media_verify_recheck_sec", 5))
        self.media_still_playing_check_sec = int(A.get("media_still_playing_check_sec", 45))

        self.volume_entities = list(A["volume_entities"])
        self.volume_start = float(A["volume_start"])
        self.volume_target = float(A["volume_target"])
        self.volume_step = float(A["volume_step"])
        self.volume_interval_sec = int(A["volume_interval_sec"]) 

        self.closed_is_100 = bool(A["closed_is_100"])
        # A "closed" blind can report 98-99 instead of a clean 100 (battery/motor slack),
        # so the morning open must treat near-closed as closed - otherwise the wake nudge
        # never fires and the room stays dark (user-reported 2026-07-20). Read via cover_util.
        self.closed_threshold = int(A.get("closed_threshold", 95))
        self.bedroom_cover = A["covers"]["bedroom"]
        self.bathroom_cover = A["covers"]["bathroom"]
        # The single owner of the bedroom blind (apps/blinds/bedroom_blind_owner.py).
        # The wake submits its blind request there; a manual hold outranking it means
        # the blind stays where the human put it AND the light ramp still runs (see
        # _set_cover_position / _blind_wait_complete). Looked up lazily per use, and
        # every failure shape degrades to the direct write - this is the alarm.
        self.owner_app = A.get("owner_app", "BedroomBlindOwner")
        self.bedroom_cover_target = int(A["bedroom_cover_target"])
        self.bathroom_cover_target = int(A["bathroom_cover_target"])
        # Heat-wave override: the normal wake nudge (38, i.e. mostly open) floods a hot
        # bedroom with morning sun/heat if the blind was closed overnight. Toggle-gated
        # (user, 2026-07-15) rather than auto-detected - simple, predictable, matches the
        # same manual-toggle pattern as the AC. Off by default = today's normal behavior.
        self.heat_wave_entity = A.get("heat_wave_entity", "input_boolean.heat_wave_mode")
        self.bedroom_cover_target_heat_wave = int(A.get("bedroom_cover_target_heat_wave", 72))
        self._current_bedroom_wake_target = self.bedroom_cover_target
        # Automatic heat-block: closes the bedroom blind to bedroom_cover_target_heat_wave
        # instead of bedroom_cover_target at wake time when direct sun is on the window, the
        # forecast high is hot, or it's already warm outside. heat_wave_entity above is now a
        # manual FORCE-ON over this decision (see _decide_bedroom_wake_target).
        self.heat_auto_enable = bool(A.get("heat_auto_enable", True))
        self.heat_wave_manual_auto_clear = bool(A.get("heat_wave_manual_auto_clear", True))
        # Venting beats shading (user 2026-08-09). The openable pane is only clear at
        # bedroom_cover_target (38); every heat-block position leaves the window physically
        # unopenable. So when the planner is NOT calling for AC, the blind must land on the
        # default whatever the sun is doing - shading a room we intend to ventilate trades away
        # the one cooling method actually in use for the one that is not. Incident 2026-08-09:
        # plan said "windows", the wake routine parked the blind at 72, the window could not be
        # opened, and the sealed dark room then also kept the lights on until 08:44.
        self.sleep_plan_entity = A.get("sleep_plan_entity", "sensor.sleep_plan")
        raw_vent = A.get("vent_plan_states", ["windows", "nothing"])
        self.vent_plan_states = {str(s).strip().lower() for s in raw_vent} if isinstance(raw_vent, list) else set()
        self.sun_entity = A.get("sun_entity", "sun.sun")
        self.solar_radiation_entity = A.get("solar_radiation_entity", "sensor.gw2000a_solar_radiation")
        self.window_azimuth = float(A.get("window_azimuth", 70))
        self.az_tolerance = float(A.get("az_tolerance", 55))
        self.min_elevation = float(A.get("min_elevation", 3))
        self.radiation_threshold = float(A.get("radiation_threshold", 250))
        self.outdoor_temp_entity = A.get("outdoor_temp_entity", "sensor.gw2000a_outdoor_temperature")
        self.live_outdoor_hot_c = float(A.get("live_outdoor_hot_c", 22.0))
        self.weather_entity = A.get("weather_entity", "weather.forecast_home")
        self.forecast_high_threshold_c = float(A.get("forecast_high_threshold_c", 25.0))
        self.forecast_refresh_min = int(A.get("forecast_refresh_min", 30))
        self.forecast_max_age_min = int(A.get("forecast_max_age_min", 180))
        self._forecast_high_c = None
        self._forecast_high_at = None

        self.bedroom_state_entity = A["bedroom_state_entity"]
        self.light_entity = A["light_entity"]

        # Optional: presence entity to determine if room is empty at start
        self.bedroom_presence_entity = A.get("bedroom_presence_entity")

        self.adaptive_main_switch = A["adaptive_main_switch"]
        self.adaptive_brightness_switch = A["adaptive_brightness_switch"]

        # Optional sleep mode helpers for post-evaluation wake-up
        self.mikkel_sleep_entity = A.get("mikkel_sleep_entity")

        self.ramp_start_pct = int(A["ramp_start_pct"])
        self.ramp_step_pct = int(A["ramp_step_pct"])
        self.ramp_interval_sec = int(A["ramp_interval_sec"])
        self.ramp_max_pct = int(A["ramp_max_pct"])
        # Bed-light session latch owned by bedroom_lights (input_boolean.bedroom_bed_session,
        # driven by the reliable local FP300 mmWave presence) - the wake ramp yields to
        # ceiling logic once the session ends, instead of tracking in-bed sensors itself.
        # See _bed_session_active / _on_bed_session_change.
        self.bed_session_entity = A.get("bed_session_entity", "input_boolean.bedroom_bed_session")
        self._session_listener = None
        # The bedroom lights' manual-override toggle pauses ALL automatic light actions -
        # including this wake ramp (user hit exactly this 2026-07-16: override on + lights
        # manually off, and a stale ramp turned them back on). Blinds/media still run; the
        # override is only about lights.
        self.manual_override_entity = A.get("manual_override_boolean", "input_boolean.bedroom_lights_manual")
        # The armed blind-position listener must not outlive the wake window: without a
        # deadline it can fire HOURS later, the first time the blind happens to cross the
        # wake target again (solar shade repositioning, manual move), and start the ramp
        # mid-day. Measured from alarm fire.
        self.wake_light_window_min = int(A.get("wake_light_window_min", 60))
        self._alarm_fired_at = None
        # Dynamic settle after blinds reach target (based on outdoor lux clue).
        self.post_blind_settle_dark_sec = max(0, int(A.get("post_blind_settle_dark_sec", 10)))
        self.post_blind_settle_mid_sec = max(0, int(A.get("post_blind_settle_mid_sec", 25)))
        self.post_blind_settle_bright_sec = max(0, int(A.get("post_blind_settle_bright_sec", 60)))
        self.post_blind_outdoor_mid_lux = float(A.get("post_blind_outdoor_mid_lux", 3000))
        self.post_blind_outdoor_bright_lux = float(A.get("post_blind_outdoor_bright_lux", 5000))
        self.outdoor_lux_entity = A.get("outdoor_lux_entity", "sensor.gw2000a_solar_lux")

        # Simple validations
        if not (0.0 <= self.volume_start <= 1.0 and 0.0 <= self.volume_target <= 1.0 and self.volume_target >= self.volume_start):
            self.log("[wake] Invalid volume settings", level="ERROR", log=self.user_log); return
        if self.ramp_start_pct < 1 or self.ramp_step_pct < 1 or not (1 <= self.ramp_max_pct <= 100):
            self.log("[wake] Invalid ramp settings", level="ERROR", log=self.user_log); return

        # State
        self.alarm_timer = None
        self.alarm_run_at = None
        self.cover_listener = None
        self.cancel_listeners = []
        self.ramp_timer = None
        self.volume_timer = None
        self.ramp_active = False
        self.volume_active = False
        self.current_pct = None
        self.media_attempt = 0
        self._media_recheck_used = False
        # One feed line / one page per wake, however many times verification loops
        self._media_fallback_reported = False
        self._media_exhausted_reported = False
        # True only while _heartbeat_check is invoking _alarm_fire, so the rescue feed
        # entry rides an alarm that actually fired (not a gated skip)
        self._fired_via_heartbeat = False
        # True only while _late_catchup_after_restart is invoking _alarm_fire, so that call
        # can bypass the +/-90s trigger window below (same style as _fired_via_heartbeat)
        self._late_catchup_in_progress = False
        # True only while _on_wake_now is invoking _alarm_fire: a manual "Wake up now" press
        # must always run, whether or not input_boolean.wakeup_bedroom is armed (user,
        # 2026-08-10 - 3 of 4 presses that morning died in _alarm_fire's toggle guard). Read
        # by the trigger-window guard, the toggle guard, and _attach_cancel_listeners below.
        self._manual_fire_in_progress = False

        # Restart-survival state: small JSON file, deploy_advisor's path convention +
        # house_events' atomic tmp+replace write (see _load_state/_save_state). An AD
        # restart (every deploy) drops last_fire_date otherwise, which can both duplicate
        # a wake-up that already fired and repeat a missed-alarm push on every restart.
        self.state_file = A.get("state_file", "/conf/apps/rutines/wakeup_bedroom_state.json")
        self._state = self._load_state()
        self.last_fire_date = None  # heartbeat duplicate guard
        stored_fire_date = self._state.get("last_fire_date")
        if stored_fire_date:
            try:
                self.last_fire_date = date.fromisoformat(stored_fire_date)
            except (TypeError, ValueError):
                self.last_fire_date = None

        # Respect per-app log_level
        self.set_log_level(self.log_level)

        # Schedule alarm and reschedule on time change
        self._schedule_daily_alarm()
        self.listen_state(self._on_alarm_time_changed, self.alarm_time_entity)
        # "Wake up now" (user 2026-07-29): a dashboard button press starts the sequence
        # THIS moment -- the press IS the alarm. Optional knob; listener only when set.
        self.wake_now_entity = self.args.get("wake_now_entity", "input_button.wake_up_now")
        if self.wake_now_entity:
            self.listen_state(self._on_wake_now, self.wake_now_entity)
        # Restart survival, init-only: reconcile Adaptive Lighting first (a late catch-up
        # ramp turns it off itself moments later, so reconciling first never fights it),
        # then check for a fire that fell in the gap between the 120s grace window above
        # and the 60s heartbeat below - an AD restart can easily eat more than either.
        self._reconcile_adaptive_lighting_after_restart()
        self._late_catchup_after_restart()
        # Always-on minute heartbeat as the alarm's safety net. History (Sep 19 - Nov 16
        # 2025): the AppDaemon scheduler developed a constant +67 min phase shift, so the
        # run_daily callback arrived 67 minutes late every single day and was rejected by
        # the ±90 s trigger-window guard; 47 mornings were saved only because a leaked
        # minute-level run_every from the old arm/disarm watchdog kept ticking. A fixed
        # 60 s cadence survives that failure mode (a phase shift moves the tick within the
        # minute, it never removes it), which arm-on-restart/config-change logic does not -
        # the old watchdog was armed on only a handful of days per month. An AD restart on
        # 2025-11-16 (4.5.12) cleared the shift; zero missed fires in the 8 months since,
        # so this should never fire - if it does, that is the scheduler regressing again.
        self.heartbeat = self.run_every(
            self._heartbeat_check, self.datetime() + timedelta(seconds=70), 60
        )
        # Periodic forecast-high refresh feeding the heat-block decision (see
        # _decide_bedroom_wake_target); cached with a max age so a stale forecast never
        # silently drives the wake-time blind target.
        self.forecast_timer = self.run_every(
            lambda kw: self.create_task(self._refresh_forecast_high()),
            self.datetime() + timedelta(seconds=20), self.forecast_refresh_min * 60)

    # ---------- scheduling ----------
    def _schedule_daily_alarm(self):
        tstr = self.get_state(self.alarm_time_entity)
        if not tstr:
            self.log(f"[wake] No time in {self.alarm_time_entity}", level="ERROR", log=self.user_log); return
        try:
            hh, mm, *ss = [int(x) for x in tstr.split(":")]
            sched = time(hh, mm, ss[0] if ss else 0)
        except Exception as e:
            self.log(f"[wake] Bad time '{tstr}': {e}", level="ERROR", log=self.user_log); return

        # Avoid AppDaemon "Invalid callback handle" warnings by only cancelling running timers
        if self.alarm_timer:
            try:
                if self.timer_running(self.alarm_timer):
                    self.cancel_timer(self.alarm_timer)
            except Exception:
                pass
        self.alarm_timer = self.run_daily(self._alarm_fire, sched)

        # Also schedule an absolute run_at for the computed time today (reliable immediate test)
        try:
            if self.alarm_run_at:
                try:
                    if self.timer_running(self.alarm_run_at):
                        self.cancel_timer(self.alarm_run_at)
                except Exception:
                    pass
                self.alarm_run_at = None
        except Exception:
            self.alarm_run_at = None

        # If the new time is just in the past (e.g., user testing), fire once now
        try:
            now_dt = self.datetime()
            today_run = now_dt.replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
            grace_seconds = 120
            if now_dt >= today_run:
                delta = (now_dt - today_run).total_seconds()
                if 0 <= delta <= grace_seconds:
                    if self.last_fire_date == now_dt.date():
                        # Already fired today - persisted, so this also catches an AD
                        # restart landing inside this same grace window. Refiring here
                        # would duplicate a wake-up that already ran moments earlier.
                        self.log(f"[wake] Time just passed {int(delta)}s ago but already fired today - not refiring", log=self.user_log)
                    elif self.get_state(self.alarm_enabled_entity) == "on" and not self._both_away():
                        self.log(f"[wake] Time just passed {int(delta)}s ago -> firing now, and scheduled daily at {sched}", log=self.user_log)
                        self.run_in(self._alarm_fire, 1)
                        return
            else:
                # Schedule a one-shot at today's time to ensure the next tick fires
                self.alarm_run_at = self.run_at(self._alarm_fire, today_run)
                self.log(f"[wake] Also scheduled one-shot today at {today_run.time()}", log=self.user_log)
        except Exception:
            pass

        self.log(f"[wake] Scheduled daily at {sched}", log=self.user_log)

    def _on_wake_now(self, entity, attr, old, new, kwargs):
        """input_button.wake_up_now pressed: run the wake sequence immediately, no matter
        whether input_boolean.wakeup_bedroom is armed (user, 2026-08-10: "wake up now" must
        always work). Sets _manual_fire_in_progress instead of reusing the late-catchup
        flag - that flag's own docstring says it is read ONLY by the trigger-window guard,
        but a manual press also needs to bypass _alarm_fire's toggle guard and suppress the
        toggle-off cancel listener in _attach_cancel_listeners, so it gets a dedicated flag
        that all three now honour."""
        if new in (None, "unknown", "unavailable"):
            return
        self.log("[wake] Wake up now pressed -- starting the sequence", log=self.user_log)
        self._manual_fire_in_progress = True
        try:
            # Fire FIRST, set the alarm time SECOND - this order is load-bearing.
            # _alarm_fire sets self.last_fire_date = today near its top, before any early
            # return. Writing the alarm time afterwards triggers _on_alarm_time_changed ->
            # _schedule_daily_alarm, whose "time just passed -> fire now" grace window only
            # refires when last_fire_date != today. Firing first means that guard already
            # sees today's date and stays a no-op; firing second would double-fire the whole
            # wake sequence a moment later.
            self._alarm_fire({})
            self._set_alarm_time_to_now_slot()
        finally:
            self._manual_fire_in_progress = False

    def _set_alarm_time_to_now_slot(self):
        """After a manual "Wake up now" press, snap the configured alarm time to the
        current 5-minute slot (user, 2026-08-10: "the press will set the alarm to the time
        XX:X0 or XX:X5 ... for the current time on click"). Never touches
        input_boolean.wakeup_bedroom - the user keeps it off deliberately; only the time
        changes. Must never raise: a failure here must not break the wake sequence that
        already ran above."""
        try:
            now = self.datetime()
            # Floor, never round up. A slot in the future would make _schedule_daily_alarm
            # (via the _on_alarm_time_changed listener this triggers) arm a fresh one-shot
            # run_at(self._alarm_fire, today_run) for later today - and unlike the grace
            # refire / heartbeat paths, _alarm_fire itself is NOT guarded by last_fire_date,
            # so that one-shot would fire the whole sequence again a few minutes from now. A
            # floored slot is already in the past, where the last_fire_date guard set by the
            # _alarm_fire call above already covers it.
            slot_min = (now.minute // 5) * 5
            hhmmss = "%02d:%02d:00" % (now.hour, slot_min)
            current = str(self.get_state(self.alarm_time_entity) or "")[:5]
            if current == hhmmss[:5]:
                return  # already at this slot - avoid pointless churn and a needless reschedule
            self.call_service(
                "input_datetime/set_datetime", entity_id=self.alarm_time_entity, time=hhmmss
            )
            self.log(f"[wake] Wake up now: alarm time set to {hhmmss[:5]}", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] Wake up now: failed to set alarm time: {e}", level="WARNING", log=self.user_log)

    def _on_alarm_time_changed(self, entity, attr, old, new, kwargs):
        self._schedule_daily_alarm()

    def _heartbeat_check(self, kwargs):
        """Fire the routine if run_daily missed the alarm minute (see initialize).

        The [5, 90) window gives a healthy run_daily a 5 s head start (it lands at
        :00.0x when the scheduler is well), still passes _alarm_fire's ±90 s guard,
        and tolerates one delayed tick. last_fire_date (set inside _alarm_fire)
        makes the normal day a date-compare no-op."""
        try:
            now_dt = self.datetime()
            if self.last_fire_date == now_dt.date():
                return
            if self.get_state(self.alarm_enabled_entity) != "on":
                return
            tstr = self.get_state(self.alarm_time_entity)
            if not tstr:
                return
            hh, mm, *ss = [int(x) for x in str(tstr).split(":")]
            target = now_dt.replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
            delta = (now_dt - target).total_seconds()
            if 5 <= delta < 90:
                # The scheduled run_daily should have fired seconds ago - this is the
                # signal the 2025 scheduler bug is back, not routine operation.
                self.log("[wake] Heartbeat firing alarm - scheduled run_daily missed its minute",
                         level="WARNING", log=self.user_log)
                self._fired_via_heartbeat = True
                try:
                    self._alarm_fire(None)
                finally:
                    self._fired_via_heartbeat = False
        except Exception as e:
            self.log(f"[wake] Heartbeat check error: {e}", level="WARNING", log=self.user_log)

    # ---------- restart survival (state file) ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_state(self):
        try:
            tmp = self.state_file + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self._state, f)
            os.replace(tmp, self.state_file)
        except Exception as e:
            self.log(f"[wake] state save failed: {e}", level="WARNING", log=self.user_log)

    def _reconcile_adaptive_lighting_after_restart(self):
        """Init-only. The ramp turns adaptive_brightness_switch off at ramp start and only
        restores it from _stop_all/_stop_light_ramp - both die with the process on an AD
        restart mid-ramp, leaving the switch off with nothing left to ever turn it back on.
        ramp_active is always False this early in a fresh init, so this only ever restores a
        switch a PAST process left off; called before _late_catchup_after_restart so a catch-up
        ramp starting moments later still gets to turn the switch off itself (see initialize)."""
        try:
            if self.get_state(self.adaptive_brightness_switch) == "off" and not self.ramp_active:
                self.turn_on(self.adaptive_brightness_switch)
                self.log(
                    "[wake] Adaptive Lighting brightness adaptation was off with no ramp "
                    "running (restart mid-ramp?) - restoring it",
                    level="INFO", log=self.user_log,
                )
        except Exception as e:
            self.log(f"[wake] Adaptive Lighting reconcile failed: {e}", level="WARNING", log=self.user_log)

    def _late_catchup_after_restart(self):
        """Init-only. Covers the gap between _schedule_daily_alarm's 120s grace refire and
        the 60s heartbeat's own [5, 90)s window: an AD restart (deploy) can easily take
        longer than either allows for. Only fires the full routine while the in-bed signal
        this app already trusts for the light ramp (_bed_session_active) says someone is
        still there; past 30 min a late wake-up routine would be more startling than useful,
        so we just push once instead of firing blind."""
        try:
            if self.get_state(self.alarm_enabled_entity) != "on":
                return
            tstr = self.get_state(self.alarm_time_entity)
            if not tstr:
                return
            hh, mm, *ss = [int(x) for x in str(tstr).split(":")]
            now_dt = self.datetime()
            today_run = now_dt.replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
            if now_dt < today_run:
                return
            delta = (now_dt - today_run).total_seconds()
            if self.last_fire_date == now_dt.date():
                return  # already fired today - nothing to catch up on

            if 120 < delta < 1800:
                if not self._bed_session_active():
                    self.log("[wake] Late catch-up window but bed session inactive - not firing", log=self.user_log)
                    return
                self.log(
                    f"[wake] Late catch-up after restart - alarm time passed {int(delta)}s ago, "
                    "still in bed - firing now",
                    level="WARNING", log=self.user_log,
                )
                self._late_catchup_in_progress = True
                try:
                    self._alarm_fire(None)
                finally:
                    self._late_catchup_in_progress = False
                return

            if delta >= 1800:
                today_str = now_dt.date().isoformat()
                if self._state.get("missed_push_date") == today_str:
                    return  # already pushed for today - don't repeat on every restart
                self._state["missed_push_date"] = today_str
                self._save_state()
                self.log("[wake] Alarm missed during a restart (>=30min late, unfired) - notifying",
                         level="WARNING", log=self.user_log)
                try:
                    notifier = self.get_app("MobileNotifier")
                    self.create_task(notifier.notify(
                        title="Wake-up alarm",
                        message="Wakeup alarm was missed during a restart",
                        target=self.notify_target,
                    ))
                except Exception as e:
                    self.log(f"[wake] missed-alarm notify failed: {e}", level="WARNING", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] Late catch-up check failed: {e}", level="WARNING", log=self.user_log)

    # ---------- heat-block wake target ----------
    def _num(self, entity, default):
        try:
            v = self.get_state(entity)
            if v in (None, "", "unknown", "unavailable"): return default
            return float(v)
        except (TypeError, ValueError):
            return default

    def _num_attr(self, entity, attr, default):
        try:
            v = self.get_state(entity, attribute=attr)
            if v in (None, "", "unknown", "unavailable"): return default
            return float(v)
        except (TypeError, ValueError):
            return default

    async def _refresh_forecast_high(self):
        try:
            resp = await asyncio.wait_for(
                self.call_service("weather/get_forecasts", entity_id=self.weather_entity,
                                  type="daily", return_response=True), timeout=12)
            # In an async AppDaemon method the sync-wrapped ADAPI calls return a Task,
            # so datetime() must be awaited; it yields a naive local datetime that stays
            # consistent with the naive self.datetime() used by the freshness check below.
            now = await self.datetime()
            hi = solar_window.daily_high_from_forecast(resp, now.date().isoformat())
            if hi is not None:
                self._forecast_high_c = float(hi)
                self._forecast_high_at = now
        except Exception as e:
            self.log(f"[wake] forecast refresh failed: {e}", level="WARNING", log=self.user_log)

    def _venting_tonight(self):
        """True when the sleep planner is NOT calling for AC, i.e. the room is meant to be
        cooled by opening the window. Unknown/missing plan returns False: without a positive
        signal the old heat-block behaviour stands, because a hot sealed room is a worse
        failure than an un-openable one."""
        try:
            state = self.get_state(self.sleep_plan_entity)
        except Exception:
            return False
        if state in (None, "", "unknown", "unavailable"):
            return False
        return str(state).strip().lower() in self.vent_plan_states

    def _decide_bedroom_wake_target(self):
        """Where this routine wants the blind at the alarm - resolved through the shared
        precedence in blind_arbiter instead of a private if-statement.

        The raw heat decision still runs FIRST and unconditionally: it carries the manual
        force flag's auto-clear side effect, and skipping it would leave a stale force
        armed for the next morning (pinned by test_wakeup_heat.VentingBeatsShading).
        Venting outranks every heat reason, manual force included - see blind_arbiter's
        module docstring for the 2026-08-09 incident that put it there.
        """
        target, reason = self._decide_bedroom_wake_target_raw()
        # Stash which SOURCE the winning target belongs to ("vent" or "wake") so the
        # blind-owner request below can claim the right rank - additive only, the
        # (position, reason) return shape is pinned by test_wakeup_heat.
        self._current_bedroom_wake_source = getattr(blind_arbiter, "SOURCE_WAKE", "wake") if blind_arbiter else "wake"
        if target == self.bedroom_cover_target or not self._venting_tonight():
            return target, reason
        vent_reason = f"venting tonight, window must open (was: {reason})"
        if blind_arbiter is None:
            # Fail-safe: the shared layer is gone, so apply the same rule locally rather
            # than let a hot morning park the blind where the window cannot open.
            self._current_bedroom_wake_source = "vent"
            return self.bedroom_cover_target, vent_reason
        winner = blind_arbiter.arbitrate([
            blind_arbiter.BlindRequest(
                blind_arbiter.SOURCE_VENT, self.bedroom_cover_target, vent_reason),
            blind_arbiter.BlindRequest(blind_arbiter.SOURCE_WAKE, target, reason),
        ])
        if winner is None:
            return target, reason
        self._current_bedroom_wake_source = winner.source
        return winner.position, winner.reason

    def _decide_bedroom_wake_target_raw(self):
        if self.get_state(self.heat_wave_entity) == "on":
            if self.heat_wave_manual_auto_clear:
                try: self.turn_off(self.heat_wave_entity)
                except Exception: pass
            return self.bedroom_cover_target_heat_wave, "manual force"
        if not self.heat_auto_enable:
            return self.bedroom_cover_target, "auto disabled"
        az = self._num_attr(self.sun_entity, "azimuth", None)
        elev = self._num_attr(self.sun_entity, "elevation", None)
        rad = self._num(self.solar_radiation_entity, None)
        if None not in (az, elev, rad) and solar_window.beam_heat(
                az, elev, rad, self.window_azimuth, self.az_tolerance,
                self.min_elevation, self.radiation_threshold):
            return self.bedroom_cover_target_heat_wave, f"sun on window (rad {rad:.0f})"
        fh, at = self._forecast_high_c, self._forecast_high_at
        fresh = at is not None and (self.datetime() - at).total_seconds() <= self.forecast_max_age_min * 60
        if fh is not None and fresh and fh >= self.forecast_high_threshold_c:
            return self.bedroom_cover_target_heat_wave, f"forecast high {fh:.1f}C"
        t = self._num(self.outdoor_temp_entity, None)
        if t is not None and t >= self.live_outdoor_hot_c:
            return self.bedroom_cover_target_heat_wave, f"warm now {t:.1f}C"
        return self.bedroom_cover_target, "cool / no sun"

    # ---------- main alarm ----------
    def _alarm_fire(self, _):
        # Debug: confirm callback invocation regardless of gating
        try:
            self.log("[wake] Callback invoked.", log=self.user_log)
        except Exception:
            pass
        # Hard guard: only run if current time is within +/-90s of configured alarm time.
        # Bypassed for a late catch-up (see _late_catchup_after_restart) or a manual
        # "Wake up now" press (see _on_wake_now) - both fire well outside this window on
        # purpose, through this same entry point.
        try:
            if (not self._late_catchup_in_progress and not self._manual_fire_in_progress
                    and not self._is_within_trigger_window(90)):
                return
        except Exception:
            pass
        # Mark date for heartbeat duplicate suppression; persisted so an AD restart sees
        # today's fire too (see _load_state / _schedule_daily_alarm / _heartbeat_check /
        # _late_catchup_after_restart).
        try:
            fire_at = self.datetime()
            self.last_fire_date = fire_at.date()
            self._state["last_fire_date"] = self.last_fire_date.isoformat()
            self._state["last_fire_at"] = fire_at.isoformat()
            self._save_state()
        except Exception:
            pass
        # A manual press is an explicit command to run now; the toggle only arms the
        # *scheduled* alarm, so a manual fire skips this check entirely instead of reading
        # the toggle at all (user, 2026-08-10 - this is the guard that killed 3 of 4
        # "Wake up now" presses that morning).
        if not self._manual_fire_in_progress and self.get_state(self.alarm_enabled_entity) != "on":
            self.log("[wake] Toggle off; skipping.", log=self.user_log); return
        if self._both_away():
            self.log("[wake] Both away; skipping.", log=self.user_log); return

        self.log("[wake] Alarm firing.", log=self.user_log)
        self._alarm_fired_at = self.datetime()
        # Explain the routine to the dashboard's Home activity feed (fire-and-forget) -
        # blinds moving and music starting "by themselves" is the house acting, and this
        # is the one place that knows why. Once per day by construction (alarm fire).
        try:
            alarm_hhmm = str(self.get_state(self.alarm_time_entity) or "")[:5]
            self.fire_event(
                "house_events_report",
                cause=f"Wake-up alarm ({alarm_hhmm})" if alarm_hhmm else "Wake-up alarm",
                effect="Raising the blinds and fading the lights in",
                icon="mdi:weather-sunset-up",
                audience="admin",  # Mikkel's bedroom routine - not the housemates' business
            )
        except Exception:
            pass
        if self._fired_via_heartbeat:
            # The daily scheduler tick was missed and the minute heartbeat rescued the
            # routine - one extra admin line, because repeats of this point at an
            # AppDaemon scheduler problem. Once per day by construction (last_fire_date).
            try:
                self.fire_event(
                    "house_events_report",
                    cause="Wake alarm's scheduled tick never fired",
                    effect="Backup heartbeat re-fired the morning routine",
                    icon="mdi:alarm-check",
                    audience="admin",
                )
            except Exception:
                pass
        self._attach_cancel_listeners()
        self._group_speakers()
        self._start_media_and_volume_ramp()
        target, reason = self._decide_bedroom_wake_target()
        self._current_bedroom_wake_target = target
        self._current_bedroom_wake_reason = reason
        self._wake_blind_outcome = None  # set by _set_cover_position when the owner answers
        if target != self.bedroom_cover_target:
            self.log(f"[wake] Heat-block: bedroom blind target {target} instead of "
                     f"{self.bedroom_cover_target} ({reason})", log=self.user_log)
        self._nudge_cover_if_closed(self.bedroom_cover, target)
        self._nudge_cover_if_closed(self.bathroom_cover, self.bathroom_cover_target)

        pos = self._cover_position(self.bedroom_cover)
        if pos is not None and int(pos) == self._current_bedroom_wake_target:
            self._maybe_start_light_ramp()
        elif self._wake_blind_outcome == "refused" or self._owner_manual_hold_active():
            # A manual hold outranks the wake target (owner said so, or a hold is
            # already active). The blind stays where the human put it - and the
            # morning must not wait for a target that will never arrive, so the
            # blind wait completes NOW through the same settle path as an arrival
            # and the light ramp still gets its chance.
            self.log(
                "[wake] Manual blind hold outranks the wake target - leaving the "
                "blind and starting the light decision without it",
                log=self.user_log,
            )
            self._blind_wait_complete("Bedroom blind left to the manual hold")
        else:
            if self.cover_listener:
                self.cancel_listen_state(self.cover_listener)
            self.cover_listener = self.listen_state(
                self._on_bedroom_position, self.bedroom_cover, attribute="current_position"
            )
            # Hard deadline: a listener that never fires (blind never hits the exact
            # target) must not stay armed into the day - see wake_light_window_min.
            # This is also the bounded backstop for the manual-yield below: if every
            # earlier detection misses, the wait still ends here.
            self.run_in(self._expire_cover_listener, self.wake_light_window_min * 60)
            self.log("[wake] Waiting for bedroom blind target to start light ramp...", log=self.user_log)

    # ---------- media / volume ----------
    def _group_speakers(self):
        if not self.join_players:
            return
        try:
            self.call_service("media_player/join", entity_id=self.master_player, group_members=self.join_players)
            self.log(f"[wake] Grouped {self.join_players} -> {self.master_player}", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] Join failed: {e}", level="WARNING", log=self.user_log)

    def _start_media_and_volume_ramp(self):
        # Attempt 0: play as-provided
        self.media_attempt = 0
        self._media_recheck_used = False
        self._media_fallback_reported = False
        self._media_exhausted_reported = False
        self._play_media(self.stream_id)
        # Unmute all target players if present sensor says empty
        try:
            if self.bedroom_presence_entity and self.get_state(self.bedroom_presence_entity) in ("off", "false", "0"):
                targets = set([self.master_player] + self.join_players)
                for t in targets:
                    self.call_service("media_player/volume_mute", entity_id=t, is_volume_muted=False)
        except Exception:
            pass
        self._set_volume(self.volume_start)
        self.volume_active = True
        self._schedule_next_volume_tick()
        # Verify playback started; if not, try fallbacks (delays sized for HLS startup,
        # see media_verify_* in initialize)
        try:
            self.run_in(self._verify_media_playback, self.media_verify_delay_sec)
            # Also check if playback drops shortly after start ("playing but dead" scenario) -
            # after the whole verify/recheck/fallback chain has had time to conclude
            self.run_in(self._verify_media_is_still_playing, self.media_still_playing_check_sec)
        except Exception:
            pass

    def _play_media(self, content_id):
        try:
            self.call_service(
                "media_player/play_media",
                entity_id=self.master_player,
                media_content_id=content_id,
                media_content_type=self.stream_type,
            )
            self.log(f"[wake] Requested play: {content_id}", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] play_media failed: {e}", level="WARNING", log=self.user_log)

    def _verify_media_playback(self, _):
        """
        Verify media playback is active. If not, escalate through configured fallback streams.
        Uses a candidates list approach for cleaner iteration through fallback options.
        """
        try:
            state = self.get_state(self.master_player)
            # Also check attributes to ensure it's actually playing (not just reporting "playing" state)
            try:
                attrs = self.get_state(self.master_player, attribute="all").get("attributes", {})
                # Check if media_duration/position indicate actual playback
                media_duration = attrs.get("media_duration", 0)
                media_position = attrs.get("media_position", 0)
                # For streams, duration might be 0, but position should advance if playing
                is_really_playing = state == "playing" and (media_duration > 0 or media_position >= 0)
            except Exception:
                attrs = {}
                is_really_playing = state == "playing"
        except Exception:
            state, attrs, is_really_playing = None, {}, False

        if is_really_playing:
            return

        # One free recheck per attempt before escalating: "not playing yet" at the first
        # check is usually HLS still buffering, not a dead stream. Every observed false
        # failover (Mar-Jul 2026) would have been absorbed here.
        if not self._media_recheck_used:
            self._media_recheck_used = True
            self.log(
                f"[wake] Playback not confirmed yet (state={state}); rechecking in "
                f"{self.media_verify_recheck_sec}s before failover",
                log=self.user_log,
            )
            self.run_in(self._verify_media_playback, self.media_verify_recheck_sec)
            return

        # Build ordered candidates list: primary + configured fallback
        candidates = [self.stream_id]
        if self.fallback:
            candidates.append(self.fallback)

        # Advance attempt counter
        self.media_attempt += 1

        # Try next candidate in the list
        if self.media_attempt < len(candidates):
            next_id = candidates[self.media_attempt]
            self.log(
                f"[wake] Playback not started (state={state}). Trying fallback #{self.media_attempt}: {next_id}",
                level="WARNING",
                log=self.user_log
            )
            if not self._media_fallback_reported:
                self._media_fallback_reported = True
                # Waking to a different stream than configured is house behavior worth
                # explaining (same reasoning as radio_watchdog's restart entry).
                try:
                    self.fire_event(
                        "house_events_report",
                        cause="Wake music stream failed to start",
                        effect="Switched to the fallback stream",
                        icon="mdi:radio",
                        audience="admin",
                    )
                except Exception:
                    pass
            self._media_recheck_used = False  # fresh recheck budget for the new stream
            self._play_media(next_id)
            self.run_in(self._verify_media_playback, self.media_verify_delay_sec)
            return

        # Final attempt: nudge media_play as last resort
        if self.media_attempt == len(candidates):
            try:
                self.call_service("media_player/media_play", entity_id=self.master_player)
                self.log("[wake] Final fallback: sent generic media_play command", level="WARNING", log=self.user_log)
                self._media_recheck_used = False
                self.run_in(self._verify_media_playback, self.media_verify_recheck_sec)
            except Exception:
                pass
            return

        # All attempts exhausted
        self.log(
            f"[wake] Could not confirm playback after all {len(candidates)} fallback attempts.",
            level="WARNING",
            log=self.user_log
        )
        if not self._media_exhausted_reported:
            self._media_exhausted_reported = True
            # A silent wake-up needs acting on (Sonos/stream is broken) and the push
            # doubles as a backup wake signal - page the phone, per the gw2000a policy
            # (failures Mikkel must fix page; the feed explains behavior, not outages).
            try:
                notifier = self.get_app("MobileNotifier")
                self.create_task(notifier.notify(
                    title="Wake-up alarm",
                    message="Wake music never started - primary and fallback streams both "
                            "failed on the bedroom Sonos. Blinds and lights still ran.",
                    target=self.notify_target,
                ))
            except Exception as e:
                self.log(f"[wake] media-failure notify failed: {e}", level="WARNING", log=self.user_log)

    def _verify_media_is_still_playing(self, _):
        """
        Second verification to catch "playing but dead" scenarios where playback
        starts briefly then stops (e.g., MA disconnect/reset issues).
        """
        try:
            state = self.get_state(self.master_player)
            try:
                attrs = self.get_state(self.master_player, attribute="all").get("attributes", {})
                media_duration = attrs.get("media_duration", 0)
                media_position = attrs.get("media_position", 0)
                is_really_playing = state == "playing" and (media_duration > 0 or media_position >= 0)
            except Exception:
                is_really_playing = state == "playing"
        except Exception:
            is_really_playing = False

        if not is_really_playing:
            self.log(
                "[wake] Playback dropped shortly after start; triggering fallback escalation.",
                level="WARNING",
                log=self.user_log
            )
            # Restart the escalation chain from the top, skipping the buffering recheck:
            # the stream already proved it can die, go straight to the fallback.
            self.media_attempt = 0
            self._media_recheck_used = True
            self._verify_media_playback(None)

    def _is_within_trigger_window(self, window_seconds: int) -> bool:
        tstr = self.get_state(self.alarm_time_entity)
        if not tstr:
            return False
        try:
            hh, mm, *ss = [int(x) for x in str(tstr).split(":")]
            target = self.datetime().replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
        except Exception:
            return False
        delta = abs((self.datetime() - target).total_seconds())
        if delta <= max(1, int(window_seconds)):
            return True
        self.log(f"[wake] Skipping fire: now not within {window_seconds}s of configured time (now={self.datetime().time()}, target={target.time()})", log=self.user_log)
        return False

    def _schedule_next_volume_tick(self):
        self.volume_timer = self.run_in(self._volume_tick, self.volume_interval_sec)

    def _volume_tick(self, _):
        # The timer that invoked this callback is now expired; clear handle to avoid invalid cancel warnings
        self.volume_timer = None
        if not self.volume_active:
            return
        current = self._get_first_volume([self.master_player])
        if current is None:
            current = self.volume_start
        next_vol = min(self.volume_target, round(current + self.volume_step, 3))
        self._set_volume(next_vol)
        if next_vol >= self.volume_target:
            self.volume_active = False
            self.log(f"[wake] Volume ramp complete at {next_vol}", log=self.user_log)
        else:
            self._schedule_next_volume_tick()

    def _get_first_volume(self, entities):
        for e in entities:
            try:
                attrs = self.get_state(e, attribute="all").get("attributes", {})
                v = attrs.get("volume_level")
                if isinstance(v, (int, float)):
                    return float(v)
            except Exception:
                pass
        return None

    def _set_volume(self, level):
        self.call_service("media_player/volume_set", entity_id=self.master_player, volume_level=float(level))

    # ---------- covers ----------
    def _nudge_cover_if_closed(self, entity, target_pct):
        # Closed-test centralized in cover_util (shared threshold semantics); the
        # nudge/re-command stays local. Threshold widens the old exact-100/exact-0
        # test so a 99% low-battery park now reads as closed.
        if cover_util.is_closed(
            self, entity, threshold=self.closed_threshold, closed_is_100=self.closed_is_100
        ):
            self._set_cover_position(entity, target_pct)
            return
        # Position unavailable: fall back to the cover's coarse state, as before.
        if cover_util.position(self, entity) is None and self.get_state(entity) == "closed":
            self._set_cover_position(entity, target_pct)

    def _cover_position(self, entity):
        try:
            return self.get_state(entity, attribute="all").get("attributes", {}).get("current_position")
        except Exception:
            return None

    def _blind_owner(self):
        """The single blind owner (apps/blinds/bedroom_blind_owner.py), or None.

        Every failure shape - arg missing, app not loaded, half-loaded without its
        API - returns None, and None always means "the pre-owner behaviour", never
        a skipped write. This is the alarm; degrading costs the house a layer,
        never the wake-up."""
        try:
            name = getattr(self, "owner_app", None)
            if not name:
                return None
            app = self.get_app(name)
        except Exception:
            return None
        if app is None or not hasattr(app, "request"):
            return None
        return app

    def _owner_manual_hold_active(self):
        """True when the blind owner says a human's move is holding the blind.
        False on ANY failure - without a definite answer the wake keeps waiting for
        its target exactly as it did before the owner existed."""
        owner = self._blind_owner()
        if owner is None:
            return False
        try:
            return bool(owner.manual_hold_active())
        except Exception:
            return False

    def _set_cover_position(self, entity, pct):
        """Hand the move to the blind owner; direct write when it is absent.

        The BEDROOM cover routes through the owner so a manual hold can outrank the
        wake - and a refusal is handled, not silent: _alarm_fire sees the outcome
        and completes the blind wait through _blind_wait_complete, so the ramp
        still runs (blind where the human put it, morning not stranded). The
        bathroom cover is not owned and keeps the pre-owner write. Fallback for a
        missing/broken owner is byte-identical to the pre-owner behaviour, where
        the wake write is deliberately NOT gated on arbitration: everything that
        legitimately outranks the wake is resolved in _decide_bedroom_wake_target,
        where losing costs a position and not the wake-up.
        """
        self.log(f"[wake] Setting {entity} -> {pct}%", log=self.user_log)
        if entity == getattr(self, "bedroom_cover", None):
            owner = self._blind_owner()
            if owner is not None:
                try:
                    source = getattr(self, "_current_bedroom_wake_source", None) or (
                        blind_arbiter.SOURCE_WAKE if blind_arbiter is not None else "wake")
                    reason = getattr(self, "_current_bedroom_wake_reason", "") or "morning open"
                    res = owner.request(source, int(pct), reason=reason)
                    if res and res.get("granted"):
                        self._wake_blind_outcome = "granted"
                    else:
                        self._wake_blind_outcome = "refused"
                        self.log(
                            f"[wake] Blind held by {(res or {}).get('winner')} - leaving it "
                            "where the human put it",
                            log=self.user_log,
                        )
                    return
                except Exception as e:
                    self.log(
                        f"[wake] Blind owner request failed ({e}) - direct write",
                        level="WARNING", log=self.user_log,
                    )
        if blind_arbiter is None:
            self.call_service("cover/set_cover_position", entity_id=entity, position=int(pct))
            return
        blind_arbiter.command(self, entity, pct, source=blind_arbiter.SOURCE_WAKE)

    def _read_outdoor_lux_for_settle(self):
        """Read outdoor lux clue for dynamic post-blind settle time."""
        try:
            val = self.get_state(self.outdoor_lux_entity)
            if val not in (None, "", "unknown", "unavailable"):
                return float(val)
        except Exception:
            pass
        try:
            attrs = self.get_state(self.bedroom_state_entity, attribute="all")
            a = (attrs or {}).get("attributes", {}) if isinstance(attrs, dict) else {}
            ol = a.get("outdoor_lux")
            if isinstance(ol, (int, float)):
                return float(ol)
        except Exception:
            pass
        return None

    def _compute_post_blind_settle_delay(self):
        """Short wait when dark outside, longer wait when it's bright outside."""
        outdoor_lux = self._read_outdoor_lux_for_settle()
        if outdoor_lux is None:
            return int(self.post_blind_settle_mid_sec), None
        if outdoor_lux >= self.post_blind_outdoor_bright_lux:
            return int(self.post_blind_settle_bright_sec), outdoor_lux
        if outdoor_lux >= self.post_blind_outdoor_mid_lux:
            return int(self.post_blind_settle_mid_sec), outdoor_lux
        return int(self.post_blind_settle_dark_sec), outdoor_lux

    def _on_bedroom_position(self, entity, attr, old, new, kwargs):
        try:
            pos = int(float(new))
        except Exception:
            return
        if pos == self._current_bedroom_wake_target:
            self._blind_wait_complete("Bedroom blind reached target")
            return
        # The blind is moving but NOT toward our number: if the owner says a human
        # took it (wall press / hand on the remote after the wake command), the
        # target will never arrive. Yield now - blind stays where the human put it,
        # and the wait completes through the same settle path as an arrival so the
        # light ramp still runs. A manual takeover mid-wait always produces position
        # reports (the blind moves), so this check is guaranteed a trigger; the
        # wake_light_window_min expiry in _alarm_fire stays the bounded backstop.
        if self.cover_listener and self._owner_manual_hold_active():
            self.log(
                "[wake] Manual move took the blind mid-wait - yielding and starting "
                "the light decision without it",
                log=self.user_log,
            )
            self._blind_wait_complete("Bedroom blind yielded to a manual move")

    def _blind_wait_complete(self, headline="Bedroom blind reached target"):
        """The blind phase of the morning is over - by arrival at the wake target or
        by yielding to a manual hold. Either way the ramp decision runs on the same
        settle timing (post_blind_settle_*), so the blind->ramp handshake is one
        path with two entrances, not two paths."""
        if self.cover_listener:
            self.cancel_listen_state(self.cover_listener)
            self.cover_listener = None
        # Allow sensor lag to catch up, then recompute darkness and decide.
        def _delayed_decide(_):
            try:
                self.fire_event("darkness_recompute", zone="bedroom")
            except Exception:
                pass
            # Check current room state to decide whether to start the ramp
            try:
                if self._room_dark_for_wake_light():
                    self._maybe_start_light_ramp()
                else:
                    self.log("[wake] Room already bright after blinds; skipping ramp.", log=self.user_log)
            except Exception:
                # If uncertain, proceed with ramp conservatively
                self._maybe_start_light_ramp()

        delay, outdoor_lux = self._compute_post_blind_settle_delay()
        if outdoor_lux is None:
            self.log(
                f"[wake] {headline}; waiting {delay}s before darkness check (outdoor lux unavailable).",
                log=self.user_log,
            )
        else:
            self.log(
                f"[wake] {headline}; outdoor {outdoor_lux:.0f}lx -> wait {delay}s before darkness check.",
                log=self.user_log,
            )
        self.run_in(_delayed_decide, delay)

    # ---------- light ramp ----------
    def _room_dark_for_wake_light(self):
        """Same darkness interpretation as bedroom_lights (sensor.room_state_* + lux sufficiency)."""
        if not self.bedroom_state_entity:
            return True
        return room_state_darkness.is_dark_for_lights(
            self, self.bedroom_state_entity, default_dark=True
        )

    def _expire_cover_listener(self, _):
        if self.cover_listener:
            try:
                self.cancel_listen_state(self.cover_listener)
            except Exception:
                pass
            self.cover_listener = None
            self.log("[wake] Wake light window over - blind listener disarmed without a ramp.", log=self.user_log)

    def _within_wake_window(self) -> bool:
        """True only within wake_light_window_min of the alarm actually firing. The ramp is a
        WAKE feature: outside this window (or before any alarm today) a start request is a
        stale trigger, not a wake-up."""
        if self._alarm_fired_at is None:
            return False
        try:
            return (self.datetime() - self._alarm_fired_at).total_seconds() <= self.wake_light_window_min * 60
        except Exception:
            return False

    def _maybe_start_light_ramp(self):
        # Prevent multiple concurrent ramps
        if self.ramp_active:
            self.log("[wake] Ramp already active; ignoring start request", log=self.user_log)
            return
        # Respect the bedroom lights' manual override - the ramp is an automatic light
        # action like any other (see initialize; user-reported 2026-07-16).
        try:
            if self.manual_override_entity and self.get_state(self.manual_override_entity) == "on":
                self.log("[wake] Bedroom lights manual override on; skipping light ramp.", log=self.user_log)
                return
        except Exception:
            pass
        if not self._within_wake_window():
            self.log("[wake] Outside the wake light window; skipping light ramp.", log=self.user_log)
            return
        if not self._room_dark_for_wake_light():
            self.log("[wake] Room is bright enough; skipping ramp.", log=self.user_log)
            return
        if not self._bed_session_active():
            self.log("[wake] Bed session not active; skipping light ramp.", log=self.user_log)
            return

        # Everything from here down is "how the room gets lit", which belongs to the
        # shared lighting layer (apps/lights/light_ramp.py) - pausing Adaptive Lighting's
        # brightness adaptation for the duration, and the actual brightness command. This
        # app's job was the decision above: that a ramp should start, now.
        if light_ramp is not None:
            light_ramp.pause_adaptive_brightness(self, self.adaptive_brightness_switch)
        else:
            self.turn_off(self.adaptive_brightness_switch)
        self.current_pct = self._set_light_pct(self._ramp_start_pct())
        self.ramp_active = True
        self._attach_cancel_listeners_light()
        self._schedule_next_ramp_tick()
        # (Decision now happens after blinds reach target; no immediate recompute here)
        self.log(f"[wake] Light ramp start: {self.current_pct}% (+{self.ramp_step_pct}%/{self.ramp_interval_sec}s)", log=self.user_log)
        # After deciding we need light, disable sleep modes if configured.
        # MikkelSleepMode listens for this boolean transition: DND off + no
        # re-arm until Withings shows out of bed (still in bed + charging would otherwise win).
        self._turn_off_sleep_modes_if_on()

    def _schedule_next_ramp_tick(self):
        self.ramp_timer = self.run_in(self._ramp_tick, self.ramp_interval_sec)

    def _ramp_tick(self, _):
        # Clear expired handle to avoid invalid cancel warnings
        self.ramp_timer = None
        if not self.ramp_active:
            return

        if not self._bed_session_active():
            self._finish_ramp("bed session ended")
            return

        # Daylight can overtake the ramp: the pre-check only runs once, right after the blind
        # moves, and a summer sunrise gains thousands of lux over the ramp's lifetime. Without
        # this the ramp commits at the alarm and drives to full brightness in a room that no
        # longer needs it (2026-08-12: started 05:50 at 1%, reached 90% at 06:06 with the sky
        # climbing the whole way). Same test as the pre-check, so the two cannot disagree.
        if not self._room_dark_for_wake_light():
            self._finish_ramp("daylight took over")
            return

        target = self._ramp_target_pct()
        if self.current_pct >= target:
            self._finish_ramp(f"Reached target {target}%"); return

        self.current_pct = self._set_light_pct(self._ramp_next_pct(target))
        self._schedule_next_ramp_tick()

    # ---------- shared lighting layer (with a direct fallback) ----------
    # Four thin adapters, each doing exactly what this app did inline before the split
    # when light_ramp is unavailable. The fallback is not defensive decoration: an
    # alarm that silently stops lighting the room because a helper module failed to
    # import would be a worse bug than the duplication it removed.
    def _ramp_start_pct(self):
        if light_ramp is not None:
            return light_ramp.start_pct(self.ramp_start_pct)
        return max(1, int(self.ramp_start_pct))

    def _ramp_target_pct(self):
        """Adaptive Lighting's current idea of the right brightness, capped by our own
        ceiling; the ceiling alone when AL has nothing to say."""
        adaptive = self._read_adaptive_target_pct()
        if light_ramp is not None:
            return light_ramp.resolve_target(adaptive, self.ramp_max_pct)
        if adaptive is None:
            return int(self.ramp_max_pct)
        return min(int(adaptive), int(self.ramp_max_pct))

    def _ramp_next_pct(self, target):
        if light_ramp is not None:
            return light_ramp.next_pct(self.current_pct, self.ramp_step_pct, target)
        return min(self.current_pct + int(self.ramp_step_pct), target)

    def _set_light_pct(self, pct):
        if light_ramp is not None:
            return light_ramp.set_brightness(
                self, self.light_entity, pct, transition=self.ramp_interval_sec
            )
        pct = max(1, min(100, int(pct)))
        self.call_service(
            "light/turn_on",
            entity_id=self.light_entity,
            brightness_pct=pct,
            transition=int(self.ramp_interval_sec),
        )
        return pct

    def _resume_adaptive_brightness(self):
        """Give brightness back to Adaptive Lighting. Runs from teardown paths, so it
        must never raise - an exception here strands the switch off with nothing left
        to restore it (exactly the state an AD restart mid-ramp leaves behind, which
        _reconcile_adaptive_lighting_after_restart then has to clean up next boot)."""
        def _log(msg):
            self.log(f"[wake] {msg}", log=self.user_log)

        if light_ramp is not None:
            light_ramp.resume_adaptive_brightness(
                self, self.adaptive_brightness_switch, log_fn=_log
            )
            return
        try:
            _log("Restoring Adaptive Lighting brightness adaptation")
            self.turn_on(self.adaptive_brightness_switch)
        except Exception:
            pass

    # ---------- bed-light session hand-off (ramp yields once the session ends) ----------
    def _bed_session_active(self):
        return self.get_state(self.bed_session_entity) != "off"  # None/unknown -> treat active

    def _on_bed_session_change(self, entity, attr, old, new, kwargs):
        if self.ramp_active and new == "off":
            self._finish_ramp("bed session ended - handing bedroom to ceiling logic")

    def _turn_off_sleep_modes_if_on(self):
        try:
            for ent in [self.mikkel_sleep_entity]:
                if ent and self.get_state(ent) == "on":
                    self.turn_off(ent)
                    self.log(f"[wake] Turned off sleep mode: {ent}", log=self.user_log)
        except Exception:
            pass

    def _read_adaptive_target_pct(self):
        """
        Read the current target brightness from Adaptive Lighting's MAIN switch attributes.
        The main switch exposes "current light settings" via attributes (implementation detail of AL).
        Return int percentage or None.
        """
        attrs = self.get_state(self.adaptive_main_switch, attribute="all")
        if not attrs: return None
        a = attrs.get("attributes", {})
        for key in ("brightness_pct", "brightness", "target_brightness_pct"):
            val = a.get(key)
            if isinstance(val, (int, float)):
                return int(round(val))
        return None

    # ---------- cancellation & safety ----------
    def _attach_cancel_listeners(self):
        # Presence watchers (stop media & lights if both leave)
        for p in self.persons:
            self.cancel_listeners.append(self.listen_state(self._on_presence_change, p))
        # Alarm toggle off stops the whole routine - but only for a SCHEDULED fire. For a
        # manual "Wake up now" run the toggle is not the arming state (see the toggle guard
        # in _alarm_fire), so an off-edge here must not _stop_all a run the user explicitly
        # asked for. This is exactly what killed the run this morning at 07:46:54
        # (2026-08-10). Presence listeners above still apply either way.
        if not self._manual_fire_in_progress:
            self.cancel_listeners.append(self.listen_state(self._on_alarm_toggle, self.alarm_enabled_entity))

    def _attach_cancel_listeners_light(self):
        # Stop the ramp when darkness logic says bright (state, pending_target, lux - same as bedroom_lights)
        self.cancel_listeners.append(
            self.listen_state(self._on_room_darkness_for_wake, self.bedroom_state_entity)
        )
        self.cancel_listeners.append(
            self.listen_state(
                self._on_room_darkness_for_wake,
                self.bedroom_state_entity,
                attribute="pending_target",
            )
        )
        # Bed-light session listener has its own slot (not cancel_listeners) since it must be
        # torn down by _stop_light_ramp specifically, same lifetime as the ramp.
        self._session_listener = self.listen_state(
            self._on_bed_session_change, self.bed_session_entity
        )

    def _on_alarm_toggle(self, entity, attr, old, new, kwargs):
        if new == "off": self._stop_all("Alarm toggle turned off")

    def _on_presence_change(self, entity, attr, old, new, kwargs):
        if self._both_away(): self._stop_all("Both users left home")

    def _on_room_darkness_for_wake(self, entity, attr, old, new, kwargs):
        if not self.ramp_active:
            return
        if self._room_dark_for_wake_light():
            return
        self._finish_ramp("Room bright enough for wakeup")
        try:
            if self.get_state(self.light_entity) == "on":
                self.turn_off(self.light_entity)
                self.log("[wake] Room bright during wakeup - turning off bed lights", log=self.user_log)
        except Exception:
            pass

    def _stop_all(self, reason):
        self.log(f"[wake] Stopping routine: {reason}", log=self.user_log)
        self._stop_media()
        # Always restore Adaptive Lighting brightness adaptation even if ramp never started
        self._resume_adaptive_brightness()
        self._stop_light_ramp()
        self._cleanup_listeners()

    def _finish_ramp(self, reason=None):
        if reason: self.log(f"[wake] Finishing light ramp: {reason}", log=self.user_log)
        self._stop_light_ramp()

    def _stop_media(self):
        self.volume_active = False
        # Do not cancel expired timer handles; just clear
        self.volume_timer = None
        for e in set([self.master_player] + self.join_players):
            try:
                self.call_service("media_player/media_stop", entity_id=e)
            except Exception:
                self.call_service("media_player/turn_off", entity_id=e)

    def _stop_light_ramp(self):
        self.ramp_active = False
        # Do not cancel expired timer handles; just clear
        self.ramp_timer = None
        if self._session_listener is not None:
            try: self.cancel_listen_state(self._session_listener)
            except Exception: pass
            self._session_listener = None
        # Re-enable Adaptive Lighting brightness adaptation
        self._resume_adaptive_brightness()

    def _cleanup_listeners(self):
        if self.cover_listener:
            self.cancel_listen_state(self.cover_listener); self.cover_listener = None
        for h in self.cancel_listeners:
            try: self.cancel_listen_state(h)
            except Exception: pass
        self.cancel_listeners = []

    def _both_away(self):
        states = [self.get_state(p) for p in self.persons]
        return all(s not in ("home", "Home", "present") for s in states)


