# tests/test_heating_alerts.py - the floor-heating controller's plant alerts (heating_alerts.py):
# cold room, pump off, floor not warming, no hot water, uneven floors, fallback, stale forecast
# and zone 8 in heat; the episode and push-limit rules; the state that survives a restart. Pure
# logic, so no AppDaemon harness is needed.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import heating_alerts as al  # noqa: E402
import heating_plan as hp  # noqa: E402

CFG = hp.AlertConfig()
T0 = 1_800_000_000.0
STEP = 300.0
SIX_HOURS = 6 * 3600.0
START, SUPPRESSED, CLEAR = al.EVENT_START, al.EVENT_SUPPRESSED, al.EVENT_CLEAR


def zobs(zone="kristines_room", **kw):
    fields = dict(
        zone=zone,
        dt_s=STEP,
        season_on=True,
        hvac_mode="heat",
        calling=False,
        air=22.0,
        air_fresh=True,
        target=21.5,
        plan_mode="hold",
        fallback=False,
        floor=21.0,
        window_active=False,
    )
    fields.update(kw)
    return al.ZoneObs(**fields)


class Run:
    """An Alerts fed one five-minute tick at a time."""

    def __init__(self, cfg=CFG, saved=None):
        self.alerts = al.Alerts(cfg, saved)
        self.ts = T0
        self.events = []

    def tick(self, zones=(), **house):
        fields = dict(
            day="2026-10-17",
            hour=12,
            season_on=True,
            relay_w=1.6,
            outdoor=8.0,
            supply=None,
            supply_enabled=False,
            forecast_stale=False,
            monitor_heat=False,
        )
        fields.update(house)
        events = self.alerts.update(al.HouseObs(now_ts=self.ts, **fields), list(zones))
        self.events += events
        self.ts += STEP
        return events

    def ticks(self, n, zones=(), **house):
        out = []
        for _ in range(n):
            out += self.tick(zones() if callable(zones) else zones, **house)
        return out

    def restart(self):
        saved = json.loads(json.dumps(self.alerts.snapshot()))
        again = Run(saved=saved)
        again.ts = self.ts
        return again


def kinds(events):
    return [(e.kind, e.key) for e in events]


class WriteFailures(unittest.TestCase):
    def test_two_consecutive_failures_start_one_episode(self):
        alerts = al.Alerts(CFG)
        self.assertEqual(alerts.write_result("living", 1, T0), [])
        self.assertEqual(kinds(alerts.write_result("living", 2, T0 + STEP)), [(START, "write:living")])
        self.assertEqual(alerts.write_result("living", 3, T0 + 2 * STEP), [])
        self.assertEqual(alerts.active(), ["write:living"])

    def test_confirmation_clears_and_rearms_the_episode(self):
        alerts = al.Alerts(CFG)
        alerts.write_result("living", 2, T0)
        self.assertEqual(kinds(alerts.write_result("living", 0, T0 + STEP)), [(CLEAR, "write:living")])
        self.assertEqual(alerts.write_result("living", 1, T0 + 2 * STEP), [])
        self.assertEqual(
            kinds(alerts.write_result("living", 2, T0 + 3 * STEP)), [(START, "write:living")]
        )

    def test_a_restart_does_not_repeat_the_push(self):
        alerts = al.Alerts(CFG)
        alerts.write_result("living", 2, T0)
        restarted = al.Alerts(CFG, json.loads(json.dumps(alerts.snapshot())))
        self.assertEqual(restarted.write_result("living", 3, T0 + STEP), [])
        self.assertEqual(restarted.active(), ["write:living"])


