"""Replay of the wake-ups where the house played a speaker louder than its standard.

Between the end of August and the 20th of October 2026 the alarm woke the house 42 times with
logs to read. In 24 of those 30-minute windows BedroomBathroomVolumeSync copied the bathroom's
volume into the bedroom, and in 23 of them what it copied was above the bedroom's standard of 0.14
(0.19 to 0.43). WINDOWS holds those 24 windows as the sync logged them, with nothing but offsets
and levels: (seconds after the alarm, what triggered the sync, the bedroom level it replaced, the
bathroom level it copied).

Each window is replayed through the real wake routine and the real sync, sharing one pair of
speakers: the alarm fires, the ramp runs its ticks, and at every logged offset the bathroom is at
the logged level and the sync is asked to act. Whatever the ramp or the sync writes is recorded.
The leftover volume before the alarm is the loudest one the window saw.

Speakers are checked against the standards in force when the windows were recorded, so the replay
does not move when a standard is retuned (the current ones are pinned in test_wakeup_volume_cap).
"""

from __future__ import annotations

import sys
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "lights"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "blinds"))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "sonos"))

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

import bedroom_bathroom_volume_sync as bbvs  # noqa: E402
import wakeup_bedroom as wb  # noqa: E402

BEDROOM = "media_player.bedroom"
BATHROOM = "media_player.bathroom"
STANDARD = {BEDROOM: 0.14, BATHROOM: 0.18}
RESET_ARGS = {"speaker_volumes": dict(STANDARD), "default_volume": 0.12}
ALARM = datetime(2026, 10, 1, 7, 0, 0)
WINDOW_SEC = 30 * 60

# (seconds after the alarm, trigger, bedroom level replaced, bathroom level copied)
WINDOWS = [
    [(1603, "activated", 0.18, 0.24)],
    [(797, "activated", 0.18, 0.33), (845, "bathroom_volume_change", 0.33, 0.43), (887, "bathroom_volume_change", 0.43, 0.33), (932, "bathroom_volume_change", 0.33, 0.43), (1366, "bathroom_volume_change", 0.43, 0.33)],
    [(536, "activated", 0.18, 0.24), (637, "bathroom_volume_change", 0.24, 0.34), (680, "bathroom_volume_change", 0.34, 0.24), (697, "bathroom_volume_change", 0.24, 0.34), (1052, "bathroom_volume_change", 0.34, 0.24)],
    [(860, "activated", 0.18, 0.24)],
    [(1, "speaker_state_change", 0.06, 0.24)],
    [(751, "activated", 0.18, 0.24)],
    [(521, "activated", 0.18, 0.24)],
    [(113, "activated", 0.09, 0.24)],
    [(1268, "activated", 0.18, 0.24)],
    [(600, "activated", 0.18, 0.24)],
    [(890, "activated", 0.18, 0.24), (1239, "bathroom_volume_change", 0.24, 0.34), (1693, "bathroom_volume_change", 0.34, 0.24)],
    [(1754, "activated", 0.18, 0.24)],
    [(2, "speaker_state_change", 0.06, 0.24), (767, "activated", 0.18, 0.34), (1107, "bathroom_volume_change", 0.34, 0.24)],
    [(274, "activated", 0.18, 0.24)],
    [(1419, "activated", 0.18, 0.24)],
    [(629, "activated", 0.18, 0.24)],
    [(1563, "activated", 0.18, 0.24)],
    [(2, "speaker_state_change", 0.06, 0.19), (8, "speaker_state_change", 0.06, 0.19)],
    [(778, "activated", 0.18, 0.24)],
    [(409, "activated", 0.18, 0.24)],
    [(636, "activated", 0.18, 0.24)],
    [(1, "speaker_state_change", 0.06, 0.24), (1405, "activated", 0.18, 0.24), (1440, "bathroom_volume_change", 0.24, 0.34), (1796, "bathroom_volume_change", 0.34, 0.24)],
    [(415, "activated", 0.13, 0.07)],
    [(104, "activated", 0.09, 0.34), (1168, "bathroom_volume_change", 0.18, 0.24)],
]


