# tests/test_bathroom_display.py - BathroomDisplay: who the display sees, the lines it gets and when it
# publishes. The pure rules come first (the phone, the transit line, the rows of sensor.house_upcoming built
# by the real row builders, the fit of a line); the app is then driven through its real initialize() with the
# real bathroom_display.yaml, a fake clock and a settable state dict.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import asyncio
import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from datetime import time as dtime
from unittest import mock
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "presence"))
sys.path.insert(0, str(APPS / "climate"))
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
import upcoming_core as core  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_once as once  # noqa: E402
import upcoming_plants as plants  # noqa: E402
import upcoming_rain as rain  # noqa: E402

CONFIG = yaml.safe_load((APPS / "home_pulse" / "bathroom_display.yaml").read_text())["BathroomDisplay"]
HOUSE = maint.HOUSE_TZ
NOW = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)  # 08:30 in the house, on a Thursday
SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
SATURDAY = NOW + 2 * DAY
FRESH = timedelta(seconds=20)
TRANSIT_AGE = timedelta(minutes=45)
ACTIVE = "binary_sensor.bathroom_active"
RSSI = CONFIG["rssi_entity"]
UPCOMING = CONFIG["upcoming_entity"]
METRO, STOG = (route["entity"] for route in CONFIG["transit"])
SLEEP = CONFIG["sleep_entity"]
LIGHT = CONFIG["light_entity"]
SPEAKER = CONFIG["speaker"]
MEASURED = CONFIG["measured_entity"]
WEIGHT = CONFIG["weight_entity"]
FAT = CONFIG["body_fat_entity"]
PIEZO = CONFIG["rain_entity"]
WEATHER = CONFIG["weather_entity"]
ALARM = CONFIG["alarm_entity"]
ALARM_TIME = CONFIG["alarm_time_entity"]
ALARM_FROM_ENTITY = CONFIG["alarm_from_entity"]


def iso(moment):
    return moment.astimezone(timezone.utc).isoformat()


def house(month, day, hour=0, minute=0):
    return datetime(2026, month, day, hour, minute, tzinfo=HOUSE)


def full(state, attributes=None, updated=NOW):
    return {"state": state, "attributes": dict(attributes or {}), "last_updated": iso(updated)}


def maint_row(row_id, title, at=None, **extra):
    extra.setdefault("audience", "admin")
    return core.make_row(f"maint:{row_id}", "maint", title, at=at, **extra)


def one_off(due, title="Spray the Alocasia", area="bathroom", now=NOW):
    """The row HouseUpcoming builds for a one-off task due on `due`."""
    [task] = once.parse_once([{"due": due, "title": title, "area": area, "icon": "mdi:spray"}]).values()
    return once.once_row(task, {"at": core.utc_iso(now - 30 * DAY), "seed": True}, now)[0]


def watering(days_ago, name="Alocasia", area="bathroom", now=NOW):
    """The row HouseUpcoming builds for a calendar plant (every 7 days) last watered `days_ago` days ago."""
    [plant] = plants.parse_timed([{"name": name, "area": area, "water": 0.7, "every_days": 7}]).values()
    return plants.timed_row(plant, {"at": core.utc_iso(now - days_ago * DAY), "seed": True}, now)[0]


def dry_plant(name="Philodendron", area="dining_room", now=NOW):
    """The row HouseUpcoming builds for a sensor plant whose soil is at its dry level: no time, no Done button."""
    [(entity, plant)] = plants.parse_plants([{"moisture": "sensor.philodendron_moisture", "name": name,
                                              "area": area, "dry": 20, "water": 0.5}]).items()
    return plants.plant_row(entity, plant, {"since": core.utc_iso(now)}, 20.0)[0]


def departure(minutes, scheduled, delay=0, cancelled=False, now=NOW):
    """A departure as TransitStatus publishes it: on time and not cancelled leave out delay_min and cancelled."""
    dep = {"ts": int((now + minutes * MIN).timestamp()), "time": scheduled, "line": "A", "scheduled_time": scheduled}
    if delay:
        dep["delay_min"] = delay
    if cancelled:
        dep["cancelled"] = "true"
    return dep


def route(state, departures=(), as_of=NOW - 2 * MIN, threshold=10, **extra):
    stamp = as_of.astimezone(HOUSE).replace(tzinfo=None).isoformat()
    return full(state, {"departures": list(departures), "data_as_of": stamp, "delay_threshold_min": threshold, **extra})


def upcoming(rows, expires=NOW + 10 * MIN):
    return full(iso(NOW), {"expires": iso(expires), "rows": rows})


def attrs_of(**overrides):
    base = {"friendly_name": "Bathroom display", "icon": "mdi:monitor-dashboard", "reason": "", "person": "",
            "greeting": "", "night": ""}
    for number in (1, 2, 3):
        base.update({f"line{number}": "", f"icon{number}": "", f"due{number}": ""})
    return {**base, **overrides}


def utc(hour, minute=0, day=5):
    return datetime(2026, 11, day, hour, minute, tzinfo=timezone.utc)


def entries(*hours):
    """Hourly forecast entries as weather.get_forecasts gives them, from (hour start, mm) pairs."""
    return [{"datetime": iso(start), "precipitation": mm, "temperature": 7.0, "condition": "cloudy"}
            for start, mm in hours]


def forecast_hours(*hours):
    return rain.forecast_hours(entries(*hours))


def weather_answer(*hours):
    """The envelope AppDaemon's call_service hands over for weather.get_forecasts."""
    return {"id": 9, "type": "result", "success": True, "result": {"context": {"id": "c"}, "response": {
        WEATHER: {"forecast": entries(*hours)}}}}


def stat_row(start, mean):
    return {"start": iso(start), "end": iso(start + HOUR), "mean": mean}


def stats_answer(weight_rows, fat_rows):
    """The envelope AppDaemon's call_service hands over for recorder.get_statistics."""
    return {"id": 7, "type": "result", "success": True, "result": {
        "context": {"id": "c", "parent_id": None, "user_id": "u"},
        "response": {"statistics": {WEIGHT: weight_rows, FAT: fat_rows}}}}


class PhoneTests(unittest.TestCase):
    def test_a_reading_is_a_finite_number_or_nothing(self):
        for state, expected in (("-68.4", -68.4), ("-70", -70.0), ("unknown", None), ("unavailable", None),
                                ("nan", None), ("NaN", None), ("inf", None), ("", None), (None, None), (True, None)):
            with self.subTest(state=state):
                self.assertEqual(bd.phone_reading(full(state))[0], expected)

    def test_its_age_is_the_entitys_own_last_updated(self):
        self.assertEqual(bd.phone_reading(full("-68", updated=NOW - 3 * SEC)), (-68.0, NOW - 3 * SEC))
        self.assertEqual(bd.phone_reading({"state": "-68"}), (-68.0, None))
        self.assertEqual(bd.phone_reading(None), (None, None))

    def test_status(self):
        for value, age, expected in ((-68, 3, "near"), (-75, 3, "near"), (-75.01, 3, "weak"), (-90, 3, "weak"),
                                     (-60, 20, "near"), (-60, 21, "stale"), (-90, 30, "stale"), (None, 0, "none")):
            with self.subTest(value=value, age=age):
                self.assertEqual(bd.phone_status(value, NOW - age * SEC, NOW, -75, FRESH), expected)
        self.assertEqual(bd.phone_status(-60, None, NOW, -75, FRESH), "stale")

    def test_a_phone_that_rose_on_its_baseline_is_rise(self):
        cases = (  # value, baseline, status
            (-79, -88, "rise"), (-80, -86, "rise"), (-76, -100, "rise"), (-80, -100, "rise"),
            (-78, -77, "weak"), (-79, -84.9, "weak"), (-80.01, -100, "weak"), (-90, -100, "weak"),
            (-75, -77, "near"), (-75, -100, "near"), (-60, -61, "near"))
        for value, baseline, expected in cases:
            with self.subTest(value=value, baseline=baseline):
                self.assertEqual(bd.phone_status(value, NOW - 3 * SEC, NOW, -75, FRESH, baseline), expected)

    def test_without_a_baseline_there_is_no_rise(self):
        self.assertEqual(bd.phone_status(-78, NOW, NOW, -75, FRESH), "weak")
        self.assertEqual(bd.phone_status(-78, NOW, NOW, -75, FRESH, None), "weak")

    def test_the_rise_limits_are_arguments(self):
        self.assertEqual(bd.phone_status(-85, NOW, NOW, -75, FRESH, -100, rise_min=-90, rise_db=10), "rise")
        self.assertEqual(bd.phone_status(-85, NOW, NOW, -75, FRESH, -100, rise_min=-84, rise_db=10), "weak")
        self.assertEqual(bd.phone_status(-85, NOW, NOW, -75, FRESH, -92, rise_min=-90, rise_db=10), "weak")

    def test_a_rise_needs_a_fresh_reading(self):
        self.assertEqual(bd.phone_status(-79, NOW - 20 * SEC, NOW, -75, FRESH, -100), "rise")
        self.assertEqual(bd.phone_status(-79, NOW - 21 * SEC, NOW, -75, FRESH, -100), "stale")
        self.assertEqual(bd.phone_status(-79, None, NOW, -75, FRESH, -100), "stale")
        self.assertEqual(bd.phone_status(None, NOW, NOW, -75, FRESH, -100), "none")


class BaselineTests(unittest.TestCase):
    def test_it_is_the_median_of_the_readings_from_120_down_to_15_seconds_before_the_visit(self):
        history = [(NOW - 121 * SEC, -50.0), (NOW - 120 * SEC, -90.0), (NOW - 15 * SEC, -86.0),
                   (NOW - 14 * SEC, -60.0), (NOW, -55.0)]
        self.assertEqual(bd.baseline_at(history, NOW), -88.0)
        self.assertEqual(bd.baseline_at(history[1:3], NOW), -88.0)
        self.assertEqual(bd.baseline_at([(NOW - 60 * SEC, -90.0), (NOW - 50 * SEC, -88.0), (NOW - 40 * SEC, -70.0)],
                                        NOW), -88.0)

    def test_it_is_measured_from_the_start_of_the_visit(self):
        history = [(NOW - 100 * SEC, -90.0), (NOW - 10 * SEC, -60.0)]
        self.assertEqual(bd.baseline_at(history, NOW), -90.0)
        self.assertEqual(bd.baseline_at(history, NOW + 60 * SEC), -60.0)
        self.assertEqual(bd.baseline_at(history, NOW - 110 * SEC), bd.UNHEARD)

    def test_a_phone_not_heard_in_the_window_is_minus_100(self):
        self.assertEqual(bd.UNHEARD, -100.0)
        self.assertEqual(bd.baseline_at([], NOW), -100.0)
        self.assertEqual(bd.baseline_at([(NOW - 121 * SEC, -60.0), (NOW - 10 * SEC, -60.0), (NOW, -60.0)], NOW), -100.0)

    def test_remember_keeps_a_reading_once_with_its_moment(self):
        history = bd.remember([], full("-88", updated=NOW - 60 * SEC), NOW)
        self.assertEqual(history, [(NOW - 60 * SEC, -88.0)])
        self.assertEqual(bd.remember(history, full("-88", updated=NOW - 60 * SEC), NOW), history)
        self.assertEqual(bd.remember(history, full("-90", updated=NOW - 60 * SEC), NOW), history)
        self.assertEqual(bd.remember(history, full("-88", updated=NOW - 59 * SEC), NOW),
                         history + [(NOW - 59 * SEC, -88.0)])

    def test_remember_leaves_out_what_is_not_a_dated_number(self):
        history = [(NOW - 60 * SEC, -88.0)]
        for state in (full("unknown"), full("unavailable"), full("nan"), full(None), {"state": "-70"}, None, "-70"):
            with self.subTest(state=state):
                self.assertEqual(bd.remember(history, state, NOW), history)

    def test_remember_drops_what_is_more_than_180_seconds_old(self):
        history = [(NOW - 181 * SEC, -81.0), (NOW - 180 * SEC, -80.0), (NOW - 10 * SEC, -79.0)]
        self.assertEqual(bd.remember(history, None, NOW), history[1:])
        self.assertEqual(bd.remember([], full("-70", updated=NOW - 181 * SEC), NOW), [])
        self.assertEqual(bd.remember([], full("-70", updated=NOW - 180 * SEC), NOW), [(NOW - 180 * SEC, -70.0)])


class StateTests(unittest.TestCase):
    def test_next_state(self):
        cases = (  # prev, active, status, state
            (None, False, "near", "off"), (None, None, "near", "off"), (None, True, "none", "someone"),
            (None, True, "stale", "someone"), (None, True, "weak", "someone"), (None, True, "near", "mikkel"),
            ("off", False, "none", "off"), ("off", None, "near", "off"), ("off", True, "weak", "someone"),
            ("off", True, "near", "mikkel"),
            ("someone", False, "near", "off"), ("someone", None, "near", "someone"),
            ("someone", None, "none", "someone"), ("someone", True, "none", "someone"),
            ("someone", True, "near", "mikkel"),
            ("mikkel", False, "near", "off"), ("mikkel", False, "none", "off"), ("mikkel", None, "near", "mikkel"),
            ("mikkel", None, "none", "mikkel"), ("mikkel", True, "near", "mikkel"),
            ("mikkel", True, "weak", "mikkel"), ("mikkel", True, "stale", "mikkel"),
            ("mikkel", True, "none", "mikkel"),
            (None, True, "rise", "mikkel"), ("off", True, "rise", "mikkel"), ("someone", True, "rise", "mikkel"),
            ("mikkel", True, "rise", "mikkel"), (None, False, "rise", "off"), ("someone", False, "rise", "off"),
            ("someone", None, "rise", "someone"), ("off", None, "rise", "off"))
        for prev, active, status, expected in cases:
            with self.subTest(prev=prev, active=active, status=status):
                self.assertEqual(bd.next_state(prev, active, status), expected)

    def test_a_visit_from_the_first_look_to_the_room_emptying_and_a_new_visit(self):
        state, seen = None, []
        for active, status in ((True, "none"), (True, "weak"), (True, "near"), (True, "weak"), (True, "stale"),
                               (None, "none"), (True, "none"), (False, "none"), (None, "near"), (True, "weak"),
                               (True, "near")):
            state = bd.next_state(state, active, status)
            seen.append(state)
        self.assertEqual(seen, ["someone", "someone", "mikkel", "mikkel", "mikkel", "mikkel", "mikkel", "off",
                                "off", "someone", "mikkel"])

    def test_a_visit_that_is_identified_by_a_rise_is_the_same_visit(self):
        state, seen = None, []
        for active, status in ((True, "weak"), (True, "rise"), (True, "weak"), (False, "rise"), (True, "rise")):
            state = bd.next_state(state, active, status)
            seen.append(state)
        self.assertEqual(seen, ["someone", "mikkel", "mikkel", "off", "mikkel"])

    def test_reason(self):
        cases = ((None, "off", "none", f"unknown:{ACTIVE}"), (False, "off", "near", f"inactive:{ACTIVE}"),
                 (True, "mikkel", "near", f"active:{ACTIVE} phone:near"),
                 (True, "mikkel", "rise", f"active:{ACTIVE} phone:rise"),
                 (True, "mikkel", "weak", f"active:{ACTIVE} phone:held"),
                 (True, "mikkel", "stale", f"active:{ACTIVE} phone:held"),
                 (True, "mikkel", "none", f"active:{ACTIVE} phone:held"),
                 (True, "someone", "weak", f"active:{ACTIVE} phone:weak"),
                 (True, "someone", "stale", f"active:{ACTIVE} phone:stale"),
                 (True, "someone", "none", f"active:{ACTIVE} phone:none"))
        for active, state, status, expected in cases:
            with self.subTest(active=active, state=state, status=status):
                self.assertEqual(bd.reason_text(ACTIVE, active, state, status), expected)


class FitTests(unittest.TestCase):
    def test_a_title_that_fits_is_left_alone(self):
        self.assertEqual(bd.fit("Water the Alocasia: 0.7 L"), "Water the Alocasia: 0.7 L")
        self.assertEqual(bd.fit("x" * 32), "x" * 32)

    def test_a_longer_title_is_cut_back_to_a_whole_word(self):
        cases = (("Raining, close the living-room terrace door", "Raining, close the..."),
                 ("Spray the Alocasia for spider mites, 2 of 3", "Spray the Alocasia for spider..."),
                 ("Water the Alocasia: 0.7 L + 3/4 tsp fertiliser", "Water the Alocasia: 0.7 L..."),
                 ("x" * 33, "x" * 29 + "..."))
        for title, expected in cases:
            with self.subTest(title=title):
                self.assertEqual(bd.fit(title), expected)
                self.assertLessEqual(len(bd.fit(title)), bd.LINE_MAX)


class DueTests(unittest.TestCase):
    def test_a_timed_row_is_due_at_its_time(self):
        self.assertEqual(bd.due_at({"at": iso(NOW + HOUR)}, NOW), NOW + HOUR)

    def test_a_done_task_that_lost_its_time_is_due_on_the_day_it_names(self):
        cases = (({"done": "tap", "info": ["Every 7 days", "Due 04.11"]}, house(11, 4, 9)),
                 ({"done": "tap", "info": ["Due 30.12.25"]}, datetime(2025, 12, 30, 9, tzinfo=HOUSE)),
                 ({"done": "tap", "detail": "due today"}, house(11, 5, 9)),
                 ({"done": "tap", "detail": "due since 02.11"}, house(11, 2, 9)))
        for row, expected in cases:
            with self.subTest(row=row):
                self.assertEqual(bd.due_at(row, NOW), expected)

    def test_a_row_without_a_time_or_a_day_is_due_now(self):
        for row in ({}, {"info": ["Due 04.11"]}, {"detail": "due today"}, {"done": "tap", "info": ["Due soon"]},
                    {"done": "tap", "detail": "due"}, {"done": "tap", "detail": "6 days left, 11.11"},
                    {"done": "tap", "info": ["Due 31.02"]}, {"done": "tap", "info": "Due 04.11"},
                    {"detail": "soil 20%, water at 20%"}, {"detail": "no data since 03.11"},
                    {"done": "tap", "info": ["Every 10 runs"]}):
            with self.subTest(row=row):
                self.assertEqual(bd.due_at(row, NOW), NOW)

    def test_a_row_with_a_time_is_due_then_even_when_it_names_a_day(self):
        row = {"at": iso(NOW + 2 * DAY), "done": "tap", "info": ["Due 04.11"], "detail": "due today"}
        self.assertEqual(bd.due_at(row, NOW), NOW + 2 * DAY)

    def test_the_chip_counts_house_days_not_utc_days(self):
        now = datetime(2026, 11, 5, 22, 30, tzinfo=timezone.utc)  # 23:30 in the house
        cases = ((datetime(2026, 11, 4, 22, 59, tzinfo=timezone.utc), "overdue"),
                 (datetime(2026, 11, 4, 23, 0, tzinfo=timezone.utc), "today"),
                 (datetime(2026, 11, 5, 22, 59, tzinfo=timezone.utc), "today"),
                 (datetime(2026, 11, 5, 23, 0, tzinfo=timezone.utc), "tomorrow"),
                 (datetime(2026, 11, 6, 22, 59, tzinfo=timezone.utc), "tomorrow"),
                 (datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc), "2 d"),
                 (datetime(2026, 11, 19, 12, 0, tzinfo=timezone.utc), "14 d"))
        for due, chip in cases:
            with self.subTest(due=due):
                self.assertEqual(bd.due_chip(due, now), chip)


