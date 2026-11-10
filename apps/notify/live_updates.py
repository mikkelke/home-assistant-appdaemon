"""Ongoing household cards, reading the apps' public HA state rather than their internals."""

import asyncio
import json
import math
import re
from dataclasses import asdict, dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass

from mobile_notifier import DASHBOARD_LINK_PREFIX

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
SEND_INTERVAL_S = 60
TICK_S = 10
TAGS = ("live_washer", "live_dryer", "live_cook_bath", "live_cook_probe", "live_coffee",
        "live_doorbell_front", "live_doorbell_back", "live_doorbell_apartment",
        "live_delivery_wolt", "live_delivery_nemlig",
        "live_delivery_airmee", "live_delivery_postnord", "live_delivery_gls")
DOORS = {"front door": "front", "back door": "back", "apartment door": "apartment"}
DELIVERY_APPS = {"wolt": "app://com.wolt.android", "nemlig": "app://com.nemlig",
                 "airmee": "app://com.airmee.customer", "postnord": "app://se.postnord.private",
                 "gls": "app://dk.gls.consigneeapp"}
DELIVERY_ICONS = {"wolt": ("mdi:chef-hat", "mdi:moped"), "nemlig": ("mdi:cart", "mdi:truck-delivery")}
PARCEL_CARRIERS = {"airmee": "Airmee", "postnord": "PostNord", "gls": "GLS"}
PARCEL_ICON = "mdi:package-variant-closed"
DELIVERY_KINDS = {"wolt": "food", "nemlig": "groceries", **dict.fromkeys(PARCEL_CARRIERS, "parcel")}
STREET_DOORS = ("front door", "back door")
COURIER_MESSAGE = "Probably the Wolt courier"
COURIER_RING_WINDOW_S = 600
COURIER_RING_KEEP_S = 3600
DELIVERY_STAGES = {"preparing": "Preparing", "on_the_way": "On the way", "arriving": "Arriving"}
ENTITIES = {"washer": "sensor.washer_state", "dryer": "sensor.dryer_state", "cook": "sensor.cook_state",
            "coffee": "sensor.coffee_state", "doorbell": "sensor.intercom_visit",
            "apartment": "binary_sensor.yale_doorbell_ding", "delivery": "sensor.home_delivery"}


def number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def epoch(value):
    if isinstance(value, datetime):
        parsed = value
    else:
        result = number(value)
        if result is not None:
            return result
        if not isinstance(value, str):
            return None
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    try:
        return (parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)).timestamp()
    except (ValueError, OverflowError, OSError):
        return None


