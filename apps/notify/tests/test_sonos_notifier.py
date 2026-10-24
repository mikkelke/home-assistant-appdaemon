# tests/test_sonos_notifier.py - Unit tests for SonosNotifier: quiet-hours / sleep-mode gating,
# target resolution to room entities, and delivery as a chime_tts.say_url render played with
# media_player.play_media announce on each room's native Sonos twin.
# Run from repo root: python3 -m unittest discover -s apps/notify/tests -q

import sys
import types
import unittest
from datetime import datetime, time
from itertools import product
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
EXPLICIT_TWIN = "media_player.explicit_2"
DAYTIME = datetime(2026, 9, 11, 12, 0, 0)
AUDIO_CONVERSION = "-af loudnorm=I=-16:TP=-2:LRA=11"


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


def _new_app(state=None, now=None, time_constraints_enabled=True, volumes=None, announce_volume_boost=0.03, announce_volume_cap=0.30):
    app = sn.SonosNotifier.__new__(sn.SonosNotifier)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.log_level = "INFO"

    app.kristine_sleep_mode_entity = "input_boolean.kristine_sleep_mode"
    app.mikkel_sleep_mode_entity = "input_boolean.mikkel_sleep_mode"
    app.claudia_sleep_mode_entity = "input_boolean.claudia_sleep_mode"
    app.tts_group_all = [
        "media_player.living_room", "media_player.kitchen", "media_player.dining_room",
        "media_player.bedroom", "media_player.bathroom", "media_player.guest_bathroom",
        "media_player.kristines_room", "media_player.claudias_room", "media_player.rooftop",
    ]
    app.tts_group_family_rooms = [
        "media_player.living_room", "media_player.kitchen", "media_player.dining_room",
        "media_player.bathroom", "media_player.guest_bathroom", "media_player.rooftop",
    ]
    app.tts_group_kristine = ["media_player.kristines_room"]
    app.tts_group_ms = ["media_player.bedroom"]
    app.tts_group_claudia = ["media_player.claudias_room"]
    app.default_chime_path = "/config/www/chimes/alert.mp3"
    app.tts_platform = "piper"
    app.audio_conversion = AUDIO_CONVERSION
    app.announce_volume_boost = announce_volume_boost
    app.announce_volume_cap = announce_volume_cap
    app.clip_base_url = ""

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


