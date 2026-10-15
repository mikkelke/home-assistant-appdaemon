"""When Miele's drum clean is due (docs/miele-wea035-washer.md: a Bomuld 90 run with an empty drum,
about once a month, which is INTERVAL_RUNS runs at the household's pace). Pure; the washer monitor's
confirm push and HouseUpcoming's drum row both ask here, so they always agree. The programme itself
is washer_profiles.DRUM_CLEAN.

All of it reads a run_ledger.Ledger of the washer's feedback records.
"""

import run_ledger
import washer_feedback as wfb

INTERVAL_RUNS = 15


def last_clean(ledger):
    """When the newest confirmed drum clean ended, None when none is on record."""
    return run_ledger.parse_time(wfb.last_drum_clean(ledger.cycles))


def runs_since(ledger):
    """The runs on record after the last confirmed drum clean, all of them while none is."""
    return run_ledger.count_after_run(ledger.runs, last_clean(ledger))


def due(ledger):
    return runs_since(ledger) >= INTERVAL_RUNS
