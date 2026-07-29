from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime, time
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import morning_briefing as mb  # noqa: E402


PLAN_ATTRS = {
    "headline": "Run the AC ~1.8 kr",
    "detail": ("Projected peak 25.0C is 2.0C over the 23.0C limit and it's not cool enough "
              "outside to open a window -- pre-cool with the AC (~1.8 kr)."),
    "projected_peak": 25.0,
    "comfort_limit": 23.0,
    "cost_label": "~1.8 kr",
    "open_windows": [],
    "windows_summary": "all closed",
    "recommendation": "ac",
}


def _attrs(**overrides):
    d = dict(PLAN_ATTRS)
    d.update(overrides)
    return d


class ComposeBriefingRecommendationBranches(unittest.TestCase):
    """Message rules per recommendation, per the deployed/armed permutations that change
    the wording (see morning_briefing.compose_briefing docstring)."""

    def test_titles_are_decided_verdicts(self):
        # The title IS the instruction (user: "A/C not needed - Keep windows open") --
        # one advice, no hedging; a changed situation later gets a NEW advice via the
        # evening rescue. hybrid rounds toward ACTION (user 2026-07-29: forgetting to
        # deploy is the expensive failure), and the ac title names the one action the
        # current deploy/arm state actually needs.
        self.assertEqual(mb.compose_briefing("windows", _attrs(), {}, False, False)[0],
                         "AC not needed")
        self.assertEqual(mb.compose_briefing("hybrid", _attrs(), {}, False, False)[0],
                         "Set up the AC")
        self.assertEqual(mb.compose_briefing("nothing", _attrs(), {}, False, False)[0],
                         "Nothing to do")
        self.assertEqual(mb.compose_briefing("ac", _attrs(), {}, False, False)[0],
                         "Deploy the AC")
        self.assertEqual(mb.compose_briefing("ac", _attrs(), {}, True, False)[0],
                         "Arm the AC")
        self.assertEqual(mb.compose_briefing("ac", _attrs(), {}, True, True)[0],
                         "AC handles tonight")
        self.assertEqual(mb.compose_briefing("weird_future_value", _attrs(), {}, False, False)[0],
                         "Morning climate")

    def test_windows_not_deployed(self):
        _, message = mb.compose_briefing("windows", _attrs(), {}, False, False)
        self.assertEqual(message, "Keep windows open.")

    def test_windows_deployed_adds_stow_hint(self):
        _, message = mb.compose_briefing("windows", _attrs(), {}, True, False)
        self.assertEqual(message, "Keep windows open. You can stow the AC.")

    def test_windows_deployed_armed_state_does_not_change_wording(self):
        # armed is irrelevant to the windows branch -- only ac_deployed matters.
        _, msg_unarmed = mb.compose_briefing("windows", _attrs(), {}, True, False)
        _, msg_armed = mb.compose_briefing("windows", _attrs(), {}, True, True)
        self.assertEqual(msg_unarmed, msg_armed)

    def test_nothing(self):
        _, message = mb.compose_briefing("nothing", _attrs(comfort_limit=22.5), {}, False, False)
        self.assertEqual(message, "The bedroom stays cool on its own.")

    def test_ac_not_deployed_bare_instruction_with_cost(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, False, False)
        self.assertEqual(message, "Before you leave. About 1.8 kr.")

    def test_ac_armed_but_not_deployed_still_the_deploy_instruction(self):
        # an unusual state (armed with nothing plugged in) -- ac_deployed is what picks
        # the verdict, so this still reads as the deploy instruction.
        title, message = mb.compose_briefing("ac", _attrs(), {}, False, True)
        self.assertEqual(title, "Deploy the AC")
        self.assertEqual(message, "Before you leave. About 1.8 kr.")

    def test_ac_deployed_but_not_armed(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, True, False)
        self.assertEqual(message, "Just arm Cool night.")

    def test_ac_deployed_and_armed(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, True, True)
        self.assertEqual(message, "Already armed. About 1.8 kr.")

    def test_ac_without_cost_still_reads_clean(self):
        _, message = mb.compose_briefing("ac", _attrs(cost_label=None, projected_peak=None),
                                         {}, False, False)
        self.assertEqual(message, "Before you leave.")
        _, message = mb.compose_briefing("ac", _attrs(cost_label=None), {}, True, True)
        self.assertEqual(message, "Already armed.")

    def test_hybrid_not_deployed_instructs_deploying_with_cost(self):
        # user 2026-07-29: hybrid rounds toward ACTION -- forgetting to deploy the AC before
        # a hybrid night is the expensive failure, deploying unnecessarily costs two
        # minutes. Not deployed -> the same "before you leave" urgency + cost clause style
        # as the ac branch.
        title, message = mb.compose_briefing("hybrid", _attrs(), {}, False, False)
        self.assertEqual(title, "Set up the AC")
        self.assertEqual(message, "Windows may not be enough tonight. Put it up before "
                                  "you leave. About 1.8 kr.")

    def test_hybrid_not_deployed_without_cost_still_reads_clean(self):
        _, message = mb.compose_briefing("hybrid", _attrs(cost_label=None), {}, False, False)
        self.assertEqual(message, "Windows may not be enough tonight. Put it up before you leave.")

    def test_hybrid_deployed_but_not_armed_offers_to_arm(self):
        _, message = mb.compose_briefing("hybrid", _attrs(), {}, True, False)
        self.assertEqual(message, "Windows may not be enough tonight. Arm it if you want "
                                  "the AC ready.")

    def test_hybrid_deployed_and_armed_acknowledges_instead_of_deploying(self):
        # Already deployed+armed -> acknowledge that instead of telling them to deploy
        # (there's nothing left to set up).
        _, message = mb.compose_briefing("hybrid", _attrs(), {}, True, True)
        self.assertEqual(message, "Windows may not be enough tonight. The AC is armed if needed.")

    def test_unknown_recommendation_falls_back_to_headline_only(self):
        _, message = mb.compose_briefing(
            "weird_future_value",
            _attrs(headline="Comfortable as-is",
                  detail="Projected peak 22.0C stays under the limit. Extra detail sentence."),
            {}, False, False)
        self.assertEqual(message, "Comfortable as-is.")

    def test_unknown_recommendation_missing_headline_and_detail_does_not_raise(self):
        title, message = mb.compose_briefing("weird_future_value", {}, {}, False, False)
        self.assertEqual(title, "Morning climate")
        self.assertEqual(message, "")


