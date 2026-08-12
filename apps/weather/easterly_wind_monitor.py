"""
Easterly Wind Monitor: log and notify when wind pattern matches "building creak" situation.

Condition: easterly direction (defaults 60-120 deg), "windy" when
  mean wind >= wind_speed_windy OR gust >= gust_windy - use the same numeric unit as your HA sensors
  (e.g. if Ecowitt reports km/h, put thresholds in km/h; no conversion in code).

Sustained for sustained_minutes (consecutive checks; ~minutes when interval is 60s).

Sets input_boolean.easterly_wind_episode_active and sends notification via MobileNotifier.

Restart survival (2026-07-27): the episode helper is an HA input_boolean, so it survives
an AD restart even though ``_in_episode``/the sustained-count counters (in-memory) do not.
initialize() seeds ``_in_episode`` from the helper's current state so a restart mid-episode
doesn't duplicate the start notification. If the helper is ON but the wind has already died
down by the time we come back up, waiting through another full end_after_minutes_not_met
debounce would leave the helper stuck ON for no reason (we have no idea how long it's
actually been calm) - a one-time definite reading at init instead runs the normal
episode-end path (turn the helper off + end notification) immediately. No state file: the
helper IS the persisted truth for episode-active/not. What the helper canNOT carry is the
episode's peak gust: a rehydrated episode only knows what this instance has measured since
the restart, so its end message qualifies that figure (or omits it entirely when nothing
windy was seen) rather than reporting a peak that was never measured.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class EasterlyWindMonitor(hass.Hass):
    def initialize(self):
        self.wind_dir = self.args.get("wind_direction_entity", "sensor.gw2000a_wind_direction")
        # Preferred direction source - see _pick_direction. Unset = instantaneous vane only.
        self.wind_dir_mean = self.args.get("wind_direction_mean_entity")
        self.wind_speed = self.args.get("wind_speed_entity", "sensor.gw2000a_wind_speed")
        self.wind_gust = self.args.get("wind_gust_entity", "sensor.gw2000a_wind_gust")
        self.episode_entity = self.args.get("episode_active_entity", "input_boolean.easterly_wind_episode_active")

        self.dir_min = float(self.args.get("direction_min", 60))
        self.dir_max = float(self.args.get("direction_max", 120))
        # Same unit as wind_speed / wind_gust entities (you precompute from m/s if needed).
        self.wind_speed_windy = float(self.args.get("wind_speed_windy", 28.8))
        self.gust_windy = float(self.args.get("gust_windy", 54.0))
        self.wind_unit_label = str(self.args.get("wind_unit_label", "")).strip()

        self.sustained_min = int(self.args.get("sustained_minutes", 5))
        self.end_after_min = int(self.args.get("end_after_minutes_not_met", 10))
        self.interval_s = int(self.args.get("check_interval_seconds", 60))

        self.notify_target = self.args.get("notify_target", "home")
        self.notify_on_end = self.args.get("notify_on_episode_end", False)

        # Rehydrate across restarts (2026-07-27): the helper is an HA input_boolean and
        # survives AD restarts/deploys even though these counters do not - see module
        # docstring. If it doesn't exist yet, get_state returns None -> not "on" -> False,
        # same as the old hardcoded default.
        try:
            self._in_episode = self.get_state(self.episode_entity) == "on"
        except Exception as e:
            self.log(f"Could not read {self.episode_entity} at startup: {e}", level="WARNING")
            self._in_episode = False
        self._condition_met_count = 0
        self._condition_not_met_count = 0
        self._last_gust_in_episode = 0.0
        # False whenever the running episode began before this instance did: the gust peak
        # then covers only the post-restart part of it and must be reported as such.
        self._peak_from_episode_start = not self._in_episode

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
            if self.mobile_notifier:
                self.log("MobileNotifier found", level="INFO")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        # "now" fires at now+interval per AppDaemon's docs, not immediately - "immediate" is the real keyword; see 8666460.
        self.run_every(self._check_conditions, "immediate", self.interval_s)
        ul = f" {self.wind_unit_label}" if self.wind_unit_label else ""
        self.log(
            f"Started: dir {self.dir_min}-{self.dir_max} deg; windy if mean >= {self.wind_speed_windy:g}{ul} "
            f"or gust >= {self.gust_windy:g}{ul}, "
            f"sustained {self.sustained_min} checks (~{self.sustained_min * self.interval_s // 60} min at this interval), "
            f"every {self.interval_s}s",
            level="INFO",
        )

        if self.args.get("test_notification_on_start"):
            self.run_in(self._send_test_notification, 3)
            self.log("Test notification scheduled in 3s", level="INFO")

        self.run_in(self._check_episode_entity_exists, 2)

        if self._in_episode and self._episode_conditions_now() is False:
            # Rehydrated an active episode (helper ON) but a definite current reading
            # says the wind has already died down - don't wait through another
            # end_after_minutes_not_met debounce with no idea how long it's actually
            # been calm. Prime the gate and run the normal (async) end path so the
            # helper turn-off + end notification stay in one place. Deferred one tick
            # so initialize() is guaranteed to have fully returned first.
            self.log(
                f"{self.episode_entity} is ON at startup but conditions have already "
                "ended - closing the episode out now instead of leaving it stuck",
                level="INFO",
            )
            self._condition_not_met_count = self.end_after_min
            self.run_in(lambda kw: self.create_task(self._maybe_end_episode()), 0)

    @staticmethod
    def _pick_direction(mean_raw, vane_raw):
        """(direction_deg, source) from the 10-minute mean, falling back to the vane.

        "Easterly wind" is a statement about the MEAN flow - meteorologically a 10-minute
        mean - and this app then asks for it to hold across 5 consecutive checks. The
        instantaneous vane cannot carry that question: over the 60 days to 2026-08-12 it sat
        a median 26 deg from the station's own 10-minute mean (p90 114 deg), and given it is
        inside the 60-120 deg band now it is still inside it 4 minutes later only 65% of the
        time. Because a single out-of-band sample resets _condition_met_count to zero, that
        jitter silently cancels episodes.

        Measured on the windy months (2026-01-01..2026-03-01), where 89 minutes had the mean
        easterly-and-windy while the vane knocked the run back to zero: on the vane the app
        would have raised 4 episodes / 1.0 h, on the mean 6 episodes / 2.2 h. The two extra
        are real building-load events it never reported - 2026-02-16 11:54-12:36 (42 min) in
        full, and 2026-02-01's start 14 min late.

        Returns (None, None) when neither reading is usable, so the caller keeps its
        existing "inconclusive" handling."""
        for raw, src in ((mean_raw, "mean"), (vane_raw, "vane")):
            if raw in (None, "unknown", "unavailable"):
                continue
            try:
                return float(raw), src
            except (TypeError, ValueError):
                continue
        return None, None

    def _episode_conditions_now(self):
        """Synchronous point-in-time read of the same easterly+windy test _check_conditions
        applies, used ONLY for the init-time rehydration check above (initialize() is not
        async). Returns True/False, or None when a reading is unavailable/unparseable -
        inconclusive, so the normal debounced loop is left to handle it once real data
        returns; only a DEFINITE calm reading justifies skipping the debounce at restart."""
        try:
            dir_raw = self.get_state(self.wind_dir)
            mean_raw = self.get_state(self.wind_dir_mean) if self.wind_dir_mean else None
            gust_raw = self.get_state(self.wind_gust)
            speed_raw = self.get_state(self.wind_speed)
        except Exception as e:
            self.log(f"get_state failed during rehydration check: {e}", level="WARNING")
            return None

        direction, _src = self._pick_direction(mean_raw, dir_raw)
        if direction is None or gust_raw in (None, "unknown", "unavailable"):
            return None
        try:
            gust_ha = float(gust_raw)
        except (TypeError, ValueError):
            return None

        speed = None
        if speed_raw not in (None, "unknown", "unavailable"):
            try:
                speed = float(speed_raw)
            except (TypeError, ValueError):
                speed = None

        windy = gust_ha >= self.gust_windy or (speed is not None and speed >= self.wind_speed_windy)
        return self.dir_min <= direction <= self.dir_max and windy

    async def _check_episode_entity_exists(self, kwargs):
        """Warn if the episode tracking entity is missing in HA."""
        try:
            state = await self.get_state(self.episode_entity)
            if state is None:
                self.log(
                    f"ERROR: Entity {self.episode_entity} not found in Home Assistant. "
                    "Create it in Settings > Devices & Services > Helpers > Create Helper > Toggle "
                    "(e.g. name: Easterly wind episode active). Episode state will not be tracked.",
                    level="ERROR",
                )
            elif state in ("unknown", "unavailable"):
                self.log(
                    f"WARNING: Entity {self.episode_entity} exists but state is {state}. "
                    "Episode state may not be tracked correctly.",
                    level="WARNING",
                )
        except Exception as e:
            self.log(
                f"ERROR: Could not check entity {self.episode_entity}: {e}. "
                "Create this helper in HA (Settings > Helpers > Toggle) or the app cannot track episodes.",
                level="ERROR",
            )

    async def _send_test_notification(self, kwargs):
        """Send one test notification (used when test_notification_on_start is true)."""
        if not self.mobile_notifier:
            self.log("Test notification skipped: MobileNotifier not available", level="WARNING")
            return
        try:
            await self.mobile_notifier.notify(
                title="[TEST] Easterly wind episode",
                message="This is a test. Building-creak wind notifications are working.",
                target=self.notify_target,
                category="weather",
            )
            self.log("Test notification sent", level="INFO")
        except Exception as e:
            self.log(f"Test notification failed: {e}", level="WARNING")

    async def _check_conditions(self, kwargs):
        try:
            dir_raw = await self.get_state(self.wind_dir)
            mean_raw = await self.get_state(self.wind_dir_mean) if self.wind_dir_mean else None
            gust_raw = await self.get_state(self.wind_gust)
            speed_raw = await self.get_state(self.wind_speed)
        except Exception as e:
            self.log(f"get_state failed: {e}", level="WARNING")
            return

        direction, _src = self._pick_direction(mean_raw, dir_raw)
        if direction is None or gust_raw in (None, "unknown", "unavailable"):
            self._condition_not_met_count += 1
            if self._in_episode:
                await self._maybe_end_episode()
            else:
                self._condition_met_count = 0
            return

        try:
            gust_ha = float(gust_raw)
        except (TypeError, ValueError):
            self._condition_not_met_count += 1
            if self._in_episode:
                await self._maybe_end_episode()
            else:
                self._condition_met_count = 0
            return

        speed = None
        if speed_raw not in (None, "unknown", "unavailable"):
            try:
                speed = float(speed_raw)
            except (TypeError, ValueError):
                speed = None

        windy = gust_ha >= self.gust_windy or (
            speed is not None and speed >= self.wind_speed_windy
        )

        condition_met = self.dir_min <= direction <= self.dir_max and windy

        if condition_met:
            self._condition_met_count += 1
            self._condition_not_met_count = 0
            if self._in_episode:
                self._last_gust_in_episode = max(self._last_gust_in_episode, gust_ha)
            else:
                if self._condition_met_count >= self.sustained_min:
                    await self._start_episode(gust_ha, speed)
        else:
            self._condition_not_met_count += 1
            if self._in_episode:
                await self._maybe_end_episode()
            else:
                self._condition_met_count = 0

    def _fmt_spd(self, gust: float, mean: float | None) -> str:
        u = self.wind_unit_label
        suf = f" {u}" if u else ""
        mean_part = f", mean {mean:.1f}{suf}" if mean is not None else ""
        return f"gust up to {gust:.0f}{suf}{mean_part}"

    def _episode_peak_text(self, ul: str) -> tuple[str, str]:
        """(log phrase, notification phrase) for the ending episode's peak gust. A rehydrated
        episode's true peak is unknowable - only what this instance measured after the restart
        is - so it is never presented as the episode peak, and a rehydrated episode that ends
        without a single windy reading reports no figure at all instead of a fabricated 0."""
        if self._peak_from_episode_start:
            return (
                f"max gust in episode: {self._last_gust_in_episode:.1f}{ul}",
                f"Max gust was {self._last_gust_in_episode:.0f}{ul}.",
            )
        if self._last_gust_in_episode <= 0.0:
            return (
                "max gust unknown - the episode started before this AppDaemon restart",
                "Max gust unknown: the episode started before AppDaemon restarted.",
            )
        return (
            f"max gust since restart: {self._last_gust_in_episode:.1f}{ul} "
            "(episode started earlier, true peak unknown)",
            f"Max gust since AppDaemon restarted was {self._last_gust_in_episode:.0f}{ul} "
            "(the episode started earlier, so its true peak is unknown).",
        )

    async def _start_episode(self, current_gust: float, speed: float | None):
        self._in_episode = True
        self._last_gust_in_episode = current_gust
        self._peak_from_episode_start = True

        try:
            await self.call_service(
                "input_boolean/turn_on",
                entity_id=self.episode_entity,
            )
        except Exception as e:
            self.log(f"Failed to turn on {self.episode_entity}: {e}. Create this helper in HA.", level="ERROR")
        ul = f" {self.wind_unit_label}" if self.wind_unit_label else ""
        mean_log = f", mean {speed:.1f}{ul}" if speed is not None else ""
        self.log(
            f"Episode START (gust {current_gust:.1f}{ul}{mean_log})",
            level="INFO",
        )

        if self.mobile_notifier:
            try:
                await self.mobile_notifier.notify(
                    title="Easterly wind episode",
                    message=(
                        f"Building-creak wind pattern: easterly wind, {self._fmt_spd(current_gust, speed)}. "
                        "Possible building load."
                    ),
                    target=self.notify_target,
                    category="weather",
                )
            except Exception as e:
                self.log(f"Notify failed: {e}", level="WARNING")

    async def _maybe_end_episode(self):
        if self._condition_not_met_count < self.end_after_min:
            return

        self._in_episode = False
        self._condition_met_count = 0
        self._condition_not_met_count = 0

        try:
            await self.call_service(
                "input_boolean/turn_off",
                entity_id=self.episode_entity,
            )
        except Exception as e:
            self.log(f"Failed to turn off {self.episode_entity}: {e}. Create this helper in HA.", level="ERROR")
        ul = f" {self.wind_unit_label}" if self.wind_unit_label else ""
        log_peak, notify_peak = self._episode_peak_text(ul)
        self.log(
            f"Episode END ({log_peak})",
            level="INFO",
        )

        if self.notify_on_end and self.mobile_notifier:
            try:
                await self.mobile_notifier.notify(
                    title="Easterly wind episode over",
                    message=f"Episode ended. {notify_peak}",
                    target=self.notify_target,
                    category="weather",
                )
            except Exception as e:
                self.log(f"Notify (end) failed: {e}", level="WARNING")

        self._last_gust_in_episode = 0.0
        self._peak_from_episode_start = True
