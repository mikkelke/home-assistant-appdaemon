# tests/test_coffee_publish.py - the seven entities keep the contract's string encoding and their size caps
# with worst-case content, the example payloads' shapes, the brews' aggregates and recent.json.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import json
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_advice as ca  # noqa: E402
import coffee_beans as cbeans  # noqa: E402
import coffee_curve as cc  # noqa: E402
import coffee_plan as cp  # noqa: E402
import coffee_publish as pub  # noqa: E402

NOW = datetime(2026, 10, 20, 6, 0, tzinfo=timezone.utc).timestamp()
DAY = 86400


def all_strings(test, attrs, key):
    test.assertIsInstance(attrs, dict)
    for name, value in attrs.items():
        test.assertTrue(pub.leaves_ok(value), (key, name, value))
    test.assertEqual(attrs["v"], "1", key)
    test.assertLessEqual(pub.size(attrs), pub.CAPS[key], key)


def long_recipe(name="4 Pour Recipe"):
    pours = [{"volume_ml": 45 + i, "temperature_c": 93.5, "pattern": 3, "flow_rate": 3.5, "pause_s": 15,
              "agitate_before": 1, "agitate_after": 1} for i in range(6)]
    return cp.normalize({"id": "1234567", "name": name, "dose_g": 15.0, "water_ratio": 17.0, "grinder_size": 46.0,
                         "rpm": 90, "cup_type": 1, "pours": pours})


def full_brew(phase="brewing"):
    recipe = long_recipe("A recipe name that is about as long as the app lets one be ok")
    return {"id": "c1792991000", "phase": phase, "source": "tablet", "recipe": recipe, "variant": "usual",
            "bean": {"id": "b1", "name": "Ethiopia Guji Hambela Natural Lot 7"}, "pour": 3, "pours_total": 6,
            "started_ts": NOW, "bloom_ts": NOW + 38, "paused_since_ts": NOW + 100, "ready_ts": NOW + 240,
            "picked_up_ts": NOW + 300, "stopped_ts": NOW + 200, "stop_reason": "home_assistant",
            "start_error": "not_on_home_screen", "who": "kristine", "who_method": "sole_occupancy",
            "notify": "kristine", "push": "sent", "verdict": "on_track", "in_band": "inside",
            "final": {"cup_g": 217.4, "water_ml": 255.0, "total_s": 207, "drawdown_s": 62}}


def record(i, **extra):
    at = datetime.fromtimestamp(NOW - i * DAY / 2, timezone.utc).isoformat(timespec="seconds")
    return {"id": f"c{int(NOW) - i * 43200}", "started_at": at, "ts": at, "outcome": "complete", "source": "tablet",
            "recipe": {"name": "4 Pour Recipe", "grind": 46}, "variant": "trial", "bean": {"id": "b1",
                                                                                         "name": "Kenya Kiambu AA"},
            "dose_g": 15.0, "debited_g": 15.0, "who": "mikkel", "rating": "balanced", "total_s": 207, "cup_g": 217.4,
            "water_ml": 255.0, "drawdown_s": 62, "deltas": {"d_total_s": "+4", "d_cup_g": "-3", "d_drawdown_s": "+2"},
            "in_band": "inside", "trial": "grind_size:46>44", "faults": ["no_water", "dose_water_error"],
            "coverage": 0.98, "first_bloom_at": at, **extra}


class EncodingTests(unittest.TestCase):
    def test_numbers(self):
        self.assertEqual([pub.num(v) for v in (217.4, 0, 3.5, None, True, "x")], ["217", "0", "4", None, None, None])
        self.assertEqual(pub.num(0.98, 2), "0.98")
        self.assertEqual(pub.num(15.0, 1), "15")
        self.assertEqual([pub.power_text(w) for w in (0.9, 2.25, 1043.2, None)], ["0.9", "2.2", "1043", None])

    def test_leaves(self):
        self.assertTrue(pub.leaves_ok("a"))
        self.assertTrue(pub.leaves_ok([{"id": "b1", "name": "x"}]))
        for bad in (0, 1.5, None, True, [1], [{"a": 1}], {"a": "b"}):
            self.assertFalse(pub.leaves_ok(bad), bad)


