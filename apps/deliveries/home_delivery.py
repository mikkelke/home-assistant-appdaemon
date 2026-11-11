"""HomeDelivery - every delivery on its way to the flat, as one entity: sensor.home_delivery.

Each source is a reader that returns Delivery records. Wolt comes from the Wait for Wolt integration's
entities, found by name so a new venue needs no config; Nemlig comes from the signed-in homepage's featured
order, polled slowly until the delivery day; parcels come from the Parcel Aggregator's incoming and delivered
lists, one source per carrier. A ledger remembers what the readers cannot: that an order ended (kept
KEEP_ENDED_S) and that one went missing (held HOLD_MISSING_S in case its source blinked).

A parcel is listed only when it matters today: out for delivery, registered or in transit with an expected time
that falls today, or delivered. Everything else the aggregator holds (days away, at a pickup point, returning,
a problem, an unmapped status, old parcels) stays out of the deliveries and is published as upcoming. The
aggregator's parcel has no stops-away or live ETA, only planned_from and planned_to: both with planned_from
before planned_to is a window, planned_from alone is an ETA.

state: the number of active deliveries (preparing, on_the_way, arriving).
attributes:
  deliveries      [{id, source: wolt|nemlig|<carrier slug, e.g. postnord>, kind: food|groceries|parcel, title
                  (a parcel's sender, else its carrier), stage: preparing|on_the_way|arriving|delivered|
                  cancelled, eta, window_start, window_end (ISO local), minutes_left, item_count}], active
                  first, each group by ETA or window start
  upcoming        [{id, source, title, status, planned_from, planned_to}]: the parcels the aggregator holds as
                  incoming that deliveries leaves out (not out for delivery, not expected today, not delivered).
                  id, source and title are built as in deliveries; status is the aggregator's own status word;
                  planned_from and planned_to are ISO local times when the carrier gives them. Soonest planned
                  first, then as given
  next            the first active entry
  next_food       the first active entry of kind food
  courier_expected  an active delivery is on its way, near its ETA or inside its window
  food_courier_expected  the same, for food deliveries only
  door_courier_kind  food|groceries|parcel: what a ring at the door most likely is, for the doorbell wording. Food
                  counts whenever food_courier_expected; groceries and parcels when one is out for delivery
                  (on_the_way or arriving). Food first, then groceries, then parcels
  door_courier    the entry whose courier a ring at the door most likely is (the first of door_courier_kind
                  counted above), None when door_courier_kind is None
  spoken          one short English answer for voice, food first
  reason          why the state is what it is

AppDaemon 4.5.13 drops None, False and 0 from set_state attributes at any depth and from the state itself, so
the state is published as a string and a missing key reads as null, 0 or false.
"""

import json
import math
import re
import time
import uuid
from dataclasses import dataclass
from datetime import datetime
from zoneinfo import ZoneInfo

import requests  # type: ignore

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

HOUSE_TZ = ZoneInfo("Europe/Copenhagen")
ENTITY = "sensor.home_delivery"
TICK_S = 30
ACTIVE = ("preparing", "on_the_way", "arriving")
OUT = ("on_the_way", "arriving")
KEEP_ENDED_S = 1800
HOLD_MISSING_S = 600
COURIER_BEFORE_S = 1200
COURIER_AFTER_S = 1800

PARCELS_INCOMING = "sensor.parcel_aggregator_incoming_parcels"
PARCELS_DELIVERED = "sensor.parcel_aggregator_delivered_parcels"
PARCEL_NOT_YET_OUT = ("registered", "in_transit")
PARCEL_ARRIVING_S = 900

WOLT_DELIVERY = "sensor.wolt_delivery_"
WOLT_ORDER = re.compile(r"sensor\.wolt_order_(.+?)_(estimated_arrival|item_count)(_\d+)?")
# Asked of Home Assistant itself: AppDaemon 4.5.13's entity cache misses entities created after it started, and
# Wait for Wolt creates new entities for every order. The J stops AppDaemon's render_template from
# literal_eval-ing the JSON into a Python object.
WOLT_SENSORS = (
    "{% set ns = namespace(found=[]) %}"
    "{% for s in states.sensor if s.entity_id.startswith('sensor.wolt_') %}"
    "{% set ns.found = ns.found + [{'entity_id': s.entity_id, 'state': s.state, 'attributes': dict(s.attributes)}] %}"
    "{% endfor %}J{{ ns.found | to_json }}"
)