class RowTests(unittest.TestCase):
    """Which house_upcoming rows become lines: the real row builders' shapes, then the rules around them."""

    def lines(self, rows, **kw):
        return bd.task_lines(rows, NOW, **kw)

    def test_an_overdue_one_off_task_is_a_line_with_its_icon_and_an_overdue_chip(self):
        row = one_off(date(2026, 11, 3))
        self.assertNotIn("at", row)
        self.assertEqual(self.lines([row]), [bd.Line("Spray the Alocasia", "mdi:spray", "overdue")])

    def test_a_one_off_task_due_today_is_a_line_with_a_today_chip(self):
        row = one_off(date(2026, 11, 5))
        self.assertNotIn("at", row)
        self.assertEqual(self.lines([row]), [bd.Line("Spray the Alocasia", "mdi:spray", "today")])

    def test_a_one_off_task_of_another_year_is_overdue(self):
        self.assertEqual([line.due for line in self.lines([one_off(date(2025, 12, 30))])], ["overdue"])

    def test_a_one_off_task_due_tomorrow_is_a_line_whatever_the_room(self):
        for area in ("bathroom", "kitchen"):
            with self.subTest(area=area):
                self.assertEqual(self.lines([one_off(date(2026, 11, 6), area=area)]),
                                 [bd.Line("Spray the Alocasia", "mdi:spray", "tomorrow")])

    def test_a_calendar_plant_is_a_line_the_day_before_on_the_day_and_after(self):
        for days_ago, chip in ((6, "tomorrow"), (7, "today"), (9, "overdue")):
            with self.subTest(days_ago=days_ago):
                self.assertEqual(self.lines([watering(days_ago)]),
                                 [bd.Line("Water the Alocasia: 0.7 L", "mdi:watering-can-outline", chip)])

    def test_a_calendar_plant_in_another_room_is_a_line_the_day_before_on_the_day_and_after(self):
        for days_ago, chip in ((6, "tomorrow"), (7, "today"), (9, "overdue")):
            with self.subTest(days_ago=days_ago):
                self.assertEqual([line.due for line in self.lines([watering(days_ago, area="kitchen")])], [chip])

    def test_timed_rows_inside_7_days_are_lines_whatever_the_room(self):
        rows = [maint_row("past", "Earlier today", at=NOW - 2 * HOUR, area="kitchen"),
                maint_row("yesterday", "Since yesterday", at=NOW - DAY, area="kitchen"),
                maint_row("edge", "Exactly 7 days", at=NOW + 7 * DAY, area="kitchen"),
                maint_row("over", "A minute over 7 days", at=NOW + 7 * DAY + MIN, area="kitchen"),
                maint_row("tonight", "Tonight", at=NOW + 6 * HOUR, area="living_room"),
                maint_row("drum", "In three days", at=NOW + 3 * DAY, area="guest_bathroom")]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Since yesterday", "overdue"), ("Earlier today", "today"), ("Tonight", "today"),
                          ("In three days", "3 d"), ("Exactly 7 days", "7 d")])

    def test_a_row_three_days_out_is_a_line_whatever_the_room(self):
        row = maint_row("washer:drum", "Washer drum + drawer clean", at=NOW + 3 * DAY, area="guest_bathroom")
        self.assertEqual(self.lines([row]), [bd.Line("Washer drum + drawer clean", "mdi:information-outline", "3 d")])

    def test_beyond_7_days_only_the_bathroom_rows_within_14_days_are_lines(self):
        rows = [maint_row("a", "Bathroom in ten days", at=NOW + 10 * DAY, area="bathroom"),
                maint_row("b", "Kitchen in ten days", at=NOW + 10 * DAY, area="kitchen"),
                maint_row("c", "Bathroom at 14 days", at=NOW + 14 * DAY, area="bathroom"),
                maint_row("d", "Bathroom a minute later", at=NOW + 14 * DAY + MIN, area="bathroom"),
                maint_row("e", "No room in ten days", at=NOW + 10 * DAY),
                maint_row("f", "Guest bathroom in ten days", at=NOW + 10 * DAY, area="guest_bathroom"),
                maint_row("g", "Bathroom a minute over 7 days", at=NOW + 7 * DAY + MIN, area="bathroom")]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Bathroom a minute over 7 days", "7 d"), ("Bathroom in ten days", "10 d"),
                          ("Bathroom at 14 days", "14 d")])

    def test_what_is_due_comes_first_earliest_first_then_the_bathroom_rows_earliest_first(self):
        rows = [maint_row("later", "Bathroom later", at=NOW + 9 * DAY, area="bathroom"),
                maint_row("soon", "Bathroom soon", at=NOW + 8 * DAY, area="bathroom"),
                maint_row("kitchen", "Kitchen in six days", at=NOW + 6 * DAY, area="kitchen"),
                maint_row("far", "Kitchen in eight days", at=NOW + 8 * DAY, area="kitchen"),
                maint_row("afternoon", "This afternoon", at=NOW + 6 * HOUR),
                maint_row("old", "Overdue since yesterday", at=NOW - DAY)]
        lines = self.lines(rows, limit=10)
        self.assertEqual([line.text for line in lines], ["Overdue since yesterday", "This afternoon",
                                                         "Kitchen in six days", "Bathroom soon", "Bathroom later"])
        self.assertEqual([line.due for line in lines], ["overdue", "today", "6 d", "8 d", "9 d"])

    def test_at_most_three_lines(self):
        rows = [maint_row(f"r{i}", f"Row {i}", at=NOW + i * HOUR) for i in range(5)]
        self.assertEqual([line.text for line in self.lines(rows)], ["Row 0", "Row 1", "Row 2"])
        self.assertEqual([line.text for line in self.lines(rows, limit=1)], ["Row 0"])
        self.assertEqual(self.lines(rows, limit=0), [])
        self.assertEqual(self.lines(rows, limit=-1), [])

    def test_a_row_the_next_chip_passes_over_is_never_a_line(self):
        rows = [maint_row("skip", "Log row", at=NOW - HOUR, skip_next=True),
                maint_row("skip2", "Overdue log row", area="bathroom", tap_done=True, skip_next=True,
                          info=["Due 04.11"]),
                maint_row("skip3", "Log row with no time", area="bathroom", skip_next=True),
                maint_row("skip4", "Log row with no Done button", area="bathroom", tap_done=True, skip_next=True),
                maint_row("ok", "Real row", at=NOW + HOUR)]
        self.assertEqual([line.text for line in self.lines(rows)], ["Real row"])

    def test_a_row_for_other_people_is_not_a_line_for_mikkel(self):
        rows = [core.make_row("train:kristine", "train", "Train to Odense", at=NOW + 20 * MIN,
                              audience_users=["Kristine"]),
                core.make_row("train:both", "train", "Shared train", at=NOW + 30 * MIN,
                              audience_users=["Kristine", "mikkel"]),
                core.make_row("rain:living_room", "rain", "Rain forecast", at=NOW + HOUR, area="living_room")]
        self.assertEqual([line.text for line in self.lines(rows)], ["Shared train", "Rain forecast"])
        self.assertEqual([line.text for line in self.lines(rows, resident="Kristine")],
                         ["Train to Odense", "Shared train", "Rain forecast"])

    def test_a_row_with_no_time_is_due_now_and_a_line_with_a_today_chip(self):
        rows = [core.make_row("maint:battery:sensor.door", "maint", "Bathroom door battery", "no data since 03.11",
                              area="bathroom", audience="admin"),
                dry_plant("Ivy", area="kitchen"),
                maint_row("tapped", "Tapped without a day", area="living_room", tap_done=True,
                          info=["Every 10 runs"]),
                maint_row("info", "Due text without a Done button", area="bathroom", info=["Due 04.11"])]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Bathroom door battery", "today"), ("Due text without a Done button", "today"),
                          ("Water the Ivy: 0.5 L", "today"), ("Tapped without a day", "today")])

    def test_a_dry_sensor_plant_is_a_line_with_its_icon_whatever_the_room(self):
        for area in ("dining_room", "bathroom"):
            with self.subTest(area=area):
                row = dry_plant(area=area)
                self.assertNotIn("at", row)
                self.assertEqual(self.lines([row]),
                                 [bd.Line("Water the Philodendron: 0.5 L", "mdi:watering-can-outline", "today")])

    def test_what_is_due_now_comes_after_what_is_overdue_and_before_what_is_timed_ahead(self):
        rows = [maint_row("ahead", "In three days", at=NOW + 3 * DAY),
                dry_plant(),
                maint_row("old", "Overdue since yesterday", at=NOW - DAY),
                maint_row("tonight", "Tonight", at=NOW + 6 * HOUR)]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Overdue since yesterday", "overdue"), ("Water the Philodendron: 0.5 L", "today"),
                          ("Tonight", "today"), ("In three days", "3 d")])

    def test_a_row_needs_a_readable_title(self):
        rows = [{"id": "maint:x", "kind": "maint", "at": iso(NOW)}, {"id": "maint:y", "title": 5, "at": iso(NOW)},
                "not a row", None, {"id": "maint:z", "title": "Fine", "at": iso(NOW)}]
        self.assertEqual([line.text for line in self.lines(rows)], ["Fine"])
        self.assertEqual(self.lines(None), [])
        self.assertEqual(self.lines("rows"), [])

    def test_the_icon_is_the_rows_own_mdi_icon_or_nothing(self):
        rows = [{"id": "a", "title": "A", "at": iso(NOW), "icon": "mdi:spray"},
                {"id": "b", "title": "B", "at": iso(NOW), "icon": "spray"},
                {"id": "c", "title": "C", "at": iso(NOW), "icon": 5},
                {"id": "d", "title": "D", "at": iso(NOW)}]
        self.assertEqual([line.icon for line in self.lines(rows)], ["mdi:spray", "", ""])
        self.assertEqual([line.icon for line in self.lines(rows[3:])], [""])

    def test_a_long_title_is_cut_to_the_line_length(self):
        row = maint_row("long", "Check the Alocasia for spider mites on both sides of every leaf", at=NOW)
        [line] = self.lines([row])
        self.assertLessEqual(len(line.text), bd.LINE_MAX)
        self.assertEqual(line.text, "Check the Alocasia for spider...")

    def test_a_rain_row_is_a_line_like_any_other(self):
        row = core.make_row("rain:living_room", "rain", "Raining, close the living-room terrace door",
                            "rain on the open door", at=NOW.replace(minute=0), icon="mdi:weather-pouring",
                            area="living_room")
        self.assertEqual(self.lines([row]), [bd.Line("Raining, close the...", "mdi:weather-pouring", "today")])


class LeaseTests(unittest.TestCase):
    ROWS = [{"id": "a"}]

    def test_the_rows_while_the_lease_holds(self):
        self.assertEqual(bd.live_rows(upcoming(self.ROWS), NOW), self.ROWS)
        self.assertEqual(bd.live_rows(upcoming(self.ROWS, expires=NOW + SEC), NOW), self.ROWS)

    def test_none_once_it_has_run_out_or_cannot_be_read(self):
        for state in (upcoming(self.ROWS, expires=NOW), upcoming(self.ROWS, expires=NOW - MIN),
                      full("x", {"rows": self.ROWS}), full("x", {"rows": self.ROWS, "expires": "soon"}),
                      full("x", {"rows": self.ROWS, "expires": 5}), full("x", {"expires": iso(NOW + HOUR)}),
                      full("x", {"rows": "a", "expires": iso(NOW + HOUR)}), full("x"), {"state": "x"}, None):
            with self.subTest(state=state):
                self.assertEqual(bd.live_rows(state, NOW), [])


class TransitLineTests(unittest.TestCase):
    def line(self, full_state, label="S-tog", now=NOW):
        return bd.transit_line(label, full_state, now, TRANSIT_AGE)

    def test_a_disrupted_route_names_its_first_cancelled_departure_ahead(self):
        state = route("Disrupted", [departure(-5, "08:25", cancelled=True), departure(12, "08:42", cancelled=True),
                                    departure(22, "08:52", cancelled=True)])
        self.assertEqual(self.line(state), "S-tog: cancelled 08:42")

    def test_a_disrupted_route_prefers_a_cancellation_to_a_late_train_before_it(self):
        state = route("Disrupted", [departure(4, "08:34", delay=12), departure(14, "08:44", cancelled=True)])
        self.assertEqual(self.line(state), "S-tog: cancelled 08:44")

    def test_a_delayed_route_names_the_first_departure_that_is_late_or_cancelled(self):
        late = route("Delayed", [departure(4, "08:34", delay=12), departure(14, "08:44")])
        self.assertEqual(self.line(late), "S-tog: 08:34 +12 min")
        cancelled = route("Delayed", [departure(4, "08:34", cancelled=True), departure(14, "08:44", delay=15)])
        self.assertEqual(self.line(cancelled), "S-tog: cancelled 08:34")

    def test_a_delay_counts_from_the_threshold_the_route_publishes(self):
        departures = [departure(4, "08:34", delay=6)]
        self.assertEqual(self.line(route("Delayed", departures, threshold=5), label="Metro"), "Metro: 08:34 +6 min")
        self.assertEqual(self.line(route("Delayed", departures, threshold=10), label="Metro"), "Metro: delayed")
        self.assertEqual(self.line(route("Delayed", departures, threshold=None), label="Metro"), "Metro: delayed")

    def test_without_a_departure_to_name_it_says_the_status(self):
        self.assertEqual(self.line(route("Disrupted")), "S-tog: disrupted")
        self.assertEqual(self.line(route("Delayed", [departure(4, "08:34")])), "S-tog: delayed")
        self.assertEqual(self.line(route("Disrupted", [{"time": "08:34", "cancelled": "true"}])), "S-tog: disrupted")

    def test_a_route_that_is_fine_or_unreadable_has_no_line(self):
        for state in ("OK", "Unavailable", "unknown", "unavailable", None):
            with self.subTest(state=state):
                self.assertEqual(self.line(route(state, [departure(12, "08:42", cancelled=True)])), "")
        self.assertEqual(self.line(None), "")
        self.assertEqual(self.line({"state": "Disrupted"}), "")

    def test_old_data_is_not_a_line(self):
        deps = [departure(12, "08:42", cancelled=True)]
        self.assertEqual(self.line(route("Disrupted", deps, as_of=NOW - 45 * MIN)), "S-tog: cancelled 08:42")
        self.assertEqual(self.line(route("Disrupted", deps, as_of=NOW - 46 * MIN)), "")
        missing = route("Disrupted", deps)
        del missing["attributes"]["data_as_of"]
        self.assertEqual(self.line(missing), "")

    def test_data_as_of_is_house_time_when_it_has_no_zone(self):
        state = route("Disrupted", [departure(12, "08:42", cancelled=True)])
        state["attributes"]["data_as_of"] = "2026-11-05T08:25:00.123456"
        self.assertEqual(self.line(state), "S-tog: cancelled 08:42")
        state["attributes"]["data_as_of"] = "2026-11-05T07:25:00+00:00"
        self.assertEqual(self.line(state), "S-tog: cancelled 08:42")

    def test_every_bad_route_has_a_line_in_order_with_the_train_icon(self):
        metro = route("Disrupted", [departure(5, "08:35", cancelled=True)])
        stog = route("Delayed", [departure(9, "08:39", delay=11)])
        lines = bd.transit_lines([("Metro", metro), ("S-tog", route("OK")), ("S-tog", stog), ("Bus", None)], NOW,
                                 TRANSIT_AGE)
        self.assertEqual(lines, [bd.Line("Metro: cancelled 08:35", "mdi:train"),
                                 bd.Line("S-tog: 08:39 +11 min", "mdi:train")])


class PayloadTests(unittest.TestCase):
    def test_lines_are_padded_and_nothing_is_filled_without_mikkel(self):
        self.assertEqual(bd.payload("off", "r", "Mikkel", []), attrs_of(reason="r"))
        self.assertEqual(bd.payload("someone", "r", "Mikkel", []), attrs_of(reason="r"))

    def test_mikkel_has_a_person_a_greeting_and_his_lines(self):
        lines = [bd.Line("S-tog: cancelled 08:42", "mdi:train"), bd.Line("Spray the Alocasia", "mdi:spray", "overdue")]
        self.assertEqual(bd.payload("mikkel", "r", "Mikkel", lines),
                         attrs_of(reason="r", person="mikkel", greeting="Hi Mikkel",
                                  line1="S-tog: cancelled 08:42", icon1="mdi:train",
                                  line2="Spray the Alocasia", icon2="mdi:spray", due2="overdue"))


class PlayActionTests(unittest.TestCase):
    ALONE = [SPEAKER]
    GROUP = [SPEAKER, "media_player.kitchen"]

    def test_a_speaker_that_is_playing_is_paused_and_one_that_is_paused_plays_on_whatever_its_group(self):
        for members in (None, [], self.ALONE, self.GROUP, "media_player.a, media_player.b"):
            with self.subTest(members=members):
                self.assertEqual(bd.play_action("playing", members), "pause")
                self.assertEqual(bd.play_action("paused", members), "play")

    def test_anything_else_starts_the_radio_at_once_when_the_speaker_is_not_grouped(self):
        for state in ("idle", "off", "on", "standby", "buffering", "unavailable", "unknown", "", None):
            for members in (None, [], self.ALONE, "media_player.a, media_player.b", {"a": 1}):
                with self.subTest(state=state, members=members):
                    self.assertEqual(bd.play_action(state, members), "radio")

    def test_anything_else_leaves_the_group_first_when_the_speaker_is_grouped(self):
        for state in ("idle", "off", "on", "standby", "buffering", "unavailable", "unknown", "", None):
            with self.subTest(state=state):
                self.assertEqual(bd.play_action(state, self.GROUP), "unjoin_radio")
                self.assertEqual(bd.play_action(state, self.GROUP + ["media_player.living_room"]), "unjoin_radio")


