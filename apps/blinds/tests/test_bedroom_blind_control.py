"""BedroomBlindControl's voice path: the curtain_command event {position, room}.

The wall-remote gestures predate this file and are only smoke-tested here. What is pinned: how
the position names map onto the yaml positions per room, what is asked of each blind's owner
(a "manual" request with the reason "voice: <position>", so a voice move starts the same hold as
a hand move), what is written directly when an owner is missing or raises, "automatic" handing
the blinds back without moving them, and what is ignored rather than guessed.

POSITIONS ARE IN THE DEVICE'S FRAME (100 = covering the window, 0 = window clear) - see the
banner in bedroom_blind_control.yaml.
"""

from __future__ import annotations

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

# The harness stubs the appdaemon modules and puts apps/blinds on sys.path.
from owner_harness import make_owner, positions_commanded  # noqa: E402

import bedroom_blind_control as bbc  # noqa: E402

BEDROOM = "cover.bedroom_blind"
BATHROOM = "cover.bathroom_blind"

ARGS = {
    "device_id": "remote",
    "target_cover": BEDROOM,
    "bathroom_blind_entity": BATHROOM,
    "owner_app": "BedroomBlindOwner",
    "bathroom_owner_app": "BathroomBlindOwner",
    "close_position": 100,
    "partial_position": 38,
    "bathroom_partial_position": 40,
    "open_position": 0,
}


class FakeOwner:
    """Records what the control app asks of a blind owner; answers like BedroomBlindOwner."""

    def __init__(self, granted=True, boom=False):
        self.calls = []
        self.granted = granted
        self.boom = boom

    def request(self, source, position, *, reason="", ttl_min=None):
        if self.boom:
            raise RuntimeError("owner broke")
        self.calls.append(("request", source, position, reason))
        return {"granted": self.granted, "moved": self.granted, "winner": "manual",
                "position": position, "manual_until": None}

    def withdraw(self, source, reason=""):
        if self.boom:
            raise RuntimeError("owner broke")
        self.calls.append(("withdraw", source, reason))


def make_app(args=None, owners=None):
    """BedroomBlindControl with the real initialize() against a faked AppDaemon surface."""
    app = bbc.BedroomBlindControl.__new__(bbc.BedroomBlindControl)
    app.args = {**ARGS, **(args or {})}
    app.owners = owners if owners is not None else {
        "BedroomBlindOwner": FakeOwner(), "BathroomBlindOwner": FakeOwner(),
    }
    app.logged = []
    app.errors = []
    app.services = []
    app.listeners = []
    app.scheduled = []
    app.get_app = lambda name: app.owners.get(name)
    app.get_state = lambda entity, attribute=None, **kw: (
        {BEDROOM: 38, BATHROOM: 40}.get(entity) if attribute == "current_position" else None
    )
    app.log = lambda msg, level="INFO": app.logged.append((level, str(msg)))
    app.error = lambda msg, *a, **kw: app.errors.append(str(msg))
    app.listen_event = lambda cb, event, **kw: app.listeners.append((cb, event, kw))
    app.call_service = lambda service, **kw: app.services.append((service, kw))
    app.run_in = lambda cb, delay, **kw: app.scheduled.append((cb, delay, kw))
    app.initialize()
    return app


def fire(app, **data):
    app._on_curtain_command("curtain_command", data, {})


def voice_lines(app):
    return [msg for _lvl, msg in app.logged if msg.startswith("Voice:")]


class Listening(unittest.TestCase):
    def test_the_voice_event_is_listened_for_next_to_the_wall_remote(self):
        app = make_app()
        events = [event for _cb, event, _kw in app.listeners]
        self.assertEqual(events.count("zwave_js_value_notification"), 2)
        self.assertEqual(events.count("curtain_command"), 1)
        callback = next(cb for cb, event, _kw in app.listeners if event == "curtain_command")
        self.assertEqual(callback, app._on_curtain_command)

    def test_a_bad_open_position_stops_the_app_before_anything_listens(self):
        for bad in (-1, 101):
            with self.subTest(open_position=bad):
                app = make_app(args={"open_position": bad})
                self.assertEqual(app.listeners, [])
                self.assertTrue(any("open_position" in e for e in app.errors))

    def test_the_owner_names_default_to_the_two_owner_apps(self):
        args = {k: v for k, v in ARGS.items() if k not in ("owner_app", "bathroom_owner_app", "open_position")}
        app = bbc.BedroomBlindControl.__new__(bbc.BedroomBlindControl)
        app.args = args
        app.log = app.error = lambda *a, **kw: None
        app.get_state = lambda *a, **kw: None
        app.listen_event = lambda *a, **kw: None
        app.initialize()
        self.assertEqual((app.owner_app, app.bathroom_owner_app, app.open_position),
                         ("BedroomBlindOwner", "BathroomBlindOwner", 0))


