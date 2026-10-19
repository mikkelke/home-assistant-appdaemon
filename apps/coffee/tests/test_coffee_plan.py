# tests/test_coffee_plan.py - recipes in both of the integration's shapes, the plan they make, the key
# that makes brews comparable, one-off overrides and the coffee_recipes entry.
# Run from repo root: python3 -m unittest discover -s apps/coffee/tests

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import coffee_plan as cp  # noqa: E402


def pour(ml, pause, pattern=3, temp=93.0, flow=3.0, after=2):
    return {"volume_ml": ml, "temperature_c": temp, "pattern": pattern, "flow_rate": flow, "pause_s": pause,
            "agitate_before": 2, "agitate_after": after}


# The household's usual as a share-link import: grinder_size, rpm and water_ratio as the 1:N denominator.
USUAL_SHARED = {"id": "1234567", "name": "4 Pour Recipe", "dose_g": 15.0, "water_ratio": 17.0, "grinder_size": 46.0,
                "grinder_size_enabled": 1, "rpm": 90, "pour_count": 4, "cup_type": 1, "cup_type_name": "XPOD",
                "pours": [pour(45, 15, after=1), pour(80, 15, after=1), pour(65, 12), pour(65, 5, pattern=1)]}
# A recipe built in Home Assistant: grind_size, grinder_speed_rpm, ratio "1:N", water_ratio as total ml.
BUILT = {"id": "a1b2", "name": "Morning", "dose_g": 18.0, "ratio": "1:16", "water_ratio": 288.0, "grind_size": 50,
         "grinder_speed_rpm": 100, "pour_count": 2, "cup_type": 2,
         "pours": [pour(48, 20, pattern=2, flow=3.5), pour(240, 0, pattern=2, flow=3.5)]}


# The usual as HA's library holds it after the import: ratio "1:17", grind_size and grinder_speed_rpm added.
USUAL_STORED = {"id": "81696", "name": "4 Pour Recipe", "dose_g": 15.0, "water_ratio": 17.0, "grinder_size": 50.0,
                "grinder_size_enabled": 1, "rpm": 120, "pour_count": 4, "cup_type": 2, "bypass_water_enabled": 2,
                "ratio": "1:17", "grind_size": 50.0, "grinder_speed_rpm": 120,
                "pours": [{"name": "Bloom", "volume_ml": 45.0, "temperature_c": 93.0, "pattern": 3, "flow_rate": 3.0,
                           "pause_s": 15, "agitate_before": 2, "agitate_after": 1},
                          {"name": "Pour 2", "volume_ml": 80.0, "temperature_c": 93.0, "pattern": 3, "flow_rate": 3.5,
                           "pause_s": 15, "agitate_before": 2, "agitate_after": 1},
                          {"name": "Pour 3", "volume_ml": 65.0, "temperature_c": 93.0, "pattern": 3, "flow_rate": 3.5,
                           "pause_s": 12, "agitate_before": 2, "agitate_after": 2},
                          {"name": "Pour 4", "volume_ml": 65.0, "temperature_c": 93.0, "pattern": 1, "flow_rate": 3.5,
                           "pause_s": 5, "agitate_before": 2, "agitate_after": 2}]}


class StoredUsualTests(unittest.TestCase):
    def test_the_usual_as_the_library_holds_it(self):
        r = cp.normalize(USUAL_STORED)
        self.assertEqual((r["dose_g"], r["ratio"], r["grind"], r["rpm"], r["cup_type"], r["total_ml"]),
                         (15.0, 17.0, 50, 120, 2, 255.0))
        self.assertEqual(cp.plan_pours(r), "0-15:circular:93:45;30-53:circular:93:80;68-86:circular:93:65;"
                                           "98-117:centered:93:65")
        self.assertEqual(cp.plan_total_s(r), 192)
        self.assertEqual(cp.recipe_entry(r)["cup"], "Omni dripper")
        self.assertEqual(cp.recipe_key(r), cp.recipe_key(cp.normalize({**USUAL_STORED, "ratio": None,
                                                                       "grind_size": None,
                                                                       "grinder_speed_rpm": None})))


class NormalizeTests(unittest.TestCase):
    def test_a_share_link_recipe(self):
        r = cp.normalize(USUAL_SHARED)
        self.assertEqual((r["id"], r["name"], r["dose_g"], r["ratio"], r["grind"], r["rpm"], r["cup_type"]),
                         ("1234567", "4 Pour Recipe", 15.0, 17.0, 46, 90, 1))
        self.assertEqual(r["total_ml"], 255.0)
        self.assertEqual([p["pattern"] for p in r["pours"]], ["circular", "circular", "circular", "centered"])
        self.assertEqual(r["pours"][0]["agitate"], [False, True])

    def test_a_recipe_built_in_home_assistant(self):
        r = cp.normalize(BUILT)
        self.assertEqual((r["ratio"], r["grind"], r["rpm"], r["cup_type"], r["total_ml"]), (16.0, 50, 100, 2, 288.0))
        self.assertEqual(r["pours"][1]["pattern"], "spiral")

    def test_total_water_without_a_ratio_gives_the_ratio(self):
        r = cp.normalize({**BUILT, "ratio": None})
        self.assertEqual(r["ratio"], 16.0)

    def test_junk_is_no_recipe(self):
        for junk in (None, {}, {"name": "x"}, {"name": "x", "dose_g": 15, "pours": []}, "recipe", {"dose_g": 15}):
            self.assertIsNone(cp.normalize(junk), junk)


