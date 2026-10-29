# tests/test_washer_tail_eta.py - the Bomuld tail ETA: the Running publishes show the end the plug's strong spins give,
# driven through the real WasherMonitor (trs.make_full_init_app: real initialize(), restore, ticks and transitions; only
# the AppDaemon surface is faked) with the plug's reads fed into its PlugPoller ring.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub

from test_washer_plug import tail_reads
from washer_plug_fixture import FakeClock

NOW = trs.NOW
BASE = 10_000.0                         # the plug clock's reading at second 0 of the series, when app.now is NOW
START = NOW - timedelta(minutes=100)    # the cycle is 100 min old at second 0; Bomuld 30°C is 159 min long
CONFIRM = "input_select.washer_confirmed_programme"
TEMPERATURE = "input_select.washer_temperature"

# Spins 21 and 17 min apart, the last one under the final-spin length: the spins put the end at second 4000, 4120 and 4000.
SPINS = ((1000, 90), (2260, 120), (3280, 300))
READS = tail_reads(*SPINS, after_s=700)


def running_app(label="Bomuld"):
    """A Running cycle the user has given a programme, with the plug's clock under the test's control."""
    payload = trs.make_store_payload(state="Running", start_time=START, cycle_id="cycle-tail-eta",
                                     detected_programme="bomuld", detected_temperature="30°C")
    app = trs.make_full_init_app(
        sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=NOW,
        extra_args={"confirm_entity": CONFIRM, "temperature_entity": TEMPERATURE},
    )
    app.states[CONFIRM] = label
    app.states[TEMPERATURE] = "30°C"
    app.programme_confirmed_by_user = True
    clock = FakeClock(BASE)
    app._plug.clock = clock
    app._plug._last_ok = BASE
    return app, clock


def feed(app, clock, t):
    """Move both clocks to second t of the series and append the reads up to it that the plug's ring lacks."""
    ring = app._plug._ring
    fed = ring[-1][0] - BASE if ring else -1.0
    for ts, w in READS:
        if fed < ts <= t:
            ring.append((BASE + ts, w))
            app._plug._last_ok = BASE + ts
            app._plug._peak = max(app._plug._peak, w)
    clock.t = BASE + t
    app.now = NOW + timedelta(seconds=t)


def tick(app, clock, t):
    """One finish tick at second t; the attributes it published."""
    feed(app, clock, t)
    app._check_energy_finish({})
    return trs.last_publish(app)["attributes"]


def local(app, seconds):
    """Local HH:MM of `seconds` into the series."""
    return app._strftime_local(NOW + timedelta(seconds=seconds))


def tail_logs(app):
    return [m for m in trs.log_messages(app, "INFO") if m.startswith("Tail ETA")]


class RunningTickPublishesTheTailEta(unittest.TestCase):
    def setUp(self):
        self.app, self.clock = running_app()

    def test_before_a_spin_is_known_the_programme_duration_gives_the_end(self):
        a = tick(self.app, self.clock, 60)
        self.assertEqual(a["estimated_end_time"], self.app._strftime_local(START + timedelta(minutes=159)))
        self.assertEqual(a["estimated_remaining_min"], 58)
        self.assertEqual(a["progress_pct"], 64)
        self.assertEqual(tail_logs(self.app), [])

    def test_each_spin_moves_the_published_end_to_the_one_it_gives(self):
        tick(self.app, self.clock, 60)
        held = (self.app.expected_dur_at_start, self.app._guard_bar_class)
        for t, end_s, remaining, pct in ((1040, 4000, 49, 70), (2300, 4120, 30, 82), (3320, 4000, 11, 93)):
            a = tick(self.app, self.clock, t)
            with self.subTest(second=t):
                self.assertEqual(a["estimated_end_time"], local(self.app, end_s))
                self.assertEqual(a["estimated_remaining_min"], remaining)
                self.assertEqual(a["progress_pct"], pct)
                self.assertEqual(a["programme_duration_min"], 159)
                self.assertEqual(a["expected_dur_at_start"], 159)
        self.assertEqual((self.app.expected_dur_at_start, self.app._guard_bar_class), held)
        self.assertEqual(self.app.state, "Running")

    def test_the_spins_outlive_their_reads_in_the_ring(self):
        for t in (1040, 2300, 3320):
            tick(self.app, self.clock, t)
        a = tick(self.app, self.clock, 3900)
        self.assertGreater(self.app._plug.snapshot()[0][0], BASE + 1088)      # the first spin has left the ring
        self.assertEqual(a["estimated_end_time"], local(self.app, 4000))
        self.assertEqual((a["estimated_remaining_min"], a["progress_pct"]), (2, 99))

    def test_a_wash_that_runs_past_the_end_shows_none_left_and_a_full_bar(self):
        for t in (1040, 2300, 3320, 4100):
            a = tick(self.app, self.clock, t)
        self.assertEqual(a["estimated_end_time"], local(self.app, 4000))
        self.assertEqual((a["estimated_remaining_min"], a["progress_pct"]), ("0", 100))

    def test_a_programme_chosen_mid_wash_publishes_it_at_once(self):
        feed(self.app, self.clock, 1040)
        self.app._push_running_eta_attributes()
        a = trs.last_publish(self.app)["attributes"]
        self.assertEqual(a["estimated_end_time"], local(self.app, 4000))
        self.assertEqual((a["estimated_remaining_min"], a["progress_pct"], a["programme_duration_min"]), (49, 70, 159))

    def test_the_delayed_start_wait_still_blanks_the_eta(self):
        tick(self.app, self.clock, 1040)
        self.app.observed_heating = False
        self.app._delay_waiting = True
        a = tick(self.app, self.clock, 1070)
        self.assertEqual((a["estimated_end_time"], a["estimated_remaining_min"], a["progress_pct"],
                          a["programme_duration_min"]), ("", None, 0, None))


