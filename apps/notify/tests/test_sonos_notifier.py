# tests/test_sonos_notifier.py - Unit tests for SonosNotifier's fire-safety additions
# (2026-09-11): override_quiet_hours skips the quiet-hours early return (still logged so
# the bypass is auditable), and volume_level passes through to chime_tts/say only when given.
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


def _new_app(state=None, now=None, time_constraints_enabled=True):
    app = sn.SonosNotifier.__new__(sn.SonosNotifier)
    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.log_level = "INFO"

    app.kristine_sleep_mode_entity = "input_boolean.kristine_sleep_mode"
    app.mikkel_sleep_mode_entity = "input_boolean.mikkel_sleep_mode"
    app.tts_group_all = "media_player.tts_all"
    app.tts_group_family_rooms = "media_player.tts_family_rooms"
    app.tts_group_kristine = "media_player.tts_kristine"
    app.tts_group_ms = "media_player.tts_ms"
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
    app.get_state = lambda entity_id: states.get(entity_id, "off")

    fixed_now = now or datetime(2026, 9, 11, 3, 0, 0)
    app.datetime = lambda: fixed_now

    app.calls = []
    app.call_service = lambda service, **kwargs: app.calls.append((service, kwargs))
    return app


def _logged(app, needle):
    return any(needle in str(a) for a, kw in app.log_calls)


class QuietHoursGating(unittest.TestCase):
    """A life-safety announcement at 03:00 (inside quiet hours 22:00-07:30) must not be
    silently dropped when override_quiet_hours=True."""

    def test_quiet_hours_drops_without_override(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=["media_player.explicit"])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_override_quiet_hours_still_announces(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=["media_player.explicit"], override_quiet_hours=True)
        self.assertEqual(len(app.calls), 1)
        service, kwargs = app.calls[0]
        self.assertEqual(service, "chime_tts/say")
        self.assertEqual(kwargs["message"], "Fire!")

    def test_override_quiet_hours_logs_the_bypass(self):
        app = _new_app()
        app.notify("Fire!", target_speakers=["media_player.explicit"], override_quiet_hours=True)
        self.assertTrue(_logged(app, "override_quiet_hours=True"))

    def test_outside_quiet_hours_announces_without_override(self):
        app = _new_app(now=datetime(2026, 9, 11, 12, 0, 0))
        app.notify("Hello", target_speakers=["media_player.explicit"])
        self.assertEqual(len(app.calls), 1)


class VolumeLevelPassthrough(unittest.TestCase):
    def test_volume_level_passed_through_when_given(self):
        app = _new_app(now=datetime(2026, 9, 11, 12, 0, 0))
        app.notify("Hello", target_speakers=["media_player.explicit"], volume_level=1.0)
        _, kwargs = app.calls[0]
        self.assertEqual(kwargs["volume_level"], 1.0)

    def test_volume_level_omitted_when_not_given(self):
        app = _new_app(now=datetime(2026, 9, 11, 12, 0, 0))
        app.notify("Hello", target_speakers=["media_player.explicit"])
        _, kwargs = app.calls[0]
        self.assertNotIn("volume_level", kwargs)


class LegacyCallUnchanged(unittest.TestCase):
    """A pre-existing call site (no new kwargs) must keep sending a byte-identical
    service_data payload to chime_tts/say."""

    def test_legacy_call_payload_matches_prior_shape(self):
        app = _new_app(now=datetime(2026, 9, 11, 12, 0, 0))
        app.notify(
            "Washer finished",
            chime_path="/config/www/chimes/washer.mp3",
            target_speakers=["media_player.explicit"],
        )
        service, kwargs = app.calls[0]
        self.assertEqual(service, "chime_tts/say")
        self.assertEqual(
            kwargs,
            {
                "entity_id": ["media_player.explicit"],
                "message": "Washer finished",
                "chime_path": "/config/www/chimes/washer.mp3",
                "cache": True,
                "announce": True,
                "fade_audio": True,
                "offset": -300,
                "tts_platform": "piper",
            },
        )


class QuietHoursHelperFailSafe(unittest.TestCase):
    """When the quiet-hours helpers are unavailable/unparseable, fall back to configured
    defaults (22:00-07:00) instead of failing open (playing house-wide at night)."""

    UNAVAILABLE = {
        "input_datetime.quiet_hours_start": "unavailable",
        "input_datetime.quiet_hours_end": "unavailable",
    }

    def test_helpers_unavailable_at_2330_suppressed(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("Doorbell", target_speakers=["media_player.explicit"])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_helpers_unavailable_at_1400_plays_normally(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 14, 0, 0))
        app.notify("Doorbell", target_speakers=["media_player.explicit"])
        self.assertEqual(len(app.calls), 1)

    def test_unparseable_helper_value_uses_default(self):
        app = _new_app(
            state={"input_datetime.quiet_hours_start": "not-a-time"},
            now=datetime(2026, 9, 11, 23, 0, 0),
        )
        app.notify("Doorbell", target_speakers=["media_player.explicit"])
        self.assertEqual(app.calls, [])
        self.assertTrue(_logged(app, "skipped"))

    def test_warning_logged_once_across_two_announcements(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("First", target_speakers=["media_player.explicit"])
        app.notify("Second", target_speakers=["media_player.explicit"])
        warnings = [1 for a, kw in app.log_calls if "Falling back" in str(a)]
        self.assertEqual(len(warnings), 1)

    def test_recovery_logs_info_once_and_does_not_repeat(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("First", target_speakers=["media_player.explicit"])
        app.get_state = lambda entity_id: dict(QUIET_HOURS_STATE).get(entity_id, "off")
        app.notify("Second", target_speakers=["media_player.explicit"])
        app.notify("Third", target_speakers=["media_player.explicit"])
        recoveries = [1 for a, kw in app.log_calls if "readable again" in str(a)]
        self.assertEqual(len(recoveries), 1)

    def test_override_quiet_hours_plays_despite_unavailable_helpers_at_night(self):
        app = _new_app(state=self.UNAVAILABLE, now=datetime(2026, 9, 11, 23, 30, 0))
        app.notify("Fire!", target_speakers=["media_player.explicit"], override_quiet_hours=True)
        self.assertEqual(len(app.calls), 1)

    def test_healthy_helpers_unchanged_no_fallback_logging(self):
        app = _new_app(now=datetime(2026, 9, 11, 12, 0, 0))
        app.notify("Hello", target_speakers=["media_player.explicit"])
        self.assertEqual(len(app.calls), 1)
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
        self.assertEqual(len(app.calls), 1)
        _, kwargs = app.calls[0]
        self.assertEqual(kwargs["entity_id"], [app.tts_group_family_rooms, app.tts_group_ms])

    def test_unavailable_sleep_entity_during_day_unchanged(self):
        app = _new_app(
            state={"input_boolean.kristine_sleep_mode": "unavailable", "input_boolean.mikkel_sleep_mode": "off"},
            now=datetime(2026, 9, 11, 12, 0, 0),
        )
        app.notify("Doorbell")
        self.assertEqual(len(app.calls), 1)
        _, kwargs = app.calls[0]
        self.assertEqual(kwargs["entity_id"], [app.tts_group_all])


if __name__ == "__main__":
    unittest.main()