class ColdRoom(unittest.TestCase):
    def test_it_pushes_when_the_room_has_been_cold_for_thirty_minutes_not_before(self):
        r = Run()
        self.assertEqual(r.ticks(6, [zobs(air=18.6)]), [])
        (event,) = r.tick([zobs(air=18.6)])
        self.assertEqual(
            (event.kind, event.key, event.zone), (START, "cold:kristines_room", "kristines_room")
        )
        self.assertEqual(event.title, "Heating: cold room")
        self.assertEqual(event.message, "Kristines room 18.6 °C for 30 min (target 21.5)")

    def test_it_stays_quiet_while_the_room_stays_cold(self):
        r = Run()
        r.ticks(7, [zobs(air=18.6)])
        self.assertEqual(r.ticks(40, [zobs(air=18.6)]), [])
        self.assertEqual(r.alerts.active(), ["cold:kristines_room"])

    def test_one_warm_reading_starts_the_count_again(self):
        r = Run()
        r.ticks(5, [zobs(air=18.6)])
        r.tick([zobs(air=19.0)])
        self.assertEqual(r.ticks(5, [zobs(air=18.6)]), [])

    def test_it_clears_when_the_room_is_warm_and_a_later_cold_spell_is_a_new_episode(self):
        r = Run()
        r.ticks(7, [zobs(air=18.6)])
        self.assertEqual(kinds(r.tick([zobs(air=21.0)])), [(CLEAR, "cold:kristines_room")])
        self.assertEqual(r.alerts.active(), [])
        r.ts = T0 + SIX_HOURS + 1800
        events = r.ticks(8, [zobs(air=18.6)])
        self.assertEqual(kinds(events), [(START, "cold:kristines_room")])

    def test_a_second_cold_spell_within_six_hours_is_logged_but_not_pushed(self):
        r = Run()
        r.ticks(7, [zobs(air=18.6)])
        r.tick([zobs(air=21.0)])
        events = r.ticks(8, [zobs(air=18.6)])
        self.assertEqual(kinds(events), [(SUPPRESSED, "cold:kristines_room")])
        self.assertEqual(r.alerts.active(), ["cold:kristines_room"])
        self.assertEqual(r.ticks(20, [zobs(air=18.6)]), [])

    def test_the_limit_belongs_to_the_zone_so_another_zone_still_pushes(self):
        r = Run()
        both = [zobs("kristines_room", air=18.6), zobs("claudias_room", air=18.6)]
        events = r.ticks(7, both)
        self.assertEqual(
            sorted(kinds(events)), [(START, "cold:claudias_room"), (START, "cold:kristines_room")]
        )

    def test_a_deliberate_coast_is_not_a_cold_room(self):
        for mode in (
            hp.MODE_WINDOW, hp.MODE_SETTLE, hp.MODE_PARTY, hp.MODE_FLOOR_CAP, hp.MODE_GUARD
        ):
            r = Run()
            self.assertEqual(r.ticks(12, [zobs(air=18.0, plan_mode=mode)]), [], mode)

    def test_other_reasons_for_a_cool_reading_that_are_not_a_cold_room(self):
        cases = {
            "thermostat off": ([zobs(air=18.0, hvac_mode="off")], {}),
            "season off": ([zobs(air=18.0)], {"season_on": False}),
            "no reading": ([zobs(air=None)], {}),
            "stale reading": ([zobs(air=18.0, air_fresh=False)], {}),
            "target at the floor": ([zobs("bathroom", air=18.0, target=19.0)], {}),
            "exactly the limit": ([zobs(air=19.0)], {}),
        }
        for name, (zones, house) in cases.items():
            r = Run()
            self.assertEqual(r.ticks(12, zones, **house), [], name)

    def test_the_names_are_in_sentence_case(self):
        r = Run()
        events = r.ticks(7, [zobs("claudias_room", air=18.0, target=21.0)])
        self.assertEqual(events[0].message, "Claudias room 18.0 °C for 30 min (target 21.0)")

    def test_the_thresholds_come_from_the_config(self):
        cfg = hp.AlertConfig(cold_c=20.0, cold_minutes=10)
        r = Run(cfg)
        events = r.ticks(3, [zobs(air=19.5)])
        self.assertEqual(events[0].message, "Kristines room 19.5 °C for 10 min (target 21.5)")


class PumpOff:
    """Shared helpers: a zone calling every tick for hours, its floor rising so that only the
    pump alert can fire unless a test says otherwise."""

    @staticmethod
    def calling(zone="bathroom", floor=None, **kw):
        counter = {"i": 0}

        def make():
            counter["i"] += 1
            f = 20.0 + 0.05 * counter["i"] if floor is None else floor(counter["i"])
            return [zobs(zone, calling=True, floor=f, **kw)]

        return make