NEMLIG_SITE = "https://www.nemlig.com"
NEMLIG_FEATURED = "https://webapi.prod.knl.nemlig.it/productbff/api/web/orderHistory/orders/featured"
NEMLIG_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "da-DK,da;q=0.9,en;q=0.8",
    "User-Agent": "home-assistant-appdaemon-delivery/1.0",
    "Device-Size": "desktop",
    "Platform": "web",
    "Version": "11.201.0",
    "Referer": NEMLIG_SITE + "/",
    "Origin": NEMLIG_SITE,
}
NEMLIG_IDLE_S = 900
NEMLIG_ACTIVE_S = 90
NEMLIG_LEAD_S = 3600
NEMLIG_STALE_S = 10800
NEMLIG_MAX_AGE_S = 3600
NEMLIG_RETRY_S = 300
NEMLIG_AUTH_RETRY_S = 3600
NEMLIG_GUARD_S = 300
NEMLIG_TIMEOUT_S = 15
NEMLIG_LOGIN_MAX_AGE_S = 21600
NEMLIG_TOKEN_SKEW_S = 30


@dataclass(frozen=True)
class Delivery:
    id: str
    source: str
    kind: str
    title: str
    stage: str
    eta: float | None = None
    window_start: float | None = None
    window_end: float | None = None
    item_count: int | None = None
    ended_at: float | None = None
    carrier: str | None = None


def attributes(full):
    attrs = full.get("attributes") if isinstance(full, dict) else None
    return attrs if isinstance(attrs, dict) else {}


