"""
Easterly Wind Monitor: log and notify when wind pattern matches "building creak" situation.

Condition: easterly direction (defaults 60-120 deg), "windy" when
  mean wind >= wind_speed_windy OR gust >= gust_windy - use the same numeric unit as your HA sensors
  (e.g. if Ecowitt reports km/h, put thresholds in km/h; no conversion in code).

Sustained for sustained_minutes (consecutive checks; ~minutes when interval is 60s).

Sets input_boolean.easterly_wind_episode_active and sends notification via MobileNotifier.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class EasterlyWindMonitor(hass.Hass):
    def initialize(self):
        self.wind_dir = self.args.get("wind_direction_entity", "sensor.gw2000a_wind_direction")
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

        self._in_episode = False
        self._condition_met_count = 0
        self._condition_not_met_count = 0
        self._last_gust_in_episode = 0.0

        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
            if self.mobile_notifier:
                self.log("MobileNotifier found", level="INFO")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        self.run_every(self._check_conditions, "now", self.interval_s)
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
            )
            self.log("Test notification sent", level="INFO")
        except Exception as e:
            self.log(f"Test notification failed: {e}", level="WARNING")

    async def _check_conditions(self, kwargs):
        try:
            dir_raw = await self.get_state(self.wind_dir)
            gust_raw = await self.get_state(self.wind_gust)
            speed_raw = await self.get_state(self.wind_speed)
        except Exception as e:
            self.log(f"get_state failed: {e}", level="WARNING")
            return

        if dir_raw in (None, "unknown", "unavailable") or gust_raw in (None, "unknown", "unavailable"):
            self._condition_not_met_count += 1
            if self._in_episode:
                await self._maybe_end_episode()
            else:
                self._condition_met_count = 0
            return

        try:
            direction = float(dir_raw)
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

    async def _start_episode(self, current_gust: float, speed: float | None):
        self._in_episode = True
        self._last_gust_in_episode = current_gust

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
        self.log(
            f"Episode END (max gust in episode: {self._last_gust_in_episode:.1f}{ul})",
            level="INFO",
        )

        if self.notify_on_end and self.mobile_notifier:
            try:
                await self.mobile_notifier.notify(
                    title="Easterly wind episode over",
                    message=f"Episode ended. Max gust was {self._last_gust_in_episode:.0f}{ul}.",
                    target=self.notify_target,
                )
            except Exception as e:
                self.log(f"Notify (end) failed: {e}", level="WARNING")

        self._last_gust_in_episode = 0.0