class PlanTests(unittest.TestCase):
    def setUp(self):
        self.usual = cp.normalize(USUAL_SHARED)

    def test_the_timeline_is_each_pour_then_its_pause(self):
        steps = cp.timeline(self.usual)
        self.assertEqual([(round(a, 1), round(b, 1)) for a, b, _ in steps],
                         [(0.0, 15.0), (30.0, 56.7), (71.7, 93.3), (105.3, 127.0)])
        self.assertAlmostEqual(cp.plan_end_s(self.usual), 127.0, places=0)
        self.assertEqual(cp.plan_total_s(self.usual), 127 + cp.DRAWDOWN_GUESS_S)

    def test_plan_water_rises_through_each_pour_and_holds_in_its_pause(self):
        water = cp.plan_water(self.usual)
        self.assertEqual(len(water), 128)
        self.assertEqual(water[0], 0)
        self.assertEqual(water[15], 450)
        self.assertEqual(water[20], 450)
        self.assertEqual(water[-1], 2550)
        self.assertTrue(all(b >= a for a, b in zip(water, water[1:])))

    def test_plan_pours(self):
        self.assertEqual(cp.plan_pours(self.usual),
                         "0-15:circular:93:45;30-57:circular:93:80;72-93:circular:93:65;105-127:centered:93:65")


class KeyTests(unittest.TestCase):
    def test_the_same_recipe_in_either_shape_has_one_key(self):
        a = cp.normalize(USUAL_SHARED)
        b = cp.normalize({**USUAL_SHARED, "grinder_size": None, "grind_size": 46, "rpm": None,
                          "grinder_speed_rpm": 90, "ratio": "1:17", "name": "Renamed", "id": "9"})
        self.assertEqual(cp.recipe_key(a), cp.recipe_key(b))

    def test_any_brew_field_changes_the_key(self):
        base = cp.recipe_key(cp.normalize(USUAL_SHARED))
        changed = [{**USUAL_SHARED, "grinder_size": 44.0}, {**USUAL_SHARED, "rpm": 100},
                   {**USUAL_SHARED, "cup_type": 2},
                   {**USUAL_SHARED, "pours": USUAL_SHARED["pours"][:3] + [pour(65, 6, pattern=1)]}]
        for recipe in changed:
            self.assertNotEqual(cp.recipe_key(cp.normalize(recipe)), base)


class OverrideTests(unittest.TestCase):
    def test_a_grind_override_changes_only_the_grind(self):
        usual = cp.normalize(USUAL_SHARED)
        trial = cp.with_overrides(usual, grind=44)
        self.assertEqual(trial["grind"], 44)
        self.assertEqual(trial["pours"], usual["pours"])
        self.assertEqual(usual["grind"], 46)

    def test_dose_and_ratio_scale_the_pours(self):
        built = cp.normalize(BUILT)
        tweak = cp.with_overrides(built, dose=16, ratio=16)
        self.assertEqual((tweak["dose_g"], tweak["total_ml"]), (16.0, 256.0))
        self.assertAlmostEqual(tweak["pours"][0]["ml"], 42.7, places=1)


class EntryTests(unittest.TestCase):
    def test_the_recipes_entry_is_strings(self):
        entry = cp.recipe_entry(cp.normalize(USUAL_SHARED))
        self.assertEqual(entry, {"id": "1234567", "name": "4 Pour Recipe", "dose_g": "15", "ratio": "17", "grind": "46",
                                 "rpm": "90", "cup": "xPod", "total_ml": "255", "plan_s": "202",
                                 "pours": "45/93/3/circular/15;80/93/3/circular/15;65/93/3/circular/12;"
                                          "65/93/3/centered/5"})

    def test_lookups(self):
        recipes = [cp.normalize(USUAL_SHARED), cp.normalize(BUILT)]
        self.assertEqual(cp.by_id(recipes, 1234567)["name"], "4 Pour Recipe")
        self.assertEqual(cp.by_name(recipes, "Morning")["id"], "a1b2")
        self.assertIsNone(cp.by_id(recipes, None))
        self.assertIsNone(cp.by_name(recipes, "Nope"))


if __name__ == "__main__":
    unittest.main()
