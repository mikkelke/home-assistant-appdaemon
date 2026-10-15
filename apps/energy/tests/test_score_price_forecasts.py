# tests/test_score_price_forecasts.py - scripts/score_price_forecasts.py on synthetic data: ranks,
# blocks and regret, the spot footing of every unit and basis, publication and origins, persistence
# across DST, and the adoption rule end to end.
# Run from repo root: python3 -m unittest discover -s apps/energy/tests -q

import io
import json
import math
import os
import random
import sys
import tempfile
import unittest
from contextlib import redirect_stdout
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import score_price_forecasts as sp  # noqa: E402

TABLE = [0.287] * 6 + [0.552] * 11 + [1.348] * 4 + [0.552] * 3
RATE = 7.46


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def line(source, kind, fetched, points, unit="DKK/kWh", basis="spot", **extra):
    return {"v": 1, "run": fetched, "trigger": "test", "fetched_at": fetched, "source": source, "kind": kind,
            "unit": unit, "basis": basis, **extra, "points": [[iso(h), v] for h, v in sorted(points.items())]}


class Synthetic:
    """A tape over `days` origin days: actual spot with a daily curve, a level per day and noise, and
    the all-in price on TABLE; actual lines at 00:05 on the first day and at 13:05 every day (that
    day and the next); forecast lines just after both origins, for D+1..D+5."""

    def __init__(self, days, first=date(2026, 10, 1), seed=7):
        rng = random.Random(seed)
        self.spot = {}
        for offset in range(days + 7):
            level = rng.uniform(0.3, 1.2)
            for hour in sp.day_hours(first + timedelta(days=offset)):
                clock = sp._local(hour).hour
                self.spot[hour] = round(level + 0.3 * math.sin(2 * math.pi * (clock - 5) / 24) + rng.uniform(-0.15, 0.15), 6)
        self.noise = {hour: rng.uniform(-0.3, 0.3) for hour in self.spot}
        self.records = []
        self.actuals(sp.at(first, time(0, 5)), [first])
        for offset in range(days + 6):
            day = first + timedelta(days=offset)
            self.actuals(sp.at(day, time(13, 5)), [day, day + timedelta(days=1)])
        for offset in range(days):
            day = first + timedelta(days=offset)
            for clock in (time(12, 40), time(15, 30)):
                self.forecasts(sp.at(day, clock) + 10, [day + timedelta(days=k) for k in range(1, 6)])

    def all_in(self, hour):
        return round((self.spot[hour] + 0.012) * 1.25 + TABLE[sp._local(hour).hour], 6)

    def actuals(self, fetched, days):
        hours = [hour for day in days for hour in sp.day_hours(day)]
        self.records.append(line("eds_spot", "actual", iso(fetched), {h: self.spot[h] for h in hours}, dkk_per_eur=RATE))
        self.records.append(line("eds_sensor", "actual", iso(fetched), {h: self.all_in(h) for h in hours},
                                 basis="all_in", tariffs_incl_vat=TABLE))

    def forecasts(self, fetched, days):
        hours = [hour for day in days for hour in sp.day_hours(day)]
        at = iso(fetched)
        self.records.append(line("perfect", "forecast", at, {h: self.spot[h] for h in hours}))
        self.records.append(line("noisy", "forecast", at, {h: self.spot[h] + self.noise[h] for h in hours}))
        self.records.append(line("carnot", "forecast", at, {h: self.all_in(h) for h in hours}, basis="all_in",
                                 tariffs_incl_vat=TABLE))
        self.records.append(line("smartere", "forecast", at, {h: self.spot[h] * 1000 / RATE for h in hours}, unit="EUR/MWh"))


def mean_of(samples, source, lead, key):
    return sp._mean(m[key] for _, m, _ in samples[(source, lead)])


