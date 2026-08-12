"""BedroomBlindOwner - the one writer of cover.bedroom_blind.

What this file pins, in order of importance:

1. The precedence matrix END TO END: requests in, arbitration through
   blind_arbiter, service calls (or refusals) out.
2. The unified manual notion: a wall-press request, an uncommanded move on the
   cover, and a dashboard move (person's context user id) all become the SAME
   manual hold - and this app's own writes (or another app's fallback writes,
   recognised by the AppDaemon user id) never do.
3. Restart survival: claims incl. the manual hold reload from the state file,
   and an AppDaemon restart never moves the blind.
4. The one-time adoption of an in-flight manual pause from bedroom_solar_shade's
   pre-owner state file (whose schema stays untouched).

POSITIONS ARE IN THE DEVICE'S FRAME (100 = covering the window) - see the banner
in bedroom_blind_control.yaml.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The faked-AppDaemon surface and the owner import live in the shared harness
# (owner_harness.py) so the wakeup yield tests can drive a REAL owner too.
from owner_harness import (  # noqa: E402
    AD_UID, NOW, _fresh_path, make_owner, positions_commanded, report, settle,
)


class PrecedenceMatrixEndToEnd(unittest.TestCase):
    """manual > vent > wake > shade, driven through request() -> service calls."""

    def test_shade_alone_moves_the_blind(self):
        app = make_owner()
        res = app.request("shade", 55, reason="blocking sun heat")
        self.assertTrue(res["granted"])
        self.assertTrue(res["moved"])
        self.assertEqual(positions_commanded(app), [55])

    # Start position 20: every target below differs from it by more than the
    # tolerance, so a granted request always shows up as a service call.
    ORDER = [("shade", 92), ("wake", 38), ("vent", 38), ("manual", 100)]

    def test_every_lower_rank_loses_to_every_higher_one(self):
        for i, (high, high_pos) in enumerate(self.ORDER):
            for low, low_pos in self.ORDER[:i]:
                with self.subTest(high=high, low=low):
                    app = make_owner(position=20)
                    self.assertTrue(app.request(high, high_pos)["granted"])
                    res = app.request(low, low_pos)
                    self.assertFalse(res["granted"])
                    self.assertEqual(res["winner"], high)
                    # the refused claim must not have produced a second write
                    self.assertEqual(len(positions_commanded(app)), 1)

    def test_every_higher_rank_beats_a_standing_lower_one(self):
        for i, (high, high_pos) in enumerate(self.ORDER):
            for low, low_pos in self.ORDER[:i]:
                with self.subTest(high=high, low=low):
                    app = make_owner(position=20)
                    app.request(low, low_pos)
                    res = app.request(high, high_pos)
                    self.assertTrue(res["granted"])
                    self.assertEqual(positions_commanded(app)[-1], high_pos)

    def test_a_refused_request_reports_who_won(self):
        app = make_owner()
        app.request("manual", 100, reason="wall press")
        res = app.request("shade", 92, reason="hot")
        self.assertFalse(res["granted"])
        self.assertEqual(res["winner"], "manual")
        self.assertIsNotNone(res["manual_until"])
        self.assertTrue(any("outranked by manual" in l for l in app.log_lines), app.log_lines)

    def test_expired_claim_stops_blocking(self):
        app = make_owner()
        app.request("wake", 38)  # ttl 20 min by default
        app._now = app._now + timedelta(minutes=21)
        res = app.request("shade", 92)
        self.assertTrue(res["granted"])
        self.assertEqual(positions_commanded(app), [38, 92])

    def test_already_there_is_idempotent(self):
        app = make_owner(position=100)
        res = app.request("manual", 100, reason="wall press at the night position")
        self.assertTrue(res["granted"])   # the claim (and hold) still lands
        self.assertFalse(res["moved"])    # but the motor is not churned
        self.assertEqual(positions_commanded(app), [])
        self.assertTrue(app.manual_hold_active())

    def test_in_flight_travel_does_not_swallow_a_new_command(self):
        """Wall press seconds after the wake open: the cover still reads ~95 (close
        to the close target) but the motor is heading for 38 - the manual command
        must still be issued, exactly as the pre-owner direct write would have."""
        app = make_owner(position=100)
        app.request("wake", 38)
        report(app, 95)  # barely moved yet
        res = app.request("manual", 100, reason="wall press")
        self.assertTrue(res["moved"])
        self.assertEqual(positions_commanded(app), [38, 100])

    def test_unknown_source_raises_so_callers_fall_back(self):
        app = make_owner()
        with self.assertRaises(ValueError):
            app.request("gremlin", 50)

    def test_withdraw_removes_the_claim_but_never_moves(self):
        app = make_owner()
        app.request("wake", 38)
        app.withdraw("wake")
        self.assertEqual(positions_commanded(app), [38])
        res = app.request("shade", 92)
        self.assertTrue(res["granted"])

    def test_dry_run_never_writes(self):
        app = make_owner(args={"dry_run": True})
        res = app.request("shade", 55, reason="hot")
        self.assertTrue(res["granted"])
        self.assertFalse(res["moved"])
        self.assertEqual(positions_commanded(app), [])
        self.assertTrue(any("DRY-RUN" in l for l in app.log_lines))


class UncommandedMoveRegistersManual(unittest.TestCase):
    """A move the owner didn't order is a human at the blind - one episode, one
    hold, however many position reports the travel emits (solar shade's machine)."""

    def test_uncommanded_travel_becomes_one_manual_hold(self):
        app = make_owner(position=38)
        for pos in (47, 56, 65, 74, 83, 92, 100):
            report(app, pos)
        settle(app)
        self.assertTrue(app.manual_hold_active())
        manual = app._claims["manual"]
        self.assertEqual(manual["position"], 100)
        moves = [l for l in app.log_lines if "Manual blind move" in l]
        self.assertEqual(len(moves), 1, app.log_lines)
        self.assertIn("38% -> 100%", moves[0])

    def test_manual_hold_then_blocks_automation(self):
        app = make_owner(position=38)
        report(app, 80)
        settle(app)
        res = app.request("shade", 55, reason="hot")
        self.assertFalse(res["granted"])
        self.assertEqual(positions_commanded(app), [])

    def test_hold_is_persisted_on_the_first_report_not_only_at_settle(self):
        # An AppDaemon restart mid-travel must still know a manual move happened.
        app = make_owner(position=38)
        report(app, 60)
        with open(app.state_file) as f:
            on_disk = json.load(f)
        self.assertIsNotNone(on_disk["claims"].get("manual", {}).get("until"))

    def test_jitter_within_tolerance_is_not_a_move(self):
        app = make_owner(position=38)
        report(app, 40)
        self.assertFalse(app.manual_hold_active())
        self.assertEqual([s for s in app.scheduled if s[0] == app._motor_settled], [])

    def test_hold_runs_from_the_end_of_the_travel(self):
        app = make_owner(position=38)
        for pos in (60, 80, 100):
            report(app, pos)
        settle(app)
        expected = app._now + timedelta(minutes=120)
        self.assertEqual(app.manual_hold_until(), expected)


class OwnWriteVsHumanWrite(unittest.TestCase):
    """The family_room_lights discrimination, applied to the blind."""

    def test_own_command_travel_is_never_manual(self):
        app = make_owner(position=100)
        app.request("wake", 38, reason="morning open")
        for pos in (90, 70, 45, 38):
            report(app, pos)  # context-less reports while OUR command travels
        settle(app)
        self.assertFalse(app.manual_hold_active())
        self.assertEqual(app._baseline, 38)

    def test_appdaemon_context_write_is_adopted_not_manual(self):
        """Another app's fail-safe direct write (owner alive but its request path
        failed) carries the AppDaemon user id - ecosystem, not a hand."""
        app = make_owner(position=100)
        for pos in (80, 50, 38):
            report(app, pos, ctx=AD_UID)
        settle(app)
        self.assertFalse(app.manual_hold_active())
        self.assertEqual(app._baseline, 38)
        self.assertTrue(any("adopted" in l.lower() for l in app.log_lines), app.log_lines)

    def test_person_context_is_manual_even_during_own_travel(self):
        """A dashboard move mid-wake-open: the person's user id trumps the pending
        expectation window."""
        app = make_owner(position=100)
        app.request("wake", 38)
        report(app, 90)                     # our travel
        report(app, 70, ctx="a-person-id")  # human grabs the dashboard slider
        report(app, 65)
        settle(app)
        self.assertTrue(app.manual_hold_active())
        self.assertEqual(app._claims["manual"]["position"], 65)

    def test_own_travel_stopped_off_target_is_manual(self):
        """Hand on the physical remote mid-travel: our command, but the motor
        settles nowhere near our target - automation holds off."""
        app = make_owner(position=100)
        app.request("wake", 38)
        report(app, 90)
        report(app, 85)
        settle(app)  # silence, far from 38
        self.assertTrue(app.manual_hold_active())
        self.assertTrue(any("own command interrupted" in l for l in app.log_lines), app.log_lines)


class RestartSurvival(unittest.TestCase):
    def test_claims_and_manual_hold_reload_and_nothing_moves(self):
        state_file = _fresh_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))
        before = make_owner(state_file=state_file)
        before.request("wake", 38, reason="morning open")
        before.request("manual", 100, reason="wall press")

        after = make_owner(state_file=state_file, now=NOW + timedelta(minutes=5), position=100)
        self.assertTrue(after.manual_hold_active())
        self.assertEqual(set(after._claims), {"wake", "manual"})
        self.assertEqual(after._claims["manual"]["position"], 100)
        # An AppDaemon restart must never move the blind.
        self.assertEqual(positions_commanded(after), [])

    def test_expired_hold_does_not_resurrect_after_restart(self):
        state_file = _fresh_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))
        before = make_owner(state_file=state_file)
        before.request("manual", 100, reason="wall press")

        after = make_owner(state_file=state_file, now=NOW + timedelta(hours=3), position=100)
        self.assertFalse(after.manual_hold_active())
        res = after.request("shade", 55)
        self.assertTrue(res["granted"])

    def test_pending_own_command_survives_so_its_travel_is_not_misread(self):
        """Restart mid-own-travel: the finishing reports must not be classified as
        a manual move just because the process forgot it issued the command."""
        state_file = _fresh_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))
        before = make_owner(state_file=state_file)
        before.request("wake", 38)

        after = make_owner(state_file=state_file, now=NOW + timedelta(seconds=20), position=70)
        for pos in (55, 42, 38):
            report(after, pos)
        settle(after)
        self.assertFalse(after.manual_hold_active())
        self.assertEqual(after._baseline, 38)


