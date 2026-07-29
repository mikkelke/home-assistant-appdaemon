"""
Morning climate briefing -- ONE phone push, once a day, carrying the climate decision
SmartCooling has already computed: open/close windows, and whether to deploy + arm the
portable bedroom AC for tonight.

The user has ~1h at home after waking, before leaving for work, and the decision itself
is manual by design -- this app is purely advisory (see below). It composes the message
from what's already published (sensor.sleep_plan + sensor.smart_cooling_status); no new
projection logic lives here.

Trigger = the alarm, not motion (2026-07-29 fix): the briefing used to fire on the first
PIR/bed-exit edge after from_hour, and an early riser (or an early wake-up in general) sent
it at 05:30 -- the coldest, most misleading moment of the day for the sleep plan's own
window-feasibility test (see climate_model.plan_sleep's night_outdoor grounding: "AC not
needed" at 05:55 while the plan's own projected peak was already 3.4C over the limit,
flipping itself to "ac" by 10:00 once the day's real numbers rolled in). Now:
  - Alarm ENABLED (input_boolean.wakeup_bedroom) -> send at the alarm time
    (input_datetime.wakeup_bedroom), rescheduled via listen_state whenever that datetime
    changes.
  - Alarm DISABLED -> send at the day-type fallback instead: 07:00 on workdays,
    09:00 on weekends (user 2026-07-29).
  - Alarm turned OFF DURING the morning window (woke early / cancelled the alarm) -> send
    immediately, at that moment -- but only inside [from_hour, until_hour); disabling it the
    night before must not send.
The fallback run never pre-empts a still-pending alarm: if the alarm is enabled and its
time hasn't happened yet today, the fallback silently defers to it (see
_alarm_pending_today). All three trigger paths funnel into the same _handle_wake() -- one
set of gates, never duplicated.

Gates, checked in this order inside an asyncio.Lock (the sent-date is re-checked INSIDE
the lock so two near-simultaneous triggers -- e.g. the alarm firing right as the fallback's
own pre-empt check is mid-flight -- can't both slip past the once-per-day check and
double-send):
  1. Local hour in [from_hour, until_hour) -- outside it, ignore silently.
  2. Once per day -- last-sent date persisted to state_file.
  3. Home (person_entity) -- suppress ONLY on a live "not_home" reading; a dead/unknown/
     unavailable sensor is never evidence of being away (same semantics as SmartCooling's
     rescue_home_entity gate).
  4. Data -- sensor.sleep_plan must have a real state and non-empty attributes. Missing/
     unknown data does NOT mark the day as sent, so a later trigger (e.g. after an HA
     restart delays the plan's first publish) retries.

compose_briefing() is a pure function (plan/status data in, (title, message) out), so the
wording is directly unit-testable without any AppDaemon/HA plumbing.

Advisory only: this app NEVER calls a climate/switch/cover service. Its only outputs are
one notification and its own logs.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import asyncio
import json
from datetime import time, timedelta
from typing import Optional


import climate_model as cm  # the one-voice copy + wake resolution live in the shared model

# Compatibility aliases: the pure copy moved to climate_model (2026-07-29) so the push,
# the Tonight card and any future surface render the SAME words from the SAME function.
_nice_cost = cm.nice_cost
compose_briefing = cm.compose_briefing


class MorningBriefing(hass.Hass):
    def initialize(self) -> None:
        a = self.args.get
        # --- alarm-based trigger (see module docstring) ---
        self.alarm_time_entity = a("alarm_time_entity", "input_datetime.wakeup_bedroom")
        self.alarm_enabled_entity = a("alarm_enabled_entity", "input_boolean.wakeup_bedroom")
        # Weekday-aware fallback (user 2026-07-29): with no alarm set the briefing lands
        # 07:00 on workdays, 09:00 on weekends. A legacy single `fallback_time` (if present)
        # seeds both so old configs keep their behavior.
        legacy_fb = a("fallback_time", None)
        self.fallback_workday = a("fallback_time_workday", legacy_fb or "07:00:00")
        self.fallback_weekend = a("fallback_time_weekend", legacy_fb or "09:00:00")
        # --- gates ---
        self.person_entity = a("person_entity", "person.mikkel")
        self.from_hour = int(a("from_hour", 5))
        self.until_hour = int(a("until_hour", 12))
        # --- data sources (read-only; published by SmartCooling) ---
        self.sleep_plan_entity = a("sleep_plan_entity", "sensor.sleep_plan")
        self.status_entity = a("status_entity", "sensor.smart_cooling_status")
        self.climate_entity = a("climate_entity", "climate.air_conditioner_thermostat")
        self.enable_entity = a("enable_entity", "input_boolean.smart_cooling")
        # DeployAdvisor's multi-night projection -- its lead-time warning is a line in
        # THIS briefing now (one-advice stream, user 2026-07-23); its own pushes are off.
        self.projection_entity = a("projection_entity", "sensor.bedroom_night_projection")
        # --- notification ---
        self.notify_target = a("notify_target", "user")
        # Push polish (Android companion-app extras; each can be set "" in yaml to disable):
        # a stable tag makes a re-send REPLACE the previous briefing instead of stacking,
        # the channel gives the briefing its own Android notification channel (per-channel
        # sound/importance on the phone), the icon brands it. click_url defaults EMPTY:
        # with no clickAction the companion app's default tap opens the HA app itself
        # (user 2026-07-22 -- a /local/ URL opened the phone BROWSER instead). Set it to
        # an in-app path only if a specific view should open.
        self.notify_tag = a("notify_tag", "morning_briefing")
        self.notify_channel = a("notify_channel", "Morning climate")
        self.notify_icon = a("notify_icon", "mdi:bed-clock")
        self.click_url = a("click_url", "")
        self.state_file = a("state_file", "/conf/apps/climate/morning_briefing_state.json")

        self._sent_date: Optional[str] = None
        self._load_state()
        # Serializes _handle_wake(): the alarm/fallback/alarm-disabled triggers can each
        # schedule their own create_task on near-simultaneous events (e.g. the fallback's
        # own pre-empt check racing the alarm's fire); the once-per-day date is re-checked
        # INSIDE the lock so only the first to get there actually sends (see
        # _handle_wake_locked).
        self._wake_lock = asyncio.Lock()

        # get_app must be resolved in the sync init context -- inside async methods
        # AppDaemon hands back a Task instead of the app instance.
        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        # Fallback runs: one per day-type (workday 07:00 / weekend 09:00); each fires only
        # on its own day-type and defers to a still-pending alarm (see _on_fallback_fire /
        # _alarm_pending_today). Both may target the same time -- the once-per-day gate
        # dedupes.
        fb_work = self._parse_hms(self.fallback_workday) or time(7, 0, 0)
        fb_week = self._parse_hms(self.fallback_weekend) or time(9, 0, 0)
        self.run_daily(self._on_fallback_fire, fb_work, day_type="workday")
        self.run_daily(self._on_fallback_fire, fb_week, day_type="weekend")
        # Alarm run: (re)scheduled now and whenever the alarm datetime changes.
        self._alarm_timer = None
        self._schedule_alarm_run()
        self.listen_state(self._on_alarm_time_changed, self.alarm_time_entity)
        # Alarm turned off mid-morning (woke early / cancelled) -> send right now instead of
        # waiting for the fallback -- still gated to the sanity window (see the callback).
        self.listen_state(self._on_alarm_enabled_change, self.alarm_enabled_entity, new="off")
        # "Wake up now" button (user 2026-07-29): the press IS the wake moment -- deliver
        # the briefing right then. All the usual gates (window, once/day, home, data)
        # still apply inside _handle_wake.
        self.wake_now_entity = a("wake_now_entity", "input_button.wake_up_now")
        if self.wake_now_entity:
            self.listen_state(self._on_wake_now, self.wake_now_entity)

        self.log(f"MorningBriefing started (window {self.from_hour}-{self.until_hour}, "
                 f"fallback {self.fallback_workday} workdays / {self.fallback_weekend} "
                 f"weekends, sent_date={self._sent_date})", level="INFO")

    # ---------- state ----------
    def _load_state(self):
        try:
            with open(self.state_file) as f:
                d = json.load(f)
            self._sent_date = d.get("sent_date")
        except Exception:
            pass

    def _save_state(self):
        try:
            with open(self.state_file, "w") as f:
                json.dump({"sent_date": self._sent_date}, f)
        except Exception as e:
            self.log(f"state save failed ({e}) -- continuing in-memory", level="WARNING")

    # ---------- small async helpers ----------
    async def _state(self, entity):
        try:
            return await self.get_state(entity)
        except Exception:
            return None

    async def _attrs(self, entity):
        """All attributes for `entity` as a plain dict, or {} on any failure/missing entity."""
        try:
            full = await self.get_state(entity, attribute="all")
            if not isinstance(full, dict):
                return {}
            return dict(full.get("attributes") or {})
        except Exception:
            return {}

    # ---------- alarm scheduling ----------
    @staticmethod
    def _parse_hms(tstr):
        """'HH:MM[:SS]' -> datetime.time, or None on any missing/malformed input."""
        if not tstr:
            return None
        try:
            hh, mm, *ss = [int(x) for x in str(tstr).split(":")]
            return time(hh, mm, ss[0] if ss else 0)
        except Exception:
            return None

    def _schedule_alarm_run(self):
        """(Re)schedule the daily alarm-time run -- called at init and whenever
        alarm_time_entity changes. Cancel/reschedule safely: only cancel a timer AppDaemon
        still considers running (same guard wakeup_bedroom.py's _schedule_daily_alarm uses),
        to avoid an 'Invalid callback handle' warning on a timer that already fired or was
        never set."""
        if self._alarm_timer is not None:
            try:
                if self.timer_running(self._alarm_timer):
                    self.cancel_timer(self._alarm_timer)
            except Exception:
                pass
            self._alarm_timer = None
        sched = self._parse_hms(self.get_state(self.alarm_time_entity))
        if sched is None:
            self.log(f"{self.alarm_time_entity} has no valid time yet -- alarm run not "
                     f"scheduled (fallback at {self.fallback_time} still covers today)",
                     level="WARNING")
            return
        self._alarm_timer = self.run_daily(self._on_alarm_fire, sched)
        self.log(f"Morning briefing alarm run scheduled at {sched}", level="INFO")

    def _on_alarm_time_changed(self, entity, attribute, old, new, kwargs):
        self._schedule_alarm_run()

    def _alarm_pending_today(self):
        """True when the alarm is enabled and its configured time hasn't happened yet
        today. Used ONLY by the fallback run (_on_fallback_fire) so it never pre-empts a
        still-pending alarm -- e.g. fallback_time 07:00 with the alarm enabled for 08:00
        must wait for the alarm, not fire early with the wrong (fallback) advice."""
        if self.get_state(self.alarm_enabled_entity) != "on":
            return False
        sched = self._parse_hms(self.get_state(self.alarm_time_entity))
        if sched is None:
            return False
        return self.datetime().time() < sched

    # ---------- trigger ----------
    def _fire_wake(self, source):
        """Sync trigger -> async handler: every trigger path (alarm, fallback, alarm
        disabled mid-window) funnels through the same asyncio.Lock-guarded _handle_wake(),
        so the once-per-day/home/data gates never need duplicating (see module docstring)."""
        self.log(f"Wake trigger: {source}", level="DEBUG")
        self.create_task(self._handle_wake())

    def _on_wake_now(self, entity, attribute, old, new, kwargs):
        if new in (None, "unknown", "unavailable"):
            return
        self._fire_wake("wake up now button")

    def _on_alarm_fire(self, kwargs):
        self._fire_wake("alarm time")

    def _on_fallback_fire(self, kwargs):
        # Each fallback run only owns its own day-type (workday run is silent on
        # weekends and vice versa); with no day_type it covers every day.
        day_type = (kwargs or {}).get("day_type")
        is_weekend = self.datetime().weekday() >= 5
        if day_type == "workday" and is_weekend:
            return
        if day_type == "weekend" and not is_weekend:
            return
        if self._alarm_pending_today():
            self.log("Fallback fire skipped -- alarm is enabled and still pending today",
                     level="DEBUG")
            return
        self._fire_wake("fallback time")

    def _on_alarm_enabled_change(self, entity, attribute, old, new, kwargs):
        now = self.datetime()
        if not (self.from_hour <= now.hour < self.until_hour):
            self.log(f"Alarm disabled outside the {self.from_hour}-{self.until_hour} window "
                     f"(hour={now.hour}) -- not sending instantly", level="DEBUG")
            return
        self._fire_wake("alarm disabled mid-window")

    async def _handle_wake(self):
        async with self._wake_lock:
            await self._handle_wake_locked()

    async def _handle_wake_locked(self):
        try:
            now = (await self.get_now()).replace(tzinfo=None)

            # Gate 1: hour window.
            if not (self.from_hour <= now.hour < self.until_hour):
                self.log(f"Wake trigger outside the {self.from_hour}-{self.until_hour} window "
                         f"(hour={now.hour}) -- ignoring", level="DEBUG")
                return

            # Gate 2: once per day. Re-checked HERE, inside _wake_lock, so two edges firing
            # within the same tick can't both pass before either marks the day sent.
            today = now.strftime("%Y-%m-%d")
            if self._sent_date == today:
                self.log("Morning briefing already sent today -- ignoring", level="DEBUG")
                return

            # Gate 3: home. Suppress ONLY on a live "not_home" reading -- a dead/unknown/
            # unavailable sensor is not evidence of being away.
            person = await self._state(self.person_entity)
            if person == "not_home":
                self.log(f"{self.person_entity} is not_home -- skipping the morning briefing",
                         level="DEBUG")
                return

            # Gate 4: data. Missing/unknown plan or empty attributes -> the plan hasn't
            # published yet (e.g. right after an HA restart); leave the day UNMARKED so
            # the next wake edge retries once it's up.
            plan_state = await self._state(self.sleep_plan_entity)
            plan_attrs = await self._attrs(self.sleep_plan_entity)
            if plan_state in (None, "unknown", "unavailable") or not plan_attrs:
                self.log(f"{self.sleep_plan_entity} missing/unknown or has no attributes yet -- "
                         f"skipping (will retry on the next wake trigger)", level="WARNING")
                return

            status_attrs = await self._attrs(self.status_entity)
            climate_state = await self._state(self.climate_entity)
            ac_deployed = climate_state not in (None, "unavailable", "unknown")
            armed = (await self._state(self.enable_entity)) == "on"

            # Tomorrow's night from DeployAdvisor's published projection. over_ceiling is
            # a bool in the source but may arrive stringified -- accept both. Any failure
            # just means no lead-time line (the projection is advisory garnish here).
            tomorrow_needs_ac = False
            try:
                proj = await self._attrs(self.projection_entity)
                tomorrow_iso = (now.date() + timedelta(days=1)).isoformat()
                tomorrow_needs_ac = any(
                    n.get("date") == tomorrow_iso and n.get("over_ceiling") in (True, "true")
                    for n in (proj.get("nights") or []) if isinstance(n, dict))
            except Exception:
                pass

            title, message = compose_briefing(plan_state, plan_attrs, status_attrs,
                                              ac_deployed, armed,
                                              tomorrow_needs_ac=tomorrow_needs_ac)

            if not await self._notify(title, message):
                return

            self._sent_date = today
            self._save_state()
            self.log(f"Morning briefing sent -- {title}: {message}", level="INFO")
        except Exception as e:
            self.log(f"morning briefing handler failed ({e})", level="WARNING")

    # ---------- notify ----------
    def _notify_data(self):
        """Companion-app extras for the push (tag/channel/icon/clickAction -- see the
        initialize comment). Empty knobs are dropped; returns None when nothing is set so
        MobileNotifier's data handling is skipped entirely."""
        extras = {k: v for k, v in (("tag", self.notify_tag),
                                    ("channel", self.notify_channel),
                                    ("notification_icon", self.notify_icon),
                                    ("clickAction", self.click_url)) if v}
        return {"data": extras} if extras else None

    async def _notify(self, title, message):
        """Send via MobileNotifier; True on success. WARNING-logs and returns False on any
        failure (notifier unavailable, HA call raises) -- the caller must NOT mark the day
        as sent when this returns False, so a later wake trigger retries."""
        if not self.mobile_notifier:
            self.log("MobileNotifier not available -- cannot send the morning briefing",
                     level="WARNING")
            return False
        try:
            await self.mobile_notifier.notify(title=title, message=message,
                                              target=self.notify_target,
                                              data=self._notify_data())
            return True
        except Exception as e:
            self.log(f"notify failed ({e}) -- morning briefing not sent", level="WARNING")
            return False
