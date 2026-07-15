"""
Transit Alarm - Rejseplanen 2.0 departureBoard integration for AppDaemon.

Two independent functions:

  1. TRIGGERED CHECK  - fires when a trigger entity changes state (rising or
     falling edge).  Sends a mobile notification if issues are detected.
     Supports both a mailbox beacon (rising edge) and a person entity leaving
     home (falling edge) for testing.

  2. BACKGROUND POLL  - runs every `status_update_interval_min` minutes and
     writes current transit health to persistent HA helpers:

       input_select.transit_<route>_status  ->  OK / Delayed / Disrupted / Unavailable
       input_text.transit_<route>_info      ->  human-readable one-liner

     These helpers are stored in HA's .storage and survive reboots.
     A set_state() sensor is also written for rich attributes (upcoming
     times, issue list), but it is intentionally transient - think of it
     as a cache that the background poll refreshes seconds after startup.

Multiple named instances can be defined in apps.yaml - one per household
member - each with independent routes, notification targets, and thresholds.
Trigger-driven instances (e.g. leave-home trigger) should set
enable_status_change_alerts: false to avoid duplicate problem/clear notifications.

Requires an AppDaemon build where get_state/call_service are awaitable (async API).
"""

import re
import unicodedata
import asyncio
import aiohttp
from datetime import datetime, timedelta
from typing import Optional

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

REJSEPLANEN_BASE = "https://www.rejseplanen.dk/api"

# Per-route policy for `_evaluate` (required in YAML for every route).
EVALUATION_MODES = frozenset({"high_frequency", "passenger_impact", "infrequent_strict"})


def _ascii_lower(s: str) -> str:
    """Normalize Danish/accented chars to ASCII and lowercase for reliable string matching.
    e.g. 'København H' -> 'kobenhavn h' so direction filters work regardless of ø encoding."""
    return unicodedata.normalize("NFKD", s).encode("ascii", "ignore").decode("ascii").lower()


