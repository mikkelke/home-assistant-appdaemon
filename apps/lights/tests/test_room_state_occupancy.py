"""Room occupancy survives publication, AD attribute serialization and lighting reads."""

import math
import time
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from test_darkness_calculator import make_app, calls_for, ROOM_ENT, BIN_ENT, SEN_ENT

import lighting_actions as actions


RAW = "binary_sensor.raw_presence"
SEMANTIC = "binary_sensor.testzone_active"


def room_app(sources=RAW, states=None, attrs=None, mirrors=False):
    args = {
        "zones": {"testzone": {"always_dark": True, "mirrors": ["mirror"] if mirrors else []}},
        "presence_sensors": {"testzone": sources},
        "room_state_helpers": {"testzone": "input_text.testzone_state"},
    }
    if mirrors:
        args["presence_sensors"]["mirror"] = "binary_sensor.mirror_presence"
    app = make_app(args=args, states=states, attrs=attrs)

    def publish(entity, **kwargs):
        app.set_state_calls.append((entity, kwargs))
        app._states[entity] = kwargs["state"]
        # AD 4.5.13 omits falsey values and serializes booleans as strings.
        app._attrs[entity] = {
            key: str(value).lower() if isinstance(value, bool) else value
            for key, value in kwargs.get("attributes", {}).items() if value
        }

    app.set_state = publish
    return app


def light_actions(app, **kwargs):
    on, off, bright_off = Mock(), Mock(), Mock()
    result = actions.apply_global_lighting(
        app, room_state_entity=ROOM_ENT, darkness_sensor=SEN_ENT,
        default_dark=kwargs.pop("default_dark", True),
        lights_on=kwargs.pop("lights_on", True), turn_on=on, turn_off=off,
        bright_lights_on=True, turn_off_bright=bright_off, **kwargs,
    )
    return result, on, off, bright_off


