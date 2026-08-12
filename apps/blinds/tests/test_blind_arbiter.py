"""Unit tests for the shared blind precedence (no AppDaemon runtime).

The order under test is the one documented in blind_arbiter's docstring:

    manual wall press  >  venting need  >  wake target  >  solar shade

The second class checks that bedroom_solar_shade - the lowest rank, and the one
writer that re-evaluates often enough to quietly undo the others - actually asks
before it moves, and still moves when the shared layer is missing entirely.
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import MagicMock
from pathlib import Path

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

import blind_arbiter as ba  # noqa: E402
import bedroom_solar_shade as bss  # noqa: E402

MANUAL, VENT, WAKE, SHADE = ba.SOURCE_MANUAL, ba.SOURCE_VENT, ba.SOURCE_WAKE, ba.SOURCE_SHADE


def req(source, position=None, reason="", active=True):
    return ba.BlindRequest(source, position, reason, active)


class PrecedenceOrder(unittest.TestCase):
    """The whole point of the module: one ranking, in one place."""

    def test_documented_order_is_strictly_descending(self):
        ranks = [ba.priority(s) for s in ba.PRECEDENCE]
        self.assertEqual(ranks, sorted(ranks, reverse=True))
        self.assertEqual(len(set(ranks)), len(ranks), "no two sources may tie")

    def test_manual_beats_everything(self):
        winner = ba.arbitrate([req(SHADE, 92), req(WAKE, 38), req(VENT, 38), req(MANUAL, 100)])
        self.assertEqual(winner.source, MANUAL)

    def test_vent_beats_wake_and_shade(self):
        winner = ba.arbitrate([req(SHADE, 92), req(WAKE, 72), req(VENT, 38)])
        self.assertEqual(winner.source, VENT)
        self.assertEqual(winner.position, 38)

    def test_wake_beats_shade(self):
        winner = ba.arbitrate([req(SHADE, 92), req(WAKE, 38)])
        self.assertEqual(winner.source, WAKE)

    def test_shade_wins_only_when_alone(self):
        self.assertEqual(ba.arbitrate([req(SHADE, 55)]).source, SHADE)

    def test_order_of_the_list_does_not_matter(self):
        forward = ba.arbitrate([req(MANUAL, 100), req(VENT, 38), req(WAKE, 72), req(SHADE, 92)])
        backward = ba.arbitrate([req(SHADE, 92), req(WAKE, 72), req(VENT, 38), req(MANUAL, 100)])
        self.assertEqual(forward.source, backward.source)

    def test_unknown_source_ranks_below_every_known_one(self):
        self.assertEqual(ba.priority("something_new"), 0)
        winner = ba.arbitrate([req("something_new", 10), req(SHADE, 92)])
        self.assertEqual(winner.source, SHADE)

    def test_priority_of_none_is_zero_not_an_error(self):
        self.assertEqual(ba.priority(None), 0)
        self.assertEqual(ba.priority(""), 0)


class InactiveAndVetoRequests(unittest.TestCase):
    def test_inactive_requests_are_skipped(self):
        winner = ba.arbitrate([req(MANUAL, 100, active=False), req(SHADE, 92)])
        self.assertEqual(winner.source, SHADE)

    def test_no_active_requests_means_nobody_writes(self):
        self.assertIsNone(ba.arbitrate([req(MANUAL, 100, active=False)]))
        self.assertIsNone(ba.arbitrate([]))
        self.assertIsNone(ba.arbitrate(None))

    def test_a_positionless_higher_rank_blocks_rather_than_proposes(self):
        """"The wake routine owns the blind" is a veto, not a number: shading must not
        write, and nothing else may move in its place."""
        self.assertIsNone(ba.arbitrate([req(WAKE, None, "wake owns it"), req(SHADE, 92)]))
        self.assertFalse(ba.may_write(SHADE, [req(WAKE, None), req(SHADE, 92)]))

    def test_positionless_lower_rank_does_not_block_a_higher_one(self):
        winner = ba.arbitrate([req(SHADE, None), req(WAKE, 38)])
        self.assertEqual(winner.source, WAKE)

    def test_none_entries_in_the_list_are_ignored(self):
        winner = ba.arbitrate([None, req(SHADE, 92), None])
        self.assertEqual(winner.source, SHADE)


class MayWriteAndBlockedBy(unittest.TestCase):
    def test_may_write_is_true_only_for_the_winner(self):
        rs = [req(WAKE, 38), req(SHADE, 92)]
        self.assertTrue(ba.may_write(WAKE, rs))
        self.assertFalse(ba.may_write(SHADE, rs))

    def test_blocked_by_names_the_source_that_won(self):
        blocker = ba.blocked_by(SHADE, [req(MANUAL, 100, "hand on the wall"), req(SHADE, 92)])
        self.assertEqual(blocker.source, MANUAL)
        self.assertEqual(blocker.reason, "hand on the wall")

    def test_blocked_by_is_none_for_the_winner(self):
        self.assertIsNone(ba.blocked_by(SHADE, [req(SHADE, 92)]))

    def test_blocked_by_is_none_when_nobody_is_active(self):
        self.assertIsNone(ba.blocked_by(SHADE, []))

    def test_describe_lists_active_requests_highest_first(self):
        text = ba.describe([req(SHADE, 92, "hot"), req(MANUAL, 100, "wall press")])
        self.assertTrue(text.startswith("manual=100"), text)
        self.assertIn("shade=92", text)

    def test_describe_survives_an_empty_set(self):
        self.assertEqual(ba.describe([]), "no active requests")


class CommandIsTheSingleWriteSite(unittest.TestCase):
    def test_emits_the_same_service_call_the_apps_used_to_make(self):
        hass = MagicMock()
        returned = ba.command(hass, "cover.bedroom_blind", 38, source=WAKE)
        hass.call_service.assert_called_once_with(
            "cover/set_cover_position", entity_id="cover.bedroom_blind", position=38
        )
        self.assertEqual(returned, 38)

    def test_coerces_a_float_position_to_an_int(self):
        hass = MagicMock()
        ba.command(hass, "cover.bedroom_blind", 38.0, source=SHADE)
        self.assertEqual(hass.call_service.call_args.kwargs["position"], 38)

    def test_logs_only_when_a_log_fn_is_supplied(self):
        hass = MagicMock()
        lines = []
        ba.command(hass, "cover.x", 55, source=SHADE, reason="hot", log_fn=lines.append)
        self.assertEqual(len(lines), 1)
        self.assertIn("[shade]", lines[0])
        self.assertIn("hot", lines[0])

        hass2 = MagicMock()
        ba.command(hass2, "cover.x", 55, source=SHADE)  # no log_fn -> caller logs it


class SolarShadeUsesTheArbiter(unittest.TestCase):
    """The lowest rank has to ask before it moves - and still move when the shared
    module is missing, or a failed import would freeze the blind for the whole summer."""

    def _app(self, *, override_until=None):
        app = bss.BedroomSolarShade.__new__(bss.BedroomSolarShade)
        app.cover = "cover.bedroom_blind"
        app._override_until = override_until
        app._last_cmd = None
        app._save_state = MagicMock()
        app.log_lines = []
        app.log = lambda msg, **kw: app.log_lines.append(msg)
        app.call_service_calls = []
        app.call_service = lambda service, **kw: app.call_service_calls.append((service, kw))
        return app

    def test_shade_may_move_when_nothing_outranks_it(self):
        app = self._app()
        self.assertTrue(app._may_move_blind(datetime(2026, 8, 12, 12, 0), 92, "hot", wake_owns=False))

    def test_wake_veto_stops_the_shade_move(self):
        app = self._app()
        self.assertFalse(app._may_move_blind(datetime(2026, 8, 12, 6, 0), 92, "hot", wake_owns=True))
        self.assertTrue(any("outranked by wake" in line for line in app.log_lines), app.log_lines)

    def test_manual_pause_stops_the_shade_move(self):
        now = datetime(2026, 8, 12, 12, 0)
        app = self._app(override_until=now + timedelta(minutes=30))
        self.assertFalse(app._may_move_blind(now, 92, "hot", wake_owns=False))
        self.assertTrue(any("outranked by manual" in line for line in app.log_lines), app.log_lines)

    def test_manual_beats_wake_in_the_shared_ranking(self):
        """Both veto shading, but the log must name the higher rank - that is the
        difference between the shared ranking and three private pause flags."""
        now = datetime(2026, 8, 12, 6, 0)
        app = self._app(override_until=now + timedelta(minutes=30))
        app._may_move_blind(now, 92, "hot", wake_owns=True)
        self.assertTrue(any("outranked by manual" in line for line in app.log_lines), app.log_lines)

    def test_wake_ownership_is_read_from_the_entities_when_not_passed(self):
        """The default path: _blind_requests works out for itself whether the wake
        routine still owns the blind, so the gate in _tick and the arbitration at the
        write site cannot disagree about it."""
        app = self._app()
        app.alarm_time_entity = "input_datetime.wakeup_bedroom"
        app.sleep_entity = "input_boolean.mikkel_sleep_mode"
        app.fallback_wake = __import__("datetime").time(7, 30)
        app.wake_grace_min = 20
        app.get_state = lambda entity, **kw: {
            app.alarm_time_entity: "06:00",
            app.sleep_entity: "off",
        }.get(entity)

        # 05:50 is before wake+grace (06:20) -> the wake routine still owns it.
        self.assertFalse(app._may_move_blind(datetime(2026, 8, 12, 5, 50), 92, "hot"))
        self.assertTrue(any("outranked by wake" in line for line in app.log_lines), app.log_lines)
        # 07:00 is past it -> shading gets the blind.
        self.assertTrue(app._may_move_blind(datetime(2026, 8, 12, 7, 0), 92, "hot"))

    def test_a_blocked_move_logs_the_whole_claim_set(self):
        """The 06:00 log has to answer "why did the blind not move" on its own."""
        now = datetime(2026, 8, 12, 6, 0)
        app = self._app(override_until=now + timedelta(minutes=30))
        app._may_move_blind(now, 92, "blocking sun heat", wake_owns=True)
        line = next(l for l in app.log_lines if "outranked" in l)
        self.assertIn("manual=-", line)
        self.assertIn("wake=-", line)
        self.assertIn("shade=92", line)

    def test_expired_manual_pause_no_longer_outranks(self):
        now = datetime(2026, 8, 12, 12, 0)
        app = self._app(override_until=now - timedelta(minutes=1))
        self.assertTrue(app._may_move_blind(now, 92, "hot", wake_owns=False))

    def test_command_blind_writes_and_records_the_baseline(self):
        app = self._app()
        app._command_blind(92, "blocking sun heat")
        self.assertEqual(
            app.call_service_calls,
            [("cover/set_cover_position", {"entity_id": "cover.bedroom_blind", "position": 92})],
        )
        self.assertEqual(app._last_cmd, 92)
        app._save_state.assert_called_once()
        self.assertTrue(any("-> 92%" in line for line in app.log_lines), app.log_lines)

    def test_missing_shared_module_still_moves_the_blind(self):
        """Fail-safe: no arbiter -> pre-arbiter behaviour, not a frozen cover."""
        app = self._app()
        original = bss.blind_arbiter
        try:
            bss.blind_arbiter = None
            self.assertTrue(app._may_move_blind(datetime(2026, 8, 12, 6, 0), 92, "hot", wake_owns=True))
            app._command_blind(92, "hot")
        finally:
            bss.blind_arbiter = original
        self.assertEqual(
            app.call_service_calls,
            [("cover/set_cover_position", {"entity_id": "cover.bedroom_blind", "position": 92})],
        )
        self.assertEqual(app._last_cmd, 92)


if __name__ == "__main__":
    unittest.main()
