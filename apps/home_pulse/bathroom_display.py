"""BathroomDisplay - what the round display in the main bathroom (ESPHome node bathroom-display) shows, and the
speaker its play button works.

Publishes sensor.bathroom_display, the one entity the display reads:
    state       "off" (the bathroom is not active), "someone" (active, Mikkel not identified yet) or
                "mikkel" (active, and his phone has been near or has risen during this visit, or he has
                weighed himself)
    attributes  every value a string, since AppDaemon 4.5.13's set_state drops None, False and 0:
        reason      why, in RoomActive's style: "inactive:binary_sensor.bathroom_active"; "unknown:..."
                    for a first evaluation room_active_read has no opinion on;
                    "sleep:input_boolean.mikkel_sleep_mode" for an active room kept dark; while active
                    "active:binary_sensor.bathroom_active phone:near", the word being the phone's status:
                    near (at or above mikkel_rssi_min), rise (under it, but at or above rssi_rise_min and
                    rssi_rise_db dB above the phone's baseline), weak (neither), stale (older than
                    rssi_fresh_s) or none (no reading), or held for a mikkel whose phone is neither near
                    nor rising right now
        person      "mikkel" for state mikkel, else ""
        greeting    "Hi Mikkel" for state mikkel, else ""
        night       "on" while Mikkel's sleep mode (sleep_entity) is on, else "", in every state
        line1..3    the title of each line, at most LINE_MAX characters; "" when unused
        icon1..3    the line's mdi icon, the row's own or the line's (mdi:train for transit); "" when unused
        due1..3     the text under the line's chip: when a row is due ("overdue", "today", "tomorrow" or
                    "6 d"), or the short value of a weighing, alarm or rain line ("73.3 kg", "09:15",
                    "09-11"); "" when it has none
        friendly_name, icon
    The lines are filled only for state mikkel, except while the night is on (below). The phone's dBm is not
    published (mikkel_rssi_min is calibrated from the history of its sensor), so the payload changes only
    with the state, the lines and the phone's status word.

The state is next_state(). Occupancy is room_active_read.active(), never a raw presence sensor, and False is
off at once. In an active room Mikkel is identified when the latest reading of his phone - the iBeacon the
display hears, sensor.bathroom_display_mikkel_phone_rssi, which reads unknown (NaN on the device) once the
phone is out of range - is a number that was updated at most rssi_fresh_s seconds ago and is
    near  at or above mikkel_rssi_min dBm, or
    rise  at or above rssi_rise_min dBm and at least rssi_rise_db dB above the phone's baseline,
or when a weighing of his arrives (below). From then on he stays identified for as long as the room stays
active, whatever the phone does.
No opinion on the room (None) changes nothing, the state, reason and lines staying as they were; before
there is a state it is off.

The baseline is how loud the phone was before the visit, which tells a phone that has just come into the
bathroom from one that sits at the same level in a room next door: the median of its readings that were
updated from BASELINE_FROM_S (120) down to BASELINE_TO_S (15) seconds before the visit started, or UNHEARD
(-100 dBm) when it was not heard then. A visit starts at the evaluation where the state leaves off (or has no
value yet) for an active room: the room becoming active, the main spots coming on in sleep mode (below) or
the app starting in an active room. The baseline is worked out there and holds for the whole visit. For it
the app keeps the numeric readings of the phone's last HISTORY_S seconds, one for each last_updated, from the
snapshot at initialize and from every callback.

While Mikkel's sleep mode (sleep_entity) is on, an active room stays off unless the main spots (light_entity)
are on. The lights app never switches them on automatically in sleep mode, so they are on only because
someone used the wall switch. A visit that starts dark begins again as someone once the light comes on.

The night is sleep mode being on, in any state of the room, also off. The lines are then just the alarm line
and night is "on", so the display shows the clock and the alarm and nothing else, and a touch of the dark
display wakes it to the same.

The lines for Mikkel, at most three, in this order:
    weighing  for CARD_S (90) seconds from a weighing of his, the card below in place of all the others.
    alarm     from the time of alarm_from_entity (input_datetime.bathroom_display_alarm_from; ALARM_FROM,
              21:00, while it cannot be read as a time) until 02:00 house time: "Alarm 09:15" while
              alarm_entity (input_boolean.wakeup_bedroom) is on, with the time of alarm_time_entity, else
              "No alarm"; none while alarm_entity cannot be read as on or off.
    rain      06:00-12:00 house time, on the way out: "Raining now" while rain_entity (the roof station's
              piezo sensor) is on, else the first run of consecutive forecast hours with at least
              upcoming_rain.MIN_MM that starts within RAIN_AHEAD (4 h): "Rain 09-11", from the start of
              its first hour to the end of its last, or "Rain from 09" when it goes on past those 4 hours.
              An hour that is going on counts, one that is over does not, and a missing hour ends a run.
              Monday to Friday (house days, holidays not known) it also covers the way home: the first
              run of such hours among those that start inside today's HOME_FROM-HOME_TO (16:00-20:00),
              "home 17-19", which comes after the way out's part and a " · " ("Rain 09-11 · home 17-19",
              "Raining now · home 17-19") or stands alone as "Rain home 17-19". The text under the chip
              is the way out's value ("now", "09-11", "from 09"), else the way home's ("17-19").
              No rain, no line. The hourly forecast (weather.get_forecasts for weather_entity) is
              fetched only when it is wanted - state mikkel inside the window, and not while it rains on
              a Saturday or Sunday, when the line needs none - and at most every RAIN_REFETCH (30 min).
              One older than upcoming_rain.MAX_AGE is not used: no forecast part shows then, while
              "Raining now" stays.
    transit   one line for each of his routes (the `transit` list) whose status is Delayed or Disrupted
              and whose data is at most transit_max_age_min old: the first upcoming cancelled departure
              ("S-tog: cancelled 07:42"), else the first late one ("S-tog: 07:42 +12 min"), else the status.
    rows      from sensor.house_upcoming while its lease (`expires`) holds, never a row that asks the next
              chip to pass it over or names other people. First what is due by 7 days from now, earliest
              first: a row timed in the past or inside 7 days, a one-off task that lost its time on its
              day ("Due dd.mm" info), a calendar plant that is due ("due today", "due since dd.mm"
              detail) and any other row with no time (an offline battery, a dry sensor plant), which is
              due now, as it is on the dashboard. Then the rows of the zone's own area (the zone doubles
              as the HA area id) timed within 14 days. A line is the row's title, cut back to a whole word
              and ended in "..." when it is longer than LINE_MAX; its due chip counts house days.

The weighing: the scale claims a weighing for Mikkel only while his phone is in the bathroom. It is a change
of the state of measured_entity (sensor.mikkel_last_measurement, a timestamp) to a moment at most WEIGH_FRESH
(120 s) before now; the entity is also updated, with its old state, when HA restarts or the integration
reloads. One that arrives while the room is active identifies Mikkel for the visit and starts the card; in
any other state of the room it is ignored. At once the app asks the recorder, in one recorder.get_statistics
call, for the weight and the body fat of 30 days ago: the hourly mean row nearest to now - 30 d within 3
days of it, among the rows that have both. The card waits for the answer (or its failure) and shows only the
values the weight and fat sensors took in this weighing, those updated within WEIGH_SPREAD (30 s) of the
measurement sensor, so a fat percentage the scale did not measure is left out and not repeated from an
earlier weighing. Its lines, with a decimal point and a true minus:
    "73.3 kg · 20.0% fat"     mdi:scale-bathroom; just "73.3 kg" without a fat percentage
    "Fat 30 d: −0.4 kg"       the change in fat mass (weight x fat % / 100) since then
    "Lean 30 d: +0.6 kg"      the change in the rest
A change of at most DEAD_BAND_KG (0.3 kg) either way reads "±0 kg". With no row to compare with, or no fat
percentage, only the first line shows.

State acquisition follows HouseUpcoming: one snapshot of each source at initialize, then
listen_state(attribute="all"), and a value from a callback is never replaced by a later read, because
AppDaemon 4.5.13's state store can lag behind HA. The freshness of a reading is its own last_updated, so a
late callback or a stale snapshot does not look new. Every TICK_S seconds everything is evaluated again, which
is how a reading that stops coming, a lease that runs out, an old transit report and a due chip that rolls
over reach the display, and so is the end of the card; the result is published only when it differs from the
last one published, and always on plugin_started, since HA forgets a set_state entity when it restarts.
The recorder's answer and the forecast are fetched like HouseUpcoming's: an async method awaits the service
call (with a timeout of FETCH_TIMEOUT_S), leaves the result in a hand-over and has run_in(.., 0) apply it.

The play button: the display fires the HA event play_event (esphome.bathroom_display_play) when its round
button is tapped. The app then reads the speaker (media_player.bathroom) afresh and play_action() decides: a
speaker that is playing is paused and one that is paused plays on; anything else (idle, off, standby,
unavailable, unreadable) starts `radio` (dr_podcasts://radio/p3) as music, and a speaker whose group_members
holds more than one member first leaves its group (media_player/unjoin) and starts the radio a second later
(RADIO_DELAY_S). A tap logs one line, and a service call that fails logs a warning.

The alarm button: the display fires the HA event alarm_event (esphome.bathroom_display_alarm) when the alarm
line is tapped, in a chip or on the night face. The app then toggles alarm_entity, and the line follows
the entity's new state like any other source. A tap logs one line, and a failed call logs a warning.
"""