def epoch(value):
    """Seconds since the epoch for an ISO timestamp with an offset, None for anything else."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed.timestamp() if parsed.tzinfo else None


def iso(at):
    return datetime.fromtimestamp(at, HOUSE_TZ).isoformat(timespec="seconds") if at is not None else None


def count(value):
    try:
        number = int(float(value))
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def flag(value):
    return value is True or value == "true"


def local_day(at):
    return datetime.fromtimestamp(at, HOUSE_TZ).date()


def wolt_states(rendered):
    """Entity id -> {state, attributes} from the WOLT_SENSORS output; raises when it is anything else."""
    if not isinstance(rendered, str) or not rendered.startswith("J"):
        raise ValueError("unexpected template output")
    return {item["entity_id"]: item for item in json.loads(rendered[1:])
            if isinstance(item, dict) and isinstance(item.get("entity_id"), str)}


def wolt_stage(attrs):
    status = attrs.get("order_status")
    if status == "delivered":
        return "delivered"
    if status in ("cancelled", "failed"):
        return "cancelled"
    if attrs.get("order_status_type") != "IN_PROGRESS":
        return None
    if flag(attrs.get("is_arriving_soon")):
        return "arriving"
    if flag(attrs.get("delivering_your_order")):
        return "on_the_way"
    return "preparing"


def wolt_deliveries(states):
    """Wolt orders from the integration's sensors. The tracking sensor holds the stage; the estimated-arrival
    and item-count sensors join it by venue and entity-id suffix, since only the tracking sensor lacks a device."""
    siblings = {}
    for entity_id, full in states.items():
        match = WOLT_ORDER.fullmatch(entity_id)
        if match and isinstance(full, dict):
            siblings[(attributes(full).get("venue_name"), match[2], match[1] + (match[3] or ""))] = full.get("state")
    found = []
    for entity_id, full in states.items():
        if not entity_id.startswith(WOLT_DELIVERY):
            continue
        attrs = attributes(full)
        stage = wolt_stage(attrs)
        if stage is None:
            continue
        key = entity_id[len(WOLT_DELIVERY):]
        venue = attrs.get("venue_name")
        eta = epoch(siblings.get((venue, "estimated_arrival", key)))
        if eta is None:
            eta = epoch(attrs.get("delivery_eta"))
        found.append(Delivery(f"wolt:{key}", "wolt", "food", venue if isinstance(venue, str) and venue else "Wolt",
                              stage, eta=eta, item_count=count(siblings.get((venue, "item_count", key)))))
    return found


def interval(value):
    if not isinstance(value, dict):
        return None
    start, end = epoch(value.get("start")), epoch(value.get("end"))
    return (start, end) if start is not None and end is not None and start < end else None


def nemlig_delivery(order, now):
    """The Delivery for the homepage's featured order; None when there is nothing to track: no order, a
    finished one offered for reorder, one booked for a later day, or one the shop never closed."""
    if not isinstance(order, dict):
        return None
    status = order.get("orderStatus")
    if not isinstance(status, str) or status == "Reorder":
        return None
    booked = interval(order.get("timeslotInterval"))
    start, end = interval(order.get("deliveryInterval")) or booked or (None, None)
    at = epoch(order.get("deliveryTime"))
    if status == "Delivered":
        return Delivery("nemlig", "nemlig", "groceries", "Nemlig", "delivered", window_start=start, window_end=end,
                        ended_at=at)
    if booked and now > booked[1] + NEMLIG_STALE_S:
        return None
    en_route = status == "EnRoute"
    if not en_route and booked and local_day(booked[0]) > local_day(now):
        return None
    return Delivery("nemlig", "nemlig", "groceries", "Nemlig", "on_the_way" if en_route else "preparing",
                    eta=at if en_route else None, window_start=start, window_end=end)


def nemlig_poll_s(order, now):
    """Seconds to the next poll: NEMLIG_IDLE_S, but NEMLIG_ACTIVE_S from NEMLIG_LEAD_S before the booked
    window until the order is delivered."""
    if not isinstance(order, dict) or order.get("orderStatus") in (None, "Reorder", "Delivered"):
        return NEMLIG_IDLE_S
    if order.get("orderStatus") == "EnRoute":
        return NEMLIG_ACTIVE_S
    booked = interval(order.get("timeslotInterval"))
    if booked is None or now > booked[1] + NEMLIG_STALE_S:
        return NEMLIG_IDLE_S
    return min(NEMLIG_IDLE_S, max(NEMLIG_ACTIVE_S, booked[0] - NEMLIG_LEAD_S - now))


class NemligAuthError(Exception):
    pass


class NemligClient:
    """The nemlig.com web login and the homepage's featured order. Blocking: run it off the app's thread."""

    def __init__(self, user, password, http=None, clock=time.time):
        self._user = user
        self._password = password
        self._http = http or requests.Session()
        self._clock = clock
        self._xsrf = None
        self._bearer = None
        self._expires = 0.0
        self._logged_in_at = None

    def featured_order(self):
        """The featured order object, None when the account has none."""
        for attempt in (1, 2):
            self._ensure_session()
            response = self._http.get(NEMLIG_FEATURED, headers=self._headers(bearer=True), timeout=NEMLIG_TIMEOUT_S)
            if response.status_code == 401 and attempt == 1:
                self._logged_in_at = None
                continue
            response.raise_for_status()
            body = response.json()
            order = body.get("order") if isinstance(body, dict) else None
            return order if isinstance(order, dict) else None

    def _headers(self, bearer=False):
        headers = dict(NEMLIG_HEADERS)
        headers["X-Correlation-Id"] = str(uuid.uuid4())
        if self._xsrf:
            headers["X-XSRF-TOKEN"] = self._xsrf
        if bearer and self._bearer:
            headers["Authorization"] = "Bearer " + self._bearer
        return headers

    def _ensure_session(self):
        now = self._clock()
        if self._logged_in_at is None or now - self._logged_in_at > NEMLIG_LOGIN_MAX_AGE_S:
            self._login()
        elif now >= self._expires - NEMLIG_TOKEN_SKEW_S:
            self._token()

    def _token(self):
        response = self._http.get(NEMLIG_SITE + "/webapi/Token", headers=self._headers(), timeout=NEMLIG_TIMEOUT_S)
        response.raise_for_status()
        body = response.json()
        self._bearer = body["access_token"]
        self._expires = self._clock() + float(body.get("expires_in") or 300)

    def _login(self):
        response = self._http.get(NEMLIG_SITE + "/webapi/AntiForgery", headers=self._headers(), timeout=NEMLIG_TIMEOUT_S)
        response.raise_for_status()
        self._xsrf = response.json()["Value"]
        self._token()
        response = self._http.post(
            NEMLIG_SITE + "/webapi/login", headers=self._headers(bearer=True), timeout=NEMLIG_TIMEOUT_S,
            json={"Username": self._user, "Password": self._password, "CheckForExistingProducts": True,
                  "DoMerge": True, "AppInstalled": False, "SaveExistingBasket": False})
        if response.status_code in (401, 403):
            raise NemligAuthError(f"login rejected (HTTP {response.status_code})")
        response.raise_for_status()
        self._token()
        self._logged_in_at = self._clock()


def poll_nemlig(client):
    """One poll, run on the executor. Never raises: a failure comes back as {"ok": False, ...}."""
    try:
        return {"ok": True, "order": client.featured_order()}
    except NemligAuthError as error:
        return {"ok": False, "auth": True, "error": str(error)}
    except Exception as error:
        return {"ok": False, "error": f"{type(error).__name__}: {error}"[:200]}


def tidy(value):
    """A display string without stray whitespace; '' for anything that is not a string."""
    return " ".join(value.split()) if isinstance(value, str) else ""


def parcel_identity(parcel):
    """(carrier, source, barcode) of an aggregator parcel, source being the carrier as a slug; None for anything
    that is not a parcel with a carrier and a barcode."""
    if not isinstance(parcel, dict):
        return None
    carrier, barcode = tidy(parcel.get("carrier")), tidy(parcel.get("barcode"))
    source = re.sub(r"[^a-z0-9]+", "_", carrier.lower()).strip("_")
    return (carrier, source, barcode) if source and barcode else None


def parcel_delivery(parcel, now):
    """The Delivery for one Parcel Aggregator parcel; None unless it matters today: out for delivery, registered
    or in transit with an expected time today, or delivered. Arriving means the expected time is within
    PARCEL_ARRIVING_S or past. A delivered parcel ends when its carrier says; the ledger dates it otherwise."""
    identity = parcel_identity(parcel)
    if identity is None:
        return None
    carrier, source, barcode = identity
    start, end = epoch(parcel.get("planned_from")), epoch(parcel.get("planned_to"))
    window = start is not None and end is not None and start < end
    times = {"window_start": start, "window_end": end} if window else {"eta": start}
    status, ended_at = parcel.get("status"), None
    if status == "out_for_delivery":
        soon = start is not None and start - now <= PARCEL_ARRIVING_S
        stage = "arriving" if soon else "on_the_way"
    elif status in PARCEL_NOT_YET_OUT and start is not None and local_day(start) == local_day(now):
        stage = "preparing"
    elif status == "delivered":
        stage, ended_at = "delivered", epoch(parcel.get("delivered_at"))
    else:
        return None
    return Delivery(f"{source}:{barcode}", source, "parcel", tidy(parcel.get("sender")) or carrier, stage,
                    ended_at=ended_at, carrier=carrier, **times)


def parcel_deliveries(lists, now):
    """Deliveries from the aggregator's parcel lists. A parcel in two lists is delivered if either says so:
    that end is final."""
    found = {}
    for parcels in lists:
        for parcel in parcels:
            delivery = parcel_delivery(parcel, now)
            if delivery and (delivery.id not in found or delivery.stage == "delivered"):
                found[delivery.id] = delivery
    return list(found.values())


def upcoming_parcels(parcels, listed):
    """The incoming parcels whose id is not in `listed` (the published deliveries) and that are not delivered:
    what is on its way but not today's. Soonest planned first, then as given."""
    seen, found = set(listed), []
    for parcel in parcels:
        identity = parcel_identity(parcel)
        if identity is None or parcel.get("status") == "delivered":
            continue
        carrier, source, barcode = identity
        id_ = f"{source}:{barcode}"
        if id_ in seen:
            continue
        seen.add(id_)
        start, end = epoch(parcel.get("planned_from")), epoch(parcel.get("planned_to"))
        status = parcel.get("status")
        planned = start if start is not None else end
        found.append((math.inf if planned is None else planned,
                      {"id": id_, "source": source, "title": tidy(parcel.get("sender")) or carrier,
                       "status": status if isinstance(status, str) and status else "unknown",
                       "planned_from": iso(start), "planned_to": iso(end)}))
    found.sort(key=lambda item: item[0])
    return [item for _, item in found]