class ComposeBriefingTodayOnly(unittest.TestCase):
    def test_push_never_mentions_tomorrow(self):
        # user 2026-07-29: "I just need to know what to do today... Nothing about
        # tomorrow." The multi-day outlook lives on the card's day strip, never in the
        # sentence -- for ANY verdict or deploy/arm state.
        for rec in ("windows", "hybrid", "nothing", "ac"):
            for dep in (False, True):
                for armed in (False, True):
                    _, message = mb.compose_briefing(rec, _attrs(), {}, dep, armed)
                    self.assertNotIn("omorrow", message, f"{rec}/{dep}/{armed}")


class ComposeBriefingNoDayOutlook(unittest.TestCase):
    def test_status_attrs_are_ignored_entirely(self):
        # "Still too chatty" (user 2026-07-22): the day-outlook line was cut. status_attrs
        # stays in the signature for call-site stability but must never leak into the copy,
        # and a None value must not raise.
        for status in ({"kitchen_max_pred": 27.6, "outdoor_max_est": 31.2}, {}, None):
            _, message = mb.compose_briefing("nothing", _attrs(), status, False, False)
            self.assertEqual(message, "The bedroom stays cool on its own.")


class ComposeBriefingNoWindowInventory(unittest.TestCase):
    def test_open_windows_never_listed(self):
        # user 2026-07-22: the push is a decision, not a status report -- which windows
        # happen to be open is dashboard material, never notification material.
        attrs = _attrs(open_windows=["bedroom", "bathroom"],
                       windows_summary="bedroom + bathroom open")
        for rec in ("windows", "nothing", "hybrid", "ac"):
            _, message = mb.compose_briefing(rec, attrs, {}, False, False)
            self.assertNotIn("Open now", message)
            self.assertNotIn("bedroom + bathroom", message)