import asyncio
import re
import statistics
from datetime import date, datetime, time, timedelta, timezone
from typing import NamedTuple

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import climate_model as cm  # apps/climate - AppDaemon puts every app dir on sys.path
import room_active_read  # apps/presence
import upcoming_core as core
import upcoming_maint as maint
import upcoming_rain as rain
import upcoming_train as train

TICK_S = 10
LINE_MAX = 32
LINES = 3
RESIDENT = "Mikkel"
ZONE = "bathroom"
RSSI_MIN = -75.0
RISE_MIN = -80.0
RISE_DB = 6.0
UNHEARD = -100.0
BASELINE_FROM_S = 120
BASELINE_TO_S = 15
HISTORY_S = 180
RSSI_FRESH_S = 20.0
IDENTIFIED = ("near", "rise")
TRANSIT_MAX_AGE_MIN = 45.0
LATE_MIN = 10
SOON = timedelta(days=7)
AHEAD = timedelta(days=14)
BAD_STATUS = ("Delayed", "Disrupted")
ICON = "mdi:monitor-dashboard"
TRANSIT_ICON = "mdi:train"
ICON_MAX = 80
PLAY_EVENT = "esphome.bathroom_display_play"
SPEAKER = "media_player.bathroom"
RADIO = "dr_podcasts://radio/p3"
RADIO_TYPE = "music"
RADIO_DELAY_S = 1
MEASURED = "sensor.mikkel_last_measurement"
WEIGHT = "sensor.mikkel_weight"
BODY_FAT = "sensor.mikkel_body_fat"
WEIGH_FRESH = timedelta(seconds=120)
WEIGH_SPREAD = timedelta(seconds=30)
CARD_S = 90
COMPARE_AGO = timedelta(days=30)
COMPARE_SLACK = timedelta(days=3)
DEAD_BAND_KG = 0.3
SCALE_ICON = "mdi:scale-bathroom"
FAT_ICON = "mdi:water-percent"
LEAN_ICON = "mdi:arm-flex"
PIEZO = "binary_sensor.gw2000a_rain_state_piezo"
WEATHER = "weather.forecast_home"
RAIN_ICON = "mdi:weather-rainy"
RAIN_FROM = time(6)
RAIN_TO = time(12)
HOME_FROM = time(16)
HOME_TO = time(20)
RAIN_AHEAD = timedelta(hours=4)
RAIN_REFETCH = timedelta(minutes=30)
ALARM = "input_boolean.wakeup_bedroom"
ALARM_TIME = "input_datetime.wakeup_bedroom"
ALARM_ICON = "mdi:alarm"
ALARM_FROM_ENTITY = "input_datetime.bathroom_display_alarm_from"
ALARM_FROM = time(21)
ALARM_TO = time(2)
ALARM_EVENT = "esphome.bathroom_display_alarm"
FETCH_TIMEOUT_S = 12

