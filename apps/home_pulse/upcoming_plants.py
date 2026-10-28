"""Watering reminders for sensor.house_upcoming: one row for each houseplant that needs water. Pure:
each step takes the persisted state and returns new state.

A plant (the `plants` list in house_upcoming.yaml) has either a soil moisture sensor, `moisture`, or a
calendar interval, `every_days`, never both, and optionally a `water` amount in litres. The row's title
is "Water the <name>: <litres> L", and gives up name characters before it gives up the amount. A plant
may also say what it looks like, `aka`, and where it stands, `where`: plain text of one line, which
lead the info lines of the row's sheet.

The title says to add liquid feed when the plant is due for it: "... + <tsp> tsp fertiliser", at one
teaspoon a litre of water, to the nearest quarter. A sensor plant with a `conductivity` sensor is due for
it while the reading is under FEED_BELOW; an unknown reading says nothing. A calendar plant is due for it
at the watering the row is for (its due day, or today once that has passed): from March to September at
every watering, in the other months once FEED_EVERY_DAYS days have passed since it was last fed or if it
never was. A succulent (`feed: summer`) is fed from March to September only, and then at most every
FEED_EVERY_DAYS days. The anchor keeps `fed_at`, set by a Done that said to feed and carried over by one
that did not; a seed counts as a feeding at the moment it is made.

Every plant is also one line in the `items` of a single row, `maint:plants`, which is always there: its title
counts the plants due and its detail names them, or the title gives the next due day, and the lines say when
each plant is due (a sensor plant: its soil and its dry level) and when it was last watered, the plants due
first. A calendar plant was last watered at its anchor's time. A sensor plant was watered when its soil
moisture rose RISE_POINTS or more within RISE_WINDOW and then stopped climbing for RISE_SETTLE; that moment is
what `step_rise` finds, live and in the recorder's history.

A sensor plant is dry once its soil moisture reading, in percent, is at or under its `dry`, and stays dry
until a reading is CLOSE_MARGIN above it: watering makes the reading jump, while a reading wandering
around `dry` never flaps the row. A reading that is unknown, unavailable or not a number changes nothing
and has no row; a sensor that stays offline is the battery inventory's to report. A dry plant's row is
admin only and untimed, like an open battery condition. It has no Done button, the moisture rising
closes it, and its push key is unique per episode: the time the latch opened.

A calendar plant is a tapped task counted in days, like the dishwasher's filters (upcoming_runs, whose
anchors, Done and undo it uses): the anchor says when it was last watered, seeded at the moment the plant
is first seen. It has no row until LEAD_DAYS house days before the house day that is `every_days` after
the anchor's, so plants weeks away do not crowd the timeline or its row cap. From then on the row is
timed at DUE_TIME on the due day, and once that day has come it is untimed and due, with a push key for
the episode, which is the anchor's time. A Done tap starts the interval over.
"""

import re
import unicodedata
from datetime import datetime, timedelta

from upcoming_core import ITEMS_MAX, TEXT_MAX, clean_text, make_row, utc_iso
from upcoming_maint import DUE_TIME, HOUSE_TZ, ddmm, fit_parts, house_date, parse_iso, reading

ID_PREFIX = "maint:plant:"
ANCHOR_PREFIX = "plant:"
ICON = "mdi:watering-can-outline"
CLOSE_MARGIN = 5
LEAD_DAYS = 1
FEED_BELOW = 350
RISE_POINTS = 10
RISE_WINDOW = timedelta(minutes=30)
RISE_SETTLE = timedelta(minutes=15)
OVERVIEW_ID = "maint:plants"
OVERVIEW_ICON = "mdi:sprout"
FEED_EVERY_DAYS = 28
SUMMER_MONTHS = range(3, 10)


def slug(name):
    """The lowercase ASCII letters and digits of `name`, each run of anything else becoming one "_"."""
    ascii_name = unicodedata.normalize("NFKD", str(name)).encode("ascii", "ignore").decode("ascii")
    return re.sub(r"[^a-z0-9]+", "_", ascii_name.lower()).strip("_")


def _note(value, who, field):
    """[value] for a note that is plain one-line ASCII text of at most TEXT_MAX characters, [] for none."""
    if value is None:
        return []
    if not isinstance(value, str) or not value or clean_text(value) != value:
        raise ValueError(f"{who}: {field} must be plain text on one line of at most {TEXT_MAX} characters "
                         f"without accents, not {value!r}")
    return [value]


