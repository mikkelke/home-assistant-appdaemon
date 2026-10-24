"""EntryTruth's secure/not-secure/unknown contract, including LockHealth fallback."""

import sys
import types
import unittest
from pathlib import Path


_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from entry_truth import EntryTruth  # noqa: E402


SOURCE = "sensor.apartment_lock"
FALLBACK = "lock.yale_bt"
DOOR = "binary_sensor.apartment_door_open"
PUBLISH = "binary_sensor.apartment_entry_secure"
MISSING = object()


def make_app(door="off", lock="locked", fallback="locked", source_attrs=None):
    app = EntryTruth.__new__(EntryTruth)
    app.lock_source = SOURCE
    app.lock_fallback = FALLBACK
    app.door = DOOR
    app.publish_entity = PUBLISH
    app.states = {SOURCE: lock, FALLBACK: fallback}
    if door is not MISSING:
        app.states[DOOR] = door
    app.source_attrs = source_attrs or {}
    app.reads = []
    app.publications = []
    app.logs = []

    async def get_state(entity, attribute=None):
        app.reads.append((entity, attribute))
        if attribute == "all":
            return {"state": app.states.get(entity), "attributes": app.source_attrs}
        return app.states.get(entity)

    async def set_state(entity, **kwargs):
        # Model the relevant AppDaemon 4.5.13 omission: False/None/0 disappear.
        attrs = {key: value for key, value in kwargs["attributes"].items()
                 if value is not None and value is not False and value != 0}
        app.publications.append((entity, dict(kwargs, attributes=attrs)))

    app.get_state = get_state
    app.set_state = set_state
    app.log = lambda message, **kwargs: app.logs.append((message, kwargs))
    return app


class EntryTruthTests(unittest.IsolatedAsyncioTestCase):
    async def evaluate(self, app):
        await app._eval()
        self.assertEqual(len(app.publications), 1)
        entity, publication = app.publications[0]
        self.assertEqual(entity, PUBLISH)
        self.assertTrue(publication["replace"])
        self.assertFalse(any(kwargs.get("level") == "ERROR" for _, kwargs in app.logs))
        return publication

    async def test_contact_and_lock_truth_table(self):
        locks = ("locked", "unlocked", "locking", "unlocking", "jammed", "open",
                 "unknown", "unavailable", None)
        # Columns follow locks above. Missing/invalid evidence is never a closed door.
        expected = {
            "on": ("off",) * 9,
            "off": ("on", "off", "unknown", "unknown", "unknown", "unknown",
                    "unknown", "unknown", "unknown"),
            "unknown": ("unknown", "off", "unknown", "unknown", "unknown", "unknown",
                        "unknown", "unknown", "unknown"),
        }
        for door in ("on", "off", "unknown", "unavailable", None, MISSING, "closed"):
            row = expected.get(door, expected["unknown"])
            for lock, state in zip(locks, row):
                with self.subTest(door=door, lock=lock):
                    # Keep the fallback identical to isolate the truth-table result.
                    publication = await self.evaluate(make_app(door, lock, fallback=lock))
                    self.assertEqual(publication["state"], state)
                    self.assertTrue(publication["attributes"]["reason"])

    async def test_closed_contact_stays_visible_despite_false_attribute_filtering(self):
        publication = await self.evaluate(make_app())
        self.assertEqual(publication["state"], "on")
        self.assertEqual(publication["attributes"]["door_state"], "off")
        self.assertEqual(publication["attributes"]["door_open"], "false")

    async def test_unknown_contact_does_not_publish_door_closed(self):
        for door, normalized in (("unknown", "unknown"), ("unavailable", "unavailable"),
                                 (None, "unknown"), (MISSING, "unknown"), ("closed", "unknown")):
            with self.subTest(door=door):
                publication = await self.evaluate(make_app(door))
                self.assertEqual(publication["state"], "unknown")
                attrs = publication["attributes"]
                self.assertEqual(attrs["door_state"], normalized)
                self.assertEqual(attrs["door_open"], "unknown")
                self.assertIn(f"door contact reports {normalized}", attrs["reason"])

    async def test_arbitrated_lock_owns_decision_and_provenance(self):
        app = make_app(lock="locked", fallback="unlocked",
                       source_attrs={"bt_state": "unlocked", "cloud_state": "locked"})
        publication = await self.evaluate(app)
        attrs = publication["attributes"]
        self.assertEqual(publication["state"], "on")
        self.assertEqual(attrs["lock_source_used"], "arbitrated")
        self.assertEqual(attrs["bt_state"], "unlocked")
        self.assertEqual(attrs["cloud_state"], "locked")
        self.assertEqual(attrs["source_entities"], [SOURCE, FALLBACK, DOOR])
        self.assertNotIn((FALLBACK, None), app.reads)
        self.assertIn("computed_at", attrs)

    async def test_unavailable_arbitrator_uses_ble_fallback(self):
        for lock in (None, "unknown", "unavailable"):
            for fallback, expected in (("locked", "on"), ("unlocked", "off"),
                                       ("jammed", "unknown"), (None, "unknown")):
                with self.subTest(lock=lock, fallback=fallback):
                    publication = await self.evaluate(make_app(lock=lock, fallback=fallback))
                    self.assertEqual(publication["state"], expected)
                    attrs = publication["attributes"]
                    self.assertEqual(attrs["lock_source_used"], "fallback_ble")
                    self.assertEqual(attrs["lock_state"], fallback or "unknown")
                    self.assertIn("fallback_ble", attrs["reason"])

    async def test_transitional_or_jammed_arbitrator_does_not_switch_to_ble(self):
        for lock in ("locking", "unlocking", "jammed"):
            with self.subTest(lock=lock):
                app = make_app(lock=lock, fallback="locked")
                publication = await self.evaluate(app)
                self.assertEqual(publication["state"], "unknown")
                self.assertEqual(publication["attributes"]["lock_source_used"], "arbitrated")
                self.assertNotIn((FALLBACK, None), app.reads)

    async def test_contact_outage_and_recovery_replace_previous_result(self):
        app = make_app()
        for door, expected in (("off", "on"), ("unavailable", "unknown"),
                               ("on", "off"), ("off", "on")):
            app.states[DOOR] = door
            app.publications.clear()
            publication = await self.evaluate(app)
            self.assertEqual(publication["state"], expected)

    async def test_lockhealth_recovery_takes_over_from_ble(self):
        app = make_app(lock="unavailable", fallback="unlocked")
        publication = await self.evaluate(app)
        self.assertEqual(publication["state"], "off")
        self.assertEqual(publication["attributes"]["lock_source_used"], "fallback_ble")
        app.states[SOURCE] = "locked"
        app.source_attrs = {"bt_state": "unlocked", "cloud_state": "locked"}
        app.publications.clear()
        app.reads.clear()
        publication = await self.evaluate(app)
        self.assertEqual(publication["state"], "on")
        self.assertEqual(publication["attributes"]["lock_source_used"], "arbitrated")
        self.assertNotIn((FALLBACK, None), app.reads)


if __name__ == "__main__":
    unittest.main()