class PumpAlert(unittest.TestCase):
    def test_four_hours_of_calling_with_the_relay_at_idle_pushes(self):
        r = Run()
        make = PumpOff.calling()
        self.assertEqual(r.ticks(48, make, relay_w=1.6), [])
        (event,) = r.tick(make(), relay_w=1.6)
        self.assertEqual((event.kind, event.key), (START, "no_pump:bathroom"))
        self.assertEqual(event.title, "Heating: pump off")
        self.assertEqual(event.message, "Bathroom has called 4 h, wiring-centre pump is off")

    def test_a_relay_that_draws_power_is_a_pump_that_runs(self):
        r = Run()
        self.assertEqual(r.ticks(80, PumpOff.calling(), relay_w=16.0), [])

    def test_five_watts_is_still_idle_and_six_is_not(self):
        for watts, expected in ((5.0, [START]), (5.1, [])):
            r = Run()
            events = r.ticks(49, PumpOff.calling(), relay_w=watts)
            self.assertEqual([e.kind for e in events], expected, watts)

    def test_ninety_percent_of_the_last_four_hours_is_the_line(self):
        for powered, expected in ((4, [START]), (5, [])):
            r = Run()
            make = PumpOff.calling()
            r.ticks(44, make, relay_w=1.6)
            r.ticks(powered, make, relay_w=16.0)
            r.ticks(48 - 44 - powered, make, relay_w=1.6)
            events = r.tick(make(), relay_w=1.6)
            self.assertEqual([e.kind for e in events], expected, powered)

    def test_a_break_in_the_call_starts_the_count_again(self):
        r = Run()
        make = PumpOff.calling()
        r.ticks(40, make, relay_w=1.6)
        r.tick([zobs("bathroom", calling=False)], relay_w=1.6)
        self.assertEqual(r.ticks(30, make, relay_w=1.6), [])

    def test_a_long_silence_ends_the_call_too(self):
        r = Run()
        make = PumpOff.calling()
        r.ticks(36, make, relay_w=1.6)
        r.ts += 25 * 60
        self.assertEqual(r.ticks(30, make, relay_w=1.6), [])

    def test_a_relay_nobody_reads_concludes_nothing(self):
        r = Run()
        make = PumpOff.calling()
        self.assertEqual(r.ticks(60, make, relay_w=None), [])
        r = Run()
        r.ticks(10, make, relay_w=1.6)
        self.assertEqual(r.ticks(50, make, relay_w=None), [])

    def test_the_pump_coming_back_clears_the_alert(self):
        r = Run()
        make = PumpOff.calling()
        r.ticks(49, make, relay_w=1.6)
        self.assertEqual(r.alerts.active(), ["no_pump:bathroom"])
        self.assertEqual(r.ticks(4, make, relay_w=16.0), [])
        self.assertEqual(kinds(r.tick(make(), relay_w=16.0)), [(CLEAR, "no_pump:bathroom")])

    def test_the_hours_in_the_message_follow_the_config(self):
        cfg = hp.AlertConfig(calling_hours=2)
        r = Run(cfg)
        events = r.ticks(25, PumpOff.calling(), relay_w=1.6)
        self.assertEqual(events[0].message, "Bathroom has called 2 h, wiring-centre pump is off")