class ShadeStateFileAdoption(unittest.TestCase):
    """The deploy that introduces the owner must not drop an in-flight manual pause
    recorded by bedroom_solar_shade. Its file is read once, never written."""

    def _shade_file(self, override_until):
        fd, path = tempfile.mkstemp(suffix=".json")
        os.close(fd)
        with open(path, "w") as f:
            json.dump({"last_cmd": 55, "override_until": override_until}, f)
        self.addCleanup(lambda: os.path.exists(path) and os.remove(path))
        return path

    def test_live_pause_is_adopted_as_a_positionless_veto(self):
        until = (NOW + timedelta(minutes=45)).isoformat()
        app = make_owner(args={"shade_state_file": self._shade_file(until)})
        self.assertTrue(app.manual_hold_active())
        self.assertIsNone(app._claims["manual"]["position"])
        # A veto blocks without proposing: shading is refused, nothing moves.
        res = app.request("shade", 92)
        self.assertFalse(res["granted"])
        self.assertEqual(positions_commanded(app), [])
        # The shade's own file is untouched (schema stays what its tests pin).
        with open(app.shade_state_file) as f:
            self.assertEqual(json.load(f), {"last_cmd": 55, "override_until": until})

    def test_expired_pause_is_not_adopted(self):
        until = (NOW - timedelta(minutes=5)).isoformat()
        app = make_owner(args={"shade_state_file": self._shade_file(until)})
        self.assertFalse(app.manual_hold_active())

    def test_own_persisted_hold_wins_over_the_shade_file(self):
        state_file = _fresh_path()
        self.addCleanup(lambda: os.path.exists(state_file) and os.remove(state_file))
        before = make_owner(state_file=state_file)
        before.request("manual", 100)
        shade = self._shade_file((NOW + timedelta(minutes=200)).isoformat())
        after = make_owner(
            state_file=state_file, args={"shade_state_file": shade},
            now=NOW + timedelta(minutes=5), position=100,
        )
        self.assertEqual(after._claims["manual"]["position"], 100)  # not the veto


