"""A door peek (Emptied reverted to Unemptied) followed by an AppDaemon reload must not let the
Unemptied poll count the peek's own, already-handled door-open edge as a missed emptying."""
import os
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import cycle_store as cs  # noqa: E402
from test_dishwasher_audit_2026_09 import (  # noqa: E402
    DOOR_SENSOR,
    make_boot_app,
    make_live_app,
    scheduled_callbacks_named,
    seed_store,
)


class PeekThenReload(unittest.TestCase):
    def _boot_after_peek(self, tmp, door_history):
        now = datetime.now(timezone.utc)
        app, _ = make_boot_app(tmp, helper_state=None, power_w="0")
        seed_store(tmp, {
            "state": "Unemptied",
            "state_since": cs.format_utc(now - timedelta(hours=2)),
            "door_handled_until": cs.format_utc(now - timedelta(minutes=59, seconds=58)),
        })
        app._history[DOOR_SENSOR] = [
            {"state": state, "last_changed": cs.format_utc(now - ago)} for state, ago in door_history
        ]
        app.initialize()
        self.assertEqual(app.state, "Unemptied")
        poll_cbs = scheduled_callbacks_named(app, "_poll_power")
        self.assertEqual(len(poll_cbs), 1)
        poll_cbs[0]({})
        return app

    def test_the_peeks_own_open_edge_is_not_a_missed_emptying(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._boot_after_peek(tmp, [
                ("on", timedelta(hours=1)),
                ("off", timedelta(minutes=59, seconds=59)),
            ])
            self.assertEqual(app.state, "Unemptied")

    def test_a_real_open_missed_after_the_peek_still_empties(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = self._boot_after_peek(tmp, [
                ("on", timedelta(hours=1)),
                ("off", timedelta(minutes=59, seconds=59)),
                ("on", timedelta(minutes=10)),
            ])
            self.assertEqual(app.state, "Emptied")


class PeekRevertRecordsTheHandledDoor(unittest.TestCase):
    def test_revert_stamps_door_handled_until_into_the_store_payload(self):
        now = datetime(2026, 11, 21, 10, 11, 10, tzinfo=timezone.utc)
        app = make_live_app(now, state="Emptied", start_minutes_ago=90, power_w="1.5")
        app._revert_emptied_to_unemptied()
        self.assertEqual(app.door_handled_until, now)
        payload = {k: f() for k, f in app._cycle_store_field_map().items()}
        self.assertEqual(payload["door_handled_until"], cs.format_utc(now))


if __name__ == "__main__":
    unittest.main()
