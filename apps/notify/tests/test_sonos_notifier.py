# tests/test_sonos_notifier.py - Unit tests for SonosNotifier: quiet-hours / sleep-mode gating,
# target resolution to Music Assistant room entities, and delivery as a chime_tts.say_url render
# played with media_player.play_media announce.
# Run from repo root: python3 -m unittest discover -s apps/notify/tests -q

import sys
import types
import unittest
from datetime import datetime, time
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

import sonos_notifier as sn  # noqa: E402

QUIET_HOURS_STATE = {
    "input_datetime.quiet_hours_end": "07:30",
    "input_datetime.quiet_hours_start": "22:00",
}

CLIP_URL = "https://ha.example/local/chime_tts/abc123.mp3"
EXPLICIT = "media_player.explicit"
DAYTIME = datetime(2026, 9, 11, 12, 0, 0)


def _say_url_response(url=CLIP_URL, success=True):
    """AppDaemon's envelope around chime_tts.say_url's own response."""
    return {
        "id": 7,
        "type": "result",
        "success": True,
        "result": {
            "context": {"id": "ctx"},
            "response": {"url": url, "media_content_id": None, "duration": 3.1, "success": success},
        },
    }


def _new_app(state=None, now=None, time_constraints_enabled=True, volumes=None):
    app = sn.SonosNotifier.__new__(sn.SonosNotifier)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.log_level = "INFO"

    app.kristine_sleep_mode_entity = "input_boolean.kristine_sleep_mode"
    app.mikkel_sleep_mode_entity = "input_boolean.mikkel_sleep_mode"
    app.tts_group_all = [
        "media_player.living_room", "media_player.kitchen", "media_player.bedroom", "media_player.kristines_room",
    ]
    app.tts_group_family_rooms = ["media_player.living_room", "media_player.kitchen"]
    app.tts_group_kristine = ["media_player.kristines_room"]
    app.tts_group_ms = ["media_player.bedroom"]
    app.default_chime_path = "/config/www/chimes/alert.mp3"
    app.tts_platform = "piper"

    app.time_constraints_enabled = time_constraints_enabled
    app.quiet_hours_end_entity = "input_datetime.quiet_hours_end"
    app.quiet_hours_start_entity = "input_datetime.quiet_hours_start"
    app._default_quiet_hours_start = time(22, 0)
    app._default_quiet_hours_end = time(7, 0)
    app._quiet_hours_fallback_active = False

    states = dict(QUIET_HOURS_STATE)
    states.update(state or {})
    room_volumes = dict(volumes or {})

    def get_state(entity_id, attribute=None):
        if attribute == "volume_level":
            return room_volumes.get(entity_id)
        return states.get(entity_id, "off")

    app.get_state = get_state

    fixed_now = now or datetime(2026, 9, 11, 3, 0, 0)
    app.datetime = lambda: fixed_now

    app.calls = []
    app.say_url_response = _say_url_response()
    app.play_result = {"id": 8, "type": "result", "success": True}

    def call_service(service, **kwargs):
        app.calls.append((service, kwargs))
        if service == "chime_tts/say_url":
            if isinstance(app.say_url_response, Exception):
                raise app.say_url_response
            return app.say_url_response
        callback = kwargs.get("callback")
        if callback:
            callback(app.play_result)
        return None

    app.call_service = call_service
    return app


def _renders(app):
    return [kwargs for service, kwargs in app.calls if service == "chime_tts/say_url"]


def _plays(app):
    return [kwargs for service, kwargs in app.calls if service == "media_player/play_media"]


def _logged(app, needle):
    return any(needle in str(a) for a, kw in app.log_calls)


def _logged_at(app, needle, level):
    return any(needle in str(a) and kw.get("level") == level for a, kw in app.log_calls)