class FloorAlert(unittest.TestCase):
    def run_call(self, floor, outdoor=8.0, window_at=None, n=49):
        r = Run()
        make = PumpOff.calling("claudias_room", floor=floor)
        events = []
        for i in range(n):
            zones = make()
            if window_at == i:
                zones = [
                    zobs("claudias_room", calling=True, floor=floor(i + 1), window_active=True)
                ]
            events += r.tick(zones, relay_w=16.0, outdoor=outdoor)
        return events

    def test_a_floor_that_does_not_rise_in_a_four_hour_call_pushes(self):
        (event,) = self.run_call(lambda i: 20.0)
        self.assertEqual((event.kind, event.key), (START, "floor:claudias_room"))
        self.assertEqual(event.title, "Heating: floor not warming")
        self.assertEqual(event.message, "Claudias room floor not warming after 4 h of heat")

    def test_a_tenth_of_a_degree_is_the_line(self):
        late = lambda i: 20.0 if i < 48 else 20.05  # noqa: E731
        self.assertEqual(len(self.run_call(late)), 1)
        late = lambda i: 20.0 if i < 48 else 20.5  # noqa: E731
        self.assertEqual(self.run_call(late), [])

    def test_a_floor_that_warms_is_fine(self):
        self.assertEqual(self.run_call(lambda i: 20.0 + 0.05 * i), [])

    def test_it_only_counts_in_cold_weather(self):
        flat = lambda i: 20.0  # noqa: E731
        self.assertEqual(self.run_call(flat, outdoor=12.0), [])
        self.assertEqual(self.run_call(flat, outdoor=None), [])
        self.assertEqual(len(self.run_call(flat, outdoor=11.9)), 1)

    def test_an_open_window_during_the_call_explains_a_cold_floor(self):
        self.assertEqual(self.run_call(lambda i: 20.0, window_at=20), [])

    def test_it_needs_floor_readings_at_the_one_hour_and_four_hour_marks(self):
        def gap(lo, hi):
            return lambda i: None if lo <= i < hi else 20.0

        self.assertEqual(self.run_call(gap(13, 19)), [])
        self.assertEqual(self.run_call(gap(49, 55), n=56), [])
        self.assertEqual(len(self.run_call(gap(13, 18))), 1)

    def test_it_clears_when_the_call_ends(self):
        r = Run()
        make = PumpOff.calling("claudias_room", floor=lambda i: 20.0)
        r.ticks(49, make, relay_w=16.0)
        self.assertEqual(r.alerts.active(), ["floor:claudias_room"])
        events = r.tick([zobs("claudias_room", calling=False)], relay_w=16.0)
        self.assertEqual(kinds(events), [(CLEAR, "floor:claudias_room")])


class SharedLimit(unittest.TestCase):
    def test_pump_and_floor_alerts_of_one_zone_are_one_push(self):
        r = Run()
        make = PumpOff.calling("bathroom", floor=lambda i: 20.0)
        r.ticks(48, make, relay_w=1.6)
        events = r.tick(make(), relay_w=1.6)
        self.assertEqual(
            kinds(events), [(START, "no_pump:bathroom"), (SUPPRESSED, "floor:bathroom")]
        )
        self.assertEqual(r.alerts.active(), ["floor:bathroom", "no_pump:bathroom"])

    def test_a_cold_room_and_a_pump_that_is_off_are_one_push_too(self):
        r = Run()
        make = PumpOff.calling("bathroom", air=18.0, target=22.0)
        events = r.ticks(49, make, relay_w=1.6)
        self.assertEqual(
            kinds(events), [(START, "cold:bathroom"), (SUPPRESSED, "no_pump:bathroom")]
        )

    def test_after_six_hours_the_next_episode_pushes_again(self):
        r = Run()
        make = PumpOff.calling("bathroom", air=18.0, target=22.0)
        r.ticks(7, make, relay_w=16.0)
        r.tick([zobs("bathroom", air=21.0)])
        r.ts = T0 + SIX_HOURS + 1800
        events = r.ticks(7, [zobs("bathroom", air=18.0, target=22.0)])
        self.assertEqual(kinds(events), [(START, "cold:bathroom")])

    def test_the_other_alerts_are_not_limited(self):
        r = Run()
        both = [zobs("family", fallback=True), zobs("kristines_room", fallback=True)]
        r.ticks(37, both, monitor_heat=True, forecast_stale=True)
        self.assertEqual(
            sorted(kinds(r.events)),
            [
                (START, "fallback:family"),
                (START, "fallback:kristines_room"),
                (START, "forecast_stale"),
                (START, "zone8_heat"),
            ],
        )


class NoHotWater:
    @staticmethod
    def zones(*calling):
        return [zobs(z, calling=True) for z in calling] + [zobs("family")]