class Ledger:
    """What the readers cannot know: that a delivery ended, and when."""

    def __init__(self, keep_s=KEEP_ENDED_S, hold_s=HOLD_MISSING_S):
        self._keep_s = keep_s
        self._hold_s = hold_s
        self._records = {}

    def update(self, observed, now):
        """Fold in one round of observations; returns the deliveries to publish. An ended delivery counts only
        if it was seen alive or its source says when it ended; a missing active one is held for a while."""
        seen = set()
        for delivery in observed:
            seen.add(delivery.id)
            record = self._records.get(delivery.id)
            if delivery.stage in ACTIVE:
                self._records[delivery.id] = {"delivery": delivery, "ended_at": None, "missing_since": None}
                continue
            ended_at = record["ended_at"] if record else None
            if ended_at is None:
                ended_at = delivery.ended_at
            if ended_at is None and record is not None:
                ended_at = now
            if ended_at is not None:
                self._records[delivery.id] = {"delivery": delivery, "ended_at": ended_at, "missing_since": None}
        kept = {}
        for id_, record in self._records.items():
            if id_ not in seen and record["ended_at"] is None and record["missing_since"] is None:
                record["missing_since"] = now
            if record["ended_at"] is not None and now - record["ended_at"] >= self._keep_s:
                continue
            if record["missing_since"] is not None and now - record["missing_since"] >= self._hold_s:
                continue
            kept[id_] = record
        self._records = kept
        return [record["delivery"] for record in kept.values()]