class PositionNames(unittest.TestCase):
    def test_each_name_maps_to_the_yaml_position_of_that_room(self):
        cases = (
            ("bedroom", "BedroomBlindOwner", {"fully_up": 100, "standard": 38, "fully_open": 0}),
            ("bathroom", "BathroomBlindOwner", {"fully_up": 100, "standard": 40, "fully_open": 0}),
        )
        for room, owner_name, want in cases:
            for word, position in want.items():
                with self.subTest(room=room, position=word):
                    app = make_app()
                    fire(app, position=word, room=room)
                    self.assertEqual(app.owners[owner_name].calls,
                                     [("request", "manual", position, f"voice: {word}")])
                    other = "BathroomBlindOwner" if room == "bedroom" else "BedroomBlindOwner"
                    self.assertEqual(app.owners[other].calls, [])
                    self.assertEqual(app.services, [])
                    self.assertEqual(voice_lines(app), [f"Voice: {room} -> {word} ({position}%)"])

    def test_the_positions_come_from_the_yaml_arguments(self):
        app = make_app(args={"close_position": 99, "partial_position": 36,
                             "bathroom_partial_position": 41, "open_position": 3})
        fire(app, position="fully_up")
        fire(app, position="standard")
        fire(app, position="fully_open")
        self.assertEqual([c[2] for c in app.owners["BedroomBlindOwner"].calls], [99, 36, 3])
        self.assertEqual([c[2] for c in app.owners["BathroomBlindOwner"].calls], [99, 41, 3])


class Rooms(unittest.TestCase):
    def test_both_asks_the_bedroom_owner_and_the_bathroom_owner(self):
        app = make_app()
        fire(app, position="standard", room="both")
        self.assertEqual(app.owners["BedroomBlindOwner"].calls, [("request", "manual", 38, "voice: standard")])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [("request", "manual", 40, "voice: standard")])
        self.assertEqual(voice_lines(app), ["Voice: bedroom -> standard (38%)", "Voice: bathroom -> standard (40%)"])

    def test_no_room_means_both(self):
        for data in ({"position": "fully_up"}, {"position": "fully_up", "room": ""}, {"position": "fully_up", "room": None}):
            with self.subTest(data=data):
                app = make_app()
                fire(app, **data)
                self.assertEqual([c[2] for c in app.owners["BedroomBlindOwner"].calls], [100])
                self.assertEqual([c[2] for c in app.owners["BathroomBlindOwner"].calls], [100])

    def test_case_and_spacing_do_not_matter(self):
        app = make_app()
        fire(app, position=" Standard ", room="BEDROOM")
        self.assertEqual(app.owners["BedroomBlindOwner"].calls, [("request", "manual", 38, "voice: standard")])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [])

    def test_a_blind_that_is_not_configured_is_skipped(self):
        app = make_app(args={"bathroom_blind_entity": None})
        fire(app, position="standard", room="both")
        self.assertEqual([c[2] for c in app.owners["BedroomBlindOwner"].calls], [38])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [])
        self.assertEqual(app.services, [])
        self.assertTrue(any(lvl == "WARNING" and "bathroom" in msg for lvl, msg in app.logged))