class SupplyAlert(unittest.TestCase):
    def test_nothing_happens_while_no_clamp_is_configured(self):
        r = Run()
        self.assertEqual(
            r.ticks(30, NoHotWater.zones("bathroom"), supply=20.0, supply_enabled=False), []
        )

    def test_a_zone_calling_for_half_an_hour_with_a_cold_supply_pushes(self):
        r = Run()
        zones = NoHotWater.zones("bathroom")
        self.assertEqual(r.ticks(6, zones, supply=24.0, supply_enabled=True), [])
        (event,) = r.tick(zones, supply=24.0, supply_enabled=True)
        self.assertEqual((event.kind, event.key, event.zone), (START, "no_hot_water", None))
        self.assertEqual(event.message, "No hot water from the building (supply 24 °C)")

    def test_the_line_is_below_28_degrees(self):
        for supply, expected in ((27.9, 1), (28.0, 0), (45.0, 0)):
            r = Run()
            events = r.ticks(7, NoHotWater.zones("bathroom"), supply=supply, supply_enabled=True)
            self.assertEqual(len(events), expected, supply)

    def test_without_a_call_or_without_a_reading_there_is_nothing_to_say(self):
        r = Run()
        self.assertEqual(r.ticks(12, NoHotWater.zones(), supply=20.0, supply_enabled=True), [])
        self.assertEqual(
            r.ticks(12, NoHotWater.zones("bathroom"), supply=None, supply_enabled=True), []
        )

    def test_any_zone_will_do(self):
        r = Run()
        r.ticks(5, NoHotWater.zones("bathroom"), supply=20.0, supply_enabled=True)
        events = r.ticks(
            3, NoHotWater.zones("bathroom", "claudias_room"), supply=20.0, supply_enabled=True
        )
        self.assertEqual(len(events), 1)

    def test_it_clears_when_the_supply_is_back(self):
        r = Run()
        r.ticks(7, NoHotWater.zones("bathroom"), supply=20.0, supply_enabled=True)
        events = r.tick(NoHotWater.zones("bathroom"), supply=40.0, supply_enabled=True)
        self.assertEqual(kinds(events), [(CLEAR, "no_hot_water")])

    def test_the_line_comes_from_the_config(self):
        r = Run(hp.AlertConfig(no_hot_water_c=35.0))
        events = r.ticks(7, NoHotWater.zones("bathroom"), supply=30.0, supply_enabled=True)
        self.assertEqual(len(events), 1)


class EvennessAlert(unittest.TestCase):
    DAYS = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19"]

    def night(self, r, day, value, hours=(2, 3, 4, 5), **house):
        for hour in hours:
            for _ in range(12):
                r.tick([zobs("family", evenness=value)], day=day, hour=hour, **house)
        return r.tick([zobs("family", evenness=value)], day=day, hour=6, **house)

    def nights(self, values, **house):
        r = Run()
        out = []
        for day, value in zip(self.DAYS, values):
            out.append(self.night(r, day, value, **house))
        return r, out

    def test_the_third_uneven_night_pushes_with_the_mean(self):
        r, out = self.nights([1.8, 1.8, 1.8])
        self.assertEqual([e for e in out[0] + out[1]], [])
        (event,) = out[2]
        self.assertEqual((event.kind, event.key), (START, "uneven:family"))
        self.assertEqual(event.title, "Heating: uneven floors")
        self.assertEqual(event.message, "Living/dining floors uneven: 1.8 °C")

    def test_it_is_the_mean_of_three_nights_that_counts(self):
        r, out = self.nights([1.0, 2.0, 2.0])
        self.assertEqual(out[2][0].message, "Living/dining floors uneven: 1.7 °C")
        r, out = self.nights([0.0, 2.0, 2.0])
        self.assertEqual(out[2], [])

    def test_the_floors_may_be_uneven_either_way(self):
        r, out = self.nights([-1.8, -1.8, -1.8])
        self.assertEqual(out[2][0].message, "Living/dining floors uneven: 1.8 °C")

    def test_one_and_a_half_degrees_is_the_line_and_beyond_it_alerts(self):
        r, out = self.nights([1.5, 1.5, 1.5])
        self.assertEqual(out[2], [])
        r, out = self.nights([1.6, 1.6, 1.6])
        self.assertEqual(len(out[2]), 1)

    def test_it_re_arms_below_one_degree_not_below_the_line(self):
        r, out = self.nights([1.8, 1.8, 1.8, 0.5, 0.5])
        self.assertEqual([len(o) for o in out], [0, 0, 1, 0, 1])
        self.assertEqual(kinds(out[4]), [(CLEAR, "uneven:family")])
        r2, out2 = self.nights([1.8, 1.8, 1.8, 1.0, 1.0])
        self.assertEqual([len(o) for o in out2], [0, 0, 1, 0, 0])
        self.assertEqual(r2.alerts.active(), ["uneven:family"])

    def test_after_it_has_cleared_a_new_run_of_uneven_nights_pushes_again(self):
        r = Run()
        values = [1.8, 1.8, 1.8, 0.5, 0.5, 2.0, 2.0, 2.0]
        days = [f"2026-10-{14 + i}" for i in range(len(values))]
        out = [self.night(r, d, v) for d, v in zip(days, values)]
        self.assertEqual(kinds(out[-1]), [(START, "uneven:family")])

    def test_a_night_with_less_than_two_hours_of_readings_is_not_counted(self):
        r = Run()
        for day in self.DAYS[:3]:
            r.ticks(18, [zobs("family", evenness=3.0)], day=day, hour=3)
            r.tick([zobs("family", evenness=3.0)], day=day, hour=6)
        self.assertEqual(r.events, [])

    def test_readings_outside_the_small_hours_are_ignored(self):
        r = Run()
        for day in self.DAYS[:3]:
            r.ticks(48, [zobs("family", evenness=3.0)], day=day, hour=14)
            r.tick([zobs("family", evenness=3.0)], day=day, hour=15)
        self.assertEqual(r.events, [])

    def test_outside_the_season_the_floors_are_not_compared(self):
        r, out = self.nights([3.0, 3.0, 3.0], season_on=False)
        self.assertEqual(out, [[], [], []])

    def test_a_zone_that_does_not_watch_its_floors_has_nothing_to_report(self):
        r = Run()
        for day in self.DAYS[:3]:
            r.ticks(48, [zobs("family", evenness=None)], day=day, hour=3)
            r.tick([zobs("family", evenness=None)], day=day, hour=6)
        self.assertEqual(r.events, [])

    def test_a_night_still_open_when_the_day_changes_is_closed_then(self):
        r = Run()
        r.ticks(36, [zobs("family", evenness=2.5)], day="2026-10-14", hour=3)
        r.tick([zobs("family")], day="2026-10-15", hour=1)
        self.assertEqual(r.alerts.snapshot()["nights"]["family"], [["2026-10-14", 2.5]])

    def test_the_count_of_nights_follows_the_config(self):
        r = Run(hp.AlertConfig(evenness_nights=1, evenness_c=1.0))
        out = self.night(r, "2026-10-14", 1.2)
        self.assertEqual(kinds(out), [(START, "uneven:family")])

    def test_the_nights_kept_do_not_grow_without_end(self):
        r = Run()
        for i in range(20):
            self.night(r, f"2026-09-{i + 1:02d}", 0.2)
        self.assertEqual(len(r.alerts.snapshot()["nights"]["family"]), al.NIGHTS_KEPT)