def sort_key(delivery):
    return (delivery.stage not in ACTIVE, delivery.eta or delivery.window_start or math.inf, delivery.id)


def entry(delivery, now):
    minutes = None
    if delivery.stage in ACTIVE and delivery.eta is not None:
        minutes = max(0, math.ceil((delivery.eta - now) / 60))
    return {"id": delivery.id, "source": delivery.source, "kind": delivery.kind, "title": delivery.title,
            "stage": delivery.stage, "eta": iso(delivery.eta), "window_start": iso(delivery.window_start),
            "window_end": iso(delivery.window_end), "minutes_left": minutes, "item_count": delivery.item_count}


def courier_expected(active, now):
    """(expected, why): some active delivery is on its way, is within COURIER_BEFORE_S..COURIER_AFTER_S of its
    ETA, or has its booked window open now."""
    for delivery in active:
        if delivery.stage in OUT:
            return True, "on the way"
        if delivery.eta is not None and delivery.eta - COURIER_BEFORE_S <= now <= delivery.eta + COURIER_AFTER_S:
            return True, "close to its ETA"
        if delivery.window_start is not None and delivery.window_end is not None \
                and delivery.window_start <= now <= delivery.window_end:
            return True, "inside its window"
    return False, None


def door_courier(active, now):
    """The delivery a ring at the door most likely brings: the first food order while its courier is expected,
    else the first groceries, else the first parcel out for delivery; None when no courier is expected."""
    food = [delivery for delivery in active if delivery.kind == "food"]
    if courier_expected(food, now)[0]:
        return food[0]
    for kind in ("groceries", "parcel"):
        for delivery in active:
            if delivery.kind == kind and delivery.stage in OUT:
                return delivery
    return None


def clock(at, whole=False):
    """12-hour wall clock for speech: 5:56 PM, or with whole=True 4 PM for 16:00."""
    moment = datetime.fromtimestamp(at, HOUSE_TZ)
    hour = moment.hour % 12 or 12
    text = f"{hour}" if whole and moment.minute == 0 else f"{hour}:{moment.minute:02d}"
    return text + (" AM" if moment.hour < 12 else " PM")