class Ranks(unittest.TestCase):
    def test_ties_share_their_mean_rank(self):
        self.assertEqual(sp.ranks([3, 1, 3, 2]), [2.5, 0.0, 2.5, 1.0])

    def test_spearman(self):
        self.assertAlmostEqual(sp.spearman([1, 2, 3, 4], [10, 20, 30, 40]), 1.0)
        self.assertAlmostEqual(sp.spearman([4, 3, 2, 1], [10, 20, 30, 40]), -1.0)
        self.assertAlmostEqual(sp.spearman([1, 1, 2], [1, 2, 3]), 1.5 / math.sqrt(3))
        self.assertIsNone(sp.spearman([1, 1, 1], [1, 2, 3]))


class Blocks(unittest.TestCase):
    def test_cheapest_block_and_its_tie(self):
        self.assertEqual(sp.cheapest_block([3, 1, 1, 1, 3, 3], 3), 1)
        self.assertEqual(sp.cheapest_block([1, 1, 1, 1], 2), 0)

    def test_regret_of_a_backwards_day(self):
        metrics = sp.day_score([1, 1, 1, 0, 0, 0], [0, 0, 0, 1, 1, 1], [0, 0, 0, 1.25, 1.25, 1.25], kwh=1.5, block=3, vat=0.25)
        self.assertAlmostEqual(metrics["regret"], 1.875)
        self.assertAlmostEqual(metrics["mae"], 100.0)
        self.assertAlmostEqual(metrics["bias"], 0.0)
        self.assertAlmostEqual(metrics["rho"], -1.0)

    def test_a_spot_error_smaller_than_the_tariff_step_keeps_the_pick(self):
        metrics = sp.day_score([0.6, 0.6, 0.6, 0.5, 0.5, 0.5], [0.5] * 6, [1, 1, 1, 2, 2, 2])
        self.assertEqual(metrics["regret"], 0.0)
        self.assertAlmostEqual(metrics["bias"], 5.0)


class Footing(unittest.TestCase):
    T0 = sp.at(date(2026, 10, 14), time(12, 40))
    HOUR = sp.at(date(2026, 10, 16), time(18))

    def tape(self, *records):
        return sp.Tape(list(records))

    def test_all_in_backs_out_on_its_own_tariffs(self):
        price = (0.5 + 0.012) * 1.25 + TABLE[18]
        tape = self.tape(line("carnot", "forecast", iso(self.T0), {self.HOUR: price}, basis="all_in", tariffs_incl_vat=TABLE))
        self.assertAlmostEqual(tape.forecasts["carnot"][0][1][self.HOUR], 0.5)

    def test_a_stated_vat_and_addon_are_used(self):
        price = (0.5 + 0.012 + 0.1) * 1.2 + TABLE[18]
        tape = self.tape(line("osf", "forecast", iso(self.T0), {self.HOUR: price}, basis="all_in", tariffs_incl_vat=TABLE,
                              vat=0.2, addon_ex_vat=0.1))
        self.assertAlmostEqual(tape.forecasts["osf"][0][1][self.HOUR], 0.5)

    def test_eur_uses_the_latest_rate_before_the_line_else_the_first(self):
        rates = [line("eds_spot", "actual", iso(self.T0 + offset), {}, dkk_per_eur=rate) for offset, rate in ((0, 7.40), (7200, 7.50))]
        eur = [line("smartere", "forecast", iso(self.T0 + offset), {self.HOUR: 100.0}, unit="EUR/MWh") for offset in (-60, 3600, 9000)]
        tape = self.tape(*rates, *eur)
        self.assertEqual([round(points[self.HOUR], 4) for _, points in tape.forecasts["smartere"]], [0.74, 0.74, 0.75])

    def test_unknown_units_bases_and_tableless_all_in_are_unusable(self):
        tape = self.tape(line("a", "forecast", iso(self.T0), {self.HOUR: 1.0}, unit="SEK/kWh"),
                         line("b", "forecast", iso(self.T0), {self.HOUR: 1.0}, basis="retail"),
                         line("c", "forecast", iso(self.T0), {self.HOUR: 1.0}, basis="all_in"))
        self.assertEqual(dict(tape.unusable), {"a": 1, "b": 1, "c": 1})
        self.assertEqual(dict(tape.forecasts), {})

    def test_actuals_latest_value_wins_first_sighting_counts_and_glitches_drop(self):
        tape = self.tape(line("eds_spot", "actual", iso(self.T0), {self.HOUR: 1.0}),
                         line("eds_spot", "actual", iso(self.T0 + 600), {self.HOUR: 1.1}),
                         line("eds_sensor", "actual", iso(self.T0), {self.HOUR: 109.565, self.HOUR + 3600: 2.0}, basis="all_in"))
        self.assertEqual(tape.spot[self.HOUR], 1.1)
        self.assertEqual(tape.seen[self.HOUR], self.T0)
        self.assertEqual(tape.all_in, {self.HOUR + 3600: 2.0})

    def test_backout_check_against_eds_spot(self):
        tape = self.tape(line("eds_spot", "actual", iso(self.T0), {self.HOUR: 0.5}),
                         line("eds_sensor", "actual", iso(self.T0), {self.HOUR: (0.5 + 0.012) * 1.25 + TABLE[18]},
                              basis="all_in", tariffs_incl_vat=TABLE))
        self.assertEqual(len(tape.backout_errors()), 1)
        self.assertAlmostEqual(tape.backout_errors()[0], 0.0)