class NiceCost(unittest.TestCase):
    def test_tilde_label_becomes_prose(self):
        self.assertEqual(mb._nice_cost("~1.3 kr"), "about 1.3 kr")

    def test_non_cost_labels_are_dropped(self):
        for label in (None, "", "free", "cost unknown"):
            self.assertIsNone(mb._nice_cost(label))

    def test_plain_label_passes_through(self):
        self.assertEqual(mb._nice_cost("2 kr"), "2 kr")


# ---------------------------------------------------------------- app-level handler

def _make_app(now=datetime(2026, 7, 22, 6, 0), from_hour=5, until_hour=12,
             sent_date=None, person="home", plan_state="nothing",
             plan_attrs=None, status_attrs=None, climate="off", enable="off"):
    """MorningBriefing instance without running AppDaemon's initialize() -- _state/_attrs/
    get_now/_notify/_save_state are stubbed so the gate logic in _handle_wake_locked runs
    for real against plain dicts, mirroring test_smart_cooling.py's EveningRescue pattern."""
    app = mb.MorningBriefing.__new__(mb.MorningBriefing)
    app.from_hour = from_hour
    app.until_hour = until_hour
    app.person_entity = "person.mikkel"
    app.sleep_plan_entity = "sensor.sleep_plan"
    app.status_entity = "sensor.smart_cooling_status"
    app.climate_entity = "climate.air_conditioner_thermostat"
    app.enable_entity = "input_boolean.smart_cooling"
    app.notify_target = "user"
    app._sent_date = sent_date
    app._wake_lock = asyncio.Lock()
    app.log = lambda *a, **k: None

    app._save_calls = 0

    def _save_state():
        app._save_calls += 1
    app._save_state = _save_state

    # Mutable so a test can flip a value between two calls (data-gate retry scenario).
    app._states = {
        app.person_entity: person,
        app.climate_entity: climate,
        app.enable_entity: enable,
        app.sleep_plan_entity: plan_state,
    }
    app._attr_map = {
        app.sleep_plan_entity: dict(plan_attrs) if plan_attrs is not None else {},
        app.status_entity: dict(status_attrs) if status_attrs is not None else {},
    }

    async def _state(entity):
        return app._states.get(entity)
    app._state = _state

    async def _attrs_fn(entity):
        return app._attr_map.get(entity, {})
    app._attrs = _attrs_fn

    async def get_now():
        return now
    app.get_now = get_now

    app._notified = []

    async def _notify(title, message):
        app._notified.append((title, message))
        return True
    app._notify = _notify

    return app


def _run(app):
    asyncio.run(app._handle_wake())


class OncePerDayGuard(unittest.TestCase):
    def test_sends_once_then_suppresses_same_day(self):
        app = _make_app(plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)
        self.assertEqual(app._sent_date, "2026-07-22")
        self.assertEqual(app._save_calls, 1)

        _run(app)   # a second wake edge later the same morning
        self.assertEqual(len(app._notified), 1)
        self.assertEqual(app._save_calls, 1)

    def test_already_sent_today_is_ignored(self):
        app = _make_app(sent_date="2026-07-22", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])

    def test_new_calendar_day_resets_the_gate(self):
        app = _make_app(sent_date="2026-07-21", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)
        self.assertEqual(app._sent_date, "2026-07-22")

    def test_concurrent_wake_edges_send_only_once(self):
        # motion + a bed-exit firing within the same tick both schedule _handle_wake();
        # the lock + in-lock date re-check must let only one through.
        app = _make_app(plan_attrs=PLAN_ATTRS)

        async def fire_both():
            await asyncio.gather(app._handle_wake(), app._handle_wake())
        asyncio.run(fire_both())
        self.assertEqual(len(app._notified), 1)
        self.assertEqual(app._save_calls, 1)


