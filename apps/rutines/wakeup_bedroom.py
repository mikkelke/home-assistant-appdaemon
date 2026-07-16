import appdaemon.plugins.hass.hassapi as hass # type: ignore
from datetime import time, timedelta

import room_state_darkness

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

        self.volume_entities = list(A["volume_entities"])
        self.volume_start = float(A["volume_start"])
        self.volume_target = float(A["volume_target"])
        self.volume_step = float(A["volume_step"])
        self.volume_interval_sec = int(A["volume_interval_sec"]) 

        self.closed_is_100 = bool(A["closed_is_100"])
        self.bedroom_cover = A["covers"]["bedroom"]
        self.bathroom_cover = A["covers"]["bathroom"]
        self.bedroom_cover_target = int(A["bedroom_cover_target"])
        self.bathroom_cover_target = int(A["bathroom_cover_target"])
        # Heat-wave override: the normal wake nudge (38, i.e. mostly open) floods a hot
        # bedroom with morning sun/heat if the blind was closed overnight. Toggle-gated
        # (user, 2026-07-15) rather than auto-detected - simple, predictable, matches the
        # same manual-toggle pattern as the AC. Off by default = today's normal behavior.
        self.heat_wave_entity = A.get("heat_wave_entity", "input_boolean.heat_wave_mode")
        self.bedroom_cover_target_heat_wave = int(A.get("bedroom_cover_target_heat_wave", 65))
        self._current_bedroom_wake_target = self.bedroom_cover_target

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
        self.last_fire_date = None  # watchdog duplicate guard
        # Must exist before _schedule_daily_alarm() (it calls _schedule_watchdog_if_needed)
        self.watchdog = None

        # Respect per-app log_level
        self.set_log_level(self.log_level)

        # Schedule alarm and reschedule on time change
        self._schedule_daily_alarm()
        self.listen_state(self._on_alarm_time_changed, self.alarm_time_entity)
        # Also reschedule watchdog when alarm enabled/disabled changes
        self.listen_state(self._on_alarm_enabled_changed, self.alarm_enabled_entity)
        self._schedule_watchdog_if_needed()

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
        
        # Schedule watchdog if needed (after scheduling alarm)
        self._schedule_watchdog_if_needed()

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
                    if self.get_state(self.alarm_enabled_entity) == "on" and not self._both_away():
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

    def _on_alarm_time_changed(self, entity, attr, old, new, kwargs):
        self._schedule_daily_alarm()
        self._schedule_watchdog_if_needed()
    
    def _on_alarm_enabled_changed(self, entity, attr, old, new, kwargs):
        """Reschedule watchdog when alarm is enabled/disabled."""
        self._schedule_watchdog_if_needed()

    def _schedule_watchdog_if_needed(self):
        """Schedule watchdog only if alarm is enabled and we're within 2 hours of alarm time."""
        try:
            # Cancel existing watchdog if any
            if self.watchdog:
                try:
                    if self.timer_running(self.watchdog):
                        self.cancel_timer(self.watchdog)
                except Exception:
                    pass
                self.watchdog = None
            
            # Only schedule if alarm is enabled
            if self.get_state(self.alarm_enabled_entity) != "on":
                return
            
            # Check if we're within 2 hours of alarm time
            tstr = self.get_state(self.alarm_time_entity)
            if not tstr:
                return
            
            hh, mm, *ss = [int(x) for x in tstr.split(":")]
            now_dt = self.datetime()
            target_today = now_dt.replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
            
            # Calculate time until alarm (handle next day if needed)
            if target_today < now_dt:
                target_today = target_today + timedelta(days=1)
            
            delta_seconds = (target_today - now_dt).total_seconds()
            
            # Only schedule watchdog if within 2 hours (7200 seconds) of alarm time
            if 0 <= delta_seconds <= 7200:
                # Schedule to start 1 minute before alarm time
                start_watchdog_at = max(60, delta_seconds - 60)
                self.watchdog = self.run_in(self._start_watchdog, start_watchdog_at)
                self.log(f"[wake] Watchdog scheduled to start in {start_watchdog_at:.0f}s (alarm in {delta_seconds:.0f}s)", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] Error scheduling watchdog: {e}", level="ERROR", log=self.user_log)
    
    def _start_watchdog(self, kwargs):
        """Start the minute-level watchdog when close to alarm time."""
        try:
            # Cancel any existing watchdog
            if self.watchdog:
                try:
                    if self.timer_running(self.watchdog):
                        self.cancel_timer(self.watchdog)
                except Exception:
                    pass
            
            # Start watchdog that runs every minute
            self.watchdog = self.run_every(self._minute_watchdog, self.datetime(), 60)
            self.log("[wake] Watchdog started (running every minute until alarm fires)", log=self.user_log)
        except Exception as e:
            self.log(f"[wake] Error starting watchdog: {e}", level="ERROR", log=self.user_log)
            self.watchdog = None
    
    def _minute_watchdog(self, kwargs):
        """Minute-level watchdog to guarantee firing even if a daily tick is missed."""
        try:
            # Stop watchdog if alarm is disabled
            if self.get_state(self.alarm_enabled_entity) != "on":
                if self.watchdog:
                    try:
                        if self.timer_running(self.watchdog):
                            self.cancel_timer(self.watchdog)
                    except Exception:
                        pass
                    self.watchdog = None
                return
            
            tstr = self.get_state(self.alarm_time_entity)
            if not tstr:
                return
            hh, mm, *ss = [int(x) for x in tstr.split(":")]
            target = self.datetime().replace(hour=hh, minute=mm, second=(ss[0] if ss else 0), microsecond=0)
            now_dt = self.datetime()
            if self.last_fire_date == now_dt.date():
                return
            delta = (now_dt - target).total_seconds()
            if 0 <= delta < 60:
                # Conditions enforced inside _alarm_fire
                self.log("[wake] Watchdog firing alarm (minute check)", log=self.user_log)
                self._alarm_fire(None)
                self.last_fire_date = now_dt.date()
                # Stop watchdog after firing
                if self.watchdog:
                    try:
                        if self.timer_running(self.watchdog):
                            self.cancel_timer(self.watchdog)
                    except Exception:
                        pass
                    self.watchdog = None
        except Exception:
            pass

    # ---------- main alarm ----------
    def _alarm_fire(self, _):
        # Debug: confirm callback invocation regardless of gating
        try:
            self.log("[wake] Callback invoked.", log=self.user_log)
        except Exception:
            pass
        # Hard guard: only run if current time is within +/-90s of configured alarm time
        try:
            if not self._is_within_trigger_window(90):
                return
        except Exception:
            pass
        # Mark date for watchdog duplicate suppression
        try:
            self.last_fire_date = self.datetime().date()
        except Exception:
            pass
        if self.get_state(self.alarm_enabled_entity) != "on":
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
            )
        except Exception:
            pass
        self._attach_cancel_listeners()
        self._group_speakers()
        self._start_media_and_volume_ramp()
        heat_wave = self.get_state(self.heat_wave_entity) == "on"
        self._current_bedroom_wake_target = (
            self.bedroom_cover_target_heat_wave if heat_wave else self.bedroom_cover_target
        )
        if heat_wave:
            self.log(f"[wake] Heat wave mode: bedroom blind target {self._current_bedroom_wake_target} "
                     f"instead of {self.bedroom_cover_target}", log=self.user_log)
        self._nudge_cover_if_closed(self.bedroom_cover, self._current_bedroom_wake_target)
        self._nudge_cover_if_closed(self.bathroom_cover, self.bathroom_cover_target)

        pos = self._cover_position(self.bedroom_cover)
        if pos is not None and int(pos) == self._current_bedroom_wake_target:
            self._maybe_start_light_ramp()
        else:
            if self.cover_listener:
                self.cancel_listen_state(self.cover_listener)
            self.cover_listener = self.listen_state(
                self._on_bedroom_position, self.bedroom_cover, attribute="current_position"
            )
            # Hard deadline: a listener that never fires (blind never hits the exact
            # target) must not stay armed into the day - see wake_light_window_min.
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
        # Verify playback started; if not, try fallbacks
        try:
            self.run_in(self._verify_media_playback, 2)
            # Also check if playback drops shortly after start ("playing but dead" scenario)
            self.run_in(self._verify_media_is_still_playing, 10)
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
            self._play_media(next_id)
            self.run_in(self._verify_media_playback, 2)
            return

        # Final attempt: nudge media_play as last resort
        if self.media_attempt == len(candidates):
            try:
                self.call_service("media_player/media_play", entity_id=self.master_player)
                self.log("[wake] Final fallback: sent generic media_play command", level="WARNING", log=self.user_log)
                self.run_in(self._verify_media_playback, 2)
            except Exception:
                pass
            return

        # All attempts exhausted
        self.log(
            f"[wake] Could not confirm playback after all {len(candidates)} fallback attempts.",
            level="WARNING",
            log=self.user_log
        )

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
            # Force escalation immediately by calling verification
            # Reset attempt counter to allow retry of fallbacks
            if self.media_attempt < 1:
                self.media_attempt = 0  # Will increment to 1 on next call
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
        pos = self._cover_position(entity)
        if pos is None:
            if self.get_state(entity) == "closed":
                self._set_cover_position(entity, target_pct)
            return
        if (self.closed_is_100 and int(pos) == 100) or ((not self.closed_is_100) and int(pos) == 0):
            self._set_cover_position(entity, target_pct)

    def _cover_position(self, entity):
        try:
            return self.get_state(entity, attribute="all").get("attributes", {}).get("current_position")
        except Exception:
            return None

    def _set_cover_position(self, entity, pct):
        self.log(f"[wake] Setting {entity} -> {pct}%", log=self.user_log)
        self.call_service("cover/set_cover_position", entity_id=entity, position=int(pct))

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
                    f"[wake] Bedroom blind reached target; waiting {delay}s before darkness check (outdoor lux unavailable).",
                    log=self.user_log,
                )
            else:
                self.log(
                    f"[wake] Bedroom blind reached target; outdoor {outdoor_lux:.0f}lx -> wait {delay}s before darkness check.",
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

        # Pause Adaptive Lighting's brightness adaptation during the ramp
        self.turn_off(self.adaptive_brightness_switch)

        self.current_pct = max(1, int(self.ramp_start_pct))
        self.call_service(
            "light/turn_on",
            entity_id=self.light_entity,
            brightness_pct=self.current_pct,
            transition=int(self.ramp_interval_sec),
        )
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

        target = self._read_adaptive_target_pct()
        if target is None:
            target = self.ramp_max_pct
        target = min(int(target), int(self.ramp_max_pct))

        if self.current_pct >= target:
            self._finish_ramp(f"Reached target {target}%"); return

        self.current_pct = min(self.current_pct + int(self.ramp_step_pct), target)
        self.call_service(
            "light/turn_on",
            entity_id=self.light_entity,
            brightness_pct=self.current_pct,
            transition=int(self.ramp_interval_sec),
        )
        self._schedule_next_ramp_tick()

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
        # Alarm toggle off stops the whole routine
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
        try:
            self.log("[wake] Restoring Adaptive Lighting brightness adaptation", log=self.user_log)
            self.turn_on(self.adaptive_brightness_switch)
        except Exception:
            pass
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
        # Re-enable Adaptive Lighting brightness adaptation
        try:
            self.log("[wake] Restoring Adaptive Lighting brightness adaptation", log=self.user_log)
            self.turn_on(self.adaptive_brightness_switch)
        except Exception:
            pass

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