class WindowTests(unittest.TestCase):
    def test_the_rain_window_is_06_to_12_in_house_time(self):
        cases = ((house(11, 5, 5, 59), False), (datetime(2026, 11, 5, 5, 59, 59, tzinfo=HOUSE), False),
                 (house(11, 5, 6, 0), True), (house(11, 5, 11, 59), True),
                 (datetime(2026, 11, 5, 11, 59, 59, tzinfo=HOUSE), True), (house(11, 5, 12, 0), False),
                 (house(11, 5, 0, 0), False), (house(11, 5, 20, 0), False))
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(bd.in_window(moment, bd.RAIN_FROM, bd.RAIN_TO), expected)

    def test_the_alarm_window_is_21_to_02_in_house_time_through_midnight(self):
        cases = ((house(11, 5, 20, 59), False), (datetime(2026, 11, 5, 20, 59, 59, tzinfo=HOUSE), False),
                 (house(11, 5, 21, 0), True), (house(11, 5, 23, 59), True), (house(11, 6, 0, 0), True),
                 (house(11, 6, 1, 59), True), (datetime(2026, 11, 6, 1, 59, 59, tzinfo=HOUSE), True),
                 (house(11, 6, 2, 0), False), (house(11, 6, 6, 0), False), (house(11, 5, 12, 0), False),
                 (house(11, 5, 20, 0), False))
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(bd.in_window(moment, bd.ALARM_FROM, bd.ALARM_TO), expected)

    def test_a_window_is_the_house_clock_in_winter_and_in_summer_time(self):
        cases = ((datetime(2026, 11, 5, 5, 0, tzinfo=timezone.utc), True), (datetime(2026, 11, 5, 4, 59, tzinfo=timezone.utc), False),
                 (datetime(2026, 7, 1, 4, 0, tzinfo=timezone.utc), True), (datetime(2026, 7, 1, 3, 59, tzinfo=timezone.utc), False),
                 (datetime(2026, 7, 1, 10, 0, tzinfo=timezone.utc), False), (datetime(2026, 7, 1, 9, 59, tzinfo=timezone.utc), True))
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(bd.in_window(moment, bd.RAIN_FROM, bd.RAIN_TO), expected)


class WeighingMomentTests(unittest.TestCase):
    def moment(self, age, previous="2026-11-02T06:47:03+00:00", now=NOW):
        return bd.weighing_moment(previous, full(iso(now - age)), now)

    def test_a_change_to_a_moment_just_gone_is_a_weighing_at_that_moment(self):
        self.assertEqual(self.moment(2 * SEC), NOW - 2 * SEC)
        self.assertEqual(self.moment(0 * SEC), NOW)

    def test_the_moment_may_be_at_most_120_seconds_old(self):
        self.assertEqual(self.moment(120 * SEC), NOW - 120 * SEC)
        self.assertIsNone(self.moment(121 * SEC))
        self.assertIsNone(self.moment(3 * DAY))

    def test_a_moment_still_to_come_is_not_a_weighing(self):
        self.assertIsNone(self.moment(-1 * SEC))

    def test_the_same_state_again_is_not_one_however_new_its_update(self):
        recent = iso(NOW - 2 * SEC)
        self.assertIsNone(bd.weighing_moment(recent, full(recent, updated=NOW), NOW))

    def test_the_first_state_the_app_sees_is_a_change(self):
        self.assertEqual(bd.weighing_moment(None, full(iso(NOW - 2 * SEC)), NOW), NOW - 2 * SEC)

    def test_a_state_that_is_no_moment_is_not_a_weighing(self):
        for state in ("unknown", "unavailable", "", None, 5, "73.3"):
            with self.subTest(state=state):
                self.assertIsNone(bd.weighing_moment("2026-11-02T06:47:03+00:00", full(state), NOW))
        for state in (None, {}, "2026-11-05T07:29:58+00:00"):
            with self.subTest(full=state):
                self.assertIsNone(bd.weighing_moment(None, state, NOW))

    def test_a_moment_in_another_zone_is_the_same_moment(self):
        local = (NOW - 5 * SEC).astimezone(HOUSE).isoformat()
        self.assertEqual(bd.weighing_moment(None, full(local), NOW), NOW - 5 * SEC)


class WeighedValueTests(unittest.TestCase):
    def test_a_reading_updated_with_the_weighing_counts(self):
        for offset in (0, 1, -1, 30, -30):
            with self.subTest(offset=offset):
                self.assertEqual(bd.weighed_value(full("73.3", updated=NOW + offset * SEC), NOW), 73.3)

    def test_a_reading_that_kept_its_old_update_does_not(self):
        for offset in (31, -31, 3600, 3 * 86400):
            with self.subTest(offset=offset):
                self.assertIsNone(bd.weighed_value(full("73.3", updated=NOW - offset * SEC), NOW))

    def test_a_reading_needs_a_number_an_update_and_an_anchor(self):
        for state in ("unknown", "unavailable", "", None, "nan"):
            with self.subTest(state=state):
                self.assertIsNone(bd.weighed_value(full(state), NOW))
        self.assertIsNone(bd.weighed_value({"state": "73.3"}, NOW))
        self.assertIsNone(bd.weighed_value(None, NOW))
        self.assertIsNone(bd.weighed_value(full("73.3"), None))


LIVE_STATISTICS = {  # recorder.get_statistics as the websocket answered it, through AppDaemon's call_service
    "id": 1, "type": "result", "success": True,
    "result": {"context": {"id": "01M6Y1N7KHT77XK6K4YQ0E3F0Z", "parent_id": None, "user_id": "f4fda494358943beaf8ad2c70db099f8"},
               "response": {"statistics": {
                   "sensor.mikkel_body_fat": [
                       {"start": "2026-10-05T06:00:00+00:00", "end": "2026-10-05T07:00:00+00:00", "mean": 20.1},
                       {"start": "2026-10-06T06:00:00+00:00", "end": "2026-10-06T07:00:00+00:00", "mean": 19.9},
                       {"start": "2026-10-08T06:00:00+00:00", "end": "2026-10-08T07:00:00+00:00", "mean": 20.0},
                       {"start": "2026-10-12T04:00:00+00:00", "end": "2026-10-12T05:00:00+00:00", "mean": 19.6}],
                   "sensor.mikkel_weight": [
                       {"start": "2026-10-05T06:00:00+00:00", "end": "2026-10-05T07:00:00+00:00", "mean": 74.3},
                       {"start": "2026-10-06T06:00:00+00:00", "end": "2026-10-06T07:00:00+00:00", "mean": 74.3},
                       {"start": "2026-10-08T06:00:00+00:00", "end": "2026-10-08T07:00:00+00:00", "mean": 74.1},
                       {"start": "2026-10-12T04:00:00+00:00", "end": "2026-10-12T05:00:00+00:00", "mean": 73.9}]}}}}


class StatisticsTests(unittest.TestCase):
    IDS = (WEIGHT, FAT)
    TARGET = datetime(2026, 10, 9, 17, 0, tzinfo=timezone.utc)

    def test_the_rows_are_under_result_response_and_statistics(self):
        stats = bd.statistics_rows(LIVE_STATISTICS)
        self.assertEqual(set(stats), {WEIGHT, FAT})
        self.assertEqual(len(stats[WEIGHT]), 4)

    def test_other_shapes_have_no_rows(self):
        for resp in (None, {}, [], "x", 5, {"result": None}, {"result": {"response": []}}, {"result": {"response": {"statistics": []}}}):
            with self.subTest(resp=resp):
                self.assertEqual(bd.statistics_rows(resp), {})

    def test_an_answer_without_the_websocket_envelope_is_read_too(self):
        self.assertEqual(set(bd.statistics_rows({"statistics": {WEIGHT: [], FAT: []}})), {WEIGHT, FAT})
        self.assertEqual(set(bd.statistics_rows({"response": {"statistics": {WEIGHT: []}}})), {WEIGHT})

    def test_the_nearest_hour_is_taken_from_the_live_shape(self):
        self.assertEqual(bd.before_values(LIVE_STATISTICS, self.IDS, self.TARGET), (74.1, 20.0))
        self.assertEqual(bd.before_values(LIVE_STATISTICS, self.IDS, datetime(2026, 10, 11, 9, 0, tzinfo=timezone.utc)),
                         (73.9, 19.6))

    def test_a_row_is_taken_within_three_days_and_not_beyond(self):
        target = utc(7)
        for offset, expected in ((timedelta(days=3), True), (-timedelta(days=3), True),
                                 (timedelta(days=3, minutes=1), False), (-timedelta(days=3, minutes=1), False)):
            with self.subTest(offset=offset):
                resp = stats_answer([stat_row(target + offset, 74.0)], [stat_row(target + offset, 20.0)])
                self.assertEqual(bd.before_values(resp, self.IDS, target), (74.0, 20.0) if expected else None)

    def test_the_nearest_wins_and_a_tie_goes_to_the_earlier_hour(self):
        target = utc(7)
        rows = [stat_row(target - 2 * DAY, 75.0), stat_row(target + DAY, 74.0), stat_row(target + 2 * DAY, 73.0)]
        fat = [stat_row(row_start, 20.0) for row_start in (target - 2 * DAY, target + DAY, target + 2 * DAY)]
        self.assertEqual(bd.before_values(stats_answer(rows, fat), self.IDS, target), (74.0, 20.0))
        tie = stats_answer([stat_row(target - DAY, 75.0), stat_row(target + DAY, 74.0)],
                           [stat_row(target - DAY, 21.0), stat_row(target + DAY, 20.0)])
        self.assertEqual(bd.before_values(tie, self.IDS, target), (75.0, 21.0))

    def test_only_an_hour_both_sensors_have_counts(self):
        target = utc(7)
        resp = stats_answer([stat_row(target - HOUR, 75.0), stat_row(target + DAY, 74.0)],
                            [stat_row(target + DAY, 20.0)])
        self.assertEqual(bd.before_values(resp, self.IDS, target), (74.0, 20.0))
        self.assertIsNone(bd.before_values(stats_answer([stat_row(target, 75.0)], []), self.IDS, target))
        self.assertIsNone(bd.before_values(stats_answer([], [stat_row(target, 20.0)]), self.IDS, target))

    def test_a_row_needs_a_start_and_a_mean(self):
        target = utc(7)
        bad = [{"start": iso(target), "mean": None}, {"start": iso(target), "mean": "unknown"},
               {"start": "soon", "mean": 74.0}, {"mean": 74.0}, {"start": iso(target)}, "row", None,
               {"start": iso(target), "mean": float("nan")}]
        self.assertIsNone(bd.before_values(stats_answer(bad, bad), self.IDS, target))
        mixed = stats_answer(bad + [stat_row(target + HOUR, 74.0)], bad + [stat_row(target + HOUR, 20.0)])
        self.assertEqual(bd.before_values(mixed, self.IDS, target), (74.0, 20.0))

    def test_nothing_is_none(self):
        for resp in (None, {}, stats_answer([], []), {"result": {"response": {"statistics": {}}}},
                     stats_answer("rows", "rows")):
            with self.subTest(resp=resp):
                self.assertIsNone(bd.before_values(resp, self.IDS, utc(7)))


class WeighingTextTests(unittest.TestCase):
    def test_a_value_has_one_decimal_and_a_decimal_point(self):
        for value, text in ((73.3, "73.3"), (73.0, "73.0"), (20, "20.0"), (19.96, "20.0"), (100.04, "100.0")):
            with self.subTest(value=value):
                self.assertEqual(bd.kg_text(value), text)

    def test_a_change_of_at_most_0_3_kg_either_way_reads_plus_minus_zero(self):
        for delta in (0, 0.1, -0.1, 0.2999, -0.2999, 0.3, -0.3, 14.82 - 14.52, 14.52 - 14.82, 74.1 * 0.2 - 73.8 * 0.2):
            with self.subTest(delta=delta):
                self.assertEqual(bd.change_text(delta), "±0")

    def test_a_bigger_change_has_its_sign_and_one_decimal(self):
        cases = ((0.31, "+0.3"), (-0.31, "−0.3"), (0.4, "+0.4"), (-0.4335, "−0.4"), (0.1335 + 0.2, "+0.3"), (2.0, "+2.0"),
                 (-0.64, "−0.6"), (-12.34, "−12.3"))
        for delta, text in cases:
            with self.subTest(delta=delta):
                self.assertEqual(bd.change_text(delta), text)

    def test_a_fall_is_written_with_a_true_minus_sign(self):
        text = bd.change_text(-0.64)
        self.assertEqual(text[0], "−")
        self.assertNotIn("-", text)


class WeighingLinesTests(unittest.TestCase):
    def test_the_card_has_the_weight_and_fat_then_the_change_in_fat_and_in_the_rest(self):
        lines = bd.weighing_lines(73.6, 21.0, (73.0, 22.0))
        self.assertEqual(lines, [bd.Line("73.6 kg · 21.0% fat", "mdi:scale-bathroom", "73.6 kg"),
                                 bd.Line("Fat 30 d: −0.6 kg", "mdi:water-percent", "−0.6 kg"),
                                 bd.Line("Lean 30 d: +1.2 kg", "mdi:arm-flex", "+1.2 kg")])

    def test_fat_mass_is_weight_times_fat_percentage_and_the_rest_is_what_is_left(self):
        [_, fat, rest] = bd.weighing_lines(80.0, 25.0, (82.0, 30.0))  # 20 kg and 60 kg now, 24.6 kg and 57.4 kg then
        self.assertEqual((fat.text, rest.text), ("Fat 30 d: −4.6 kg", "Lean 30 d: +2.6 kg"))

    def test_changes_inside_the_dead_band_read_plus_minus_zero(self):
        [_, fat, rest] = bd.weighing_lines(73.3, 20.0, (73.3, 20.0))
        self.assertEqual((fat.text, rest.text), ("Fat 30 d: ±0 kg", "Lean 30 d: ±0 kg"))
        self.assertEqual((fat.due, rest.due), ("±0 kg", "±0 kg"))

    def test_the_dead_band_is_for_each_change_alone(self):
        [_, fat, rest] = bd.weighing_lines(73.3, 20.0, (74.1, 20.0))
        self.assertEqual((fat.text, rest.text), ("Fat 30 d: ±0 kg", "Lean 30 d: −0.6 kg"))

    def test_without_a_row_to_compare_with_only_the_first_line_shows(self):
        self.assertEqual(bd.weighing_lines(73.3, 20.0, None),
                         [bd.Line("73.3 kg · 20.0% fat", "mdi:scale-bathroom", "73.3 kg")])

    def test_without_a_fat_percentage_it_is_the_weight_alone(self):
        for before in (None, (74.1, 20.0)):
            with self.subTest(before=before):
                self.assertEqual(bd.weighing_lines(73.3, None, before),
                                 [bd.Line("73.3 kg", "mdi:scale-bathroom", "73.3 kg")])

    def test_every_line_fits(self):
        for line in bd.weighing_lines(123.4, 45.6, (65.4, 12.3)):
            self.assertLessEqual(len(line.text), bd.LINE_MAX)
            self.assertLessEqual(len(line.due), 8)


class ForecastRunTests(unittest.TestCase):
    def run_of(self, *hours, now=NOW):
        return bd.forecast_run(forecast_hours(*hours), now)

    def test_the_run_is_from_the_start_of_its_first_hour_to_the_end_of_its_last(self):
        self.assertEqual(self.run_of((utc(8), 0.0), (utc(9), 0.6), (utc(10), 0.4), (utc(11), 0.0), (utc(12), 2.0)),
                         (utc(9), utc(11)))

    def test_an_hour_that_is_going_on_counts_and_one_that_is_over_does_not(self):
        self.assertEqual(self.run_of((utc(7), 0.5), (utc(8), 0.5)), (utc(7), utc(9)))
        self.assertIsNone(self.run_of((utc(6), 5.0), (utc(7), 0.0)))
        self.assertEqual(self.run_of((utc(6), 5.0), (utc(7), 0.0), (utc(8), 1.0)), (utc(8), utc(9)))

    def test_a_run_has_to_start_within_the_next_four_hours(self):
        self.assertEqual(self.run_of((utc(11), 0.5)), (utc(11), utc(12)))  # 11:00 is before 11:30
        self.assertIsNone(self.run_of((utc(12), 0.5)))
        self.assertIsNone(self.run_of((utc(8), 0.0), (utc(11), 0.0), (utc(12), 3.0)))
        self.assertIsNone(self.run_of((utc(11), 0.5), now=utc(7)))  # 11:00 is four hours from 07:00
        self.assertEqual(self.run_of((utc(10), 0.5), now=utc(7)), (utc(10), utc(11)))

    def test_a_hour_needs_the_least_rain_the_station_counts(self):
        self.assertEqual(self.run_of((utc(9), rain.MIN_MM)), (utc(9), utc(10)))
        self.assertIsNone(self.run_of((utc(9), rain.MIN_MM - 0.01)))
        self.assertEqual(self.run_of((utc(9), 0.29), (utc(10), 0.3)), (utc(10), utc(11)))

    def test_a_dry_hour_or_a_missing_one_ends_the_run(self):
        self.assertEqual(self.run_of((utc(8), 0.5), (utc(9), 0.0), (utc(10), 0.5)), (utc(8), utc(9)))
        self.assertEqual(self.run_of((utc(8), 0.5), (utc(10), 0.5)), (utc(8), utc(9)))
        self.assertEqual(self.run_of((utc(8), 0.5), (utc(9), 0.5), (utc(11), 0.5)), (utc(8), utc(10)))

    def test_a_run_goes_on_past_the_four_hours_when_the_forecast_says_so(self):
        self.assertEqual(self.run_of((utc(10), 0.5), (utc(11), 0.5), (utc(12), 0.5), (utc(13), 0.5), (utc(14), 0.0)),
                         (utc(10), utc(14)))

    def test_an_hour_without_a_precipitation_figure_is_dry(self):
        hours = rain.forecast_hours([{"datetime": iso(utc(9)), "precipitation": None},
                                     {"datetime": iso(utc(10))}, {"datetime": iso(utc(11)), "precipitation": 1.0}])
        self.assertEqual(bd.forecast_run(hours, NOW), (utc(11), utc(12)))

    def test_no_hours_no_run(self):
        self.assertIsNone(bd.forecast_run([], NOW))
        self.assertIsNone(self.run_of((utc(9), 0.0), (utc(10), 0.0)))


