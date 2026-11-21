"""MaTwinWatch: restart MA after a 5 min continuous twin mismatch, never for unavailable twins."""

import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import ma_twin_watch as mtw  # noqa: E402

RAW, TWIN = "media_player.kitchen_2", "media_player.kitchen"
RAW2, TWIN2 = "media_player.bedroom_2", "media_player.bedroom"
T0 = 1_800_000_000.0


class Rule(unittest.TestCase):
    def test_unavailable_and_unknown_twins_never_count(self):
        for twin in ("unavailable", "unknown", None):
            self.assertFalse(mtw.mismatched("playing", twin))
        for twin in ("idle", "paused", "on", "off"):
            self.assertTrue(mtw.mismatched("playing", twin))
        self.assertFalse(mtw.mismatched("playing", "playing"))
        self.assertFalse(mtw.mismatched("paused", "idle"))

    def test_clock_keeps_earliest_start_across_twin_state_changes(self):
        since = mtw.track({}, {RAW: ("playing", None, "idle", None)}, T0)
        since = mtw.track(since, {RAW: ("playing", None, "paused", None)}, T0 + 200)
        self.assertEqual(since, {RAW: T0})

    def test_any_mirrored_reading_resets_the_clock(self):
        since = mtw.track({}, {RAW: ("playing", None, "idle", None)}, T0)
        since = mtw.track(since, {RAW: ("playing", None, "playing", None)}, T0 + 100)
        self.assertEqual(since, {})
        since = mtw.track(since, {RAW: ("playing", None, "idle", None)}, T0 + 130)
        self.assertEqual(mtw.longest(since, T0 + 400), (RAW, 270))

    def test_unavailable_breaks_the_continuous_window(self):
        since = mtw.track({}, {RAW: ("playing", None, "idle", None)}, T0)
        since = mtw.track(since, {RAW: ("playing", None, "unavailable", None)}, T0 + 200)
        self.assertEqual(since, {})

    def test_new_mismatch_starts_at_later_last_changed(self):
        obs = {RAW: ("playing", "2027-01-15T08:00:00+00:00", "idle", "2027-01-15T08:01:00+00:00")}
        start = mtw.track({}, obs, T0 + 10**8)[RAW]
        self.assertEqual(start, mtw._ts("2027-01-15T08:01:00+00:00"))


CFG = {"hold_s": 300, "cooldown_s": 1800, "max_per_day": 3}


def st(**kw):
    s = {"restarts": [], "gave_up": False, "verify_at": None}
    s.update(kw)
    return s


class Decide(unittest.TestCase):
    def test_restart_only_after_five_minutes(self):
        self.assertIsNone(mtw.decide(st(), T0, 299, False, CFG))
        self.assertEqual(mtw.decide(st(), T0, 300, False, CFG), "restart")

    def test_mismatch_again_within_cooldown_is_persisted(self):
        s = st(restarts=[T0])
        self.assertEqual(mtw.decide(s, T0 + 400, 300, False, CFG), "persisted")
        self.assertEqual(mtw.decide(s, T0 + 1800, 300, False, CFG), "restart")

    def test_verify_window(self):
        s = st(restarts=[T0], verify_at=T0 + 600)
        self.assertIsNone(mtw.decide(s, T0 + 599, 0, False, CFG))
        self.assertEqual(mtw.decide(s, T0 + 600, 0, True, CFG), "verified")
        self.assertEqual(mtw.decide(s, T0 + 600, 0, False, CFG), "verify_failed")

    def test_daily_cap(self):
        s = st(restarts=[T0, T0 + 2000, T0 + 4000])
        self.assertIsNone(mtw.decide(s, T0 + 6000, 400, False, CFG))
        self.assertEqual(mtw.decide(s, T0 + mtw.DAY_S + 1, 400, False, CFG), "restart")

    def test_gave_up_waits_for_mirror(self):
        s = st(restarts=[T0], gave_up=True)
        self.assertIsNone(mtw.decide(s, T0 + 9000, 900, False, CFG))
        self.assertEqual(mtw.decide(s, T0 + 9000, 0, True, CFG), "rearm")