_DUE_INFO = re.compile(r"Due (\d\d)\.(\d\d)(?:\.(\d\d))?")
_DUE_DETAIL = re.compile(r"due (?:today|since (\d\d)\.(\d\d)(?:\.(\d\d))?)")


class Line(NamedTuple):
    text: str
    icon: str = ""
    due: str = ""


# -- the phone ----------------------------------------------------------------


def phone_reading(full):
    """(dBm, moment of the update) of the RSSI sensor's state dict; None for a part it does not have. A
    state that is not a finite number (unknown, unavailable) has no dBm."""
    if not isinstance(full, dict):
        return None, None
    return maint.reading(full.get("state")), maint.parse_iso(full.get("last_updated"))


def remember(history, full, now):
    """`history` - [(moment of the update, dBm)] - with the reading of the RSSI sensor's state dict `full`
    added, unless it is not a number, undated or already in it (one update has one last_updated), and
    whatever is more than HISTORY_S old at `now` dropped."""
    value, at = phone_reading(full)
    span = timedelta(seconds=HISTORY_S)
    kept = [(when, dbm) for when, dbm in history if now - when <= span]
    if value is not None and at is not None and now - at <= span and all(when != at for when, _ in kept):
        kept.append((at, value))
    return kept


def baseline_at(history, started):
    """The phone's baseline for a visit that started at `started`: the median dBm of the readings in `history`
    that were updated from BASELINE_FROM_S down to BASELINE_TO_S seconds before it, both ends included,
    UNHEARD when there are none."""
    oldest, newest = timedelta(seconds=BASELINE_FROM_S), timedelta(seconds=BASELINE_TO_S)
    levels = [dbm for when, dbm in history if newest <= started - when <= oldest]
    return statistics.median(levels) if levels else UNHEARD


def phone_status(value, at, now, rssi_min, fresh, baseline=None, rise_min=RISE_MIN, rise_db=RISE_DB):
    """"none" without a dBm, "stale" once it is older than `fresh` or undated, "near" at or above `rssi_min`,
    "rise" at or above `rise_min` and at least `rise_db` above `baseline` (None: no baseline, so no rise),
    else "weak"."""
    if value is None:
        return "none"
    if at is None or now - at > fresh:
        return "stale"
    if value >= rssi_min:
        return "near"
    return "rise" if baseline is not None and value >= rise_min and value - baseline >= rise_db else "weak"


def next_state(prev, active, status, weighed=False):
    """The state after an evaluation: `prev` is the one before it (None before the first), `active` what
    room_active_read says (True, False or None), `status` the phone's and `weighed` whether a weighing of his
    has just arrived. An inactive room is off. In an active one a phone that is near or has risen, or a
    weighing, identifies Mikkel, who then stays identified, whatever the phone does, for as long as the room
    stays active; until then it is someone. No opinion on the room keeps `prev`, which is off before there
    is one."""
    if active is None:
        return prev or "off"
    if not active:
        return "off"
    return "mikkel" if prev == "mikkel" or weighed or status in IDENTIFIED else "someone"


def kept_dark(sleep_state, light_state):
    """True while sleep mode is on and the main spots are not: the display then stays off. A sleep mode
    that cannot be read is not on, and a light that cannot be read is not on."""
    return sleep_state == "on" and light_state != "on"


def reason_text(active_entity, active, state, status):
    """Why `state`, in RoomActive's style. While active it names the phone's status; a mikkel whose phone is
    neither near nor rising right now is "held"."""
    if active is None:
        return f"unknown:{active_entity}"
    if not active:
        return f"inactive:{active_entity}"
    word = "held" if state == "mikkel" and status not in IDENTIFIED else status
    return f"active:{active_entity} phone:{word}"


# -- lines --------------------------------------------------------------------


def fit(title, limit=LINE_MAX):
    """`title` on one line of at most `limit` characters: one that is longer is cut back to a whole word where
    it can be and ends in "..."."""
    if len(title) <= limit:
        return title
    head = title[:limit - 3]
    if " " in head and head[-1] != " " and title[limit - 3] != " ":
        head = head.rsplit(" ", 1)[0]
    return head.rstrip(" ,:;-+") + "..."