class HourWindowGate(unittest.TestCase):
    def test_before_from_hour_is_ignored(self):
        app = _make_app(now=datetime(2026, 7, 22, 4, 59), plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_at_from_hour_boundary_fires(self):
        app = _make_app(now=datetime(2026, 7, 22, 5, 0), plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)

    def test_just_before_until_hour_fires(self):
        app = _make_app(now=datetime(2026, 7, 22, 11, 59), plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)

    def test_at_until_hour_boundary_is_ignored(self):
        app = _make_app(now=datetime(2026, 7, 22, 12, 0), plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_after_until_hour_is_ignored(self):
        app = _make_app(now=datetime(2026, 7, 22, 18, 0), plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])

    def test_respects_configured_window(self):
        app = _make_app(now=datetime(2026, 7, 22, 13, 0), from_hour=6, until_hour=14,
                        plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)


class HomeGate(unittest.TestCase):
    def test_live_not_home_suppresses(self):
        app = _make_app(person="not_home", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_missing_unknown_unavailable_person_does_not_suppress(self):
        for state in (None, "unknown", "unavailable"):
            app = _make_app(person=state, plan_attrs=PLAN_ATTRS)
            _run(app)
            self.assertEqual(len(app._notified), 1, f"person={state!r} should not suppress")

    def test_home_does_not_suppress(self):
        app = _make_app(person="home", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(len(app._notified), 1)


class DataGate(unittest.TestCase):
    def test_missing_plan_state_does_not_mark_sent(self):
        app = _make_app(plan_state=None, plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)
        self.assertEqual(app._save_calls, 0)

    def test_unknown_plan_state_does_not_mark_sent(self):
        app = _make_app(plan_state="unknown", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_unavailable_plan_state_does_not_mark_sent(self):
        app = _make_app(plan_state="unavailable", plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_empty_plan_attrs_does_not_mark_sent(self):
        app = _make_app(plan_state="nothing", plan_attrs={})
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

    def test_failure_does_not_consume_the_day_retry_succeeds(self):
        app = _make_app(plan_state=None, plan_attrs=PLAN_ATTRS)
        _run(app)
        self.assertEqual(app._notified, [])
        self.assertIsNone(app._sent_date)

        # the plan publishes later the same morning -- a later wake edge retries
        app._states[app.sleep_plan_entity] = "nothing"
        _run(app)
        self.assertEqual(len(app._notified), 1)
        self.assertEqual(app._sent_date, "2026-07-22")


class NotifierUnavailable(unittest.TestCase):
    def test_notify_failure_does_not_mark_sent(self):
        app = _make_app(plan_attrs=PLAN_ATTRS)

        async def _notify_fail(title, message):
            return False
        app._notify = _notify_fail

        _run(app)
        self.assertIsNone(app._sent_date)
        self.assertEqual(app._save_calls, 0)


class NotifyMethod(unittest.TestCase):
    """MorningBriefing._notify's own gating -- exercised directly rather than via a stub,
    since HourWindowGate/HomeGate/DataGate/OncePerDayGuard all stub it out."""

    def _app(self):
        app = mb.MorningBriefing.__new__(mb.MorningBriefing)
        app.notify_target = "user"
        app.notify_tag = "morning_briefing"
        app.notify_channel = "Morning climate"
        app.notify_icon = "mdi:bed-clock"
        app.click_url = "/local/ha-dashboard/index.html"
        app.log = lambda *a, **k: None
        return app

    def test_no_notifier_returns_false(self):
        app = self._app()
        app.mobile_notifier = None
        ok = asyncio.run(app._notify("Morning climate", "msg"))
        self.assertFalse(ok)

    def test_notifier_success_returns_true_and_forwards_args(self):
        app = self._app()
        calls = []

        class FakeNotifier:
            async def notify(self, title, message, target, data=None):
                calls.append((title, message, target, data))
        app.mobile_notifier = FakeNotifier()
        ok = asyncio.run(app._notify("Morning climate", "msg"))
        self.assertTrue(ok)
        self.assertEqual(calls, [("Morning climate", "msg", "user",
                                  {"data": {"tag": "morning_briefing",
                                            "channel": "Morning climate",
                                            "notification_icon": "mdi:bed-clock",
                                            "clickAction": "/local/ha-dashboard/index.html"}})])

    def test_empty_polish_knobs_send_no_data_payload(self):
        # All four extras disabled ("" in yaml) -> data must be None, not {"data": {}},
        # so MobileNotifier's data handling is skipped entirely.
        app = self._app()
        app.notify_tag = app.notify_channel = app.notify_icon = app.click_url = ""
        calls = []

        class FakeNotifier:
            async def notify(self, title, message, target, data=None):
                calls.append(data)
        app.mobile_notifier = FakeNotifier()
        self.assertTrue(asyncio.run(app._notify("t", "m")))
        self.assertEqual(calls, [None])

    def test_notifier_raises_returns_false(self):
        app = self._app()

        class BoomNotifier:
            async def notify(self, **kwargs):
                raise RuntimeError("boom")
        app.mobile_notifier = BoomNotifier()
        ok = asyncio.run(app._notify("Morning climate", "msg"))
        self.assertFalse(ok)


# ---------------------------------------------------------------- alarm-based trigger (2026-07-29)

def _sched_app(now, states, *, alarm_timer=None, from_hour=5, until_hour=12,
              fallback_time="07:00:00"):
    """MorningBriefing instance for the SYNC alarm-scheduling side (_schedule_alarm_run /
    _alarm_pending_today / _on_fallback_fire / _on_alarm_fire / _on_alarm_enabled_change) --
    distinct from _make_app (which exercises the async _handle_wake_locked gate chain).
    Same __new__ + monkeypatched-callables trick as
    apps/rutines/tests/test_wakeup_restart_survival.py's make_app(). states maps entity_id
    -> state string (get_state has no attribute lookups here, so kw is ignored)."""
    app = mb.MorningBriefing.__new__(mb.MorningBriefing)
    app.alarm_time_entity = "input_datetime.wakeup_bedroom"
    app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
    app.fallback_time = fallback_time
    app.from_hour = from_hour
    app.until_hour = until_hour
    app._alarm_timer = alarm_timer
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: states.get(entity)
    app.datetime = lambda: now
    app.timer_running = lambda handle: handle is not None
    app.cancel_timer = MagicMock()
    app.run_daily = MagicMock(return_value="daily-handle")
    # _fire_wake is stubbed directly (rather than create_task/_handle_wake) so these tests
    # assert the SCHEDULING/gating decision -- which trigger source fires, if any -- without
    # touching asyncio at all; _handle_wake_locked's own gates are covered by
    # OncePerDayGuard/HourWindowGate/HomeGate/DataGate above.
    app.woken = []
    app._fire_wake = lambda source: app.woken.append(source)
    return app


def _logged(app, needle):
    return any(needle in str(a) for a, kw in app.log_calls)


class ParseHms(unittest.TestCase):
    def test_hh_mm_ss(self):
        self.assertEqual(mb.MorningBriefing._parse_hms("08:15:30"), time(8, 15, 30))

    def test_hh_mm_defaults_seconds_to_zero(self):
        self.assertEqual(mb.MorningBriefing._parse_hms("08:15"), time(8, 15, 0))

    def test_none_empty_and_malformed_return_none(self):
        for bad in (None, "", "not-a-time", "08"):
            self.assertIsNone(mb.MorningBriefing._parse_hms(bad))


class AlarmPendingToday(unittest.TestCase):
    """_alarm_pending_today: the fallback run's sole pre-empt check."""

    NOW = datetime(2026, 7, 29, 7, 0)

    def test_enabled_and_time_in_the_future_is_pending(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "on",
                                    "input_datetime.wakeup_bedroom": "08:00:00"})
        self.assertTrue(app._alarm_pending_today())

    def test_enabled_but_time_already_passed_is_not_pending(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "on",
                                    "input_datetime.wakeup_bedroom": "06:00:00"})
        self.assertFalse(app._alarm_pending_today())

    def test_disabled_is_never_pending_regardless_of_time(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "off",
                                    "input_datetime.wakeup_bedroom": "08:00:00"})
        self.assertFalse(app._alarm_pending_today())

    def test_enabled_but_no_valid_time_is_not_pending(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "on",
                                    "input_datetime.wakeup_bedroom": "unknown"})
        self.assertFalse(app._alarm_pending_today())


class FallbackDoesNotPreemptPendingAlarm(unittest.TestCase):
    """The fallback run (_on_fallback_fire) must defer to a still-pending alarm -- e.g.
    fallback_time 07:00 with the alarm enabled for 08:00 must NOT send at 07:00 with the
    (wrong) fallback advice; the alarm's own run_daily handles 08:00 instead."""

    NOW = datetime(2026, 7, 29, 7, 0)

    def test_pending_alarm_defers_and_does_not_fire(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "on",
                                    "input_datetime.wakeup_bedroom": "08:00:00"})
        app._on_fallback_fire({})
        self.assertEqual(app.woken, [])
        self.assertTrue(_logged(app, "pending"))

    def test_alarm_disabled_fires_the_fallback(self):
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "off",
                                    "input_datetime.wakeup_bedroom": "08:00:00"})
        app._on_fallback_fire({})
        self.assertEqual(app.woken, ["fallback time"])

    def test_alarm_enabled_but_already_passed_still_fires_as_a_safety_net(self):
        # the alarm's own run_daily should have already handled 06:00 (and _handle_wake_
        # locked's once-per-day gate makes a redundant fallback attempt a harmless no-op) --
        # but if it somehow didn't (e.g. a reload landed in between), the fallback must not
        # stay silent forever just because the alarm is enabled.
        app = _sched_app(self.NOW, {"input_boolean.wakeup_bedroom": "on",
                                    "input_datetime.wakeup_bedroom": "06:00:00"})
        app._on_fallback_fire({})
        self.assertEqual(app.woken, ["fallback time"])


