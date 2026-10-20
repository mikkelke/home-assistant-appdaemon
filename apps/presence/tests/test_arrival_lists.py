# tests/test_arrival_lists.py - ArrivalLists: which arrivals push, empty lists stay quiet,
# cooldown, and the todo.get_items envelope parse.
# Run from repo root: python3 -m unittest discover -s apps/presence/tests -q

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

import arrival_lists as al  # noqa: E402

NAMES = {
    "todo.husk_hjemme": "Husk hjemme",
    "todo.husk_kontor": "Husk kontor",
    "todo.firma_indkob": "Firma indkøb",
}


def envelope(entity_id, summaries, completed=()):
    items = [{"summary": s, "status": "needs_action"} for s in summaries]
    items += [{"summary": s, "status": "completed"} for s in completed]
    return {"success": True, "result": {"response": {entity_id: {"items": items}}}}


def make_app(lists):
    app = al.ArrivalLists.__new__(al.ArrivalLists)
    app.person = "person.mikkel"
    app.recipient = "mikkel"
    app.category = "arrival_lists"
    app.places = {"home": ["todo.husk_hjemme"], "Office": ["todo.husk_kontor", "todo.firma_indkob"]}
    app.cooldown_s = 7200
    app.link = None
    app._last_push = {}
    app._notifier = MagicMock()
    app._notifier.notify = AsyncMock(return_value=1)
    app.log = MagicMock()

    async def call_service(service, entity_id, status, return_response):
        return envelope(entity_id, lists.get(entity_id, []))

    async def get_state(entity_id, attribute=None):
        return NAMES[entity_id]

    app.call_service = call_service
    app.get_state = get_state
    app.tasks = []
    app.create_task = lambda coro: app.tasks.append(coro)
    return app


def run_tasks(app):
    for coro in app.tasks:
        asyncio.run(coro)
    app.tasks.clear()


class ArrivalListsTest(unittest.TestCase):
    def test_home_arrival_pushes_open_items_to_mikkel(self):
        app = make_app({"todo.husk_hjemme": ["Vand blomster"]})
        app._on_person_change("person.mikkel", "state", "not_home", "home", {})
        run_tasks(app)
        kwargs = app._notifier.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "Husk hjemme")
        self.assertEqual(kwargs["message"], "• Vand blomster")
        self.assertEqual(kwargs["target"], ["mikkel"])
        self.assertEqual(kwargs["category"], "arrival_lists")

    def test_office_skips_empty_list(self):
        app = make_app({"todo.husk_kontor": [], "todo.firma_indkob": ["Kaffe", "Mælk"]})
        app._on_person_change("person.mikkel", "state", "not_home", "Office", {})
        run_tasks(app)
        kwargs = app._notifier.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "Firma indkøb")
        self.assertEqual(kwargs["message"], "• Kaffe\n• Mælk")

    def test_office_with_both_lists_sends_one_push_with_sections(self):
        app = make_app({"todo.husk_kontor": ["Lader"], "todo.firma_indkob": ["Kaffe"]})
        app._on_person_change("person.mikkel", "state", "not_home", "Office", {})
        run_tasks(app)
        app._notifier.notify.assert_called_once()
        kwargs = app._notifier.notify.call_args.kwargs
        self.assertEqual(kwargs["title"], "Husk kontor + Firma indkøb")
        self.assertEqual(kwargs["message"], "Husk kontor\n• Lader\n\nFirma indkøb\n• Kaffe")

    def test_all_lists_empty_sends_nothing(self):
        app = make_app({})
        app._on_person_change("person.mikkel", "state", "not_home", "Office", {})
        run_tasks(app)
        app._notifier.notify.assert_not_called()

    def test_unconfigured_place_and_same_state_are_ignored(self):
        app = make_app({"todo.husk_hjemme": ["x"]})
        app._on_person_change("person.mikkel", "state", "home", "not_home", {})
        app._on_person_change("person.mikkel", "state", "home", "home", {})
        self.assertEqual(app.tasks, [])

    def test_cooldown_blocks_second_arrival(self):
        app = make_app({"todo.husk_hjemme": ["x"]})
        app._on_person_change("person.mikkel", "state", "not_home", "home", {})
        app._on_person_change("person.mikkel", "state", "not_home", "home", {})
        self.assertEqual(len(app.tasks), 1)
        run_tasks(app)

    def test_open_items_ignores_completed_and_bad_envelopes(self):
        env = envelope("todo.x", ["a"], completed=["b"])
        self.assertEqual(al.open_items(env, "todo.x"), ["a"])
        self.assertIsNone(al.open_items({"success": False}, "todo.x"))
        self.assertIsNone(al.open_items(None, "todo.x"))


if __name__ == "__main__":
    unittest.main()
