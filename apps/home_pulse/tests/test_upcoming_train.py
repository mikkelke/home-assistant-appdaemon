# tests/test_upcoming_train.py - Kristine's train row, including the departures exactly as
# TransitAlarm publishes them through AppDaemon 4.5.13.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "transit"))
sys.modules.setdefault("aiohttp", types.ModuleType("aiohttp"))
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for _name, _mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(_name, _mod)

import transit_alarm as ta  # noqa: E402
import upcoming_core as core  # noqa: E402
import upcoming_train as train  # noqa: E402
from upcoming_maint import HOUSE_TZ  # noqa: E402

WEDNESDAY_0700 = datetime(2026, 10, 14, 7, 0, tzinfo=HOUSE_TZ)
MIN = timedelta(minutes=1)


def local(moment):
    return moment.astimezone(HOUSE_TZ).strftime("%H:%M")


def dep(moment, delay=0, cancelled=False):
    """One departure as it reaches HA: AppDaemon drops 0 and False and writes True as "true"."""
    out = {"ts": int((moment + delay * MIN).timestamp()), "time": local(moment + delay * MIN), "line": "IC 123",
           "scheduled_time": local(moment)}
    if delay:
        out["delay_min"] = delay
    if cancelled:
        out["cancelled"] = "true"
    if delay or cancelled:
        out["problematic"] = "true"
    return out


def route(departures, as_of):
    return {"state": "OK", "attributes": {
        "departures": departures,
        "data_as_of": as_of.astimezone(HOUSE_TZ).replace(tzinfo=None).isoformat(),
    }}


FIRST = WEDNESDAY_0700 + 46 * MIN
SECOND = WEDNESDAY_0700 + 62 * MIN


def row(now=WEDNESDAY_0700, departures=None, as_of=None, stog="OK", person="home"):
    departures = departures if departures is not None else [dep(FIRST), dep(SECOND, delay=5)]
    return train.train_row(route(departures, as_of or now - 2 * MIN), stog, person, now)


def ad_clean(value):
    """AppDaemon 4.5.13's clean_http_kwargs on a set_state attribute value."""
    if value is True:
        return "true"
    if isinstance(value, dict):
        return {k: ad_clean(v) for k, v in value.items() if v is not None and v is not False and v != 0}
    if isinstance(value, list):
        return [ad_clean(v) for v in value]
    return value


class TrainRowTests(unittest.TestCase):
    def test_a_weekday_morning_at_home_shows_the_next_two_departures(self):
        found = row()
        self.assertEqual((found["title"], found["detail"]), ("Train to Odense", "07:46, 08:02 +5"))
        self.assertEqual(found["at"], core.utc_iso(FIRST))
        self.assertEqual((found["kind"], found["icon"], found["audience_users"]), ("train", "mdi:train", ["Kristine"]))
        self.assertNotIn("area", found)
        self.assertNotIn("chip", found)
        self.assertTrue(core.is_valid_row(found))

    def test_no_row_on_a_saturday_outside_the_window_or_while_she_is_away(self):
        saturday = WEDNESDAY_0700 + timedelta(days=3)
        self.assertIsNone(row(now=saturday, departures=[dep(saturday + 46 * MIN)]))
        self.assertIsNone(row(now=WEDNESDAY_0700.replace(hour=9, minute=30)))
        self.assertIsNone(row(now=WEDNESDAY_0700.replace(hour=5, minute=59)))
        self.assertIsNotNone(row(now=WEDNESDAY_0700.replace(hour=6, minute=0)))
        self.assertIsNone(row(person="not_home"))

    def test_data_eleven_minutes_old_gives_no_row(self):
        self.assertIsNone(row(as_of=WEDNESDAY_0700 - 11 * MIN))
        self.assertIsNotNone(row(as_of=WEDNESDAY_0700 - 10 * MIN))

    def test_a_cancelled_departure_reads_cancelled_and_never_sets_the_time(self):
        found = row(departures=[dep(FIRST, cancelled=True), dep(SECOND)])
        self.assertEqual(found["detail"], "07:46 cancelled, 08:02")
        self.assertEqual(found["at"], core.utc_iso(SECOND))

    def test_all_cancelled_is_untimed(self):
        found = row(departures=[dep(FIRST, cancelled=True), dep(SECOND, cancelled=True)])
        self.assertNotIn("at", found)

    def test_the_s_tog_leg_adds_a_chip_when_it_is_not_ok(self):
        self.assertEqual(row(stog="Delayed")["chip"], "S-tog delayed")
        self.assertEqual(row(stog="Disrupted")["chip"], "S-tog disrupted")
        self.assertNotIn("chip", row(stog="Unavailable"))

    def test_a_departure_that_left_between_polls_is_gone(self):
        found = row(now=FIRST + MIN, as_of=FIRST - MIN)
        self.assertEqual(found["detail"], "08:02 +5")
        self.assertEqual(found["at"], core.utc_iso(SECOND + 5 * MIN))

    def test_no_departures_no_route_or_no_absolute_times_give_no_row(self):
        self.assertIsNone(row(departures=[]))
        self.assertIsNone(train.train_row(None, "OK", "home", WEDNESDAY_0700))
        stripped = [{k: v for k, v in dep(FIRST).items() if k != "ts"}]
        self.assertIsNone(row(departures=stripped))

    def test_the_payload_transit_alarm_publishes(self):
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        app.expose_departure_ts = True

        def normalized(moment, delay=0, cancelled=False):
            effective = (moment + delay * MIN).astimezone().replace(tzinfo=None)
            return {"effective": effective, "line": "IC 123", "cancelled": cancelled, "delay_min": delay,
                    "scheduled_str": local(moment), "time_label": local(moment)}

        result = app._assemble_eval_result(
            issues=[], severity=0, now=WEDNESDAY_0700.astimezone().replace(tzinfo=None), delay_thr=10, max_deps=25,
            future_only=[normalized(FIRST, cancelled=True), normalized(SECOND, delay=5),
                         normalized(SECOND + 30 * MIN)],
        )
        published = ad_clean({"departures": result["departures"]})["departures"]
        self.assertNotIn("cancelled", published[1])
        self.assertEqual(published[0]["cancelled"], "true")
        found = row(departures=published)
        self.assertEqual(found["detail"], "07:46 cancelled, 08:02 +5")
        self.assertEqual(found["at"], core.utc_iso(SECOND + 5 * MIN))


if __name__ == "__main__":
    unittest.main()
