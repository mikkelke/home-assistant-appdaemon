# tests/test_bathroom_departures.py - the bathroom display's transit departures, including the
# departures exactly as TransitAlarm publishes them through AppDaemon 4.5.13.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path

APPS = Path(__file__).resolve().parents[2]
for _dir in ("home_pulse", "presence", "climate", "deliveries", "transit", "appliances"):
    sys.path.insert(0, str(APPS / _dir))
_aiohttp = types.ModuleType("aiohttp")
_aiohttp.ClientSession = object
sys.modules.setdefault("aiohttp", _aiohttp)
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for _name, _mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(_name, _mod)

import bathroom_display as bd  # noqa: E402
import transit_alarm as ta  # noqa: E402
from upcoming_maint import HOUSE_TZ  # noqa: E402

WEDNESDAY_0700 = datetime(2026, 10, 14, 7, 0, tzinfo=HOUSE_TZ)
MIN = timedelta(minutes=1)
FIRST = WEDNESDAY_0700 + 46 * MIN
SECOND = WEDNESDAY_0700 + 62 * MIN


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
    return out


def ad_clean(value):
    """AppDaemon 4.5.13's clean_http_kwargs on a set_state attribute value."""
    if value is True:
        return "true"
    if isinstance(value, dict):
        return {k: ad_clean(v) for k, v in value.items() if v is not None and v is not False and v != 0}
    if isinstance(value, list):
        return [ad_clean(v) for v in value]
    return value


class DeparturesTests(unittest.TestCase):
    def test_future_departures_soonest_first_with_delay_and_cancelled(self):
        found = bd.departures([dep(SECOND, delay=5), dep(FIRST, cancelled=True)], WEDNESDAY_0700)
        self.assertEqual([(label, delay, cancelled) for _, label, delay, cancelled in found],
                         [("07:46", 0, True), ("08:02", 5, False)])

    def test_a_departure_that_left_between_polls_is_gone(self):
        found = bd.departures([dep(FIRST), dep(SECOND, delay=5)], FIRST + MIN)
        self.assertEqual([label for _, label, _, _ in found], ["08:02"])

    def test_no_list_or_no_absolute_times_give_none(self):
        self.assertEqual(bd.departures(None, WEDNESDAY_0700), [])
        stripped = [{k: v for k, v in dep(FIRST).items() if k != "ts"}]
        self.assertEqual(bd.departures(stripped, WEDNESDAY_0700), [])
        self.assertEqual(bd.departures([{**dep(FIRST), "ts": True}], WEDNESDAY_0700), [])

    def test_the_payload_transit_alarm_publishes(self):
        app = ta.TransitAlarm.__new__(ta.TransitAlarm)
        app.expose_departure_ts = True

        def normalized(moment, delay=0, cancelled=False):
            effective = (moment + delay * MIN).astimezone().replace(tzinfo=None)
            return {"effective": effective, "line": "IC 123", "cancelled": cancelled, "delay_min": delay,
                    "scheduled_str": local(moment), "time_label": local(moment)}

        result = app._assemble_eval_result(
            issues=[], severity=0, now=WEDNESDAY_0700.astimezone().replace(tzinfo=None), delay_thr=10, max_deps=25,
            future_only=[normalized(FIRST, cancelled=True), normalized(SECOND, delay=5)],
        )
        published = ad_clean({"departures": result["departures"]})["departures"]
        found = bd.departures(published, WEDNESDAY_0700)
        self.assertEqual([(label, delay, cancelled) for _, label, delay, cancelled in found],
                         [("07:46", 0, True), ("08:02", 5, False)])


if __name__ == "__main__":
    unittest.main()