def _parse(raw):
    sensor, calendar, slugs, entities = {}, {}, set(), set()
    for cfg in raw or []:
        cfg = cfg if isinstance(cfg, dict) else {}
        entity, days, water = cfg.get("moisture"), cfg.get("every_days"), cfg.get("water")
        name = str(cfg.get("name") or "").strip()
        who, key, litres = f"plant {name or cfg!r}", slug(name), reading(water)
        if not key:
            raise ValueError(f"{who}: the name needs letters or digits")
        if (entity is None) == (days is None):
            raise ValueError(f"{who}: give exactly one of moisture and every_days")
        if water is not None and (litres is None or litres <= 0):
            raise ValueError(f"{who}: water must be a positive number of litres, not {water!r}")
        if key in slugs:
            raise ValueError(f"{who}: name listed twice")
        slugs.add(key)
        info = _note(cfg.get("aka"), who, "aka") + _note(cfg.get("where"), who, "where")
        plant = {"name": name, "slug": key, "area": cfg.get("area") or None, "water": litres, "info": info}
        if entity is None:
            interval = reading(days)
            if interval is None or interval < 1 or interval != int(interval):
                raise ValueError(f"{who}: every_days must be a whole number of days above zero, not {days!r}")
            if cfg.get("dry") is not None or cfg.get("conductivity") is not None:
                raise ValueError(f"{who}: dry and conductivity go with moisture, not every_days")
            if cfg.get("feed") not in (None, "summer"):
                raise ValueError(f"{who}: feed can only be summer, not {cfg.get('feed')!r}")
            calendar[key] = {**plant, "every_days": int(interval), "feed": cfg.get("feed")}
            continue
        dry, conductivity = reading(cfg.get("dry")), cfg.get("conductivity")
        if cfg.get("feed") is not None:
            raise ValueError(f"{who}: feed goes with every_days, not moisture")
        used = [each for each in (entity, conductivity) if each is not None]
        valid = all(isinstance(each, str) and each for each in used)
        if not valid or len(set(used)) < len(used) or entities & set(used):
            raise ValueError(f"{who}: moisture and conductivity must be sensor entity ids, each listed once")
        if dry is None:
            raise ValueError(f"{who}: dry must be a number, not {cfg.get('dry')!r}")
        entities |= set(used)
        sensor[entity] = {**plant, "dry": dry, "conductivity": conductivity}
    return sensor, calendar


def parse_plants(raw):
    """The sensor plants of the `plants` list of house_upcoming.yaml, validated, as {moisture entity:
    plant}. Raises ValueError on a plant that has no name with letters or digits, does not have exactly
    one of `moisture` and `every_days`, has a `dry` that is not a number, a `water` that is given but not
    a positive number, an `aka` or `where` that is not plain text, a `conductivity` or `feed` that does not
    fit its kind, or lists an entity or name twice, so a typo fails at start instead of silently dropping
    a plant."""
    return _parse(raw)[0]


def parse_timed(raw):
    """The calendar plants of the same list, as {slug: plant}, under the same checks, and `every_days`
    has to be a whole number of days above zero and `dry` left out."""
    return _parse(raw)[1]


def valid_latches(raw, plants):
    """The saved latches that are for a configured plant and carry the time they opened."""
    raw = raw if isinstance(raw, dict) else {}
    return {entity: {"since": latch["since"]} for entity, latch in raw.items()
            if entity in plants and isinstance(latch, dict) and parse_iso(latch.get("since")) is not None}


def step_plant(st, plant, value, now):
    """One observation of a sensor plant: `value` is its soil moisture reading or None. The latch is
    {"since": when it opened} while the plant is dry and {} while it is not."""
    st = dict(st or {})
    if value is None:
        return st
    if "since" in st:
        return {} if value >= plant["dry"] + CLOSE_MARGIN else st
    if value <= plant["dry"]:
        st["since"] = utc_iso(now)
    return st


def _percent(value):
    return f"{round(value, 1):g}%"


def tsp_text(litres):
    """The teaspoons of feed for `litres` of water at one a litre, to the nearest quarter and at least a
    quarter: 1/4, 1/2, 3/4, 1, 1 1/2."""
    whole, part = divmod(max(1, int(litres * 4 + 0.5)), 4)
    return " ".join(filter(None, [str(whole) if whole else "", {1: "1/4", 2: "1/2", 3: "3/4"}.get(part, "")]))