class HomeRunTests(unittest.TestCase):
    def run_of(self, *hours, now=NOW):
        return bd.home_run(forecast_hours(*hours), now)

    def rainy(self, first, last, day=5):
        """Rain in every house hour from `first` to `last`, both included."""
        return [(house(11, day, hour), 1.0) for hour in range(first, last + 1)]

    def test_the_run_is_from_the_start_of_its_first_hour_to_the_end_of_its_last(self):
        hours = ((house(11, 5, 16), 0.0), (house(11, 5, 17), 0.6), (house(11, 5, 18), 0.4), (house(11, 5, 19), 0.0))
        self.assertEqual(self.run_of(*hours), (house(11, 5, 17), house(11, 5, 19)))

    def test_the_hours_that_start_from_16_up_to_20_are_the_window(self):
        self.assertIsNone(self.run_of((house(11, 5, 15), 1.0)))
        self.assertEqual(self.run_of((house(11, 5, 16), 1.0)), (house(11, 5, 16), house(11, 5, 17)))
        self.assertEqual(self.run_of((house(11, 5, 19), 1.0)), (house(11, 5, 19), house(11, 5, 20)))
        self.assertIsNone(self.run_of((house(11, 5, 20), 1.0)))

    def test_a_run_is_clipped_to_the_window(self):
        self.assertEqual(self.run_of(*self.rainy(14, 22)), (house(11, 5, 16), house(11, 5, 20)))
        self.assertEqual(self.run_of(*self.rainy(12, 17)), (house(11, 5, 16), house(11, 5, 18)))
        self.assertEqual(self.run_of(*self.rainy(18, 23)), (house(11, 5, 18), house(11, 5, 20)))

    def test_only_the_first_run_counts_and_a_dry_or_missing_hour_ends_it(self):
        self.assertEqual(self.run_of((house(11, 5, 16), 0.5), (house(11, 5, 17), 0.0), (house(11, 5, 18), 0.5)),
                         (house(11, 5, 16), house(11, 5, 17)))
        self.assertEqual(self.run_of((house(11, 5, 16), 0.5), (house(11, 5, 18), 0.5)),
                         (house(11, 5, 16), house(11, 5, 17)))
        self.assertEqual(self.run_of((house(11, 5, 16), 0.2), (house(11, 5, 17), 0.5), (house(11, 5, 18), 0.5),
                                     (house(11, 5, 19), 0.1)), (house(11, 5, 17), house(11, 5, 19)))

    def test_an_hour_needs_the_least_rain_the_station_counts(self):
        self.assertEqual(self.run_of((house(11, 5, 17), rain.MIN_MM)), (house(11, 5, 17), house(11, 5, 18)))
        self.assertIsNone(self.run_of((house(11, 5, 17), rain.MIN_MM - 0.01)))

    def test_only_the_house_day_of_now_has_a_window(self):
        yesterday, tomorrow = self.rainy(16, 19, day=4), self.rainy(16, 19, day=6)
        self.assertIsNone(self.run_of(*yesterday, *tomorrow))
        self.assertEqual(self.run_of(*yesterday, *self.rainy(18, 19), *tomorrow), (house(11, 5, 18), house(11, 5, 20)))
        late = datetime(2026, 11, 5, 23, 30, tzinfo=timezone.utc)  # 00:30 on the 6th in the house
        self.assertEqual(self.run_of(*self.rainy(16, 19), *self.rainy(17, 18, day=6), now=late),
                         (house(11, 6, 17), house(11, 6, 19)))

    def test_the_window_is_the_house_clock_in_summer_time_too(self):
        summer = datetime(2026, 7, 1, 5, 30, tzinfo=timezone.utc)  # 07:30 in the house
        hours = [(datetime(2026, 7, 1, hour, tzinfo=HOUSE), 1.0) for hour in (15, 16, 17, 20)]
        self.assertEqual(self.run_of(*hours, now=summer),
                         (datetime(2026, 7, 1, 16, tzinfo=HOUSE), datetime(2026, 7, 1, 18, tzinfo=HOUSE)))

    def test_no_hours_no_run(self):
        self.assertIsNone(bd.home_run([], NOW))
        self.assertIsNone(self.run_of((house(11, 5, 17), 0.0), (house(11, 5, 18), 0.1)))


class WorkdayTests(unittest.TestCase):
    def test_monday_to_friday_in_house_time(self):
        for day, expected in ((2, True), (3, True), (4, True), (5, True), (6, True), (7, False), (8, False)):
            with self.subTest(day=day):
                self.assertEqual(bd.workday(house(11, day, 8, 30)), expected)

    def test_the_day_is_the_house_day_not_the_utc_day(self):
        cases = ((datetime(2026, 11, 6, 22, 59, tzinfo=timezone.utc), True),  # Friday 23:59 in the house
                 (datetime(2026, 11, 6, 23, 0, tzinfo=timezone.utc), False),  # Saturday 00:00
                 (datetime(2026, 11, 8, 22, 59, tzinfo=timezone.utc), False),  # Sunday 23:59
                 (datetime(2026, 11, 8, 23, 0, tzinfo=timezone.utc), True))  # Monday 00:00
        for moment, expected in cases:
            with self.subTest(moment=moment):
                self.assertEqual(bd.workday(moment), expected)


class RainLineTests(unittest.TestCase):
    FETCHED = NOW - 5 * MIN
    OUT = ((utc(8), 0.6), (utc(9), 0.4))
    HOME = ((house(11, 5, 17), 0.5), (house(11, 5, 18), 0.5))

    def line(self, *hours, raining=False, fetched=FETCHED, now=NOW):
        return bd.rain_line(raining, forecast_hours(*hours), fetched, now)

    def test_it_raining_now_is_raining_now_whatever_the_forecast(self):
        expected = bd.Line("Raining now", "mdi:weather-rainy", "now")
        self.assertEqual(self.line(raining=True), expected)
        self.assertEqual(self.line((utc(9), 3.0), raining=True), expected)
        self.assertEqual(bd.rain_line(True, [], None, NOW), expected)

    def test_the_first_run_is_named_by_the_house_hours_it_covers(self):
        self.assertEqual(self.line((utc(8), 0.6), (utc(9), 0.4)), bd.Line("Rain 09-11", "mdi:weather-rainy", "09-11"))
        self.assertEqual(self.line((utc(8), 0.6)), bd.Line("Rain 09-10", "mdi:weather-rainy", "09-10"))

    def test_the_hours_have_two_digits(self):
        self.assertEqual(self.line((utc(5), 0.6), now=utc(4, 30)).text, "Rain 06-07")

    def test_a_run_that_goes_on_past_the_next_four_hours_is_named_by_its_start(self):
        self.assertEqual(self.line((utc(10), 0.5), (utc(11), 0.5), (utc(12), 0.5), (utc(13), 0.1)),
                         bd.Line("Rain from 11", "mdi:weather-rainy", "from 11"))

    def test_a_run_that_ends_just_as_the_four_hours_do_is_not_one_that_goes_on(self):
        self.assertEqual(self.line((utc(9), 0.5), (utc(10), 0.5), now=utc(7)).text, "Rain 10-12")
        self.assertEqual(self.line((utc(10), 0.5), (utc(11), 0.5), now=utc(7)).text, "Rain from 11")

    def test_no_rain_in_the_next_four_hours_is_no_line(self):
        self.assertIsNone(self.line((utc(9), 0.0), (utc(10), 0.1)))
        self.assertIsNone(self.line((utc(12), 5.0)))
        self.assertIsNone(self.line())

    def test_a_forecast_that_was_not_fetched_or_is_too_old_is_not_used(self):
        hours = ((utc(9), 1.0),)
        self.assertEqual(self.line(*hours, fetched=NOW - rain.MAX_AGE).text, "Rain 10-11")
        self.assertIsNone(self.line(*hours, fetched=NOW - rain.MAX_AGE - SEC))
        self.assertIsNone(self.line(*hours, fetched=None))

    def test_a_workday_adds_the_way_home_after_the_way_out(self):
        past_four_hours = ((utc(10), 0.5), (utc(11), 0.5), (utc(12), 0.5), (utc(13), 0.1))
        cases = ((self.OUT + self.HOME, False, bd.Line("Rain 09-11 · home 17-19", "mdi:weather-rainy", "09-11")),
                 (past_four_hours + self.HOME, False,
                  bd.Line("Rain from 11 · home 17-19", "mdi:weather-rainy", "from 11")),
                 (self.HOME, True, bd.Line("Raining now · home 17-19", "mdi:weather-rainy", "now")),
                 (self.OUT + self.HOME, True, bd.Line("Raining now · home 17-19", "mdi:weather-rainy", "now")))
        for hours, raining, expected in cases:
            with self.subTest(hours=hours, raining=raining):
                self.assertEqual(self.line(*hours, raining=raining), expected)

    def test_with_no_rain_on_the_way_out_the_way_home_stands_alone(self):
        self.assertEqual(self.line(*self.HOME), bd.Line("Rain home 17-19", "mdi:weather-rainy", "17-19"))
        self.assertEqual(self.line((utc(8), 0.0), (utc(9), 0.1), *self.HOME).text, "Rain home 17-19")

    def test_a_dry_way_home_leaves_the_line_as_it_was(self):
        dry = ((house(11, 5, 17), 0.0), (house(11, 5, 18), rain.MIN_MM - 0.01))
        self.assertEqual(self.line(*self.OUT, *dry), bd.Line("Rain 09-11", "mdi:weather-rainy", "09-11"))
        self.assertEqual(self.line(*dry, raining=True), bd.Line("Raining now", "mdi:weather-rainy", "now"))
        self.assertIsNone(self.line(*dry))

    def test_the_way_home_is_the_first_run_inside_16_to_20_clipped_to_it(self):
        cases = (((16, 17), "home 16-18"), ((19,), "home 19-20"), ((14, 15, 16, 17, 18, 19, 20, 21), "home 16-20"),
                 ((18, 19, 20, 21), "home 18-20"), ((17, 19), "home 17-18"), ((15,), None), ((20,), None),
                 ((13, 14, 15), None))
        for rainy, expected in cases:
            with self.subTest(rainy=rainy):
                line = self.line(*[(house(11, 5, hour), 1.0) for hour in rainy])
                self.assertEqual(line.text if line else None, f"Rain {expected}" if expected else None)

    def test_the_chip_of_the_way_home_alone_is_its_hours(self):
        self.assertEqual(self.line(*[(house(11, 5, hour), 1.0) for hour in range(14, 22)]).due, "16-20")

    def test_only_today_has_a_way_home(self):
        for day in (4, 6):
            with self.subTest(day=day):
                self.assertIsNone(self.line((house(11, day, 17), 0.5), (house(11, day, 18), 0.5)))

    def test_only_a_workday_has_a_way_home(self):
        for day, home in ((2, True), (3, True), (4, True), (5, True), (6, True), (7, False), (8, False)):
            now = house(11, day, 8, 30)
            with self.subTest(day=day):
                line = bd.rain_line(False, forecast_hours((house(11, day, 17), 0.5)), now - 5 * MIN, now)
                self.assertEqual(line.text if line else None, "Rain home 17-18" if home else None)

    def test_a_weekend_has_the_way_out_only(self):
        hours = forecast_hours((utc(8, day=7), 0.6), (utc(9, day=7), 0.4), (house(11, 7, 17), 0.5))
        fetched = SATURDAY - 5 * MIN
        self.assertEqual(bd.rain_line(False, hours, fetched, SATURDAY),
                         bd.Line("Rain 09-11", "mdi:weather-rainy", "09-11"))
        self.assertEqual(bd.rain_line(True, hours, fetched, SATURDAY),
                         bd.Line("Raining now", "mdi:weather-rainy", "now"))
        self.assertIsNone(bd.rain_line(False, forecast_hours((house(11, 7, 17), 0.5)), fetched, SATURDAY))

    def test_the_way_home_follows_the_house_day_at_midnight(self):
        friday_night = datetime(2026, 11, 6, 23, 30, tzinfo=timezone.utc)  # Saturday 00:30 in the house
        sunday_night = datetime(2026, 11, 8, 23, 30, tzinfo=timezone.utc)  # Monday 00:30 in the house
        for now, day, expected in ((friday_night, 7, None), (sunday_night, 9, "Rain home 17-18")):
            with self.subTest(now=now):
                line = bd.rain_line(False, forecast_hours((house(11, day, 17), 0.5)), now - 5 * MIN, now)
                self.assertEqual(line.text if line else None, expected)

    def test_the_way_home_needs_a_fresh_forecast_and_raining_now_does_not(self):
        hours = self.OUT + self.HOME
        self.assertEqual(self.line(*hours, fetched=NOW - rain.MAX_AGE).text, "Rain 09-11 · home 17-19")
        for fetched in (NOW - rain.MAX_AGE - SEC, None):
            with self.subTest(fetched=fetched):
                self.assertIsNone(self.line(*hours, fetched=fetched))
                self.assertEqual(self.line(*hours, raining=True, fetched=fetched),
                                 bd.Line("Raining now", "mdi:weather-rainy", "now"))

    def test_every_variant_fits_the_line(self):
        longest = self.line(*[(utc(hour), 0.5) for hour in (8, 9, 10, 11)], *self.HOME)
        self.assertEqual(longest.text, "Rain from 09 · home 17-19")
        for line in (longest, self.line(*self.HOME, raining=True), self.line(*self.HOME),
                     self.line(*self.OUT, *self.HOME)):
            self.assertLessEqual(len(line.text), bd.LINE_MAX)


class AlarmLineTests(unittest.TestCase):
    def test_an_alarm_that_is_on_says_when_it_wakes_him(self):
        self.assertEqual(bd.alarm_line("on", "09:15:00"), bd.Line("Alarm 09:15", "mdi:alarm", "09:15"))
        self.assertEqual(bd.alarm_line("on", "06:05:00").text, "Alarm 06:05")
        self.assertEqual(bd.alarm_line("on", "23:59:59").text, "Alarm 23:59")

    def test_an_alarm_that_is_off_says_so(self):
        for clock in ("09:15:00", None, "unknown"):
            with self.subTest(clock=clock):
                self.assertEqual(bd.alarm_line("off", clock), bd.Line("No alarm", "mdi:alarm", "off"))

    def test_an_alarm_that_cannot_be_read_is_no_line(self):
        for armed in ("unknown", "unavailable", "", None, True, "ON"):
            with self.subTest(armed=armed):
                self.assertIsNone(bd.alarm_line(armed, "09:15:00"))

    def test_a_time_that_cannot_be_read_leaves_the_time_out(self):
        for clock in ("unknown", "unavailable", "", None, "soon", 915):
            with self.subTest(clock=clock):
                self.assertEqual(bd.alarm_line("on", clock), bd.Line("Alarm", "mdi:alarm", ""))

    def test_the_texts_fit_the_line(self):
        for line in (bd.alarm_line("on", "09:15:00"), bd.alarm_line("off", None)):
            self.assertLessEqual(len(line.text), bd.LINE_MAX)


class AlarmStartTests(unittest.TestCase):
    def test_the_helpers_time_is_when_the_line_starts(self):
        for clock, expected in (("21:00:00", dtime(21)), ("22:30:00", dtime(22, 30)), ("20:00:00", dtime(20)),
                                ("00:15:00", dtime(0, 15)), ("21:00", dtime(21))):
            with self.subTest(clock=clock):
                self.assertEqual(bd.alarm_start(clock), expected)

    def test_a_state_that_is_no_time_falls_back_to_21(self):
        self.assertEqual(bd.ALARM_FROM, dtime(21))
        for clock in ("unknown", "unavailable", "", None, "soon", 2100, True, "2026-11-05", "2026-11-05 22:00:00",
                      "25:00:00"):
            with self.subTest(clock=clock):
                self.assertEqual(bd.alarm_start(clock), bd.ALARM_FROM)


class WeighedStateTests(unittest.TestCase):
    def test_a_weighing_identifies_him_whatever_the_phone_says(self):
        for prev in (None, "off", "someone", "mikkel"):
            for status in ("none", "stale", "weak", "near", "rise"):
                with self.subTest(prev=prev, status=status):
                    self.assertEqual(bd.next_state(prev, True, status, weighed=True), "mikkel")

    def test_a_weighing_makes_no_difference_to_an_empty_room_or_no_opinion_on_it(self):
        self.assertEqual(bd.next_state("mikkel", False, "none", weighed=True), "off")
        self.assertEqual(bd.next_state("someone", None, "none", weighed=True), "someone")
        self.assertEqual(bd.next_state(None, None, "none", weighed=True), "off")

    def test_it_is_not_weighed_unless_told(self):
        self.assertEqual(bd.next_state("someone", True, "weak"), "someone")
        self.assertEqual(bd.next_state("someone", True, "weak", weighed=False), "someone")


class NightPayloadTests(unittest.TestCase):
    def test_night_is_on_or_empty_in_every_state(self):
        for state in ("off", "someone", "mikkel"):
            with self.subTest(state=state):
                self.assertEqual(bd.payload(state, "r", "Mikkel", [], night=True)["night"], "on")
                self.assertEqual(bd.payload(state, "r", "Mikkel", [], night=False)["night"], "")
                self.assertEqual(bd.payload(state, "r", "Mikkel", [])["night"], "")

    def test_lines_are_kept_whatever_the_state(self):
        line = bd.Line("Alarm 09:15", "mdi:alarm", "09:15")
        self.assertEqual(bd.payload("someone", "r", "Mikkel", [line], night=True),
                         attrs_of(reason="r", night="on", line1="Alarm 09:15", icon1="mdi:alarm", due1="09:15"))


def room(state):
    return full(state, {"stale": "false"})


def maybe_async(value):
    """`value`, or an awaitable of it inside a running event loop: AppDaemon's API answers the way it is called,
    from a worker thread or from the loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return value

    async def wrap():
        return value

    return wrap()


MADE = []


def tearDownModule():
    for app in MADE:
        for task in app.tasks:
            task.close()


def make_app(states=None, active="on", now=NOW, fail=0, **args):
    """The app after initialize(), the first `fail` publishes of which fail. Its fetches are the coroutines in
    `tasks`, run by drain(); the answers to them are in `responses`, by service, and the calls that asked for
    one are in `requests`."""
    app = bd.BathroomDisplay.__new__(bd.BathroomDisplay)
    MADE.append(app)
    app.args = {**CONFIG, **args}
    app.now = now
    app.states = {ACTIVE: room(active), **(states or {})}
    app.published, app.logs, app.listened, app.events, app.scheduled = [], [], [], [], []
    app.calls, app.later, app.fail_services = [], [], set()
    app.tasks, app.requests, app.responses, app.hang = [], [], {}, set()
    app.fail_publish = fail

    def get_state(entity, attribute=None, **kw):
        value = app.states.get(entity)
        if attribute == "all" or value is None:
            return value
        if attribute == "computed_at" and entity == ACTIVE:
            return datetime.now(timezone.utc).isoformat()
        if attribute is not None:
            return value["attributes"].get(attribute)
        return value["state"]

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        if app.fail_publish:
            app.fail_publish -= 1
            raise RuntimeError("HA unreachable")
        app.published.append({"entity": entity, "state": state, "attributes": attributes, "replace": replace})

    def call_service(service, **kw):
        if kw.get("return_response"):
            app.requests.append((service, kw))
        if service in app.fail_services:
            raise RuntimeError("HA unreachable")
        if kw.get("return_response"):
            return asyncio.sleep(3600) if service in app.hang else maybe_async(app.responses.get(service))
        app.calls.append((service, kw))

    app.get_state = get_state
    app.set_state = set_state
    app.call_service = call_service
    app.run_in = lambda callback, delay, **kw: (app.later.append((callback, delay, kw)), maybe_async("handle"))[1]
    app.create_task = lambda coro: app.tasks.append(coro)
    app.get_now = lambda: app.now
    app.get_app = lambda name: None
    app.listen_state = lambda callback, entity, **kw: app.listened.append((callback.__name__, entity, kw))
    app.listen_event = lambda callback, event_name=None, **kw: app.events.append((callback.__name__, event_name))
    app.run_every = lambda callback, start, interval, **kw: app.scheduled.append((callback.__name__, start, interval))
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.initialize()
    return app


def fire(app, entity, new):
    """A state callback: the store takes `new` and the app is told."""
    old = app.states.get(entity)
    app.states[entity] = new
    app._on_source(entity, "all", old, new, {})


def occupy(app, state):
    old = app.states[ACTIVE]["state"]
    app.states[ACTIVE] = room(state)
    app._on_active(ACTIVE, "state", old, state, {})


def phone(app, dbm, age=0):
    """A reading of the phone that is `age` seconds old by the app's clock."""
    fire(app, RSSI, full(dbm, updated=app.now - age * SEC))


