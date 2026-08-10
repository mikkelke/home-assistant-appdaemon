from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))

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

import wakeup_bedroom as wb  # noqa: E402


def make_app(now, states, last_fire_date=None):
    """WakeupRoutine with fake state/clock, without AppDaemon's initialize()."""
    app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    app.alarm_time_entity = "input_datetime.wakeup_bedroom"
    app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
    app.master_player = "media_player.bedroom"
    app.stream_id = "https://example/primary.m3u8"
    app.stream_type = "music"
    app.fallback = "https://example/fallback.m3u8"
    app.media_verify_delay_sec = 12
    app.media_verify_recheck_sec = 5
    app.media_attempt = 0
    app._media_recheck_used = False
    # Feed/page dedup flags from the surfacing pass (25c95ee)
    app._media_fallback_reported = False
    app._media_exhausted_reported = False
    app._fired_via_heartbeat = False
    app.last_fire_date = last_fire_date
    app.fired_events = []
    app.fire_event = lambda *a, **kw: app.fired_events.append((a, kw))
    app.user_log = "test_log"
    app.datetime = lambda: now
    app.get_state = lambda entity, **kw: states.get((entity, kw.get("attribute")))
    app.log = lambda *a, **kw: None
    app.alarm_fired = []
    app._alarm_fire = lambda arg: app.alarm_fired.append(arg)
    app.scheduled = []

    def run_in(cb, delay, **kw):
        app.scheduled.append((cb, delay, kw))
        return object()

    app.run_in = run_in
    app.service_calls = []
    app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
    return app


class HeartbeatRescue(unittest.TestCase):
    """2026-07-17 root-cause session: Sep 19 - Nov 16 2025 the AD scheduler ran every
    timer +67 min late, run_daily was window-guarded off daily, and only a leaked
    minute run_every woke Mikkel (47 mornings). The always-on heartbeat replaces the
    arm/disarm watchdog: it must fire when run_daily misses the minute and stay
    silent on every healthy day."""

    STATES = {
        ("input_boolean.wakeup_bedroom", None): "on",
        ("input_datetime.wakeup_bedroom", None): "06:15:00",
    }

    def test_fires_when_run_daily_missed_the_minute(self):
        now = datetime(2026, 7, 17, 6, 15, 10)
        app = make_app(now, self.STATES)
        app._heartbeat_check(None)
        self.assertEqual(len(app.alarm_fired), 1)

    def test_head_start_lets_a_healthy_run_daily_win(self):
        # At +3 s run_daily may still be about to fire; the heartbeat must wait.
        now = datetime(2026, 7, 17, 6, 15, 3)
        app = make_app(now, self.STATES)
        app._heartbeat_check(None)
        self.assertEqual(app.alarm_fired, [])

    def test_deduped_when_alarm_already_fired_today(self):
        now = datetime(2026, 7, 17, 6, 15, 10)
        app = make_app(now, self.STATES, last_fire_date=date(2026, 7, 17))
        app._heartbeat_check(None)
        self.assertEqual(app.alarm_fired, [])

    def test_yesterdays_fire_does_not_block_today(self):
        now = datetime(2026, 7, 17, 6, 15, 10)
        app = make_app(now, self.STATES, last_fire_date=date(2026, 7, 16))
        app._heartbeat_check(None)
        self.assertEqual(len(app.alarm_fired), 1)

    def test_silent_when_alarm_disabled(self):
        now = datetime(2026, 7, 17, 6, 15, 10)
        states = dict(self.STATES)
        states[("input_boolean.wakeup_bedroom", None)] = "off"
        app = make_app(now, states)
        app._heartbeat_check(None)
        self.assertEqual(app.alarm_fired, [])

    def test_delayed_tick_still_rescues_within_guard(self):
        # A tick delayed past the alarm minute (delta 70 s) must still fire; the
        # ±90 s guard inside _alarm_fire accepts it.
        now = datetime(2026, 7, 17, 6, 16, 10)
        app = make_app(now, self.STATES)
        app._heartbeat_check(None)
        self.assertEqual(len(app.alarm_fired), 1)

    def test_outside_window_never_fires(self):
        # +95 s would be rejected by the trigger-window guard anyway; don't try.
        now = datetime(2026, 7, 17, 6, 16, 35)
        app = make_app(now, self.STATES)
        app._heartbeat_check(None)
        self.assertEqual(app.alarm_fired, [])

    def test_long_after_alarm_is_a_no_op(self):
        now = datetime(2026, 7, 17, 7, 22, 0)  # the classic +67 min ghost time
        app = make_app(now, self.STATES)
        app._heartbeat_check(None)
        self.assertEqual(app.alarm_fired, [])


