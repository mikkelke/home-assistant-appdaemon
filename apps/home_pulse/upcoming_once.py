"""One-off tasks for sensor.house_upcoming: a job with a day on it that is done once, such as checking a
plant for pests. Pure: each step takes the persisted anchors and `now` and returns new state.

A task (the `once` list in house_upcoming.yaml) is a tapped task like the plants watered by the calendar
(upcoming_plants, whose anchors, Done and undo it uses). Its row appears LEAD_DAYS house days before its
`due` day, timed at DUE_TIME on that day; from the day itself the row is untimed and has a push key. The
anchor is a seed while the task is open and a Done replaces it, which takes the row away for good; undo
brings the seed back, and the row with it.
"""

from datetime import datetime

from upcoming_core import clean_text, make_row, utc_iso
from upcoming_maint import DUE_TIME, HOUSE_TZ, ddmm, house_date, parse_iso
from upcoming_plants import LEAD_DAYS, slug

ID_PREFIX = "maint:once:"
ANCHOR_PREFIX = "once:"
ICON = "mdi:check-circle-outline"


def parse_once(raw):
    """The `once` list of house_upcoming.yaml, validated, as {slug of the title: task}. Raises ValueError
    on a task without a title with letters or digits, with a `due` that is not a date, or whose title
    is listed twice, so a typo fails at start instead of silently dropping a task."""
    tasks = {}
    for cfg in raw or []:
        cfg = cfg if isinstance(cfg, dict) else {}
        title, due = clean_text(str(cfg.get("title") or "")), cfg.get("due")
        key = slug(title)
        if not key:
            raise ValueError(f"one-off task {cfg!r}: needs a title with letters or digits")
        try:
            day = datetime.strptime(str(due), "%Y-%m-%d").date()
        except ValueError:
            raise ValueError(f"one-off task {title}: due must be a date like 2026-11-17, not {due!r}") from None
        if key in tasks:
            raise ValueError(f"one-off task {title}: listed twice")
        tasks[key] = {"slug": key, "title": title, "detail": clean_text(str(cfg.get("detail") or "")),
                      "area": cfg.get("area") or None, "icon": cfg.get("icon") or ICON, "due": day}
    return tasks


def anchor_key(task):
    return ANCHOR_PREFIX + task["slug"]


def by_row(row, tasks):
    """The task whose row id is `row`, None for any other id."""
    return next((task for task in tasks.values() if ID_PREFIX + task["slug"] == row), None)


def seed(anchors, tasks, now):
    """`anchors` plus a seed at `now` for every task that has none."""
    out = dict(anchors)
    for task in tasks.values():
        out.setdefault(anchor_key(task), {"at": utc_iso(now), "seed": True})
    return out


def is_done(anchor):
    return isinstance(anchor, dict) and bool(anchor.get("at")) and not anchor.get("seed")


def once_row(task, anchor, now):
    """(row, push key) for a task that is open and within LEAD_DAYS of its day, else None. Dated and
    without a key until the day; untimed, with a key for the task, from then on."""
    when = parse_iso((anchor or {}).get("at"))
    if when is None or is_done(anchor):
        return None
    left = (task["due"] - house_date(now)).days
    if left > LEAD_DAYS:
        return None
    if left > 0:
        at, key = datetime.combine(task["due"], DUE_TIME, HOUSE_TZ), None
    else:
        at, key = None, f"{anchor_key(task)}:{utc_iso(when)}"
    row = make_row(ID_PREFIX + task["slug"], "maint", task["title"], task["detail"], at=at, icon=task["icon"],
                   area=task["area"], audience="admin", tap_done=True, info=[f"Due {ddmm(task['due'], now)}"])
    return row, key
