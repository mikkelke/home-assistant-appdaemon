"""Run-based maintenance rows for sensor.house_upcoming: the dishwasher's hygiene run, filters and spray
arms and general check, the washer's drum clean and inlet filters, the dryer's plinth filter, and the
xBloom's brew head, water tank, grinder burrs and descale, and the stick vacuum's filters. A task is
due after so many finished runs (run_ledger) since it was last done, which suits parts that clog with
use better than the calendar. A task counts in its unit: the coffee machine's runs are brews, the
stick vacuum's are cleaner visits. Its filter flag makes the filter task due regardless of the count.
Pure: each step takes the persisted anchors and `now` and returns new state.

A task is closed one of two ways. A confirmed task is closed by a person confirming a run as its
programme, which is the dishwasher's Intensive run and the washer's drum clean; the ledger's records
say when. Without one on record the task is due, its row says "last unknown", and it is never pushed:
missing evidence is not an overdue clean. A tapped task is closed by an admin tapping Done in the
row's sheet, which fires house_upcoming_done; the anchor `{at, runs}` says when and at which run
count, and keeps the anchor it replaced for one undo. Runs are counted from the time, not the count,
so a change to what counts as a run never shifts an anchor: the runs that finish after it, a run still
going at that moment included. A confirmed task counts the runs after the confirmed one. A tapped task
with no anchor is seeded at the moment it is first seen, so it does not open overdue.

A row says how many runs are left and, at the pace of the last 90 days, the day they run out (~dd.mm;
timed at DUE_TIME that day, beyond ROW_HORIZON_DAYS none; DEFAULT_PER_WEEK until the ledger covers
two weeks). It reads as an estimate, like the Rober2 parts' rows. Once due the row says so and carries
a push key for the episode, which is the time it was last closed. A tapped task's due row is untimed;
the drum clean's and the hygiene run's are timed at their cheapest start (upcoming_drum), kept out of
the next chip while that start is now. The hygiene run says the dishwasher Timer setting for its start.

A task without an interval is a log: its row counts the runs since it was last logged, is untimed and
out of the next chip, is never due and never pushed. The dryer's plinth filter is one until the
dryer's interval_runs is configured (house_upcoming.yaml), when it is a tapped task like the others.
Every row opens a sheet with what to do (upcoming_guides). The houseplants watered by the calendar
(upcoming_plants) keep their anchors, Done and undo here too.
"""

from datetime import datetime, timedelta
from typing import Callable, NamedTuple, Optional

import drum_clean  # apps/appliances - AppDaemon puts every app dir on sys.path
import run_ledger
from upcoming_core import make_row, utc_iso
from upcoming_drum import Start
from upcoming_guides import GUIDES
from upcoming_maint import DUE_TIME, HOUSE_TZ, ddmm, fit_parts, house_date, parse_iso

ROW_HORIZON_DAYS = 366
COFFEE_CLOSES = "Provisional interval: check the xBloom manual"
DEFAULT_PER_WEEK = {"washer": 3.5, "dishwasher": 3.5, "dryer": 2.4, "coffee": 3.4, "stick": 1.0}
WEEKS_PER_MONTH = 4.345


class Task(NamedTuple):
    key: str
    appliance: str
    title: str
    how: str
    interval: Optional[int]
    icon: str
    confirmed: Optional[Callable] = None
    closes: str = ""
    unit: str = "run"