def heard(app, dbm, *ages):
    """Readings of the phone, each `age` seconds old by the app's clock and its own update."""
    for age in ages:
        phone(app, dbm, age=age)


def tick(app, delta=10 * SEC):
    app.now += delta
    app._on_tick({})


def tap(app):
    app._on_play(CONFIG["play_event"], {}, {})


def run_later(app):
    """The callbacks run_in was given, run now."""
    pending, app.later = app.later, []
    for callback, _, kw in pending:
        callback(kw)


def drain(app):
    """The fetches create_task was given, run, and then the callbacks they handed their results to."""
    while app.tasks or any(delay == 0 for _, delay, _ in app.later):
        while app.tasks:
            asyncio.run(app.tasks.pop(0))
        ready = [item for item in app.later if item[1] == 0]
        app.later = [item for item in app.later if item[1] != 0]
        for callback, _, kw in ready:
            callback(kw)


def last(app):
    return app.published[-1]


class TransitionTests(unittest.TestCase):
    def test_off_until_the_room_is_active_then_someone(self):
        app = make_app(active="off")
        self.assertEqual(last(app)["state"], "off")
        occupy(app, "on")
        self.assertEqual(last(app)["state"], "someone")
        occupy(app, "off")
        self.assertEqual(last(app)["state"], "off")

    def test_a_near_phone_in_an_active_room_is_mikkel_and_leaving_the_room_ends_it(self):
        app = make_app()
        self.assertEqual(last(app)["state"], "someone")
        phone(app, "-67.6")
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"], attrs_of(reason=f"active:{ACTIVE} phone:near", person="mikkel",
                                                          greeting="Hi Mikkel"))
        occupy(app, "off")
        self.assertEqual(last(app)["state"], "off")
        self.assertEqual(last(app)["attributes"], attrs_of(reason=f"inactive:{ACTIVE}"))

    def test_a_room_that_becomes_active_with_the_phone_already_near_is_mikkel_at_once(self):
        app = make_app({RSSI: full("-60")}, active="off")
        occupy(app, "on")
        self.assertEqual(last(app)["state"], "mikkel")

    def test_the_phone_alone_does_not_wake_the_display(self):
        app = make_app({RSSI: full("-50")}, active="off")
        self.assertEqual(last(app)["state"], "off")
        phone(app, "-49")
        self.assertEqual(len(app.published), 1)

    def test_a_weak_phone_is_someone_and_the_threshold_is_inclusive(self):
        app = make_app({RSSI: full("-76", updated=NOW - 60 * SEC)}, active="off")
        occupy(app, "on")
        phone(app, "-76")
        self.assertEqual(last(app)["state"], "someone")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")
        phone(app, "-75.4")
        self.assertEqual(last(app)["state"], "someone")
        phone(app, "-75")
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:near")

    def test_a_phone_that_rises_on_what_it_was_before_the_visit_is_mikkel(self):
        app = make_app(active="off")
        heard(app, "-88", 100, 60, 30)
        occupy(app, "on")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:stale"))
        phone(app, "-79")
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"],
                         attrs_of(reason=f"active:{ACTIVE} phone:rise", person="mikkel", greeting="Hi Mikkel"))
        phone(app, "-95")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:held"))

    def test_a_phone_that_was_already_as_loud_next_door_is_not_a_rise(self):
        app = make_app(active="off")
        heard(app, "-77", 100, 60, 30)
        occupy(app, "on")
        for reading in ("-78", "-76", "-79", "-80", "-76.5"):
            phone(app, reading)
            self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                             ("someone", f"active:{ACTIVE} phone:weak"))
        phone(app, "-75")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:near"))

    def test_near_identifies_him_whatever_the_baseline_was(self):
        for before in ("-77", "-75", "-90"):
            with self.subTest(before=before):
                app = make_app(active="off")
                heard(app, before, 100, 60, 30)
                occupy(app, "on")
                phone(app, "-75")
                self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                                 ("mikkel", f"active:{ACTIVE} phone:near"))

    def test_the_baseline_leaves_out_the_last_15_seconds_and_what_is_older_than_120(self):
        app = make_app(active="off")
        heard(app, "-60", 130, 125)
        heard(app, "-88", 100, 60)
        heard(app, "-79", 10, 5)
        occupy(app, "on")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:rise"))

    def test_a_phone_not_heard_before_the_visit_is_minus_100_so_minus_80_is_a_rise(self):
        app = make_app(active="off")
        occupy(app, "on")
        phone(app, "-81")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))
        phone(app, "-80")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:rise"))

    def test_unknown_readings_before_the_visit_do_not_count_as_a_level(self):
        app = make_app(active="off")
        heard(app, "-77", 100, 90)
        heard(app, "unknown", 80, 70, 60)
        heard(app, "unavailable", 50)
        occupy(app, "on")
        phone(app, "-76")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")

    def test_an_app_that_starts_in_an_active_room_takes_its_baseline_from_the_snapshot(self):
        app = make_app({RSSI: full("-79")})
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:rise"))
        app = make_app({RSSI: full("-79", updated=NOW - 60 * SEC)})
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:stale")
        phone(app, "-79")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")

    def test_the_baseline_is_worked_out_once_for_the_visit(self):
        app = make_app(active="off")
        heard(app, "-88", 100, 60, 30)
        occupy(app, "on")
        for _ in range(13):
            tick(app)
            phone(app, "-81")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))
        phone(app, "-79")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:rise"))

    def test_the_next_visit_has_its_own_baseline(self):
        app = make_app(active="off")
        heard(app, "-88", 100, 60, 30)
        occupy(app, "on")
        phone(app, "-79")
        self.assertEqual(last(app)["state"], "mikkel")
        occupy(app, "off")
        tick(app, 5 * MIN)
        heard(app, "-78", 100, 60, 30)
        occupy(app, "on")
        phone(app, "-77")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))

    def test_a_visit_that_begins_when_the_light_comes_on_takes_its_baseline_then(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("off")})
        heard(app, "-88", 100, 60, 30)
        fire(app, LIGHT, full("on"))
        phone(app, "-79")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("mikkel", f"active:{ACTIVE} phone:rise"))
        app = make_app({SLEEP: full("on"), LIGHT: full("off")})
        heard(app, "-79", 100, 60, 30)
        fire(app, LIGHT, full("on"))
        phone(app, "-79")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))

    def test_a_reading_the_tick_fetched_is_in_the_history_too(self):
        app = make_app(active="off")
        app.states[RSSI] = full("-77", updated=app.now - 60 * SEC)
        tick(app, 1 * SEC)
        occupy(app, "on")
        phone(app, "-78")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")

    def test_the_rise_limits_are_from_the_yaml(self):
        app = make_app(active="off", rssi_rise_min=-85, rssi_rise_db=3)
        heard(app, "-90", 100, 60, 30)
        occupy(app, "on")
        phone(app, "-86")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")
        phone(app, "-85")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:rise")
        app = make_app(active="off", rssi_rise_min=-85, rssi_rise_db=8)
        heard(app, "-90", 100, 60, 30)
        occupy(app, "on")
        phone(app, "-85")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")

    def test_the_threshold_and_the_freshness_are_from_the_yaml(self):
        app = make_app(mikkel_rssi_min=-90, rssi_fresh_s=5)
        phone(app, "-91")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))
        phone(app, "-85", age=6)
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:stale"))
        phone(app, "-85", age=5)
        self.assertEqual(last(app)["state"], "mikkel")

    def test_a_reading_that_is_not_a_number_is_no_phone(self):
        for state in ("unknown", "unavailable", "nan", "NaN"):
            with self.subTest(state=state):
                app = make_app({RSSI: full(state)})
                self.assertEqual(last(app)["state"], "someone")
                self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:none")
                phone(app, "-61")
                self.assertEqual(last(app)["state"], "mikkel")

    def test_a_removed_sensor_is_no_phone(self):
        app = make_app({RSSI: full("-90")})
        app.states.pop(RSSI)
        app._on_source(RSSI, "all", full("-90"), None, {})
        self.assertEqual(last(app)["state"], "someone")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:none")

    def test_a_late_callback_with_an_old_reading_is_stale_on_arrival(self):
        app = make_app()
        phone(app, "-60", age=21)
        self.assertEqual(last(app)["state"], "someone")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:stale")
        phone(app, "-60", age=20)
        self.assertEqual(last(app)["state"], "mikkel")

    def test_a_snapshot_reading_is_as_old_as_the_entity_says(self):
        app = make_app({RSSI: full("-60", updated=NOW - 30 * SEC)})
        self.assertEqual(last(app)["state"], "someone")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:stale")

    def test_a_value_from_a_callback_wins_over_a_lagging_store(self):
        app = make_app({RSSI: full("-90")})
        self.assertEqual(last(app)["state"], "someone")
        app._on_source(RSSI, "all", None, full("-60", updated=app.now), {})
        self.assertEqual(app.states[RSSI]["state"], "-90")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:near")
        tick(app)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:near")

    def test_a_source_the_snapshot_missed_is_read_on_the_tick(self):
        app = make_app()
        app.states[RSSI] = full("-60", updated=app.now)
        tick(app, 1 * SEC)
        self.assertEqual(last(app)["state"], "mikkel")


class StickyTests(unittest.TestCase):
    """Once the phone has been near, Mikkel stays identified until the room goes inactive."""

    NEAR = f"active:{ACTIVE} phone:near"
    HELD = f"active:{ACTIVE} phone:held"

    def test_no_reading_of_the_phone_ends_it(self):
        for reading in ("-90", "unknown", "unavailable", "nan"):
            with self.subTest(reading=reading):
                app = make_app({RSSI: full("-60")})
                self.assertEqual(last(app)["attributes"]["reason"], self.NEAR)
                phone(app, reading)
                self.assertEqual(last(app)["state"], "mikkel")
                self.assertEqual(last(app)["attributes"],
                                 attrs_of(reason=self.HELD, person="mikkel", greeting="Hi Mikkel"))
                phone(app, "-61")
                self.assertEqual(last(app)["attributes"]["reason"], self.NEAR)

    def test_a_rise_is_held_like_a_near_phone(self):
        app = make_app(active="off")
        heard(app, "-88", 100, 60, 30)
        occupy(app, "on")
        phone(app, "-79")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:rise")
        for reading in ("-81", "-95", "unknown"):
            phone(app, reading)
            self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("mikkel", self.HELD))
        tick(app, 30 * MIN)
        self.assertEqual(last(app)["state"], "mikkel")
        occupy(app, "off")
        self.assertEqual(last(app)["state"], "off")

    def test_a_removed_sensor_does_not_end_it(self):
        app = make_app({RSSI: full("-60")})
        app.states.pop(RSSI)
        app._on_source(RSSI, "all", full("-60"), None, {})
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("mikkel", self.HELD))

    def test_a_reading_that_stops_coming_does_not_end_it(self):
        app = make_app({RSSI: full("-60")})
        tick(app)
        tick(app)
        self.assertEqual(len(app.published), 1)
        tick(app)
        self.assertEqual(len(app.published), 2)
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("mikkel", self.HELD))
        tick(app, 30 * MIN)
        self.assertEqual(len(app.published), 2)
        phone(app, "-62")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("mikkel", self.NEAR))

    def test_the_room_going_inactive_ends_it_and_the_next_visit_starts_from_the_phone(self):
        app = make_app({RSSI: full("-60")})
        phone(app, "-90")
        self.assertEqual(last(app)["state"], "mikkel")
        occupy(app, "off")
        self.assertEqual(last(app)["state"], "off")
        occupy(app, "on")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:weak"))
        phone(app, "-60")
        self.assertEqual(last(app)["state"], "mikkel")

    def test_a_visit_that_never_saw_the_phone_stays_someone(self):
        app = make_app({RSSI: full("-90")})
        for reading in ("unknown", "-81", "unavailable"):
            phone(app, reading)
            self.assertEqual(last(app)["state"], "someone")
        tick(app, 30 * MIN)
        self.assertEqual(last(app)["state"], "someone")

    def test_the_lines_keep_following_the_rows_while_the_phone_is_held(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(LineTests.ROWS)})
        phone(app, "unknown")
        self.assertEqual((last(app)["state"], last(app)["attributes"]["line1"]), ("mikkel", "Spray the Alocasia"))
        fire(app, UPCOMING, upcoming([]))
        self.assertEqual((last(app)["state"], last(app)["attributes"]["line1"]), ("mikkel", ""))
        fire(app, UPCOMING, upcoming(LineTests.ROWS))
        self.assertEqual(last(app)["attributes"]["line1"], "Spray the Alocasia")


class NoOpinionTests(unittest.TestCase):
    """room_active_read has no opinion (None): callers make no change."""

    def test_before_there_is_a_state_it_is_off_with_the_unknown_reason(self):
        app = make_app({RSSI: full("-60")}, active="unknown")
        self.assertEqual(last(app)["state"], "off")
        self.assertEqual(last(app)["attributes"], attrs_of(reason=f"unknown:{ACTIVE}"))
        tick(app)
        phone(app, "-59")
        self.assertEqual(len(app.published), 1)
        occupy(app, "on")
        self.assertEqual(last(app)["state"], "mikkel")

    def test_mikkel_keeps_his_state_reason_and_lines(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(LineTests.ROWS)})
        before = last(app)
        self.assertEqual(before["state"], "mikkel")
        occupy(app, "unknown")
        fire(app, UPCOMING, upcoming([]))
        phone(app, "-90")
        tick(app, 5 * MIN)
        self.assertEqual(app.published, [before])
        occupy(app, "on")
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:held")
        self.assertEqual(last(app)["attributes"]["line1"], "")

    def test_someone_stays_someone_when_the_phone_comes_near(self):
        app = make_app({RSSI: full("-90")})
        before = last(app)
        occupy(app, "unknown")
        phone(app, "-60")
        self.assertEqual(app.published, [before])
        self.assertEqual(before["state"], "someone")
        occupy(app, "on")
        self.assertEqual(last(app)["state"], "mikkel")

    def test_off_stays_off(self):
        app = make_app({RSSI: full("-60")}, active="off")
        before = last(app)
        occupy(app, "unknown")
        phone(app, "-59")
        self.assertEqual(app.published, [before])
        self.assertEqual(before["attributes"], attrs_of(reason=f"inactive:{ACTIVE}"))

    def test_an_inactive_room_after_no_opinion_ends_it(self):
        app = make_app({RSSI: full("-60")})
        occupy(app, "unknown")
        occupy(app, "off")
        self.assertEqual(last(app)["state"], "off")
        self.assertEqual(last(app)["attributes"]["reason"], f"inactive:{ACTIVE}")

    def test_a_first_publish_that_failed_is_retried_while_there_is_still_no_opinion(self):
        app = make_app(active="unknown", fail=1)
        self.assertEqual(app.published, [])
        tick(app)
        self.assertEqual(len(app.published), 1)
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("off", f"unknown:{ACTIVE}"))

    def test_plugin_started_republishes_what_was_kept(self):
        app = make_app({RSSI: full("-60")})
        before = last(app)
        occupy(app, "unknown")
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(app.published, [before, before])


class PublishTests(unittest.TestCase):
    def test_initialize_publishes_the_contract_for_an_empty_bathroom(self):
        app = make_app(active="off")
        self.assertEqual(app.published, [{"entity": "sensor.bathroom_display", "state": "off",
                                          "attributes": attrs_of(reason=f"inactive:{ACTIVE}"), "replace": True}])

    def test_it_listens_to_the_room_the_phone_the_rows_mikkels_routes_the_scale_the_rain_and_the_alarm(self):
        app = make_app()
        self.assertEqual(sorted((entity, kw.get("attribute")) for _, entity, kw in app.listened),
                         sorted([(ACTIVE, None), (RSSI, "all"), (UPCOMING, "all"), (SLEEP, "all"), (LIGHT, "all"),
                                 (METRO, "all"), (STOG, "all"), (MEASURED, "all"), (WEIGHT, "all"), (FAT, "all"),
                                 (PIEZO, "all"), (ALARM, "all"), (ALARM_TIME, "all"), (ALARM_FROM_ENTITY, "all")]))
        self.assertEqual(app.events, [("_on_plugin_started", "plugin_started"),
                                      ("_on_play", "esphome.bathroom_display_play"),
                                      ("_on_alarm", "esphome.bathroom_display_alarm")])
        self.assertEqual(app.scheduled, [("_on_tick", "now+10", 10)])

    def test_it_publishes_only_when_the_payload_changes(self):
        app = make_app({RSSI: full("-60")})
        self.assertEqual(len(app.published), 1)
        tick(app)
        fire(app, UPCOMING, upcoming([]))
        fire(app, STOG, route("OK"))
        occupy(app, "on")
        for dbm in ("-60.4", "-61", "-74.9", "-75", "-59"):
            phone(app, dbm)
        self.assertEqual(len(app.published), 1)
        phone(app, "-81")
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:held")
        phone(app, "-82")
        self.assertEqual(len(app.published), 2)
        phone(app, "-70")
        self.assertEqual(len(app.published), 3)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:near")
        phone(app, "-77")
        self.assertEqual(len(app.published), 4)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:rise")

    def test_plugin_started_republishes_the_same_payload(self):
        app = make_app({RSSI: full("-60")})
        before = last(app)
        app._on_plugin_started("plugin_started", {}, {})
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app), before)

    def test_a_failed_publish_is_retried_by_the_next_evaluation(self):
        app = make_app(active="off")
        app.fail_publish = 1
        occupy(app, "on")
        self.assertEqual(len(app.published), 1)
        self.assertTrue(any(level == "WARNING" and "Publish failed" in msg for level, msg in app.logs))
        tick(app)
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app)["state"], "someone")

    def test_every_attribute_is_a_string_and_every_line_fits(self):
        rows = [one_off(date(2026, 11, 3), title="Spray the Alocasia for spider mites on both sides"),
                watering(7), maint_row("battery", "Bathroom door battery", at=NOW + 4 * DAY, area="bathroom")]
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(rows), STOG: route("Disrupted")})
        attributes = last(app)["attributes"]
        self.assertEqual(set(attributes), set(attrs_of()))
        self.assertNotIn("rssi", attributes)
        for key, value in attributes.items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
                self.assertTrue(value.isascii())
        for number in (1, 2, 3):
            self.assertLessEqual(len(attributes[f"line{number}"]), bd.LINE_MAX)
        self.assertEqual(attributes["line3"], "Water the Alocasia: 0.7 L")


