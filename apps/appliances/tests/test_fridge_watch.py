# tests/test_fridge_watch.py - FridgeWatch: run tracking with off_hold dip tolerance, ignored
# unavailable readings, one push per long run, restart recovery from the recorder history, and
# the published semantic entity.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import ast
import asyncio
import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import fridge_watch as fw  # noqa: E402

POWER = "sensor.fridge_plug_power"
HEALTH = "sensor.fridge_health"
T0 = datetime(2026, 10, 22, 12, 0, tzinfo=timezone.utc).timestamp()
MIN = 60

ARGS = {
    "power_sensor": POWER,
    "health_entity": HEALTH,
    "display_name": "Køleskab",
    "alert_minutes": 180,
}
WINE_ARGS = {
    "power_sensor": "sensor.wine_fridge_power",
    "health_entity": "sensor.wine_fridge_health",
    "display_name": "Vinkøleskab",
    "alert_minutes": 45,
}


def row(t, state):
    return {"state": str(state), "last_changed": datetime.fromtimestamp(t, timezone.utc).isoformat()}


def rows_off_then_on(off_min, on_min):
    """2 W at T0 - off_min, then 40 W every minute from T0 - on_min up to T0."""
    return [row(T0 - off_min * MIN, 2)] + [row(T0 - m * MIN, 40) for m in range(on_min, -1, -1)]


def make_app(args=None, states=None, history=None, now=T0, delivered=1):
    """FridgeWatch with initialize() actually run and AppDaemon's primitives stubbed.
    `history` is the recorder rows get_history returns, or an exception to raise."""
    app = fw.FridgeWatch.__new__(fw.FridgeWatch)
    app.args = dict(ARGS if args is None else args)
    app.states = dict(states or {})
    app.logs = []
    app.clock = {"now": now}
    app._now = lambda: app.clock["now"]
    app.get_state = lambda entity, attribute=None, **kw: app.states.get(entity)

    app.history_calls = []

    def get_history(**kw):
        app.history_calls.append(kw)
        if isinstance(history, Exception):
            raise history
        return [history] if history is not None else []

    app.get_history = get_history
    app.set_state = MagicMock()
    app.listen_state = MagicMock()
    app.run_every = MagicMock()
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.notifier = MagicMock()
    app.notifier.notify = AsyncMock(return_value=delivered)
    app.get_app = MagicMock(return_value=app.notifier)
    app.create_task = lambda coro: asyncio.run(coro)
    app.initialize()
    return app


def feed(app, t, value):
    app.clock["now"] = t
    app._on_power(app.power_sensor, "state", None, value, {})


def tick(app, t):
    app.clock["now"] = t
    app._tick({})


def run_for(app, start, minutes, watts=40):
    """A run: a reading at `start`, then a tick every minute until start + minutes."""
    feed(app, start, watts)
    for m in range(1, minutes + 1):
        tick(app, start + m * MIN)


def published(app):
    return [(c.kwargs["state"], c.kwargs["attributes"]) for c in app.set_state.call_args_list]