def transit_line(label, full, now, max_age):
    """The title for a route whose status is Delayed or Disrupted and whose data is at most `max_age` old, else
    "". With a cancelled departure ahead (the first one, whatever else is wrong, when Disrupted) it names
    that, otherwise the first one late by delay_threshold_min, otherwise the status. `full` is the state
    dict of the sensor TransitStatus writes; departures need the `ts` it adds with expose_departure_ts."""
    state = full.get("state") if isinstance(full, dict) else None
    if state not in BAD_STATUS:
        return ""
    attrs = full.get("attributes") if isinstance(full.get("attributes"), dict) else {}
    as_of = maint.parse_iso(attrs.get("data_as_of"))
    if as_of is None or now - as_of > max_age:
        return ""
    try:
        threshold = float(attrs.get("delay_threshold_min") or LATE_MIN)
    except (TypeError, ValueError):
        threshold = LATE_MIN
    problems = [dep for dep in train.departures(attrs.get("departures"), now) if dep[3] or dep[2] >= threshold]
    if state == "Disrupted":
        problems = [dep for dep in problems if dep[3]] or problems
    if not problems:
        return fit(f"{label}: {state.lower()}")
    _, scheduled, delay, cancelled = problems[0]
    return fit(f"{label}: " + (f"cancelled {scheduled}" if cancelled else f"{scheduled} +{delay} min"))


def transit_lines(routes, now, max_age):
    """The lines of [(label, state dict)] that have one, in order."""
    texts = (transit_line(label, full, now, max_age) for label, full in routes)
    return [Line(text, TRANSIT_ICON) for text in texts if text]


def live_rows(full, now):
    """The rows of the sensor.house_upcoming state dict while its lease holds, else none: an `expires` that is
    missing, unreadable or past means the publisher has stopped."""
    attrs = full.get("attributes") if isinstance(full, dict) else None
    attrs = attrs if isinstance(attrs, dict) else {}
    expires, rows = maint.parse_iso(attrs.get("expires")), attrs.get("rows")
    return rows if expires is not None and expires > now and isinstance(rows, list) else []


def _day(match, today):
    """The house day a "dd.mm" or "dd.mm.yy" match names: `today` for a match with no date in it, None for a
    date that does not exist."""
    day, month, year = match.groups()
    if day is None:
        return today
    try:
        return date(2000 + int(year) if year else today.year, int(month), int(day))
    except ValueError:
        return None


def due_at(row, now):
    """When `row` is due: its `at`; else, for a Done task that has lost its time on the day it is due, that day
    at maint.DUE_TIME - a one-off task's "Due dd.mm" info line (upcoming_once) or a calendar plant's "due
    today" / "due since dd.mm" detail (upcoming_plants); else `now`, since the dashboard counts a row with no
    time as due now (an offline battery, a dry sensor plant)."""
    at = maint.parse_iso(row.get("at"))
    if at is not None:
        return at
    if row.get("done") != "tap":
        return now
    info = row.get("info") if isinstance(row.get("info"), list) else []
    for text, pattern in [(line, _DUE_INFO) for line in info] + [(row.get("detail"), _DUE_DETAIL)]:
        match = pattern.fullmatch(text) if isinstance(text, str) else None
        if match:
            day = _day(match, maint.house_date(now))
            return datetime.combine(day, maint.DUE_TIME, maint.HOUSE_TZ) if day else now
    return now


def due_chip(due, now):
    """overdue, today or tomorrow by house day, else the days ahead as "6 d"."""
    days = (maint.house_date(due) - maint.house_date(now)).days
    if days < 0:
        return "overdue"
    return "today" if days == 0 else "tomorrow" if days == 1 else f"{days} d"


def _wanted(row, resident):
    """Whether `row` may be a line for `resident`: it has a title, does not ask the next chip to pass it, and
    names nobody or names him."""
    if not isinstance(row, dict) or row.get("next") == "skip" or not core.clean_text(row.get("title")):
        return False
    users = row.get("audience_users")
    if users is None:
        return True
    return isinstance(users, list) and resident.lower() in {str(user).strip().lower() for user in users}


def _icon(row):
    icon = row.get("icon")
    return core.clean_text(icon, ICON_MAX) if isinstance(icon, str) and icon.startswith("mdi:") else ""


def task_lines(rows, now, resident=RESIDENT, area=ZONE, limit=LINES):
    """At most `limit` lines from the house_upcoming rows: what is due by SOON from now (including anything
    overdue or due now), earliest first, then the rows of `area` due within AHEAD, earliest first."""
    soon, later = [], []
    for row in rows if isinstance(rows, list) else []:
        if not _wanted(row, resident):
            continue
        due = due_at(row, now)
        if due <= now + SOON:
            soon.append((due, str(row.get("id")), row))
        elif row.get("area") == area and due <= now + AHEAD:
            later.append((due, str(row.get("id")), row))
    picked = sorted(soon, key=lambda item: item[:2]) + sorted(later, key=lambda item: item[:2])
    return [Line(fit(core.clean_text(row.get("title"))), _icon(row), due_chip(due, now))
            for due, _, row in picked[:max(0, limit)]]


def in_window(now, start, end):
    """Whether the house clock at `now` is from `start` up to, not including, `end`; a window that ends before
    it starts runs through midnight."""
    clock = now.astimezone(maint.HOUSE_TZ).time()
    if start <= end:
        return start <= clock < end
    return clock >= start or clock < end


# -- the weighing card --------------------------------------------------------


