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
        "live_doorbell_front", "live_doorbell_back", "live_doorbell_apartment")
DOORS = {"front door": "front", "back door": "back", "apartment door": "apartment"}
ENTITIES = {"washer": "sensor.washer_state", "dryer": "sensor.dryer_state", "cook": "sensor.cook_state",
            "coffee": "sensor.coffee_state", "doorbell": "sensor.intercom_visit",
            "apartment": "binary_sensor.yale_doorbell_ding"}


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


def doorbell_cards(full, now, max_age_s=180):
    if not isinstance(full, dict) or full.get("state") != "active":
        return {}
    visits = attributes(full).get("visits")
    if not isinstance(visits, list):
        return {}
    cards = {}
    for visit in visits:
        if not isinstance(visit, dict):
            continue
        door = visit.get("door")
        at = epoch(visit.get("ring_at"))
        if not isinstance(door, str) or door not in DOORS or at is None or not 0 <= now - at < max_age_s:
            continue
        minutes = int((now - at) // 60)
        critical = f"{minutes} min" if minutes else "Now"
        message = f"Rang {minutes} min ago" if minutes else "Someone is at the door"
        tag = "live_doorbell_" + DOORS[door]
        card = Card(tag, f"Doorbell · {door}", message, critical, "mdi:bell-ring",
                    DASHBOARD_LINK_PREFIX + f"now=ring-{int(at)}")
        previous = cards.get(tag)
        if previous is None or int(previous.link.rsplit("-", 1)[1]) < at:
            cards[tag] = card
    return cards


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


def notification_payload(card, now, channel="Live"):
    data = {"tag": card.tag, "live_update": True, "critical_text": card.critical_text,
            "notification_icon": card.icon, "sticky": True, "alert_once": True, "channel": channel,
            "importance": "low", "priority": "low", "clickAction": card.link, "url": card.link}
    if card.finish_at is not None:
        data.update(chronometer=True, when=max(0, math.ceil(card.finish_at - now)), when_relative=True)
    return {"title": card.title, "message": card.message, "data": data}


class LiveUpdates(hass.Hass):
    async def initialize(self):
        args = self.args
        self.enabled_people = people(args.get("enabled_people", ["mikkel"]))
        self.source_people = {source: people(args.get(source + "_people", ["mikkel"]))
                              for source in ("cook", "coffee", "doorbell")}
        self.entities = {source: args.get(source + "_entity", entity) for source, entity in ENTITIES.items()}
        self.channel = args.get("channel", "Live")
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
            source_cards = {"cook": cook_cards(full["cook"], now),
                            "coffee": {"live_coffee": coffee_card(full["coffee"], now)},
                            "doorbell": doorbell_cards(full["doorbell"], now, self.doorbell_max_s)}
            source_cards["coffee"] = {tag: card for tag, card in source_cards["coffee"].items() if card}
            for source, cards in source_cards.items():
                cards = self._retained(source, full[source], cards, now)
                for tag, card in cards.items():
                    # A stale intercom source cannot extend a visit beyond the hard maximum.
                    if source == "doorbell":
                        at = epoch(card.link.rsplit("-", 1)[-1])
                        if at is None or now - at >= self.doorbell_max_s:
                            continue
                    desired[tag] = (card, self._services(self.source_people[source]))
            apartment = full["apartment"]
            if isinstance(apartment, dict) and apartment.get("state") == "on":
                at = epoch(apartment.get("last_changed"))
                if at is not None:
                    self._state["apartment_ring_at"] = max(at, self._state.get("apartment_ring_at") or at)
            ring_at = self._state.get("apartment_ring_at")
            virtual = {"state": "active", "attributes": {"visits": [{"door": "apartment door", "ring_at": ring_at}]}}
            cards = doorbell_cards(virtual, now, self.doorbell_max_s)
            cards = self._retained("apartment", apartment, cards, now)
            for tag, card in cards.items():
                if ring_at is not None and 0 <= now - ring_at < self.doorbell_max_s:
                    desired[tag] = (card, self._services(self.source_people["doorbell"]))
            await self._deliver(desired, now, startup)
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

    async def _send(self, service, payload):
        try:
            await self.call_service(service.replace("notify.", "notify/", 1), **payload)
            return True
        except Exception as error:
            self.log(f"Live update delivery to {service} failed: {error}", level="WARNING")
            return False