TASKS = (
    Task("dishwasher:hygiene", "dishwasher", "Dishwasher hygiene run", "Intensive 75", 15, "mdi:dishwasher",
         confirmed=lambda led: run_ledger.last_confirmed(led.cycles, HYGIENE_PROGRAMME),
         closes="Logged when you pick Intensive on the dishwasher card"),
    Task("dishwasher:filters", "dishwasher", "Dishwasher filters + spray arms", "rinse filter + arms", 30,
         "mdi:dishwasher"),
    Task("dishwasher:check", "dishwasher", "Dishwasher general check", "seal, sides, inside", 60, "mdi:dishwasher"),
    Task("washer:drum", "washer", "Washer drum + drawer clean", "Bomuld 90 + drain filter, door seal",
         drum_clean.INTERVAL_RUNS, "mdi:washing-machine", confirmed=drum_clean.last_clean,
         closes="Logged when you pick Drum clean on the washer card"),
    Task("washer:inlet", "washer", "Washer inlet filters", "hose + valve filters", 90, "mdi:washing-machine"),
    Task("dryer:plinth", "dryer", "Dryer plinth filter", "plinth filter + air path", None, "mdi:tumble-dryer"),
    Task("coffee:rinse", "coffee", "xBloom brew head", "shower screen + scale plate", 10, "mdi:coffee-maker",
         closes=COFFEE_CLOSES, unit="brew"),
    Task("coffee:tank", "coffee", "xBloom water tank", "empty, rinse, air-dry", 20, "mdi:coffee-maker",
         closes=COFFEE_CLOSES, unit="brew"),
    Task("coffee:burrs", "coffee", "xBloom grinder burrs", "brush burrs + chute", 40, "mdi:coffee-maker",
         closes=COFFEE_CLOSES, unit="brew"),
    Task("coffee:descale", "coffee", "xBloom descale", "descaler, 2 rounds", 60, "mdi:coffee-maker",
         closes=COFFEE_CLOSES, unit="brew"),
    Task("stick:filters", "stick", "Stick vacuum filters", "wash filters + dry 24 h", 4, "mdi:vacuum", unit="visit"),
)
HYGIENE_KEY = "dishwasher:hygiene"
HYGIENE_PROGRAMME = "intensive"
DRUM_KEY = "washer:drum"
DRYER_KEY = "dryer:plinth"
STICK_KEY = "stick:filters"


class Status(NamedTuple):
    task: Task
    when: Optional[datetime]
    seeded: bool
    since: int
    left: Optional[int]
    per_week: float


def configured(dryer_interval=None):
    """TASKS with the dryer plinth filter's interval in runs, which keeps it a log while it is None."""
    if dryer_interval is None:
        return TASKS
    return tuple(task._replace(interval=dryer_interval) if task.key == DRYER_KEY else task for task in TASKS)


def interval_from(value):
    """The configured interval in runs: a whole number above zero, else None."""
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 1 else None


def row_id(task):
    return f"maint:{task.key}"


def tapped_task(row, tasks=TASKS):
    """The tapped task the row id `row` belongs to, None for any other id."""
    for task in tasks:
        if task.confirmed is None and row_id(task) == row:
            return task
    return None


def valid_anchors(raw, tasks=TASKS, extra=()):
    """The saved anchors that are for a tapped task, or for one of the `extra` keys, and carry a time."""
    raw = raw if isinstance(raw, dict) else {}
    keys = {task.key for task in tasks if task.confirmed is None} | set(extra)
    return {key: anchor for key, anchor in raw.items()
            if key in keys and isinstance(anchor, dict) and parse_iso(anchor.get("at")) is not None}


def seed(anchors, ledgers, now, tasks=TASKS):
    """`anchors` plus a seed for every tapped task of an appliance in `ledgers` that has none."""
    out = dict(anchors)
    for task in tasks:
        if task.confirmed is None and task.key not in out and task.appliance in ledgers:
            out[task.key] = {"at": utc_iso(now), "runs": len(ledgers[task.appliance].runs), "seed": True}
    return out


def mark_done(anchors, key, now, runs=None, extra=None):
    """Done at `now`, when the ledger held `runs` runs if there is one, with the `extra` fields the
    anchor carries; the anchor it replaces is kept for undo_done."""
    anchor = {"at": utc_iso(now), **({} if runs is None else {"runs": runs}), **(extra or {})}
    previous = anchors.get(key)
    if isinstance(previous, dict):
        anchor["prev"] = {k: v for k, v in previous.items() if k != "prev"}
    return {**anchors, key: anchor}


def undo_done(anchors, key):
    previous = (anchors.get(key) or {}).get("prev")
    return {**anchors, key: previous} if isinstance(previous, dict) else anchors


def visit_ledger(records):
    """A cleaner visit is one run, even when its span overlaps another record's span."""
    cycles, counted = [], []
    for record in records or []:
        if not isinstance(record, dict):
            continue
        end = run_ledger.record_end(record)
        if end is not None:
            start = run_ledger.parse_time(record.get("start")) or end
            cycles.append(record)
            counted.append(run_ledger.Run(min(start, end), end))
    return run_ledger.Ledger(cycles, sorted(counted))