class RunTracking(unittest.TestCase):
    def test_reading_at_running_w_starts_a_run(self):
        app = make_app()
        feed(app, T0, 20)
        self.assertEqual(app._run_started, T0)

    def test_reading_below_running_w_starts_no_run(self):
        app = make_app()
        feed(app, T0, 19.9)
        self.assertIsNone(app._run_started)

    def test_dip_shorter_than_off_hold_does_not_split_the_run(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        feed(app, T0 + 160, 40)
        self.assertEqual(app._run_started, T0)
        self.assertIsNone(app._below_since)

    def test_dip_lasting_off_hold_ends_the_run(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        tick(app, T0 + 60 + 119)
        self.assertEqual(app._run_started, T0)
        tick(app, T0 + 60 + 120)
        self.assertIsNone(app._run_started)

    def test_reading_after_a_long_dip_starts_a_new_run_even_without_a_tick(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        feed(app, T0 + 600, 40)
        self.assertEqual(app._run_started, T0 + 600)

    def test_the_dip_is_measured_from_its_first_low_reading(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        feed(app, T0 + 100, 4)
        tick(app, T0 + 60 + 120)
        self.assertIsNone(app._run_started)


class UnavailableReadings(unittest.TestCase):
    def test_non_numeric_readings_start_no_run(self):
        for value in ("unavailable", "unknown", "", None, "abc", "nan", "inf"):
            with self.subTest(value=value):
                app = make_app()
                feed(app, T0, value)
                self.assertIsNone(app._run_started)
                self.assertIsNone(app._below_since)

    def test_unavailable_mid_run_neither_ends_the_run_nor_starts_a_dip(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, "unavailable")
        tick(app, T0 + 60 + 600)
        self.assertEqual(app._run_started, T0)
        self.assertIsNone(app._below_since)

    def test_unavailable_during_a_dip_leaves_the_hold_timer_alone(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        feed(app, T0 + 100, "unavailable")
        tick(app, T0 + 60 + 120)
        self.assertIsNone(app._run_started)

    def test_unavailable_after_the_run_ended_starts_nothing(self):
        app = make_app()
        feed(app, T0, 40)
        feed(app, T0 + 60, 5)
        tick(app, T0 + 60 + 120)
        feed(app, T0 + 400, "unknown")
        self.assertIsNone(app._run_started)


class AlertPush(unittest.TestCase):
    def test_no_push_before_the_threshold(self):
        app = make_app()
        run_for(app, T0, 179)
        app.notifier.notify.assert_not_awaited()
        self.assertEqual(published(app)[-1][0], "ok")

    def test_one_push_when_the_run_reaches_the_threshold(self):
        app = make_app()
        run_for(app, T0, 180)
        app.notifier.notify.assert_awaited_once_with(
            title="Køleskab kører uafbrudt",
            message="Kompressoren har kørt i 3 t. Står døren på klem?",
            target="mikkel",
            data={"tag": "fridge_health"},
        )
        self.assertEqual(published(app)[-1][0], "long_run")

    def test_message_names_how_long_the_run_has_lasted(self):
        app = make_app()
        feed(app, T0, 40)
        tick(app, T0 + 185 * MIN)
        self.assertEqual(
            app.notifier.notify.await_args.kwargs["message"],
            "Kompressoren har kørt i 3 t 5 min. Står døren på klem?",
        )

    def test_no_second_push_in_the_same_run(self):
        app = make_app()
        run_for(app, T0, 180)
        feed(app, T0 + 181 * MIN, 5)
        feed(app, T0 + 182 * MIN, 40)
        for m in range(183, 400):
            tick(app, T0 + m * MIN)
        self.assertEqual(app.notifier.notify.await_count, 1)

    def test_run_end_returns_to_ok_without_another_push(self):
        app = make_app()
        run_for(app, T0, 200)
        end = T0 + 200 * MIN
        feed(app, end, 5)
        tick(app, end + 120)
        self.assertIsNone(app._run_started)
        self.assertEqual(published(app)[-1][0], "ok")
        self.assertEqual(app.notifier.notify.await_count, 1)
        self.assertTrue(any("long run ended after 200 min" in m for _, m in app.logs))

    def test_a_new_long_run_after_the_end_pushes_again(self):
        app = make_app()
        run_for(app, T0, 200)
        end = T0 + 200 * MIN
        feed(app, end, 5)
        tick(app, end + 120)
        run_for(app, end + 3600, 180)
        self.assertEqual(app.notifier.notify.await_count, 2)

    def test_a_short_run_never_pushes_however_often_it_repeats(self):
        app = make_app()
        t = T0
        for _ in range(10):
            run_for(app, t, 60)
            feed(app, t + 61 * MIN, 5)
            tick(app, t + 63 * MIN)
            t += 90 * MIN
        app.notifier.notify.assert_not_awaited()

    def test_a_dip_in_progress_does_not_count_towards_the_threshold(self):
        app = make_app()
        run_for(app, T0, 179)
        feed(app, T0 + 179 * MIN + 30, 5)
        tick(app, T0 + 180 * MIN + 29)
        app.notifier.notify.assert_not_awaited()

    def test_wine_fridge_instance_uses_its_own_threshold_and_name(self):
        app = make_app(args=WINE_ARGS)
        run_for(app, T0, 44)
        app.notifier.notify.assert_not_awaited()
        tick(app, T0 + 45 * MIN)
        self.assertEqual(
            app.notifier.notify.await_args.kwargs,
            {
                "title": "Vinkøleskab kører uafbrudt",
                "message": "Kompressoren har kørt i 45 min. Står døren på klem?",
                "target": "mikkel",
                "data": {"tag": "wine_fridge_health"},
            },
        )


class PushRetry(unittest.TestCase):
    def test_a_push_that_reached_nobody_is_retried_after_the_retry_interval(self):
        app = make_app(delivered=0)
        run_for(app, T0, 180)
        self.assertEqual(app.notifier.notify.await_count, 1)
        for m in range(181, 190):
            tick(app, T0 + m * MIN)
        self.assertEqual(app.notifier.notify.await_count, 1)
        app.notifier.notify.return_value = 1
        tick(app, T0 + 190 * MIN)
        self.assertEqual(app.notifier.notify.await_count, 2)
        for m in range(191, 260):
            tick(app, T0 + m * MIN)
        self.assertEqual(app.notifier.notify.await_count, 2)

    def test_a_notify_error_is_retried_like_a_push_that_reached_nobody(self):
        app = make_app()
        app.notifier.notify.side_effect = RuntimeError("boom")
        run_for(app, T0, 180)
        self.assertEqual(app.notifier.notify.await_count, 1)
        self.assertEqual(len([m for lvl, m in app.logs if lvl == "WARNING" and "notify failed" in m]), 1)
        app.notifier.notify.side_effect = None
        tick(app, T0 + 190 * MIN)
        self.assertEqual(app.notifier.notify.await_count, 2)

    def test_a_run_that_ends_while_the_push_is_retrying_is_not_pushed_later(self):
        app = make_app(delivered=0)
        run_for(app, T0, 180)
        feed(app, T0 + 181 * MIN, 5)
        tick(app, T0 + 183 * MIN)
        for m in range(184, 220):
            tick(app, T0 + m * MIN)
        self.assertEqual(app.notifier.notify.await_count, 1)


class RestartRecovery(unittest.TestCase):
    def test_run_start_is_rebuilt_from_history_not_reset_to_now(self):
        app = make_app(states={POWER: "40"}, history=rows_off_then_on(240, 100))
        self.assertEqual(app._run_started, T0 - 100 * MIN)
        tick(app, T0 + 79 * MIN)
        app.notifier.notify.assert_not_awaited()
        tick(app, T0 + 80 * MIN)
        app.notifier.notify.assert_awaited_once()

    def test_a_dip_inside_off_hold_is_bridged_in_the_history(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 100 * MIN, 40), row(T0 - 60 * MIN, 5),
                row(T0 - 60 * MIN + 90, 40), row(T0, 40)]
        app = make_app(states={POWER: "40"}, history=rows)
        self.assertEqual(app._run_started, T0 - 100 * MIN)

    def test_a_dip_lasting_off_hold_splits_the_history_run(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 100 * MIN, 40), row(T0 - 60 * MIN, 5),
                row(T0 - 60 * MIN + 120, 40), row(T0, 40)]
        app = make_app(states={POWER: "40"}, history=rows)
        self.assertEqual(app._run_started, T0 - 60 * MIN + 120)

    def test_history_rows_out_of_order_are_replayed_chronologically(self):
        rows = list(reversed(rows_off_then_on(240, 100)))
        app = make_app(states={POWER: "40"}, history=rows)
        self.assertEqual(app._run_started, T0 - 100 * MIN)

    def test_history_ending_in_a_long_off_period_gives_no_run(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 200 * MIN, 40), row(T0 - 190 * MIN, 3)]
        app = make_app(states={POWER: "3"}, history=rows)
        self.assertIsNone(app._run_started)

    def test_history_still_running_but_live_reading_already_low_ends_after_the_hold(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 30 * MIN, 40), row(T0 - 10, 40)]
        app = make_app(states={POWER: "3"}, history=rows)
        self.assertEqual(app._run_started, T0 - 30 * MIN)
        self.assertEqual(app._below_since, T0)
        tick(app, T0 + 120)
        self.assertIsNone(app._run_started)

    def test_unavailable_rows_in_the_history_are_skipped(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 100 * MIN, 40), row(T0 - 50 * MIN, "unavailable"),
                row(T0 - 40 * MIN, 40), row(T0 - 30 * MIN, "unknown")]
        app = make_app(states={POWER: "40"}, history=rows)
        self.assertEqual(app._run_started, T0 - 100 * MIN)

    def test_unreadable_history_starts_the_clock_now_when_the_fridge_runs(self):
        app = make_app(states={POWER: "40"}, history=RuntimeError("recorder down"))
        self.assertEqual(app._run_started, T0)
        self.assertTrue(any(lvl == "WARNING" and "history unreadable" in m for lvl, m in app.logs))

    def test_empty_history_starts_the_clock_now_when_the_fridge_runs(self):
        app = make_app(states={POWER: "40"}, history=None)
        self.assertEqual(app._run_started, T0)

    def test_unreadable_history_with_the_fridge_off_starts_no_run(self):
        app = make_app(states={POWER: "3"}, history=RuntimeError("recorder down"))
        self.assertIsNone(app._run_started)

    def test_unavailable_power_sensor_at_start_up_starts_no_run_from_a_blank_history(self):
        app = make_app(states={POWER: "unavailable"}, history=None)
        self.assertIsNone(app._run_started)

    def test_history_window_reaches_back_past_the_threshold(self):
        app = make_app(states={POWER: "40"}, history=rows_off_then_on(240, 100))
        call = app.history_calls[0]
        self.assertEqual(call["entity_id"], POWER)
        self.assertEqual(call["end_time"], datetime.fromtimestamp(T0, timezone.utc))
        self.assertGreater(call["end_time"] - call["start_time"], timedelta(minutes=180))

    def test_restart_inside_a_long_run_that_already_pushed_does_not_push_again(self):
        app = make_app(states={POWER: "40", HEALTH: "long_run"}, history=rows_off_then_on(240, 200))
        for m in range(1, 30):
            tick(app, T0 + m * MIN)
        app.notifier.notify.assert_not_awaited()
        self.assertEqual(published(app)[0][0], "long_run")

    def test_restart_inside_a_long_run_with_no_earlier_state_pushes_once(self):
        app = make_app(states={POWER: "40"}, history=rows_off_then_on(240, 200))
        for m in range(1, 30):
            tick(app, T0 + m * MIN)
        self.assertEqual(app.notifier.notify.await_count, 1)

    def test_a_stale_long_run_state_does_not_swallow_the_next_runs_push(self):
        rows = [row(T0 - 240 * MIN, 2), row(T0 - 230 * MIN, 40), row(T0 - 100 * MIN, 3)]
        app = make_app(states={POWER: "3", HEALTH: "long_run"}, history=rows)
        self.assertFalse(app._alerted)
        run_for(app, T0 + 600, 180)
        self.assertEqual(app.notifier.notify.await_count, 1)