class Automatic(unittest.TestCase):
    def test_automatic_withdraws_the_manual_hold_and_moves_nothing(self):
        app = make_app()
        fire(app, position="automatic", room="both")
        for name in ("BedroomBlindOwner", "BathroomBlindOwner"):
            self.assertEqual(app.owners[name].calls, [("withdraw", "manual", "voice: automatic")])
        self.assertEqual(app.services, [])
        self.assertEqual(app.scheduled, [])
        self.assertEqual(voice_lines(app), ["Voice: bedroom -> automatic (manual hold withdrawn)",
                                            "Voice: bathroom -> automatic (manual hold withdrawn)"])

    def test_automatic_for_one_room_leaves_the_other_hold_alone(self):
        app = make_app()
        fire(app, position="automatic", room="bathroom")
        self.assertEqual(app.owners["BedroomBlindOwner"].calls, [])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [("withdraw", "manual", "voice: automatic")])

    def test_automatic_without_an_owner_writes_nothing(self):
        app = make_app(owners={})
        fire(app, position="automatic")
        self.assertEqual(app.services, [])
        self.assertEqual(len([1 for lvl, _ in app.logged if lvl == "WARNING"]), 2)


class Ignored(unittest.TestCase):
    def test_an_unknown_position_or_room_asks_nobody_and_writes_nothing(self):
        bad = (
            {"position": "half", "room": "both"},
            {"position": "standard", "room": "kitchen"},
            {"position": "standard", "room": 7},
            {"position": "open"},
            {"position": ["standard"]},
            {"position": None},
            {"room": "both"},
            {},
        )
        for data in bad:
            with self.subTest(data=data):
                app = make_app()
                fire(app, **data)
                for name in ("BedroomBlindOwner", "BathroomBlindOwner"):
                    self.assertEqual(app.owners[name].calls, [])
                self.assertEqual(app.services, [])
                self.assertEqual([lvl for lvl, msg in app.logged if "ignoring curtain_command" in msg], ["WARNING"])

    def test_event_data_that_is_not_a_dict_is_ignored(self):
        app = make_app()
        app._on_curtain_command("curtain_command", None, {})
        app._on_curtain_command("curtain_command", "standard", {})
        self.assertEqual(app.owners["BedroomBlindOwner"].calls, [])
        self.assertEqual(app.services, [])


class WithoutAnOwner(unittest.TestCase):
    def test_a_missing_owner_means_a_direct_write_for_that_room_only(self):
        app = make_app(owners={"BathroomBlindOwner": FakeOwner()})
        fire(app, position="standard", room="both")
        self.assertEqual(app.services, [("cover/set_cover_position", {"entity_id": BEDROOM, "position": 38})])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [("request", "manual", 40, "voice: standard")])
        self.assertEqual(voice_lines(app), ["Voice: bedroom -> standard (38%), direct write",
                                            "Voice: bathroom -> standard (40%)"])

    def test_the_direct_write_checks_arrival_later(self):
        app = make_app(owners={})
        fire(app, position="fully_up", room="bathroom")
        self.assertEqual(app.services, [("cover/set_cover_position", {"entity_id": BATHROOM, "position": 100})])
        (callback, delay, kw), = app.scheduled
        self.assertEqual((callback, delay), (app._verify_position, app.verification_delay))
        self.assertEqual((kw["entity_id"], kw["expected_position"]), (BATHROOM, 100))

    def test_an_owner_that_raises_costs_a_direct_write_not_the_other_room(self):
        app = make_app(owners={"BedroomBlindOwner": FakeOwner(boom=True), "BathroomBlindOwner": FakeOwner()})
        fire(app, position="fully_open")
        self.assertEqual(app.services, [("cover/set_cover_position", {"entity_id": BEDROOM, "position": 0})])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [("request", "manual", 0, "voice: fully_open")])
        self.assertTrue(any(lvl == "WARNING" and "falling back" in msg for lvl, msg in app.logged))

    def test_an_owner_that_raises_on_automatic_does_not_stop_the_other_room(self):
        app = make_app(owners={"BedroomBlindOwner": FakeOwner(boom=True), "BathroomBlindOwner": FakeOwner()})
        fire(app, position="automatic")
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [("withdraw", "manual", "voice: automatic")])
        self.assertTrue(any("bedroom -> automatic failed" in e for e in app.errors))

    def test_an_owner_that_does_not_grant_is_logged_not_overwritten(self):
        app = make_app(owners={"BedroomBlindOwner": FakeOwner(granted=False), "BathroomBlindOwner": FakeOwner()})
        fire(app, position="standard", room="bedroom")
        self.assertEqual(app.services, [])
        self.assertEqual(voice_lines(app), ["Voice: bedroom -> standard (38%), not granted"])


