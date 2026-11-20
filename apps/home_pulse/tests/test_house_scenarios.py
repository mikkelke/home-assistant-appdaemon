# tests/test_house_scenarios.py - scenarios_core on real rows and readings from 19-20.11.2026, and
# HouseScenarios' pure helpers. Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import types
import unittest
from datetime import datetime
from pathlib import Path

_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import house_scenarios  # noqa: E402
import scenarios_core as core  # noqa: E402


def at(local):
    """Epoch of a house-local time, as the recorder gives it."""
    return datetime.fromisoformat(local).replace(tzinfo=core.HOUSE_TZ).timestamp()


def row(ts, icon, text, cause=None, effect=None, **extra):
    out = {"ts": ts, "icon": icon, "text": text}
    if cause:
        out.update(cause=cause, effect=effect)
    out.update(extra)
    return out


# The WokXpress order, 19.11: the intercom's two ring rows and the delivery row.
WOLT_ROWS = [
    row("2026-11-19T20:10:31.103+01:00", "mdi:bell-ring",
        "Someone rang the front door -> Announcing on the speakers and opening the door",
        "Someone rang the front door", "Announcing on the speakers and opening the door"),
    row("2026-11-19T20:10:36.553+01:00", "mdi:door-open", "Someone rang the front door -> The door was opened for them",
        "Someone rang the front door", "The door was opened for them"),
    row("2026-11-19T19:11:38.613601+00:00", "mdi:moped", "Wolt order from WokXpress - Vesterbro delivered"),
    row("2026-11-19T20:06:42.1+01:00", "mdi:watering-can-outline", "Watered Pothos, soil 15% to 69%", audience="admin"),
]

NIGHT_ROWS = [
    row("2026-11-19T21:36:41.047862+00:00", "mdi:weather-night", "Night mode on"),
    row("2026-11-19T23:21:29.260431+00:00", "mdi:bed", "Went to bed", by="Kristine", audience_users=["Kristine"]),
    row("2026-11-20T00:24:29.250414+00:00", "mdi:bed", "Went to bed", by="Claudia", audience_users=["Claudia"]),
]

DISH_ROWS = [
    row("2026-11-20T06:33:06.2+01:00", "mdi:dishwasher", "Dishwasher finished - used 0.79 kWh", by="Claudia"),
    row("2026-11-20T09:26:08.4+01:00", "mdi:basket-check", "Dishwasher emptied", by="Mikkel"),
]

SIGNALS = {
    "night": [(at("2026-11-19T22:36:41"), "on"), (at("2026-11-20T07:33:04"), "off")],
    "machines": {"dish": [(at("2026-11-20T02:25:18"), "Running"), (at("2026-11-20T06:33:06"), "Unemptied"),
                          (at("2026-11-20T09:26:08"), "Emptied")]},
    "power": {"hob": [(at("2026-11-19T19:29:41"), 469.7), (at("2026-11-19T19:31:19"), 68.3),
                      (at("2026-11-19T19:31:24"), 423.8), (at("2026-11-19T19:39:29"), 8.8),
                      (at("2026-11-19T19:41:34"), 531.9), (at("2026-11-19T19:43:00"), 83.0),
                      (at("2026-11-19T19:43:06"), 439.3), (at("2026-11-19T19:49:50"), 73.3)]},
    "areas": {"Mikkel": [(at("2026-11-19T19:15:59"), "Kitchen")],
              "Claudia": [(at("2026-11-19T19:19:06"), "Kitchen")],
              "Kristine": []},
}
NOW = at("2026-11-20T12:00:00")


def build(rows=None, signals=None, now=NOW):
    return core.build_scenarios(rows if rows is not None else WOLT_ROWS + NIGHT_ROWS + DISH_ROWS,
                                SIGNALS if signals is None else signals, now)


def of(scenarios, kind):
    return [s for s in scenarios if s["type"] == kind]


class DoorTests(unittest.TestCase):
    def test_a_wolt_ring_is_one_scenario_with_its_rows(self):
        (door,) = of(build(), "door")
        self.assertEqual(door["headline"], "Wolt delivery from WokXpress - Vesterbro")
        self.assertEqual(door["summary"], "Rang the front door 20:10 · opened")
        self.assertEqual(len(door["members"]), 3)
        self.assertNotIn("public_headline", door)
        self.assertEqual(door["where"], "front door")

    def test_the_merged_ring_row_says_how_long_the_door_took(self):
        rows = [row("2026-11-21T12:00:00+01:00", "mdi:bell-ring", "Someone rang the front door -> Opened after 5 s",
                    "Someone rang the front door", "Opened after 5 s"),
                row("2026-11-21T12:00:52+01:00", "mdi:lock-open-variant", "Apartment door unlocked", by="Claudia")]
        (door,) = of(build(rows, {}), "door")
        self.assertEqual(door["headline"], "Claudia came home")
        self.assertEqual(door["summary"], "Rang the front door 12:00 · opened after 5 s · apartment door +52 s")
        self.assertEqual(door["who"], ["Claudia"])

    def test_a_parcel_only_the_admin_sees_stays_out_of_the_public_headline(self):
        rows = [WOLT_ROWS[0], row("2026-11-19T20:15:00+01:00", "mdi:package-variant-closed",
                                  "Airmee parcel probably outside the door", audience="admin")]
        (door,) = of(build(rows, {}), "door")
        self.assertEqual(door["headline"], "Airmee parcel probably outside the door")
        self.assertEqual(door["public_headline"], "Someone rang the front door")

    def test_a_guest_with_a_keypad_code_comes_in_rather_than_home(self):
        rows = [WOLT_ROWS[0], row("2026-11-19T20:12:00+01:00", "mdi:lock-open-variant", "Apartment door unlocked",
                                  by="Pernille")]
        (door,) = of(build(rows, {}), "door")
        self.assertEqual(door["headline"], "Pernille came in")

    def test_rings_minutes_apart_are_two_visits(self):
        later = dict(WOLT_ROWS[0], ts="2026-11-19T20:20:31+01:00")
        self.assertEqual(len(of(build([WOLT_ROWS[0], later], {}), "door")), 2)