class AlarmDisabledInstantPath(unittest.TestCase):
    """Alarm turned off DURING the morning window (woke early / cancelled) -> send right
    now; disabling it outside the window (e.g. the night before) must NOT send."""

    def _fire(self, now, from_hour=5, until_hour=12):
        app = _sched_app(now, {}, from_hour=from_hour, until_hour=until_hour)
        app._on_alarm_enabled_change("input_boolean.wakeup_bedroom", None, "on", "off", {})
        return app

    def test_inside_the_window_fires_immediately(self):
        app = self._fire(datetime(2026, 7, 29, 6, 30))
        self.assertEqual(app.woken, ["alarm disabled mid-window"])

    def test_at_from_hour_boundary_fires(self):
        app = self._fire(datetime(2026, 7, 29, 5, 0))
        self.assertEqual(app.woken, ["alarm disabled mid-window"])

    def test_before_from_hour_does_not_fire(self):
        # e.g. cancelled the night before at 23:00 -- must not send at that moment.
        app = self._fire(datetime(2026, 7, 28, 23, 0))
        self.assertEqual(app.woken, [])

    def test_at_until_hour_boundary_does_not_fire(self):
        app = self._fire(datetime(2026, 7, 29, 12, 0))
        self.assertEqual(app.woken, [])

    def test_after_until_hour_does_not_fire(self):
        app = self._fire(datetime(2026, 7, 29, 18, 0))
        self.assertEqual(app.woken, [])