class Publication(unittest.TestCase):
    DAY = date(2026, 10, 14)

    def setUp(self):
        hours = sp.day_hours(self.DAY + timedelta(days=1))
        self.tape = sp.Tape([line("eds_spot", "actual", iso(sp.at(self.DAY, time(13, 5))), {h: 1.0 for h in hours[:-1]}),
                             line("eds_spot", "actual", iso(sp.at(self.DAY, time(13, 20))), {h: 1.0 for h in hours}),
                             line("p", "forecast", iso(sp.at(self.DAY, time(6, 45))), {hours[0]: 1.0}),
                             line("p", "forecast", iso(sp.at(self.DAY, time(12, 40, 10))), {hours[0]: 2.0}),
                             line("p", "forecast", iso(sp.at(self.DAY, time(12, 50))), {hours[0]: 3.0})])

    def test_a_day_is_published_once_all_its_hours_were_on_the_tape(self):
        tomorrow = self.DAY + timedelta(days=1)
        self.assertFalse(self.tape.published(tomorrow, sp.at(self.DAY, time(12, 40))))
        self.assertFalse(self.tape.published(tomorrow, sp.at(self.DAY, time(13, 10))))
        self.assertTrue(self.tape.published(tomorrow, sp.at(self.DAY, time(13, 15))))
        self.assertFalse(self.tape.published(self.DAY, sp.at(self.DAY, time(15, 30))))

    def test_an_origin_takes_the_latest_line_within_its_window(self):
        hour = sp.day_hours(self.DAY + timedelta(days=1))[0]
        origin = sp.at(self.DAY, time(12, 40))
        self.assertEqual(self.tape.forecast_at("p", origin, 3 * 3600)[hour], 2.0)
        self.assertEqual(self.tape.forecast_at("p", sp.at(self.DAY, time(9)), 3 * 3600)[hour], 1.0)
        self.assertIsNone(self.tape.forecast_at("p", sp.at(self.DAY, time(10)), 3600))


class Persistence(unittest.TestCase):
    def tape_with(self, day):
        return sp.Tape([line("eds_spot", "actual", "2026-01-01T00:00:00Z",
                             {hour: float(sp._local(hour).hour) for hour in sp.day_hours(day)})])

    def test_the_fall_back_day_repeats_its_two_o_clock(self):
        known, target = date(2026, 10, 24), date(2026, 10, 25)
        values = self.tape_with(known).persistence(known, sp.day_hours(target))
        self.assertEqual(len(values), 25)
        self.assertEqual(values[:5], [0.0, 1.0, 2.0, 2.0, 3.0])

    def test_the_spring_forward_day_skips_two_o_clock(self):
        known, target = date(2026, 3, 28), date(2026, 3, 29)
        values = self.tape_with(known).persistence(known, sp.day_hours(target))
        self.assertEqual(len(values), 23)
        self.assertEqual(values[:4], [0.0, 1.0, 3.0, 4.0])

    def test_a_missing_hour_gives_none(self):
        known = date(2026, 10, 14)
        tape = self.tape_with(known)
        del tape.spot[sp.day_hours(known)[5]]
        self.assertIsNone(tape.persistence(known, sp.day_hours(known + timedelta(days=1))))