def window_text(start, end, link="to"):
    if start is None or end is None:
        return None
    first, last = clock(start, whole=True), clock(end, whole=True)
    if first[-2:] == last[-2:]:
        first = first[:-3]
    return f"{first} {link} {last}"


def article(name):
    return "An" if name.startswith(tuple("AEIOU")) and not name.isupper() else "A"


def parcel_sentence(delivery, now):
    carrier = delivery.carrier or delivery.title
    parcel = f"{article(carrier)} {carrier} parcel"
    window = window_text(delivery.window_start, delivery.window_end, "and")
    when = None
    if window:
        when = f"between {window}"
    elif delivery.eta is not None:
        when = f"around {clock(delivery.eta, whole=True)}"
    if delivery.stage == "preparing":
        return f"{parcel} is expected {when or 'today'}."
    due = delivery.window_end if delivery.window_end is not None else delivery.eta
    if due is not None and due <= now:
        return f"{parcel} should be there any moment now."
    return f"{parcel} is out for delivery" + (f", expected {when}." if when else ".")


def sentence(delivery, now):
    if delivery.kind == "parcel":
        return parcel_sentence(delivery, now)
    if delivery.source == "wolt":
        if delivery.stage == "preparing":
            text = f"{delivery.title} is still making it."
            return text + (f" Wolt expects it around {clock(delivery.eta)}." if delivery.eta is not None else "")
        if delivery.eta is None:
            return f"{delivery.title} is on the way."
        if delivery.eta <= now:
            return "It should be there any moment now."
        minutes = max(1, math.ceil((delivery.eta - now) / 60))
        wait = "a minute" if minutes == 1 else f"{minutes} minutes"
        return f"In about {wait}, around {clock(delivery.eta)}."
    if delivery.source == "nemlig":
        if delivery.stage == "preparing":
            window = window_text(delivery.window_start, delivery.window_end)
            return f"Nemlig is booked for {window}." if window else "Nemlig has an order coming."
        if delivery.eta is None:
            return "Nemlig is on the way."
        if delivery.eta <= now:
            return "Nemlig should be there any moment now."
        return f"Nemlig is on the way, around {clock(delivery.eta)}."
    return f"{delivery.title} is on the way."


def spoken(active, now):
    """The answer to "when is the food coming": the first food delivery if there is one, else the first of the rest."""
    if not active:
        return "There's no delivery on the way."
    active = sorted(active, key=lambda delivery: delivery.kind != "food")
    text = sentence(active[0], now)
    more = len(active) - 1
    if more and active[0].kind == "parcel":
        return f"{text[:-1]}, and {more} more."
    if more:
        text += " There's one more after that." if more == 1 else f" There are {more} more after that."
    return text


def snapshot(deliveries, now, incoming=()):
    """The published state and attributes for these deliveries and the aggregator's incoming parcels."""
    ordered = sorted(deliveries, key=sort_key)
    entries = [entry(delivery, now) for delivery in ordered]
    active = [delivery for delivery in ordered if delivery.stage in ACTIVE]
    expected, why = courier_expected(active, now)
    courier = door_courier(active, now)
    food = [delivery for delivery in active if delivery.kind == "food"]
    next_food = next((item for delivery, item in zip(ordered, entries)
                      if delivery.kind == "food" and delivery.stage in ACTIVE), None)
    if active:
        reason = f"{len(active)} active, next {active[0].title} {active[0].stage}; " + \
                 (f"courier expected: {why}" if expected else "no courier expected yet")
    else:
        reason = "no active delivery"
    return {"state": len(active),
            "attributes": {"friendly_name": "Home delivery", "icon": "mdi:truck-delivery", "deliveries": entries,
                           "upcoming": upcoming_parcels(incoming, {delivery.id for delivery in ordered}),
                           "next": entries[0] if active else None, "next_food": next_food,
                           "courier_expected": expected, "food_courier_expected": courier_expected(food, now)[0],
                           "door_courier_kind": courier.kind if courier else None,
                           "door_courier": next((item for delivery, item in zip(ordered, entries)
                                                 if delivery is courier), None),
                           "spoken": spoken(active, now), "reason": reason}}