class StatusEntity(unittest.TestCase):
    """sensor.bedroom_blind_owner - FIXED CONTRACT with the dashboard:
    state is the winning source key (or "idle"); attributes are position (number,
    null at idle), manual_pause_until (ISO string or "" - empty string, never
    null, because AD 4.5.13 drops some falsy attrs), claims (list of
    {"source", "position"} for active NON-winning requests), reason (one human
    sentence), friendly_name."""

    def _last_publish(self, app):
        entity, kwargs = app.set_state_calls[-1]
        self.assertEqual(entity, "sensor.bedroom_blind_owner")
        self.assertTrue(kwargs.get("replace"), "must publish with replace=True")
        return kwargs

    def test_winner_and_claims_are_published(self):
        app = make_owner()
        app.request("shade", 92, reason="hot")
        app.request("wake", 38, reason="morning open")
        pub = self._last_publish(app)
        self.assertEqual(pub["state"], "wake")
        a = pub["attributes"]
        self.assertEqual(a["position"], 38)
        # claims = active NON-winning requests only, as a list
        self.assertEqual(a["claims"], [{"source": "shade", "position": 92}])
        self.assertEqual(a["manual_pause_until"], "")
        self.assertEqual(a["reason"], "morning open")
        self.assertEqual(a["friendly_name"], "Bedroom blind owner")

    def test_manual_hold_is_visible(self):
        app = make_owner(position=20)
        app.request("wake", 38, reason="morning open")
        app.request("manual", 100, reason="wall press")
        pub = self._last_publish(app)
        self.assertEqual(pub["state"], "manual")
        a = pub["attributes"]
        self.assertEqual(a["position"], 100)
        # ISO-8601 timestamp string while the pause is active
        self.assertEqual(
            a["manual_pause_until"],
            (app.get_now() + timedelta(minutes=120)).isoformat(),
        )
        self.assertEqual(a["claims"], [{"source": "wake", "position": 38}])
        self.assertIn("holding until", a["reason"])

    def test_idle_when_nothing_claims(self):
        app = make_owner()
        pub = self._last_publish(app)
        self.assertEqual(pub["state"], "idle")
        a = pub["attributes"]
        self.assertIsNone(a["position"])
        self.assertEqual(a["manual_pause_until"], "")
        self.assertEqual(a["claims"], [])

    def test_positionless_manual_veto_reports_where_the_blind_sits(self):
        """The adopted pre-owner pause has no target position - the hold keeps the
        blind exactly where it is, and the dashboard still needs a number."""
        import json as _json
        import tempfile as _tempfile
        fd, shade_path = _tempfile.mkstemp(suffix=".json")
        os.close(fd)
        self.addCleanup(lambda: os.path.exists(shade_path) and os.remove(shade_path))
        with open(shade_path, "w") as f:
            _json.dump({"last_cmd": 55,
                        "override_until": (NOW + timedelta(minutes=45)).isoformat()}, f)
        app = make_owner(position=77, args={"shade_state_file": shade_path})
        pub = self._last_publish(app)
        self.assertEqual(pub["state"], "manual")
        self.assertEqual(pub["attributes"]["position"], 77)


if __name__ == "__main__":
    unittest.main()
