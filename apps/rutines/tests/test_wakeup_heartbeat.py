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


if __name__ == "__main__":
    unittest.main()