def _title(plant, feed=False):
    """"Water the <name>", then ": <litres> L" when the plant has an amount, then " + <tsp> tsp fertiliser"
    when it is due for feed, or " + <tsp> tsp feed" if that is what fits. The name is cut to make room,
    never the amount or the dose."""
    base, litres = clean_text(f"Water the {plant['name']}"), plant["water"]
    amount = f": {round(litres, 2):g} L" if litres else ""
    if feed:
        dose = f" + {tsp_text(litres)} tsp" if litres else " +"
        if len(base + amount + dose + " fertiliser") <= TEXT_MAX:
            return base + amount + dose + " fertiliser"
        amount += dose + " feed"
    return clean_text(base, TEXT_MAX - len(amount)) + amount


def plant_row(entity, plant, st, value, conductivity=None):
    """(row, push key) while the sensor plant is dry and its reading is known, else None. The row says to
    add feed while the `conductivity` reading, if there is one, is under FEED_BELOW."""
    st = st or {}
    if "since" not in st or value is None:
        return None
    feed = conductivity is not None and conductivity < FEED_BELOW
    row = make_row(ID_PREFIX + plant["slug"], "maint", _title(plant, feed),
                   f"soil {_percent(value)}, water at {_percent(plant['dry'])}", icon=ICON, area=plant["area"],
                   audience="admin", info=plant["info"])
    return row, f"plant:{entity}:{st['since']}"


# ---------------------------------------------------------------- watering seen in the soil


def valid_watered(raw, plants):
    """The saved last-watered times that are for a configured sensor plant (by slug) and parse."""
    raw = raw if isinstance(raw, dict) else {}
    slugs = {plant["slug"] for plant in plants.values()}
    return {slug: when for slug, when in raw.items() if slug in slugs and parse_iso(when) is not None}


def step_rise(st, value, now):
    """One look at a sensor plant's soil moisture, `value` its reading or None: (state, event). The state
    keeps the readings of the last RISE_WINDOW and the rise under way. The event is {"at", "low",
    "peak"} once a rise of RISE_POINTS or more has stopped climbing for RISE_SETTLE: when it began, the
    lowest reading before it and the highest after."""
    st = st or {}
    recent = [[when, seen] for when, seen in st.get("recent", []) if now - parse_iso(when) < RISE_WINDOW]
    rise = st.get("rise")
    if value is not None:
        if rise is None:
            lows = [seen for _, seen in recent]
            if lows and value - min(lows) >= RISE_POINTS:
                rise = {"at": utc_iso(now), "low": min(lows), "peak": value, "top": utc_iso(now)}
        elif value > rise["peak"]:
            rise = {**rise, "peak": value, "top": utc_iso(now)}
        if not recent or recent[-1][1] != value:
            recent.append([utc_iso(now), value])
    if rise is not None and now - parse_iso(rise["top"]) >= RISE_SETTLE:
        done = {"at": rise["at"], "low": rise["low"], "peak": rise["peak"]}
        return {"recent": [] if value is None else [[utc_iso(now), value]], "rise": None}, done
    return {"recent": recent, "rise": rise}, None


def rises(series, now):
    """The waterings [{"at", "low", "peak"}] in a moisture history [(aware datetime, reading or None)],
    oldest first, as of `now`."""
    st, found = None, []
    for when, value in list(series) + [(now, None)]:
        st, event = step_rise(st, value, when)
        if event:
            found.append(event)
    return found


# ---------------------------------------------------------------- calendar plants


def anchor_key(plant):
    return ANCHOR_PREFIX + plant["slug"]


def timed_by_row(row, calendar):
    """The calendar plant whose row id is `row`, None for any other id."""
    return next((plant for plant in calendar.values() if ID_PREFIX + plant["slug"] == row), None)


def seed_timed(anchors, calendar, now):
    """`anchors` plus a seed at `now` for every calendar plant that has none."""
    out = dict(anchors)
    for plant in calendar.values():
        out.setdefault(anchor_key(plant), {"at": utc_iso(now), "seed": True})
    return out


def fed_on(anchor):
    """When the plant was last fed: the anchor's `fed_at`, else its own time if it is a seed, else None."""
    anchor = anchor or {}
    return parse_iso(anchor.get("fed_at")) or (parse_iso(anchor.get("at")) if anchor.get("seed") else None)


def feeds(plant, anchor, now):
    """Whether the watering of a calendar plant, on its due day or today once that has passed, comes
    with feed."""
    when = parse_iso((anchor or {}).get("at"))
    if when is None:
        return False
    day = max(house_date(when) + timedelta(days=plant["every_days"]), house_date(now))
    fed = fed_on(anchor)
    stale = fed is None or (day - house_date(fed)).days >= FEED_EVERY_DAYS
    summer = day.month in SUMMER_MONTHS
    return summer and stale if plant["feed"] == "summer" else summer or stale


