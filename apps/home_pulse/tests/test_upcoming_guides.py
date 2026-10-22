# tests/test_upcoming_guides.py - the step-by-step guides on the maintenance rows' sheets: every task and
# Rober2 part has one, short and plain, with the safety note its manual stresses.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests

import sys
import unittest
from pathlib import Path

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "home_pulse"))
sys.path.insert(0, str(APPS / "appliances"))
import upcoming_core as core  # noqa: E402
import upcoming_guides as guides  # noqa: E402
import upcoming_maint as maint  # noqa: E402
import upcoming_runs as runs  # noqa: E402

MIELE = ("dishwasher:hygiene", "dishwasher:filters", "dishwasher:check", "washer:drum", "washer:inlet", "dryer:plinth")
XBLOOM = ("coffee:rinse", "coffee:tank", "coffee:burrs", "coffee:descale")


class CoverageTests(unittest.TestCase):
    def test_every_task_and_every_rober2_part_has_a_guide_and_nothing_else_does(self):
        wanted = {task.key for task in runs.TASKS} | {f"rober2:{part}" for part, _, _ in maint.ROBER2_PARTS}
        self.assertEqual(set(guides.GUIDES), wanted)
        self.assertEqual(set(MIELE) | set(XBLOOM), {task.key for task in runs.TASKS})

    def test_each_guide_is_three_to_six_steps(self):
        for key, guide in guides.GUIDES.items():
            self.assertIn(len(guide.steps), range(3, 7), key)

    def test_the_text_is_plain_ascii_that_a_row_carries_whole(self):
        for key, guide in guides.GUIDES.items():
            for step in guide.steps:
                self.assertEqual(core.clean_text(step, core.STEP_MAX), step, (key, step))
                self.assertTrue(step[0].isupper() and step.endswith("."), (key, step))
            self.assertEqual(core.clean_text(guide.warn, core.STEP_MAX), guide.warn, key)
            self.assertEqual(len(set(guide.steps)), len(guide.steps), key)

    def test_a_row_made_from_a_guide_keeps_every_step_and_the_note(self):
        for key, guide in guides.GUIDES.items():
            row = core.make_row(f"maint:{key}", "maint", "Task", steps=guide.steps, warn=guide.warn, audience="admin")
            self.assertEqual(row["steps"], list(guide.steps), key)
            self.assertEqual(row.get("warn", ""), guide.warn, key)
            self.assertTrue(core.is_valid_row(row), key)


class SafetyNoteTests(unittest.TestCase):
    """What each manual stresses is on the sheet."""

    def warn(self, key):
        return guides.GUIDES[key].warn.lower()

    def test_every_miele_and_xbloom_guide_carries_a_note(self):
        for key in MIELE + XBLOOM:
            self.assertTrue(guides.GUIDES[key].warn, key)

    def test_the_xbloom_notes(self):
        self.assertIn("cool", self.warn("coffee:rinse"))
        self.assertIn("soap", self.warn("coffee:tank"))
        self.assertIn("unplug", self.warn("coffee:burrs"))
        self.assertIn("water on the burrs", self.warn("coffee:burrs"))
        self.assertIn("vinegar", self.warn("coffee:descale"))
        self.assertIn("hot", self.warn("coffee:descale"))

    def test_the_hygiene_run_warns_of_the_hot_water(self):
        self.assertIn("hot", self.warn("dishwasher:hygiene"))

    def test_the_filters_are_never_left_out(self):
        self.assertIn("without the filters", self.warn("dishwasher:filters"))
        self.assertIn("lower spray arm", self.warn("dishwasher:filters"))

    def test_the_general_check_names_what_damages_the_surfaces(self):
        for word in ("scouring", "ammonia", "acid", "chloride", "descaler", "oven"):
            self.assertIn(word, self.warn("dishwasher:check"))

    def test_the_drum_clean_rules_out_vinegar_and_liquid_and_the_inlet_closes_the_tap(self):
        for word in ("vinegar", "liquid", "bleach", "hot"):
            self.assertIn(word, self.warn("washer:drum"))
        self.assertIn("close the water tap", self.warn("washer:inlet"))

    def test_the_dryer_waits_for_its_cool_down_and_never_runs_without_the_plinth_filter(self):
        warn = self.warn("dryer:plinth")
        for phrase in ("unplug", "cool-down", "without the plinth filter", "flap open", "wet filters"):
            self.assertIn(phrase, warn)


