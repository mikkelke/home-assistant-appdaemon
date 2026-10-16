# tests/test_ui_usage_report.py - scripts/ui_usage_report.py on a synthetic recorder: taps per control
# and person, the room of a tap, test rows, app.open per day across the Copenhagen midnight, users that
# are unknown or missing, and that the database is only ever read.
# Run from repo root: python3 -m unittest discover -s apps/home_pulse/tests -q

import hashlib
import io
import json
import sqlite3
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "scripts"))

import ui_usage_report as report  # noqa: E402

MIKKEL = "ff5112b8da6f4a28b3c3dfaf758f372d"
KRISTINE = "5bee78148825439488b70f92565e6c26"
CLAUDIA = "c93da3250c0442849b0bfe746980f7ce"
STRANGER = "0123456789abcdef0123456789abcdef"
NOW = datetime(2026, 10, 16, 8, 0, tzinfo=timezone.utc).timestamp()


def utc(day, hour, minute=0):
    return datetime(2026, 10, day, hour, minute, tzinfo=timezone.utc).timestamp()


class Recorder:
    """A recorder with the three tables the report reads, in the columns HA gives them."""

    def __init__(self, folder):
        self.db = str(Path(folder) / "recorder.db")
        self.auth = str(Path(folder) / "auth")
        self.con = sqlite3.connect(self.db)
        self.con.executescript(
            "CREATE TABLE event_types (event_type_id INTEGER PRIMARY KEY, event_type VARCHAR(64));"
            "CREATE TABLE event_data (data_id INTEGER PRIMARY KEY, shared_data TEXT);"
            "CREATE TABLE events (event_id INTEGER PRIMARY KEY, event_type_id INTEGER, data_id INTEGER,"
            " time_fired_ts FLOAT, context_user_id_bin BLOB);"
            "INSERT INTO event_types VALUES (1, 'state_reported'), (2, 'dashboard_ui_tap');"
        )
        self.users({MIKKEL: "Mikkel", KRISTINE: "Kristine", CLAUDIA: "Claudia"})

    def users(self, names):
        data = {"users": [{"id": uid, "name": name} for uid, name in names.items()], "credentials": [{"data": {"secret": "x"}}]}
        Path(self.auth).write_text(json.dumps({"version": 1, "data": data}), encoding="utf-8")

    def tap(self, epoch, user, ui, detail="", raw=None, event_type=2):
        data_id = self.con.execute("INSERT INTO event_data (shared_data) VALUES (?)", (raw or json.dumps({"ui": ui, "detail": detail}),)).lastrowid
        self.con.execute(
            "INSERT INTO events (event_type_id, data_id, time_fired_ts, context_user_id_bin) VALUES (?, ?, ?, ?)",
            (event_type, data_id, epoch, bytes.fromhex(user) if user else None),
        )
        self.con.commit()

    def run(self, *args, db=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = report.main([*args, "--db", db or self.db, "--auth", self.auth], now=NOW)
        return code, out.getvalue(), err.getvalue()


def rows(text):
    """The report's lines as lists of cells, to compare without caring about padding."""
    return [line.split() for line in text.splitlines()]


class UiUsageReportTest(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.rec = Recorder(self.folder.name)
        self.addCleanup(self.rec.con.close)

    def taps(self):
        return report.read_taps(self.rec.con, 0)

    def test_counts_per_control_and_person(self):
        for user, ui in [(MIKKEL, "qa.media"), (MIKKEL, "qa.media"), (KRISTINE, "qa.transit"), (KRISTINE, "qa.media"), (CLAUDIA, "qa.timeline")]:
            self.rec.tap(utc(15, 10), user, ui)
        code, out, _ = self.rec.run("30")
        self.assertEqual(code, 0)
        table = rows(out)
        self.assertIn(["control", "Claudia", "Kristine", "Mikkel", "Total"], table)
        self.assertIn(["qa.media", "-", "1", "2", "3"], table)
        self.assertIn(["qa.transit", "-", "1", "-", "1"], table)
        self.assertIn(["qa.timeline", "1", "-", "-", "1"], table)
        self.assertIn(["Total", "1", "2", "2", "5"], table)
        # The busiest control comes first.
        self.assertLess(out.index("qa.media"), out.index("qa.transit"))

    def test_detail_splits_a_control_only_when_asked(self):
        self.rec.tap(utc(15, 10), KRISTINE, "home.room", "kitchen")
        self.rec.tap(utc(15, 11), KRISTINE, "home.room", "kitchen")
        self.rec.tap(utc(15, 12), KRISTINE, "home.room", "bathroom")
        _, plain, _ = self.rec.run("30")
        self.assertIn(["home.room", "3", "3"], rows(plain))
        _, split, _ = self.rec.run("30", "--detail")
        self.assertIn(["home.room", "[kitchen]", "2", "2"], rows(split))
        self.assertIn(["home.room", "[bathroom]", "1", "1"], rows(split))

    def test_only_the_last_days_count(self):
        self.rec.tap(utc(1, 10), MIKKEL, "qa.media")
        self.rec.tap(utc(15, 10), MIKKEL, "qa.transit")
        _, out, _ = self.rec.run("7")
        self.assertIn("qa.transit", out)
        self.assertNotIn("qa.media", out)

    def test_test_rows_are_left_out_unless_asked_for(self):
        self.rec.tap(utc(16, 7), MIKKEL, "test.live_check", "report")
        self.rec.tap(utc(16, 7), MIKKEL, "qa.media")
        _, out, _ = self.rec.run("30")
        self.assertNotIn("test.live_check", out)
        self.assertIn("Left out: 1 test rows", out)
        _, everything, _ = self.rec.run("30", "--all")
        self.assertIn("test.live_check", everything)
        self.assertNotIn("Left out", everything)

    def test_app_open_has_its_own_section_with_a_row_per_day(self):
        # 23:30 UTC on the 14th is 01:30 on the 15th in Copenhagen (summer time until 25.10).
        self.rec.tap(utc(14, 23, 30), KRISTINE, "app.open", "home")
        self.rec.tap(utc(15, 9), KRISTINE, "app.open", "room=kitchen")
        self.rec.tap(utc(15, 9, 30), MIKKEL, "app.open", "home")
        self.rec.tap(utc(16, 5), MIKKEL, "app.open", "home")
        _, out, _ = self.rec.run("30")
        controls, opens = out.split("Dashboard opens")
        self.assertNotIn("app.open", controls)
        table = rows(opens)
        self.assertIn(["day", "Kristine", "Mikkel", "Total"], table)
        self.assertIn(["16.10", "-", "1", "1"], table)
        self.assertIn(["15.10", "2", "1", "3"], table)
        self.assertIn(["Total", "2", "2", "4"], table)
        self.assertNotIn("14.10", opens)

    def test_a_day_without_opens_is_a_row_of_dashes(self):
        self.rec.tap(utc(14, 9), MIKKEL, "app.open", "home")
        self.rec.tap(utc(16, 5), MIKKEL, "app.open", "home")
        _, out, _ = self.rec.run("30")
        self.assertIn(["15.10", "-", "-"], rows(out))

    def test_an_empty_recorder_says_so(self):
        self.rec.con.execute("DELETE FROM event_types WHERE event_type = 'dashboard_ui_tap'")
        self.rec.con.commit()
        code, out, _ = self.rec.run("30")
        self.assertEqual(code, 0)
        self.assertIn("No taps yet.", out)
        self.assertIn("No opens yet.", out)

    def test_unknown_and_missing_users_and_unreadable_data_do_not_stop_the_report(self):
        self.rec.tap(utc(15, 10), STRANGER, "qa.media")
        self.rec.tap(utc(15, 10), None, "qa.media")
        self.rec.tap(utc(15, 10), MIKKEL, "ignored", raw="{not json")
        self.rec.tap(utc(15, 10), MIKKEL, "ignored", raw="[1, 2]")
        self.rec.tap(utc(15, 10), MIKKEL, "ignored", raw=json.dumps({"ui": "home.room", "detail": 7}))
        _, out, _ = self.rec.run("30", "--detail")
        table = rows(out)
        self.assertIn(["control", "(none)", "?01234567", "Mikkel", "Total"], table)
        self.assertIn(["qa.media", "1", "1", "-", "2"], table)
        self.assertIn(["home.room", "[7]", "-", "-", "1", "1"], table)
        self.assertIn(["(no", "ui)", "-", "-", "2", "2"], table)

    def test_other_events_are_not_taps(self):
        self.rec.tap(utc(15, 10), MIKKEL, "qa.media", event_type=1)
        _, out, _ = self.rec.run("30")
        self.assertIn("No taps yet.", out)

    def test_the_recorder_is_only_read(self):
        self.rec.tap(utc(15, 10), MIKKEL, "qa.media")
        self.rec.con.close()
        before = hashlib.sha256(Path(self.rec.db).read_bytes()).hexdigest()
        code, _, _ = self.rec.run("30", "--detail", "--all")
        self.assertEqual(code, 0)
        self.assertEqual(hashlib.sha256(Path(self.rec.db).read_bytes()).hexdigest(), before)

    def test_an_unreadable_recorder_is_an_error_not_a_traceback(self):
        code, out, err = self.rec.run("30", db=str(Path(self.folder.name) / "missing.db"))
        self.assertEqual(code, 1)
        self.assertEqual(out, "")
        self.assertIn("Cannot read", err)

    def test_only_user_names_are_taken_from_the_auth_file(self):
        self.assertEqual(report.load_users(self.rec.auth), {MIKKEL: "Mikkel", KRISTINE: "Kristine", CLAUDIA: "Claudia"})


if __name__ == "__main__":
    unittest.main()
