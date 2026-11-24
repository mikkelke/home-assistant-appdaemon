"""The fill-log seed: what it adds, that a second run adds nothing, and that the app loads what it wrote; no SSH."""

import contextlib
import importlib.util
import io
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

SPEC = importlib.util.spec_from_file_location("coffee_seed_fills", Path(__file__).parents[1] / "coffee_seed_fills.py")
seed = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(seed)

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "apps" / "coffee"))
import coffee_tank  # noqa: E402


def state(**tank):
    return {"version": 1, "saved_at": "2026-11-23T16:49:02+00:00", "beans": {"bags": []},
            "tank": {"filled_at": "2026-11-23T07:13:07+00:00", "used_ml": 863.1, "dry": False, **tank}}


class SeedTests(unittest.TestCase):
    def test_every_tap_is_added_in_time_order_and_nothing_else_changes(self):
        before = state()
        seeded, added = seed.seed(before)
        self.assertEqual(len(added), len(seed.TAPS))
        self.assertEqual([f["at"] for f in seeded["tank"]["fills"]], sorted(at for at, _ in seed.TAPS))
        self.assertTrue(all(f["source"] == "tap" for f in seeded["tank"]["fills"]))
        self.assertEqual({k: v for k, v in seeded["tank"].items() if k != "fills"}, before["tank"])
        self.assertEqual(seeded["beans"], before["beans"])
        self.assertNotIn("fills", before["tank"])

    def test_the_last_tap_is_the_fill_the_tank_has(self):
        seeded, _ = seed.seed(state())
        self.assertEqual(seeded["tank"]["fills"][-1]["at"], seeded["tank"]["filled_at"])

    def test_a_second_run_adds_nothing(self):
        once, _ = seed.seed(state())
        twice, added = seed.seed(once)
        self.assertEqual((added, twice), ([], once))

    def test_a_fill_the_app_logged_already_is_left_as_it_is(self):
        live = [{"at": seed.TAPS[3][0], "source": "tap", "user": "claudia"},
                {"at": "2026-11-24T08:00:00+00:00", "source": "tap", "user": None}]
        seeded, added = seed.seed(state(fills=live))
        self.assertEqual(len(added), len(seed.TAPS) - 1)
        fills = seeded["tank"]["fills"]
        self.assertEqual(len(fills), len(seed.TAPS) + 1)
        self.assertEqual(fills[-1], live[1])
        self.assertEqual([f for f in fills if f["at"] == seed.TAPS[3][0]], [live[0]])

    def test_the_app_loads_what_was_seeded(self):
        seeded, _ = seed.seed(state())
        tank = coffee_tank.Tank(json.loads(json.dumps(seeded["tank"])))
        self.assertEqual(tank.fills, seeded["tank"]["fills"])
        self.assertEqual(tank.fills[0], {"at": "2026-10-25T11:23:09+00:00", "source": "tap", "user": "mikkel"})
        self.assertEqual(tank.fills[-1], {"at": "2026-11-23T07:13:07+00:00", "source": "tap", "user": None})

    def run_main(self, path, *flags):
        with patch.object(sys, "argv", ["coffee_seed_fills.py", "--state", str(path), *flags]), \
                contextlib.redirect_stdout(io.StringIO()) as out:
            seed.main()
        return out.getvalue()

    def test_a_local_file_is_only_written_with_apply_and_only_when_something_is_new(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "coffee_state.json"
            path.write_text(json.dumps(state()))
            self.assertIn(f"{len(seed.TAPS)} to add", self.run_main(path))
            self.assertNotIn("fills", json.loads(path.read_text())["tank"])
            self.run_main(path, "--apply")
            written = path.read_text()
            self.assertEqual(len(json.loads(written)["tank"]["fills"]), len(seed.TAPS))
            self.assertIn("0 to add", self.run_main(path, "--apply"))
            self.assertEqual(path.read_text(), written)

    def test_a_file_without_a_tank_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "other.json"
            path.write_text(json.dumps({"version": 1}))
            with self.assertRaises(SystemExit):
                self.run_main(path, "--apply")


if __name__ == "__main__":
    unittest.main()