class OccupancyPublication(unittest.TestCase):
    def test_raw_votes_survive_ad_serialization_and_shared_reader(self):
        for raw, label, expected, reason in (
            ("on", "Occupied", True, f"source_on:{RAW}"),
            ("off", "Empty", False, "readable_sources_off"),
            ("unknown", "Unknown", None, "no_readable_sources"),
            ("unavailable", "Unknown", None, "no_readable_sources"),
            (None, "Unknown", None, "no_readable_sources"),
            ("invalid", "Unknown", None, "no_readable_sources"),
        ):
            with self.subTest(raw=raw):
                app = room_app(states={RAW: raw})
                app._recompute_all()
                self.assertEqual(app._states[ROOM_ENT], f"{label} (Dark)")
                self.assertEqual(app._attrs[ROOM_ENT]["occupied"],
                                 "unknown" if expected is None else str(expected).lower())
                self.assertEqual(app._attrs[ROOM_ENT]["occupancy_reason"], reason)
                self.assertEqual(app._attrs[ROOM_ENT]["reason"], "always_dark zone")
                self.assertIs(actions.read_occupied_from_room_state(app, ROOM_ENT), expected)
                self.assertEqual(app._states[BIN_ENT], "on")
                self.assertEqual(app._states[SEN_ENT], "dark")

    def test_no_sources_is_unknown(self):
        for sources in (None, "", [], [None]):
            with self.subTest(sources=sources):
                app = room_app(sources=sources)
                app._recompute_all()
                self.assertEqual(app._states[ROOM_ENT], "Unknown (Dark)")
                self.assertEqual(app._attrs[ROOM_ENT]["occupancy_reason"], "no_presence_config")

    def test_mixed_sources_keep_bounded_union_and_positive_precedence(self):
        other = "binary_sensor.other_presence"
        for left, right, expected in (("unknown", "off", False), ("off", "unavailable", False),
                                      ("off", "on", True), ("unknown", "on", True),
                                      ("unknown", "unavailable", None)):
            with self.subTest(left=left, right=right):
                app = room_app(sources=[RAW, other], states={RAW: left, other: right})
                app._recompute_all()
                self.assertIs(actions.read_occupied_from_room_state(app, ROOM_ENT), expected)

    def test_read_exception_is_unknown_but_other_positive_source_wins(self):
        app = room_app()
        read = app.get_state
        app.get_state = lambda entity, **kwargs: (_ for _ in ()).throw(RuntimeError()) if entity == RAW else read(entity, **kwargs)
        app._recompute_all()
        self.assertIsNone(actions.read_occupied_from_room_state(app, ROOM_ENT))
        app.presence_sensors["testzone"] = [RAW, "input_boolean.other"]
        app._states["input_boolean.other"] = "on"
        app._recompute_all()
        self.assertTrue(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_mirror_has_independent_occupancy_and_helper_tracks_recovery(self):
        app = room_app(states={RAW: "unknown", "binary_sensor.mirror_presence": "on"}, mirrors=True)
        for raw, label in (("unknown", "Unknown"), ("on", "Occupied"), ("off", "Empty")):
            app._states[RAW] = raw
            app._recompute_all()
            self.assertEqual(app._states[ROOM_ENT], f"{label} (Dark)")
            self.assertEqual(app._states["input_text.testzone_state"], f"{label} (Dark)")
            self.assertEqual(app._states["sensor.room_state_mirror"], "Occupied (Dark)")

    def test_unchanged_unknown_refreshes_timestamp_and_keeps_brightness(self):
        app = room_app(states={RAW: "unknown"})
        with patch("darkness_calculator.time.time", return_value=1000):
            app._recompute_all()
        with patch("darkness_calculator.time.time", return_value=1090):
            app._recompute_all()
        self.assertEqual(app._attrs[ROOM_ENT]["presence_ts"], 1090)
        self.assertEqual(len(calls_for(app, BIN_ENT)), 1)
        self.assertEqual(len(calls_for(app, SEN_ENT)), 1)


class SemanticSourcePolicy(unittest.TestCase):
    def app(self, state, age=0, witnesses=None, stale=None):
        computed = (datetime.now(timezone.utc) - timedelta(seconds=age)).isoformat()
        app = room_app(sources=SEMANTIC, states={SEMANTIC: state},
                       attrs={SEMANTIC: {"computed_at": computed, "stale": stale}})
        if witnesses is not None:
            app.get_app = lambda name: SimpleNamespace(zones={
                "testzone": [{"entity": entity} for entity in witnesses]
            })
            app._states.update(witnesses)
        return app

    def test_fresh_unknown_preserves_recovery_even_when_witness_is_off(self):
        app = self.app("unknown", witnesses={RAW: "off"})
        app._recompute_all()
        self.assertIsNone(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_semantic_recovery_publishes_known_off(self):
        app = self.app("unknown", witnesses={RAW: "off"})
        app._recompute_all()
        app._states[SEMANTIC] = "off"
        app._recompute_all()
        self.assertFalse(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_expired_publication_uses_live_witnesses_or_unknown(self):
        for stale_state, witness, expected in (("off", "on", True), ("on", "off", False),
                                                ("off", "unavailable", None)):
            with self.subTest(stale_state=stale_state, witness=witness):
                app = self.app(stale_state, age=600, witnesses={RAW: witness})
                app._recompute_all()
                self.assertIs(actions.read_occupied_from_room_state(app, ROOM_ENT), expected)
        app = self.app("off", age=600)
        app._recompute_all()
        self.assertIsNone(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_missing_semantic_timestamp_rechecks_witnesses(self):
        app = self.app("off", witnesses={RAW: "on"})
        app._attrs[SEMANTIC] = {}
        app._recompute_all()
        self.assertTrue(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_legacy_stale_off_checks_live_evidence(self):
        app = self.app("off", stale="true", witnesses={RAW: "unavailable"})
        app._recompute_all()
        self.assertIsNone(actions.read_occupied_from_room_state(app, ROOM_ENT))


class LightingOccupancyRead(unittest.TestCase):
    def test_only_recognized_attributes_establish_occupancy(self):
        for value, expected in ((True, True), (False, False), ("true", True), ("false", False),
                                ("on", True), ("off", False), (1, True), (0, False),
                                ("unknown", None), ("unavailable", None), ("broken", None), ("", None)):
            with self.subTest(value=value):
                app = room_app()
                app._states[ROOM_ENT] = "Empty (Dark)"
                app._attrs[ROOM_ENT] = {"presence_ts": time.time(), "occupied": value}
                self.assertIs(actions.read_occupied_from_room_state(app, ROOM_ENT), expected)

    def test_absent_attribute_uses_fresh_legacy_label_only(self):
        app = room_app()
        app._attrs[ROOM_ENT] = {"presence_ts": time.time()}
        for label, expected in (("Empty (Dark)", False), ("Occupied (Bright)", True),
                                ("Unknown (Dark)", None), ("unoccupied", None)):
            app._states[ROOM_ENT] = label
            self.assertIs(actions.read_occupied_from_room_state(app, ROOM_ENT), expected)

    def test_invalid_or_old_publication_never_establishes_occupancy(self):
        app = room_app()
        app._states[ROOM_ENT] = "Empty (Dark)"
        for timestamp in (None, "broken", math.nan, math.inf, time.time() - 301, time.time() + 60):
            with self.subTest(timestamp=timestamp):
                app._attrs[ROOM_ENT] = {"presence_ts": timestamp, "occupied": "false"}
                self.assertIsNone(actions.read_occupied_from_room_state(app, ROOM_ENT))

    def test_unknown_never_acts_for_either_brightness_or_default(self):
        app = room_app(states={RAW: "unknown"})
        for dark in (True, False):
            app._publish_one("testzone", dark, {})
            for default in (True, False):
                for lights_on in (True, False):
                    result, *callbacks = light_actions(app, default_dark=default, lights_on=lights_on)
                    self.assertEqual(result, "noop_unknown")
                    for callback in callbacks:
                        callback.assert_not_called()

    def test_explicit_local_occupancy_is_not_replaced_by_room_label(self):
        app = room_app(states={RAW: "off"})
        app._recompute_all()
        result, *callbacks = light_actions(app, occupied=None)
        self.assertEqual(result, "noop_unknown")
        for callback in callbacks:
            callback.assert_not_called()
        result, on, off, _ = light_actions(app, occupied=True, lights_on=False)
        self.assertEqual(result, "on_dark")
        on.assert_called_once()
        off.assert_not_called()

    def test_direct_primitive_preserves_unknown(self):
        on, off = Mock(), Mock()
        result = actions.apply_occupied_brightness(
            occupied=None, is_dark_for_on=False, is_dark_for_off=False,
            lights_on=True, turn_on=on, turn_off=off,
        )
        self.assertEqual(result, "noop_unknown")
        on.assert_not_called()
        off.assert_not_called()

    def test_known_recovery_restores_normal_on_and_off_actions(self):
        app = room_app()
        for raw, lights_on, expected in (("unknown", True, "noop_unknown"),
                                         ("off", True, "off_vacant"),
                                         ("on", False, "on_dark")):
            app._states[RAW] = raw
            app._recompute_all()
            result, on, off, _ = light_actions(app, lights_on=lights_on)
            self.assertEqual(result, expected)
            self.assertEqual(on.call_count, expected == "on_dark")
            self.assertEqual(off.call_count, expected == "off_vacant")


if __name__ == "__main__":
    unittest.main()
