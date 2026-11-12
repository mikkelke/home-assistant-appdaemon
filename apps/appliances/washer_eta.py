"""Expected length of an unconfirmed washer run, from the household's own washes. Pure.

The history is every real wash on record: not interrupted, 30 to 240 minutes, at least 0.1 kWh and
no soak. Each is its duration and, when RunProfiles holds an energy profile that validates against
its record (run_profile_model.entry_rejection), its marks: the cumulative kWh at every MARK_MIN from
the cycle start, rescaled to the kWh the record metered. A run has marks of its own, noted as it goes.

A run that has lasted `elapsed` minutes is expected to last as long as the median of the NEIGHBOURS
washes that lasted longer and had used the energy it has. A wash's distance from the run is its
largest gap over the run's marks so far and its reading now. Before the first mark, or while fewer
than MIN_SAMPLES longer washes have marks, the median is over all the longer washes; with fewer than
MIN_SAMPLES longer washes there is no estimate.
"""

import statistics

import run_profile_model as model

MARK_MIN = 5
NEIGHBOURS = 5
MIN_SAMPLES = 3
MIN_WASH_MIN = 30
MAX_WASH_MIN = 240
DWH_PER_KWH = 10000.0


def profile_marks(entry, duration_min, energy_kwh):
    """Marks of a wash from its extracted energy profile, which counts minutes from the power onset
    in dWh (0.1 Wh) and is rescaled to the `energy_kwh` the record metered. The profile's `onset` and
    `cst` (the cycle start) are epoch seconds. One mark per whole MARK_MIN of `duration_min`."""
    dwh = entry["dwh"]
    cumulative = [0.0]
    for v in dwh:
        cumulative.append(cumulative[-1] + v / DWH_PER_KWH)
    scale = energy_kwh / cumulative[-1]
    onset_after_start = (entry["onset"] - entry["cst"]) / 60.0
    marks = []
    for i in range(int(duration_min // MARK_MIN)):
        t = MARK_MIN * (i + 1) - onset_after_start
        if t <= 0:
            used = 0.0
        elif t >= len(dwh):
            used = cumulative[-1]
        else:
            k = int(t)
            used = cumulative[k] + (t - k) * (cumulative[k + 1] - cumulative[k])
        marks.append(round(used * scale, 3))
    return marks


def eta_history(cycles, entries):
    """[(duration_min, marks or None)] of the real washes among the feedback `cycles`; `entries` is
    RunProfiles' extraction cache, keyed by model.cache_key. A wash without a valid entry has no marks."""
    history = []
    for rec in cycles:
        if model.record_eligibility(rec) is not None:
            continue
        duration, energy = rec["duration_min"], rec["energy_kwh"]
        if not MIN_WASH_MIN <= duration <= MAX_WASH_MIN:
            continue
        entry = entries.get(model.cache_key("washer", rec))
        marks = None
        try:
            if model.entry_rejection(rec, entry) is None:
                marks = profile_marks(entry, duration, energy)
        except (KeyError, TypeError, ValueError):
            pass
        history.append((float(duration), marks))
    return history


def kwh_at(marks, minute):
    """kWh used by `minute` into a wash, interpolated between its marks; the last mark past the last."""
    if minute <= 0 or not marks:
        return 0.0
    x = minute / MARK_MIN
    lo = int(x)
    if lo >= len(marks):
        return marks[-1]
    before = marks[lo - 1] if lo >= 1 else 0.0
    return before + (x - lo) * (marks[lo] - before)


def note_marks(marks, prev, elapsed_min, kwh):
    """`marks` extended to every MARK_MIN boundary a run has now reached. `prev` is the (elapsed_min,
    kWh) of the tick before, or None: a boundary between the two readings is interpolated, any other
    one is None (missed)."""
    marks = list(marks)
    while len(marks) < elapsed_min // MARK_MIN:
        at = MARK_MIN * (len(marks) + 1)
        if prev is not None and prev[0] < at:
            marks.append(round(prev[1] + (kwh - prev[1]) * (at - prev[0]) / (elapsed_min - prev[0]), 3))
        else:
            marks.append(None)
    return marks


def typical_total_after(history, live_marks, live_kwh, elapsed_min):
    """Expected total minutes of a run that has lasted `elapsed_min` and used `live_kwh`, with its own
    marks `live_marks` (None where missed); None while too few washes lasted longer."""
    longer = [(d, m) for d, m in history if d > elapsed_min]
    if len(longer) < MIN_SAMPLES:
        return None
    if elapsed_min >= MARK_MIN:
        scored = []
        for d, m in longer:
            if m:
                gaps = [abs(m[i] - v) for i, v in enumerate(live_marks) if v is not None and i < len(m)]
                gaps.append(abs(kwh_at(m, elapsed_min) - live_kwh))
                scored.append((max(gaps), d))
        if len(scored) >= MIN_SAMPLES:
            scored.sort()
            return round(statistics.median(d for _, d in scored[:NEIGHBOURS]))
    return round(statistics.median(d for d, _ in longer))