class Publishing(unittest.TestCase):
    def test_initialize_publishes_ok_with_a_reason(self):
        app = make_app()
        app.set_state.assert_called_once_with(
            HEALTH,
            state="ok",
            replace=True,
            attributes={
                "friendly_name": "Fridge health",
                "icon": "mdi:fridge",
                "alert_minutes": 180,
                "reason": "Compressor is off",
            },
        )

    def test_wine_fridge_entity_gets_its_own_friendly_name(self):
        app = make_app(args=WINE_ARGS)
        self.assertEqual(app.set_state.call_args.args[0], "sensor.wine_fridge_health")
        self.assertEqual(published(app)[0][1]["friendly_name"], "Wine fridge health")

    def test_run_start_publishes_the_run_attributes(self):
        app = make_app()
        feed(app, T0, 40)
        state, attrs = published(app)[-1]
        self.assertEqual(state, "ok")
        self.assertEqual(attrs["run_started"], "2026-10-22T12:00:00+00:00")
        self.assertEqual(attrs["run_minutes"], 0)
        self.assertEqual(attrs["reason"], "Compressor has been running for 0 min (alert after 3 h)")

    def test_crossing_the_threshold_publishes_long_run_straight_away(self):
        app = make_app()
        run_for(app, T0, 180)
        state, attrs = published(app)[-1]
        self.assertEqual(state, "long_run")
        self.assertEqual(attrs["run_minutes"], 180)
        self.assertEqual(attrs["icon"], "mdi:fridge-alert")
        self.assertEqual(attrs["reason"], "Compressor has run for 3 h without a break (alert after 3 h)")

    def test_run_end_publishes_ok_without_run_attributes(self):
        app = make_app()
        run_for(app, T0, 200)
        end = T0 + 200 * MIN
        feed(app, end, 5)
        tick(app, end + 120)
        state, attrs = published(app)[-1]
        self.assertEqual(state, "ok")
        self.assertNotIn("run_started", attrs)
        self.assertNotIn("run_minutes", attrs)

    def test_republished_every_five_minutes_and_not_on_every_tick(self):
        app = make_app()
        for s in range(60, 300, 60):
            tick(app, T0 + s)
        self.assertEqual(app.set_state.call_count, 1)
        tick(app, T0 + 300)
        self.assertEqual(app.set_state.call_count, 2)
        tick(app, T0 + 360)
        self.assertEqual(app.set_state.call_count, 2)

    def test_a_failed_publish_is_retried_on_the_next_tick(self):
        app = make_app()
        app.set_state.side_effect = RuntimeError("HA down")
        tick(app, T0 + 300)
        app.set_state.side_effect = None
        tick(app, T0 + 360)
        self.assertEqual(app.set_state.call_count, 3)
        self.assertTrue(any(lvl == "WARNING" and "publish failed" in m for lvl, m in app.logs))