def attributes(full):
    attrs = full.get("attributes") if isinstance(full, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def unavailable(full):
    return not isinstance(full, dict) or full.get("state") in (None, "", "unknown", "unavailable")


def people(values):
    values = [values] if isinstance(values, str) else values
    if not isinstance(values, (list, tuple)):
        return []
    return list(dict.fromkeys(value.strip().lower() for value in values
                              if isinstance(value, str) and value.strip()))


def appliance_end(full, now):
    attrs = attributes(full)
    value = attrs.get("estimated_end_time")
    updated = epoch(full.get("last_updated")) or now
    remaining = number(attrs.get("estimated_remaining_min"))
    if isinstance(value, str) and re.fullmatch(r"\d\d:\d\d", value):
        try:
            clock = time.fromisoformat(value)
            day = datetime.fromtimestamp(updated, HOUSE_TZ).date()
            target = updated + max(0, remaining or 0) * 60
            candidates = [datetime.combine(day + timedelta(days=offset), clock, HOUSE_TZ).timestamp()
                          for offset in (-1, 0, 1)]
            return min(candidates, key=lambda candidate: abs(candidate - target))
        except (ValueError, OverflowError, OSError):
            return None
    end = epoch(value)
    if end is not None:
        return end
    return updated + max(0, remaining) * 60 if remaining is not None else None


@dataclass(frozen=True)
class Card:
    tag: str
    title: str
    message: str
    critical_text: str
    icon: str
    link: str
    finish_at: float | None = None


def stored_card(value):
    if not isinstance(value, dict):
        return None
    try:
        card = Card(**value)
    except TypeError:
        return None
    if card.tag not in TAGS or any(not isinstance(getattr(card, key), str)
                                  for key in ("tag", "title", "message", "critical_text", "icon", "link")):
        return None
    if card.finish_at is not None and number(card.finish_at) is None:
        return None
    return Card(card.tag, card.title, card.message, card.critical_text, card.icon, card.link,
                number(card.finish_at))


def countdown(end, now, fallback):
    return f"{max(0, math.ceil((end - now) / 60))} min" if end is not None else fallback


def laundry_card(source, full, now):
    state = full.get("state") if isinstance(full, dict) else None
    if state not in ("Running", "Paused"):
        return None
    paused = state == "Paused"
    attrs = attributes(full)
    end = None if paused else appliance_end(full, now)
    stage = "Pause" if paused else "Running"
    message = attrs.get("programme_label") or stage
    if not isinstance(message, str):
        message = stage
    if paused and message != stage:
        message = f"Pause · {message}"
    return Card(f"live_{source}", "Washer" if source == "washer" else "Dryer", message,
                countdown(end, now, stage), "mdi:washing-machine" if source == "washer" else "mdi:tumble-dryer",
                DASHBOARD_LINK_PREFIX + "room=guest_bathroom", end)


def _temperature(value, label):
    value = number(value)
    return f"{label} {value:g}°" if value is not None else ""


def cook_cards(full, now):
    attrs = attributes(full)
    cards = {}
    bath_phase = attrs.get("bath_phase", "off")
    bath_stages = {"heating": "Heating", "too_hot": "Too hot", "ready": "Ready", "cooking": "Cooking",
                   "chilling": "Chilling"}
    if isinstance(bath_phase, str) and bath_phase in bath_stages:
        stage = bath_stages[bath_phase]
        end = epoch(attrs.get({"heating": "bath_ready_at", "cooking": "bath_done_at",
                               "chilling": "bath_chill_until"}.get(bath_phase)))
        message = " · ".join(filter(None, (stage, _temperature(attrs.get("bath_temp"), "bath"))))
        cards["live_cook_bath"] = Card("live_cook_bath", "Sous vide", message, countdown(end, now, stage),
                                      "mdi:pot-steam", DASHBOARD_LINK_PREFIX + "cook", end)
    probe_phase = attrs.get("probe_phase", "off")
    probe_stages = {"tracking": "Cooking", "near": "Almost ready", "pull": "Pull now", "resting": "Resting"}
    if isinstance(probe_phase, str) and probe_phase in probe_stages:
        stage = probe_stages[probe_phase]
        end = epoch(attrs.get("probe_carve_at" if probe_phase == "resting" else "probe_eta"))
        if probe_phase == "pull":
            end = None
        message = " · ".join(filter(None, (stage, _temperature(attrs.get("probe_core"), "core"))))
        cards["live_cook_probe"] = Card("live_cook_probe", "Roast", message, countdown(end, now, stage),
                                       "mdi:food-steak", DASHBOARD_LINK_PREFIX + "cook", end)
    return cards


def coffee_card(full, now):
    state = full.get("state") if isinstance(full, dict) else None
    stages = {"starting": "Starting", "grinding": "Grinding", "brewing": "Brewing", "paused": "Pause"}
    if not isinstance(state, str) or state not in stages:
        return None
    attrs = attributes(full)
    stage = stages[state]
    end = epoch(attrs.get("eta")) if state in ("grinding", "brewing") else None
    recipe = attrs.get("recipe_name")
    message = f"{stage} · {recipe}" if isinstance(recipe, str) and recipe else stage
    return Card("live_coffee", "Coffee", message, countdown(end, now, stage), "mdi:coffee-maker",
                DASHBOARD_LINK_PREFIX + "coffee", end)


def fresh_visits(full, now, max_age_s):
    """(door label, ring time) of each visit in sensor.intercom_visit that rang less than max_age_s ago."""
    if not isinstance(full, dict) or full.get("state") != "active":
        return []
    visits = attributes(full).get("visits")
    if not isinstance(visits, list):
        return []
    found = []
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        door = visit.get("door")
        at = epoch(visit.get("ring_at"))
        if isinstance(door, str) and door in DOORS and at is not None and 0 <= now - at < max_age_s:
            found.append((door, at))
    return found


def ring_key(at):
    return str(int(at))


def doorbell_cards(full, now, max_age_s=180, matched=()):
    """One card per door for its latest fresh ring. A ring whose ring_key is in matched says it is probably the
    Wolt courier instead of how long ago it rang."""
    cards = {}
    for door, at in fresh_visits(full, now, max_age_s):
        minutes = int((now - at) // 60)
        critical = f"{minutes} min" if minutes else "Now"
        message = f"Rang {minutes} min ago" if minutes else "Someone is at the door"
        if ring_key(at) in matched:
            message = COURIER_MESSAGE
        tag = "live_doorbell_" + DOORS[door]
        card = Card(tag, f"Doorbell · {door}", message, critical, "mdi:bell-ring",
                    DASHBOARD_LINK_PREFIX + f"now=ring-{int(at)}")
        previous = cards.get(tag)
        if previous is None or int(previous.link.rsplit("-", 1)[1]) < at:
            cards[tag] = card
    return cards


def hhmm(at):
    return datetime.fromtimestamp(at, HOUSE_TZ).strftime("%H:%M")


def window_label(start, end):
    if start is None or end is None:
        return None
    first, last = datetime.fromtimestamp(start, HOUSE_TZ), datetime.fromtimestamp(end, HOUSE_TZ)
    if first.minute or last.minute:
        return f"{first:%H:%M}–{last:%H:%M}"
    return f"{first:%H}–{last:%H}"


def delivery_title(delivery):
    source, name = delivery.get("source"), delivery.get("title")
    name = name if isinstance(name, str) else ""
    if source == "wolt":
        return f"Wolt · {name}" if name else "Wolt"
    return name or "Nemlig"


def parcel_card(delivery, now):
    """A parcel has a card only while it is out for delivery, titled by its carrier. An exact ETA is counted
    down to; otherwise the window is shown, or just the stage."""
    source, stage = delivery.get("source"), delivery.get("stage")
    if stage not in ("on_the_way", "arriving"):
        return None
    tag, title, link = "live_delivery_" + source, PARCEL_CARRIERS[source], DELIVERY_APPS[source]
    label = "Arriving" if stage == "arriving" else "Out for delivery"
    eta = epoch(delivery.get("eta"))
    start, end = epoch(delivery.get("window_start")), epoch(delivery.get("window_end"))
    due = end if end is not None else eta
    if due is not None and due <= now:
        return Card(tag, title, "Arriving", "Arriving", PARCEL_ICON, link)
    if eta is not None:
        return Card(tag, title, f"{label} · {hhmm(eta)}", countdown(eta, now, label), PARCEL_ICON, link, eta)
    window = window_label(start, end)
    if window:
        return Card(tag, title, f"{label} · {window}", window, PARCEL_ICON, link)
    return Card(tag, title, label, "Arriving" if stage == "arriving" else "On the way", PARCEL_ICON, link)


def delivery_card(delivery, now):
    source, stage = delivery.get("source"), delivery.get("stage")
    if source in PARCEL_CARRIERS:
        return parcel_card(delivery, now)
    if source not in DELIVERY_APPS or stage not in DELIVERY_STAGES:
        return None
    tag, title, link = "live_delivery_" + source, delivery_title(delivery), DELIVERY_APPS[source]
    waiting, moving = DELIVERY_ICONS[source]
    label = DELIVERY_STAGES[stage]
    eta = epoch(delivery.get("eta"))
    if source == "nemlig" and stage == "preparing":
        start = epoch(delivery.get("window_start"))
        window = window_label(start, epoch(delivery.get("window_end")))
        message = f"Delivery {window}" if window else "Delivery today"
        if start is not None and 0 < start - now <= 3600:
            return Card(tag, title, message, countdown(start, now, window or "Today"), waiting, link, start)
        return Card(tag, title, message, window or "Today", waiting, link)
    if eta is None:
        return Card(tag, title, label, label, waiting if stage == "preparing" else moving, link)
    if eta <= now:
        return Card(tag, title, "Arriving", "Arriving", moving, link)
    return Card(tag, title, f"{label} · {hhmm(eta)}", countdown(eta, now, label),
                waiting if stage == "preparing" else moving, link, eta)


def delivery_entries(full):
    values = attributes(full).get("deliveries") if not unavailable(full) else None
    return [value for value in values if isinstance(value, dict)] if isinstance(values, list) else []


def delivery_cards(full, now, handed_over=()):
    """One card per source: its first delivery that has a card, the sensor having sorted them by arrival. An order
    in handed_over has gone to the doorbell card and gets none."""
    cards = {}
    for delivery in delivery_entries(full):
        if delivery.get("id") in handed_over:
            continue
        card = delivery_card(delivery, now)
        if card is not None and card.tag not in cards:
            cards[card.tag] = card
    return cards


def cancelled_deliveries(full):
    """(id, card title, kind) of every cancelled delivery."""
    return [(delivery["id"], delivery_title(delivery), DELIVERY_KINDS.get(delivery.get("source")))
            for delivery in delivery_entries(full)
            if delivery.get("stage") == "cancelled" and isinstance(delivery.get("id"), str)]


def matched_rings(previous, doorbell, delivery, now, max_age_s):
    """Street-door rings that came while a food courier was expected, as {ring_key: id of the food order that was
    next, or ""}. A ring is matched the first time it is seen with food expected and stays matched, so a card never
    flips back; a matched ring is forgotten after COURIER_RING_KEEP_S."""
    rings = {}
    for key, order in (previous.items() if isinstance(previous, dict) else ()):
        at = number(key)
        if at is not None and isinstance(order, str) and now - at < COURIER_RING_KEEP_S:
            rings[key] = order
    attrs = attributes(delivery)
    if not unavailable(delivery) and attrs.get("food_courier_expected") is True:
        food = attrs.get("next_food")
        order = food.get("id") if isinstance(food, dict) and isinstance(food.get("id"), str) else ""
        for door, at in fresh_visits(doorbell, now, max_age_s):
            if door in STREET_DOORS:
                rings.setdefault(ring_key(at), order)
    return rings


def apartment_ring_matched(rings, at):
    """An apartment ring up to COURIER_RING_WINDOW_S after a matched street-door ring is the same courier."""
    return at is not None and any(0 <= at - number(key) <= COURIER_RING_WINDOW_S for key in rings)


def laundry_audience(previous, cycle, starter, enabled, home, now=None):
    """Keep the start-time audience until the first named attribution, including across reloads."""
    enabled = people(enabled)
    starter = starter.strip().lower() if isinstance(starter, str) else ""
    previous = previous if isinstance(previous, dict) else {}
    previous_cycle = previous.get("cycle")
    last_seen = number(previous.get("last_seen_at"))
    start = epoch(cycle)
    same_cycle = bool(previous) and (previous_cycle == cycle or previous_cycle == "active" or cycle == "active"
                                    or (last_seen is not None and (start is None or start <= last_seen)))
    if same_cycle:
        result = {"cycle": cycle if cycle != "active" else previous_cycle,
                  "people": [person for person in people(previous.get("people")) if person in enabled],
                  "attributed": bool(previous.get("attributed"))}
    else:
        audience = [person for person in enabled if person in home]
        result = {"cycle": cycle, "people": audience or (["mikkel"] if "mikkel" in enabled else []),
                  "attributed": False}
    if now is not None:
        result["last_seen_at"] = now
    if starter and not result["attributed"]:
        result.update(people=[starter] if starter in enabled else [], attributed=True)
    return result


def card_changed(previous, card):
    if not isinstance(previous, Card):
        previous = stored_card(previous)
    if previous is None:
        return True
    for field in ("title", "message", "icon", "link"):
        if getattr(previous, field) != getattr(card, field):
            return True
    old_end, new_end = previous.finish_at, card.finish_at
    if (old_end is None) != (new_end is None):
        return True
    if new_end is not None:
        # The phone owns the countdown; its naturally changing minute label is not a reason to push.
        return abs(new_end - old_end) >= 60
    return previous.critical_text != card.critical_text


def notification_payload(card, now, channel="Live updates"):
    data = {"tag": card.tag, "live_update": True, "critical_text": card.critical_text,
            "notification_icon": card.icon, "sticky": True, "alert_once": True, "channel": channel,
            "importance": "low", "clickAction": card.link, "url": card.link}
    if card.finish_at is not None:
        data.update(chronometer=True, when=max(0, math.ceil(card.finish_at - now)), when_relative=True)
    return {"title": card.title, "message": card.message, "data": data}


class LiveUpdates(hass.Hass):
    async def initialize(self):
        args = self.args
        self.enabled_people = people(args.get("enabled_people", ["mikkel"]))
        self.source_people = {source: people(args.get(source + "_people", ["mikkel"]))
                              for source in ("cook", "coffee", "doorbell")}
        self.delivery_people = {kind: people(args.get(f"delivery_{kind}_people", ["mikkel"]))
                                for kind in ("food", "groceries", "parcel")}
        self.entities = {source: args.get(source + "_entity", entity) for source, entity in ENTITIES.items()}
        self.channel = args.get("channel", "Live updates")
        self.stale_limit_s = max(0, number(args.get("stale_limit_s", 600)) or 0)
        self.doorbell_max_s = max(0, number(args.get("doorbell_max_s", 180)) or 0)
        self.state_file = Path(args.get("state_file") or Path(__file__).with_name("live_updates_state.json"))
        self._lock = asyncio.Lock()
        self._notifier = await self.get_app("MobileNotifier")
        self._state = self._load()
        self._saved = None
        for entity in self.entities.values():
            await self.listen_state(self._on_state, entity, attribute="all")
        await self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)
        await self._reconcile(startup=True)

    def _load(self):
        try:
            value = json.loads(self.state_file.read_text(), parse_constant=lambda value: None)
        except FileNotFoundError:
            value = {}
        except (OSError, ValueError) as error:
            self.log(f"Cannot read live update state: {error}", level="WARNING")
            value = {}
        if not isinstance(value, dict) or value.get("version") != 1:
            value = {}
        return {"version": 1, "laundry": value.get("laundry") if isinstance(value.get("laundry"), dict) else {},
                "sources": value.get("sources") if isinstance(value.get("sources"), dict) else {},
                "deliveries": value.get("deliveries") if isinstance(value.get("deliveries"), dict) else {},
                "delivery_ends": value.get("delivery_ends") if isinstance(value.get("delivery_ends"), dict) else {},
                "courier_rings": value.get("courier_rings") if isinstance(value.get("courier_rings"), dict) else {},
                "apartment_ring_at": epoch(value.get("apartment_ring_at"))}

    def _save(self):
        serialized = json.dumps(self._state, ensure_ascii=False, sort_keys=True, allow_nan=False)
        if serialized == self._saved:
            return
        try:
            temporary = self.state_file.with_name(self.state_file.name + ".tmp")
            temporary.write_text(serialized)
            temporary.replace(self.state_file)
            self._saved = serialized
        except OSError as error:
            self.log(f"Cannot save live update state: {error}", level="WARNING")

    async def _on_state(self, entity, attribute, old, new, kwargs):
        await self._reconcile(updates={entity: new})

    async def _on_tick(self, kwargs):
        await self._reconcile()

    def _services(self, audience):
        mapping = self._notifier.device_mapping
        services = []
        for person in audience:
            if person not in self.enabled_people:
                continue
            values = mapping.get(person, [])
            values = [values] if isinstance(values, str) else values
            if isinstance(values, (list, tuple)):
                services.extend(service for service in values if isinstance(service, str) and service)
        return list(dict.fromkeys(services))

    def _audience(self, source, tag):
        if source == "delivery":
            return self.delivery_people.get(DELIVERY_KINDS.get(tag.removeprefix("live_delivery_")), [])
        return self.source_people[source]

    async def _read(self, entity):
        try:
            return await self.get_state(entity, attribute="all")
        except Exception as error:
            self.log(f"Cannot read {entity}: {error}", level="WARNING")
            return None

    def _retained(self, source, full, cards, now):
        sources = self._state["sources"]
        saved = sources.get(source)
        saved = saved if isinstance(saved, dict) else {}
        if unavailable(full):
            since = number(saved.get("unavailable_since"))
            if since is None:
                since = now
            old = saved.get("cards")
            old = old if isinstance(old, dict) else {}
            retained = {}
            if now - since < self.stale_limit_s:
                for tag, value in old.items():
                    card = stored_card(value)
                    if card is not None and card.tag == tag:
                        retained[tag] = card
            sources[source] = {"cards": {tag: asdict(card) for tag, card in retained.items()},
                               "unavailable_since": since}
            return retained
        sources[source] = {"cards": {tag: asdict(card) for tag, card in cards.items()}}
        return cards

    async def _reconcile(self, startup=False, updates=None):
        async with self._lock:
            now = (await self.get_now()).timestamp()
            full = {source: updates[entity] if updates and entity in updates else await self._read(entity)
                    for source, entity in self.entities.items()}
            home = []
            for person in self.enabled_people:
                try:
                    if await self.get_state("person." + person) == "home":
                        home.append(person)
                except Exception:
                    pass
            desired = {}
            for source in ("washer", "dryer"):
                card = laundry_card(source, full[source], now)
                cards = self._retained(source, full[source], {card.tag: card} if card else {}, now)
                previous = self._state["laundry"].get(source)
                if not unavailable(full[source]):
                    if card:
                        attrs = attributes(full[source])
                        cycle = str(attrs.get("cycle_start_time") or "active")
                        previous = laundry_audience(previous, cycle, attrs.get("started_by"), self.enabled_people, home, now)
                        self._state["laundry"][source] = previous
                    else:
                        self._state["laundry"].pop(source, None)
                        previous = None
                audience = people(previous.get("people")) if isinstance(previous, dict) else []
                for tag, card in cards.items():
                    desired[tag] = (card, self._services(audience))
            rings = matched_rings(self._state["courier_rings"], full["doorbell"], full["delivery"], now,
                                  self.doorbell_max_s)
            self._state["courier_rings"] = rings
            source_cards = {"cook": cook_cards(full["cook"], now),
                            "coffee": {"live_coffee": coffee_card(full["coffee"], now)},
                            "doorbell": doorbell_cards(full["doorbell"], now, self.doorbell_max_s, rings),
                            "delivery": delivery_cards(full["delivery"], now,
                                                       {order for order in rings.values() if order})}
            source_cards["coffee"] = {tag: card for tag, card in source_cards["coffee"].items() if card}
            for source, cards in source_cards.items():
                cards = self._retained(source, full[source], cards, now)
                for tag, card in cards.items():
                    # A stale intercom source cannot extend a visit beyond the hard maximum.
                    if source == "doorbell":
                        at = epoch(card.link.rsplit("-", 1)[-1])
                        if at is None or now - at >= self.doorbell_max_s:
                            continue
                    desired[tag] = (card, self._services(self._audience(source, tag)))
            apartment = full["apartment"]
            if isinstance(apartment, dict) and apartment.get("state") == "on":
                at = epoch(apartment.get("last_changed"))
                if at is not None:
                    self._state["apartment_ring_at"] = max(at, self._state.get("apartment_ring_at") or at)
            ring_at = self._state.get("apartment_ring_at")
            virtual = {"state": "active", "attributes": {"visits": [{"door": "apartment door", "ring_at": ring_at}]}}
            matched = {ring_key(ring_at): ""} if apartment_ring_matched(rings, ring_at) else {}
            cards = doorbell_cards(virtual, now, self.doorbell_max_s, matched)
            cards = self._retained("apartment", apartment, cards, now)
            for tag, card in cards.items():
                if ring_at is not None and 0 <= now - ring_at < self.doorbell_max_s:
                    desired[tag] = (card, self._services(self.source_people["doorbell"]))
            await self._deliver(desired, now, startup)
            await self._announce_cancelled(full["delivery"], now)
            self._save()

    async def _deliver(self, desired, now, startup):
        allowed = self._services(self.enabled_people)
        deliveries = self._state["deliveries"]
        for tag in TAGS:
            card, wanted = desired.get(tag, (None, []))
            records = deliveries.get(tag)
            if not isinstance(records, dict):
                records = {}
                deliveries[tag] = records
            for service in allowed:
                record = records.get(service)
                record = record if isinstance(record, dict) else {}
                if service not in wanted:
                    needs_clear = startup or record.get("card") is not None or record.get("pending_clear")
                    if not needs_clear:
                        continue
                    if record.get("pending_clear") and now - (number(record.get("clear_attempt_at")) or 0) < SEND_INTERVAL_S:
                        continue
                    record["pending_clear"] = True
                    record["clear_attempt_at"] = now
                    records[service] = record
                    if await self._send(service, {"message": "clear_notification", "data": {"tag": tag}}):
                        record.update(card=None, pending_clear=False)
                    continue
                last_sent = number(record.get("at"))
                last_attempt = number(record.get("attempt_at"))
                if last_sent is not None and now - last_sent < SEND_INTERVAL_S:
                    continue
                if last_attempt is not None and now - last_attempt < SEND_INTERVAL_S:
                    continue
                if not card_changed(record.get("card"), card):
                    continue
                record["attempt_at"] = now
                records[service] = record
                if await self._send(service, notification_payload(card, now, self.channel)):
                    record.update(card=asdict(card), at=now, pending_clear=False)

    async def _announce_cancelled(self, full, now):
        """A cancelled delivery gets one real push, retried once a minute until it reaches someone."""
        ends = self._state["delivery_ends"]
        for delivery_id, title, kind in cancelled_deliveries(full):
            audience = [person for person in self.delivery_people.get(kind, []) if person in self.enabled_people]
            record = ends.get(delivery_id)
            record = record if isinstance(record, dict) else {}
            if record.get("sent") or not audience or now - (number(record.get("attempt_at")) or 0) < SEND_INTERVAL_S:
                continue
            record["attempt_at"] = now
            ends[delivery_id] = record
            record["sent"] = bool(await self._notifier.notify(title=title, message="Order cancelled", target=audience,
                                                              category="delivery", data={"tag": "delivery_end"}))
        for delivery_id in [key for key, record in ends.items()
                            if not isinstance(record, dict) or now - (number(record.get("attempt_at")) or 0) > 86400]:
            del ends[delivery_id]

    async def _send(self, service, payload):
        try:
            await self.call_service(service.replace("notify.", "notify/", 1), **payload)
            return True
        except Exception as error:
            self.log(f"Live update delivery to {service} failed: {error}", level="WARNING")
            return False