class HomeDelivery(hass.Hass):
    def initialize(self):
        args = self.args
        self._entity = args.get("entity", ENTITY)
        self._ledger = Ledger()
        self._listened = set()
        self._wolt = {}
        self._wolt_ok = True
        self._incoming = []
        self._stages = {}
        self._nemlig = None
        self._nemlig_order = None
        self._nemlig_at = 0.0
        self._nemlig_next = 0.0
        self._nemlig_status = None
        user, password = args.get("nemlig_user"), args.get("nemlig_pass")
        if user and password:
            self._nemlig = NemligClient(user, password)
        else:
            self.log("No Nemlig login configured: Nemlig deliveries are not tracked", level="WARNING")
        self._readers = [self._read_wolt, self._read_nemlig, self._read_parcels]
        for entity_id in (PARCELS_INCOMING, PARCELS_DELIVERED):
            self.listen_state(self._on_change, entity_id, attribute="all")
        self.run_every(self._on_tick, "now+5", TICK_S)
        self._refresh()

    def _on_tick(self, kwargs):
        self._poll_nemlig()
        self._refresh()

    def _on_change(self, entity, attribute, old, new, kwargs):
        self._refresh()

    def _refresh(self):
        now = self.get_now().timestamp()
        observed = []
        for read in self._readers:
            observed.extend(read(now))
        deliveries = self._ledger.update(observed, now)
        stages = {delivery.id: delivery.stage for delivery in deliveries}
        for id_, stage in stages.items():
            if self._stages.get(id_) != stage:
                self.log(f"{id_}: {self._stages.get(id_, 'new')} -> {stage}")
        self._stages = stages
        published = snapshot(deliveries, now, self._incoming)
        self.set_state(self._entity, state=str(published["state"]), attributes=published["attributes"], replace=True)

    def _read_wolt(self, now):
        try:
            self._wolt = wolt_states(self.render_template(WOLT_SENSORS))
        except Exception as error:
            if self._wolt_ok:
                self.log(f"Wolt sensors unreadable, keeping the last read: {type(error).__name__}", level="WARNING")
            self._wolt_ok = False
        else:
            if not self._wolt_ok:
                self.log("Wolt sensors readable again")
            self._wolt_ok = True
        for entity_id in self._wolt:
            if entity_id.startswith(WOLT_DELIVERY) and entity_id not in self._listened:
                self._listened.add(entity_id)
                self.listen_state(self._on_change, entity_id, attribute="all")
        return wolt_deliveries(self._wolt)

    def _read_nemlig(self, now):
        if now - self._nemlig_at > NEMLIG_MAX_AGE_S:
            return []
        delivery = nemlig_delivery(self._nemlig_order, now)
        return [delivery] if delivery else []

    def _read_parcels(self, now):
        lists = []
        for entity_id in (PARCELS_INCOMING, PARCELS_DELIVERED):
            parcels = attributes(self.get_state(entity_id, attribute="all")).get("parcels")
            lists.append(parcels if isinstance(parcels, list) else [])
        self._incoming = lists[0]
        return parcel_deliveries(lists, now)

    def _poll_nemlig(self):
        now = self.get_now().timestamp()
        if self._nemlig is None or now < self._nemlig_next:
            return
        self._nemlig_next = now + NEMLIG_GUARD_S
        self.submit_to_executor(poll_nemlig, self._nemlig, callback=self._on_nemlig)

    def _on_nemlig(self, result=None, **kwargs):
        now = self.get_now().timestamp()
        result = result if isinstance(result, dict) else {}
        if result.get("ok"):
            order = result.get("order")
            status = order.get("orderStatus") if isinstance(order, dict) else None
            if status != self._nemlig_status:
                self.log(f"Nemlig order status: {self._nemlig_status} -> {status}")
                self._nemlig_status = status
            self._nemlig_order, self._nemlig_at = order, now
            self._nemlig_next = now + nemlig_poll_s(order, now)
        else:
            self.log(f"Nemlig poll failed: {result.get('error', 'no result')}", level="WARNING")
            self._nemlig_next = now + (NEMLIG_AUTH_RETRY_S if result.get("auth") else NEMLIG_RETRY_S)
        self._refresh()