class WallRemoteIsUntouched(unittest.TestCase):
    def test_a_close_hold_still_asks_the_bedroom_owner_as_manual_with_its_old_reason(self):
        app = make_app()
        app._execute_close_hold({})
        self.assertEqual(app.owners["BedroomBlindOwner"].calls, [("request", "manual", 100, "CLOSE_HOLD")])
        self.assertEqual(app.owners["BathroomBlindOwner"].calls, [])

    def test_the_bedroom_owner_lookup_is_unchanged(self):
        app = make_app()
        self.assertIs(app._blind_owner(), app.owners["BedroomBlindOwner"])
        self.assertIs(app._blind_owner("BathroomBlindOwner"), app.owners["BathroomBlindOwner"])
        self.assertIsNone(app._blind_owner("NoSuchOwner"))


class WithTheRealOwners(unittest.TestCase):
    """The owner sensors read what the voice acceptance checks look for."""

    def setUp(self):
        self.bedroom = make_owner(position=100)
        self.bathroom = make_owner(
            args={"cover_entity": BATHROOM, "status_entity": "sensor.bathroom_blind_owner"}, position=100,
        )
        self.app = make_app(owners={"BedroomBlindOwner": self.bedroom, "BathroomBlindOwner": self.bathroom})

    @staticmethod
    def published(owner):
        entity, kw = owner.set_state_calls[-1]
        return entity, kw["state"], kw["attributes"]

    def test_standard_for_both_holds_both_blinds_as_manual_voice_and_moves_them(self):
        fire(self.app, position="standard", room="both")
        self.assertEqual(positions_commanded(self.bedroom), [38])
        self.assertEqual(positions_commanded(self.bathroom), [40])
        for owner, entity in ((self.bedroom, "sensor.bedroom_blind_owner"), (self.bathroom, "sensor.bathroom_blind_owner")):
            with self.subTest(entity=entity):
                published_entity, state, attrs = self.published(owner)
                self.assertEqual((published_entity, state), (entity, "manual"))
                self.assertTrue(attrs["reason"].startswith("voice: standard"), attrs["reason"])
                self.assertTrue(attrs["manual_pause_until"])
                self.assertTrue(owner.manual_hold_active())

    def test_automatic_after_that_hands_both_blinds_back_and_moves_nothing(self):
        fire(self.app, position="standard", room="both")
        moved = (positions_commanded(self.bedroom), positions_commanded(self.bathroom))
        fire(self.app, position="automatic", room="both")
        for owner in (self.bedroom, self.bathroom):
            self.assertFalse(owner.manual_hold_active())
            self.assertEqual(self.published(owner)[1], "idle")
        self.assertEqual((positions_commanded(self.bedroom), positions_commanded(self.bathroom)), moved)

    def test_closing_one_room_leaves_the_other_alone(self):
        fire(self.app, position="fully_up", room="bedroom")
        self.assertEqual(positions_commanded(self.bedroom), [])  # already at 100: held, not moved
        self.assertTrue(self.bedroom.manual_hold_active())
        self.assertFalse(self.bathroom.manual_hold_active())
        self.assertEqual(self.bathroom.set_state_calls[-1][1]["state"], "idle")


class ConfigStaysInStep(unittest.TestCase):
    def yaml(self):
        return (Path(__file__).resolve().parents[1] / "bedroom_blind_control.yaml").read_text()

    def test_the_voice_arguments_are_in_the_yaml_with_the_owner_names_the_owner_yaml_defines(self):
        text = self.yaml()
        self.assertRegex(text, r"(?m)^  open_position: 0\s*$")
        self.assertRegex(text, r"(?m)^  bathroom_owner_app: BathroomBlindOwner\s*$")
        owners = (Path(__file__).resolve().parents[1] / "bedroom_blind_owner.yaml").read_text()
        for name in re.findall(r"(?m)^  (?:owner_app|bathroom_owner_app): (\w+)\s*$", text):
            self.assertRegex(owners, rf"(?m)^{name}:\s*$")

    def test_the_voice_positions_match_the_yaml_numbers(self):
        text = self.yaml()
        for key, value in (("close_position", 100), ("partial_position", 38),
                           ("bathroom_partial_position", 40), ("open_position", 0)):
            self.assertRegex(text, rf"(?m)^  {key}: {value}\s*$")


if __name__ == "__main__":
    unittest.main()