class AlarmTimeReschedule(unittest.TestCase):
    """_schedule_alarm_run (called at init and on every alarm_time_entity change via
    _on_alarm_time_changed) safely cancels any running timer and reschedules a fresh
    run_daily at the new time -- same cancel/reschedule guard wakeup_bedroom.py uses."""

    NOW = datetime(2026, 7, 29, 6, 0)

    def test_schedules_run_daily_at_the_configured_time(self):
        app = _sched_app(self.NOW, {"input_datetime.wakeup_bedroom": "08:00:00"})
        app._schedule_alarm_run()
        app.run_daily.assert_called_once_with(app._on_alarm_fire, time(8, 0, 0))
        self.assertEqual(app._alarm_timer, "daily-handle")

    def test_reschedule_cancels_a_running_previous_timer(self):
        app = _sched_app(self.NOW, {"input_datetime.wakeup_bedroom": "08:00:00"},
                        alarm_timer="old-handle")
        app._schedule_alarm_run()
        app.cancel_timer.assert_called_once_with("old-handle")
        app.run_daily.assert_called_once_with(app._on_alarm_fire, time(8, 0, 0))

    def test_reschedule_does_not_cancel_an_already_expired_timer(self):
        # timer_running() False -> the old handle is stale; cancelling it would only log an
        # AppDaemon "Invalid callback handle" warning (see wakeup_bedroom.py's own guard).
        app = _sched_app(self.NOW, {"input_datetime.wakeup_bedroom": "08:00:00"},
                        alarm_timer="stale-handle")
        app.timer_running = lambda handle: False
        app._schedule_alarm_run()
        app.cancel_timer.assert_not_called()

    def test_on_alarm_time_changed_reschedules(self):
        app = _sched_app(self.NOW, {"input_datetime.wakeup_bedroom": "08:00:00"},
                        alarm_timer="old-handle")
        app._on_alarm_time_changed("input_datetime.wakeup_bedroom", None,
                                   "07:00:00", "08:00:00", {})
        app.cancel_timer.assert_called_once_with("old-handle")
        app.run_daily.assert_called_once_with(app._on_alarm_fire, time(8, 0, 0))

    def test_no_valid_time_yet_does_not_schedule_and_does_not_raise(self):
        app = _sched_app(self.NOW, {"input_datetime.wakeup_bedroom": "unknown"})
        app._schedule_alarm_run()
        app.run_daily.assert_not_called()
        self.assertIsNone(app._alarm_timer)
        self.assertTrue(_logged(app, "no valid time"))