class TransitAlarm(hass.Hass):
    # ── Lifecycle ──────────────────────────────────────────────────────────

    def initialize(self) -> None:
        self.log("TransitAlarm initializing...")

        # trigger_entity: state-based trigger (person, beacon).  Empty = no state trigger.
        # Accepts both "trigger_entity" and legacy "mailbox_entity" key.
        self.trigger_entity: str = (
            self.args.get("trigger_entity") or self.args.get("mailbox_entity", "")
        )
        self.access_id: str = self.args["access_id"]

        # Trigger modes:
        #   "rising"   - entity becomes active (beacon detected)
        #   "falling"  - entity becomes inactive (person leaves home)
        #   "schedule" - time window + optional home check (for morning alerts)
        self.trigger_mode: str = self.args.get("trigger_mode", "rising").lower()

        # Schedule trigger settings (used when trigger_mode == "schedule")
        self.schedule_start: str   = self.args.get("schedule_start", "07:00")
        self.schedule_end: str     = self.args.get("schedule_end",   "09:00")
        self.schedule_interval_min: int = int(self.args.get("schedule_interval_min", 15))
        self.schedule_weekdays_only: bool = bool(self.args.get("schedule_weekdays_only", True))
        # Entity that must be "home" for schedule trigger to fire (e.g. person.kristine)
        self.requires_home_entity: str = self.args.get("requires_home_entity", "")
        # Entity used for "person home" gating on background alerts. If unset, uses
        # requires_home_entity then trigger_entity. Set explicitly when trigger is a
        # beacon (e.g. mailbox) so alerts are gated by person presence, not beacon.
        self.presence_entity: str = self.args.get("presence_entity", "")

        self.notify_target = self.args.get("notify_target", "home")

        self.duration_min: int  = int(self.args.get("duration_min", 30))
        self.max_journeys: int  = int(self.args.get("max_journeys", 25))
        self.double_check_sec: int = int(self.args.get("double_check_sec", 60))

        # Background polling interval (0 = disabled).
        # Typically only the dedicated TransitStatus instance has this > 0.
        self.status_update_interval_min: int = int(
            self.args.get("status_update_interval_min", 0)
        )

        # ── Smart polling ─────────────────────────────────────────────────
        # Slower interval outside active hours, skip when nobody home or all sleeping.
        self.poll_interval_night_min: int = int(
            self.args.get("poll_interval_night_min", self.status_update_interval_min * 6)
        )
        self.poll_active_start: str = self.args.get("poll_active_start", "05:30")
        self.poll_active_end:   str = self.args.get("poll_active_end",   "21:00")

        # Lists of entities to check.  Polling is skipped if:
        #   - poll_pause_nobody_home is true AND all home_entities are not "home"
        #   - all sleep_entities are "on"  (everyone is asleep)
        self.poll_pause_nobody_home: bool = bool(
            self.args.get("poll_pause_nobody_home", False)
        )
        # If set, we still poll at least once when data is this many minutes stale,
        # even if nobody home - so the Transit card doesn't show "300+ min ago".
        self.poll_max_stale_min: int = int(
            self.args.get("poll_max_stale_min", 0)
        )
        raw_home = self.args.get("home_entities", [])
        self.home_entities: list = (
            raw_home if isinstance(raw_home, list) else [raw_home]
        )
        raw_sleep = self.args.get("sleep_entities", [])
        self.sleep_entities: list = (
            raw_sleep if isinstance(raw_sleep, list) else [raw_sleep]
        )

        # Tracks when we last successfully polled (used to enforce intervals)
        self._last_polled: Optional[datetime] = None

        # Optional: entity to write "last update" time + interval for frontend
        self.last_updated_sensor: str = (
            self.args.get("last_updated_sensor", "sensor.transit_last_updated").strip()
            or ""
        )

        # Optional: include "ts" (Unix timestamp) in departure dicts for UI/debugging (default: strip for clean attributes).
        self.expose_departure_ts: bool = bool(self.args.get("expose_departure_ts", False))

        # Routes (required; validated before any listeners or timers)
        self.routes: list = self.args.get("routes")
        if self.routes is None:
            raise ValueError("TransitAlarm: 'routes' is required in app configuration.")
        if not isinstance(self.routes, list):
            raise ValueError("TransitAlarm: 'routes' must be a list.")
        if len(self.routes) == 0:
            raise ValueError("TransitAlarm: 'routes' must be non-empty.")
        for idx, route in enumerate(self.routes):
            if not isinstance(route, dict):
                raise ValueError(f"TransitAlarm: routes[{idx}] must be a mapping/dict.")
            mode = route.get("evaluation_mode")
            label = route.get("transport_name") or route.get("sensor_id") or f"routes[{idx}]"
            if not mode:
                raise ValueError(
                    f"TransitAlarm: route '{label}' is missing required 'evaluation_mode' "
                    f"({', '.join(sorted(EVALUATION_MODES))})."
                )
            if mode not in EVALUATION_MODES:
                raise ValueError(
                    f"TransitAlarm: route '{label}' has invalid evaluation_mode {mode!r}; "
                    f"expected one of: {', '.join(sorted(EVALUATION_MODES))}."
                )
        for route in self.routes:
            if "sensor_id" not in route:
                self.log(
                    f"Route '{route.get('transport_name', '?')}' has no sensor_id; "
                    "entity IDs use transport_name slug. Set sensor_id for multi-instance setups.",
                    level="WARNING",
                )
                break  # once per startup

        # Runtime state
        self._pending_handle = None
        # After sending "Transit Alert", don't send another until we've sent "back to normal".
        self._triggered_alert_cooldown: bool = False
        # Tracks the last status we sent a background alert for, per route.
        # Prevents repeat notifications while disruption persists.
        # Maps sensor_id -> "Delayed" | "Disrupted" | "Unavailable"
        self._disruption_notified: dict = {}

        # Wire up the trigger
        if self.trigger_mode == "schedule":
            # Only run a periodic timer if we don't have status listeners.
            # Notification instances rely on listener: TransitStatus updates every 3 min -> we notify on change.
            if not (self.status_update_interval_min == 0 and self.notify_target):
                self.run_every(
                    self._on_schedule_tick,
                    "now+30",
                    self.schedule_interval_min * 60,
                )
        else:
            if self.trigger_entity:
                self.listen_state(self._on_trigger_state, self.trigger_entity)
            # Optional: extra run_every in the schedule window (more API calls).
            if self.args.get("secondary_schedule") and not (
                self.status_update_interval_min == 0 and self.notify_target
            ):
                self.run_every(
                    self._on_schedule_tick,
                    "now+30",
                    self.schedule_interval_min * 60,
                )

        # Background disruption alerts: listen for status helper changes.
        # Only when enabled (default True). Set to false for trigger-driven instances
        # (e.g. leave-home) to avoid duplicate alerts: one "Transit Alert" + one status-change
        # "Delayed/Disrupted", and duplicate "back to normal" (triggered clear + _send_clear_alert).
        self.enable_status_change_alerts: bool = bool(
            self.args.get("enable_status_change_alerts", True)
        )
        if (
            self.enable_status_change_alerts
            and self.notify_target
            and self.status_update_interval_min == 0
            and (
                (self.trigger_mode in ("rising", "falling") and self.trigger_entity)
                or (
                    self.trigger_mode == "schedule"
                    and (self.status_update_interval_min != 0 or self.args.get("secondary_schedule"))
                )
            )
        ):
            self.log(
                "enable_status_change_alerts is true on a notification instance. "
                "If this instance also sends 'Transit Alert' (triggered/schedule), you may get duplicate "
                "problem/clear notifications. Consider setting enable_status_change_alerts: false.",
                level="WARNING",
            )
        if self.status_update_interval_min == 0 and self.notify_target and self.enable_status_change_alerts:
            for route in self.routes:
                self.listen_state(
                    self._on_route_status_change,
                    self._status_helper(route),
                    route=route,
                )

        # Optional manual-refresh button (press in HA dashboard -> immediate poll)
        refresh_button = self.args.get("refresh_button", "")
        if refresh_button:
            self.listen_state(self._on_refresh_button, refresh_button)
            self.log(f"Manual refresh button: '{refresh_button}'")

        if self.status_update_interval_min > 0:
            # First poll fires 5 s after startup unconditionally (warm up helpers).
            # Then a 60-second ticker drives _should_poll_now() to decide interval.
            self.run_in(lambda _: self.create_task(self._do_poll()), 5)
            self.run_every(self._on_status_poll, "now+60", 60)

        # Build a descriptive ready message
        if self.trigger_mode == "schedule":
            days = "weekdays" if self.schedule_weekdays_only else "daily"
            home = f", {self.requires_home_entity} home" if self.requires_home_entity else ""
            if self.status_update_interval_min == 0 and self.notify_target:
                mode_label = (
                    f"status changes in window {self.schedule_start}-{self.schedule_end} ({days}{home})"
                )
            else:
                mode_label = (
                    f"schedule {self.schedule_start}-{self.schedule_end} "
                    f"every {self.schedule_interval_min} min ({days}{home})"
                )
            trigger_desc = f"[{mode_label}]"
        elif self.trigger_entity:
            mode_label = (
                "person leaves (falling)" if self.trigger_mode == "falling"
                else "beacon detected (rising)"
            )
            trigger_desc = f"'{self.trigger_entity}' [{mode_label}]"
            if self.args.get("secondary_schedule") and not (
                self.status_update_interval_min == 0 and self.notify_target
            ):
                days = "weekdays" if self.schedule_weekdays_only else "daily"
                home = f", {self.requires_home_entity} home" if self.requires_home_entity else ""
                sec = (
                    f" + schedule {self.schedule_start}-{self.schedule_end} "
                    f"every {self.schedule_interval_min} min ({days}{home})"
                )
                trigger_desc += sec
        else:
            trigger_desc = "[no trigger - status poller only]"

        notify_desc = f"notify -> {self.notify_target}" if self.notify_target else "no notifications"
        self.log(
            f"Ready - trigger: {trigger_desc}, "
            f"{len(self.routes)} route(s), {notify_desc}"
        )

    # ── Entity-ID helpers ──────────────────────────────────────────────────

    def _route_slug(self, route: dict) -> str:
        """Returns the slug used for all helper entity IDs.
        Explicit sensor_id wins; otherwise derived from transport_name."""
        if "sensor_id" in route:
            return route["sensor_id"]
        name = route.get("transport_name", "route")
        return re.sub(r"[^a-z0-9]+", "_", name.lower()).strip("_")

    def _status_helper(self, route: dict) -> str:
        """Persistent input_select entity for this route's status."""
        return route.get(
            "status_helper",
            f"input_select.transit_{self._route_slug(route)}_status",
        )

    def _info_helper(self, route: dict) -> str:
        """Persistent input_text entity for this route's info line."""
        return route.get(
            "info_helper",
            f"input_text.transit_{self._route_slug(route)}_info",
        )

    def _sensor_entity(self, route: dict) -> str:
        """Transient sensor entity (rich attributes, recreated at startup)."""
        prefix = self.args.get("status_sensor_prefix", "sensor.transit_")
        return f"{prefix}{self._route_slug(route)}"

    def _enabled_helper(self, route: dict) -> str:
        """input_boolean that enables/disables a route (skips API call when off)."""
        return f"input_boolean.transit_{self._route_slug(route)}_enabled"

    async def _route_is_enabled(self, route: dict) -> bool:
        """Returns True unless the enabled toggle explicitly exists and is off."""
        entity = self._enabled_helper(route)
        state = await self.get_state(entity)
        if state is None:
            return True   # helper not yet created -> treat as enabled
        return str(state).lower() != "off"

    # ── Background disruption alerts ───────────────────────────────────────

    def _on_route_status_change(self, entity, attribute, old, new, kwargs) -> None:
        """Fires when a route status helper changes value.
        Sends one notification when a disruption starts and one when it clears."""
        if old == new or not new:
            return
        route     = kwargs.get("route", {})
        sensor_id = self._route_slug(route)
        name      = route.get("transport_name", sensor_id)

        bad = {"Delayed", "Disrupted", "Unavailable"}

        if new in bad and self._disruption_notified.get(sensor_id) != new:
            self._disruption_notified[sensor_id] = new
            self.log(f"Background alert: {name} changed to {new} (was {old}).")
            self.create_task(self._send_disruption_alert(route, new))

        elif new == "OK" and sensor_id in self._disruption_notified:
            prev = self._disruption_notified.pop(sensor_id)
            self.log(f"Background alert: {name} cleared ({prev} -> OK).")
            self.create_task(self._send_clear_alert(route))

    def _within_window(self, start: str, end: str, weekdays_only: bool) -> bool:
        """True if now is inside the time window [start, end) and (if weekdays_only) a weekday.
        Supports windows crossing midnight (e.g. 22:00-02:00)."""
        if not start or not end:
            return True
        now = datetime.now()
        if weekdays_only and now.weekday() >= 5:
            return False
        sh, sm = map(int, start.split(":"))
        eh, em = map(int, end.split(":"))
        start_min = sh * 60 + sm
        end_min = eh * 60 + em
        now_min = now.hour * 60 + now.minute
        if start_min < end_min:
            return start_min <= now_min < end_min
        # Wraparound: e.g. 22:00-02:00 -> now >= 22:00 or now < 02:00
        return now_min >= start_min or now_min < end_min

    async def _person_is_home(self) -> bool:
        """True if the presence entity (or requires_home_entity / trigger_entity fallback) is home.
        If no entity is configured (e.g. schedule-only with no requires_home_entity), returns True
        so background alerts are not gated by presence."""
        entity = self.presence_entity or self.requires_home_entity or self.trigger_entity
        if not entity:
            return True   # no entity configured -> always allow
        state = await self.get_state(entity)
        return self._is_active(str(state or ""))

    def _in_notification_window(self) -> bool:
        """True if we're inside this instance's notification window (for background alerts)."""
        return self._within_window(
            getattr(self, "schedule_start", "") or "",
            getattr(self, "schedule_end", "") or "",
            getattr(self, "schedule_weekdays_only", False),
        )

    async def _background_alert_allowed(self) -> tuple[bool, str]:
        """True if we may send a background (disruption/clear) notification. Returns (allowed, reason_if_not)."""
        if not self._in_notification_window():
            return False, "outside notification window"
        if not await self._person_is_home():
            return False, "person not home"
        return True, ""

    async def _send_disruption_alert(self, route: dict, status: str) -> None:
        allowed, reason = await self._background_alert_allowed()
        if not allowed:
            self.log(f"Disruption alert suppressed: {reason}.", level="DEBUG")
            return
        name = route.get("transport_name", "Transit")
        info = await self.get_state(self._info_helper(route)) or ""
        icon = "🚨" if status == "Disrupted" else "⚠️"
        await self._send_notification(
            f"{icon} {name}: {status}",
            info or f"Transit problems - {name} is {status.lower()}.",
        )

    async def _send_clear_alert(self, route: dict) -> None:
        allowed, reason = await self._background_alert_allowed()
        if not allowed:
            self.log(f"Clear alert suppressed: {reason}.", level="DEBUG")
            return
        name = route.get("transport_name", "Transit")
        info = await self.get_state(self._info_helper(route)) or ""
        await self._send_notification(
            f"✅ {name}: back to normal",
            info or f"{name} is running normally again.",
        )
        # Next triggered check may send a problem notification again.
        self._triggered_alert_cooldown = False

    # ── Trigger: state listener ────────────────────────────────────────────

    def _is_active(self, state: str) -> bool:
        return (state or "").lower().strip() in (
            "home", "present", "on", "detected", "true", "1",
        )

    def _on_trigger_state(self, entity, attribute, old, new, kwargs) -> None:
        was_active = self._is_active(old or "")
        is_active  = self._is_active(new or "")

        triggered = (
            (self.trigger_mode == "rising"  and is_active  and not was_active)
            or
            (self.trigger_mode == "falling" and was_active and not is_active)
        )

        if triggered:
            edge = "rising" if self.trigger_mode == "rising" else "falling"
            self.log(
                f"Trigger ({edge} edge): '{old}' -> '{new}' on {entity}. "
                "Starting transit check."
            )
            self._handle_trigger()
        else:
            self.log(
                f"State change on {entity}: '{old}' -> '{new}' "
                f"(no {self.trigger_mode} edge - ignored).",
                level="DEBUG",
            )

    # ── Trigger: schedule tick ─────────────────────────────────────────────

    def _on_schedule_tick(self, kwargs) -> None:
        """Called every schedule_interval_min. Schedules async work so get_state can be awaited."""
        self.create_task(self._async_on_schedule_tick())

    async def _async_on_schedule_tick(self) -> None:
        """Run within window + home gate (await get_state), then start triggered check."""
        if not self._within_window(
            self.schedule_start, self.schedule_end, self.schedule_weekdays_only
        ):
            return

        if self.requires_home_entity:
            state = await self.get_state(self.requires_home_entity)
            if not self._is_active(str(state or "")):
                self.log(
                    f"Schedule tick: {self.requires_home_entity} is '{state}' (not home) - skip.",
                    level="DEBUG",
                )
                return

        now = datetime.now()
        self.log(
            f"Schedule tick: {now.strftime('%H:%M')} within "
            f"{self.schedule_start}-{self.schedule_end} window. Starting transit check."
        )
        self._handle_trigger()

    # ── Triggered check path ───────────────────────────────────────────────

    def _handle_trigger(self) -> None:
        # Always run checks so sensors stay updated; cooldown only suppresses problem notifications.
        # If we skipped checks during cooldown, a single-instance setup would never get "OK" -> cooldown deadlock.
        if self._pending_handle:
            try:
                self.cancel_timer(self._pending_handle)
            except Exception:
                pass
            self._pending_handle = None

        if self.double_check_sec > 0:
            self.log(f"Initial check now; double-check in {self.double_check_sec} s.")
            self.run_in(self._run_triggered_check, 0, is_double_check=False)
            self._pending_handle = self.run_in(
                self._run_triggered_check, self.double_check_sec, is_double_check=True,
            )
        else:
            self.run_in(self._run_triggered_check, 0, is_double_check=False)

    def _run_triggered_check(self, kwargs) -> None:
        self.create_task(self._async_triggered_check(kwargs.get("is_double_check", False)))

    async def _async_triggered_check(self, is_double_check: bool) -> None:
        self.log(f"Triggered check (double_check={is_double_check})...")
        results = await self._fetch_all_routes()

        if is_double_check:
            self._pending_handle = None

        await self._update_status_sensors(results)

        impacted = [
            res for _, res in results
            if not res.get("disabled") and (res.get("has_issues") or res.get("error"))
        ]
        if not impacted:
            if self._triggered_alert_cooldown:
                self._triggered_alert_cooldown = False
                # Send "back to normal" only when status-change alerts are disabled, to avoid
                # duplicate with _send_clear_alert() when status helper flips to OK.
                if self.notify_target and not self.enable_status_change_alerts:
                    msg = self._build_all_clear_message(results)
                    await self._send_notification("Transit: back to normal", msg)
                self.log("Status clear; cooldown reset.")
            else:
                self.log("No transit issues - notification suppressed.")
            return

        if self._triggered_alert_cooldown:
            self.log("Cooldown active; problem notification suppressed (sensors updated).", level="DEBUG")
            return

        await self._send_notification("Transit Alert", self._build_message(results))
        self._triggered_alert_cooldown = True
        self.log("Cooldown active until status clears (next notification will be 'back to normal').")

    # ── Background polling path ────────────────────────────────────────────

    def _on_status_poll(self, kwargs) -> None:
        self.create_task(self._async_status_poll())

    async def _should_poll_now(self) -> bool:
        """Gate called on every 60-second tick.  Returns True when a real API
        poll should happen, based on time-of-day interval, presence, and sleep."""
        now = datetime.now()

        # ── Determine desired interval for this moment ──
        sh, sm = map(int, self.poll_active_start.split(":"))
        eh, em = map(int, self.poll_active_end.split(":"))
        now_min = now.hour * 60 + now.minute
        in_active = sh * 60 + sm <= now_min < eh * 60 + em

        desired_min = (
            self.status_update_interval_min if in_active
            else self.poll_interval_night_min
        )
        if desired_min <= 0:
            return False  # polling explicitly disabled for this window

        # ── Interval elapsed? ──
        if self._last_polled:
            elapsed = (now - self._last_polled).total_seconds() / 60
            if elapsed < desired_min:
                return False

        # ── Nobody home? ──
        if self.poll_pause_nobody_home and self.home_entities:
            home_states = [await self.get_state(e) for e in self.home_entities]
            if not any(s == "home" for s in home_states):
                # Allow one poll when data is very stale so the card doesn't show "300+ min ago"
                if self.poll_max_stale_min > 0:
                    if self._last_polled is None:
                        self.log("Poll forced: no previous poll (e.g. after restart).", level="INFO")
                        setattr(self, "_poll_reason", "stale")
                        return True
                    stale_min = (now - self._last_polled).total_seconds() / 60
                    if stale_min >= self.poll_max_stale_min:
                        self.log(
                            f"Poll forced: data {int(stale_min)} min stale (nobody home).",
                            level="INFO",
                        )
                        setattr(self, "_poll_reason", "stale")
                        return True
                self.log("Poll skipped: nobody home.", level="DEBUG")
                return False

        # ── Everyone sleeping? ──
        if self.sleep_entities:
            sleep_states = [await self.get_state(e) for e in self.sleep_entities]
            if all(s == "on" for s in sleep_states):
                self.log("Poll skipped: everyone sleeping.", level="DEBUG")
                return False

        setattr(self, "_poll_reason", "interval")
        return True

    async def _run_fetch_and_update(self, poll_reason: str) -> list[tuple[dict, dict]]:
        """Fetch all routes and write to HA helpers/sensors. Sets _last_polled. Returns results."""
        self._last_polled = datetime.now()
        results = await self._fetch_all_routes()
        await self._update_status_sensors(results, poll_reason=poll_reason)
        return results

    async def _async_status_poll(self) -> None:
        if not await self._should_poll_now():
            return
        self.log("Background poll: updating transit status...", level="DEBUG")
        reason = getattr(self, "_poll_reason", "interval")
        await self._run_fetch_and_update(reason)

    def _on_refresh_button(self, entity, attribute, old, new, kwargs) -> None:
        self.log("Manual refresh triggered via button.")
        self.create_task(self._do_poll())

    async def _do_poll(self) -> None:
        """Unconditional poll - bypasses _should_poll_now gate."""
        self.log("Polling transit status...", level="DEBUG")
        await self._run_fetch_and_update("manual")

    # ── Shared data-fetch layer ────────────────────────────────────────────

    async def _fetch_all_routes(self) -> list[tuple[dict, dict]]:
        max_concurrent = int(self.args.get("fetch_concurrency", 4))
        sem = asyncio.Semaphore(max_concurrent)

        async def limited_check(route: dict, session: aiohttp.ClientSession) -> dict:
            async with sem:
                return await self._check_route(route, session)

        async with aiohttp.ClientSession() as session:
            coros = [limited_check(route, session) for route in self.routes]
            res_list = await asyncio.gather(*coros, return_exceptions=True)
        results = []
        for route, res in zip(self.routes, res_list):
            if isinstance(res, Exception):
                results.append((route, {"error": str(res), "has_issues": True}))
                self.log(f"Route {route.get('transport_name', '?')} failed: {res}", level="WARNING")
            else:
                results.append((route, res))
        return results

    # ── HA entity / helper update ──────────────────────────────────────────

    async def _update_status_sensors(self, results: list[tuple[dict, dict]], poll_reason: Optional[str] = None) -> None:
        """
        Write transit health to two layers:
          1. Persistent helpers  - input_select (status) + input_text (info)
             Stored in HA .storage -> survive reboots.
          2. Transient sensor    - set_state() with rich attributes
             Recreated 5 s after AppDaemon starts; useful for templates/dashboards
             that need the full upcoming-departures list.
        """
        now = datetime.now()
        now_str = now.strftime("%H:%M")
        data_as_of_iso = now.isoformat()  # so frontend can hide past departures and age data

        for route, res in results:
            name     = route.get("transport_name", "Route")
            severity = 4 if res.get("error") else res.get("severity", 0)

            # ── Classify ──
            # Only real disruptions (delay, cancellation, API error) change status.
            # Quiet periods and scheduled gaps are always OK.
            # Disabled routes are written as OK with a "Paused" info line.
            if res.get("disabled"):
                status = "OK"
                icon   = "mdi:pause-circle-outline"
            elif res.get("error"):
                status = "Unavailable"
                icon   = "mdi:alert-circle-outline"
            elif severity >= 3:
                status = "Disrupted"
                icon   = "mdi:alert"
            elif severity >= 2:
                status = "Delayed"
                icon   = "mdi:clock-alert-outline"
            else:
                status = "OK"
                icon   = "mdi:check-circle-outline"

            # ── Build info line (<= 255 chars for input_text) ──
            info = self._build_info_line(res, status, route)

            # 1. Persistent input_select: status
            sel_id = self._status_helper(route)
            try:
                await self.call_service(
                    "input_select/select_option",
                    entity_id=sel_id,
                    option=status,
                )
            except Exception as exc:
                self.log(f"Could not update {sel_id}: {exc}", level="WARNING")

            # 2. Persistent input_text: info line
            txt_id = self._info_helper(route)
            try:
                await self.call_service(
                    "input_text/set_value",
                    entity_id=txt_id,
                    value=info,
                )
            except Exception as exc:
                self.log(f"Could not update {txt_id}: {exc}", level="WARNING")

            # 3. Transient sensor (rich attributes - recreated on startup)
            lines_str = ", ".join(str(ln) for ln in route.get("lines", []))
            self.set_state(
                self._sensor_entity(route),
                state=status,
                attributes={
                    "friendly_name":  f"Transit: {name}",
                    "icon":           icon,
                    "next_departure": res.get("next_departure"),
                    "mins_to_next":   res.get("mins_to_next"),
                    "upcoming":       res.get("future_dep_times", []),
                    "departures":     res.get("departures", []),
                    "issues":         res.get("issues", []),
                    "lines":          lines_str,
                    "last_checked":   now_str,
                    "data_as_of":     data_as_of_iso,  # ISO timestamp; UI should hide departures before current time
                },
                replace=True,
            )

            self.log(
                f"{sel_id} -> {status} | {txt_id} -> \"{info[:60]}\"",
                level="DEBUG",
            )

        # Single sensor for frontend: last update time (state) + interval as setting (attribute).
        # Only TransitStatus writes it. poll_reason is passed in so it matches why we actually polled (no race).
        if getattr(self, "last_updated_sensor", "") and self.status_update_interval_min > 0:
            reason = poll_reason or getattr(self, "_poll_reason", "interval")
            now_min = now.hour * 60 + now.minute
            sh, sm = map(int, getattr(self, "poll_active_start", "05:30").split(":"))
            eh, em = map(int, getattr(self, "poll_active_end", "21:00").split(":"))
            in_active = (sh * 60 + sm) <= now_min < (eh * 60 + em)
            if reason == "stale":
                effective_min = getattr(self, "poll_max_stale_min", 0) or getattr(
                    self, "poll_interval_night_min", 30
                )
            else:
                effective_min = (
                    self.status_update_interval_min
                    if in_active
                    else getattr(self, "poll_interval_night_min", 30)
                )
            self.set_state(
                self.last_updated_sensor,
                state=data_as_of_iso,
                attributes={
                    "friendly_name": "Transit last update",
                    "icon": "mdi:update",
                    "update_interval_min": effective_min,
                    "effective_interval_min": effective_min,
                    "poll_active_start": getattr(self, "poll_active_start", ""),
                    "poll_active_end": getattr(self, "poll_active_end", ""),
                    "poll_interval_night_min": getattr(self, "poll_interval_night_min", 30),
                    "poll_max_stale_min": getattr(self, "poll_max_stale_min", 0),
                },
                replace=True,
            )

    def _build_info_line(self, res: dict, status: str, route: dict = None) -> str:
        """One-liner for the input_text helper (max 255 chars)."""
        stop_name = (route or {}).get("stop_name", "")

        if res.get("disabled"):
            return f"{stop_name}: Paused" if stop_name else "Paused"
        if res.get("error"):
            return f"{stop_name}: Unavailable" if stop_name else "Unavailable"
        if res.get("no_service"):
            duration = int((route or {}).get("duration_min", self.duration_min))
            base = f"Quiet period - no trains in next {duration} min"
            return f"{stop_name}: {base}" if stop_name else base

        next_dep = res.get("next_departure")
        mins     = res.get("mins_to_next")
        upcoming = res.get("future_dep_times", [])

        parts: list[str] = []
        if stop_name:
            parts.append(stop_name)
        if next_dep:
            parts.append(f"Next: {next_dep}" + (f" ({mins} min)" if mins is not None else ""))
        if len(upcoming) > 1:
            parts.append("then " + "  ".join(upcoming[1:4]))
        if res.get("issues"):
            short = "; ".join(i.split(": ", 1)[-1] for i in res["issues"][:2])
            parts.append(short)

        return " | ".join(parts)[:255] or status

    # ── Rejseplanen API call ───────────────────────────────────────────────

    async def _check_route(self, route: dict, session: aiohttp.ClientSession) -> dict:
        if not await self._route_is_enabled(route):
            self.log(
                f"{route.get('transport_name', route.get('sensor_id', '?'))} is disabled - skipping.",
                level="DEBUG",
            )
            return {"disabled": True, "has_issues": False, "severity": 0}

        stop_id        = route["stop_id"]
        direction_id   = route.get("direction_id", "")
        lines          = route.get("lines", [])
        name_filter    = (route.get("direction_name_filter") or "").lower()
        delay_thr      = int(route.get("delay_threshold_min", 5))
        transport_name = route.get("transport_name", f"Stop {stop_id}")

        # Per-route duration override - useful for infrequent services (IC, regional)
        # where the instance-level duration_min may not capture any trains.
        duration = int(route.get("duration_min", self.duration_min))

        params: dict = {
            "id":          stop_id,
            "duration":    duration,
            "maxJourneys": self.max_journeys,
            "format":      "json",
            "rtMode":      "SERVER_DEFAULT",
            "accessId":    self.access_id,
        }
        if direction_id:
            params["direction"] = direction_id
        if lines:
            params["lines"] = ",".join(str(ln) for ln in lines)

        url = f"{REJSEPLANEN_BASE}/departureBoard"
        try:
            async with session.get(
                url, params=params, timeout=aiohttp.ClientTimeout(total=10)
            ) as resp:
                if resp.status != 200:
                    self.log(f"API HTTP {resp.status} for {transport_name}", level="WARNING")
                    return {"error": f"HTTP {resp.status}", "has_issues": True}
                data = await resp.json(content_type=None)
        except Exception as exc:
            self.log(f"API request failed for {transport_name}: {exc}", level="WARNING")
            return {"error": str(exc), "has_issues": True}

        if "errorCode" in data:
            msg = data.get("errorText", "unknown API error")
            self.log(f"API error for {transport_name}: {msg}", level="WARNING")
            return {"error": msg, "has_issues": True}

        departures: list = data.get("Departure", [])

        if name_filter:
            nf = _ascii_lower(name_filter)
            departures = [
                d for d in departures
                if nf in _ascii_lower(d.get("direction") or "")
            ]

        # Optional post-filter by transport category (e.g. ["IC", "Re"] for long-distance).
        # Useful when a stop serves mixed modes (Metro + IC at KBH H).
        cat_filter = [c.lower() for c in route.get("line_categories", [])]
        if cat_filter:
            departures = [
                d for d in departures
                if (d.get("ProductAtStop", {}).get("catOut") or "").lower() in cat_filter
            ]

        had_filters = bool(name_filter or cat_filter)
        if not departures:
            if had_filters:
                self.log(
                    f"No departures for {transport_name} after direction/category filters "
                    "(quiet period or check config).",
                    level="INFO",
                )
            else:
                self.log(f"Quiet period for {transport_name} - no departures in next {duration} min.")
            return {
                "has_issues":       False,
                "no_service":       True,
                "issues":           [],
                "future_dep_times": [],
                "next_departure":   None,
                "mins_to_next":     None,
                "severity":         0,
                "departures":       [],
            }

        return self._evaluate(departures, route, delay_thr, transport_name)

    # ── Evaluation ─────────────────────────────────────────────────────────

    def _parse_dt(self, date_str: Optional[str], time_str: Optional[str]) -> Optional[datetime]:
        if not time_str:
            return None
        try:
            base = date_str or datetime.now().strftime("%Y-%m-%d")
            dt   = datetime.strptime(f"{base} {time_str[:5]}", "%Y-%m-%d %H:%M")
            if dt < datetime.now() - timedelta(hours=6):
                dt += timedelta(days=1)
            return dt
        except ValueError:
            return None

    def _normalize_departure(self, dep: dict) -> dict:
        """Parse one API departure into a single dict: effective, line, cancelled, delay_min, etc."""
        sched = self._parse_dt(dep.get("date"), dep.get("time"))
        rt = self._parse_dt(dep.get("rtDate") or dep.get("date"), dep.get("rtTime"))
        line = (
            dep.get("ProductAtStop", {}).get("displayNumber")
            or dep.get("ProductAtStop", {}).get("line")
            or ""
        )
        cancelled = (
            dep.get("cancelled") is True
            or str(dep.get("cancelled", "")).lower() == "true"
            or dep.get("JourneyStatus") == "C"
        )
        effective = rt or sched
        delay_min = 0
        if sched and rt and rt > sched:
            delay_min = int((rt - sched).total_seconds() / 60)
        time_label = (dep.get("rtTime") or dep.get("time") or "?")[:5]
        scheduled_str = (dep.get("time") or "")[:5]
        return {
            "effective": effective,
            "scheduled": sched,
            "scheduled_str": scheduled_str,
            "line": line,
            "cancelled": cancelled,
            "delay_min": delay_min,
            "time_label": time_label,
        }

    @staticmethod
    def _fmt_dep(t: datetime, line: str) -> str:
        return f"{line} {t.strftime('%H:%M')}" if line else t.strftime("%H:%M")

    def _assemble_eval_result(
        self,
        *,
        issues: list[str],
        severity: int,
        future_only: list[dict],
        delay_thr: int,
        now: datetime,
        max_deps: int,
    ) -> dict:
        future_deps = sorted(
            [(n["effective"], n["line"]) for n in future_only if not n["cancelled"]],
            key=lambda x: x[0],
        )
        next_t = future_deps[0][0] if future_deps else None
        next_line = future_deps[0][1] if future_deps else ""
        mins_to_next = int((next_t - now).total_seconds() / 60) if next_t else None

        departures_for_ui: list[dict] = [
            {
                "ts": int(n["effective"].timestamp()),
                "time": n["effective"].strftime("%H:%M"),
                "line": n["line"],
                "delay_min": n["delay_min"],
                "cancelled": n["cancelled"],
                "scheduled_time": n["scheduled_str"],
                "problematic": n["cancelled"] or n["delay_min"] >= delay_thr,
            }
            for n in future_only
        ]
        seen: dict[tuple[int, str], dict] = {}
        for d in departures_for_ui:
            key = (d["ts"], d["line"])
            if key not in seen:
                seen[key] = d.copy()
            else:
                prev = seen[key]
                prev["delay_min"] = max(prev["delay_min"], d["delay_min"])
                prev["cancelled"] = prev["cancelled"] or d["cancelled"]
                prev["problematic"] = prev["problematic"] or d["problematic"]
        departures_for_ui = list(seen.values())
        departures_for_ui.sort(key=lambda d: (d["ts"], d["line"]))

        departures_out = [d.copy() for d in departures_for_ui[:max_deps]]
        if not getattr(self, "expose_departure_ts", False):
            for d in departures_out:
                d.pop("ts", None)
        return {
            "has_issues":       severity > 0,
            "no_service":       False,
            "issues":           issues,
            "severity":         severity,
            "future_dep_times": [self._fmt_dep(t, ln) for t, ln in future_deps[:6]],
            "next_departure":   self._fmt_dep(next_t, next_line) if next_t else None,
            "mins_to_next":     mins_to_next,
            "departures":       departures_out,
        }

    def _evaluate_high_frequency(self, normalized: list, name: str, now: datetime) -> tuple[list[str], int]:
        issues: list[str] = []
        severity = 0
        cancellations: list[str] = []
        for n in normalized:
            if not n["effective"]:
                continue
            if n["cancelled"]:
                if n["line"]:
                    msg = f"{name}: ({n['line']}) departure at {n['time_label']} is cancelled"
                else:
                    msg = f"{name}: departure at {n['time_label']} is cancelled"
                cancellations.append(msg)
        if len(cancellations) >= 2:
            issues.extend(cancellations)
            severity = max(severity, 3)
        return issues, severity

    def _evaluate_infrequent_strict(
        self, normalized: list, name: str, now: datetime, delay_thr: int
    ) -> tuple[list[str], int]:
        issues: list[str] = []
        severity = 0
        next_dep_candidates: list[tuple[datetime, str, int]] = []
        for n in normalized:
            if not n["effective"]:
                continue
            if n["cancelled"]:
                if n["line"]:
                    msg = f"{name}: ({n['line']}) departure at {n['time_label']} is cancelled"
                else:
                    msg = f"{name}: departure at {n['time_label']} is cancelled"
                issues.append(msg)
                severity = max(severity, 3)
                continue
            if n["effective"] >= now - timedelta(minutes=1):
                next_dep_candidates.append((n["effective"], n["line"], n["delay_min"]))
        if next_dep_candidates:
            next_dep_candidates.sort(key=lambda x: x[0])
            _, next_line, next_delay = next_dep_candidates[0]
            if next_delay >= delay_thr:
                line_tag = f" line {next_line}" if next_line else ""
                issues.append(
                    f"{name}:{line_tag} {next_delay} min delay (next departure)"
                )
                severity = max(severity, 2)
        return issues, severity

    def _evaluate_passenger_impact(
        self, future_only: list, route: dict, delay_thr: int, name: str
    ) -> tuple[list[str], int]:
        issues: list[str] = []
        severity = 0
        rescue_window = int(route.get("rescue_window_min", delay_thr))

        if not future_only:
            return issues, severity

        viable = [n for n in future_only if not n["cancelled"]]
        had_cancel = any(n["cancelled"] for n in future_only)

        if not viable:
            cancelled_labels: list[str] = []
            for n in future_only:
                if n["cancelled"]:
                    if n["line"]:
                        cancelled_labels.append(f"({n['line']}) {n['time_label']}")
                    else:
                        cancelled_labels.append(n["time_label"])
            tail = ", ".join(cancelled_labels[:3])
            if cancelled_labels:
                issues.append(
                    f"{name}: no viable upcoming departures (cancelled: {tail})"
                )
            else:
                issues.append(f"{name}: no viable upcoming departures")
            severity = max(severity, 3)
            return issues, severity

        t_sched = min((n["scheduled"] or n["effective"]) for n in future_only)
        t_viable = min(n["effective"] for n in viable)
        extra_wait = max(0, int((t_viable - t_sched).total_seconds() / 60))
        best = min(viable, key=lambda n: (n["effective"], n["line"]))

        if had_cancel and extra_wait < rescue_window:
            issues.append(
                f"{name}: cancellation absorbed by alternative departure "
                f"({best['time_label']}, +{extra_wait} min)"
            )

        if extra_wait >= delay_thr:
            line_tag = f" line {best['line']}" if best["line"] else ""
            issues.append(
                f"{name}:{line_tag} best alternative departs {extra_wait} min later than earliest scheduled"
            )
            severity = max(severity, 2)

        return issues, severity

    def _evaluate(self, departures: list, route: dict, delay_thr: int, name: str) -> dict:
        now = datetime.now()
        normalized = [self._normalize_departure(d) for d in departures]
        future_only = [
            n for n in normalized
            if n["effective"] and n["effective"] >= now - timedelta(minutes=1)
        ]
        future_only.sort(key=lambda n: (n["effective"], n["line"]))

        mode = route["evaluation_mode"]
        if mode == "high_frequency":
            issues, severity = self._evaluate_high_frequency(normalized, name, now)
            max_deps = 6
        elif mode == "passenger_impact":
            issues, severity = self._evaluate_passenger_impact(future_only, route, delay_thr, name)
            max_deps = 12
        elif mode == "infrequent_strict":
            issues, severity = self._evaluate_infrequent_strict(normalized, name, now, delay_thr)
            max_deps = 12
        else:
            raise RuntimeError(f"Unhandled evaluation_mode {mode!r}")

        return self._assemble_eval_result(
            issues=issues,
            severity=severity,
            future_only=future_only,
            delay_thr=delay_thr,
            now=now,
            max_deps=max_deps,
        )

    # ── Message building ───────────────────────────────────────────────────

    def _build_all_clear_message(self, results: list[tuple[dict, dict]]) -> str:
        """Short message for 'back to normal' when triggered check finds no issues."""
        lines: list[str] = []
        for route, res in results:
            if res.get("disabled"):
                continue
            name = route.get("transport_name", "Transit")
            if res.get("no_service"):
                lines.append(f"✅ {name}: quiet period")
            else:
                next_dep = res.get("next_departure")
                mins = res.get("mins_to_next")
                mins_str = f" (in {mins} min)" if mins is not None else ""
                lines.append(f"✅ {name}: next {next_dep}{mins_str}, on time")
        return "\n".join(lines) if lines else "All routes OK"

    def _build_message(self, results: list[tuple[dict, dict]]) -> str:
        lines: list[str] = []

        for route, res in results:
            if res.get("disabled"):
                continue
            name     = route.get("transport_name", "Transit")
            next_dep = res.get("next_departure")
            mins     = res.get("mins_to_next")
            mins_str = f" (in {mins} min)" if mins is not None else ""

            if res.get("error"):
                lines.append(f"⚠️ {name}: data unavailable ({res['error']})")
            elif res.get("has_issues"):
                lines.append(f"⚠️ Transit problems - {name}: next {next_dep}{mins_str}")
                for issue in res.get("issues", []):
                    lines.append(f"  • {issue}")
            elif res.get("no_service"):
                duration = int(route.get("duration_min", self.duration_min))
                lines.append(f"✅ {name}: quiet period (no trains in next {duration} min)")
            else:
                lines.append(f"✅ {name}: next {next_dep}{mins_str}, on time")

        rec = self._recommend(results)
        if rec:
            lines.append(f"\n💡 {rec}")

        return "\n".join(lines)

    def _recommend(self, results: list[tuple[dict, dict]]) -> Optional[str]:
        active = [(r, res) for r, res in results if not res.get("disabled")]
        if len(active) < 2:
            return None

        (route_a, res_a), (route_b, res_b) = active[0], active[1]
        name_a     = route_a.get("transport_name", "Option A")
        name_b     = route_b.get("transport_name", "Option B")
        stop_a     = route_a.get("stop_name", name_a)
        stop_b     = route_b.get("stop_name", name_b)

        a_ok  = not res_a.get("has_issues") and not res_a.get("error") and not res_a.get("no_service")
        b_ok  = not res_b.get("has_issues") and not res_b.get("error") and not res_b.get("no_service")
        mins_a = res_a.get("mins_to_next")
        mins_b = res_b.get("mins_to_next")

        def _next(mins):
            return f"next in {mins} min" if mins is not None else "no info"

        if a_ok and b_ok:
            # Both fine - suggest whichever departs significantly sooner
            if mins_b is not None and mins_a is not None and mins_b + 8 <= mins_a:
                return (
                    f"Head to {stop_b} ({name_b}, {_next(mins_b)}) - "
                    f"{mins_a - mins_b} min earlier than {name_a}"
                )
            return f"Both OK - head to {stop_a} ({name_a}, {_next(mins_a)})"
        if a_ok:
            return (
                f"Head to {stop_a} ({name_a}, {_next(mins_a)}) - "
                f"{name_b} has problems"
            )
        if b_ok:
            return (
                f"Head to {stop_b} ({name_b}, {_next(mins_b)}) - "
                f"{name_a} has problems"
            )
        return "Both routes have problems - consider bus or taxi"

    # ── Notification dispatch ──────────────────────────────────────────────

    async def _send_notification(self, title: str, message: str) -> None:
        # self.get_app is @sync_decorator-wrapped: from the main async thread it
        # returns an asyncio.Task, not the app. Use app_management.get_app for the instance.
        notifier = self.AD.app_management.get_app("MobileNotifier")
        if notifier:
            target = self.notify_target
            # MobileNotifier maps person names (mikkel, kristine) only when
            # target is a list. A bare string is treated as a service name and fails.
            if (
                target
                and target not in ("home", "user", "all")
                and isinstance(target, str)
                and "notify." not in target
            ):
                target = [target]
            await notifier.notify(title=title, message=message, target=target)
            self.log(f"Notification sent via MobileNotifier (target={target})", level="INFO")
            return

        services = self.notify_target if isinstance(self.notify_target, list) else [self.notify_target]
        # Fallback: "home"/"all" are not valid HA service names; use default notify.
        default_svc = "notify/notify"
        for svc in services:
            svc_path = svc.replace("notify.", "notify/", 1) if "." in svc else svc
            if svc_path in ("home", "user", "all") or not svc_path.startswith("notify"):
                svc_path = default_svc
            try:
                await self.call_service(svc_path, title=title, message=message)
                self.log(f"Notification sent via {svc_path}")
            except Exception as exc:
                self.log(f"Failed to send via {svc_path}: {exc}", level="WARNING")