class OtherAlerts(unittest.TestCase):
    def test_a_zone_with_no_room_temperature_for_three_hours_pushes_once_per_episode(self):
        r = Run()
        fallback = [zobs("family", fallback=True)]
        self.assertEqual(r.ticks(36, fallback), [])
        (event,) = r.tick(fallback)
        self.assertEqual((event.kind, event.key), (START, "fallback:family"))
        self.assertEqual(event.title, "Heating: no room temperature")
        self.assertEqual(
            event.message,
            "Family has no usable room temperature and runs on its constant target",
        )
        self.assertEqual(r.ticks(20, fallback), [])
        self.assertEqual(kinds(r.tick([zobs("family")])), [(CLEAR, "fallback:family")])
        self.assertEqual(r.ticks(36, fallback), [])
        self.assertEqual(kinds(r.tick(fallback)), [(START, "fallback:family")])

    def test_a_reading_that_comes_back_for_a_moment_starts_the_three_hours_again(self):
        r = Run()
        fallback = [zobs("family", fallback=True)]
        r.ticks(30, fallback)
        r.tick([zobs("family")])
        self.assertEqual(r.ticks(30, fallback), [])

    def test_fallback_outside_the_season_is_not_news(self):
        r = Run()
        self.assertEqual(r.ticks(40, [zobs("family", fallback=True)], season_on=False), [])

    def test_a_stale_forecast_pushes_once_and_clears(self):
        r = Run()
        events = r.tick([], forecast_stale=True)
        self.assertEqual(kinds(events), [(START, "forecast_stale")])
        self.assertEqual(events[0].title, "Heating: forecast stale")
        self.assertIn("no sun cuts", events[0].message)
        self.assertEqual(r.ticks(10, [], forecast_stale=True), [])
        self.assertEqual(kinds(r.tick([], forecast_stale=False)), [(CLEAR, "forecast_stale")])

    def test_a_stale_forecast_outside_the_season_is_not_news(self):
        r = Run()
        self.assertEqual(r.ticks(5, [], forecast_stale=True, season_on=False), [])

    def test_the_living_room_monitor_in_heat_runs_the_pump_for_nothing(self):
        r = Run()
        events = r.tick([], monitor_heat=True, season_on=False)
        self.assertEqual(kinds(events), [(START, "zone8_heat")])
        self.assertEqual(events[0].message, "Zone 8 has no actuator: heat mode runs the pump")
        self.assertEqual(events[0].title, "Heating: zone 8 in heat")
        self.assertEqual(r.ticks(5, [], monitor_heat=True), [])
        self.assertEqual(kinds(r.tick([], monitor_heat=False)), [(CLEAR, "zone8_heat")])

    def test_active_lists_the_open_episodes_sorted(self):
        r = Run()
        r.ticks(37, [zobs("family", fallback=True)], monitor_heat=True, forecast_stale=True)
        self.assertEqual(r.alerts.active(), ["fallback:family", "forecast_stale", "zone8_heat"])