class LineTests(unittest.TestCase):
    ROWS = [one_off(date(2026, 11, 3)), watering(7),
            maint_row("battery", "Bathroom door battery", at=NOW + 9 * DAY, area="bathroom",
                      icon="mdi:battery-clock-outline")]

    def test_mikkel_gets_the_rows_that_are_due_and_the_bathroom_rows_after_them(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS)})
        self.assertEqual(last(app)["attributes"], attrs_of(
            reason=f"active:{ACTIVE} phone:near", person="mikkel", greeting="Hi Mikkel",
            line1="Spray the Alocasia", icon1="mdi:spray", due1="overdue",
            line2="Water the Alocasia: 0.7 L", icon2="mdi:watering-can-outline", due2="today",
            line3="Bathroom door battery", icon3="mdi:battery-clock-outline", due3="9 d"))

    def test_a_dry_plant_and_a_washer_drum_clean_two_days_out_show_and_the_dryer_log_does_not(self):
        rows = [maint_row("washer:drum", "Washer drum + drawer clean", at=NOW + 2 * DAY, area="guest_bathroom",
                          icon="mdi:washing-machine"),
                dry_plant(),
                maint_row("dryer:plinth", "Dryer plinth filter", area="guest_bathroom", tap_done=True,
                          skip_next=True)]
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(rows)})
        self.assertEqual(last(app)["attributes"], attrs_of(
            reason=f"active:{ACTIVE} phone:near", person="mikkel", greeting="Hi Mikkel",
            line1="Water the Philodendron: 0.5 L", icon1="mdi:watering-can-outline", due1="today",
            line2="Washer drum + drawer clean", icon2="mdi:washing-machine", due2="2 d"))

    def test_a_route_that_is_bad_comes_first_and_takes_a_slot(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS),
                        STOG: route("Disrupted", [departure(12, "08:42", cancelled=True)])})
        attributes = last(app)["attributes"]
        self.assertEqual((attributes["line1"], attributes["icon1"], attributes["due1"]),
                         ("S-tog: cancelled 08:42", "mdi:train", ""))
        self.assertEqual((attributes["line2"], attributes["due2"]), ("Spray the Alocasia", "overdue"))
        self.assertEqual((attributes["line3"], attributes["due3"]), ("Water the Alocasia: 0.7 L", "today"))

    def test_two_bad_routes_come_first_in_the_order_of_the_yaml(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS),
                        STOG: route("Delayed", [departure(9, "08:39", delay=11)]),
                        METRO: route("Disrupted", [departure(5, "08:35", cancelled=True)])})
        attributes = last(app)["attributes"]
        self.assertEqual([attributes[f"line{number}"] for number in (1, 2, 3)],
                         ["Metro: cancelled 08:35", "S-tog: 08:39 +11 min", "Spray the Alocasia"])

    def test_a_route_that_recovers_gives_its_line_back_to_the_rows(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS),
                        STOG: route("Disrupted", [departure(12, "08:42", cancelled=True)])})
        fire(app, STOG, route("OK"))
        self.assertEqual(last(app)["attributes"]["line1"], "Spray the Alocasia")

    def test_the_lines_are_empty_unless_it_is_mikkel(self):
        states = {RSSI: full("-90"), UPCOMING: upcoming(self.ROWS), STOG: route("Disrupted")}
        keys = [f"{kind}{number}" for kind in ("line", "icon", "due") for number in (1, 2, 3)]
        for active in ("on", "off"):
            with self.subTest(active=active):
                app = make_app(states, active=active)
                self.assertNotEqual(last(app)["state"], "mikkel")
                self.assertEqual({key: last(app)["attributes"][key] for key in keys}, {key: "" for key in keys})

    def test_the_rows_end_with_their_lease(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS, expires=NOW + 10 * MIN)})
        self.assertEqual(last(app)["attributes"]["line1"], "Spray the Alocasia")
        app.now += 11 * MIN
        phone(app, "-60")
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"]["line1"], "")

    def test_rows_that_arrive_later_are_shown(self):
        app = make_app({RSSI: full("-60")})
        self.assertEqual(last(app)["attributes"]["line1"], "")
        fire(app, UPCOMING, upcoming(self.ROWS))
        self.assertEqual(last(app)["attributes"]["line1"], "Spray the Alocasia")


class SleepTests(unittest.TestCase):
    """While Mikkel's sleep mode is on the display stays off, unless the light was switched on by hand."""

    DARK = f"sleep:{SLEEP}"

    def test_kept_dark(self):
        for sleep, light, expected in (("on", "off", True), ("on", None, True), ("on", "unavailable", True),
                                       ("on", "on", False), ("off", "off", False), ("unavailable", "off", False),
                                       (None, None, False)):
            with self.subTest(sleep=sleep, light=light):
                self.assertEqual(bd.kept_dark(sleep, light), expected)

    def test_an_active_room_stays_off_in_sleep_mode_even_with_the_phone_near(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("off"), RSSI: full("-60")})
        self.assertEqual(last(app)["state"], "off")
        self.assertEqual(last(app)["attributes"], attrs_of(reason=self.DARK, night="on"))

    def test_the_wall_switch_wakes_it_and_the_visit_starts_again(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("off")})
        fire(app, LIGHT, full("on"))
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:none"))
        phone(app, "-60")
        self.assertEqual(last(app)["state"], "mikkel")
        fire(app, LIGHT, full("off"))
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("off", self.DARK))

    def test_sleep_mode_turning_on_mid_visit_switches_it_off_and_turning_off_brings_it_back(self):
        app = make_app({SLEEP: full("off"), LIGHT: full("off"), RSSI: full("-60")})
        self.assertEqual(last(app)["state"], "mikkel")
        fire(app, SLEEP, full("on"))
        self.assertEqual(last(app)["state"], "off")
        fire(app, SLEEP, full("off"))
        self.assertEqual(last(app)["state"], "mikkel")

    def test_an_empty_room_in_sleep_mode_is_inactive_not_dark(self):
        app = make_app({SLEEP: full("on")}, active="off")
        self.assertEqual(last(app)["attributes"]["reason"], f"inactive:{ACTIVE}")


class PlayButtonTests(unittest.TestCase):
    GROUP = [SPEAKER, "media_player.kitchen"]
    PLAY_RADIO = ("media_player/play_media",
                  {"entity_id": SPEAKER, "media_content_id": "dr_podcasts://radio/p3", "media_content_type": "music"})

    def app(self, state, members=None, **args):
        attributes = {} if members is None else {"group_members": members}
        return make_app({SPEAKER: full(state, attributes)} if state is not None else {}, **args)

    def lines(self, app):
        return [msg for _, msg in app.logs if msg.startswith("Play button")]

    def test_a_playing_speaker_is_paused(self):
        app = self.app("playing", [SPEAKER])
        tap(app)
        self.assertEqual(app.calls, [("media_player/media_pause", {"entity_id": SPEAKER})])
        self.assertEqual(app.later, [])
        self.assertEqual(self.lines(app), [f"Play button: {SPEAKER} is playing -> pause"])

    def test_a_paused_speaker_plays_on(self):
        app = self.app("paused", [SPEAKER])
        tap(app)
        self.assertEqual(app.calls, [("media_player/media_play", {"entity_id": SPEAKER})])
        self.assertEqual(app.later, [])
        self.assertEqual(self.lines(app), [f"Play button: {SPEAKER} is paused -> play"])

    def test_a_grouped_speaker_that_plays_or_is_paused_stays_in_its_group(self):
        for state, service in (("playing", "media_player/media_pause"), ("paused", "media_player/media_play")):
            with self.subTest(state=state):
                app = self.app(state, self.GROUP)
                tap(app)
                self.assertEqual(app.calls, [(service, {"entity_id": SPEAKER})])
                self.assertEqual(app.later, [])

    def test_any_other_speaker_starts_the_radio_as_music_at_once(self):
        for state in ("idle", "off", "standby", "unavailable", "unknown", None):
            with self.subTest(state=state):
                app = self.app(state, [SPEAKER])
                tap(app)
                self.assertEqual(app.calls, [self.PLAY_RADIO])
                self.assertEqual(app.later, [])
                self.assertEqual(len(self.lines(app)), 1)

    def test_a_speaker_without_group_members_starts_the_radio_at_once(self):
        app = self.app("idle")
        tap(app)
        self.assertEqual(app.calls, [self.PLAY_RADIO])
        self.assertEqual(app.later, [])

    def test_a_grouped_speaker_leaves_its_group_and_starts_the_radio_a_second_later(self):
        app = self.app("idle", self.GROUP)
        tap(app)
        self.assertEqual(app.calls, [("media_player/unjoin", {"entity_id": SPEAKER})])
        self.assertEqual([(callback.__name__, delay) for callback, delay, _ in app.later], [("_start_radio", 1)])
        self.assertEqual(len(self.lines(app)), 1)
        run_later(app)
        self.assertEqual(app.calls, [("media_player/unjoin", {"entity_id": SPEAKER}), self.PLAY_RADIO])
        self.assertEqual(app.later, [])
        self.assertEqual(len(self.lines(app)), 1)

    def test_the_state_is_read_at_the_tap(self):
        app = self.app("idle", [SPEAKER])
        tap(app)
        app.states[SPEAKER] = full("playing", {"group_members": [SPEAKER]})
        tap(app)
        app.states[SPEAKER] = full("paused", {"group_members": [SPEAKER]})
        tap(app)
        app.states[SPEAKER] = full("idle", {"group_members": self.GROUP})
        tap(app)
        self.assertEqual([call for call, _ in app.calls],
                         ["media_player/play_media", "media_player/media_pause", "media_player/media_play",
                          "media_player/unjoin"])
        self.assertEqual(len(self.lines(app)), 4)

    def warnings(self, app):
        return [msg for level, msg in app.logs if level == "WARNING"]

    def test_a_service_call_that_fails_logs_a_warning(self):
        for state, service in (("playing", "media_player/media_pause"), ("paused", "media_player/media_play"),
                               ("idle", "media_player/play_media")):
            with self.subTest(state=state):
                app = self.app(state, [SPEAKER])
                app.fail_services = {service}
                tap(app)
                self.assertEqual(app.calls, [])
                self.assertEqual(self.warnings(app), [f"{service} failed: RuntimeError('HA unreachable')"])

    def test_a_failed_unjoin_logs_a_warning_and_the_radio_still_follows(self):
        app = self.app("idle", self.GROUP)
        app.fail_services = {"media_player/unjoin"}
        tap(app)
        run_later(app)
        self.assertEqual(app.calls, [self.PLAY_RADIO])
        self.assertEqual(self.warnings(app), ["media_player/unjoin failed: RuntimeError('HA unreachable')"])

    def test_a_failed_radio_after_the_unjoin_logs_a_warning(self):
        app = self.app("idle", self.GROUP)
        app.fail_services = {"media_player/play_media"}
        tap(app)
        run_later(app)
        self.assertEqual([call for call, _ in app.calls], ["media_player/unjoin"])
        self.assertEqual(self.warnings(app), ["media_player/play_media failed: RuntimeError('HA unreachable')"])

    def test_a_speaker_that_cannot_be_read_gets_the_radio_and_a_warning(self):
        app = self.app("playing", [SPEAKER])

        def broken(entity, **kw):
            raise RuntimeError("no state")

        app.get_state = broken
        tap(app)
        self.assertEqual(app.calls, [self.PLAY_RADIO])
        self.assertEqual(len(self.warnings(app)), 1)
        self.assertIn(f"Read of {SPEAKER} failed", self.warnings(app)[0])

    def test_the_event_the_speaker_and_the_radio_are_from_the_yaml(self):
        app = make_app({"media_player.den": full("idle", {"group_members": ["media_player.den"]})},
                       play_event="esphome.den_play", speaker="media_player.den", radio="http://example.test/stream")
        self.assertIn(("_on_play", "esphome.den_play"), app.events)
        self.assertNotIn(("_on_play", "esphome.bathroom_display_play"), app.events)
        app._on_play("esphome.den_play", {}, {})
        self.assertEqual(app.calls, [("media_player/play_media", {
            "entity_id": "media_player.den", "media_content_id": "http://example.test/stream",
            "media_content_type": "music"})])


STATS = "recorder/get_statistics"
FORECAST = "weather/get_forecasts"
BLANK = ("", "", "")
OLD = NOW - 3 * DAY


def scale_states():
    """The scale's three sensors as the weighing before left them."""
    return {MEASURED: full(iso(OLD), updated=OLD), WEIGHT: full("73.0", updated=OLD), FAT: full("20.2", updated=OLD)}


def weigh(app, weight="73.3", fat="20.0", ago=2):
    """What HA reports for a weighing: the weight, the fat and then the measurement sensor, which holds the moment."""
    fire(app, WEIGHT, full(weight, updated=app.now))
    fire(app, FAT, full(fat, updated=app.now))
    fire(app, MEASURED, full(iso(app.now - ago * SEC), updated=app.now))


def thirty_days_ago(app, kg=74.1, fat=20.0):
    at = app.now - 30 * DAY - HOUR
    app.responses[STATS] = stats_answer([stat_row(at, kg)], [stat_row(at, fat)])


def shown(app):
    attributes = last(app)["attributes"]
    return [(attributes[f"line{n}"], attributes[f"icon{n}"], attributes[f"due{n}"]) for n in (1, 2, 3)]


def warnings(app):
    return [msg for level, msg in app.logs if level == "WARNING"]