class StepContentTests(unittest.TestCase):
    """The steps say what the manual says, in the order it does it."""

    def steps(self, key):
        return " | ".join(guides.GUIDES[key].steps).lower()

    def test_the_hygiene_run_is_intensive_75_and_is_logged_on_the_card(self):
        text = self.steps("dishwasher:hygiene")
        self.assertIn("intensive 75", text)
        self.assertIn("dishwasher card", text)

    def test_the_hygiene_run_is_a_normal_wash_with_a_normal_load_and_is_never_run_empty(self):
        steps = guides.GUIDES["dishwasher:hygiene"].steps
        self.assertIn("normal load", steps[0].lower())
        self.assertIn("pots and pans", steps[0].lower())
        self.assertNotIn("empty", self.steps("dishwasher:hygiene"))
        self.assertIn("once a month", steps[1])

    def test_the_filters_come_out_before_they_are_rinsed_and_go_back_with_the_arm(self):
        steps = guides.GUIDES["dishwasher:filters"].steps
        order = [next(i for i, step in enumerate(steps) if word in step.lower())
                 for word in ("lower spray arm", "filter combination", "rinse the filters", "refit")]
        self.assertEqual(order, sorted(order))
        self.assertIn("micro filter", self.steps("dishwasher:filters"))
        self.assertIn("lock symbol", self.steps("dishwasher:filters"))

    def test_the_general_check_covers_the_seal_the_door_sides_the_panel_and_the_front(self):
        text = self.steps("dishwasher:check")
        for word in ("door seal", "sides of the door", "control panel", "front"):
            self.assertIn(word, text)

    def test_the_drawer_seal_and_drain_filter_are_cleaned_before_the_hot_wash_and_the_wash_is_logged(self):
        steps = guides.GUIDES["washer:drum"].steps
        self.assertIn("drawer", steps[0].lower())
        for word in ("siphon", "door seal", "drain filter", "oxygen bleach"):
            self.assertIn(word, self.steps("washer:drum"))
        self.assertIn("bomuld 90", steps[-1].lower())
        self.assertIn("drum empty", steps[-1].lower())
        self.assertIn("drum clean", steps[-1].lower())

    def test_the_inlet_filters_are_two_and_both_go_back(self):
        text = self.steps("washer:inlet")
        for word in ("hose end", "machine end", "both filters", "tap"):
            self.assertIn(word, text)

    def test_the_dryer_covers_the_plinth_filter_and_the_air_path(self):
        text = self.steps("dryer:plinth")
        for word in ("plinth flap", "plinth filter", "lint filters", "long nozzle", "filter lamp"):
            self.assertIn(word, text)

    def test_the_descale_runs_twice_once_with_the_descaler_and_once_to_rinse(self):
        text = self.steps("coffee:descale")
        for word in ("descaling powder", "descaling program", "clean water", "again"):
            self.assertIn(word, text)

    def test_the_burrs_are_brushed_with_the_machine_unplugged(self):
        steps = guides.GUIDES["coffee:burrs"].steps
        self.assertIn("unplug", steps[0].lower())
        self.assertIn("brush", steps[1].lower())

    def test_each_rober2_part_ends_with_resetting_its_counter(self):
        for part, _, _ in maint.ROBER2_PARTS:
            self.assertIn("reset", guides.GUIDES[f"rober2:{part}"].steps[-1].lower(), part)
            self.assertIn("roborock app", guides.GUIDES[f"rober2:{part}"].steps[-1].lower(), part)


if __name__ == "__main__":
    unittest.main()