class Restart(unittest.TestCase):
    def test_an_episode_that_was_pushed_is_not_pushed_again_after_a_restart(self):
        r = Run()
        r.ticks(7, [zobs(air=18.6)])
        again = r.restart()
        self.assertEqual(again.alerts.active(), ["cold:kristines_room"])
        self.assertEqual(again.ticks(10, [zobs(air=18.6)]), [])
        self.assertEqual(kinds(again.tick([zobs(air=21.0)])), [(CLEAR, "cold:kristines_room")])

    def test_a_cold_spell_in_progress_keeps_its_clock(self):
        r = Run()
        r.ticks(4, [zobs(air=18.6)])
        again = r.restart()
        self.assertEqual(kinds(again.ticks(3, [zobs(air=18.6)])), [(START, "cold:kristines_room")])

    def test_a_call_in_progress_carries_on(self):
        r = Run()
        make = PumpOff.calling()
        r.ticks(30, make, relay_w=1.6)
        again = r.restart()
        events = again.ticks(19, make, relay_w=1.6)
        self.assertEqual(kinds(events), [(START, "no_pump:bathroom")])

    def test_the_push_limit_is_remembered(self):
        r = Run()
        r.ticks(7, [zobs(air=18.6)])
        r.tick([zobs(air=21.0)])
        again = r.restart()
        self.assertEqual(
            kinds(again.ticks(8, [zobs(air=18.6)])), [(SUPPRESSED, "cold:kristines_room")]
        )

    def test_a_night_in_progress_and_the_nights_so_far_are_kept(self):
        r = Run()
        for day in ("2026-10-14", "2026-10-15"):
            EvennessAlert().night(r, day, 1.8)
        r.ticks(24, [zobs("family", evenness=1.8)], day="2026-10-16", hour=3)
        again = r.restart()
        again.ticks(24, [zobs("family", evenness=1.8)], day="2026-10-16", hour=4)
        events = again.tick([zobs("family")], day="2026-10-16", hour=6)
        self.assertEqual(kinds(events), [(START, "uneven:family")])

    def test_junk_in_the_saved_state_gives_a_fresh_start(self):
        for junk in (
            None,
            [],
            "x",
            3,
            {"episodes": [1], "runs": {"z": 5}, "night": {"z": "x"}, "nights": {"z": [1, 2]}},
            {"last_push": {"z": "x"}, "cold_since": 3, "runs": {"z": {"samples": [[1, 2], "x"]}}},
            {"night": {"z": ["d", "a", "b", "c"]}, "nights": {"z": [["d", "x"]]}},
        ):
            r = Run(saved=junk)
            r.ticks(3, [zobs(air=18.6, calling=True)])
            self.assertEqual(r.alerts.active(), [], junk)

    def test_the_snapshot_is_plain_json(self):
        r = Run()
        r.ticks(10, PumpOff.calling(), relay_w=1.6)
        r.ticks(7, [zobs(air=18.6)])
        json.dumps(r.alerts.snapshot())


class Module(unittest.TestCase):
    def test_it_is_pure(self):
        source = (Path(__file__).resolve().parents[1] / "heating_alerts.py").read_text()
        self.assertIsNone(re.search(r"^\s*(import|from)\s+appdaemon", source, re.M))
        self.assertNotIn("open(", source)


if __name__ == "__main__":
    unittest.main()