class FallbackDayTyping(unittest.TestCase):
    """The workday fallback run is silent on weekends and vice versa (user 2026-07-29:
    07:00 workdays / 09:00 weekends)."""

    def _app(self, weekday):
        import datetime as _dt
        app = mb.MorningBriefing.__new__(mb.MorningBriefing)
        app.log = lambda *a, **k: None
        app.datetime = lambda: _dt.datetime(2026, 8, 3 + (weekday - 0), 7, 0)  # Mon=0 base
        app._fired = []
        app._fire_wake = lambda source: app._fired.append(source)
        app._alarm_pending_today = lambda: False
        return app

    def test_workday_run_fires_on_monday(self):
        app = self._app(weekday=0)      # 2026-08-03 is a Monday
        app._on_fallback_fire({"day_type": "workday"})
        self.assertEqual(len(app._fired), 1)

    def test_workday_run_silent_on_saturday(self):
        app = self._app(weekday=5)      # 2026-08-08 is a Saturday
        app._on_fallback_fire({"day_type": "workday"})
        self.assertEqual(app._fired, [])

    def test_weekend_run_fires_on_saturday(self):
        app = self._app(weekday=5)
        app._on_fallback_fire({"day_type": "weekend"})
        self.assertEqual(len(app._fired), 1)

    def test_weekend_run_silent_on_wednesday(self):
        app = self._app(weekday=2)      # 2026-08-05 is a Wednesday
        app._on_fallback_fire({"day_type": "weekend"})
        self.assertEqual(app._fired, [])

    def test_untyped_run_covers_any_day(self):
        for wd in (0, 5):
            app = self._app(weekday=wd)
            app._on_fallback_fire({})
            self.assertEqual(len(app._fired), 1, f"weekday={wd}")

    def test_still_defers_to_pending_alarm(self):
        app = self._app(weekday=0)
        app._alarm_pending_today = lambda: True
        app._on_fallback_fire({"day_type": "workday"})
        self.assertEqual(app._fired, [])


class WakeNowButton(unittest.TestCase):
    def test_press_fires_wake(self):
        app = mb.MorningBriefing.__new__(mb.MorningBriefing)
        fired = []
        app._fire_wake = lambda source: fired.append(source)
        app._on_wake_now("input_button.wake_up_now", None, "unknown",
                         "2026-07-29T16:30:00+00:00", {})
        self.assertEqual(fired, ["wake up now button"])

    def test_unavailable_states_ignored(self):
        app = mb.MorningBriefing.__new__(mb.MorningBriefing)
        fired = []
        app._fire_wake = lambda source: fired.append(source)
        for bad in (None, "unknown", "unavailable"):
            app._on_wake_now("input_button.wake_up_now", None, None, bad, {})
        self.assertEqual(fired, [])


if __name__ == "__main__":
    unittest.main()
