# tests/test_feedback_io.py - the feedback files are written whole or not at all, and a history that cannot
# be read is set aside instead of replaced by an empty one: feedback_io itself, and the three monitors
# that write through it. The ledger HouseUpcoming counts maintenance runs from lives in these files.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import test_washer_restart_survival as trs  # noqa: E402  (installs the appdaemon stub)

import dishwasher_monitor as dwm  # noqa: E402
import dryer_monitor as drm  # noqa: E402
import feedback_io  # noqa: E402

APPLIANCES = Path(feedback_io.__file__).resolve().parent
NOW = datetime(2026, 10, 15, 12, 0, 5, tzinfo=timezone.utc)


def history(*programmes):
    return {"version": 2, "cycles": [{"ts": f"2026-10-0{i + 1}T10:00:00+02:00", "confirmed": p, "duration_min": 200.0}
                                    for i, p in enumerate(programmes)]}


def files(folder):
    return sorted(os.listdir(folder))


class WriteJsonTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feedback_io_")
        self.path = os.path.join(self.tmp, "dishwasher_feedback.json")

    def test_it_writes_the_whole_file_and_leaves_nothing_else_behind(self):
        feedback_io.write_json(self.path, history("eco", "auto"))
        self.assertEqual(json.loads(Path(self.path).read_text()), history("eco", "auto"))
        self.assertEqual(files(self.tmp), ["dishwasher_feedback.json"])

    def test_it_keeps_the_layout_the_monitors_always_wrote(self):
        feedback_io.write_json(self.path, {"version": 2, "cycles": [{"label": "Mørkt/Denim"}]})
        self.assertEqual(Path(self.path).read_text(encoding="utf-8"),
                         json.dumps({"version": 2, "cycles": [{"label": "Mørkt/Denim"}]}, indent=2, ensure_ascii=False))

    def test_it_replaces_an_existing_file(self):
        feedback_io.write_json(self.path, history("eco"))
        feedback_io.write_json(self.path, history("eco", "auto"))
        self.assertEqual(len(json.loads(Path(self.path).read_text())["cycles"]), 2)

    def test_a_dump_that_fails_halfway_leaves_the_old_file_untouched_and_no_tmp(self):
        feedback_io.write_json(self.path, history("eco"))
        before = Path(self.path).read_bytes()
        with self.assertRaises(TypeError):
            feedback_io.write_json(self.path, {"version": 2, "cycles": [{"fine": 1}, {"bad": object()}]})
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual(files(self.tmp), ["dishwasher_feedback.json"])

    def test_a_failed_rename_leaves_the_old_file_untouched_and_no_tmp(self):
        feedback_io.write_json(self.path, history("eco"))
        before = Path(self.path).read_bytes()
        with mock.patch("feedback_io.os.replace", side_effect=OSError("disk gone")):
            with self.assertRaises(OSError):
                feedback_io.write_json(self.path, history("eco", "auto"))
        self.assertEqual(Path(self.path).read_bytes(), before)
        self.assertEqual(files(self.tmp), ["dishwasher_feedback.json"])

    def test_the_new_content_is_staged_in_the_same_directory_and_swapped_in_by_a_rename(self):
        seen = []
        real = os.replace

        def spy(source, target):
            seen.append((os.path.dirname(source), os.path.dirname(target), Path(source).read_text()))
            return real(source, target)

        with mock.patch("feedback_io.os.replace", side_effect=spy):
            feedback_io.write_json(self.path, history("eco"))
        self.assertEqual(len(seen), 1)
        self.assertEqual(seen[0][0], seen[0][1])
        self.assertEqual(json.loads(seen[0][2]), history("eco"))

    def test_a_reader_during_the_write_meets_the_old_file_whole(self):
        feedback_io.write_json(self.path, history("eco"))
        met = []
        real = os.replace

        def spy(source, target):
            met.append(json.loads(Path(target).read_text()))
            return real(source, target)

        with mock.patch("feedback_io.os.replace", side_effect=spy):
            feedback_io.write_json(self.path, history("eco", "auto"))
        self.assertEqual(met, [history("eco")])


class LoadForAppendTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="feedback_io_")
        self.path = os.path.join(self.tmp, "washer_feedback.json")
        self.logs = []

    def log(self, message, level="INFO"):
        self.logs.append((level, message))

    def load(self):
        return feedback_io.load_for_append(self.path, self.log)

    def test_no_file_is_a_new_empty_history(self):
        self.assertEqual(self.load(), {"version": 2, "cycles": []})
        self.assertEqual((files(self.tmp), self.logs), ([], []))

    def test_a_readable_history_comes_back_whole(self):
        Path(self.path).write_text(json.dumps(history("eco", "auto")))
        self.assertEqual(self.load(), history("eco", "auto"))
        self.assertEqual((files(self.tmp), self.logs), (["washer_feedback.json"], []))

    def test_a_history_without_a_cycles_list_gets_one(self):
        Path(self.path).write_text(json.dumps({"version": 2, "migration_version": "1"}))
        self.assertEqual(self.load(), {"version": 2, "migration_version": "1", "cycles": []})
        self.assertEqual(self.logs, [])

    def test_a_truncated_file_is_set_aside_with_its_bytes_and_the_history_starts_empty(self):
        whole = json.dumps(history("eco", "auto", "gentle"), indent=2)
        Path(self.path).write_text(whole[:-40])
        with mock.patch("feedback_io.datetime") as clock:
            clock.now.return_value = NOW
            data = self.load()
        self.assertEqual(data, {"version": 2, "cycles": []})
        aside = os.path.join(self.tmp, "washer_feedback.json.corrupt-20261015T120005Z")
        self.assertEqual(files(self.tmp), ["washer_feedback.json.corrupt-20261015T120005Z"])
        self.assertEqual(Path(aside).read_text(), whole[:-40])
        self.assertEqual([level for level, _ in self.logs], ["WARNING"])
        self.assertIn("washer_feedback.json.corrupt-20261015T120005Z", self.logs[0][1])

    def test_an_empty_file_and_a_file_that_is_no_history_are_set_aside_too(self):
        for content in ("", "[]", "null", '{"cycles": null}', '{"cycles": {"a": 1}}', "{not json"):
            tmp = tempfile.mkdtemp(prefix="feedback_io_")
            path = os.path.join(tmp, "dryer_feedback.json")
            Path(path).write_text(content)
            self.assertEqual(feedback_io.load_for_append(path, self.log), {"version": 2, "cycles": []}, content)
            (aside,) = files(tmp)
            self.assertTrue(aside.startswith("dryer_feedback.json.corrupt-"), content)
            self.assertEqual(Path(tmp, aside).read_text(), content)

    def test_two_files_set_aside_in_the_same_second_keep_their_own_names(self):
        with mock.patch("feedback_io.datetime") as clock:
            clock.now.return_value = NOW
            for content in ("{one", "{two"):
                Path(self.path).write_text(content)
                self.load()
        self.assertEqual(files(self.tmp), ["washer_feedback.json.corrupt-20261015T120005Z",
                                           "washer_feedback.json.corrupt-20261015T120005Z-1"])
        self.assertEqual({Path(self.tmp, name).read_text() for name in files(self.tmp)}, {"{one", "{two"})

    def test_a_file_that_cannot_be_set_aside_is_left_alone_and_says_so(self):
        Path(self.path).write_text("{broken")
        with mock.patch("feedback_io.os.rename", side_effect=OSError("read-only")):
            self.assertIsNone(self.load())
        self.assertEqual(Path(self.path).read_text(), "{broken")
        self.assertEqual([level for level, _ in self.logs], ["WARNING"])
        self.assertIn("not saving the new record", self.logs[0][1])

    def test_it_works_without_a_logger(self):
        Path(self.path).write_text("{broken")
        self.assertEqual(feedback_io.load_for_append(self.path), {"version": 2, "cycles": []})