class House:
    """The bedroom and bathroom speakers as the wake routine and the sync see them."""

    def __init__(self, leftover):
        self.now = ALARM
        self.volume = {BEDROOM: leftover, BATHROOM: leftover}
        self.writes = []

    def service(self, who):
        def call_service(service, **kw):
            if service == "media_player/volume_set":
                self.volume[kw["entity_id"]] = kw["volume_level"]
                self.writes.append((self.now, who, kw["entity_id"], kw["volume_level"]))

        return call_service

    def wake_routine(self):
        app = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
        app.log = lambda *a, **kw: None
        app.user_log = "replay"
        app.datetime = lambda: self.now
        app.master_player = BEDROOM
        app.join_players = [BATHROOM]
        app.volume_entities = [BEDROOM, BATHROOM]
        app.volume_start, app.volume_target, app.volume_step, app.volume_interval_sec = 0.06, 0.18, 0.03, 60
        app.volume_active = False
        app.volume_timer = None
        app.volume_ceilings = {}
        app.volume_levels = {}
        app.wake_window_min = 30
        app._wake_window_until = None
        app._wake_group_seen = False
        app._alarm_fired_at = ALARM
        app._state = {}
        app._save_state = MagicMock()
        app.stream_id = "stream"
        app.media_verify_delay_sec = 12
        app.media_still_playing_check_sec = 45
        app.bedroom_presence_entity = None
        app._play_media = MagicMock()
        app.run_in = MagicMock(return_value="timer")
        app.call_service = self.service("wake")
        app.get_app = lambda name: SimpleNamespace(args=RESET_ARGS) if name == wb.STANDARD_VOLUME_APP else None

        def get_state(entity, **kw):
            if kw.get("attribute") == "all":
                return {"attributes": {"volume_level": self.volume[entity]}}
            if (entity, kw.get("attribute")) == (BEDROOM, "group_members"):
                return [BEDROOM, BATHROOM]
            return None

        app.get_state = get_state
        return app

    def sync(self, wake_app):
        app = bbvs.BedroomBathroomVolumeSync.__new__(bbvs.BedroomBathroomVolumeSync)
        app.bathroom_speaker = BATHROOM
        app.bedroom_speaker = BEDROOM
        app.wake_app = "WakeupBedroom"
        app._sync_active = True
        app._wake_hold_logged = False
        app.log = lambda *a, **kw: None
        app.get_app = lambda name: wake_app
        app.call_service = self.service("sync")
        app.get_state = lambda entity, attribute=None, **kw: (
            "playing" if attribute is None else self.volume[entity]
        )
        return app


def replay(window, *, hold=True, extra=()):
    """One wake-up: the alarm fires, the ramp ticks every minute, and at each logged offset the
    bathroom is at the logged level and the sync is asked. hold=False answers the sync as if no
    wake window existed, which is how it ran when the windows were logged."""
    house = House(leftover=max(e[3] for e in window))
    wake = house.wake_routine()
    sync = house.sync(wake if hold else SimpleNamespace(wake_window_open=lambda: False))
    wake._open_wake_window()
    wake._start_media_and_volume_ramp()
    steps = [(60 * k, 0, "tick", None) for k in range(1, 61)]
    steps += [(off, 1, "sync", (reason, bathroom)) for off, reason, _bedroom, bathroom in [*window, *extra]]
    for offset, _, kind, payload in sorted(steps, key=lambda s: (s[0], s[1])):
        house.now = ALARM + timedelta(seconds=offset)
        if kind == "tick":
            wake._volume_tick(None)
        else:
            reason, bathroom = payload
            house.volume[BATHROOM] = bathroom
            sync._maybe_sync(reason)
    return house.writes


def above_standard(writes):
    return [w for w in writes if w[3] > STANDARD[w[2]]]


class TheWindowsThatBrokeTheRule(unittest.TestCase):
    def test_the_fixture_is_the_windows_that_were_found(self):
        self.assertEqual(len(WINDOWS), 24)
        self.assertEqual(sum(any(e[3] > STANDARD[BEDROOM] for e in w) for w in WINDOWS), 23)
        self.assertTrue(all(e[0] < WINDOW_SEC for w in WINDOWS for e in w))

    def test_as_the_sync_ran_before_the_hold_it_wrote_above_the_standard_in_23_windows(self):
        broke = [w for w in WINDOWS if above_standard(replay(w, hold=False))]
        self.assertEqual(len(broke), 23)
        copied = [replay(w, hold=False) for w in WINDOWS]
        self.assertTrue(all(any(who == "sync" for _, who, _, _ in writes) for writes in copied))

    def test_with_the_hold_no_speaker_is_ever_set_above_its_standard(self):
        for i, window in enumerate(WINDOWS):
            self.assertEqual(above_standard(replay(window)), [], f"window {i}")

    def test_with_the_hold_the_sync_copies_nothing_in_any_window(self):
        for i, window in enumerate(WINDOWS):
            self.assertEqual([w for w in replay(window) if w[1] == "sync"], [], f"window {i}")

    def test_with_the_hold_the_alarm_starts_both_speakers_at_volume_start_and_ramps_them_home(self):
        for i, window in enumerate(WINDOWS):
            writes = replay(window)
            first = [(who, entity, level) for t, who, entity, level in writes if t == ALARM]
            self.assertEqual(first, [("wake", BEDROOM, 0.06), ("wake", BATHROOM, 0.06)], f"window {i}")
            last = {entity: level for _, _, entity, level in writes}
            self.assertEqual(last, {BEDROOM: 0.14, BATHROOM: 0.18}, f"window {i}")

    def test_a_bathroom_raised_mid_ramp_is_brought_straight_back_to_its_standard(self):
        writes = replay([(1, "speaker_state_change", 0.06, 0.24)])
        self.assertEqual([level for _, _, entity, level in writes if entity == BATHROOM], [0.06, 0.18])

    def test_the_sync_resumes_the_moment_the_window_has_ended(self):
        window = [(120, "activated", 0.12, 0.24)]
        writes = replay(window, extra=[(WINDOW_SEC - 1, "bathroom_volume_change", 0.14, 0.24),
                                       (WINDOW_SEC, "bathroom_volume_change", 0.14, 0.24)])
        sync_writes = [(t, entity, level) for t, who, entity, level in writes if who == "sync"]
        self.assertEqual(sync_writes, [(ALARM + timedelta(seconds=WINDOW_SEC), BEDROOM, 0.24)])


if __name__ == "__main__":
    unittest.main()
