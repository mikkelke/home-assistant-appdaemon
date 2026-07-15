from __future__ import annotations

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

import smart_cooling as sc  # noqa: E402


def make_app(**overrides):
    """SmartCooling instance without running AppDaemon's initialize() -
    _attrs() only reads a handful of instance attributes."""
    app = sc.SmartCooling.__new__(sc.SmartCooling)
    app.min_temp = overrides.get("min_temp", 16.0)
    app._rise_frac = overrides.get("rise_frac", 0.5)
    app._rise_samples = overrides.get("rise_samples", 10)
    app.dry_run = overrides.get("dry_run", False)
    return app


class AttrsBuild(unittest.TestCase):
    """Regression test for the 2026-07-14 incident: `_attrs()` referenced
    `ceiling_base` as a free variable instead of a parameter, so EVERY armed
    evaluation crashed with NameError and SmartCooling silently never
    published anything but the disarmed status - "not ready to be used" with
    no visible error to the user. Calling `_attrs()` at all is the trip wire:
    the bug fired on the very first reference, independent of arguments."""

    def _call(self, ceiling, ceiling_base):
        app = make_app()
        return app._attrs(
            floor=22.0, mid=22.5, zone=22.2, ceil_s=21.0, ac_s=17.0,
            bath=24.0, kitchen=23.0, E=24.0, target=21.5, deficit=0.5,
            ceiling=ceiling, price_now=1.2, window_open=True,
            bedtime=datetime(2026, 7, 14, 23, 0), run_min=45,
            next_start=datetime(2026, 7, 14, 22, 15), est_cost=1.8,
            floor_limited=False, ceiling_base=ceiling_base,
        )

    def test_no_nameerror_and_keys_present(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        for key in ("ceiling_base", "ceiling_source", "night_ceiling", "floor_target"):
            self.assertIn(key, attrs)

    def test_ceiling_source_comfort_layer_when_lowered(self):
        attrs = self._call(ceiling=22.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "comfort layer")
        self.assertEqual(attrs["ceiling_base"], 23.0)

    def test_ceiling_source_knob_when_unadjusted(self):
        attrs = self._call(ceiling=23.0, ceiling_base=23.0)
        self.assertEqual(attrs["ceiling_source"], "knob")


if __name__ == "__main__":
    unittest.main()