class StateTests(unittest.TestCase):
    def test_every_phase_with_everything_set_is_strings_under_the_cap(self):
        for phase in ("starting", "start_failed", "grinding", "brewing", "paused", "ready", "picked_up", "stopped"):
            state, attrs = pub.state_entity(full_brew(phase), None, NOW, "working", 1043.2, "connected", NOW,
                                            "no_water", "mikkel", "claudia", "on_track", NOW + 245, "baseline")
            self.assertEqual(state, phase)
            all_strings(self, attrs, "coffee_state")

    def test_the_brewing_example(self):
        brew = full_brew("brewing")
        state, attrs = pub.state_entity(brew, None, NOW, "working", 1043, "connected", NOW, "ok", None, None,
                                        "on_track", NOW + 245, "baseline")
        for key in ("machine", "power_w", "link", "brew_id", "source", "recipe_id", "recipe_name", "variant", "bean_id",
                    "bean_name", "pour", "pours_total", "started_at", "first_bloom_at", "eta", "eta_basis", "verdict",
                    "who", "who_method", "notify"):
            self.assertIn(key, attrs, key)
        self.assertNotIn("fault", attrs)
        self.assertEqual((attrs["pour"], attrs["eta"]), ("3", "2026-10-20T06:04:05+00:00"))
        self.assertNotIn("in_band", attrs)

    def test_paused_hides_the_eta_and_says_since_when(self):
        _, attrs = pub.state_entity(full_brew("paused"), None, NOW, "awake", 2.2, "connected", NOW, "no_water", None,
                                    None, "on_track", NOW + 245, "baseline")
        self.assertNotIn("eta", attrs)
        self.assertEqual((attrs["paused_since"], attrs["fault"]), ("2026-10-20T06:01:40+00:00", "no_water"))

    def test_only_a_real_fault_is_a_fault(self):
        for status in ("ok", "unknown", "unavailable", None):
            _, attrs = pub.state_entity(None, None, NOW, "awake", 2.3, "not_connected", None, status, None, None, None,
                                        None, None)
            self.assertNotIn("fault", attrs, status)

    def test_idle_and_asleep(self):
        state, attrs = pub.state_entity(None, {"id": "c1"}, NOW, "awake", 2.3, "not_connected", None, "ok", "mikkel",
                                        None, None, None, None)
        self.assertEqual((state, attrs["brew_id"], attrs["notify_guess"]), ("idle", "c1", "mikkel"))
        self.assertNotIn("notify", attrs)
        for machine in ("asleep", "off"):
            state, attrs = pub.state_entity(None, None, NOW, machine, 0.9, "not_connected", None, None, None,
                                            "kristine", None, None, None)
            self.assertEqual((state, attrs["machine"], attrs["notify"]), ("asleep", machine, "kristine"))


class LiveTests(unittest.TestCase):
    def test_a_long_brew_stays_under_the_cap_by_stepping(self):
        recipe = long_recipe()
        plan = cp.plan_water(recipe)
        for seconds, expected in ((300, "1"), (700, "2"), (2000, "4"), (4000, "8")):
            curve = cc.Curve(NOW)
            for s in range(seconds):
                curve.sample(NOW + s, 999.9, 2550.0, True)
            curve.pour(NOW, 0)
            curve.pause(NOW + 100)
            curve.resume(NOW + 200)
            state, attrs = pub.live_entity({"id": "c1"}, curve, NOW + seconds, True, NOW + seconds, plan,
                                           cp.plan_pours(recipe))
            all_strings(self, attrs, "coffee_live")
            self.assertEqual((state, attrs["step_s"]), ("live", expected), seconds)

    def test_the_live_example_shape(self):
        curve = cc.Curve(NOW)
        for s, (cup, water) in enumerate([(0, 0), (0, 3.2), (0.3, 6.7), (0.9, 10.1), (1.8, 13.5)]):
            curve.sample(NOW + s, cup, water, True)
        curve.pour(NOW, 0)
        state, attrs = pub.live_entity({"id": "c1792991000"}, curve, NOW + 4, True, NOW + 5, [0, 35, 70, 105, 140],
                                       "0-13:circular:93:45")
        self.assertEqual(state, "live")
        self.assertEqual((attrs["cup"], attrs["water"], attrs["pours"], attrs["step_s"]),
                         ("0,0,3,9,18", "0,32,67,101,135", "0:0", "1"))
        self.assertEqual(attrs["plan_water"], "0,35,70,105,140")
        self.assertEqual(attrs["t0"], "2026-10-20T06:00:00+00:00")
        self.assertNotIn("end_s", attrs)

    def test_stale_ended_and_idle(self):
        curve = cc.Curve(NOW)
        curve.sample(NOW + 3, None, None, False)
        self.assertEqual(pub.live_entity({"id": "c1"}, curve, NOW + 3, False, NOW - 20, [], None)[0], "stale")
        ended = curve.file_data("c1")
        ended["end_s"] = 3
        state, attrs = pub.live_entity(None, None, NOW, False, None, [], None, ended=ended)
        self.assertEqual((state, attrs["brew_id"], attrs["end_s"]), ("ended", "c1", "3"))
        self.assertEqual(pub.live_entity(None, None, NOW, False, None, [], None)[0], "idle")
        grinding = pub.live_entity({"id": "c2"}, None, NOW, True, NOW, [0, 1], "0-1:spiral:93:1")
        self.assertEqual(grinding[0], "live")
        self.assertNotIn("t0", grinding[1])


