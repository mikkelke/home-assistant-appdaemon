# tests/test_bathroom_display.py - BathroomDisplay: who the display sees, the lines it gets and when it
# publishes. The pure rules come first (the phone, the transit line, the rows of sensor.house_upcoming built
# by the real row builders, the fit of a line); the app is then driven through its real initialize() with the
# real bathroom_display.yaml, a fake clock and a settable state dict.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import types
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "presence"))
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

CONFIG = yaml.safe_load((APPS / "home_pulse" / "bathroom_display.yaml").read_text())["BathroomDisplay"]
HOUSE = maint.HOUSE_TZ
NOW = datetime(2026, 11, 5, 7, 30, tzinfo=timezone.utc)  # 08:30 in the house
SEC = timedelta(seconds=1)
MIN = timedelta(minutes=1)
HOUR = timedelta(hours=1)
DAY = timedelta(days=1)
FRESH = timedelta(seconds=20)
TRANSIT_AGE = timedelta(minutes=45)
ACTIVE = "binary_sensor.bathroom_active"
RSSI = CONFIG["rssi_entity"]
UPCOMING = CONFIG["upcoming_entity"]
METRO, STOG = (route["entity"] for route in CONFIG["transit"])
SLEEP = CONFIG["sleep_entity"]
LIGHT = CONFIG["light_entity"]
SPEAKER = CONFIG["speaker"]


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


def departure(minutes, scheduled, delay=0, cancelled=False):
    """A departure as TransitStatus publishes it: on time and not cancelled leave out delay_min and cancelled."""
    dep = {"ts": int((NOW + minutes * MIN).timestamp()), "time": scheduled, "line": "A", "scheduled_time": scheduled}
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
            "greeting": ""}
    for number in (1, 2, 3):
        base.update({f"line{number}": "", f"icon{number}": "", f"due{number}": ""})
    return {**base, **overrides}


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
                         attrs_of(reason="r", person="mikkel", greeting="Hej Mikkel",
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


def room(state):
    return full(state, {"stale": "false"})


def make_app(states=None, active="on", now=NOW, fail=0, **args):
    """The app after initialize(), the first `fail` publishes of which fail."""
    app = bd.BathroomDisplay.__new__(bd.BathroomDisplay)
    app.args = {**CONFIG, **args}
    app.now = now
    app.states = {ACTIVE: room(active), **(states or {})}
    app.published, app.logs, app.listened, app.events, app.scheduled = [], [], [], [], []
    app.calls, app.later, app.fail_services = [], [], set()
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
        if service in app.fail_services:
            raise RuntimeError("HA unreachable")
        app.calls.append((service, kw))

    app.get_state = get_state
    app.set_state = set_state
    app.call_service = call_service
    app.run_in = lambda callback, delay, **kw: app.later.append((callback, delay, kw))
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
                                                          greeting="Hej Mikkel"))
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
                         attrs_of(reason=f"active:{ACTIVE} phone:rise", person="mikkel", greeting="Hej Mikkel"))
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
                                 attrs_of(reason=self.HELD, person="mikkel", greeting="Hej Mikkel"))
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

    def test_it_listens_to_the_room_the_phone_the_rows_and_mikkels_routes(self):
        app = make_app()
        self.assertEqual(sorted((entity, kw.get("attribute")) for _, entity, kw in app.listened),
                         sorted([(ACTIVE, None), (RSSI, "all"), (UPCOMING, "all"), (SLEEP, "all"), (LIGHT, "all"),
                                 (METRO, "all"), (STOG, "all")]))
        self.assertEqual(app.events, [("_on_plugin_started", "plugin_started"),
                                      ("_on_play", "esphome.bathroom_display_play")])
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
            reason=f"active:{ACTIVE} phone:near", person="mikkel", greeting="Hej Mikkel",
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
            reason=f"active:{ACTIVE} phone:near", person="mikkel", greeting="Hej Mikkel",
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
        self.assertEqual(last(app)["attributes"], attrs_of(reason=self.DARK))

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

    def test_the_history_outlasts_the_baseline_window(self):
        self.assertGreater(bd.HISTORY_S, bd.BASELINE_FROM_S)


if __name__ == "__main__":
    unittest.main()
