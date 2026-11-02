import asyncio
import json
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock
from zoneinfo import ZoneInfo

APPS = Path(__file__).resolve().parents[2]
for folder in ("rutines", "lights", "blinds"):
    sys.path.insert(0, str(APPS / folder))
if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    for module in ("appdaemon", "appdaemon.plugins", "appdaemon.plugins.hass", "appdaemon.plugins.hass.hassapi"):
        sys.modules[module] = types.ModuleType(module)
    sys.modules["appdaemon.plugins.hass.hassapi"].Hass = object

import wakeup_bedroom as wb


class WakeSession(unittest.TestCase):
    def make_app(self, state=None):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        app.state_file = str(Path(tmp.name) / "state.json")
        app._state = state or {}
        app.now = datetime(2026, 11, 1, 6, 30)
        app.datetime = lambda: app.now
        app.user_log = "test"
        app.log = MagicMock()
        app.set_state = MagicMock()
        app.services = []
        app.call_service = lambda service, **kwargs: app.services.append((service, kwargs))
        app.turn_on = MagicMock()
        app.turn_off = MagicMock()
        app.master_player = "media_player.bedroom"
        app.join_players = ["media_player.bathroom"]
        app.watchdog_players = ["media_player.bedroom_2", "media_player.bathroom_2"]
        app.bedroom_cover = "cover.bedroom_blind"
        app.bathroom_cover = "cover.bathroom_blind"
        app.closed_is_100 = True
        app.light_entity = "light.bedroom"
        app.adaptive_brightness_switch = "switch.adaptive_brightness"
        app._session_listener = "bed-session"
        app.cover_listener = "blind-listener"
        app.cancel_listeners = ["person", "toggle", "light-darkness"]
        app._wake_cancel_handles = ["person", "toggle"]
        app._light_cancel_handles = ["light-darkness"]
        app.cancel_listen_state = MagicMock()
        app.cancel_timer = MagicMock()
        app.ramp_active = True
        app.volume_active = True
        app.ramp_timer = "light-timer"
        app.volume_timer = "volume-timer"
        app._wake_window_until = app.now + timedelta(minutes=30)
        app._alarm_fired_at = app.now
        app._wake_group_seen = False
        app.wake_window_min = 30
        app._media_generation = 1
        app._light_generation = 1
        app._media_running = True
        app.scheduled = []
        app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or len(app.scheduled)
        app._snooze_generation = 0
        app._notification_generation = 0
        app._notification_lock = None
        app._snooze_return_in_progress = False
        app._manual_fire_in_progress = False
        app._late_catchup_in_progress = False
        app._fired_via_heartbeat = False
        app._load_wake_session()
        if not state:
            app._session_status = "ringing"
        app._radio_playing = True
        app.notifier = types.SimpleNamespace(notify=AsyncMock(), clear_notification=AsyncMock())
        app.watchdog = types.SimpleNamespace(suppress_restart=MagicMock())
        app.get_app = lambda name: app.notifier if name == "MobileNotifier" else app.watchdog if name == "RadioWatchdog" else None
        app.tasks = []
        app.create_task = lambda task: app.tasks.append(task)
        self.addCleanup(lambda: [task.close() for task in app.tasks if hasattr(task, "close")])
        app.states = {app.master_player: "playing", app.join_players[0]: "playing"}
        app.get_state = lambda entity, **kw: app.states.get(entity)
        app._wake_group_state = lambda: True
        app.alarm_enabled_entity = "input_boolean.wakeup_bedroom"
        app.alarm_time_entity = "input_datetime.wakeup_bedroom"
        app.persons = ["person.mikkel"]
        app.bedroom_cover_target = 38
        app.bathroom_cover_target = 40
        app._decide_bedroom_wake_target = lambda: (38, "cool")
        app._cover_position = lambda entity: 38
        app._nudge_cover_if_closed = MagicMock()
        app._maybe_start_light_ramp = MagicMock()
        app._group_speakers = MagicMock()
        app._start_media_and_volume_ramp = MagicMock()
        app.fire_event = MagicMock()
        app.listen_state = MagicMock(side_effect=lambda *a, **kw: object())
        app.last_fire_date = app.now.date()
        return app

    def drain_notifications(self, app):
        async def drain():
            while app.tasks:
                await app.tasks.pop(0)
        asyncio.run(drain())

    def test_dismiss_only_stops_radio_and_preserves_window_and_light_work(self):
        app = self.make_app()
        until, fired, generation = app._wake_window_until, app._alarm_fired_at, app._light_generation
        app._dismiss_wake()
        self.assertEqual(app._session_status, "dismissed")
        self.assertEqual(app._wake_window_until, until)
        self.assertEqual(app._alarm_fired_at, fired)
        self.assertEqual(app._light_generation, generation)
        self.assertTrue(app.ramp_active)
        self.assertEqual(app.cover_listener, "blind-listener")
        self.assertEqual([service for service, kw in app.services], ["media_player/media_stop"] * 2)
        self.assertFalse(app.volume_active)
        app.watchdog.suppress_restart.assert_called_once_with(app.watchdog_players)
        self.drain_notifications(app)
        app.notifier.clear_notification.assert_awaited_once_with(tag="wake_alarm", target=["mikkel"])

    def test_closed_snooze_starts_clock_at_tap_and_respects_blind_owner(self):
        app = self.make_app()
        requested = []
        owner = types.SimpleNamespace(request=lambda source, pct, **kw: requested.append((source, pct)) or {"granted": False, "winner": "manual"})
        app._blind_owner = lambda: owner
        start = app._session_now()
        app._start_snooze(10, "closed")
        self.assertEqual(app._snooze_until, start + timedelta(minutes=10))
        self.assertEqual(app.scheduled[0][1], 600)
        self.assertEqual(requested, [("wake", 100)])
        self.assertFalse(app.ramp_active)
        self.assertFalse(app.volume_active)
        self.assertIsNone(app.cover_listener)
        self.assertIsNone(app._wake_window_until)
        self.assertIn(("light/turn_off", {"entity_id": app.light_entity, "transition": 3}), app.services)
        app.turn_on.assert_called_with(app.adaptive_brightness_switch)
        self.assertFalse(any(service.startswith("cover/") for service, kw in app.services))
        self.assertEqual(app._state["wake_session"]["minutes"], 10)

    def test_open_snooze_preserves_room_and_running_light_ramp(self):
        app = self.make_app()
        before = (app._light_generation, app._alarm_fired_at, app.cover_listener, app.ramp_timer)
        app._start_snooze(5, "open")
        self.assertEqual((app._light_generation, app._alarm_fired_at, app.cover_listener, app.ramp_timer), before)
        self.assertTrue(app.ramp_active)
        self.assertIn("light-darkness", app.cancel_listeners)
        self.assertNotIn("person", app.cancel_listeners)
        self.assertFalse(any(service.startswith(("light/", "cover/")) for service, kw in app.services))
        app.turn_on.assert_not_called()
        app.turn_off.assert_not_called()

    def test_return_full_vs_radio_only_and_does_not_change_scheduled_date(self):
        for curtain, full in (("closed", True), ("open", False)):
            with self.subTest(curtain=curtain):
                app = self.make_app()
                app._start_snooze(5, curtain)
                old_date = app.last_fire_date
                app.now += timedelta(minutes=5)
                app._start_wake_sequence = MagicMock()
                app._snooze_return(app.scheduled[0][2])
                app._start_wake_sequence.assert_called_once_with(full_room=full)
                self.assertEqual(app.last_fire_date, old_date)
                self.assertIsNone(app._snooze_until)

    def test_real_return_ignores_schedule_toggle_and_away_and_opens_new_window(self):
        for curtain in ("closed", "open"):
            app = self.make_app()
            app._start_snooze(5, curtain)
            app.states[app.alarm_enabled_entity] = "off"
            app.states["person.mikkel"] = "not_home"
            app._is_within_trigger_window = lambda seconds: False
            app.now += timedelta(minutes=5)
            app._snooze_return(app.scheduled[0][2])
            app._start_media_and_volume_ramp.assert_called_once()
            self.assertEqual(app._session_status, "ringing")
            self.assertEqual(app._wake_window_until, app.now + timedelta(minutes=30))
            self.assertEqual(app._nudge_cover_if_closed.call_count, 2 if curtain == "closed" else 0)
            self.assertEqual(app._maybe_start_light_ramp.call_count, 1 if curtain == "closed" else 0)
            self.assertEqual(app._wake_cancel_handles, [])

    def test_replace_invalidates_old_timer_and_cancel_leaves_room_untouched(self):
        app = self.make_app()
        app._start_snooze(5, "open")
        old_timer = app.scheduled[0][2]
        app._start_snooze(15, "open")
        app._start_wake_sequence = MagicMock()
        app._snooze_return(old_timer)
        app._start_wake_sequence.assert_not_called()
        app.services.clear()
        app._cancel_snooze()
        app._snooze_return(app.scheduled[-1][2])
        app._start_wake_sequence.assert_not_called()
        self.assertEqual(app._session_status, "dismissed")
        self.assertEqual(app._session_reason, "snooze_cancelled")
        self.assertEqual(app.services, [])
        self.assertEqual(app._state["wake_session"]["minutes"], 15)
        self.assertIsNone(app._state["wake_session"]["snooze_until"])

    def test_dismiss_during_snooze_cancels(self):
        app = self.make_app()
        app._start_snooze(5, "open")
        app.services.clear()
        app._dismiss_wake()
        self.assertIsNone(app._snooze_until)
        self.assertEqual(app._session_reason, "snooze_cancelled")
        self.assertEqual(app.services, [])

    def test_wake_now_consumes_pending_snooze_and_runs_full_room(self):
        app = self.make_app()
        app._start_snooze(5, "open")
        timer = app.scheduled[0][2]
        app._set_alarm_time_to_now_slot = MagicMock()
        app._on_wake_now(None, None, None, "pressed", {})
        self.assertEqual(app._nudge_cover_if_closed.call_count, 2)
        app._start_media_and_volume_ramp.assert_called_once()
        app._snooze_return(timer)
        app._start_media_and_volume_ramp.assert_called_once()
        self.assertIsNone(app._snooze_until)

    def test_restart_rearms_future_snooze_and_preserves_choice_on_disk(self):
        first = self.make_app()
        first._start_snooze(15, "open")
        saved = json.loads(Path(first.state_file).read_text())
        app = self.make_app(saved)
        app.now += timedelta(minutes=3)
        app._restore_wake_session()
        self.assertEqual(app.scheduled[0][1], 12 * 60)
        self.assertEqual(app._session_status, "snoozing")
        self.assertEqual((app._snooze_minutes, app._snooze_curtain), (15, "open"))

    def test_restart_past_due_fires_once_and_pending_is_removed_after_wake(self):
        first = self.make_app()
        first._start_snooze(5, "open")
        saved = json.loads(Path(first.state_file).read_text())
        app = self.make_app(saved)
        app.now += timedelta(minutes=6)
        app._restore_wake_session()
        self.assertEqual(app.scheduled[0][1], 0)
        cb, delay, kwargs = app.scheduled[0]
        cb(kwargs)
        cb(kwargs)
        app._start_media_and_volume_ramp.assert_called_once()
        self.assertIsNone(json.loads(Path(app.state_file).read_text())["wake_session"]["snooze_until"])

    def test_restart_during_return_before_media_recovers_pending_return_once(self):
        class Restart(BaseException):
            pass

        for interrupted in ("_start_wake_sequence", "_start_media_and_volume_ramp", "_nudge_cover_if_closed"):
            with self.subTest(interrupted=interrupted):
                app = self.make_app()
                app._start_snooze(5, "closed")
                app.now += timedelta(minutes=5)
                setattr(app, interrupted, MagicMock(side_effect=Restart))
                with self.assertRaises(Restart):
                    app._snooze_return(app.scheduled[0][2])
                saved = json.loads(Path(app.state_file).read_text())
                recovered = self.make_app(saved)
                recovered.now = app.now
                recovered._wake_window_until = None
                recovered.states.update({player: "idle" for player in [recovered.master_player] + recovered.join_players})
                recovered._restore_wake_session()
                self.assertEqual(len(recovered.scheduled), 1)
                callback, delay, kwargs = recovered.scheduled[0]
                self.assertEqual(delay, 0)
                callback(kwargs)
                callback(kwargs)
                recovered._start_media_and_volume_ramp.assert_called_once()
                self.assertEqual(recovered._session_status, "ringing")
                self.assertIsNone(json.loads(Path(recovered.state_file).read_text())["wake_session"]["snooze_until"])

    def test_duplicate_scheduled_callback_preserves_ringing_snoozed_and_dismissed_session(self):
        for action in (None, "snooze", "dismiss"):
            with self.subTest(action=action):
                app = self.make_app()
                app.last_fire_date = None
                app._session_status = "idle"
                app._is_within_trigger_window = lambda seconds: True
                app.states[app.alarm_enabled_entity] = "on"
                app.states["person.mikkel"] = "home"
                app._alarm_fire({})
                if action == "snooze":
                    app._start_snooze(5, "closed")
                elif action == "dismiss":
                    app._dismiss_wake()
                before = (app._session_status, app._snooze_until, app._snooze_generation)
                app._alarm_fire({})
                app._start_media_and_volume_ramp.assert_called_once()
                self.assertEqual((app._session_status, app._snooze_until, app._snooze_generation), before)

    def test_scheduled_callback_respects_current_session_even_without_fire_date(self):
        app = self.make_app()
        app.last_fire_date = None
        app._session_started_at = app._session_now().isoformat()
        app._is_within_trigger_window = lambda seconds: True
        app.states[app.alarm_enabled_entity] = "on"
        app.states["person.mikkel"] = "home"
        for status in ("ringing", "snoozing", "dismissed"):
            app._session_status = status
            app._alarm_fire({})
        app._start_media_and_volume_ramp.assert_not_called()

    def test_explicit_wake_now_still_runs_twice_in_same_day(self):
        app = self.make_app()
        app._set_alarm_time_to_now_slot = MagicMock()
        app._on_wake_now(None, None, None, "pressed", {})
        app._on_wake_now(None, None, None, "pressed", {})
        self.assertEqual(app._start_media_and_volume_ramp.call_count, 2)

    def test_snooze_wait_survives_toggle_off_and_both_away(self):
        app = self.make_app()
        app._start_snooze(5, "open")
        app.states["person.mikkel"] = "not_home"
        app._on_alarm_toggle(None, None, "on", "off", {})
        app._on_presence_change(None, None, "home", "not_home", {})
        self.assertEqual(app._session_status, "snoozing")
        self.assertIsNotNone(app._snooze_until)

    def test_inactive_actions_and_invalid_choices_do_not_remember_or_act(self):
        app = self.make_app()
        app._session_status = "dismissed"
        app._start_snooze(15, "open")
        app._dismiss_wake()
        app._cancel_snooze()
        self.assertEqual(app.services, [])
        self.assertEqual((app._snooze_minutes, app._snooze_curtain), (10, "closed"))
        self.assertEqual(app.log.call_count, 3)
        app._session_status = "ringing"
        for minutes, curtain in ((6, "closed"), (5.5, "open"), (True, "closed"), (10, "half"), (None, "closed")):
            app._start_snooze(minutes, curtain)
        self.assertEqual(app.services, [])
        self.assertEqual(app.scheduled, [])

    def test_stale_media_callbacks_cannot_restart_after_dismiss_or_accelerate_new_wake(self):
        app = self.make_app()
        old = {"generation": app._media_generation}
        app._dismiss_wake()
        app.services.clear()
        app._verify_media_playback(old)
        app._verify_media_is_still_playing(old)
        app._volume_tick(old)
        app._media_running = True
        app.volume_active = True
        app._verify_media_playback(old)
        app._volume_tick(old)
        self.assertEqual(app.services, [])
        self.assertEqual(app.scheduled, [])

    def test_closed_snooze_invalidates_deferred_light_and_old_expiry(self):
        app = self.make_app()
        app._compute_post_blind_settle_delay = lambda: (10, None)
        app._room_dark_for_wake_light = lambda: True
        old_generation = app._light_generation
        app._blind_wait_complete()
        delayed = app.scheduled[0][0]
        app._start_snooze(5, "closed")
        app.cover_listener = "new-listener"
        delayed({})
        app._expire_cover_listener({"generation": old_generation})
        app._ramp_tick({"generation": old_generation})
        app._maybe_start_light_ramp.assert_not_called()
        self.assertEqual(app.cover_listener, "new-listener")

    def test_missing_sensor_never_blocks_wake_and_logs_once(self):
        app = self.make_app()
        app.set_state.side_effect = RuntimeError("missing helper")
        app._start_wake_sequence()
        app._publish_wake_session()
        app._start_media_and_volume_ramp.assert_called_once()
        failures = [call for call in app.log.call_args_list if "Session state unavailable" in call.args[0]]
        self.assertEqual(len(failures), 1)

    def test_definitely_stopped_audio_ends_only_after_window_expires(self):
        app = self.make_app()
        app.states.update({player: "idle" for player in [app.master_player] + app.join_players})
        app._on_wake_media_state(None, None, None, "idle", {})
        self.assertFalse(app._radio_playing)
        self.assertEqual(app._session_status, "ringing")
        app._reconcile_finished_session()
        self.assertEqual(app._session_status, "ringing")
        app.now += timedelta(minutes=31)
        app._reconcile_finished_session()
        self.assertEqual(app._session_status, "stopped")

    def test_notification_actions_route_only_named_actions(self):
        app = self.make_app()
        for method in ("_start_snooze", "_dismiss_wake", "_cancel_snooze", "_queue_wake_notification", "_on_wake_now"):
            setattr(app, method, MagicMock())
        app._snooze_minutes, app._snooze_curtain = 15, "open"
        for action in ("OTHER", "WAKE_SNOOZE", "WAKE_DISMISS", "WAKE_SNOOZE_CANCEL", "WAKE_NOW"):
            app._on_notification_action("mobile_app_notification_action", {"action": action}, {})
        app._start_snooze.assert_called_once_with(15, "open")
        app._dismiss_wake.assert_called_once()
        app._cancel_snooze.assert_called_once()
        app._queue_wake_notification.assert_not_called()
        app._on_wake_now.assert_not_called()
        app._session_status = "snoozing"
        app._on_notification_action("mobile_app_notification_action", {"action": "WAKE_NOW"}, {})
        app._on_wake_now.assert_called_once()

    def test_notifications_use_correct_channel_tag_target_actions_and_deep_link(self):
        app = self.make_app()
        app._queue_wake_notification("ringing")
        self.drain_notifications(app)
        ring = app.notifier.notify.await_args.kwargs
        self.assertEqual(ring["target"], ["mikkel"])
        self.assertEqual(ring["link"], wb.ALARM_LINK)
        self.assertEqual(ring["data"]["channel"], "wake_alarm")
        self.assertEqual(ring["data"]["importance"], "high")
        self.assertEqual(ring["data"]["visibility"], "public")
        self.assertTrue(ring["data"]["sticky"])
        ring_actions = ring["data"]["actions"]
        self.assertEqual([item["action"] for item in ring_actions], [f"WAKE_SNOOZE|{app._session_id}", "URI", f"WAKE_DISMISS|{app._session_id}"])
        self.assertEqual(ring_actions[1]["uri"], wb.ALARM_LINK + "=options")
        app._start_snooze(10, "open")
        self.drain_notifications(app)
        snooze = app.notifier.notify.await_args.kwargs
        self.assertEqual(snooze["data"]["tag"], "wake_alarm")
        self.assertEqual(snooze["data"]["channel"], "wake_alarm_snooze")
        self.assertEqual(snooze["data"]["importance"], "low")
        self.assertEqual([item["action"] for item in snooze["data"]["actions"]], [f"{action}|{app._session_id}" for action in ("WAKE_SNOOZE_CANCEL", "WAKE_NOW")])

    def test_notification_stale_ring_does_not_replace_clear(self):
        app = self.make_app()
        app._queue_wake_notification("ringing")
        app._dismiss_wake()
        self.drain_notifications(app)
        app.notifier.notify.assert_not_awaited()
        app.notifier.clear_notification.assert_awaited_once()

    def test_restart_keeps_ringing_past_window_when_radio_plays_or_is_unknown(self):
        for state in ("playing", "unknown", None):
            app = self.make_app()
            app.now += timedelta(minutes=31)
            app.states.update({player: state for player in [app.master_player] + app.join_players})
            app._restore_wake_session()
            self.assertEqual(app._session_status, "ringing")

    def test_corrupt_optional_session_does_not_stop_loading_defaults(self):
        for stored in (["bad"], "bad", 3):
            app = self.make_app({"wake_session": stored})
            self.assertEqual(app._session_status, "idle")
            self.assertEqual((app._snooze_minutes, app._snooze_curtain), (10, "closed"))

    def test_queued_old_cancel_and_light_events_cannot_stop_replacement_wake(self):
        app = self.make_app()
        app._attach_cancel_listeners()
        wake_generation = app._wake_cancel_generation if hasattr(app, "_wake_cancel_generation") else 0
        light_generation = app._light_generation
        app._start_snooze(5, "closed")
        app.now += timedelta(minutes=5)
        app._snooze_return(app.scheduled[0][2])
        app._stop_all = MagicMock()
        app._finish_ramp = MagicMock()
        app._room_dark_for_wake_light = lambda: False
        app.ramp_active = True
        app.states["person.mikkel"] = "not_home"
        app._on_alarm_toggle(None, None, "on", "off", {"generation": wake_generation})
        app._on_presence_change(None, None, "home", "not_home", {"generation": wake_generation})
        app._on_bed_session_change(None, None, "on", "off", {"generation": light_generation})
        app._on_room_darkness_for_wake(None, None, "dark", "bright", {"generation": light_generation})
        app._stop_all.assert_not_called()
        app._finish_ramp.assert_not_called()
        self.assertEqual(app._session_status, "ringing")

    def test_keep_room_return_reports_only_radio_effect(self):
        app = self.make_app()
        app._start_wake_sequence(full_room=False)
        self.assertEqual(app.fire_event.call_args.kwargs["effect"], "Restarting the wake radio")

    def test_failed_speaker_stop_does_not_block_other_speaker_or_pending_return(self):
        app = self.make_app()
        attempts = []
        def service(name, **kwargs):
            attempts.append((name, kwargs))
            if kwargs.get("entity_id") == app.master_player:
                raise RuntimeError("speaker unavailable")
        app.call_service = service
        app._start_snooze(5, "open")
        self.assertTrue(any(kwargs.get("entity_id") == app.join_players[0] for name, kwargs in attempts))
        self.assertEqual(app._session_status, "snoozing")
        self.assertIsNone(app._wake_window_until)
        self.assertIsNotNone(app._snooze_until)

    def test_midnight_return_does_not_consume_next_scheduled_day(self):
        app = self.make_app()
        app.now = datetime(2026, 11, 1, 23, 58)
        app.last_fire_date = app.now.date()
        app._state["last_fire_date"] = app.last_fire_date.isoformat()
        app._start_snooze(5, "open")
        app.now += timedelta(minutes=5)
        app._snooze_return(app.scheduled[0][2])
        self.assertEqual(app.last_fire_date.isoformat(), "2026-11-01")
        self.assertEqual(app._state["last_fire_date"], "2026-11-01")
        app.now = datetime(2026, 11, 2, 6, 30)
        app.states[app.alarm_enabled_entity] = "on"
        app.states["person.mikkel"] = "home"
        app._is_within_trigger_window = lambda seconds: True
        app._alarm_fire({})
        self.assertEqual(app.last_fire_date.isoformat(), "2026-11-02")
        self.assertEqual(app._start_media_and_volume_ramp.call_count, 2)

    def test_dst_changes_keep_five_elapsed_minutes(self):
        zone = ZoneInfo("Europe/Copenhagen")
        for start, return_hour in ((datetime(2026, 3, 29, 1, 58, tzinfo=zone), 3),
                                   (datetime(2026, 10, 25, 2, 58, tzinfo=zone, fold=0), 2)):
            with self.subTest(start=start):
                app = self.make_app()
                app.now = start
                app._start_snooze(5, "open")
                self.assertEqual(app.scheduled[0][1], 300)
                self.assertEqual((app._snooze_until - app._session_now()).total_seconds(), 300)
                returned = app._snooze_until.astimezone(zone)
                self.assertEqual((returned.hour, returned.minute), (return_hour, 3))

    def test_missing_notifier_logs_once_and_never_blocks_physical_wake(self):
        app = self.make_app()
        app.get_app = MagicMock(side_effect=RuntimeError("notifier unavailable"))
        app._start_wake_sequence()
        self.drain_notifications(app)
        app._queue_wake_notification("ringing")
        self.drain_notifications(app)
        app._start_media_and_volume_ramp.assert_called_once()
        self.assertEqual(app._nudge_cover_if_closed.call_count, 2)
        failures = [call for call in app.log.call_args_list if "Alarm notification unavailable" in call.args[0]]
        self.assertEqual(len(failures), 1)


if __name__ == "__main__":
    unittest.main()
