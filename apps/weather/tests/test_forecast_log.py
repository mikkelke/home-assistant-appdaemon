# tests/test_forecast_log.py - the forecast snapshotter's pure logic.
# Run from repo root: python3 -m unittest discover -s apps/weather/tests -q
#
# This app exists because HA throws old forecasts away, so the file it writes is the ONLY
# record of what was predicted. Three things therefore have to hold, and each has a class
# below: the response envelope must be unwrapped whatever shape HA/AppDaemon wraps it in
# (smart_cooling learned that the hard way - see its _get_forecast comments); entries that
# cannot be parsed must be DROPPED rather than stored as null, because a gap is honest and a
# null would later score as a miss; and the file must stay bounded, since a phone downloads it.

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import forecast_log as fl  # noqa: E402


ENTITY = "weather.forecast_home"
FORECAST = [{"datetime": "2026-08-07T13:00:00+00:00", "temperature": 21.0}]


class ParseEnvelope(unittest.TestCase):
    """Every wrapper shape HA/AppDaemon has been seen to return must unwrap to the same list,
    and anything unrecognised must yield [] - callers treat empty exactly like a failed fetch."""

    def test_appdaemon_result_response_wrapper(self):
        resp = {"result": {"response": {ENTITY: {"forecast": FORECAST}}}}
        self.assertEqual(fl.parse_forecast_envelope(resp, ENTITY), FORECAST)

    def test_response_wrapper(self):
        self.assertEqual(fl.parse_forecast_envelope({"response": {ENTITY: {"forecast": FORECAST}}}, ENTITY), FORECAST)

    def test_entity_keyed(self):
        self.assertEqual(fl.parse_forecast_envelope({ENTITY: {"forecast": FORECAST}}, ENTITY), FORECAST)

    def test_bare_list(self):
        self.assertEqual(fl.parse_forecast_envelope(FORECAST, ENTITY), FORECAST)

    def test_unknown_shape_is_empty(self):
        self.assertEqual(fl.parse_forecast_envelope({"nothing": "useful"}, ENTITY), [])
        self.assertEqual(fl.parse_forecast_envelope(None, ENTITY), [])


class BuildSnapshot(unittest.TestCase):
    ISSUED = "2026-08-07T12:00:00+00:00"

    def _entry(self, hour, **extra):
        return {"datetime": f"2026-08-07T{hour:02d}:00:00+00:00", "temperature": 20.0, **extra}

    def test_keeps_entries_inside_horizon(self):
        snap = fl.build_snapshot(self.ISSUED, [self._entry(13), self._entry(20)], 24)
        self.assertEqual(len(snap["entries"]), 2)
        self.assertEqual(snap["issued"], "2026-08-07T12:00:00+00:00")

    def test_drops_entries_past_horizon(self):
        far = {"datetime": "2026-08-09T12:00:00+00:00", "temperature": 20.0}
        snap = fl.build_snapshot(self.ISSUED, [self._entry(13), far], 24)
        self.assertEqual(len(snap["entries"]), 1)

    def test_drops_entries_before_issue_time(self):
        snap = fl.build_snapshot(self.ISSUED, [self._entry(9), self._entry(13)], 24)
        self.assertEqual(len(snap["entries"]), 1)

    def test_unparseable_entry_is_dropped_not_nulled(self):
        bad = {"datetime": "not-a-date", "temperature": 20.0}
        missing_temp = {"datetime": "2026-08-07T14:00:00+00:00"}
        snap = fl.build_snapshot(self.ISSUED, [bad, missing_temp, self._entry(13)], 24)
        self.assertEqual(len(snap["entries"]), 1)
        self.assertNotIn(None, snap["entries"][0].values())

    def test_optional_fields_carried_when_present(self):
        snap = fl.build_snapshot(
            self.ISSUED,
            [self._entry(13, wind_speed=13.3, precipitation=0.4, precipitation_probability=40, condition="rainy")],
            24,
        )
        row = snap["entries"][0]
        self.assertEqual((row["wind"], row["precip"], row["precip_prob"], row["condition"]), (13.3, 0.4, 40.0, "rainy"))

    def test_optional_fields_omitted_when_absent(self):
        row = fl.build_snapshot(self.ISSUED, [self._entry(13)], 24)["entries"][0]
        self.assertEqual(set(row), {"at", "temp"})

    def test_no_usable_entries_returns_none(self):
        self.assertIsNone(fl.build_snapshot(self.ISSUED, [{"datetime": "bad", "temperature": 1}], 24))
        self.assertIsNone(fl.build_snapshot(self.ISSUED, [], 24))

    def test_bad_issued_returns_none(self):
        self.assertIsNone(fl.build_snapshot("not-a-date", [self._entry(13)], 24))


class PruneSnapshots(unittest.TestCase):
    NOW = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)

    def _snap(self, days_ago):
        return {"issued": (self.NOW - timedelta(days=days_ago)).isoformat(), "entries": []}

    def test_keeps_recent_drops_old(self):
        out = fl.prune_snapshots([self._snap(20), self._snap(1), self._snap(13)], self.NOW, 14)
        self.assertEqual(len(out), 2)

    def test_sorted_chronologically(self):
        out = fl.prune_snapshots([self._snap(1), self._snap(5), self._snap(3)], self.NOW, 14)
        issued = [s["issued"] for s in out]
        self.assertEqual(issued, sorted(issued))

    def test_unparseable_snapshot_dropped(self):
        out = fl.prune_snapshots([{"issued": "nope"}, self._snap(1), "not-a-dict"], self.NOW, 14)
        self.assertEqual(len(out), 1)

    def test_boundary_is_inclusive(self):
        self.assertEqual(len(fl.prune_snapshots([self._snap(14)], self.NOW, 14)), 1)


if __name__ == "__main__":
    unittest.main()