class EndToEnd(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.synthetic = Synthetic(32)
        cls.tape = sp.Tape(cls.synthetic.records)
        cls.samples, cls.picks = sp.score(cls.tape)

    def test_every_lead_is_scored_once_per_origin_day(self):
        for source in (sp.PERSISTENCE, "perfect", "noisy", "carnot", "smartere"):
            for lead in (1, 2, 3, 4, 5):
                self.assertEqual(len(self.samples[(source, lead)]), 32, (source, lead))

    def test_a_perfect_forecast_in_any_unit_scores_perfectly(self):
        for source in ("perfect", "carnot", "smartere"):
            for lead in (1, 2, 5):
                self.assertLess(mean_of(self.samples, source, lead, "mae"), 0.001, (source, lead))
                self.assertAlmostEqual(mean_of(self.samples, source, lead, "rho"), 1.0, places=6)
                self.assertLess(mean_of(self.samples, source, lead, "regret"), 1e-4)
            self.assertEqual(self.picks[source], [True] * 32)

    def test_persistence_repeats_the_latest_published_day(self):
        day, metrics, _ = self.samples[(sp.PERSISTENCE, 1)][0]
        hours = sp.day_hours(day)
        previous = [self.synthetic.spot[sp.at(day - timedelta(days=1), sp._local(h).time())] for h in hours]
        actual = [self.synthetic.spot[h] for h in hours]
        all_in = [self.synthetic.all_in(h) for h in hours]
        self.assertEqual(metrics, sp.day_score(previous, actual, all_in))
        self.assertGreater(mean_of(self.samples, sp.PERSISTENCE, 2, "regret"), 0.0)
        self.assertNotIn(sp.PERSISTENCE, self.picks)

    def test_adoption_rule(self):
        for source in ("perfect", "carnot", "smartere"):
            self.assertEqual(sp.verdict(self.samples, source)[0], "ADOPT")
        self.assertGreater(mean_of(self.samples, "noisy", 1, "mae"), 5.0)
        result, parts = sp.verdict(self.samples, "noisy")
        self.assertEqual((result, [lead for lead, _, _, _ in parts]), ("no", [1, 2, 3]))
        few = Synthetic(20)
        self.assertEqual(sp.verdict(sp.score(sp.Tape(few.records))[0], "perfect")[0], "not yet")

    def test_report_and_command_line(self):
        with tempfile.TemporaryDirectory() as folder:
            path = os.path.join(folder, "tape.jsonl")
            with open(path, "w") as f:
                f.write("\n".join(json.dumps(record) for record in self.synthetic.records) + "\n{broken\n")
            out = io.StringIO()
            with redirect_stdout(out):
                self.assertEqual(sp.main([path]), 0)
        text = out.getvalue()
        self.assertIn("1 malformed", text)
        self.assertIn("back-out check", text)
        self.assertRegex(text, r"D\+1\s+persistence\s+32 ")
        self.assertIn("perfect 32/32 (100%)", text)
        self.assertRegex(text, r"perfect\s+D\+1 0\.000 vs \d\.\d{3} \(32 d\).*-> ADOPT")
        self.assertIn("no scored days yet", sp.report(sp.Tape([]), {}, {}))


class ReadTape(unittest.TestCase):
    def test_blank_lines_skip_and_bad_lines_count(self):
        records, malformed = sp.read_tape([
            "", "{broken", '{"fetched_at": "2026-10-01T10:00:00", "points": []}', '{"fetched_at": "2026-10-01T10:00:00Z"}',
            '[1, 2]', '{"fetched_at": "2026-10-01T10:00:00Z", "points": []}',
        ])
        self.assertEqual((len(records), malformed), (1, 4))


if __name__ == "__main__":
    unittest.main()