class OtherEntityTests(unittest.TestCase):
    def test_the_baseline_with_a_full_band(self):
        band = [2550] * 160
        base = {"state": "ready", "id": "bl12345678", "recipe_id": "1234567", "recipe_key": "abcdef1234",
                "bean_id": "b1", "n": 20, "brew_ids": [f"c17929{i:05d}" for i in range(20)], "step_s": 2,
                "total_s_median": 206, "total_s_pm": 4.5, "cup_g_median": 216.2, "cup_g_pm": 3.1,
                "drawdown_s_median": 61, "drawdown_s_pm": 5, "cup_lo": band, "cup_hi": band, "water_lo": band,
                "water_hi": band, "plan_water": [2550] * 320, "plan_pours": "0-13:circular:93:45;" * 6,
                "plan_total_s": 202, "computed_at": "2026-10-20T06:00:00+00:00"}
        state, attrs = pub.baseline_entity(base)
        self.assertEqual(state, "ready")
        all_strings(self, attrs, "coffee_baseline")
        self.assertEqual(attrs["n"], "20")
        self.assertEqual(pub.baseline_entity(None)[0], "none")
        self.assertEqual(pub.baseline_entity({"state": "building", "n": 2, "brew_ids": ["c1", "c2"]})[1]["n"], "2")

    def test_beans_with_many_known_beans(self):
        beans = cbeans.Beans()
        now = datetime.fromtimestamp(NOW, timezone.utc)
        for i in range(80):
            beans.add(250, now, new_name=f"A long bean name from a roaster number {i:02d}")
        state, attrs = pub.beans_entity(beans.view(15.0, now))
        all_strings(self, attrs, "coffee_beans")
        self.assertEqual(state, "20000")
        self.assertEqual(attrs["dose_g"], "15")

    def test_the_beans_example(self):
        beans = cbeans.Beans()
        now = datetime.fromtimestamp(NOW, timezone.utc)
        beans.add(112, now, new_name="Kenya Kiambu")
        state, attrs = pub.beans_entity(beans.view(15.0, now + timedelta(minutes=11)))
        self.assertEqual((state, attrs["bean_id"], attrs["bean_name"], attrs["brews_left"]), ("112", "b1",
                                                                                             "Kenya Kiambu", "7"))
        self.assertNotIn("low", attrs)
        self.assertEqual(attrs["known_beans"], [{"id": "b1", "name": "Kenya Kiambu"}])
        self.assertEqual(pub.beans_entity(cbeans.Beans().view(15.0, now))[0], "unknown")

    def test_brews_cap_and_aggregates(self):
        records = [record(i) for i in range(120)]
        state, attrs = pub.brews_entity(records, NOW, "b1", list(range(0, 2200, 10)), NOW)
        all_strings(self, attrs, "coffee_brews")
        self.assertLessEqual(len(attrs["brews"]), pub.BREWS_MAX)
        self.assertGreater(len(attrs["brews"]), 20)
        self.assertEqual(attrs["brews"][0]["id"], records[0]["id"])
        self.assertEqual(attrs["brews_60d"], "120")
        self.assertEqual(len(attrs["weeks"].split(",")), 9)
        self.assertEqual(len(attrs["hours"].split(",")), 24)
        self.assertEqual(sum(int(n) for n in attrs["hours"].split(",")), 120)
        self.assertEqual(attrs["taste"], "sour:0,balanced:120,bitter:0")
        self.assertEqual(attrs["beans_planned_g_60d"], "1800")
        self.assertEqual(attrs["last_curve"].split(",")[:3], ["0", "40", "80"])
        self.assertEqual(state, "2026-10-20T06:00:00+00:00")

    def test_old_brews_drop_out_but_still_fill_the_weeks(self):
        records = [record(i) for i in range(0, 200, 10)]
        _, attrs = pub.brews_entity(records, NOW, "b1", None, NOW)
        self.assertEqual(attrs["brews_60d"], str(sum(1 for r in records if NOW - int(r["id"][1:]) <= 60 * DAY)))
        self.assertNotIn("last_curve", attrs)

    def test_a_brew_row_leaves_out_what_is_unknown(self):
        row = pub.brew_row({"id": "c1", "started_at": "2026-10-01T06:00:00+00:00", "outcome": "unobserved",
                            "source": "power", "faults": []})
        self.assertEqual(row, {"id": "c1", "at": "2026-10-01T06:00:00+00:00", "outcome": "unobserved",
                               "source": "power"})

    def test_recipes_with_the_usual_first_and_a_cap(self):
        entries = [cp.recipe_entry(long_recipe(f"Recipe {i:02d}") | {}) for i in range(30)]
        for i, entry in enumerate(entries):
            entry["id"] = str(1000 + i)
        usual = {"id": "1015", "name": "Recipe 15"}
        state, attrs = pub.recipes_entity(entries, usual)
        all_strings(self, attrs, "coffee_recipes")
        self.assertEqual((state, attrs["usual_id"], attrs["recipes"][0]["id"]), ("Recipe 15", "1015", "1015"))
        self.assertEqual(pub.recipes_entity([], None)[0], "none")

    def test_advice_with_a_long_thread(self):
        advice = ca.Advice()
        advice.asking([], {"name": "x"}, {"id": "b1", "name": "y"}, {}, "t", 46)
        advice.answered({"action": "try", "to": 44, "confidence": "high", "why": "w" * 600}, "t", "a1")
        for i in range(6):
            advice.reply("a1", "r" * 600, "mikkel", "2026-10-20T06:00:00+00:00")
            advice.answered({"action": "try", "to": 44, "confidence": "high", "why": "w" * 600}, "t", "a1")
        state, attrs = pub.advice_entity(advice.view("b1"))
        all_strings(self, attrs, "coffee_advice")
        self.assertEqual((state, attrs["from"], attrs["to"]), ("suggested", "46", "44"))
        self.assertEqual(pub.advice_entity({"state": "no_agent"}), ("no_agent", pub.finish("coffee_advice", {})))


