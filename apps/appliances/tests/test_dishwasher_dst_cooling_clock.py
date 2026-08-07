# tests/test_dishwasher_dst_cooling_clock.py - DST fall-back regression for
# DishwasherMonitor._should_change_state's cooling-period comparison.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q
#
# Sibling of test_dryer_dst_cooling_clock.py, which covers the same bug in DryerMonitor. The
# dishwasher carried the identical method, verbatim, and was missed by the original writeup
# because that only looked at the dryer - when auditing this family, grep all three monitors.
#
# The bug: _should_change_state compared a naive datetime.now() against last_state_change. At the
# Europe/Copenhagen October fall-back the local clock jumps an hour BACKWARDS (03:00 CEST ->
# 02:00 CET), so `now - last_state_change` goes negative for the whole repeated hour - and a
# negative delta is trivially < cooling_period, so every un-forced transition is silently refused
# (DEBUG only) until the hour is out. Now compares self._now_utc(), matching washer_monitor.py.
#
# These tests pin the INVARIANT rather than the calendar: the stamp must be timezone-aware, and
# the elapsed comparison must run on aware UTC. The fall-back instants below are used because they
# make the naive-vs-aware difference concrete, not because the behaviour is date-specific.
#
# Per this repo's incident history, stubbing out the very function whose interaction with its
# caller IS the bug would hide it again - so these call the real _should_change_state. Only the
# AppDaemon I/O boundary (log / get_state) is faked.
#
# The real Europe/Copenhagen 2026-10-25 fall-back instants (03:00 CEST -> 02:00 CET;
# CEST = UTC+2, CET = UTC+1). The changeover is at 01:00 UTC, so BOTH comparison instants below
# sit after it while the stamp sits before - that is what makes naive local time run backwards
# across every case here. Note these differ from the dryer sibling file's: the dishwasher's
# cooling_period is 300s, not 600s, so the stamp has to sit closer to the boundary for an
# inside-the-window case to exist on the far side of it at all.
#   2026-10-25 00:58:00+00:00 -> 02:58 CEST (stamp time)
#   2026-10-25 01:01:00+00:00 -> 02:01 CET  (+180 real s, inside 300s; naive delta -57 min)
#   2026-10-25 01:07:00+00:00 -> 02:07 CET  (+540 real s, past 300s;   naive delta -51 min)

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta, timezone
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

import dishwasher_monitor as dm  # noqa: E402

# See the module docstring above for what each looks like in naive Europe/Copenhagen local time.
FALL_BACK_STAMP_UTC = datetime(2026, 10, 25, 0, 58, 0, tzinfo=timezone.utc)          # 02:58 CEST
FALL_BACK_INSIDE_COOLING_UTC = datetime(2026, 10, 25, 1, 1, 0, tzinfo=timezone.utc)  # 02:01 CET
FALL_BACK_PAST_COOLING_UTC = datetime(2026, 10, 25, 1, 7, 0, tzinfo=timezone.utc)    # 02:07 CET


def make_app():
    """DishwasherMonitor built with __new__ (initialize() never runs), holding only the attributes
    _should_change_state touches. cooling_period is hardcoded to 300 in dishwasher_monitor.py's
    initialize() (never read from args), so it is set here to match rather than derived."""
    app = dm.DishwasherMonitor.__new__(dm.DishwasherMonitor)

    app.state_entity = "sensor.dishwasher_state"
    app.cooling_period = 300

    app.state = "Off"
    app.states = {}
    app.last_state_change = None

    app.log_calls = []
    app.log = lambda *a, **kw: app.log_calls.append((a, kw))
    app.get_state = lambda entity, **kw: app.states.get(entity)

    return app


class CoolingCheckCrossesTheFallBackBoundary(unittest.TestCase):
    """The core regression. Under the old naive datetime.now(), crossing the fall-back made
    `now - last_state_change` go negative (naive local time ran BACKWARDS between the two calls),
    which is trivially < cooling_period and refused the transition no matter how much real time
    had passed. Both directions are checked: past the cooling period must now be allowed (proves
    the fix), and still inside it must remain refused (proves the fix did not degenerate into
    always returning True)."""

    def test_past_cooling_period_across_fall_back_is_allowed(self):
        """540 real seconds elapsed, past the 300s cooling_period. The old naive code saw local
        time go from 02:58 CEST to 02:07 CET - a -51 minute delta, which is < 300 and would have
        wrongly refused this transition."""
        app = make_app()
        app.states[app.state_entity] = "Off"
        app.last_state_change = FALL_BACK_STAMP_UTC
        app._now_utc = lambda: FALL_BACK_PAST_COOLING_UTC

        self.assertTrue(app._should_change_state("Running"))

    def test_inside_cooling_period_across_fall_back_is_still_refused(self):
        """Only 180 real seconds elapsed, genuinely inside the 300s cooling_period, and still on
        the far side of the changeover so naive local time has already run backwards - the cooling
        check must keep working across the DST boundary, not just stop mattering."""
        app = make_app()
        app.states[app.state_entity] = "Off"
        app.last_state_change = FALL_BACK_STAMP_UTC
        app._now_utc = lambda: FALL_BACK_INSIDE_COOLING_UTC

        self.assertFalse(app._should_change_state("Running"))


class LastStateChangeStaysAwareAfterAnAcceptedTransition(unittest.TestCase):
    """The invariant every writer depends on: last_state_change must always be an aware UTC
    datetime. _should_change_state is the dishwasher's sole writer - there is no dishwasher
    equivalent of the dryer's separate plug-outage writer - but a naive stamp here would still
    make the very next comparison raise TypeError instead of returning a bool."""

    def test_accepted_transition_stamps_an_aware_utc_datetime(self):
        app = make_app()
        app.states[app.state_entity] = "Off"
        app._now_utc = lambda: FALL_BACK_STAMP_UTC

        self.assertTrue(app._should_change_state("Running"))
        self.assertIsNotNone(app.last_state_change.tzinfo)
        self.assertEqual(app.last_state_change.utcoffset(), timedelta(0))
