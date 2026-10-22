"""Replay of the 42 wake-ups with logs to read, against the rule for the bedroom speaker.

During the wake window the speaker in the bedroom stays at or under its standard volume for as
long as the bed is not known to be empty; once the bed reads empty the volume follows the person
(the sync copies the bathroom in full), and the bathroom is never capped. The ramp starts every
wake speaker at volume_start whatever it was left at and only ever raises.

WINDOWS holds each morning as the house saw it, with nothing but offsets and states (no dates,
no clock times):
  - the bed witnesses (left mat, right mat, strip) as three letters, n on / f off / u unreadable,
    at the alarm and at every change;
  - how long the bathroom door had been open when the alarm fired (None: closed) and every change
    of it - the sync is active once the door has been open 60 s;
  - the bath presence sensor at the alarm and every change, and any wall-button presses.

Each morning runs through the real wake routine, the real sync and the real bath presence control,
sharing one pair of speakers that start at 0.43 (the loudest leftover ever seen): the alarm fires,
the ramp ticks every minute, the bed guard every minute, and the sync is asked at every moment it
would be (a bathroom volume change, the speakers starting, the door arming it). Whatever the three
write is recorded with the state of the bed at that instant.

Speakers are checked against the standards in force when the mornings were recorded, so the replay
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
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "presence"))

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
import room_active_read  # noqa: E402
import sonos_bathroom_control as sbc  # noqa: E402
import wakeup_bedroom as wb  # noqa: E402

# wakeup_bedroom imports its shared layers defensively, and another test file may have imported it
# first without apps/presence on the path; the cap needs the real reader.
wb.room_active_read = room_active_read

BEDROOM = "media_player.bedroom"
BATHROOM = "media_player.bathroom"
STANDARD = {BEDROOM: 0.14, BATHROOM: 0.18}
RESET_ARGS = {"speaker_volumes": dict(STANDARD), "default_volume": 0.12}
WITNESSES = [
    "binary_sensor.left_bedside",
    "binary_sensor.right_bedside",
    "binary_sensor.bed_presence_6b9c94_bed_occupied_either",
]
ZONES = {"bedroom": [{"entity": e, "tier": "bed"} for e in WITNESSES] + [{"entity": "binary_sensor.fp300", "tier": "room"}]}
STATE = {"n": "on", "f": "off", "u": "unknown"}
SENSOR = "binary_sensor.bathroom_bath_presence_presence"
ALARM = datetime(2026, 10, 1, 7, 0, 0)
WINDOW_SEC = 30 * 60
GUARD_PHASE_SEC = 30
GUARD_LAG_SEC = 70

# (bed, door open for, door changes, bath presence at the alarm, bath presence changes, wall presses)
WINDOWS = [
    ([(0, "nun"), (1534, "nuf"), (1567, "fuf")], None, [(1542, "on")], "off", [], []),
    ([(0, "nfn"), (412, "nff"), (448, "fff"), (1126, "ffn"), (1293, "fnn")], None, [(572, "on"), (577, "off"), (1106, "on"), (1111, "off")], "off", [], []),
    ([(0, "nfn"), (836, "nff"), (848, "fff")], None, [], "off", [], []),
    ([(0, "nun"), (207, "nuf"), (232, "fuf")], None, [(216, "on"), (223, "off"), (737, "on")], "off", [(845, "on"), (887, "off"), (932, "on"), (1366, "off")], [(735, "up"), (740, "up")]),
    ([(0, "nun"), (466, "nuf"), (501, "fuf")], None, [(476, "on")], "off", [(637, "on"), (680, "off"), (697, "on"), (1051, "off")], []),
    ([(0, "nfn"), (58, "nff"), (59, "nfn"), (107, "ffn"), (172, "fnn"), (481, "fnf"), (525, "fff"), (1350, "ffn"), (1367, "fff")], None, [(482, "on"), (486, "off"), (800, "on"), (1670, "off")], "off", [(689, "on"), (741, "off"), (752, "on"), (822, "off")], []),
    ([(0, "nfn"), (654, "ffn"), (711, "fff"), (869, "ffn"), (873, "fff")], None, [(517, "on"), (571, "off"), (904, "on"), (940, "off"), (998, "on")], "off", [], []),
    ([(0, "ffn")], 2129, [], "off", [], []),
    ([(0, "nfn"), (563, "nff"), (581, "fff"), (598, "ffn"), (612, "fff")], None, [(603, "on"), (607, "off"), (691, "on")], "off", [], []),
    ([(0, "nff"), (9, "fff"), (472, "ffn"), (697, "nfn")], None, [(0, "on"), (5, "off"), (461, "on")], "off", [], []),
    ([(0, "nfn"), (50, "nff"), (67, "fff")], None, [(51, "on"), (117, "off"), (1328, "on"), (1336, "off")], "off", [(372, "on"), (736, "off")], []),
    ([(0, "fun"), (190, "nun")], None, [], "off", [], []),
    ([(0, "nfn"), (287, "ffn"), (969, "fff")], None, [(971, "on"), (975, "off"), (1207, "on")], "off", [], []),
    ([(0, "nfn"), (47, "nff"), (58, "fff")], None, [(52, "on"), (56, "off"), (540, "on")], "off", [], []),
    ([(0, "nfn"), (3, "nff"), (31, "fff"), (839, "ffn"), (1078, "fff")], None, [(5, "on"), (8, "off"), (828, "on")], "off", [(1236, "on"), (1692, "off")], []),
    ([(0, "nfn"), (935, "ffn"), (937, "fff"), (938, "ffn"), (976, "fff")], None, [(1051, "on"), (1278, "off"), (1755, "on")], "off", [], []),
    ([(0, "nfn")], None, [], "off", [], []),
    ([(0, "nnn"), (209, "fnn"), (1036, "fnf"), (1045, "fff")], None, [(1037, "on"), (1042, "off"), (1694, "on")], "off", [], []),
    ([(0, "fnn"), (334, "fnf"), (427, "fff"), (1201, "ffn")], 5563, [(380, "off"), (705, "on")], "off", [(713, "on"), (753, "off"), (764, "on"), (1106, "off")], []),
    ([(0, "fff"), (166, "ffn"), (167, "fff"), (188, "ffn"), (192, "fff"), (214, "ffn"), (217, "fff"), (224, "ffn"), (528, "fff"), (531, "ffn"), (557, "fff"), (565, "ffn"), (605, "fff"), (607, "ffn")], None, [], "off", [], []),
    ([(0, "nfn"), (57, "nff"), (70, "fff"), (227, "ffn")], None, [(58, "on"), (62, "off"), (214, "on")], "off", [], []),
    ([(0, "nfn"), (511, "nff"), (557, "fff")], None, [(619, "on"), (623, "off"), (716, "on"), (779, "off")], "off", [], []),
    ([(0, "ffn"), (9, "fff"), (9, "ffn"), (255, "fnn"), (986, "fnf"), (987, "fnn"), (992, "fnf"), (1024, "fff")], None, [(1045, "on"), (1345, "off"), (1493, "on")], "off", [(1337, "on"), (1367, "off")], []),
    ([(0, "ffn"), (1323, "fff")], None, [(1359, "on"), (1602, "off")], "off", [], []),
    ([(0, "nfn"), (563, "nff"), (591, "fff"), (1580, "ffn"), (1595, "fff"), (1598, "ffn"), (1615, "fff")], None, [(568, "on"), (655, "off"), (1563, "on")], "off", [(869, "on"), (1289, "off")], []),
    ([(0, "nfn"), (163, "ffn"), (454, "nfn"), (543, "ffn"), (1219, "fff")], None, [(1220, "on"), (1224, "off"), (1501, "on")], "off", [], []),
    ([(0, "nfn"), (1407, "nff"), (1408, "nfn"), (1416, "ffn"), (1445, "fff")], None, [(1480, "on"), (1484, "off")], "off", [], []),
    ([(0, "fff")], 2853, [], "off", [], []),
    ([(0, "ffn"), (1246, "fff"), (1249, "ffn"), (1252, "fff"), (1253, "ffn"), (1531, "fff")], None, [], "off", [], []),
    ([(0, "nun"), (308, "nuf"), (312, "nun"), (371, "nuf"), (383, "fuf")], None, [(380, "on"), (384, "off"), (718, "on")], "off", [], []),
    ([(0, "nun"), (6, "nuf"), (44, "fuf")], None, [(10, "on"), (14, "off"), (349, "on")], "off", [], []),
    ([(0, "nfn"), (1598, "nff"), (1602, "nfn"), (1605, "nff"), (1637, "fff")], None, [(1605, "on"), (1609, "off"), (1745, "on")], "off", [(1795, "on")], []),
    ([(0, "nfn"), (394, "nff"), (395, "nfn"), (588, "nff"), (589, "nfn"), (619, "ffn"), (971, "fnn")], None, [(390, "on"), (396, "off"), (574, "on")], "off", [], []),
    ([(0, "ffn"), (566, "fff")], 6625, [(1303, "off"), (1345, "on")], "off", [(1440, "on"), (1796, "off")], []),
    ([(0, "fnn"), (1029, "fnf"), (1041, "fff"), (1179, "ffn"), (1180, "fff"), (1181, "ffn"), (1191, "fff"), (1194, "ffn"), (1368, "nfn")], None, [(355, "on"), (1032, "off"), (1159, "on"), (1163, "off")], "off", [], []),
    ([(0, "ffn")], None, [], "off", [], []),
    ([(0, "nfn"), (450, "ffn"), (706, "fnn"), (975, "fnf"), (1021, "fff")], None, [(507, "on"), (511, "off"), (618, "on"), (623, "off"), (1100, "on")], "off", [], []),
    ([(0, "nun"), (566, "nuf"), (567, "nun"), (570, "nuf"), (641, "fuf"), (1240, "fun"), (1315, "fuf")], None, [(573, "on"), (577, "off"), (704, "on")], "off", [], []),
    ([(0, "nfn"), (43, "nff"), (161, "fff")], None, [(44, "on")], "off", [(93, "on"), (1168, "off")], []),
    ([(0, "nfn")], None, [(696, "on")], "off", [], []),
    ([(0, "nfn")], None, [(99, "on")], "off", [], []),
    ([(0, "nfn")], None, [], "off", [], []),
]


class House:
    """The two speakers and the bed as the wake routine, the sync and the bath control see them."""

    def __init__(self, bed):
        self.now = ALARM
        self.bed = bed
        self.volume = {BEDROOM: 0.43, BATHROOM: 0.43}
        self.writes = []
        self.on_bathroom_volume = None

    @property
    def offset(self):
        return int((self.now - ALARM).total_seconds())

    def service(self, who):
        def call_service(service, **kw):
            if service == "media_player/volume_set":
                entity, level = kw["entity_id"], kw["volume_level"]
                changed = self.volume[entity] != level
                self.volume[entity] = level
                self.writes.append((self.offset, who, entity, level, self.bed))
                if entity == BATHROOM and changed and self.on_bathroom_volume:
                    self.on_bathroom_volume()

        return call_service

    def get_state(self, entity, attribute=None, **kw):
        if attribute == "all":
            return {"attributes": {"volume_level": self.volume[entity]}}
        if (entity, attribute) == (BEDROOM, "group_members"):
            return [BEDROOM, BATHROOM]
        if attribute == "volume_level":
            return self.volume[entity]
        if entity in WITNESSES:
            return STATE[self.bed[WITNESSES.index(entity)]]
        if entity in (BEDROOM, BATHROOM):
            return "playing"
        return None


def build(bed, bath_present, *, capped=True):
    house = House(bed)
    wake = wb.WakeupRoutine.__new__(wb.WakeupRoutine)
    wake.log = lambda *a, **kw: None
    wake.user_log = "replay"
    wake.datetime = lambda: house.now
    wake.master_player = BEDROOM
    wake.join_players = [BATHROOM]
    wake.volume_entities = [BEDROOM, BATHROOM]
    wake.bed_speaker = BEDROOM
    wake.volume_start, wake.volume_target, wake.volume_step, wake.volume_interval_sec = 0.06, 0.18, 0.03, 60
    wake.volume_active = False
    wake.volume_timer = None
    wake.volume_ramp = {}
    wake.volume_levels = {}
    wake.wake_window_min = 30
    wake._wake_window_until = None
    wake._wake_group_seen = False
    wake._alarm_fired_at = ALARM
    wake._bed_was_empty = False
    wake._state = {}
    wake._save_state = MagicMock()
    wake.stream_id = "stream"
    wake.media_verify_delay_sec = 12
    wake.media_still_playing_check_sec = 45
    wake.bedroom_presence_entity = None
    wake._play_media = MagicMock()
    wake.run_in = MagicMock(return_value="timer")
    wake.call_service = house.service("wake")
    wake.get_app = lambda name: (
        SimpleNamespace(args=RESET_ARGS) if name == wb.STANDARD_VOLUME_APP
        else SimpleNamespace(zones=ZONES) if name == "RoomActive" else None
    )
    wake.get_state = house.get_state

    asked = []

    def wake_volume_cap(entity):
        cap = wake.wake_volume_cap(entity) if capped else None
        asked.append((house.offset, entity, cap, house.volume[BATHROOM], house.bed))
        return cap

    sync = bbvs.BedroomBathroomVolumeSync.__new__(bbvs.BedroomBathroomVolumeSync)
    sync.bathroom_speaker, sync.bedroom_speaker, sync.wake_app = BATHROOM, BEDROOM, "WakeupBedroom"
    sync._sync_active = False
    sync.log = lambda *a, **kw: None
    sync.get_app = lambda name: SimpleNamespace(wake_volume_cap=wake_volume_cap)
    sync.call_service = house.service("sync")
    sync.get_state = house.get_state

    bath = sbc.SonosBathroomControl.__new__(sbc.SonosBathroomControl)
    bath.media_player = BATHROOM
    bath.bath_presence_volume_boost = 0.1
    bath.presence_on_max_volume = 0.60
    bath.volume_step = 0.05
    bath.zwave_property_key_volume_up = "004"
    bath.zwave_property_key_volume_down = "003"
    bath._logically_present = bath_present == "on"
    bath._last_presence_change_time = 0.0
    bath._presence_throttle_seconds = 0.5
    bath.log = lambda *a, **kw: None
    bath.error = lambda *a, **kw: None
    bath.call_service = house.service("bath")
    bath.get_state = house.get_state

    house.on_bathroom_volume = lambda: sync._sync_active and sync._maybe_sync("bathroom_volume_change")
    return house, wake, sync, bath, asked


def door_steps(door_open_for, door):
    """(offset, order, kind) for the sync: armed once the door has been open 60 s, disarmed when it closes."""
    steps = []
    open_at = -door_open_for if door_open_for is not None else None
    for offset, state in door:
        if state == "on":
            open_at = offset
        elif open_at is not None:
            if offset - open_at >= 60:
                steps.append((open_at + 60, "arm"))
                steps.append((offset, "disarm"))
            open_at = None
    if open_at is not None:
        steps.append((open_at + 60, "arm"))
    return steps


class Result:
    def __init__(self, house, asked, samples):
        self.writes = house.writes
        self.volume = dict(house.volume)
        self.asked = asked
        self.samples = samples


def replay(window, *, capped=True):
    bed, door_open_for, door, bath_present, bath_changes, presses = window
    house, wake, sync, bath, asked = build(bed[0][1], bath_present, capped=capped)
    wake._open_wake_window()
    wake._start_media_and_volume_ramp()
    steps = [(off, 0, "bed", code) for off, code in bed[1:]]
    for off, kind in door_steps(door_open_for, door):
        steps.append((max(off, 0), 1, kind, None))
    steps += [(off, 2, "presence", state) for off, state in bath_changes]
    steps += [(off, 2, "press", direction) for off, direction in presses]
    steps += [(60 * k, 3, "tick", None) for k in range(1, 31)]
    steps += [(GUARD_PHASE_SEC + 60 * k, 4, "guard", None) for k in range(0, 30)]
    steps += [(1, 5, "speakers", None)]
    steps += [(k, 9, "sample", None) for k in range(0, WINDOW_SEC, 10)]
    samples = []
    for offset, _, kind, payload in sorted(steps, key=lambda s: (s[0], s[1])):
        house.now = ALARM + timedelta(seconds=offset)
        if kind == "bed":
            house.bed = payload
        elif kind == "arm":
            sync._sync_active = True
            sync._maybe_sync("activated")
        elif kind == "disarm":
            sync._sync_active = False
        elif kind == "presence":
            bath._last_presence_change_time = 0.0
            bath._handle_presence_change(SENSOR, None, "off" if payload == "on" else "on", payload, {})
        elif kind == "press":
            key = "004" if payload == "up" else "003"
            bath.handle_volume_event("zwave_js_value_notification", {"property_key": key}, {})
        elif kind == "tick":
            wake._volume_tick(None)
        elif kind == "guard":
            wake._bed_guard(None)
        elif kind == "speakers":
            if sync._sync_active:
                sync._maybe_sync("speaker_state_change")
        else:
            cap = wake.wake_volume_cap(BEDROOM)
            samples.append((offset, house.bed, cap, house.volume[BEDROOM], house.volume[BATHROOM]))
    return Result(house, asked, samples)


def bed_is_empty(code):
    return code == "fff"


def bedroom_writes_above_standard(result):
    return [w for w in result.writes if w[2] == BEDROOM and w[3] > STANDARD[BEDROOM]]


class TheForty2Mornings(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.results = [replay(w) for w in WINDOWS]

    def test_the_fixture_is_the_forty_two_mornings(self):
        self.assertEqual(len(WINDOWS), 42)
        for bed, _, door, _, bath, presses in WINDOWS:
            self.assertEqual(bed[0][0], 0)
            for changes in (bed[1:], door, bath, presses):
                self.assertTrue(all(0 <= off < WINDOW_SEC for off, _ in changes))

    def test_every_alarm_starts_both_speakers_at_volume_start_whatever_they_were_left_at(self):
        for i, result in enumerate(self.results):
            first = [(who, entity, level) for off, who, entity, level, _ in result.writes if off == 0]
            self.assertEqual(first, [("wake", BEDROOM, 0.06), ("wake", BATHROOM, 0.06)], f"morning {i}")

    def test_no_write_sets_the_bedroom_above_its_standard_while_the_bed_is_not_empty(self):
        for i, result in enumerate(self.results):
            bad = [w for w in bedroom_writes_above_standard(result) if not bed_is_empty(w[4])]
            self.assertEqual(bad, [], f"morning {i}")

    def test_the_bedroom_is_never_above_its_standard_while_the_cap_holds_beyond_one_guard_interval(self):
        for i, result in enumerate(self.results):
            non_empty_since = None
            for offset, bed, cap, bedroom, _ in result.samples:
                if cap is None:
                    non_empty_since = None
                    continue
                if non_empty_since is None:
                    non_empty_since = offset
                if bedroom > cap + 1e-9:
                    self.assertLessEqual(offset - non_empty_since, GUARD_LAG_SEC, f"morning {i} at {offset}")

    def test_the_bathroom_is_never_capped_and_the_boost_lands_the_moment_he_steps_in(self):
        steps_in = 0
        for i, (window, result) in enumerate(zip(WINDOWS, self.results)):
            for offset, state in window[4]:
                if state != "on":
                    continue
                steps_in += 1
                boost = [w for w in result.writes if w[1] == "bath" and w[0] == offset]
                before = [w[3] for w in result.writes if w[2] == BATHROOM and w[0] <= offset and w not in boost]
                self.assertEqual(len(boost), 1, f"morning {i} at {offset}")
                self.assertAlmostEqual(boost[0][3], round(before[-1] + 0.10, 2), msg=f"morning {i}")
        self.assertEqual(steps_in, 15)

    def test_the_ramp_never_takes_the_bathroom_down_again_after_the_boost(self):
        for i, result in enumerate(self.results):
            level = None
            for offset, who, entity, written, _ in result.writes:
                if entity == BATHROOM and level is not None and who == "wake" and offset > 0:
                    self.assertGreaterEqual(written, level, f"morning {i} at {offset}")
                if entity == BATHROOM:
                    level = written

    def test_leaving_the_bath_takes_back_what_the_boost_gave(self):
        window = ([(0, "nfn")], None, [], "off", [(300, "on"), (500, "off")], [])
        result = replay(window)
        self.assertEqual([w[3] for w in result.writes if w[1] == "bath"], [0.28, 0.18])

    def test_a_boost_in_the_middle_of_the_ramp_is_not_pulled_back(self):
        window = ([(0, "nfn")], None, [], "off", [(93, "on"), (400, "off")], [])
        result = replay(window)
        bathroom = [(off, who, level) for off, who, entity, level, _ in result.writes if entity == BATHROOM]
        self.assertEqual(bathroom[:3], [(0, "wake", 0.06), (60, "wake", 0.09), (93, "bath", 0.19)])
        self.assertTrue(all(who != "wake" for _, who, _ in bathroom[3:]))

    def test_without_the_cap_the_sync_would_have_raised_the_bedroom_over_a_bed_that_was_not_empty(self):
        uncapped = [replay(w, capped=False) for w in WINDOWS]
        broke = [r for r in uncapped if [w for w in bedroom_writes_above_standard(r) if not bed_is_empty(w[4])]]
        witness_on = [r for r in broke if [w for w in bedroom_writes_above_standard(r) if "n" in w[4]]]
        self.assertEqual(len(broke), 17)
        self.assertEqual(len(witness_on), 11)

    def test_the_cap_lowers_the_copy_29_times_in_17_mornings(self):
        lowered = [(i, bed) for i, r in enumerate(self.results) for _, _, cap, bath, bed in r.asked
                   if cap is not None and bath > cap + 0.005]
        self.assertEqual(len(lowered), 29)
        self.assertEqual(len({i for i, _ in lowered}), 17)
        self.assertEqual(sum(1 for _, bed in lowered if "n" in bed), 15)
        self.assertEqual(sum(1 for _, bed in lowered if "n" not in bed), 14)

    def test_the_cap_holds_while_the_bathroom_plays_louder_in_most_mornings(self):
        louder = [i for i, r in enumerate(self.results)
                  if any(cap is not None and bath > STANDARD[BEDROOM] for _, _, cap, _, bath in r.samples)]
        witness_on = [i for i, r in enumerate(self.results)
                      if any(cap is not None and "n" in bed and bath > STANDARD[BEDROOM] for _, bed, cap, _, bath in r.samples)]
        self.assertEqual(len(louder), 39)
        self.assertEqual(len(witness_on), 38)

    def test_once_the_bed_reads_empty_the_sync_follows_the_person(self):
        followed = [i for i, r in enumerate(self.results)
                    if [w for w in bedroom_writes_above_standard(r) if w[1] == "sync" and bed_is_empty(w[4])]]
        self.assertEqual(len(followed), 18)


if __name__ == "__main__":
    unittest.main()
