"""FamilyRoomLights keeps a person's sit-in lamps on across an app restart while someone sleeps.

Incident 2026-10-30 07:02-07:05: Mikkel brightened the dining lamps while Claudia slept, which
latched normal lighting for the session in memory only. HA restarted, the app re-initialised
with the latch cleared, and the next PIR evaluation chose island-only and switched the dining
lamps off. The stateless fallback (non-island lamps on -> preserve) never ran because it was
only computed while the island was off, and the island was on.
"""

from __future__ import annotations

import unittest
from datetime import timedelta

import test_family_room_sleep_latch_race as base

DINING = "light.dining_room_lights"


def make_restarted_app(dining_on):
    states = base.base_states(base.SESSION_START - timedelta(hours=1))
    states[base.ISLAND] = {"state": "on"}
    states[DINING] = {"state": "on" if dining_on else "off"}
    app = base.make_app(states, presence_session_started_at=base.SESSION_START.timestamp())
    app._manual_bright_watch = {DINING}
    app.light_map["all"] = [base.ISLAND, base.COUNTER, DINING]
    return app


class SitInLampsSurviveARestart(unittest.TestCase):
    def test_dining_on_with_island_on_is_preserved_and_latched(self):
        app = make_restarted_app(dining_on=True)
        _context, action = base.decide(app)
        self.assertEqual(action["action"], "preserve_current_state")
        self.assertTrue(app._sleep_activated_during_presence)
        base.assert_no_errors(self, app)

    def test_island_alone_still_reduces_to_island_only(self):
        app = make_restarted_app(dining_on=False)
        _context, action = base.decide(app)
        self.assertEqual(action["action"], "turn_on_island_only")
        self.assertFalse(app._sleep_activated_during_presence)
        base.assert_no_errors(self, app)


if __name__ == "__main__":
    unittest.main()