class MediaVerifyRecheck(unittest.TestCase):
    """The 2 s verify window produced 18 false failovers Mar-Jul 2026 (HLS still
    buffering). One free recheck per attempt must absorb those before any
    stream switch, and a genuinely dead stream must still escalate."""

    def make_media_app(self, player_state):
        now = datetime(2026, 7, 17, 6, 15, 14)
        states = {
            ("media_player.bedroom", None): player_state,
            ("media_player.bedroom", "all"): {
                "attributes": {"media_duration": 0, "media_position": 0}
            },
        }
        return make_app(now, states)

    def test_playing_stream_needs_no_action(self):
        app = self.make_media_app("playing")
        app._verify_media_playback(None)
        self.assertEqual(app.scheduled, [])
        self.assertEqual(app.service_calls, [])

    def test_first_idle_check_rechecks_instead_of_failing_over(self):
        app = self.make_media_app("idle")
        app._verify_media_playback(None)
        # No play_media fired, one recheck scheduled at the recheck cadence
        self.assertEqual(app.service_calls, [])
        self.assertEqual(len(app.scheduled), 1)
        cb, delay, _ = app.scheduled[0]
        self.assertEqual(cb, app._verify_media_playback)
        self.assertEqual(delay, app.media_verify_recheck_sec)
        self.assertTrue(app._media_recheck_used)

    def test_second_idle_check_escalates_to_fallback(self):
        app = self.make_media_app("idle")
        app._media_recheck_used = True
        app._verify_media_playback(None)
        play_calls = [c for c in app.service_calls if c[0] == "media_player/play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][1]["media_content_id"], app.fallback)
        # Fresh recheck budget for the fallback stream
        self.assertFalse(app._media_recheck_used)
        # The switch is reported to the house feed exactly once
        feed = [kw for a, kw in app.fired_events if a and a[0] == "house_events_report"]
        self.assertEqual(len(feed), 1)
        self.assertTrue(app._media_fallback_reported)

    def test_dropped_playback_skips_recheck_and_goes_to_fallback(self):
        app = self.make_media_app("idle")
        app._verify_media_is_still_playing(None)
        play_calls = [c for c in app.service_calls if c[0] == "media_player/play_media"]
        self.assertEqual(len(play_calls), 1)
        self.assertEqual(play_calls[0][1]["media_content_id"], app.fallback)


class WakeNowButton(unittest.TestCase):
    """The dashboard's Wake-up-now press runs the sequence immediately, toggle armed or
    not (user, 2026-08-10). _on_wake_now now sets its own _manual_fire_in_progress flag
    (not the late-catchup flag - that one's docstring says it's read ONLY by the trigger-
    window guard) and always restores it; _alarm_fire must run before the alarm time is
    rewritten (see the load-bearing-order comment on _on_wake_now)."""

    def _app(self):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.user_log = "log"
        app.log = lambda *a, **k: None
        app._manual_fire_in_progress = False
        app._late_catchup_in_progress = False  # _on_wake_now must never touch this flag
        app._fired = []
        app._order = []

        def fire(_=None):
            app._order.append("alarm_fire")
            app._fired.append(app._manual_fire_in_progress)

        app._alarm_fire = fire
        app._set_alarm_time_to_now_slot = lambda: app._order.append("set_alarm_time")
        return app

    def test_press_fires_with_window_bypass_and_restores_flag(self):
        app = self._app()
        app._on_wake_now("input_button.wake_up_now", None, "unknown",
                         "2026-07-29T16:30:00+00:00", {})
        self.assertEqual(app._fired, [True])              # manual bypass active during fire
        self.assertFalse(app._manual_fire_in_progress)     # ...and restored after
        self.assertFalse(app._late_catchup_in_progress)    # ...and never touched at all

    def test_flag_restored_even_when_sequence_raises(self):
        app = self._app()
        def boom(_=None):
            raise RuntimeError("boom")
        app._alarm_fire = boom
        with self.assertRaises(RuntimeError):
            app._on_wake_now("e", None, None, "2026-07-29T16:30:00+00:00", {})
        self.assertFalse(app._manual_fire_in_progress)

    def test_unavailable_states_ignored(self):
        app = self._app()
        for bad in (None, "unknown", "unavailable"):
            app._on_wake_now("e", None, None, bad, {})
        self.assertEqual(app._fired, [])

    def test_alarm_fire_runs_before_alarm_time_is_written(self):
        app = self._app()
        app._on_wake_now("input_button.wake_up_now", None, "unknown",
                         "2026-07-29T16:30:00+00:00", {})
        self.assertEqual(app._order, ["alarm_fire", "set_alarm_time"])


class ManualFireBypassesToggleGuard(unittest.TestCase):
    """_alarm_fire's toggle guard bailed on 3 of 4 "Wake up now" presses on 2026-08-10
    because input_boolean.wakeup_bedroom was off. A manual fire must get all the way
    through it; a non-manual call with the toggle off must still bail there, same as
    before. Drives the REAL _alarm_fire (module make_app above stubs it out for the other
    classes in this file), stubbing only what it calls once past the guards it's testing."""

    ALARM_TIME = "06:15:00"

    def _real_alarm_fire_app(self, manual_flag):
        # "now" matches ALARM_TIME closely so the +/-90s trigger-window guard passes on its
        # own merits either way, isolating the toggle guard as the only thing that differs
        # between the manual and non-manual runs below.
        now = datetime(2026, 8, 10, 6, 15, 10)
        states = {
            ("input_boolean.wakeup_bedroom", None): "off",
            ("input_datetime.wakeup_bedroom", None): self.ALARM_TIME,
        }
        app = make_app(now, states)
        del app._alarm_fire  # use the real method instead of module make_app's stub
        app._manual_fire_in_progress = manual_flag
        app._late_catchup_in_progress = False
        app._state = {}
        app._save_state = lambda: None
        app._both_away = lambda: False
        app._current_bedroom_wake_target = None
        app.bedroom_cover = "cover.bedroom"
        app.bathroom_cover = "cover.bathroom"
        app.bedroom_cover_target = 38
        app.bathroom_cover_target = 20
        app._decide_bedroom_wake_target = lambda: (app.bedroom_cover_target, "test")
        app._cover_position = lambda entity: app.bedroom_cover_target
        app._nudge_cover_if_closed = lambda entity, target: None
        app._maybe_start_light_ramp = lambda: None
        app.attach_calls = []
        app._attach_cancel_listeners = lambda: app.attach_calls.append(True)
        app._group_speakers = lambda: None
        app._start_media_and_volume_ramp = lambda: None
        app.log_calls = []
        app.log = lambda *a, **kw: app.log_calls.append(a[0] if a else "")
        return app

    def test_manual_fire_reaches_alarm_firing_with_toggle_off(self):
        app = self._real_alarm_fire_app(manual_flag=True)
        app._alarm_fire({})
        self.assertIn("[wake] Alarm firing.", app.log_calls)
        self.assertEqual(app.attach_calls, [True])

    def test_non_manual_fire_still_bails_on_toggle_off(self):
        app = self._real_alarm_fire_app(manual_flag=False)
        app._alarm_fire({})
        self.assertNotIn("[wake] Alarm firing.", app.log_calls)
        self.assertIn("[wake] Toggle off; skipping.", app.log_calls)
        self.assertEqual(app.attach_calls, [])


class AttachCancelListenersManualBypass(unittest.TestCase):
    """_attach_cancel_listeners must not arm the toggle-off cancel listener for a manual
    "Wake up now" run (2026-08-10): the toggle is not the arming state for a manual fire,
    so an off-edge must not _stop_all a run the user explicitly asked for - this is what
    killed the run at 07:46:54 this morning. Presence listeners are unaffected either way."""

    def _app(self, manual_flag):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.persons = ["person.mikkel", "person.kristine"]
        app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
        app.cancel_listeners = []
        app._manual_fire_in_progress = manual_flag
        app.listen_state_calls = []

        def listen_state(cb, entity, **kw):
            app.listen_state_calls.append(entity)
            return object()

        app.listen_state = listen_state
        return app

    def test_omits_toggle_listener_when_manual_fire_in_progress(self):
        app = self._app(manual_flag=True)
        app._attach_cancel_listeners()
        self.assertNotIn(app.alarm_enabled_entity, app.listen_state_calls)
        self.assertEqual(app.listen_state_calls, ["person.mikkel", "person.kristine"])
        self.assertEqual(len(app.cancel_listeners), 2)

    def test_includes_toggle_listener_for_a_scheduled_fire(self):
        app = self._app(manual_flag=False)
        app._attach_cancel_listeners()
        self.assertEqual(
            app.listen_state_calls,
            ["person.mikkel", "person.kristine", app.alarm_enabled_entity],
        )
        self.assertEqual(len(app.cancel_listeners), 3)


class SetAlarmTimeToNowSlot(unittest.TestCase):
    """Manual "Wake up now" (user, 2026-08-10): the press also snaps the configured alarm
    time to the current 5-minute slot, floored - never rounded up - so it lands in the past
    where last_fire_date already guards against a second fire (see _on_wake_now and the
    floor-not-round comment on _set_alarm_time_to_now_slot)."""

    def _app(self, now, current_time_state):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.alarm_time_entity = "input_datetime.wakeup_bedroom"
        app.user_log = "test_log"
        app.datetime = lambda: now
        app.get_state = lambda entity, **kw: current_time_state
        app.log_calls = []
        app.log = lambda *a, **kw: app.log_calls.append((a, kw))
        app.service_calls = []
        app.call_service = lambda service, **kw: app.service_calls.append((service, kw))
        return app

    def test_floors_07_46_to_07_45(self):
        app = self._app(datetime(2026, 8, 10, 7, 46, 0), "07:00:00")
        app._set_alarm_time_to_now_slot()
        self.assertEqual(len(app.service_calls), 1)
        service, kw = app.service_calls[0]
        self.assertEqual(service, "input_datetime/set_datetime")
        self.assertEqual(kw["entity_id"], app.alarm_time_entity)
        self.assertEqual(kw["time"], "07:45:00")

    def test_07_45_already_set_skips_call(self):
        app = self._app(datetime(2026, 8, 10, 7, 45, 40), "07:45:00")
        app._set_alarm_time_to_now_slot()
        self.assertEqual(app.service_calls, [])

    def test_floors_07_49_to_07_45(self):
        app = self._app(datetime(2026, 8, 10, 7, 49, 59), "06:00:00")
        app._set_alarm_time_to_now_slot()
        self.assertEqual(app.service_calls[0][1]["time"], "07:45:00")

    def test_07_50_is_its_own_slot(self):
        app = self._app(datetime(2026, 8, 10, 7, 50, 0), "06:00:00")
        app._set_alarm_time_to_now_slot()
        self.assertEqual(app.service_calls[0][1]["time"], "07:50:00")

    def test_floors_00_03_to_00_00(self):
        app = self._app(datetime(2026, 8, 10, 0, 3, 0), "06:00:00")
        app._set_alarm_time_to_now_slot()
        self.assertEqual(app.service_calls[0][1]["time"], "00:00:00")

    def test_swallows_a_raising_call_service(self):
        app = self._app(datetime(2026, 8, 10, 7, 46, 0), "07:00:00")
        def boom(service, **kw):
            raise RuntimeError("boom")
        app.call_service = boom
        app._set_alarm_time_to_now_slot()  # must not raise
        self.assertTrue(any(kw.get("level") == "WARNING" for a, kw in app.log_calls))


if __name__ == "__main__":
    unittest.main()