def _twins(rooms):
    return [f"{room}_2" for room in rooms]


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
        self.assertEqual(_plays(app)[0]["entity_id"], [EXPLICIT_TWIN])

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
    to get, and that clip is played as an announcement on the native Sonos twins."""

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
                    "audio_conversion": AUDIO_CONVERSION,
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
                "entity_id": [EXPLICIT_TWIN],
                "media_content_id": CLIP_URL,
                "media_content_type": "music",
                "announce": True,
                "extra": {"volume": 23},
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

    def test_audio_conversion_is_sent_with_every_render(self):
        app = _new_app(now=DAYTIME)
        app.notify("First", target_speakers=[EXPLICIT])
        app.notify("Second", chime_path="/config/www/chimes/other.mp3", target_speakers=[EXPLICIT], volume_level=0.7)
        self.assertEqual([r["audio_conversion"] for r in _renders(app)], [AUDIO_CONVERSION, AUDIO_CONVERSION])
        self.assertTrue(_logged(app, f"audio_conversion='{AUDIO_CONVERSION}'"))

    def test_no_audio_conversion_configured_is_not_sent(self):
        for value in (None, ""):
            app = _new_app(now=DAYTIME)
            app.audio_conversion = value
            app.notify("Hello", target_speakers=[EXPLICIT])
            self.assertNotIn("audio_conversion", _renders(app)[0])

    def test_legacy_player_options_are_ignored(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT], announce=False, fade_audio=False)
        render = _renders(app)[0]
        self.assertNotIn("announce", render)
        self.assertNotIn("fade_audio", render)
        self.assertTrue(_plays(app)[0]["announce"])


class ClipUrl(unittest.TestCase):
    def test_clip_is_played_from_the_base_url(self):
        app = _new_app(now=DAYTIME)
        app.clip_base_url = "http://10.0.0.5:8123"
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app)[0]["media_content_id"], "http://10.0.0.5:8123/local/chime_tts/abc123.mp3")

    def test_query_is_kept(self):
        app = _new_app(now=DAYTIME)
        app.clip_base_url = "http://10.0.0.5:8123"
        app.say_url_response = _say_url_response(url="https://ha.example/local/x.mp3?authSig=abc")
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app)[0]["media_content_id"], "http://10.0.0.5:8123/local/x.mp3?authSig=abc")

    def test_no_base_url_plays_the_rendered_url(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app)[0]["media_content_id"], CLIP_URL)


class VolumeLevel(unittest.TestCase):
    ROOMS = ["media_player.living_room", "media_player.kitchen", "media_player.bathroom"]

    def _announced(self, app):
        return {tuple(p["entity_id"]): p["extra"].get("volume") for p in _plays(app)}

    def test_each_room_announces_a_boost_above_its_own_volume(self):
        app = _new_app(now=DAYTIME, volumes={"media_player.living_room": 0.14, "media_player.kitchen": 0.13, "media_player.bathroom": 0.2})
        app.notify("Hello", target_speakers=self.ROOMS)
        self.assertEqual(
            self._announced(app),
            {("media_player.living_room_2",): 17, ("media_player.kitchen_2",): 16, ("media_player.bathroom_2",): 23},
        )

    def test_rooms_at_the_same_volume_share_one_call(self):
        app = _new_app(now=DAYTIME, volumes={r: 0.12 for r in self.ROOMS})
        app.notify("Hello", target_speakers=self.ROOMS)
        self.assertEqual(self._announced(app), {tuple(_twins(self.ROOMS)): 15})

    def test_the_cap_limits_the_boosted_volume(self):
        for music, pct in ((0.26, 29), (0.27, 30), (0.28, 30), (0.6, 30), (1.0, 30)):
            app = _new_app(now=DAYTIME, volumes={EXPLICIT: music})
            app.notify("Hello", target_speakers=[EXPLICIT])
            self.assertEqual(_plays(app)[0]["extra"], {"volume": pct})

    def test_boost_and_cap_follow_the_configuration(self):
        app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.2}, announce_volume_boost=0.05, announce_volume_cap=0.22)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertEqual(_plays(app)[0]["extra"], {"volume": 22})

    def test_no_usable_boost_announces_at_the_room_volume(self):
        for boost in (None, "", "loud"):
            app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.14}, announce_volume_boost=boost)
            app.notify("Hello", target_speakers=[EXPLICIT])
            self.assertEqual(_plays(app)[0]["extra"], {"volume": 14})

    def test_no_usable_cap_leaves_the_boosted_volume_uncapped(self):
        for cap in (None, "", "loud"):
            app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.9}, announce_volume_cap=cap)
            app.notify("Hello", target_speakers=[EXPLICIT])
            self.assertEqual(_plays(app)[0]["extra"], {"volume": 93})

    def test_unreadable_room_volume_sends_no_volume(self):
        app = _new_app(now=DAYTIME, volumes={"media_player.living_room": 0.14})
        app.notify("Hello", target_speakers=self.ROOMS)
        self.assertEqual(
            self._announced(app),
            {("media_player.living_room_2",): 17, ("media_player.kitchen_2", "media_player.bathroom_2"): None},
        )

    def test_explicit_volume_is_used_as_given_in_one_call(self):
        for level, pct in ((1.0, 100), (0.7, 70), (0.15, 15), (0.36, 36)):
            app = _new_app(now=DAYTIME, volumes={"media_player.living_room": 0.6, "media_player.kitchen": 0.1})
            app.notify("Hello", target_speakers=self.ROOMS, volume_level=level)
            self.assertEqual(self._announced(app), {tuple(_twins(self.ROOMS)): pct})

    def test_volume_is_never_set_on_the_player(self):
        app = _new_app(now=DAYTIME, volumes={EXPLICIT: 0.9})
        app.notify("Hello", target_speakers=[EXPLICIT])
        app.notify("Hello", target_speakers=[EXPLICIT], volume_level=0.7)
        self.assertEqual({service for service, _ in app.calls}, {"chime_tts/say_url", "media_player/play_media"})


class ShippedConfig(unittest.TestCase):
    """The yaml next to the app carries the keys the app reads, in the units it reads them."""

    @classmethod
    def setUpClass(cls):
        try:
            import yaml
        except ImportError:
            raise unittest.SkipTest("PyYAML not installed")
        with open(Path(__file__).resolve().parents[1] / "sonos_notifier.yaml") as f:
            cls.args = next(iter(yaml.safe_load(f).values()))

    def _initialized_app(self, args=None, **kwargs):
        app = _new_app(now=DAYTIME, **kwargs)
        app.args = self.args if args is None else args
        app.initialize()
        return app

    def test_clip_is_fetched_over_the_lan(self):
        app = self._initialized_app()
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_plays(app)[0]["media_content_id"].startswith("http://10.21.0.5:8123/local/chime_tts/"))

    def test_loudness_filter_reaches_the_render(self):
        app = self._initialized_app()
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertIn("alimiter", self.args["audio_conversion"])
        self.assertNotIn("loudnorm", self.args["audio_conversion"])
        self.assertEqual(_renders(app)[0]["audio_conversion"], self.args["audio_conversion"])

    def test_boost_and_cap_reach_the_announce_volume(self):
        boost, cap = self.args["announce_volume_boost"], self.args["announce_volume_cap"]
        self.assertTrue(0 <= boost < cap < 1)
        app = self._initialized_app(volumes={"media_player.kitchen": 0.13, "media_player.living_room": 1.0})
        app.notify("Hello", target_speakers=["media_player.kitchen", "media_player.living_room"])
        announced = {tuple(p["entity_id"]): p["extra"].get("volume") for p in _plays(app)}
        self.assertEqual(
            announced,
            {("media_player.kitchen_2",): 13 + round(boost * 100), ("media_player.living_room_2",): round(cap * 100)},
        )
        self.assertFalse(_logged_at(app, "announce_volume_cap", "WARNING"))

    def test_a_missing_cap_is_warned_about(self):
        args = {k: v for k, v in self.args.items() if k != "announce_volume_cap"}
        app = self._initialized_app(args=args)
        self.assertTrue(_logged_at(app, "announce_volume_cap", "WARNING"))

    def test_default_routing_protects_each_residents_room(self):
        for mikkel, kristine, claudia in product(("off", "on"), repeat=3):
            with self.subTest(mikkel=mikkel, kristine=kristine, claudia=claudia):
                app = self._initialized_app(state={
                    "input_boolean.mikkel_sleep_mode": mikkel,
                    "input_boolean.kristine_sleep_mode": kristine,
                    "input_boolean.claudia_sleep_mode": claudia,
                })
                app.notify("Washer finished")
                rooms = {entity for call in _plays(app) for entity in call["entity_id"]}
                expected = {
                    "media_player.living_room_2", "media_player.kitchen_2", "media_player.dining_room_2",
                    "media_player.bathroom_2", "media_player.guest_bathroom_2", "media_player.rooftop_2",
                }
                for sleeping, room in (
                    (mikkel, "media_player.bedroom_2"),
                    (kristine, "media_player.kristines_room_2"),
                    (claudia, "media_player.claudias_room_2"),
                ):
                    if sleeping == "off":
                        expected.add(room)
                self.assertEqual(rooms, expected)


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
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT], volume_level=0.2)
        self.assertTrue(_logged_at(app, "Announced 'Hello' on ['media_player.explicit_2'] (announce volume 20%)", "INFO"))

    def test_playback_without_a_volume_is_logged_as_such(self):
        app = _new_app(now=DAYTIME)
        app.notify("Hello", target_speakers=[EXPLICIT])
        self.assertTrue(_logged_at(app, "(own volume)", "INFO"))

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
        app = _new_app(now=DAYTIME)
        app.notify("Hello")
        self.assertEqual(_plays(app)[0]["entity_id"], _twins(app.tts_group_all))

    def test_everybody_sleeping_uses_family_rooms_only(self):
        app = _new_app(
            state={
                "input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "on",
                "input_boolean.claudia_sleep_mode": "on",
            },
            now=DAYTIME,
        )
        app.notify("Hello")
        self.assertEqual(_plays(app)[0]["entity_id"], _twins(app.tts_group_family_rooms))

    def test_kristine_sleeping_adds_only_awake_private_rooms(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "off"},
            now=DAYTIME,
        )
        app.notify("Hello")
        self.assertEqual(
            _plays(app)[0]["entity_id"], _twins(app.tts_group_family_rooms + app.tts_group_ms + app.tts_group_claudia)
        )

    def test_mikkel_sleeping_adds_only_awake_private_rooms(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "off", "input_boolean.mikkel_sleep_mode": "on"},
            now=DAYTIME,
        )
        app.notify("Hello")
        self.assertEqual(
            _plays(app)[0]["entity_id"],
            _twins(app.tts_group_family_rooms + app.tts_group_kristine + app.tts_group_claudia),
        )

    def test_claudia_sleeping_adds_only_awake_private_rooms(self):
        app = _new_app(state={"input_boolean.claudia_sleep_mode": "on"}, now=DAYTIME)
        app.notify("Hello")
        self.assertEqual(
            _plays(app)[0]["entity_id"], _twins(app.tts_group_family_rooms + app.tts_group_ms + app.tts_group_kristine)
        )

    def test_explicit_alarm_targets_still_reach_all_sleeping_residents(self):
        app = _new_app(state={
            f"input_boolean.{name}_sleep_mode": "on" for name in ("mikkel", "kristine", "claudia")
        })
        app.notify("Fire!", target_speakers=app.tts_group_all, override_quiet_hours=True)
        self.assertEqual(_plays(app)[0]["entity_id"], _twins(app.tts_group_all))

    def test_explicit_target_can_reach_sleeping_claudias_room(self):
        app = _new_app(state={"input_boolean.claudia_sleep_mode": "on"}, now=DAYTIME)
        app.notify("Hello", target_speakers="media_player.claudias_room")
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.claudias_room_2"])

    def test_explicit_targets_win_over_sleep_mode(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "on", "input_boolean.mikkel_sleep_mode": "on"},
            now=DAYTIME,
        )
        app.notify("Hello", target_speakers="media_player.kitchen")
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.kitchen_2"])

    def test_targets_are_deduplicated_in_order(self):
        app = _new_app(now=DAYTIME)
        app.notify(
            "Hello",
            target_speakers=["media_player.kitchen", None, "media_player.living_room", "media_player.kitchen", ""],
        )
        self.assertEqual(_plays(app)[0]["entity_id"], ["media_player.kitchen_2", "media_player.living_room_2"])

    def test_a_native_twin_is_passed_on_as_given(self):
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
        self.assertEqual(
            _plays(app)[0]["entity_id"], _twins(app.tts_group_family_rooms + app.tts_group_ms + app.tts_group_claudia)
        )

    def test_unavailable_sleep_entity_during_day_unchanged(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "unavailable", "input_boolean.mikkel_sleep_mode": "off"},
            now=DAYTIME,
        )
        app.notify("Doorbell")
        self.assertEqual(len(_plays(app)), 1)
        self.assertEqual(_plays(app)[0]["entity_id"], _twins(app.tts_group_all))

    def test_each_unknown_sleep_helper_follows_existing_time_policy(self):
        for name, room in (
            ("mikkel", "media_player.bedroom_2"),
            ("kristine", "media_player.kristines_room_2"),
            ("claudia", "media_player.claudias_room_2"),
        ):
            for state in (None, "unknown", "unavailable"):
                for night in (False, True):
                    with self.subTest(name=name, state=state, night=night):
                        app = _new_app(
                            state={f"input_boolean.{name}_sleep_mode": state},
                            now=datetime(2026, 9, 11, 3, 0, 0) if night else DAYTIME,
                        )
                        app.notify("Doorbell", override_quiet_hours=night)
                        targets = {entity for call in _plays(app) for entity in call["entity_id"]}
                        self.assertEqual(room in targets, not night)


if __name__ == "__main__":
    unittest.main()