class WeighingFlowTests(unittest.TestCase):
    """The app runs in the afternoon, which is neither of the windows of the rain and the alarm line."""

    AFTERNOON = house(11, 5, 14, 0)
    CARD = [("73.3 kg · 20.0% fat", "mdi:scale-bathroom", "73.3 kg"),
            ("Fat 30 d: ±0 kg", "mdi:water-percent", "±0 kg"),
            ("Lean 30 d: −0.6 kg", "mdi:arm-flex", "−0.6 kg")]
    WEIGHT_ONLY = [CARD[0][:0] or ("73.3 kg", "mdi:scale-bathroom", "73.3 kg"), BLANK, BLANK]
    ROWS = ("Spray the Alocasia", "mdi:spray", "overdue")

    def app(self, states=None, now=AFTERNOON, **kw):
        rows = upcoming(LineTests.ROWS, expires=now + 10 * MIN)
        return make_app({**scale_states(), UPCOMING: rows, **(states or {})}, now=now, **kw)

    def test_a_weighing_identifies_him_and_the_card_replaces_his_lines_once_the_statistics_are_in(self):
        app = self.app()
        self.assertEqual((last(app)["state"], shown(app)), ("someone", [BLANK] * 3))
        weigh(app)
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:held")
        self.assertEqual(shown(app)[0], self.ROWS)
        thirty_days_ago(app)
        drain(app)
        self.assertEqual((last(app)["state"], shown(app)), ("mikkel", self.CARD))
        self.assertEqual((last(app)["attributes"]["person"], last(app)["attributes"]["greeting"]),
                         ("mikkel", "Hi Mikkel"))

    def test_the_statistics_are_asked_for_once_with_both_sensors_around_30_days_ago(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(app.requests, [(STATS, {
            "statistic_ids": [WEIGHT, FAT], "start_time": "2026-10-03T13:00:00+00:00",
            "end_time": "2026-10-09T13:00:00+00:00", "period": "hour", "types": ["mean"], "return_response": True})])
        for _ in range(20):
            tick(app)
        self.assertEqual((len(app.requests), app.tasks), (1, []))

    def test_the_card_has_the_whole_set_of_lines_the_figures_and_the_change_since_then(self):
        app = self.app()
        weigh(app, weight="73.6", fat="21.0")
        thirty_days_ago(app, kg=73.0, fat=22.0)
        drain(app)
        self.assertEqual(shown(app), [("73.6 kg · 21.0% fat", "mdi:scale-bathroom", "73.6 kg"),
                                      ("Fat 30 d: −0.6 kg", "mdi:water-percent", "−0.6 kg"),
                                      ("Lean 30 d: +1.2 kg", "mdi:arm-flex", "+1.2 kg")])

    def test_the_card_takes_the_place_of_the_alarm_the_transit_and_the_rows(self):
        now = house(11, 5, 21, 0)
        app = self.app({ALARM: full("on"), ALARM_TIME: full("09:15:00"),
                        STOG: route("Disrupted", [departure(12, "21:12", cancelled=True, now=now)], as_of=now - 2 * MIN)},
                       now=now)
        weigh(app)
        self.assertEqual([line[0] for line in shown(app)], ["Alarm 09:15", "S-tog: cancelled 21:12", "Spray the Alocasia"])
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(shown(app), self.CARD)

    def test_a_weekend_morning_weighing_also_asks_for_the_forecast_and_the_card_takes_the_place_of_the_rain(self):
        now = SATURDAY
        stog = route("Disrupted", [departure(12, "08:42", cancelled=True, now=now)], as_of=now - 2 * MIN)
        app = self.app({PIEZO: full("on"), STOG: stog}, now=now)
        weigh(app)
        self.assertEqual([task.cr_code.co_name for task in app.tasks], ["_fetch_comparison"])
        self.assertEqual([line[0] for line in shown(app)], ["Raining now", "S-tog: cancelled 08:42", "Spray the Alocasia"])
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(shown(app), self.CARD)
        fire(app, PIEZO, full("off"))
        self.assertEqual(app.tasks, [])
        app.responses[FORECAST] = weather_answer((utc(8, day=7), 0.6))
        app.now += 90 * SEC
        run_later(app)
        self.assertEqual([task.cr_code.co_name for task in app.tasks], ["_fetch_forecast"])
        drain(app)
        self.assertEqual(shown(app)[0], ("Rain 09-10", "mdi:weather-rainy", "09-10"))

    def test_a_workday_morning_weighing_in_the_rain_asks_for_the_forecast_and_the_way_home_follows_the_card(self):
        app = self.app({PIEZO: full("on")}, now=NOW)
        weigh(app)
        self.assertEqual([task.cr_code.co_name for task in app.tasks], ["_fetch_comparison", "_fetch_forecast"])
        thirty_days_ago(app)
        app.responses[FORECAST] = weather_answer((house(11, 5, 17), 0.5), (house(11, 5, 18), 0.5))
        drain(app)
        self.assertEqual(shown(app), self.CARD)
        app.now += 90 * SEC
        run_later(app)
        self.assertEqual(shown(app)[0], ("Raining now · home 17-19", "mdi:weather-rainy", "now"))
        self.assertEqual(shown(app)[1], self.ROWS)

    def test_the_card_ends_90_seconds_after_the_weighing(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        seen = app.now
        self.assertEqual([(callback.__name__, delay) for callback, delay, _ in app.later], [("_on_card_end", 90)])
        tick(app, 89 * SEC)
        self.assertEqual(shown(app), self.CARD)
        app.now = seen + 90 * SEC
        run_later(app)
        self.assertEqual(shown(app)[0], self.ROWS)
        self.assertEqual(last(app)["state"], "mikkel")

    def test_the_tick_ends_it_too_when_the_scheduled_end_is_late(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        tick(app, 90 * SEC)
        self.assertEqual(shown(app)[0], self.ROWS)

    def test_without_a_row_30_days_ago_only_the_first_line_shows(self):
        app = self.app()
        weigh(app)
        app.responses[STATS] = stats_answer([], [])
        drain(app)
        self.assertEqual(shown(app), [self.CARD[0], BLANK, BLANK])

    def test_a_row_further_than_3_days_from_that_day_is_no_row(self):
        app = self.app()
        weigh(app)
        at = app.now - 30 * DAY - 3 * DAY - HOUR
        app.responses[STATS] = stats_answer([stat_row(at, 74.1)], [stat_row(at, 20.0)])
        drain(app)
        self.assertEqual(shown(app), [self.CARD[0], BLANK, BLANK])

    def test_a_fetch_that_fails_or_answers_nonsense_leaves_the_first_line_and_no_more(self):
        failing = self.app()
        failing.fail_services = {STATS}
        weigh(failing)
        drain(failing)
        self.assertEqual(shown(failing), [self.CARD[0], BLANK, BLANK])
        self.assertEqual(warnings(failing), ["Statistics fetch failed: RuntimeError('HA unreachable')"])
        for answer in (None, {}, {"oops": 1}, "x", stats_answer("rows", None)):
            with self.subTest(answer=answer):
                app = self.app()
                app.responses[STATS] = answer
                weigh(app)
                drain(app)
                self.assertEqual(shown(app), [self.CARD[0], BLANK, BLANK])

    def test_a_fetch_that_hangs_gives_up_after_its_timeout(self):
        app = self.app()
        app.hang = {STATS}
        weigh(app)
        with mock.patch.object(bd, "FETCH_TIMEOUT_S", 0.01):
            drain(app)
        self.assertEqual(shown(app), [self.CARD[0], BLANK, BLANK])
        self.assertEqual(len(warnings(app)), 1)
        self.assertIn("Statistics fetch failed", warnings(app)[0])

    def test_the_card_waits_for_the_weight_and_the_fat_the_weighing_took(self):
        app = self.app()
        fire(app, MEASURED, full(iso(app.now - 2 * SEC), updated=app.now))
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(shown(app)[0], self.ROWS)
        fire(app, WEIGHT, full("73.3", updated=app.now))
        self.assertEqual(shown(app), self.WEIGHT_ONLY)
        fire(app, FAT, full("20.0", updated=app.now))
        self.assertEqual(shown(app), self.CARD)

    def test_a_fat_percentage_the_weighing_did_not_update_is_left_out_not_repeated(self):
        app = self.app()
        fire(app, WEIGHT, full("73.3", updated=app.now))
        fire(app, MEASURED, full(iso(app.now - 2 * SEC), updated=app.now))
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(shown(app), self.WEIGHT_ONLY)

    def test_a_fat_reading_that_is_not_a_number_is_left_out_too(self):
        for fat in ("unknown", "unavailable", ""):
            with self.subTest(fat=fat):
                app = self.app()
                weigh(app, fat=fat)
                thirty_days_ago(app)
                drain(app)
                self.assertEqual(shown(app), self.WEIGHT_ONLY)

    def test_a_weight_the_weighing_did_not_update_makes_no_card(self):
        app = self.app()
        fire(app, FAT, full("20.0", updated=app.now))
        fire(app, MEASURED, full(iso(app.now - 2 * SEC), updated=app.now))
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(shown(app)[0], self.ROWS)
        self.assertEqual(last(app)["state"], "mikkel")

    def test_a_weighing_while_the_room_is_not_active_is_ignored(self):
        for active in ("off", "unknown"):
            with self.subTest(active=active):
                app = self.app(active=active)
                weigh(app)
                self.assertEqual((app.tasks, app.later, last(app)["state"]), ([], [], "off"))
                self.assertTrue(any("ignored" in msg for _, msg in app.logs))
                occupy(app, "on")
                drain(app)
                self.assertEqual((last(app)["state"], shown(app)), ("someone", [BLANK] * 3))

    def test_a_state_that_comes_back_or_is_old_is_not_a_weighing(self):
        app = self.app()
        fire(app, MEASURED, full(app.states[MEASURED]["state"], updated=app.now))
        fire(app, MEASURED, full(iso(NOW - 2 * DAY), updated=app.now))
        fire(app, MEASURED, full("unavailable", updated=app.now))
        fire(app, MEASURED, full(iso(NOW - 2 * DAY), updated=app.now))
        fire(app, MEASURED, full(iso(NOW - 121 * SEC), updated=app.now))
        self.assertEqual((app.tasks, app.later, last(app)["state"]), ([], [], "someone"))

    def test_the_same_moment_twice_is_one_weighing(self):
        app = self.app()
        weigh(app)
        fire(app, MEASURED, dict(app.states[MEASURED]))
        self.assertEqual(len(app.tasks), 1)
        self.assertEqual(len([item for item in app.later if item[0].__name__ == "_on_card_end"]), 1)

    def test_a_weighing_that_arrives_late_still_counts_up_to_120_seconds(self):
        app = self.app()
        weigh(app, ago=120)
        self.assertEqual((len(app.tasks), last(app)["state"]), (1, "mikkel"))
        app = self.app()
        weigh(app, ago=121)
        self.assertEqual((app.tasks, last(app)["state"]), ([], "someone"))

    def test_a_second_weighing_takes_over_and_the_first_ones_answer_is_not_used_for_it(self):
        app = self.app()
        weigh(app)
        app.now += 20 * SEC
        weigh(app, weight="73.5")
        self.assertEqual(len(app.tasks), 2)
        thirty_days_ago(app)
        asyncio.run(app.tasks.pop(0))
        run_later_zero = [item for item in app.later if item[1] == 0]
        app.later = [item for item in app.later if item[1] != 0]
        for callback, _, kw in run_later_zero:
            callback(kw)
        self.assertEqual(shown(app)[0], self.ROWS)
        drain(app)
        self.assertEqual(shown(app)[0][0], "73.5 kg · 20.0% fat")

    def test_the_weighing_alone_identifies_him_for_the_visit_and_not_for_the_next(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        for dbm in ("-95", "unknown"):
            phone(app, dbm)
            self.assertEqual(last(app)["state"], "mikkel")
        occupy(app, "off")
        self.assertEqual((last(app)["state"], shown(app)), ("off", [BLANK] * 3))
        occupy(app, "on")
        self.assertEqual((last(app)["state"], shown(app)), ("someone", [BLANK] * 3))

    def test_a_weighing_in_sleep_mode_identifies_nobody_in_the_dark(self):
        app = self.app({SLEEP: full("on"), LIGHT: full("off")})
        weigh(app)
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("off", f"sleep:{SLEEP}"))

    def test_in_sleep_mode_the_lines_stay_the_alarm_line_whatever_the_weighing(self):
        app = self.app({SLEEP: full("on"), LIGHT: full("on"), ALARM: full("on"), ALARM_TIME: full("09:15:00")})
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        self.assertEqual(last(app)["state"], "mikkel")
        self.assertEqual(shown(app), [("Alarm 09:15", "mdi:alarm", "09:15"), BLANK, BLANK])

    def test_the_card_never_goes_to_someone_who_is_not_him(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        occupy(app, "off")
        app.now += 30 * SEC
        occupy(app, "on")
        self.assertEqual((last(app)["state"], shown(app)), ("someone", [BLANK] * 3))

    def test_every_attribute_of_the_card_is_a_string(self):
        app = self.app()
        weigh(app)
        thirty_days_ago(app)
        drain(app)
        for key, value in last(app)["attributes"].items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
        for number in (1, 2, 3):
            self.assertLessEqual(len(last(app)["attributes"][f"line{number}"]), bd.LINE_MAX)


class RainFlowTests(unittest.TestCase):
    RAIN = ("Rain 09-11", "mdi:weather-rainy", "09-11")
    HOME = ((house(11, 5, 17), 0.5), (house(11, 5, 18), 0.5))

    def app(self, now=NOW, states=None, **kw):
        return make_app({RSSI: full("-60", updated=now), **(states or {})}, now=now, **kw)

    def test_inside_the_window_he_gets_the_forecast_once_and_a_line_from_it(self):
        app = self.app()
        self.assertEqual((len(app.tasks), shown(app)[0]), (1, BLANK))
        app.responses[FORECAST] = weather_answer((utc(8), 0.6), (utc(9), 0.4))
        drain(app)
        self.assertEqual(app.requests, [(FORECAST, {"entity_id": WEATHER, "type": "hourly", "return_response": True})])
        self.assertEqual(shown(app)[0], self.RAIN)

    def test_the_window_is_06_to_12_house_time(self):
        for now, fetches in ((house(11, 5, 5, 59), 0), (datetime(2026, 11, 5, 5, 59, 59, tzinfo=HOUSE), 0),
                             (house(11, 5, 6, 0), 1), (datetime(2026, 11, 5, 11, 59, 59, tzinfo=HOUSE), 1),
                             (house(11, 5, 12, 0), 0), (house(11, 5, 21, 0), 0)):
            with self.subTest(now=now):
                self.assertEqual(len(self.app(now=now).tasks), fetches)

    def test_the_line_is_not_there_outside_the_window_whatever_the_piezo_says(self):
        for now, line in ((house(11, 5, 5, 59), BLANK), (house(11, 5, 6, 0), ("Raining now", "mdi:weather-rainy", "now")),
                          (house(11, 5, 11, 59), ("Raining now", "mdi:weather-rainy", "now")), (house(11, 5, 12, 0), BLANK)):
            with self.subTest(now=now):
                self.assertEqual(shown(self.app(now=now, states={PIEZO: full("on")}))[0], line)

    def test_someone_an_empty_room_and_a_dark_room_get_no_forecast_and_no_line(self):
        for states, active in (({RSSI: full("-90")}, "on"), ({}, "off"), ({SLEEP: full("on"), LIGHT: full("off")}, "on")):
            with self.subTest(states=states, active=active):
                app = make_app({PIEZO: full("on"), **states}, active=active)
                self.assertEqual((app.tasks, shown(app)), ([], [BLANK] * 3))

    def test_it_raining_now_needs_no_forecast_on_a_weekend(self):
        app = self.app(now=SATURDAY, states={PIEZO: full("on")})
        self.assertEqual((app.tasks, shown(app)[0]), ([], ("Raining now", "mdi:weather-rainy", "now")))
        fire(app, PIEZO, full("off"))
        self.assertEqual((len(app.tasks), shown(app)[0]), (1, BLANK))
        fire(app, PIEZO, full("on"))
        self.assertEqual(shown(app)[0][0], "Raining now")

    def test_it_raining_now_asks_for_the_forecast_on_a_workday_for_the_way_home(self):
        app = self.app(states={PIEZO: full("on")})
        self.assertEqual((len(app.tasks), shown(app)[0]), (1, ("Raining now", "mdi:weather-rainy", "now")))
        app.responses[FORECAST] = weather_answer(*self.HOME)
        drain(app)
        self.assertEqual(shown(app)[0], ("Raining now · home 17-19", "mdi:weather-rainy", "now"))
        for _ in range(100):
            tick(app)
        self.assertEqual((len(app.requests), app.tasks), (1, []))

    def test_a_workday_morning_tells_of_the_way_home_after_the_way_out(self):
        app = self.app()
        app.responses[FORECAST] = weather_answer((utc(8), 0.6), (utc(9), 0.4), *self.HOME)
        drain(app)
        self.assertEqual(shown(app)[0], ("Rain 09-11 · home 17-19", "mdi:weather-rainy", "09-11"))

    def test_a_workday_morning_with_rain_only_on_the_way_home_says_so(self):
        app = self.app()
        app.responses[FORECAST] = weather_answer(*self.HOME)
        drain(app)
        self.assertEqual(shown(app)[0], ("Rain home 17-19", "mdi:weather-rainy", "17-19"))

    def test_a_full_hourly_forecast_reaches_the_way_home(self):
        app = self.app()
        first = house(11, 5, 8)
        app.responses[FORECAST] = weather_answer(*[(first + n * HOUR, 0.5 if n in (9, 10) else 0.0) for n in range(48)])
        drain(app)
        self.assertEqual(shown(app)[0], ("Rain home 17-19", "mdi:weather-rainy", "17-19"))

    def test_a_weekend_morning_has_the_way_out_only(self):
        app = self.app(now=SATURDAY)
        app.responses[FORECAST] = weather_answer((utc(8, day=7), 0.6), (utc(9, day=7), 0.4), (house(11, 7, 17), 0.5))
        drain(app)
        self.assertEqual(shown(app)[0], self.RAIN)
        app = self.app(now=SATURDAY)
        app.responses[FORECAST] = weather_answer((house(11, 7, 17), 0.5), (house(11, 7, 18), 0.5))
        drain(app)
        self.assertEqual(shown(app), [BLANK] * 3)

    def test_the_way_home_goes_with_a_stale_forecast_and_raining_now_stays(self):
        app = self.app(states={PIEZO: full("on")})
        app.responses[FORECAST] = weather_answer(*self.HOME)
        drain(app)
        app.fail_services = {FORECAST}
        tick(app, 31 * MIN)
        drain(app)
        self.assertEqual(shown(app)[0][0], "Raining now · home 17-19")
        tick(app, 59 * MIN)  # 90 minutes since the forecast
        self.assertEqual(shown(app)[0][0], "Raining now · home 17-19")
        tick(app, 1 * SEC)
        self.assertEqual(shown(app)[0], ("Raining now", "mdi:weather-rainy", "now"))

    def test_a_piezo_that_cannot_be_read_is_not_rain(self):
        for state in ("unknown", "unavailable", "off", None):
            with self.subTest(state=state):
                app = self.app(states={PIEZO: full(state)} if state else {})
                self.assertEqual(shown(app)[0], BLANK)

    def test_no_rain_in_the_forecast_no_line(self):
        app = self.app()
        app.responses[FORECAST] = weather_answer((utc(8), 0.0), (utc(9), 0.1), (utc(10), 0.0), (utc(12), 5.0))
        drain(app)
        self.assertEqual(shown(app), [BLANK] * 3)

    def test_the_forecast_is_asked_for_at_most_every_30_minutes_answered_or_not(self):
        app = self.app()
        app.fail_services = {FORECAST}
        drain(app)
        self.assertEqual(len(app.requests), 1)
        self.assertEqual(warnings(app), ["Forecast fetch failed: RuntimeError('HA unreachable')"])
        tick(app, 29 * MIN + 59 * SEC)
        self.assertEqual(app.tasks, [])
        tick(app, 1 * SEC)
        self.assertEqual(len(app.tasks), 1)
        drain(app)
        self.assertEqual(len(app.requests), 2)
        for _ in range(100):
            tick(app)
        self.assertEqual((len(app.requests), app.tasks), (2, []))

    def test_a_forecast_that_cannot_be_read_gives_no_line(self):
        for answer in (None, {}, {"oops": 1}, weather_answer()):
            with self.subTest(answer=answer):
                app = self.app()
                app.responses[FORECAST] = answer
                drain(app)
                self.assertEqual(shown(app), [BLANK] * 3)

    def test_the_forecast_goes_stale_when_the_fetches_stop_working(self):
        app = self.app()
        app.responses[FORECAST] = weather_answer((utc(8), 0.6), (utc(9), 0.4), (utc(10), 0.4))
        drain(app)
        app.fail_services = {FORECAST}
        tick(app, 31 * MIN)
        drain(app)
        self.assertEqual(shown(app)[0][0], "Rain 09-12")
        tick(app, 59 * MIN)  # 90 minutes since the forecast
        self.assertEqual(shown(app)[0][0], "Rain 10-12")
        tick(app, 1 * SEC)
        self.assertEqual(shown(app)[0], BLANK)

    def test_the_line_follows_the_clock(self):
        app = self.app()
        app.responses[FORECAST] = weather_answer((utc(8), 0.6), (utc(9), 0.4))
        drain(app)
        self.assertEqual(shown(app)[0][0], "Rain 09-11")
        tick(app, 60 * MIN)  # 09:30 house: the first hour is still going on
        self.assertEqual(shown(app)[0][0], "Rain 09-11")
        tick(app, 30 * MIN)  # 10:00: it is over
        self.assertEqual(shown(app)[0][0], "Rain 10-11")

    def test_the_rain_comes_before_transit_and_rows(self):
        app = self.app(states={UPCOMING: upcoming(LineTests.ROWS), PIEZO: full("on"),
                               STOG: route("Disrupted", [departure(12, "08:42", cancelled=True)])})
        self.assertEqual([line[0] for line in shown(app)],
                         ["Raining now", "S-tog: cancelled 08:42", "Spray the Alocasia"])

    def test_the_forecast_entity_is_from_the_yaml(self):
        app = self.app(weather_entity="weather.den")
        drain(app)
        self.assertEqual(app.requests[0][1]["entity_id"], "weather.den")
        app = self.app(rain_entity="binary_sensor.den_rain", states={"binary_sensor.den_rain": full("on")})
        self.assertEqual(shown(app)[0][0], "Raining now")


class AlarmFlowTests(unittest.TestCase):
    ON = ("Alarm 09:15", "mdi:alarm", "09:15")
    OFF = ("No alarm", "mdi:alarm", "off")

    def app(self, now=NOW, states=None, **kw):
        return make_app({RSSI: full("-60", updated=now), ALARM: full("on"), ALARM_TIME: full("09:15:00"),
                         **(states or {})}, now=now, **kw)

    def test_without_the_helper_the_line_is_there_from_21_to_02_house_time(self):
        cases = ((house(11, 5, 20, 59), False), (datetime(2026, 11, 5, 20, 59, 59, tzinfo=HOUSE), False),
                 (house(11, 5, 21, 0), True), (house(11, 5, 23, 59), True), (house(11, 6, 0, 0), True),
                 (datetime(2026, 11, 6, 1, 59, 59, tzinfo=HOUSE), True), (house(11, 6, 2, 0), False),
                 (house(11, 5, 12, 0), False), (house(11, 5, 6, 0), False), (house(11, 5, 20, 0), False))
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(shown(self.app(now=now))[0], self.ON if expected else BLANK)

    def test_the_helper_sets_when_the_line_starts(self):
        cases = ((house(11, 5, 21, 0), False), (house(11, 5, 21, 30), False), (house(11, 5, 21, 59), False),
                 (house(11, 5, 22, 0), True), (house(11, 5, 22, 30), True), (house(11, 6, 1, 59), True),
                 (house(11, 6, 2, 0), False), (house(11, 5, 12, 0), False))
        for now, expected in cases:
            with self.subTest(now=now):
                app = self.app(now=now, states={ALARM_FROM_ENTITY: full("22:00:00")})
                self.assertEqual(shown(app)[0], self.ON if expected else BLANK)

    def test_the_helper_may_start_the_line_earlier_than_21(self):
        for now, expected in ((house(11, 5, 19, 59), False), (house(11, 5, 20, 0), True), (house(11, 5, 20, 30), True)):
            with self.subTest(now=now):
                app = self.app(now=now, states={ALARM_FROM_ENTITY: full("20:00:00")})
                self.assertEqual(shown(app)[0], self.ON if expected else BLANK)

    def test_a_helper_that_cannot_be_read_as_a_time_starts_it_at_21(self):
        for state in ("unknown", "unavailable", "", "soon"):
            for now, expected in ((house(11, 5, 20, 59), False), (house(11, 5, 21, 0), True)):
                with self.subTest(state=state, now=now):
                    app = self.app(now=now, states={ALARM_FROM_ENTITY: full(state)})
                    self.assertEqual(shown(app)[0], self.ON if expected else BLANK)

    def test_the_line_follows_the_helper(self):
        app = self.app(now=house(11, 5, 21, 30))
        self.assertEqual(shown(app)[0], self.ON)
        fire(app, ALARM_FROM_ENTITY, full("22:00:00"))
        self.assertEqual(shown(app)[0], BLANK)
        fire(app, ALARM_FROM_ENTITY, full("21:15:00"))
        self.assertEqual(shown(app)[0], self.ON)
        fire(app, ALARM_FROM_ENTITY, full("22:00:00"))
        fire(app, ALARM_FROM_ENTITY, full("unavailable"))
        self.assertEqual(shown(app)[0], self.ON)

    def test_the_line_comes_when_the_clock_reaches_the_helpers_time(self):
        app = self.app(now=house(11, 5, 21, 30), states={ALARM_FROM_ENTITY: full("22:00:00")})
        self.assertEqual(shown(app)[0], BLANK)
        tick(app, 29 * MIN + 50 * SEC)
        self.assertEqual(shown(app)[0], BLANK)
        tick(app, 10 * SEC)
        self.assertEqual(shown(app)[0], self.ON)

    def test_a_helper_the_snapshot_missed_is_read_on_the_tick(self):
        app = self.app(now=house(11, 5, 21, 30))
        self.assertEqual(shown(app)[0], self.ON)
        app.states[ALARM_FROM_ENTITY] = full("22:00:00")
        tick(app, 1 * SEC)
        self.assertEqual(shown(app)[0], BLANK)

    def test_an_alarm_that_is_off_says_no_alarm(self):
        app = self.app(now=house(11, 5, 21, 0), states={ALARM: full("off")})
        self.assertEqual(shown(app)[0], self.OFF)

    def test_an_alarm_that_cannot_be_read_is_no_line(self):
        for state in ("unknown", "unavailable"):
            with self.subTest(state=state):
                self.assertEqual(shown(self.app(now=house(11, 5, 21, 0), states={ALARM: full(state)})), [BLANK] * 3)
        app = make_app({RSSI: full("-60", updated=house(11, 5, 21, 0))}, now=house(11, 5, 21, 0))
        self.assertEqual(shown(app), [BLANK] * 3)

    def test_someone_and_an_empty_room_get_no_alarm_line(self):
        now = house(11, 5, 21, 0)
        for states, active in (({RSSI: full("-90", updated=now)}, "on"), ({}, "off")):
            with self.subTest(active=active):
                self.assertEqual(shown(self.app(now=now, states=states, active=active)), [BLANK] * 3)

    def test_the_alarm_comes_first_then_transit_and_rows_up_to_three(self):
        now = house(11, 5, 21, 0)
        app = self.app(now=now, states={
            UPCOMING: upcoming(LineTests.ROWS, expires=now + 10 * MIN),
            METRO: route("Disrupted", [departure(5, "21:05", cancelled=True)], as_of=now - 2 * MIN),
            STOG: route("Delayed", [departure(9, "21:09", delay=11)], as_of=now - 2 * MIN)})
        self.assertEqual([line[0] for line in shown(app)], ["Alarm 09:15", "Metro: disrupted", "S-tog: delayed"])

    def test_the_line_follows_the_alarm_and_its_time(self):
        app = self.app(now=house(11, 5, 21, 0))
        fire(app, ALARM_TIME, full("06:45:00"))
        self.assertEqual(shown(app)[0], ("Alarm 06:45", "mdi:alarm", "06:45"))
        fire(app, ALARM, full("off"))
        self.assertEqual(shown(app)[0], self.OFF)
        fire(app, ALARM, full("on"))
        self.assertEqual(shown(app)[0][0], "Alarm 06:45")

    def test_a_tap_toggles_the_alarm_and_logs_it(self):
        app = self.app()
        app._on_alarm(CONFIG["alarm_event"], {}, {})
        self.assertEqual(app.calls, [("homeassistant/toggle", {"entity_id": ALARM})])
        self.assertEqual([msg for level, msg in app.logs if msg.startswith("Alarm button")],
                         [f"Alarm button: toggles {ALARM}"])

    def test_a_tap_toggles_whatever_the_display_shows(self):
        app = make_app(active="off")
        app._on_alarm(CONFIG["alarm_event"], {}, {})
        self.assertEqual(app.calls, [("homeassistant/toggle", {"entity_id": ALARM})])

    def test_a_tap_whose_call_fails_logs_a_warning(self):
        app = self.app()
        app.fail_services = {"homeassistant/toggle"}
        app._on_alarm(CONFIG["alarm_event"], {}, {})
        self.assertEqual(app.calls, [])
        self.assertEqual(warnings(app), ["homeassistant/toggle failed: RuntimeError('HA unreachable')"])

    def test_the_event_and_the_entities_are_from_the_yaml(self):
        app = make_app(alarm_event="esphome.den_alarm", alarm_entity="input_boolean.den",
                       alarm_time_entity="input_datetime.den")
        self.assertIn(("_on_alarm", "esphome.den_alarm"), app.events)
        self.assertNotIn(("_on_alarm", "esphome.bathroom_display_alarm"), app.events)
        app._on_alarm("esphome.den_alarm", {}, {})
        self.assertEqual(app.calls, [("homeassistant/toggle", {"entity_id": "input_boolean.den"})])
        self.assertIn(("_on_source", "input_datetime.den"), [(name, entity) for name, entity, _ in app.listened])

    def test_the_helper_is_from_the_yaml(self):
        now = house(11, 5, 21, 30)
        states = {RSSI: full("-60", updated=now), ALARM: full("on"), ALARM_TIME: full("09:15:00"),
                  ALARM_FROM_ENTITY: full("22:00:00"), "input_datetime.den_from": full("21:00:00")}
        app = make_app(states, now=now, alarm_from_entity="input_datetime.den_from")
        self.assertEqual(shown(app)[0], self.ON)
        self.assertIn(("_on_source", "input_datetime.den_from"), [(name, entity) for name, entity, _ in app.listened])
        self.assertNotIn(ALARM_FROM_ENTITY, [entity for _, entity, _ in app.listened])


class NightTests(unittest.TestCase):
    """Sleep mode is the night: the attribute is on in every state and the lines are just the alarm line."""

    ALARM_LINE = ("Alarm 09:15", "mdi:alarm", "09:15")
    SLEEPING = {ALARM: full("on"), ALARM_TIME: full("09:15:00"), UPCOMING: upcoming(LineTests.ROWS),
                STOG: route("Disrupted", [departure(12, "08:42", cancelled=True)]), PIEZO: full("on")}
    CASES = (  # the room, its states, whether it is active and the state it gets
        ("an empty room", {SLEEP: full("on")}, "off", "off"),
        ("a room kept dark", {SLEEP: full("on"), LIGHT: full("off")}, "on", "off"),
        ("a lit room, nobody known", {SLEEP: full("on"), LIGHT: full("on")}, "on", "someone"),
        ("a lit room, Mikkel", {SLEEP: full("on"), LIGHT: full("on"), RSSI: full("-60")}, "on", "mikkel"))

    def test_night_is_on_in_every_state_while_sleep_mode_is_on(self):
        for name, states, active, state in self.CASES:
            with self.subTest(room=name):
                app = make_app(states, active=active)
                self.assertEqual((last(app)["state"], last(app)["attributes"]["night"]), (state, "on"))

    def test_night_is_empty_in_every_state_while_sleep_mode_is_not_on(self):
        for name, states, active, state in self.CASES:
            for sleep in ("off", "unavailable", "unknown", None):
                with self.subTest(room=name, sleep=sleep):
                    states = {**states, **({SLEEP: full(sleep)} if sleep else {})}
                    if not sleep:
                        states.pop(SLEEP)
                    app = make_app(states, active=active)
                    self.assertEqual(last(app)["attributes"]["night"], "")

    def test_the_reason_and_the_state_are_what_they_were_without_the_night(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("off")})
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]), ("off", f"sleep:{SLEEP}"))
        fire(app, LIGHT, full("on"))
        self.assertEqual((last(app)["state"], last(app)["attributes"]["reason"]),
                         ("someone", f"active:{ACTIVE} phone:none"))

    def test_the_lines_are_just_the_alarm_line_whatever_the_state(self):
        for name, states, active, state in self.CASES:
            with self.subTest(room=name):
                app = make_app({**self.SLEEPING, **states}, active=active)
                self.assertEqual(last(app)["state"], state)
                self.assertEqual(shown(app), [self.ALARM_LINE, BLANK, BLANK])

    def test_the_alarm_line_is_there_at_any_hour_of_the_night_mode(self):
        for now in (house(11, 5, 8, 30), house(11, 5, 14, 0), house(11, 5, 21, 0), house(11, 6, 3, 0)):
            with self.subTest(now=now):
                app = make_app({**self.SLEEPING, SLEEP: full("on"), LIGHT: full("on"),
                                RSSI: full("-60", updated=now)}, now=now)
                self.assertEqual(shown(app), [self.ALARM_LINE, BLANK, BLANK])

    def test_the_alarm_helper_does_not_hold_the_line_back_in_the_night(self):
        for now in (house(11, 5, 8, 30), house(11, 5, 21, 30), house(11, 5, 22, 30)):
            with self.subTest(now=now):
                app = make_app({**self.SLEEPING, SLEEP: full("on"), LIGHT: full("on"),
                                ALARM_FROM_ENTITY: full("23:00:00"), RSSI: full("-60", updated=now)}, now=now)
                self.assertEqual(shown(app), [self.ALARM_LINE, BLANK, BLANK])

    def test_it_says_no_alarm_when_the_alarm_is_off(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("on"), ALARM: full("off")})
        self.assertEqual(shown(app), [("No alarm", "mdi:alarm", "off"), BLANK, BLANK])

    def test_without_an_alarm_to_read_there_are_no_lines_but_it_is_night(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("on"), UPCOMING: upcoming(LineTests.ROWS)})
        self.assertEqual((last(app)["attributes"]["night"], shown(app)), ("on", [BLANK] * 3))

    def test_no_forecast_is_asked_for_in_the_night(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("on"), RSSI: full("-60")})
        self.assertEqual(app.tasks, [])

    def test_sleep_mode_turning_on_and_off_changes_the_payload_at_once(self):
        app = make_app({**self.SLEEPING, SLEEP: full("off"), RSSI: full("-60"), PIEZO: full("off")})
        before = last(app)["attributes"]
        self.assertEqual((before["night"], before["line1"]), ("", "S-tog: cancelled 08:42"))
        count = len(app.published)
        fire(app, SLEEP, full("on"))
        self.assertEqual(len(app.published), count + 1)
        self.assertEqual((last(app)["state"], last(app)["attributes"]["night"], shown(app)),
                         ("off", "on", [self.ALARM_LINE, BLANK, BLANK]))
        fire(app, LIGHT, full("on"))
        self.assertEqual((last(app)["state"], last(app)["attributes"]["night"], shown(app)),
                         ("mikkel", "on", [self.ALARM_LINE, BLANK, BLANK]))
        fire(app, SLEEP, full("off"))
        self.assertEqual((last(app)["attributes"]["night"], shown(app)[0]),
                         ("", ("S-tog: cancelled 08:42", "mdi:train", "")))

    def test_the_alarm_line_follows_the_alarm_in_the_night(self):
        app = make_app({SLEEP: full("on"), LIGHT: full("on"), ALARM: full("on"), ALARM_TIME: full("09:15:00")})
        fire(app, ALARM, full("off"))
        self.assertEqual(shown(app)[0][0], "No alarm")
        fire(app, ALARM_TIME, full("07:00:00"))
        fire(app, ALARM, full("on"))
        self.assertEqual(shown(app)[0][0], "Alarm 07:00")

    def test_every_attribute_is_a_string_and_the_line_fits(self):
        app = make_app({**self.SLEEPING, SLEEP: full("on"), LIGHT: full("on")})
        for key, value in last(app)["attributes"].items():
            with self.subTest(key=key):
                self.assertIsInstance(value, str)
        self.assertLessEqual(len(last(app)["attributes"]["line1"]), bd.LINE_MAX)


