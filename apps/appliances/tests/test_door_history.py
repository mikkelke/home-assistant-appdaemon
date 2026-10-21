# tests/test_door_history.py - door_history.last_close: the newest recorded on -> off door edge within an
# age bound, and nothing a sensor reconnecting after an HA restart could fake. Pure; no AppDaemon.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import door_history  # noqa: E402

NOW = datetime(2026, 10, 21, 20, 0, 0, tzinfo=timezone.utc)
MAX_AGE_S = 12 * 3600


def at(**ago):
    return (NOW - timedelta(**ago)).isoformat()


def st(state, when):
    return {"state": state, "last_changed": when}


class LastClose(unittest.TestCase):

    def test_on_to_off_edge_is_found(self):
        states = [st("off", at(hours=9)), st("on", at(hours=3)), st("off", at(hours=2, minutes=30))]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=2, minutes=30))

    def test_newest_of_several_wins(self):
        states = [
            st("on", at(hours=9)), st("off", at(hours=8)),
            st("on", at(hours=5)), st("off", at(hours=4)),
            st("on", at(minutes=30)),
        ]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=4))

    def test_unavailable_or_unknown_to_off_is_not_a_close(self):
        for gap in ("unavailable", "unknown"):
            with self.subTest(gap=gap):
                self.assertIsNone(door_history.last_close([st(gap, at(hours=3)), st("off", at(hours=2))], NOW, MAX_AGE_S))
                self.assertIsNone(
                    door_history.last_close([st("on", at(hours=4)), st(gap, at(hours=3)), st("off", at(hours=2))], NOW, MAX_AGE_S)
                )

    def test_a_close_after_the_sensor_comes_back_still_counts(self):
        states = [st("on", at(hours=4)), st("unavailable", at(hours=3)), st("on", at(hours=2)), st("off", at(hours=1))]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=1))

    def test_off_without_a_preceding_on_is_not_a_close(self):
        self.assertIsNone(door_history.last_close([st("off", at(hours=3))], NOW, MAX_AGE_S))
        self.assertIsNone(door_history.last_close([st("off", at(hours=3)), st("off", at(hours=2))], NOW, MAX_AGE_S))

    def test_too_old_is_ignored(self):
        states = [st("on", at(hours=14)), st("off", at(hours=13))]
        self.assertIsNone(door_history.last_close(states, NOW, MAX_AGE_S))

    def test_the_age_bound_is_inclusive(self):
        edge = [st("on", at(hours=13)), st("off", at(hours=12))]
        self.assertEqual(door_history.last_close(edge, NOW, MAX_AGE_S), NOW - timedelta(hours=12))
        just_over = [st("on", at(hours=13)), st("off", at(hours=12, seconds=1))]
        self.assertIsNone(door_history.last_close(just_over, NOW, MAX_AGE_S))

    def test_an_old_close_does_not_hide_behind_a_newer_open(self):
        states = [st("on", at(hours=3)), st("off", at(hours=2)), st("on", at(minutes=5))]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=2))

    def test_a_close_in_the_future_is_ignored(self):
        states = [st("on", at(minutes=1)), st("off", (NOW + timedelta(seconds=30)).isoformat())]
        self.assertIsNone(door_history.last_close(states, NOW, MAX_AGE_S))

    def test_empty_or_none_is_none(self):
        self.assertIsNone(door_history.last_close([], NOW, MAX_AGE_S))
        self.assertIsNone(door_history.last_close(None, NOW, MAX_AGE_S))

    def test_a_close_that_cannot_be_dated_is_skipped(self):
        states = [
            st("on", at(hours=6)), st("off", at(hours=5)),
            st("on", at(hours=4)), {"state": "off", "last_changed": "not a time"},
            {"state": "on"}, {"state": "off"},
        ]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=5))

    def test_last_updated_stands_in_for_a_missing_last_changed(self):
        states = [st("on", at(hours=3)), {"state": "off", "last_updated": at(hours=2)}]
        self.assertEqual(door_history.last_close(states, NOW, MAX_AGE_S), NOW - timedelta(hours=2))


class Timestamps(unittest.TestCase):
    CLOSE = NOW - timedelta(hours=2)

    def _close_of(self, off_stamp):
        return door_history.last_close([st("on", at(hours=3)), st("off", off_stamp)], NOW, MAX_AGE_S)

    def test_iso_strings_with_z_and_with_an_offset(self):
        self.assertEqual(self._close_of("2026-10-21T18:00:00Z"), self.CLOSE)
        self.assertEqual(self._close_of("2026-10-21T20:00:00+02:00"), self.CLOSE)
        self.assertEqual(self._close_of("2026-10-21T18:00:00.250000+00:00"), self.CLOSE + timedelta(milliseconds=250))

    def test_a_string_without_an_offset_reads_as_utc(self):
        self.assertEqual(self._close_of("2026-10-21T18:00:00"), self.CLOSE)

    def test_datetime_objects_aware_or_naive(self):
        self.assertEqual(self._close_of(self.CLOSE.astimezone(timezone(timedelta(hours=2)))), self.CLOSE)
        self.assertEqual(self._close_of(self.CLOSE.replace(tzinfo=None)), self.CLOSE)

    def test_the_result_is_utc(self):
        close = self._close_of("2026-10-21T20:00:00+02:00")
        self.assertEqual(close.utcoffset(), timedelta(0))

    def test_now_as_a_string_or_naive_datetime(self):
        states = [st("on", at(hours=3)), st("off", at(hours=2))]
        self.assertEqual(door_history.last_close(states, NOW.isoformat(), MAX_AGE_S), self.CLOSE)
        self.assertEqual(door_history.last_close(states, NOW.replace(tzinfo=None), MAX_AGE_S), self.CLOSE)
        self.assertIsNone(door_history.last_close(states, None, MAX_AGE_S))


if __name__ == "__main__":
    unittest.main()