class DishwasherSaveTests(unittest.TestCase):
    def app(self, tmp):
        app = dwm.DishwasherMonitor.__new__(dwm.DishwasherMonitor)
        app.feedback_file = os.path.join(tmp, "dishwasher_feedback.json")
        app._learned_durations = {}
        app.short_entity = None
        app.detected_short = False
        app.detected_quick_short = False
        app.logs = []
        app.log = lambda message, level="INFO", **kw: app.logs.append((level, message))
        app.get_state = lambda entity, **kw: None
        app._now_utc = lambda: NOW
        return app

    def save(self, app):
        app._save_cycle_feedback(predicted="eco", confirmed="eco", duration_min=230.0, energy_kwh=0.75, max_power_w=1900)

    def test_a_history_that_cannot_be_read_is_kept_and_the_run_starts_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text('{"version": 2, "cycles": [{"ts": "2026-10-0')
            self.save(app)
            aside = [name for name in files(tmp) if ".corrupt-" in name]
            self.assertEqual(len(aside), 1)
            self.assertEqual(Path(tmp, aside[0]).read_text(), '{"version": 2, "cycles": [{"ts": "2026-10-0')
            cycles = json.loads(Path(app.feedback_file).read_text())["cycles"]
            self.assertEqual([c["confirmed"] for c in cycles], ["eco"])
            self.assertTrue(any(level == "WARNING" and aside[0] in message for level, message in app.logs))

    def test_a_history_that_cannot_be_set_aside_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text("{broken")
            with mock.patch("feedback_io.os.rename", side_effect=OSError("read-only")):
                self.save(app)
            self.assertEqual(Path(app.feedback_file).read_text(), "{broken")
            self.assertEqual(app._learned_durations, {})

    def test_a_write_that_fails_leaves_the_history_as_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            feedback_io.write_json(app.feedback_file, history("eco", "auto"))
            before = Path(app.feedback_file).read_bytes()
            with mock.patch("feedback_io.os.replace", side_effect=OSError("disk full")):
                self.save(app)
            self.assertEqual(Path(app.feedback_file).read_bytes(), before)
            self.assertEqual(files(tmp), ["dishwasher_feedback.json"])
            self.assertTrue(any(level == "WARNING" for level, _ in app.logs))

    def test_the_other_writers_go_through_the_same_door(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            feedback_io.write_json(app.feedback_file, history("eco", "auto"))
            before = Path(app.feedback_file).read_bytes()
            with mock.patch("feedback_io.os.replace", side_effect=OSError("disk full")):
                app._mark_last_cycle_emptied()
                app._remove_last_cycle_feedback()
            self.assertEqual(Path(app.feedback_file).read_bytes(), before)
            self.assertEqual(files(tmp), ["dishwasher_feedback.json"])


class DryerSaveTests(unittest.TestCase):
    def app(self, tmp):
        app = drm.DryerMonitor.__new__(drm.DryerMonitor)
        app.feedback_file = os.path.join(tmp, "dryer_feedback.json")
        app._learned_durations = {}
        app.logs = []
        app.log = lambda message, level="INFO", **kw: app.logs.append((level, message))
        app._now_utc = lambda: NOW
        return app

    def save(self, app):
        app._save_cycle_feedback(predicted="ekspres__skabstoert", confirmed="ekspres__skabstoert", duration_min=80.6,
                                 energy_kwh=0.79, max_power_w=893.0)

    def test_a_history_that_cannot_be_read_is_kept_and_the_run_starts_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text("")
            self.save(app)
            aside = [name for name in files(tmp) if ".corrupt-" in name]
            self.assertEqual(len(aside), 1)
            cycles = json.loads(Path(app.feedback_file).read_text())["cycles"]
            self.assertEqual([c["confirmed"] for c in cycles], ["ekspres__skabstoert"])

    def test_a_history_that_cannot_be_set_aside_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text("{broken")
            with mock.patch("feedback_io.os.rename", side_effect=OSError("read-only")):
                self.save(app)
            self.assertEqual(Path(app.feedback_file).read_text(), "{broken")
            self.assertIsNone(app._last_saved_record_ts)

    def test_a_write_that_fails_leaves_the_history_as_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            feedback_io.write_json(app.feedback_file, history("eco", "auto"))
            before = Path(app.feedback_file).read_bytes()
            with mock.patch("feedback_io.os.replace", side_effect=OSError("disk full")):
                self.save(app)
                app._patch_cycle_record(None, {"emptied_ts": "2026-10-15T14:00:00+02:00"})
            self.assertEqual(Path(app.feedback_file).read_bytes(), before)
            self.assertEqual(files(tmp), ["dryer_feedback.json"])


class WasherSaveTests(unittest.TestCase):
    def app(self, tmp):
        app = trs.make_full_init_app(sensor_state=None, helper_state=None, power_watts=0,
                                     extra_args={"feedback_file": os.path.join(tmp, "washer_feedback.json")})
        app.logs = []
        app.log = lambda message, level="INFO", **kw: app.logs.append((level, message))
        return app

    def save(self, app):
        return app._save_cycle_feedback(
            predicted="bomuld", predicted_temperature="60°C", confirmed="bomuld", confirmed_temperature="60°C",
            duration_min=150.0, energy_kwh=1.2, heating_bursts=12, max_power_w=2100.0,
            completion_class="completed", valid_for_learning=True, validation_flags=[])

    def test_a_history_that_cannot_be_read_is_kept_and_the_run_starts_a_new_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text('{"version": 2, "cycles": [')
            self.assertIsNotNone(self.save(app))
            aside = [name for name in files(tmp) if ".corrupt-" in name]
            self.assertEqual(len(aside), 1)
            self.assertEqual(Path(tmp, aside[0]).read_text(), '{"version": 2, "cycles": [')
            self.assertEqual(len(json.loads(Path(app.feedback_file).read_text())["cycles"]), 1)

    def test_a_history_that_cannot_be_set_aside_is_not_overwritten(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            Path(app.feedback_file).write_text("{broken")
            with mock.patch("feedback_io.os.rename", side_effect=OSError("read-only")):
                self.assertIsNone(self.save(app))
            self.assertEqual(Path(app.feedback_file).read_text(), "{broken")

    def test_a_write_that_fails_leaves_the_history_as_it_was(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            self.assertIsNotNone(self.save(app))
            before = Path(app.feedback_file).read_bytes()
            with mock.patch("feedback_io.os.replace", side_effect=OSError("disk full")):
                self.assertIsNone(self.save(app))
                app._remove_last_cycle_feedback()
                app._update_last_feedback_user_confirmed("bomuld", "40°C", "Mikkel")
            self.assertEqual(Path(app.feedback_file).read_bytes(), before)
            self.assertEqual(files(tmp), ["washer_feedback.json"])

    def test_the_first_record_creates_the_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self.app(tmp)
            self.assertIsNotNone(self.save(app))
            self.assertEqual(files(tmp), ["washer_feedback.json"])


class NoDirectWritesLeftTests(unittest.TestCase):
    """A write that truncates the feedback file in place is how a crash leaves half of one."""

    def test_the_three_monitors_open_no_feedback_file_for_writing(self):
        for name in ("washer_monitor.py", "dishwasher_monitor.py", "dryer_monitor.py"):
            source = (APPLIANCES / name).read_text()
            self.assertEqual(re.findall(r'open\([^)\n]*"w"[^)\n]*\)', source), [], name)
            self.assertIn("feedback_io.write_json(", source, name)


if __name__ == "__main__":
    unittest.main()