class ConfigTests(unittest.TestCase):
    def test_the_yaml_names_the_module_and_class(self):
        self.assertEqual((CONFIG["module"], CONFIG["class"]), ("bathroom_display", "BathroomDisplay"))
        self.assertTrue(hasattr(bd, CONFIG["class"]))

    def test_the_routes_are_the_ones_transit_mikkel_names(self):
        class Loader(yaml.SafeLoader):
            pass

        Loader.add_constructor("!secret", lambda loader, node: "")
        alarm = yaml.load((APPS / "transit" / "transit_alarm.yaml").read_text(), Loader=Loader)
        prefix = alarm["TransitStatus"]["status_sensor_prefix"]
        names = [prefix + route["sensor_id"] for route in alarm["TransitMikkel"]["routes"]]
        self.assertEqual([route["entity"] for route in CONFIG["transit"]], names)
        self.assertTrue(alarm["TransitStatus"]["expose_departure_ts"])

    def test_the_defaults_are_the_yamls_values(self):
        app = make_app()
        self.assertEqual((app.rssi_min, app.rssi_fresh), (bd.RSSI_MIN, timedelta(seconds=bd.RSSI_FRESH_S)))
        self.assertEqual(app.transit_max_age, timedelta(minutes=bd.TRANSIT_MAX_AGE_MIN))
        self.assertEqual((CONFIG["mikkel_rssi_min"], CONFIG["rssi_fresh_s"]), (-75, 20))
        self.assertEqual((app.rise_min, app.rise_db), (bd.RISE_MIN, bd.RISE_DB))
        self.assertEqual((CONFIG["rssi_rise_min"], CONFIG["rssi_rise_db"]), (-80, 6))
        self.assertEqual((app.play_event, app.speaker, app.radio), (bd.PLAY_EVENT, bd.SPEAKER, bd.RADIO))
        self.assertEqual((CONFIG["play_event"], CONFIG["speaker"], CONFIG["radio"]),
                         ("esphome.bathroom_display_play", "media_player.bathroom", "dr_podcasts://radio/p3"))

    def test_the_weighing_rain_and_alarm_defaults_are_the_yamls_values(self):
        app = make_app()
        self.assertEqual((app.measured_entity, app.weight_entity, app.fat_entity),
                         (bd.MEASURED, bd.WEIGHT, bd.BODY_FAT))
        self.assertEqual((CONFIG["measured_entity"], CONFIG["weight_entity"], CONFIG["body_fat_entity"]),
                         ("sensor.mikkel_last_measurement", "sensor.mikkel_weight", "sensor.mikkel_body_fat"))
        self.assertEqual((app.rain_entity, app.weather_entity), (bd.PIEZO, bd.WEATHER))
        self.assertEqual((CONFIG["rain_entity"], CONFIG["weather_entity"]),
                         ("binary_sensor.gw2000a_rain_state_piezo", "weather.forecast_home"))
        self.assertEqual((app.alarm_entity, app.alarm_time_entity, app.alarm_from_entity, app.alarm_event),
                         (bd.ALARM, bd.ALARM_TIME, bd.ALARM_FROM_ENTITY, bd.ALARM_EVENT))
        self.assertEqual((CONFIG["alarm_entity"], CONFIG["alarm_time_entity"], CONFIG["alarm_from_entity"],
                          CONFIG["alarm_event"]),
                         ("input_boolean.wakeup_bedroom", "input_datetime.wakeup_bedroom",
                          "input_datetime.bathroom_display_alarm_from", "esphome.bathroom_display_alarm"))

    def test_the_defaults_are_the_apps_without_the_yaml(self):
        app = make_app()
        app.args = {}
        app.initialize()
        self.assertEqual((app.measured_entity, app.weight_entity, app.fat_entity, app.rain_entity,
                          app.weather_entity, app.alarm_entity, app.alarm_time_entity, app.alarm_from_entity,
                          app.alarm_event),
                         (bd.MEASURED, bd.WEIGHT, bd.BODY_FAT, bd.PIEZO, bd.WEATHER, bd.ALARM, bd.ALARM_TIME,
                          bd.ALARM_FROM_ENTITY, bd.ALARM_EVENT))
        self.assertEqual(app.alarm_event, CONFIG["alarm_event"])

    def test_the_limits_and_windows_are_the_agreed_ones(self):
        self.assertEqual((bd.CARD_S, bd.WEIGH_FRESH, bd.COMPARE_AGO, bd.COMPARE_SLACK, bd.DEAD_BAND_KG),
                         (90, 120 * SEC, 30 * DAY, 3 * DAY, 0.3))
        self.assertEqual((bd.RAIN_FROM, bd.RAIN_TO, bd.RAIN_AHEAD, bd.RAIN_REFETCH),
                         (dtime(6), dtime(12), 4 * HOUR, 30 * MIN))
        self.assertEqual((bd.HOME_FROM, bd.HOME_TO), (dtime(16), dtime(20)))
        self.assertEqual((bd.ALARM_FROM, bd.ALARM_TO), (dtime(21), dtime(2)))
        self.assertEqual((bd.LINES, bd.LINE_MAX), (3, 32))

    def test_the_history_outlasts_the_baseline_window(self):
        self.assertGreater(bd.HISTORY_S, bd.BASELINE_FROM_S)


if __name__ == "__main__":
    unittest.main()