class Registration(unittest.TestCase):
    def test_listens_to_the_power_sensor_and_ticks_every_minute(self):
        app = make_app()
        app.listen_state.assert_called_once_with(app._on_power, POWER)
        app.run_every.assert_called_once_with(app._tick, "now+60", 60)
        app.get_app.assert_called_once_with("MobileNotifier")

    def test_optional_args_default(self):
        app = make_app()
        self.assertEqual((app.running_w, app.off_hold_s, app.notify_target), (20.0, 120.0, "mikkel"))

    def test_optional_args_can_be_set(self):
        app = make_app(args={**ARGS, "running_w": 35, "off_hold_s": 300, "notify_target": "kristine"})
        self.assertEqual((app.running_w, app.off_hold_s, app.notify_target), (35.0, 300.0, "kristine"))

    def test_required_args(self):
        for key in ARGS:
            with self.subTest(key=key):
                args = {k: v for k, v in ARGS.items() if k != key}
                with self.assertRaises(KeyError):
                    make_app(args=args)


class Duration(unittest.TestCase):
    def test_formats_minutes_and_hours(self):
        cases = [(0, "0 min"), (5, "5 min"), (45, "45 min"), (60, "1 t"), (65, "1 t 5 min"),
                 (180, "3 t"), (185.9, "3 t 5 min")]
        for minutes, expected in cases:
            with self.subTest(minutes=minutes):
                self.assertEqual(fw._duration(minutes, "t"), expected)

    def test_hour_unit_is_a_parameter(self):
        self.assertEqual(fw._duration(65, "h"), "1 h 5 min")


class AsciiLogs(unittest.TestCase):
    def test_log_literals_in_the_source_are_ascii(self):
        tree = ast.parse(Path(fw.__file__).read_text(encoding="utf-8"))
        checked = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "log":
                for const in ast.walk(node):
                    if isinstance(const, ast.Constant) and isinstance(const.value, str):
                        checked += 1
                        self.assertTrue(const.value.isascii(), const.value)
        self.assertGreater(checked, 5)

    def test_logged_messages_are_ascii_with_a_danish_display_name(self):
        app = make_app(states={POWER: "40"}, history=RuntimeError("recorder down"), delivered=0)
        for m in range(1, 200):
            tick(app, T0 + m * MIN)
        feed(app, T0 + 200 * MIN, 5)
        tick(app, T0 + 203 * MIN)
        self.assertGreater(len(app.logs), 3)
        for _, message in app.logs:
            self.assertTrue(message.isascii(), message)


if __name__ == "__main__":
    unittest.main()