def done_fields(plant, anchor, now):
    """The fields a Done at `now` gives the new anchor: when the plant was last fed, which is now if the
    row said to feed."""
    fed = now if feeds(plant, anchor, now) else fed_on(anchor)
    return {"fed_at": utc_iso(fed)} if fed else {}


def _days(count):
    return f"{count} day{'' if count == 1 else 's'}"


def overview_row(sensor, timed, now):
    """The `maint:plants` row, None without plants: `sensor` is [{"plant", "value", "dry", "watered"}] (the
    reading or None, whether the plant is dry, when it was last watered or None) and `timed` is
    [(plant, anchor)]. The lines come plants due first, then by due day, then the sensor plants that are
    not dry by how far above their dry level they stand."""
    today = house_date(now)
    entries = []
    for plant, anchor in timed:
        when = parse_iso((anchor or {}).get("at"))
        if when is None:
            continue
        due = house_date(when) + timedelta(days=plant["every_days"])
        late = due <= today
        line = f"{plant['name']}: {'due ' if late else ''}{ddmm(due, now)}, watered {ddmm(when, now)}"
        entries.append(((0 if late else 1, due, plant["name"]), late, due, plant["name"], line))
    for each in sensor:
        plant, value, watered = each["plant"], each["value"], parse_iso(each["watered"])
        soil = "soil unknown" if value is None else f"soil {_percent(value)}"
        line = f"{plant['name']}: {'dry, ' if each['dry'] else ''}{soil}, at {_percent(plant['dry'])}"
        line += f", watered {ddmm(watered, now)}" if watered else ""
        margin = 0 if value is None else value - plant["dry"]
        entries.append(((0, today, plant["name"]) if each["dry"] else (2, margin, plant["name"]), each["dry"], None,
                        plant["name"], line))
    if not entries:
        return None
    entries.sort(key=lambda entry: entry[0])
    due_names = [name for _, late, _, name, _ in entries if late]
    coming = [due for _, late, due, _, _ in entries if due and not late]
    lines = [line for *_, line in entries]
    if len(lines) > ITEMS_MAX:
        lines = lines[:ITEMS_MAX - 1] + [f"+{len(lines) - ITEMS_MAX + 1} more"]
    if due_names:
        title, at = f"Plants: {len(due_names)} of {len(entries)} due", None
        detail = fit_parts(due_names)
    elif coming:
        title, at = f"Plants: next {ddmm(min(coming), now)}", datetime.combine(min(coming), DUE_TIME, HOUSE_TZ)
        detail = f"{len(entries)} plants"
    else:
        title, at, detail = "Plants: none due", None, f"{len(entries)} plants"
    return make_row(OVERVIEW_ID, "maint", title, detail, at=at, icon=OVERVIEW_ICON, audience="admin", items=lines)


def timed_row(plant, anchor, now):
    """(row, push key) for a calendar plant, None without an anchor or while its due day is more than
    LEAD_DAYS away. Dated and without a key until the due day; untimed, due and with a key for the
    anchor's episode from that day on."""
    when = parse_iso((anchor or {}).get("at"))
    if when is None:
        return None
    interval = plant["every_days"]
    due = house_date(when) + timedelta(days=interval)
    left = (due - house_date(now)).days
    if left > LEAD_DAYS:
        return None
    since = max(0, (house_date(now) - house_date(when)).days)
    if left > 0:
        at, key = datetime.combine(due, DUE_TIME, HOUSE_TZ), None
        detail = fit_parts([f"{_days(left)} left", ddmm(due, now)])
    else:
        at, key = None, f"{anchor_key(plant)}:{utc_iso(when)}"
        detail = "due today" if left == 0 else f"due since {ddmm(due, now)}"
    last = (f"Counting from {ddmm(when, now)}" if anchor.get("seed")
            else f"Done {ddmm(when, now)}, {'today' if since == 0 else f'{_days(since)} ago'}")
    row = make_row(ID_PREFIX + plant["slug"], "maint", _title(plant, feeds(plant, anchor, now)), detail, at=at,
                   icon=ICON, area=plant["area"], audience="admin", tap_done=True,
                   info=[*plant["info"], "Every day" if interval == 1 else f"Every {interval} days", last])
    return row, key