class NightTests(unittest.TestCase):
    def test_the_night_holds_its_night_and_bedtime_rows(self):
        (night,) = of(build(), "night")
        self.assertEqual(night["summary"], "22:36-07:33")
        self.assertEqual(len(night["members"]), 3)
        self.assertEqual(night["who"], [])

    def test_a_night_still_on_is_ongoing(self):
        signals = dict(SIGNALS, night=[(at("2026-11-19T22:36:41"), "on")])
        (night,) = of(build(NIGHT_ROWS, signals, at("2026-11-20T03:00:00")), "night")
        self.assertIsNone(night["end"])
        self.assertEqual(night["summary"], "since 22:36")


class MachineTests(unittest.TestCase):
    def test_a_dishwasher_run_holds_finished_and_emptied(self):
        (run,) = of(build(), "machine")
        self.assertEqual(run["headline"], "Dishwasher")
        self.assertEqual(run["summary"], "02:25-06:33 · 0.79 kWh · emptied 09:26 by Mikkel")
        self.assertEqual(run["who"], ["Claudia", "Mikkel"])
        self.assertEqual(run["members"], [DISH_ROWS[0]["ts"], DISH_ROWS[1]["ts"]])

    def test_a_dryer_soon_after_the_washer_is_one_laundry(self):
        signals = {"machines": {
            "washer": [(at("2026-11-15T08:18:00"), "Running"), (at("2026-11-15T10:00:00"), "Unemptied")],
            "dryer": [(at("2026-11-15T10:20:00"), "Running"), (at("2026-11-15T12:00:00"), "Unemptied")]}}
        (laundry,) = of(build([], signals), "machine")
        self.assertEqual(laundry["headline"], "Laundry")
        self.assertIn("washer 08:18-10:00 then dryer 10:20-12:00", laundry["summary"])

    def test_a_running_machine_is_ongoing(self):
        signals = {"machines": {"washer": [(at("2026-11-20T11:59:00"), "Running")]}}
        (run,) = of(build([], signals), "machine")
        self.assertIsNone(run["end"])
        self.assertEqual(run["summary"], "running since 11:59")


class CookingTests(unittest.TestCase):
    def test_hob_spans_merge_and_name_who_was_in_the_kitchen(self):
        (cooking,) = of(build(), "cooking")
        self.assertEqual(cooking["summary"], "19:29-19:49 · hob 18 min")
        self.assertEqual(cooking["who"], ["Mikkel", "Claudia"])

    def test_a_brief_heat_is_not_cooking(self):
        signals = {"power": {"hob": [(at("2026-11-14T12:40:00"), 900), (at("2026-11-14T12:43:00"), 0)]}}
        self.assertEqual(of(build([], signals), "cooking"), [])

    def test_the_oven_still_on_is_ongoing(self):
        signals = {"power": {"oven": [(at("2026-11-20T11:30:00"), 2100)]}}
        (cooking,) = of(build([], signals), "cooking")
        self.assertIsNone(cooking["end"])


class OwnershipTests(unittest.TestCase):
    def test_a_row_belongs_to_one_scenario_at_most(self):
        members = [ts for s in build() for ts in s["members"]]
        self.assertEqual(len(members), len(set(members)))
        self.assertNotIn(WOLT_ROWS[3]["ts"], members)


class HelperTests(unittest.TestCase):
    def test_power_keeps_only_threshold_crossings(self):
        points = []
        for t, watts in ((1, 0), (2, 5), (3, 400), (4, 500), (5, 10)):
            house_scenarios.power_point(points, t, watts, 300)
        self.assertEqual(points, [(1, 0.0), (3, 400.0), (5, 10.0)])

    def test_epoch_reads_get_history_datetimes_and_strings(self):
        when = datetime(2026, 11, 20, 6, 33, 6, tzinfo=core.HOUSE_TZ)
        self.assertEqual(core.epoch(when), core.epoch("2026-11-20T06:33:06+01:00"))

    def test_prune_keeps_the_state_at_the_cut(self):
        self.assertEqual(house_scenarios.prune([(1, "a"), (2, "b"), (5, "c")], 3), [(2, "b"), (5, "c")])

    def test_recent_keeps_ongoing_and_lately_ended(self):
        kept = core.recent(build(), at("2026-11-21T07:00:00"), 86400, 10)
        self.assertEqual([s["type"] for s in kept], ["night"])


if __name__ == "__main__":
    unittest.main()