class QuietHoursGating(unittest.TestCase):
    """A life-safety announcement at 03:00 (inside quiet hours 22:00-07:30) must not be
    silently dropped when override_quiet_hours=True."""

    def test_quiet_hours_drops_without_override(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=[EXPLICIT])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_override_quiet_hours_still_announces(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=[EXPLICIT], override_quiet_hours=True)
        self.assertEqual(len(_renders(app)), 1)
        self.assertEqual(_renders(app)[0]["message"], "Fire!")
        self.assertEqual(len(_plays(app)), 1)
        self.assertEqual(_plays(app)[0]["entity_id"], [EXPLICIT])

    def test_override_quiet_hours_logs_the_bypass(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=[EXPLICIT], override_quiet_hours=True)
        self.assertTrue(_logged(app, "override_quiet_hours=True"))

    def test_outside_quiet_hours_announces_without_override(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(len(_plays(app)), 1)


class RenderAndPlayPayloads(unittest.TestCase):
    """The announcement is rendered by chime_tts.say_url with the options chime_tts.say used
    to get, and that clip is played as a Music Assistant announcement without its own chime."""

    def test_render_payload(self):
        app = _new_app(now=DAYTIME)
        app.notify("Washer finished", chime_path="/config/www/chimes/washer.mp3", target_speakers=[EXPLICIT])
        self.assertEqual(
            _renders(app),
            [
                {
                    "message": "Washer finished",
                    "chime_path": "/config/www/chimes/washer.mp3",
                    "cache": True,
                    "offset": -300,
                    "tts_platform": "piper",
                    "return_response": True,
                }
            ],
        )

    def test_play_payload(self):
        app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.2})
        app.notify("Washer finished", target_speakers=[EXPLICIT])
        play = dict(_plays(app)[0])
        self.assertTrue(callable(play.pop("callback")))
        self.assertEqual(
            play,
            {
                "entity_id": [EXPLICIT],
                "media_content_id": CLIP_URL,
                "media_content_type": "music",
                "announce": True,
                "extra": {"use_pre_announce": False, "announce_volume": 20},
            },
        )

    def test_render_happens_before_play(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual([service for service, _ in app.calls], ["chime_tts/say_url", "media_player/play_media"])

    def test_default_chime_path_is_used(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_renders(app)[0]["chime_path"], "/config/www/chimes/alert.mp3")

    def test_no_chime_configured_renders_without_one(self):
        app = _new_app(now=DAYTIME)
        app.default_chime_path = None
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertNotIn("chime_path", _renders(app)[0])
        self.assertTrue(_logged(app, "without chime"))

    def test_render_options_can_be_overridden(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT], cache=False, offset=-100, tts_platform="other")
        render = _renders(app)[0]
        self.assertEqual((render["cache"], render["offset"], render["tts_platform"]), (False, -100, "other"))

    def test_no_tts_platform_configured_is_not_sent(self):
        app = _new_app(now=DAYTIME)
        app.tts_platform = None
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertNotIn("tts_platform", _renders(app)[0])

    def test_legacy_player_options_are_ignored(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT], announce=False, fade_audio=False)
        render = _renders(app)[0]
        self.assertNotIn("announce", render)
        self.assertNotIn("fade_audio", render)
        self.assertTrue(_plays(app)[0]["announce"])


class VolumeLevel(unittest.TestCase):
    def test_explicit_volume_is_the_announce_volume_in_percent(self):
        for level, pct in ((1.0, 100), (0.7, 70), (0.15, 15), (0.29, 29)):
            app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.5})
            app.notify("Hello", target_speakers=[EXPLICIT], volume_level=level)
            self.assertEqual(_plays(app)[0]["extra"]["announce_volume"], pct)

    def test_explicit_volume_applies_to_every_room_in_one_call(self):
        app = _new_app(now=DAYTIME, volumes={"media_player.living_room": 0.2, "media_player.kitchen": 0.3})
        app.notify("Hello", target_speakers=["media_player.living_room", "media_player.kitchen"], volume_level=0.7)
        plays = _plays(app)
        self.assertEqual(len(plays), 1)
        self.assertEqual(plays[0]["entity_id"], ["media_player.living_room", "media_player.kitchen"])
        self.assertEqual(plays[0]["extra"]["announce_volume"], 70)

    def test_omitted_volume_announces_at_each_rooms_current_volume(self):
        app = _new_app(now=DAYTIME, volumes={"media_player.living_room": 0.2, "media_player.kitchen": 0.24})
        app.notify("Hello", target_speakers=["media_player.living_room", "media_player.kitchen"])
        announced = {tuple(p["entity_id"]): p["extra"]["announce_volume"] for p in _plays(app)}
        self.assertEqual(
            announced,
            {("media_player.living_room",): 20, ("media_player.kitchen",): 24},
        )

    def test_rooms_at_the_same_volume_share_one_call(self):
        app = _new_app(
            now=DAYTIME,
            volumes={"media_player.living_room": 0.18, "media_player.kitchen": 0.18, "media_player.bedroom": 0.24},
        )
        app.notify(
            "Hello",
            target_speakers=["media_player.living_room", "media_player.kitchen", "media_player.bedroom"],
        )
        announced = {tuple(p["entity_id"]): p["extra"]["announce_volume"] for p in _plays(app)}
        self.assertEqual(
            announced,
            {("media_player.living_room", "media_player.kitchen"): 18, ("media_player.bedroom",): 24},
        )

    def test_unreadable_current_volume_sends_no_announce_volume(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app)[0]["extra"], {"use_pre_announce": False})

    def test_volume_is_never_set_on_the_player(self):
        app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.2})
        app.notify("Hello", target_speakers=[EXPLICIT], volume_level=0.7)
        self.assertEqual({service for service, _ in app.calls}, {"chime_tts/say_url", "media_player/play_media"})