class App(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        d = Path(self.tmp.name)
        (d / "req").mkdir()
        self.req = d / "req" / "ma-restart.request"
        self.states = {RAW: "playing", TWIN: "idle", RAW2: "idle", TWIN2: "idle"}
        app = mtw.MaTwinWatch.__new__(mtw.MaTwinWatch)
        app.args = {"pairs": {RAW: TWIN, RAW2: TWIN2}, "request_file": str(self.req),
                    "state_file": str(d / "state.json")}
        app.get_state = lambda e, attribute=None: (
            "Kitchen" if attribute == "friendly_name" else {"state": self.states.get(e), "last_changed": None})
        app.get_app = MagicMock()
        app.run_every = MagicMock()
        app.log = MagicMock()
        app.fire_event = MagicMock()
        app.create_task = MagicMock(side_effect=lambda coro: coro.close())
        self.now = T0
        self.clock = patch.object(mtw.time, "time", lambda: self.now)
        self.clock.start()
        app.initialize()
        self.app = app

    def tearDown(self):
        self.clock.stop()
        self.tmp.cleanup()

    def run_for(self, seconds, step=30):
        end = self.now + seconds
        while self.now < end:
            self.app._check({})
            self.now += step

    def host_consumes(self):
        if self.req.exists():
            self.req.unlink()

    def pushes(self):
        return self.app.create_task.call_count

    def test_freeze_restarts_once_then_verifies(self):
        self.run_for(290)
        self.assertFalse(self.req.exists())
        self.run_for(30)
        self.assertTrue(self.req.exists())
        self.assertEqual(self.pushes(), 1)
        self.app.fire_event.assert_called_once()
        self.assertEqual(self.app.fire_event.call_args.kwargs["audience"], "admin")
        self.host_consumes()
        self.states[TWIN] = "unavailable"
        self.run_for(300)
        self.states[TWIN] = "playing"
        self.run_for(400)
        self.assertIsNone(self.app.state["verify_at"])
        self.assertFalse(self.app.state["gave_up"])
        self.assertEqual(self.pushes(), 1)
        self.assertEqual(len(self.app.state["restarts"]), 1)

    def test_unavailable_twin_never_restarts(self):
        self.states[TWIN] = "unavailable"
        self.run_for(3600)
        self.assertFalse(self.req.exists())
        self.assertEqual(self.pushes(), 0)

    def test_mismatch_persisting_after_restart_pushes_and_stops(self):
        self.run_for(330)
        self.host_consumes()
        self.run_for(3 * 3600)
        self.assertTrue(self.app.state["gave_up"])
        self.assertEqual(len(self.app.state["restarts"]), 1)
        self.assertFalse(self.req.exists())
        self.assertEqual(self.pushes(), 2)
        self.states[TWIN] = "playing"
        self.run_for(60)
        self.assertFalse(self.app.state["gave_up"])

    def test_twin_still_unavailable_at_verify_gives_up(self):
        self.run_for(330)
        self.host_consumes()
        self.states[TWIN] = "unavailable"
        self.run_for(1200)
        self.assertTrue(self.app.state["gave_up"])
        self.assertEqual(self.pushes(), 2)

    def test_host_not_consuming_gives_up_and_clears_request(self):
        self.run_for(330)
        self.assertTrue(self.req.exists())
        self.run_for(120)
        self.assertFalse(self.req.exists())
        self.assertTrue(self.app.state["gave_up"])
        self.assertEqual(self.app.state["restarts"], [])
        self.assertEqual(self.pushes(), 2)

    def test_cooldown_and_history_survive_reload(self):
        self.run_for(330)
        self.host_consumes()
        self.app.initialize()
        self.assertEqual(len(self.app.state["restarts"]), 1)
        self.assertIsNotNone(self.app.state["verify_at"])


if __name__ == "__main__":
    unittest.main()
