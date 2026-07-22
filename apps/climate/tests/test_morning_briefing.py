from __future__ import annotations

import asyncio
import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

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

    def test_title_is_always_morning_climate(self):
        title, _ = mb.compose_briefing("nothing", _attrs(), {}, False, False)
        self.assertEqual(title, "Morning climate")

    def test_windows_not_deployed(self):
        _, message = mb.compose_briefing("windows", _attrs(), {}, False, False)
        self.assertIn("Window night", message)
        self.assertIn("open windows do the cooling for free", message)
        self.assertNotIn("still plugged in", message)

    def test_windows_deployed_adds_stow_hint(self):
        _, message = mb.compose_briefing("windows", _attrs(), {}, True, False)
        self.assertIn("still plugged in", message)
        self.assertIn("you can stow it", message)

    def test_windows_deployed_armed_state_does_not_change_wording(self):
        # armed is irrelevant to the windows branch -- only ac_deployed matters.
        _, msg_unarmed = mb.compose_briefing("windows", _attrs(), {}, True, False)
        _, msg_armed = mb.compose_briefing("windows", _attrs(), {}, True, True)
        self.assertEqual(msg_unarmed, msg_armed)

    def test_nothing(self):
        _, message = mb.compose_briefing("nothing", _attrs(comfort_limit=22.5), {}, False, False)
        self.assertIn("Nothing needed tonight", message)
        self.assertIn("22.5C", message)

    def test_ac_not_deployed_asks_to_deploy_and_arm(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, False, False)
        self.assertIn("Deploy + arm the AC", message)
        self.assertIn("~1.8 kr", message)
        self.assertIn("25.0C", message)
        self.assertIn("23.0C", message)

    def test_ac_armed_but_not_deployed_still_asks_to_deploy_and_arm(self):
        # an unusual state (armed with nothing plugged in) -- ac_deployed is what gates
        # the two override sentences, so this still falls through to the default.
        _, message = mb.compose_briefing("ac", _attrs(), {}, False, True)
        self.assertIn("Deploy + arm the AC", message)

    def test_ac_deployed_but_not_armed(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, True, False)
        self.assertIn("just arm Cool night", message)
        self.assertNotIn("Deploy + arm", message)

    def test_ac_deployed_and_armed(self):
        _, message = mb.compose_briefing("ac", _attrs(), {}, True, True)
        self.assertIn("deployed and armed", message)
        self.assertIn("~1.8 kr", message)
        self.assertNotIn("Deploy + arm", message)
        self.assertNotIn("just arm Cool night", message)

    def test_hybrid_not_deployed_uses_same_branch_as_ac(self):
        _, message = mb.compose_briefing("hybrid", _attrs(), {}, False, False)
        self.assertIn("Deploy + arm the AC", message)

    def test_hybrid_deployed_and_armed(self):
        _, message = mb.compose_briefing("hybrid", _attrs(), {}, True, True)
        self.assertIn("deployed and armed", message)

    def test_unknown_recommendation_falls_back_to_headline_and_detail_first_sentence(self):
        _, message = mb.compose_briefing(
            "weird_future_value",
            _attrs(headline="Comfortable as-is",
                  detail="Projected peak 22.0C stays under the limit. Extra detail sentence."),
            {}, False, False)
        self.assertIn("Comfortable as-is", message)
        self.assertIn("Projected peak 22.0C stays under the limit", message)
        self.assertNotIn("Extra detail sentence", message)

    def test_unknown_recommendation_missing_headline_and_detail_does_not_raise(self):
        title, message = mb.compose_briefing("weird_future_value", {}, {}, False, False)
        self.assertEqual(title, "Morning climate")
        self.assertEqual(message, "")


class ComposeBriefingDayAheadLead(unittest.TestCase):
    def test_both_present_adds_lead(self):
        _, message = mb.compose_briefing(
            "nothing", _attrs(), {"kitchen_max_pred": 27.6, "outdoor_max_est": 31.2}, False, False)
        self.assertIn("Day ahead: ~28C inside, ~31C out.", message)

    def test_missing_outdoor_omits_lead(self):
        _, message = mb.compose_briefing(
            "nothing", _attrs(), {"kitchen_max_pred": 27.6}, False, False)
        self.assertNotIn("Day ahead", message)

    def test_missing_kitchen_omits_lead(self):
        _, message = mb.compose_briefing(
            "nothing", _attrs(), {"outdoor_max_est": 31.2}, False, False)
        self.assertNotIn("Day ahead", message)

    def test_empty_status_attrs_omits_lead(self):
        _, message = mb.compose_briefing("nothing", _attrs(), {}, False, False)
        self.assertNotIn("Day ahead", message)

    def test_none_status_attrs_does_not_raise(self):
        _, message = mb.compose_briefing("nothing", _attrs(), None, False, False)
        self.assertNotIn("Day ahead", message)


class ComposeBriefingOpenWindows(unittest.TestCase):
    def test_open_windows_appended(self):
        attrs = _attrs(open_windows=["bedroom", "bathroom"],
                       windows_summary="bedroom + bathroom open")
        _, message = mb.compose_briefing("windows", attrs, {}, False, False)
        self.assertIn("Open now: bedroom + bathroom open.", message)

    def test_no_open_windows_omits_line(self):
        _, message = mb.compose_briefing("windows", _attrs(), {}, False, False)
        self.assertNotIn("Open now", message)

    def test_open_windows_appended_regardless_of_recommendation(self):
        attrs = _attrs(open_windows=["kitchen"], windows_summary="kitchen open")
        _, message = mb.compose_briefing("ac", attrs, {}, True, True)
        self.assertIn("Open now: kitchen open.", message)


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
            async def notify(self, title, message, target):
                calls.append((title, message, target))
        app.mobile_notifier = FakeNotifier()
        ok = asyncio.run(app._notify("Morning climate", "msg"))
        self.assertTrue(ok)
        self.assertEqual(calls, [("Morning climate", "msg", "user")])

    def test_notifier_raises_returns_false(self):
        app = self._app()

        class BoomNotifier:
            async def notify(self, **kwargs):
                raise RuntimeError("boom")
        app.mobile_notifier = BoomNotifier()
        ok = asyncio.run(app._notify("Morning climate", "msg"))
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