class DeliveryFailures(unittest.TestCase):
    def test_render_without_url_plays_nothing_and_logs_error(self):
        app = _new_app(now=DAYTIME)
        app.say_url_response = _say_url_response(url=None, success=False)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app), [])
        self.assertTrue(_logged_at(app, "produced no audio", "ERROR"))

    def test_render_marked_unsuccessful_plays_nothing(self):
        app = _new_app(now=DAYTIME)
        app.say_url_response = _say_url_response(success=False)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app), [])

    def test_render_error_response_plays_nothing(self):
        app = _new_app(now=DAYTIME)
        app.say_url_response = {"success": False, "error": {"code": "unknown_error", "message": "boom"}}
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app), [])
        self.assertTrue(_logged_at(app, "produced no audio", "ERROR"))

    def test_no_render_response_plays_nothing(self):
        app = _new_app(now=DAYTIME)
        app.say_url_response = None
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app), [])
        self.assertTrue(_logged_at(app, "produced no audio", "ERROR"))

    def test_render_exception_plays_nothing_and_logs_error(self):
        app = _new_app(now=DAYTIME)
        app.say_url_response = RuntimeError("unreachable")
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app), [])
        self.assertTrue(_logged_at(app, "Error calling chime_tts/say_url", "ERROR"))

    def test_successful_playback_is_logged_with_its_volume(self):
        app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.2})
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_logged_at(app, "Announced 'Hello' on ['media_player.explicit'] (announce volume 20%)", "INFO"))

    def test_playback_without_a_volume_is_logged_as_such(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_logged_at(app, "(no announce volume)", "INFO"))

    def test_failed_playback_is_logged_as_error(self):
        app = _new_app(now=DAYTIME)
        app.play_result = {"success": False, "error": {"code": "home_assistant_error", "message": "no player"}}
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_logged_at(app, "failed", "ERROR"))
        self.assertFalse(_logged(app, "Announced 'Hello'"))

    def test_playback_without_a_result_is_logged_as_error(self):
        app = _new_app(now=DAYTIME)
        app.play_result = None
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_logged_at(app, "failed", "ERROR"))


class TargetResolution(unittest.TestCase):
    def test_nobody_sleeping_uses_the_all_group(self):
        app = _new_app(now=DAYTIME, volumes={r: 0.2 for r in _new_app().tts_group_all})
        app.notify("Hello")
        self.assertEqual(_plays(app)[0]["entity_id"], app.tts_group_all)

    def test_everybody_sleeping_uses_family_rooms_only(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "on"},
            now=DAYTIME,
            volumes={r: 0.2 for r in _new_app().tts_group_all},
        )
        app.notify("Hello")
        self.assertEqual(_plays(app)[0]["entity_id"], app.tts_group_family_rooms)

    def test_kristine_sleeping_adds_the_ms_room(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "off"},
            now=DAYTIME,
            volumes={r: 0.2 for r in _new_app().tts_group_all},
        )
        app.notify("Hello")
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.living_room", "media_player.kitchen", "media_player.bedroom"])

    def test_mikkel_sleeping_adds_kristines_room(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "off", "input_boolean.mikkel_sleep_mode": "on"},
            now=DAYTIME,
            volumes={r: 0.2 for r in _new_app().tts_group_all},
        )
        app.notify("Hello")
        self.assertEqual(
            _plays(app)[0]["entity_id"],
            ["media_player.living_room", "media_player.kitchen", "media_player.kristines_room"],
        )

    def test_explicit_targets_win_over_sleep_mode(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "on"},
            now=DAYTIME,
        )
        app.notify("Hello", target_speakers="media_player.kitchen")
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.kitchen"])

    def test_targets_are_deduplicated_in_order(self):
        app = _new_app(now=DAYTIME)
        app.notify(
            "Hello",
            target_speakers=["media_player.kitchen", None, "media_player.living_room", "media_player.kitchen", ""],
        )
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.kitchen", "media_player.living_room"])

    def test_entity_ids_are_passed_on_as_given(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=["media_player.kitchen_2"])
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.kitchen_2"])

    def test_no_targets_skips_the_announcement(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged_at(app, "No target speakers", "ERROR"))

    def test_entity_list_normalisation(self):
        self.assertEqual(sn.SonosNotifier._entity_list(None), [])
        self.assertEqual(sn.SonosNotifier._entity_list("media_player.a"), ["media_player.a"])
        self.assertEqual(sn.SonosNotifier._entity_list(["media_player.a", "media_player.a"]), ["media_player.a"])


