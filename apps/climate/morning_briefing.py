"""
Morning climate briefing -- ONE phone push, once a day, carrying the climate decision
SmartCooling has already computed: open/close windows, and whether to deploy + arm the
portable bedroom AC for tonight.

The user has ~1h at home after waking, before leaving for work, and the decision itself
is manual by design -- this app is purely advisory (see below). It composes the message
from what's already published (sensor.sleep_plan + sensor.smart_cooling_status); no new
projection logic lives here.

Wake signal = first of either, after from_hour:
  - motion_entity (binary_sensor.bedroom_pir_presence) transitions to "on", or
  - either bed_entities sensor transitions to "off" (bed-exit fallback).
motion_entity ORs raw motion with a separate occupancy signal, so it can already read
"on" all night while someone sleeps -- a pure off->on edge on it alone could never fire
on an ordinary morning. Getting OUT of bed is the wake edge that's always reliably
there, so both bed sensors are wired to the same handler too; whichever of the three
edges happens first after from_hour wins, and the once-per-day gate silences the rest.

Gates, checked in this order inside an asyncio.Lock (the sent-date is re-checked INSIDE
the lock so two near-simultaneous edges -- e.g. motion and a bed-exit within the same
second -- can't both slip past the once-per-day check and double-send):
  1. Local hour in [from_hour, until_hour) -- outside it, ignore silently.
  2. Once per day -- last-sent date persisted to state_file.
  3. Home (person_entity) -- suppress ONLY on a live "not_home" reading; a dead/unknown/
     unavailable sensor is never evidence of being away (same semantics as SmartCooling's
     rescue_home_entity gate).
  4. Data -- sensor.sleep_plan must have a real state and non-empty attributes. Missing/
     unknown data does NOT mark the day as sent, so a later wake edge (e.g. after an HA
     restart delays the plan's first publish) retries.

compose_briefing() is a pure function (plan/status data in, (title, message) out), so the
wording is directly unit-testable without any AppDaemon/HA plumbing.

Advisory only: this app NEVER calls a climate/switch/cover service. Its only outputs are
one notification and its own logs.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore
import asyncio
import json
from typing import Optional


def _nice_cost(cost_label):
    """plan_sleep's cost_label ('~1.3 kr') -> prose ('about 1.3 kr'); None for the labels
    that shouldn't produce a cost clause at all ('free', 'cost unknown', empty)."""
    if not cost_label or cost_label in ("free", "cost unknown"):
        return None
    if cost_label.startswith("~"):
        return "about " + cost_label[1:].strip()
    return cost_label


def compose_briefing(plan_state, plan_attrs, status_attrs, ac_deployed, armed):
    """Pure composer: sensor.sleep_plan's state+attributes, sensor.smart_cooling_status's
    attributes, and the AC's deploy/arm state -> one short push (title, message). No I/O,
    so every branch below is directly unit-testable.

    plan_state is the sleep-plan RECOMMENDATION ("windows"|"ac"|"hybrid"|"nothing" -- the
    sensor's own state string; see SmartCooling._publish_sleep_plan). An unrecognised
    value (defensive only -- plan_sleep never actually emits one) falls back to the
    plan's own headline, so a future recommendation type still produces a sane message
    instead of silence. status_attrs is accepted for call-site stability but currently
    unused -- the day-outlook line was cut ("still too chatty").
    """
    plan_attrs = dict(plan_attrs or {})
    # Copy style (user 2026-07-22, three rounds: "like Apple made it" -> "more decided"
    # -> "still too chatty"): title = the verdict, body = the bare instruction, nothing
    # else. No explanations, no day outlook, no numbers except the cost when money is
    # being asked for — every reason lives on the dashboard. One advice; if conditions
    # change later, the evening rescue issues the NEW advice. hybrid deliberately
    # collapses into the windows verdict (the AC-backup nuance is dashboard + rescue
    # material). status_attrs is accepted but unused since the day-outlook line was cut.
    title = "Morning climate"
    body = ""

    cost = _nice_cost(plan_attrs.get("cost_label"))

    if plan_state in ("windows", "hybrid"):
        title = "AC not needed"
        body = "Keep windows open."
        # Stow advice only on a confident windows night: on hybrid the unit may still be
        # wanted as backup by evening, so "pack it away" would be bad advice.
        if ac_deployed and plan_state == "windows":
            body += " You can stow the AC."
    elif plan_state == "nothing":
        title = "Nothing to do"
        body = "The bedroom stays cool on its own."
    elif plan_state == "ac":
        if ac_deployed and armed:
            title = "AC handles tonight"
            body = "Already armed." + (f" {cost[0].upper()}{cost[1:]}." if cost else "")
        elif ac_deployed:
            title = "Arm the AC"
            body = "Just arm Cool night."
        else:
            title = "Deploy the AC"
            body = "Before you leave." + (f" {cost[0].upper()}{cost[1:]}." if cost else "")
    else:
        headline = (plan_attrs.get("headline") or "").strip()
        body = f"{headline}." if headline else ""

    return title, body


class MorningBriefing(hass.Hass):
    def initialize(self) -> None:
        a = self.args.get
        # --- wake-trigger entities (see module docstring for the PIR/bed-exit rationale) ---
        self.motion_entity = a("motion_entity", "binary_sensor.bedroom_pir_presence")
        self.bed_entities = list(a("bed_entities",
                                  ["binary_sensor.left_bedside", "binary_sensor.right_bedside_in_bed"]))
        # --- gates ---
        self.person_entity = a("person_entity", "person.mikkel")
        self.from_hour = int(a("from_hour", 5))
        self.until_hour = int(a("until_hour", 12))
        # --- data sources (read-only; published by SmartCooling) ---
        self.sleep_plan_entity = a("sleep_plan_entity", "sensor.sleep_plan")
        self.status_entity = a("status_entity", "sensor.smart_cooling_status")
        self.climate_entity = a("climate_entity", "climate.air_conditioner_thermostat")
        self.enable_entity = a("enable_entity", "input_boolean.smart_cooling")
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
        # Serializes _handle_wake(): the three wake listeners can each schedule their own
        # create_task on near-simultaneous edges (e.g. motion + a bed-exit within the same
        # second); the once-per-day date is re-checked INSIDE the lock so only the first to
        # get there actually sends (see _handle_wake_locked).
        self._wake_lock = asyncio.Lock()

        # get_app must be resolved in the sync init context -- inside async methods
        # AppDaemon hands back a Task instead of the app instance.
        self.mobile_notifier = None
        try:
            self.mobile_notifier = self.get_app("MobileNotifier")
        except Exception as e:
            self.log(f"MobileNotifier not available: {e}", level="WARNING")

        self.listen_state(self._on_wake_trigger, self.motion_entity, new="on", source="motion")
        for ent in self.bed_entities:
            self.listen_state(self._on_wake_trigger, ent, new="off", source="bed_exit")

        self.log(f"MorningBriefing started (window {self.from_hour}-{self.until_hour}, "
                 f"sent_date={self._sent_date})", level="INFO")

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

    # ---------- trigger ----------
    def _on_wake_trigger(self, entity, attribute, old, new, kwargs):
        self.log(f"Wake trigger: {kwargs.get('source', entity)} ({entity} {old}->{new})",
                 level="DEBUG")
        self.create_task(self._handle_wake())

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

            title, message = compose_briefing(plan_state, plan_attrs, status_attrs,
                                              ac_deployed, armed)

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