def weighing_moment(previous, full, now):
    """The moment of a new weighing, else None. `full` is the state dict of the measurement sensor and
    `previous` its state before it: a weighing is a change of the state to a moment from WEIGH_FRESH before
    `now` up to `now`. The same state again, an older moment (HA restarting gives the sensor its last one
    again) and a state that is no moment are not."""
    state = full.get("state") if isinstance(full, dict) else None
    moment = maint.parse_iso(state)
    if moment is None or state == previous:
        return None
    return moment if timedelta(0) <= now - moment <= WEIGH_FRESH else None


def weighed_value(full, anchor):
    """The number in the state dict of the weight or the fat sensor if it was updated within WEIGH_SPREAD of
    `anchor`, the update of the measurement sensor, which a weighing updates with them. None for a sensor
    that has kept its old value and update, or has no number."""
    if not isinstance(full, dict) or anchor is None:
        return None
    value, at = maint.reading(full.get("state")), maint.parse_iso(full.get("last_updated"))
    return value if value is not None and at is not None and abs(at - anchor) <= WEIGH_SPREAD else None


def statistics_rows(resp):
    """{statistic id: [row]} from the answer to recorder.get_statistics as AppDaemon hands it over, the rows
    ({"start", "end", "mean"}, ISO times) being under result, response and statistics; {} for anything else."""
    node = resp
    for key in ("result", "response", "statistics"):
        if isinstance(node, dict) and key in node:
            node = node[key]
    return node if isinstance(node, dict) else {}


def _means(rows):
    """{start of the hour: mean} of the rows that have both as usable values."""
    means = {}
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict):
            start, mean = maint.parse_iso(row.get("start")), maint.reading(row.get("mean"))
            if start is not None and mean is not None:
                means[start] = mean
    return means


def before_values(resp, ids, target):
    """(weight, fat percentage) of the hourly row nearest to `target` and within COMPARE_SLACK of it, among
    the rows that both of `ids` (weight, body fat) have in the answer `resp`; None when there is none."""
    stats = statistics_rows(resp)
    weight, fat = (_means(stats.get(statistic_id)) for statistic_id in ids)
    near = [(abs(start - target), start) for start in weight if start in fat and abs(start - target) <= COMPARE_SLACK]
    if not near:
        return None
    start = min(near)[1]
    return weight[start], fat[start]


def kg_text(value):
    """`value` with one decimal."""
    return f"{value:.1f}"


def change_text(delta):
    """A change in kg as it reads on a line: "±0" while it is at most DEAD_BAND_KG either way, else its size
    after a plus or a true minus."""
    if abs(delta) <= DEAD_BAND_KG + 1e-9:
        return "±0"
    return ("+" if delta > 0 else "−") + kg_text(abs(delta))


def weighing_lines(kg, percent, before):
    """The card for a weighing of `kg` with body fat `percent` (None when the scale gave none): the weight
    and fat as one line, then, given `before` - (kg, percent) from COMPARE_AGO - the change since then in fat
    mass (kg x percent / 100) and in the rest. The text under each chip is its value."""
    first = f"{kg_text(kg)} kg"
    lines = [Line(fit(first if percent is None else f"{first} · {kg_text(percent)}% fat"), SCALE_ICON, first)]
    if percent is None or before is None:
        return lines
    fat, fat_before = kg * percent / 100, before[0] * before[1] / 100
    for icon, name, delta in ((FAT_ICON, "Fat", fat - fat_before),
                              (LEAN_ICON, "Lean", (kg - fat) - (before[0] - fat_before))):
        change = f"{change_text(delta)} kg"
        lines.append(Line(fit(f"{name} {COMPARE_AGO.days} d: {change}"), icon, change))
    return lines


# -- rain and the alarm -------------------------------------------------------


def forecast_run(hours, now, ahead=RAIN_AHEAD):
    """(start of the first hour, end of the last) of the first run of consecutive forecast hours with at least
    rain.MIN_MM that starts within `ahead` of `now`, else None. `hours` is [(hour start, mm)], soonest first,
    as rain.forecast_hours gives it: an hour that is going on counts, one that is over does not, and a missing
    hour ends a run."""
    run = None
    for start, mm in hours:
        if start + rain.HOUR <= now:
            continue
        if run is None:
            if start >= now + ahead:
                return None
            if mm >= rain.MIN_MM:
                run = (start, start + rain.HOUR)
        elif start == run[1] and mm >= rain.MIN_MM:
            run = (run[0], start + rain.HOUR)
        else:
            break
    return run


def workday(now):
    """Whether the house date of `now` is a Monday to Friday; holidays are not known."""
    return now.astimezone(maint.HOUSE_TZ).weekday() < 5


def home_run(hours, now):
    """The first run of rain, as forecast_run reads one, among the forecast `hours` that start inside today's
    HOME_FROM-HOME_TO window in the house: (start of its first hour, end of its last), else None. An hour that
    starts outside the window is not looked at, so a run ends with the window at the latest."""
    start, end = (datetime.combine(maint.house_date(now), edge, maint.HOUSE_TZ) for edge in (HOME_FROM, HOME_TO))
    return forecast_run([(hour, mm) for hour, mm in hours if start <= hour < end], start, end - start)


def _house_hours(run):
    """The house hours a run starts and ends at, as ("09", "11")."""
    return tuple(moment.astimezone(maint.HOUSE_TZ).strftime("%H") for moment in run)