class QuietHoursHelperFailSafe(unittest.TestCase):
    """When the quiet-hours helpers are unavailable/unparseable, fall back to configured
    defaults (22:00-07:00) instead of failing open (playing house-wide at night)."""

    UNAVAILABLE = {
        "input_datetime.quiet_hours_start": "unavailable",
        "input_datetime.quiet_hours_end": "unavailable",
    }

    def test_helpers_unavailable_at_2330_suppressed(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("Doorbell", target_speakers=[EXPLICIT])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_helpers_unavailable_at_1400_plays_normally(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 14, 0, 0))
        app.notify("Doorbell", target_speakers=[EXPLICIT])
        self.assertEqual(len(_plays(app)), 1)

    def test_unparseable_helper_value_uses_default(self):
        app = _new_app(
            state={"input_datetime.quiet_hours_start": "not-a-time"},
            now=datetime(2026, 9, 11, 23, 0, 0),
        )
        app.notify("Doorbell", target_speakers=[EXPLICIT])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_warning_logged_once_across_two_announcements(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("First", target_speakers=[EXPLICIT])
        app.notify("Second", target_speakers=[EXPLICIT])
        warnings = [1 for a, kw in app.log_calls if "Falling back" in str(a)]
        self.assertEqual(len(warnings), 1)

    def test_recovery_logs_info_once_and_does_not_repeat(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("First", target_speakers=[EXPLICIT])
        app.get_state = lambda entity_id, attribute=None: dict(QUIET_HOURS_STATE).get(entity_id, "off")
        app.notify("Second", target_speakers=[EXPLICIT])
        app.notify("Third", target_speakers=[EXPLICIT])
        recoveries = [1 for a, kw in app.log_calls if "readable again" in str(a)]
        self.assertEqual(len(recoveries), 1)

    def test_override_quiet_hours_plays_despite_unavailable_helpers_at_night(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("Fire!", target_speakers=[EXPLICIT], override_quiet_hours=True)
        self.assertEqual(len(_plays(app)), 1)

    def test_healthy_helpers_unchanged_no_fallback_logging(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(len(_plays(app)), 1)
        self.assertFalse(_logged(app, "Falling back"))
        self.assertFalse(_logged(app, "readable again"))


class SleepModeEntityFailSafe(unittest.TestCase):
    """An unavailable sleep-mode entity is only assumed 'sleeping' while inside quiet
    hours; outside quiet hours it is treated as not sleeping."""

    def test_unavailable_sleep_entity_at_night_treated_as_sleeping(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "unavailable", "input_boolean.mikkel_sleep_mode": "off"},
            now=datetime(2026, 9, 11, 3, 0, 0),
        )
        app.notify("Doorbell", override_quiet_hours=True)
        self.assertEqual(len(_plays(app)), 1)
        self.assertEqual(_plays(app)[0]["entity_id"], app.tts_group_family_rooms + app.tts_group_ms)

    def test_unavailable_sleep_entity_during_day_unchanged(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "unavailable", "input_boolean.mikkel_sleep_mode": "off"},
            now=DAYTIME,
        )
        app.notify("Doorbell")
        self.assertEqual(len(_plays(app)), 1)
        self.assertEqual(_plays(app)[0]["entity_id"], app.tts_group_all)


if __name__ == "__main__":
    unittest.main()
