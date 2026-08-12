"""bedroom_solar_shade with the blind owner loaded - and, crucially, without it.

The shade DROPS its private manual detection and pause when the owner exists:
_on_cover_change stands down, _manual_pause_active asks the owner, and
_command_blind submits a "shade" request instead of writing the cover. Every
single one of those must degrade to the byte-identical pre-owner behaviour when
the owner is absent, half-loaded, or raising - that path is what keeps the blind
working when a deploy goes sideways.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import bedroom_solar_shade as bss  # noqa: E402

NOW = datetime(2026, 8, 12, 12, 0, tzinfo=timezone.utc)


class FakeOwner:
    def __init__(self, granted=True, winner="shade", hold=False, until=None, boom=False):
        self.granted = granted
        self.winner = winner
        self.hold = hold
        self.until = until
        self.boom = boom
        self.requests = []

    def request(self, source, position, reason="", **kw):
        if self.boom:
            raise RuntimeError("owner exploded")
        self.requests.append((source, position, reason))
        return {"granted": self.granted, "winner": self.winner, "position": position,
                "manual_until": self.until.isoformat() if self.until else None}

    def manual_hold_active(self):
        if self.boom:
            raise RuntimeError("owner exploded")
        return self.hold

    def manual_hold_until(self):
        if self.boom:
            raise RuntimeError("owner exploded")
        return self.until


def make_shade(owner=..., get_app_raises=False):
    app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
    app.cover = "cover.bedroom_blind"
    app._override_until = None
    app._last_cmd = None
    app._save_state = MagicMock()
    app.log_lines = []
    app.log = lambda msg, **kw: app.log_lines.append(str(msg))
    app.call_service_calls = []
    app.call_service = lambda service, **kw: app.call_service_calls.append((service, kw))
    if owner is not ...:
        app.owner_app = "BedroomBlindOwner"
        if get_app_raises:
            def get_app(name):
                raise RuntimeError("registry down")
        else:
            def get_app(name):
                return owner if name == "BedroomBlindOwner" else None
        app.get_app = get_app
    return app


class ManualPauseIsAskedOfTheOwner(unittest.TestCase):
    def test_owner_hold_pauses_shading(self):
        app = make_shade(FakeOwner(hold=True))
        self.assertTrue(app._manual_pause_active(NOW))

    def test_owner_no_hold_means_no_pause_even_with_stale_local_override(self):
        """The owner is the single holder: a leftover local deadline (pre-owner
        state file) must not double-govern once the owner answers."""
        app = make_shade(FakeOwner(hold=False))
        app._override_until = NOW + timedelta(minutes=30)
        self.assertFalse(app._manual_pause_active(NOW))

    def test_pause_until_comes_from_the_owner(self):
        until = NOW + timedelta(minutes=45)
        app = make_shade(FakeOwner(hold=True, until=until))
        self.assertEqual(app._manual_pause_until(), until)

    def test_owner_raising_falls_back_to_the_local_deadline(self):
        app = make_shade(FakeOwner(boom=True))
        app._override_until = NOW + timedelta(minutes=30)
        self.assertTrue(app._manual_pause_active(NOW))
        self.assertEqual(app._manual_pause_until(), app._override_until)

    def test_no_owner_at_all_is_the_pre_owner_behaviour(self):
        app = make_shade()  # owner_app attribute never set
        app._override_until = NOW + timedelta(minutes=30)
        self.assertTrue(app._manual_pause_active(NOW))
        self.assertFalse(app._manual_pause_active(NOW + timedelta(minutes=31)))


class PrivateDetectionStandsDown(unittest.TestCase):
    def test_cover_change_is_ignored_while_the_owner_exists(self):
        app = make_shade(FakeOwner())
        # No run_in / get_now / settle machinery on this app object: reaching any
        # of it would raise, so returning cleanly proves the early hand-off.
        app._on_cover_change("cover.bedroom_blind", "current_position", 38, "100", {})
        self.assertIsNone(app._override_until)
        app._save_state.assert_not_called()

    def test_detection_still_runs_without_the_owner(self):
        app = make_shade()
        app.pos_tol = 6
        app.manual_pause_min = 120
        app.manual_settle_s = 12
        app._manual_settle_handle = None
        app._manual_from_pos = None
        app._last_cmd = 38
        app.get_now = lambda: NOW
        app.scheduled = []
        app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw)) or object()
        app._on_cover_change("cover.bedroom_blind", "current_position", 38, "100", {})
        self.assertIsNotNone(app._override_until)


class CommandBlindGoesThroughTheOwner(unittest.TestCase):
    DIRECT = ("cover/set_cover_position", {"entity_id": "cover.bedroom_blind", "position": 92})

    def test_granted_submit_records_the_baseline_and_does_not_write(self):
        owner = FakeOwner(granted=True)
        app = make_shade(owner)
        app._command_blind(92, "blocking sun heat")
        self.assertEqual(owner.requests, [("shade", 92, "blocking sun heat")])
        self.assertEqual(app.call_service_calls, [])  # the OWNER writes, not this app
        self.assertEqual(app._last_cmd, 92)
        app._save_state.assert_called_once()
        self.assertTrue(any("-> 92%" in l for l in app.log_lines), app.log_lines)

    def test_refused_submit_neither_writes_nor_moves_the_baseline(self):
        app = make_shade(FakeOwner(granted=False, winner="wake"))
        app._command_blind(92, "blocking sun heat")
        self.assertEqual(app.call_service_calls, [])
        self.assertIsNone(app._last_cmd)
        self.assertTrue(any("owner gave it to wake" in l for l in app.log_lines), app.log_lines)

    def test_owner_raising_falls_back_to_the_direct_write(self):
        app = make_shade(FakeOwner(boom=True))
        app._command_blind(92, "hot")
        self.assertEqual(app.call_service_calls, [self.DIRECT])
        self.assertEqual(app._last_cmd, 92)

    def test_get_app_raising_falls_back_to_the_direct_write(self):
        app = make_shade(FakeOwner(), get_app_raises=True)
        app._command_blind(92, "hot")
        self.assertEqual(app.call_service_calls, [self.DIRECT])

    def test_half_loaded_owner_without_request_is_treated_as_absent(self):
        app = make_shade(types.SimpleNamespace())  # partial deploy: object, no API
        app._command_blind(92, "hot")
        self.assertEqual(app.call_service_calls, [self.DIRECT])


if __name__ == "__main__":
    unittest.main()
