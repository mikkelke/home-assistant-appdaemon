"""The finished-run ledger the maintenance rows count from: which of a washer's or a dishwasher's
feedback records are one physical run each. Pure.

The monitors append one record to <appliance>_feedback.json for every cycle they finish, so the file
is the count that survives restarts; HA erases the state entities and a monitor clears its cycle
store on Off. Records are not quite one per run: a finish the monitor takes back (a door opened early,
the dishwasher's drying tail) leaves a record, and the run it resumes leaves another, so records whose
spans overlap are one run. A washer record of class "interrupted" is a wash cut short, and the dryer's
Finish uld a few minutes of tumbling without heat (is_real: false in dryer_programmes.yaml); neither
is a run.

A record spans [end - duration_min, end], with end its effective_end_at, else its ts. A dishwasher's
duration leaves out the drying tail, which only makes its span shorter, and two runs never overlap.
"""

from datetime import datetime, timedelta, timezone
from typing import NamedTuple

OVERLAP = timedelta(minutes=10)
NOT_RUNS = ("finish_uld",)
RATE_WINDOW = timedelta(days=90)
RATE_MIN_SPAN = timedelta(days=14)


class Run(NamedTuple):
    start: datetime
    end: datetime


class Ledger(NamedTuple):
    cycles: list
    runs: list


def parse_time(value):
    """Aware datetime from an ISO string, a naive one read as UTC; None for anything else."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def record_end(record):
    return parse_time(record.get("effective_end_at")) or parse_time(record.get("ts"))


def _span(record):
    if not isinstance(record, dict) or record.get("completion_class") == "interrupted":
        return None
    if record.get("confirmed") in NOT_RUNS:
        return None
    end = record_end(record)
    if end is None:
        return None
    minutes = record.get("duration_min")
    minutes = float(minutes) if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) else 0.0
    return Run(end - timedelta(minutes=max(0.0, minutes)), end)


def finished_runs(cycles):
    """The distinct runs of the feedback records `cycles`, oldest first."""
    spans = sorted(filter(None, (_span(record) for record in cycles or [])))
    runs = []
    for span in spans:
        if runs and span.start < runs[-1].end - OVERLAP:
            runs[-1] = Run(runs[-1].start, max(runs[-1].end, span.end))
        else:
            runs.append(span)
    return runs


def ledger(cycles):
    cycles = [record for record in cycles or [] if isinstance(record, dict)]
    return Ledger(cycles, finished_runs(cycles))


def count_since(runs, since):
    """The runs that finished after `since` (an aware datetime), all of them for None. A run still going
    at `since` counts once it finishes."""
    if since is None:
        return len(runs)
    return sum(1 for run in runs if run.end > since)


def count_after_run(runs, ended):
    """The runs after the run a person confirmed, which ended at `ended`: those that finished after it and
    did not begin more than OVERLAP before it. The confirmed run, saved again by a resumed cycle so that
    it ends later, began long before and is not one of them. All of the runs for None."""
    if ended is None:
        return len(runs)
    return sum(1 for run in runs if run.end > ended and run.start >= ended - OVERLAP)


def per_week(runs, now, default):
    """Runs a week over the last RATE_WINDOW; `default` when the ledger covers less than RATE_MIN_SPAN
    or holds no run in the window."""
    recent = [run for run in runs if run.end > now - RATE_WINDOW]
    if not recent:
        return default
    span = min(RATE_WINDOW, now - runs[0].start)
    if span < RATE_MIN_SPAN:
        return default
    return len(recent) / (span / timedelta(days=7))


def last_confirmed(cycles, programme):
    """When the newest run a person confirmed as `programme` ended, None when no run was."""
    newest = None
    for record in cycles or []:
        if not isinstance(record, dict) or record.get("confirmed") != programme:
            continue
        if not (record.get("programme_confirmed_by_human") or record.get("programme_user_confirmed")):
            continue
        end = record_end(record)
        if end is not None and (newest is None or end > newest):
            newest = end
    return newest
