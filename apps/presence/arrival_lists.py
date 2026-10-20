"""
ArrivalLists - when a person arrives at a place, push the open items of that place's to-do lists.

`places` maps a person state (a zone name as HA writes it: "home", "Office") to the
to-do lists to read on arrival. A list with no open items is left out; when every list is
empty nothing is sent. The arrival has to hold for `settle_s` seconds, and a place pushes at
most once per `cooldown_min`, so a tracker flapping at a zone edge sends one push.
"""

from __future__ import annotations

import asyncio
import time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def open_items(envelope, entity_id):
    """Summaries of the open items in AppDaemon's todo.get_items response envelope."""
    if not isinstance(envelope, dict) or not envelope.get("success"):
        return None
    response = (envelope.get("result") or {}).get("response") or {}
    items = (response.get(entity_id) or {}).get("items") or []
    return [i["summary"] for i in items if i.get("status") == "needs_action" and i.get("summary")]


def build_message(lists):
    """Title and body for [(list name, [item, ...]), ...], empty lists already removed."""
    title = " + ".join(name for name, _ in lists)
    if len(lists) == 1:
        return title, "\n".join(f"• {item}" for item in lists[0][1])
    blocks = [name + "\n" + "\n".join(f"• {item}" for item in items) for name, items in lists]
    return title, "\n\n".join(blocks)


class ArrivalLists(hass.Hass):
    def initialize(self):
        a = self.args
        self.person = a["person"]
        self.recipient = a["recipient"]
        self.category = a.get("category")
        self.places = a["places"]
        self.cooldown_s = float(a.get("cooldown_min", 120)) * 60
        self.link = a.get("link")

        self._notifier = self.get_app("MobileNotifier")
        self._last_push = {}

        self.listen_state(self._on_person_change, self.person, duration=int(a.get("settle_s", 60)))
        self.log(f"ArrivalLists initialized for {self.person}: {', '.join(self.places)}", level="INFO")

    def _on_person_change(self, entity, attribute, old, new, kwargs):
        if new == old or new not in self.places:
            return
        now = time.time()
        if now - self._last_push.get(new, 0) < self.cooldown_s:
            self.log(f"ArrivalLists: arrived at {new}, inside cooldown, no push", level="INFO")
            return
        self._last_push[new] = now
        self.create_task(self._push(new))

    async def _push(self, place):
        lists = []
        for entity_id in self.places[place]:
            items = await self._open_items(entity_id)
            if items:
                name = await self.get_state(entity_id, attribute="friendly_name") or entity_id
                lists.append((name, items))
        if not lists:
            self.log(f"ArrivalLists: arrived at {place}, no open items", level="INFO")
            return
        title, message = build_message(lists)
        sent = await self._notifier.notify(
            title=title,
            message=message,
            target=[self.recipient],
            category=self.category,
            data={"tag": f"arrival_lists_{place.lower()}"},
            link=self.link,
        )
        count = sum(len(items) for _, items in lists)
        self.log(f"ArrivalLists: arrived at {place}, pushed {count} item(s) to {sent} device(s)", level="INFO")

    async def _open_items(self, entity_id):
        try:
            envelope = await asyncio.wait_for(
                self.call_service("todo/get_items", entity_id=entity_id, status=["needs_action"],
                                  return_response=True), timeout=10)
        except Exception as e:
            self.log(f"ArrivalLists: reading {entity_id} failed: {e}", level="WARNING")
            return None
        items = open_items(envelope, entity_id)
        if items is None:
            self.log(f"ArrivalLists: {entity_id} returned no usable response", level="WARNING")
        return items