def rain_line(raining, hours, fetched, now):
    """The rain on the way out and, on a workday, on the way home. Out: "Raining now" while it `raining`, else
    the forecast's first run of rain as "Rain 09-11" (house hours), or "Rain from 09" when it goes on past
    RAIN_AHEAD from `now`. Home: the first run inside today's HOME_FROM-HOME_TO as "home 17-19". Both make
    "Rain 09-11 · home 17-19" and home alone "Rain home 17-19". The text under the chip is the way out's value
    ("09-11", "from 09", "now"), else the way home's ("17-19"). None for no rain. The forecast parts need one
    that was fetched, at `fetched`, no more than rain.MAX_AGE ago; "Raining now" does not."""
    fresh = fetched is not None and now - fetched <= rain.MAX_AGE
    out = home = None
    if raining:
        out = ("Raining now", "now")
    elif fresh:
        run = forecast_run(hours, now)
        if run:
            start, end = _house_hours(run)
            when = f"from {start}" if run[1] > now + RAIN_AHEAD else f"{start}-{end}"
            out = (f"Rain {when}", when)
    if fresh and workday(now):
        run = home_run(hours, now)
        if run:
            span = "-".join(_house_hours(run))
            home = (f"home {span}", span)
    parts = [part for part in (out, home) if part]
    if not parts:
        return None
    joined = " · ".join(title for title, _ in parts)
    return Line(joined if out else f"Rain {joined}", RAIN_ICON, parts[0][1])


def alarm_start(clock):
    """When the alarm line starts showing in the evening: `clock`, the state of the helper input_datetime
    ("21:00:00"), as a time; ALARM_FROM for one that cannot be read as a time."""
    try:
        return time.fromisoformat(clock)
    except (TypeError, ValueError):
        return ALARM_FROM


def alarm_line(armed, clock):
    """The wake-up alarm as a line: "Alarm 09:15" while `armed` (the state of the alarm's input_boolean) is
    on, "No alarm" while it is off, none for any other state. `clock` is the state of the alarm's
    input_datetime ("09:15:00"); one that cannot be read leaves the time out."""
    if armed == "off":
        return Line("No alarm", ALARM_ICON, "off")
    if armed != "on":
        return None
    try:
        at = time.fromisoformat(clock).strftime("%H:%M")
    except (TypeError, ValueError):
        return Line("Alarm", ALARM_ICON)
    return Line(f"Alarm {at}", ALARM_ICON, at)


def payload(state, reason, resident, lines, night=False):
    """The attributes of sensor.bathroom_display: strings only, the lines padded with empty ones."""
    mikkel = state == "mikkel"
    attributes = {"friendly_name": "Bathroom display", "icon": ICON, "reason": reason,
                  "person": resident.lower() if mikkel else "", "greeting": f"Hi {resident}" if mikkel else "",
                  "night": "on" if night else ""}
    for number, line in enumerate((list(lines) + [Line("")] * LINES)[:LINES], 1):
        attributes.update({f"line{number}": line.text, f"icon{number}": line.icon, f"due{number}": line.due})
    return attributes


# -- the play button ----------------------------------------------------------


def play_action(state, group_members):
    """What a tap of the play button does for a speaker in `state` whose group_members attribute is
    `group_members`: "pause" while it is playing and "play" while it is paused. Anything else - idle, off,
    standby, unavailable, unreadable - starts the radio: "radio" at once, or "unjoin_radio" when the speaker is
    grouped (`group_members` is a list of more than one), so that it plays on its own."""
    if state == "playing":
        return "pause"
    if state == "paused":
        return "play"
    return "unjoin_radio" if isinstance(group_members, list) and len(group_members) > 1 else "radio"