class OtherProgrammesKeepTheStaticEta(unittest.TestCase):
    def test_eco_ticks_and_pushes_use_its_duration_from_the_start(self):
        app, clock = running_app("ECO")
        for t in (60, 1040, 2300, 3320):
            a = tick(app, clock, t)
            dur = a["programme_duration_min"]
            elapsed = (NOW + timedelta(seconds=t) - START).total_seconds() / 60
            with self.subTest(second=t):
                self.assertEqual(a["estimated_end_time"], app._strftime_local(START + timedelta(minutes=dur)))
                self.assertEqual(a["estimated_remaining_min"], app._falsy_drop_safe(max(0, round(dur - elapsed))))
                self.assertEqual(a["progress_pct"], min(100, max(0, round(100 * elapsed / dur))))
        app._push_running_eta_attributes()
        pushed = trs.last_publish(app)["attributes"]
        self.assertEqual(pushed["estimated_end_time"], app._strftime_local(START + timedelta(minutes=dur)))
        self.assertEqual(tail_logs(app), [])

    def test_a_programme_known_as_bomuld_only_late_still_has_the_spins(self):
        """The ring no longer holds the heating or the first spin by the time the selector says Bomuld; the tracker was
        fed all along."""
        app, clock = running_app("ECO")
        for t in (60, 1040, 2300, 3320):
            tick(app, clock, t)
        app.states[CONFIRM] = "Bomuld"
        a = tick(app, clock, 3900)
        self.assertEqual(a["estimated_end_time"], local(app, 4000))
        self.assertEqual(a["programme_duration_min"], 159)


class TailEtaLog(unittest.TestCase):
    def test_logged_when_it_appears_and_when_it_moves_a_minute_or_more(self):
        app, clock = running_app()
        for t in (60, 1040, 1070, 1100, 2300, 2330, 3320):
            tick(app, clock, t)
        self.assertEqual(tail_logs(app), [
            f"Tail ETA: spin at {local(app, 1000)} -> ETA {local(app, 4000)}",
            f"Tail ETA: spin at {local(app, 2260)} -> ETA {local(app, 4120)}",
            f"Tail ETA: spin at {local(app, 3280)} -> ETA {local(app, 4000)}",
        ])

    def test_a_move_under_a_minute_is_not_logged(self):
        app, clock = running_app()
        feed(app, clock, 1040)
        eta = app._tail_eta_utc("bomuld", app.now)
        self.assertEqual(len(tail_logs(app)), 1)
        app._tail_eta_logged = eta + timedelta(seconds=59)
        app._tail_eta_utc("bomuld", app.now)
        self.assertEqual(len(tail_logs(app)), 1)
        app._tail_eta_logged = eta + timedelta(seconds=60)
        app._tail_eta_utc("bomuld", app.now)
        self.assertEqual(len(tail_logs(app)), 2)


class CycleBoundariesStartAFreshTracker(unittest.TestCase):
    def test_a_new_cycle_does_not_inherit_the_spins(self):
        app, clock = running_app()
        tick(app, clock, 1040)
        old = app._tail
        self.assertTrue(old.spins)
        app._begin_running_cycle()
        self.assertIsNot(app._tail, old)
        self.assertEqual((app._tail.spins, app._tail.last_heat, app._tail.fed), ([], None, None))
        self.assertIsNone(app._tail_eta_utc("bomuld", app.now))

    def test_a_recovered_cycle_does_not_inherit_the_spins(self):
        app, clock = running_app()
        tick(app, clock, 1040)
        old = app._tail
        self.assertTrue(old.spins)
        app.state = "Unemptied"
        app.states[app.state_entity] = "Unemptied"
        app.attrs_store.setdefault(app.state_entity, {})["run_time_minutes"] = 120
        app._recover_from_false_unemptied(60.0)
        self.assertEqual(app.state, "Running")
        self.assertIsNot(app._tail, old)
        self.assertEqual((app._tail.spins, app._tail.last_heat, app._tail.fed), ([], None, None))


if __name__ == "__main__":
    unittest.main()