def statuses(ledgers, anchors, now, tasks=TASKS, due=()):
    """{task key: Status} for every task that can be counted: its appliance has a ledger and, for a
    tapped task, an anchor."""
    out = {}
    for task in tasks:
        led = ledgers.get(task.appliance)
        if led is None:
            continue
        seeded = False
        if task.confirmed is not None:
            when = task.confirmed(led)
            since = run_ledger.count_after_run(led.runs, when)
        else:
            anchor = anchors.get(task.key) or {}
            when, seeded = parse_iso(anchor.get("at")), bool(anchor.get("seed"))
            if when is None:
                continue
            since = run_ledger.count_since(led.runs, when)
        left = None if task.interval is None else max(0, task.interval - since)
        if task.key in due:
            left = 0
        pace = run_ledger.per_week(led.runs, now, DEFAULT_PER_WEEK[task.appliance])
        out[task.key] = Status(task, when, seeded, since, left, pace)
    return out


def estimate_day(left, per_week, now):
    """The house day `left` more runs are done at `per_week` runs a week, None beyond ROW_HORIZON_DAYS."""
    days = left * 7 / per_week
    return house_date(now + timedelta(days=days)) if days <= ROW_HORIZON_DAYS else None


def calendar_text(weeks):
    """The interval in calendar terms, for information only."""
    if weeks < 4:
        count, unit = max(1, round(weeks)), "week"
    else:
        count, unit = max(1, round(weeks / WEEKS_PER_MONTH)), "month"
    return f"~{count} {unit}{'' if count == 1 else 's'}"


def _plural(count, noun):
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _last_line(status, now):
    task, when = status.task, status.when
    if when is None:
        return "None logged yet"
    if status.seeded:
        return f"Counting from {ddmm(when, now)}"
    verb = "Logged" if task.interval is None else "Done"
    if status.since == 0:
        return f"{verb} {ddmm(when, now)}, no {task.unit}s since"
    return f"{verb} {ddmm(when, now)}, {_plural(status.since, task.unit)} ago"


def _info(status, now):
    task = status.task
    if task.interval is None:
        first = "No interval yet. Log each clean to learn it."
    else:
        first = f"Every {task.interval} {task.unit}s ({calendar_text(task.interval / status.per_week)})"
    return [first, _last_line(status, now), task.closes]


def _log_detail(status, now):
    since = f"{_plural(status.since, status.task.unit)} since cleaned"
    last = f"counting from {ddmm(status.when, now)}" if status.seeded else f"last {ddmm(status.when, now)}"
    return fit_parts([since, last])


def _row(status, now, area, start):
    task, when = status.task, status.when
    tapped = task.confirmed is None
    at, key, at_now = None, None, False
    info = _info(status, now)
    if status.left is None:
        detail = _log_detail(status, now)
    elif status.left:
        day = estimate_day(status.left, status.per_week, now)
        if day:
            at = datetime.combine(day, DUE_TIME, HOUSE_TZ)
        detail = fit_parts([task.how, f"{_plural(status.left, task.unit)} left", f"~{ddmm(day, now)}" if day else None])
    else:
        start = Start(*start) if start else Start(None, "", False)
        at, at_now = start.at, start.at_now
        lead = f"{task.how} at {at.astimezone(HOUSE_TZ):%H:%M}" if start.timer else task.how
        last = None if tapped else f"last {ddmm(when, now)}" if when else "last unknown"
        detail = fit_parts([lead, start.timer, start.price, "due", last])
        key = f"{task.key}:{utc_iso(when)}" if when else None
        info.append(start.basis)
    guide = GUIDES[task.key]
    row = make_row(row_id(task), "maint", task.title, detail, at=at, icon=task.icon, area=area, audience="admin",
                   skip_next=at_now or task.interval is None, tap_done=tapped, info=info,
                   steps=guide.steps, warn=guide.warn)
    return row, key


def rows(found, now, areas, starts=None):
    """[(row, push key)] for the tasks in `found` (statuses), one row each. `starts` maps a task key to
    the upcoming_drum start for the tasks timed at their cheapest start once due: a Start, or the drum
    clean's (when, what it saves, whether that is now)."""
    starts = starts or {}
    return [_row(status, now, areas.get(status.task.appliance), starts.get(status.task.key))
            for status in found.values()]
