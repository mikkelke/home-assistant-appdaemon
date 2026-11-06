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
            ("mikkel", True, "none", "mikkel"))
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

    def test_reason(self):
        cases = ((None, "off", "none", f"unknown:{ACTIVE}"), (False, "off", "near", f"inactive:{ACTIVE}"),
                 (True, "mikkel", "near", f"active:{ACTIVE} phone:near"),
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

    def test_a_row_without_a_day_is_not_due(self):
        for row in ({}, {"info": ["Due 04.11"]}, {"detail": "due today"}, {"done": "tap", "info": ["Due soon"]},
                    {"done": "tap", "detail": "due"}, {"done": "tap", "detail": "6 days left, 11.11"},
                    {"done": "tap", "info": ["Due 31.02"]}, {"done": "tap", "info": "Due 04.11"}):
            with self.subTest(row=row):
                self.assertIsNone(bd.due_at(row, NOW))

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

    def test_a_one_off_task_due_tomorrow_is_timed_beyond_24_hours_so_only_a_bathroom_one_is_listed(self):
        self.assertEqual(self.lines([one_off(date(2026, 11, 6))]),
                         [bd.Line("Spray the Alocasia", "mdi:spray", "tomorrow")])
        self.assertEqual(self.lines([one_off(date(2026, 11, 6), area="kitchen")]), [])

    def test_a_calendar_plant_is_a_line_the_day_before_on_the_day_and_after(self):
        for days_ago, chip in ((6, "tomorrow"), (7, "today"), (9, "overdue")):
            with self.subTest(days_ago=days_ago):
                self.assertEqual(self.lines([watering(days_ago)]),
                                 [bd.Line("Water the Alocasia: 0.7 L", "mdi:watering-can-outline", chip)])

    def test_a_calendar_plant_in_another_room_is_a_line_from_the_day_it_is_due(self):
        self.assertEqual(self.lines([watering(6, area="kitchen")]), [])
        self.assertEqual([line.due for line in self.lines([watering(7, area="kitchen")])], ["today"])
        self.assertEqual([line.due for line in self.lines([watering(9, area="kitchen")])], ["overdue"])

    def test_timed_rows_inside_24_hours_are_lines_whatever_the_room(self):
        rows = [maint_row("past", "Earlier today", at=NOW - 2 * HOUR, area="kitchen"),
                maint_row("yesterday", "Since yesterday", at=NOW - DAY, area="kitchen"),
                maint_row("edge", "Exactly 24 hours", at=NOW + DAY, area="kitchen"),
                maint_row("tonight", "Tonight", at=NOW + 6 * HOUR, area="living_room")]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Since yesterday", "overdue"), ("Earlier today", "today"), ("Tonight", "today"),
                          ("Exactly 24 hours", "tomorrow")])

    def test_beyond_24_hours_only_the_bathroom_rows_within_14_days_are_lines(self):
        rows = [maint_row("a", "Bathroom in five days", at=NOW + 5 * DAY, area="bathroom"),
                maint_row("b", "Kitchen in five days", at=NOW + 5 * DAY, area="kitchen"),
                maint_row("c", "Bathroom at 14 days", at=NOW + 14 * DAY, area="bathroom"),
                maint_row("d", "Bathroom a minute later", at=NOW + 14 * DAY + MIN, area="bathroom"),
                maint_row("e", "No room in five days", at=NOW + 5 * DAY),
                maint_row("f", "Guest bathroom in five days", at=NOW + 5 * DAY, area="guest_bathroom")]
        self.assertEqual([(line.text, line.due) for line in self.lines(rows, limit=10)],
                         [("Bathroom in five days", "5 d"), ("Bathroom at 14 days", "14 d")])

    def test_what_is_due_comes_first_earliest_first_then_the_bathroom_rows_earliest_first(self):
        rows = [maint_row("later", "Bathroom later", at=NOW + 3 * DAY, area="bathroom"),
                maint_row("soon", "Bathroom soon", at=NOW + 2 * DAY, area="bathroom"),
                maint_row("afternoon", "This afternoon", at=NOW + 6 * HOUR),
                maint_row("old", "Overdue since yesterday", at=NOW - DAY)]
        lines = self.lines(rows, limit=10)
        self.assertEqual([line.text for line in lines],
                         ["Overdue since yesterday", "This afternoon", "Bathroom soon", "Bathroom later"])
        self.assertEqual([line.due for line in lines], ["overdue", "today", "2 d", "3 d"])

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

    def test_a_row_with_no_day_is_never_a_line(self):
        rows = [core.make_row("maint:battery:sensor.door", "maint", "Bathroom door battery", "no data since 03.11",
                              area="bathroom", audience="admin"),
                core.make_row("maint:plant:ivy", "maint", "Water the Ivy", "soil 12%, water at 20%",
                              area="bathroom", audience="admin"),
                maint_row("tapped", "Tapped without a day", area="bathroom", tap_done=True, info=["Every 10 runs"]),
                maint_row("info", "Due text without a Done button", area="bathroom", info=["Due 04.11"])]
        self.assertEqual(self.lines(rows), [])

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


def room(state):
    return full(state, {"stale": "false"})


def make_app(states=None, active="on", now=NOW, fail=0, **args):
    """The app after initialize(), the first `fail` publishes of which fail."""
    app = bd.BathroomDisplay.__new__(bd.BathroomDisplay)
    app.args = {**CONFIG, **args}
    app.now = now
    app.states = {ACTIVE: room(active), **(states or {})}
    app.published, app.logs, app.listened, app.events, app.scheduled = [], [], [], [], []
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

    app.get_state = get_state
    app.set_state = set_state
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


def tick(app, delta=10 * SEC):
    app.now += delta
    app._on_tick({})


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
        app = make_app({RSSI: full("-76")})
        self.assertEqual(last(app)["state"], "someone")
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:weak")
        phone(app, "-75.4")
        self.assertEqual(last(app)["state"], "someone")
        phone(app, "-75")
        self.assertEqual(last(app)["state"], "mikkel")

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
        for reading in ("unknown", "-80", "unavailable"):
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
        self.assertEqual(app.events, [("_on_plugin_started", "plugin_started")])
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
        phone(app, "-80")
        self.assertEqual(len(app.published), 2)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:held")
        phone(app, "-81")
        self.assertEqual(len(app.published), 2)
        phone(app, "-70")
        self.assertEqual(len(app.published), 3)
        self.assertEqual(last(app)["attributes"]["reason"], f"active:{ACTIVE} phone:near")

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
            maint_row("battery", "Bathroom door battery", at=NOW + 4 * DAY, area="bathroom",
                      icon="mdi:battery-clock-outline")]

    def test_mikkel_gets_the_rows_that_are_due_and_the_bathroom_rows_after_them(self):
        app = make_app({RSSI: full("-60"), UPCOMING: upcoming(self.ROWS)})
        self.assertEqual(last(app)["attributes"], attrs_of(
            reason=f"active:{ACTIVE} phone:near", person="mikkel", greeting="Hej Mikkel",
            line1="Spray the Alocasia", icon1="mdi:spray", due1="overdue",
            line2="Water the Alocasia: 0.7 L", icon2="mdi:watering-can-outline", due2="today",
            line3="Bathroom door battery", icon3="mdi:battery-clock-outline", due3="4 d"))

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


if __name__ == "__main__":
    unittest.main()