class RecentTests(unittest.TestCase):
    def test_recent_keeps_sixty_days_under_100_kb_with_the_bands_used(self):
        records = [record(i, baseline_id="bl1" if i % 2 else "bl2") for i in range(150)]
        curves = {r["id"]: {"t0": NOW, "cup": [2170] * 600, "water": [2550] * 600, "pours": [[0, 0]], "end_s": 599}
                  for r in records}
        bands = {"bl1": {"n": 5, "cup_lo": [1]}, "bl2": {"n": 6, "cup_lo": [2]}, "bl3": {"n": 7}}
        doc = pub.recent_doc(records, curves, bands, NOW)
        self.assertLessEqual(len(json.dumps(doc, separators=(",", ":"))), pub.RECENT_CAP)
        self.assertEqual((doc["v"], doc["step_s"]), (1, 2))
        self.assertEqual(doc["brews"][0]["id"], records[0]["id"])
        self.assertEqual(len(doc["brews"][0]["cup"]), 300)
        self.assertEqual(set(doc["bands"]), {"bl1", "bl2"})
        self.assertLess(len(doc["brews"]), 120)

    def test_band_doc(self):
        doc = pub.band_doc({"n": 7, "cup_lo": [1], "cup_hi": [2], "water_lo": [3], "water_hi": [4],
                            "plan_water": [0, 1, 2, 3]})
        self.assertEqual(doc, {"n": 7, "cup_lo": [1], "cup_hi": [2], "water_lo": [3], "water_hi": [4],
                               "plan_water": [0, 2]})


if __name__ == "__main__":
    unittest.main()