class BathroomDisplay(hass.Hass):
    def initialize(self):
        a = self.args
        self.entity = a.get("entity", "sensor.bathroom_display")
        self.zone = a.get("zone", ZONE)
        self.active_entity = f"binary_sensor.{self.zone}_active"
        self.resident = str(a.get("resident", RESIDENT))
        self.rssi_entity = a.get("rssi_entity", "sensor.bathroom_display_mikkel_phone_rssi")
        self.rssi_min = float(a.get("mikkel_rssi_min", RSSI_MIN))
        self.rise_min = float(a.get("rssi_rise_min", RISE_MIN))
        self.rise_db = float(a.get("rssi_rise_db", RISE_DB))
        self.rssi_fresh = timedelta(seconds=float(a.get("rssi_fresh_s", RSSI_FRESH_S)))
        self.upcoming_entity = a.get("upcoming_entity", "sensor.house_upcoming")
        self.routes = [(str(route["label"]), route["entity"]) for route in a.get("transit") or []]
        self.transit_max_age = timedelta(minutes=float(a.get("transit_max_age_min", TRANSIT_MAX_AGE_MIN)))
        self.sleep_entity = a.get("sleep_entity", "input_boolean.mikkel_sleep_mode")
        self.light_entity = a.get("light_entity", "light.bathroom_main_lights")
        self.play_event = a.get("play_event", PLAY_EVENT)
        self.speaker = a.get("speaker", SPEAKER)
        self.radio = str(a.get("radio", RADIO))
        self.measured_entity = a.get("measured_entity", MEASURED)
        self.weight_entity = a.get("weight_entity", WEIGHT)
        self.fat_entity = a.get("body_fat_entity", BODY_FAT)
        self.rain_entity = a.get("rain_entity", PIEZO)
        self.weather_entity = a.get("weather_entity", WEATHER)
        self.alarm_entity = a.get("alarm_entity", ALARM)
        self.alarm_time_entity = a.get("alarm_time_entity", ALARM_TIME)
        self.alarm_from_entity = a.get("alarm_from_entity", ALARM_FROM_ENTITY)
        self.alarm_event = a.get("alarm_event", ALARM_EVENT)

        self._cache = {}
        self._history = []  # (last_updated, dBm) of the phone's readings of the last HISTORY_S seconds
        self._baseline = None  # the phone's baseline for the visit under way, set when it starts
        self._current = None  # (state, attributes) as last worked out
        self._published = None  # (state, attributes) as last handed to HA
        self._weighing = None  # the latest weighing: seen, updated, ready, before
        self._forecast = None  # (hours, when it was asked for) of the last hourly forecast
        self._forecast_asked = None  # when the forecast was last asked for, answered or not
        self._handoff = {}  # what an async fetch leaves for the callback that applies it

        for entity in self._sources():
            self._store(entity, self._read(entity))
            self.listen_state(self._on_source, entity, attribute="all")
        self.listen_state(self._on_active, self.active_entity)
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.listen_event(self._on_play, self.play_event)
        self.listen_event(self._on_alarm, self.alarm_event)
        self.run_every(self._on_tick, f"now+{TICK_S}", TICK_S)

        self._evaluate(publish=True)
        self.log(f"BathroomDisplay initialized - {self.entity} from {self.active_entity}, "
                 f"phone >= {self.rssi_min:g} dBm, or >= {self.rise_min:g} dBm and {self.rise_db:g} dB over its "
                 f"baseline, within {self.rssi_fresh.total_seconds():g} s, {len(self.routes)} transit routes, "
                 f"{self.play_event} works {self.speaker}, {self.alarm_event} toggles {self.alarm_entity}")

    # -- sources --------------------------------------------------------------

    def _sources(self):
        return list(dict.fromkeys([self.rssi_entity, self.upcoming_entity, self.sleep_entity, self.light_entity,
                                   self.measured_entity, self.weight_entity, self.fat_entity, self.rain_entity,
                                   self.alarm_entity, self.alarm_time_entity, self.alarm_from_entity]
                                  + [entity for _, entity in self.routes]))

    def _read(self, entity):
        try:
            full = self.get_state(entity, attribute="all")
        except Exception as e:
            self.log(f"Read of {entity} failed: {e!r}", level="WARNING")
            return None
        return full if isinstance(full, dict) else None

    def _store(self, entity, full):
        self._cache[entity] = full
        if entity == self.rssi_entity:
            self._history = remember(self._history, full, self.get_now())

    # -- callbacks ------------------------------------------------------------

    def _on_source(self, entity, attribute, old, new, kwargs):
        new = new if isinstance(new, dict) else None
        moment = None
        if entity == self.measured_entity:
            moment = weighing_moment((self._cache.get(entity) or {}).get("state"), new, self.get_now())
        self._store(entity, new)
        self._evaluate(weighed=moment is not None and self._begin_weighing(moment, new))

    def _on_active(self, entity, attribute, old, new, kwargs):
        self._evaluate()

    def _on_plugin_started(self, event_name, data, kwargs):
        self._evaluate(publish=True)

    def _on_tick(self, kwargs):
        for entity, cached in list(self._cache.items()):
            if cached is None:
                self._store(entity, self._read(entity))
        self._evaluate()

    def _on_play(self, event_name, data, kwargs):
        full = self._read(self.speaker) or {}
        attrs = full.get("attributes") if isinstance(full.get("attributes"), dict) else {}
        state = full.get("state")
        action = play_action(state, attrs.get("group_members"))
        self.log(f"Play button: {self.speaker} is {state} -> {action}")
        if action == "pause":
            self._call("media_player/media_pause", entity_id=self.speaker)
        elif action == "play":
            self._call("media_player/media_play", entity_id=self.speaker)
        elif action == "radio":
            self._start_radio({})
        else:
            self._call("media_player/unjoin", entity_id=self.speaker)
            self.run_in(self._start_radio, RADIO_DELAY_S)

    def _start_radio(self, kwargs):
        self._call("media_player/play_media", entity_id=self.speaker, media_content_id=self.radio,
                   media_content_type=RADIO_TYPE)

    def _on_alarm(self, event_name, data, kwargs):
        self.log(f"Alarm button: toggles {self.alarm_entity}")
        self._call("homeassistant/toggle", entity_id=self.alarm_entity)

    def _on_card_end(self, kwargs):
        self._evaluate()

    def _call(self, service, **kwargs):
        try:
            self.call_service(service, **kwargs)
        except Exception as e:
            self.log(f"{service} failed: {e!r}", level="WARNING")

    # -- weighing and forecast ------------------------------------------------

    def _begin_weighing(self, moment, full):
        """A weighing at `moment` has arrived: while the room is active it starts the card and the look at
        30 days ago, and True is returned; otherwise it is ignored."""
        if room_active_read.active(self, self.zone) is not True:
            self.log(f"Weighing at {core.utc_iso(moment)} ignored, {self.active_entity} is not active")
            return False
        now = self.get_now()
        self._weighing = {"seen": now, "updated": maint.parse_iso(full.get("last_updated")) or now, "ready": False,
                          "before": None}
        self.log(f"Weighing at {core.utc_iso(moment)}: the card for {CARD_S} s")
        self.run_in(self._on_card_end, CARD_S)
        self.create_task(self._fetch_comparison(now))
        return True

    async def _fetch_comparison(self, seen):
        before = None
        try:
            target = seen.astimezone(timezone.utc) - COMPARE_AGO
            resp = await asyncio.wait_for(
                self.call_service("recorder/get_statistics", statistic_ids=[self.weight_entity, self.fat_entity],
                                  start_time=core.utc_iso(target - COMPARE_SLACK),
                                  end_time=core.utc_iso(target + COMPARE_SLACK), period="hour", types=["mean"],
                                  return_response=True),
                timeout=FETCH_TIMEOUT_S)
            before = before_values(resp, (self.weight_entity, self.fat_entity), target)
        except Exception as e:
            self.log(f"Statistics fetch failed: {e!r}", level="WARNING")
        finally:
            self._handoff["weighing"] = (seen, before)
            await self.run_in(self._apply_comparison, 0)

    def _apply_comparison(self, kwargs):
        seen, before = self._handoff.pop("weighing", (None, None))
        if self._weighing and self._weighing["seen"] == seen:
            self._weighing.update(ready=True, before=before)
            found = f"{before[0]:g} kg, {before[1]:g} % fat" if before else "none"
            self.log(f"The row {COMPARE_AGO.days} d before the weighing: {found}")
        self._evaluate()

    async def _fetch_forecast(self, asked):
        entries = None
        try:
            resp = await asyncio.wait_for(
                self.call_service("weather/get_forecasts", entity_id=self.weather_entity, type="hourly",
                                  return_response=True),
                timeout=FETCH_TIMEOUT_S)
            entries = cm.parse_forecast_envelope(resp, self.weather_entity)
        except Exception as e:
            self.log(f"Forecast fetch failed: {e!r}", level="WARNING")
        finally:
            self._handoff["forecast"] = (asked, entries)
            await self.run_in(self._apply_forecast, 0)

    def _apply_forecast(self, kwargs):
        asked, entries = self._handoff.pop("forecast", (None, None))
        if asked is not None and entries is not None:
            self._forecast = (rain.forecast_hours(entries), asked)
            self.log(f"Forecast: {len(self._forecast[0])} hours")
        self._evaluate()

    # -- evaluation -----------------------------------------------------------

    def _evaluate(self, publish=False, weighed=False):
        active = room_active_read.active(self, self.zone)
        prev = self._current[0] if self._current else None
        if active is not None or prev is None:
            self._current = self._compute(prev, active, weighed)
            if self._current[0] != prev:
                self.log(f"{self._current[0]} ({self._current[1]['reason']})")
        if publish or self._current != self._published:
            self._publish(*self._current)

    def _compute(self, prev, active, weighed=False):
        now = self.get_now()
        night = self._state_of(self.sleep_entity) == "on"
        if active and kept_dark(self._state_of(self.sleep_entity), self._state_of(self.light_entity)):
            return "off", payload("off", f"sleep:{self.sleep_entity}", self.resident, self._lines(now, "off", night),
                                  night)
        if active and prev in (None, "off"):
            self._baseline = baseline_at(self._history, now)
        value, at = phone_reading(self._cache.get(self.rssi_entity))
        status = phone_status(value, at, now, self.rssi_min, self.rssi_fresh, self._baseline, self.rise_min,
                              self.rise_db)
        state = next_state(prev, active, status, weighed)
        reason = reason_text(self.active_entity, active, state, status)
        return state, payload(state, reason, self.resident, self._lines(now, state, night), night)

    def _state_of(self, entity):
        full = self._cache.get(entity)
        return full.get("state") if isinstance(full, dict) else None

    def _lines(self, now, state, night):
        alarm = alarm_line(self._state_of(self.alarm_entity), self._state_of(self.alarm_time_entity))
        alarm = [alarm] if alarm else []
        if night:
            return alarm
        if state != "mikkel":
            return []
        card = self._card(now)
        if card:
            return card
        lines = alarm if in_window(now, alarm_start(self._state_of(self.alarm_from_entity)), ALARM_TO) else []
        if in_window(now, RAIN_FROM, RAIN_TO):
            lines = lines + self._rain(now)
        routes = [(label, self._cache.get(entity)) for label, entity in self.routes]
        lines = lines + transit_lines(routes, now, self.transit_max_age)
        rows = live_rows(self._cache.get(self.upcoming_entity), now)
        return (lines + task_lines(rows, now, self.resident, self.zone, LINES - len(lines)))[:LINES]

    def _card(self, now):
        """The weighing card while it shows: CARD_S from the weighing, once the look at 30 days ago is in, and
        with the weight the weighing gave."""
        weighing = self._weighing
        if not weighing or not weighing["ready"] or now - weighing["seen"] >= timedelta(seconds=CARD_S):
            return []
        kg = weighed_value(self._cache.get(self.weight_entity), weighing["updated"])
        if kg is None:
            return []
        percent = weighed_value(self._cache.get(self.fat_entity), weighing["updated"])
        return weighing_lines(kg, percent, weighing["before"])

    def _rain(self, now):
        """The rain line, as a list. The hourly forecast is asked for, at most every RAIN_REFETCH, unless it
        rains on a Saturday or Sunday, when the line needs none."""
        raining = self._state_of(self.rain_entity) == "on"
        wanted = workday(now) or not raining
        if wanted and (self._forecast_asked is None or now - self._forecast_asked >= RAIN_REFETCH):
            self._forecast_asked = now
            self.create_task(self._fetch_forecast(now))
        hours, fetched = self._forecast or ([], None)
        line = rain_line(raining, hours, fetched, now)
        return [line] if line else []

    def _publish(self, state, attributes):
        try:
            self.set_state(self.entity, state=state, attributes=attributes, replace=True)
        except Exception as e:
            self.log(f"Publish failed: {e!r}", level="WARNING")
            return
        self._published = (state, attributes)
