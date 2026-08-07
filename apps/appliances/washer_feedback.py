"""
Feedback-store shaping for the washer monitor.

washer_feedback.json is the learned-duration memory: one record per completed
cycle, and the rolling per-programme averages and power-signature centroids
derived from the learnable subset. Everything in this module is pure - record
building, the duplicate-save guard, the aggregation that turns records into
learned durations and centroids, incremental add/remove of a single sample, the
idempotent record migration, and the confirm-push action codec.

washer_monitor.py keeps all the file I/O, the logging, the push dispatch, and
the `self` state these functions read and write.

Sibling-import module (AppDaemon puts app dirs on sys.path), matching the
existing `import climate_model as cm` precedent in apps/climate/.

Note on JSON nulls: a plain null for an unknown actor is fine in this file. The
AppDaemon 4.5.13 None-dropping bug only applies to HA entity attributes.
"""

import washer_classify as wcls
import washer_profiles as wp

# Bumped when the profile tables or the validation rules change meaning, so
# migrate_records knows a record predates the current semantics.
PROFILE_VERSION = "1"
VALIDATION_VERSION = "2"

CONFIRM_ACTION_PREFIX = "WASHER_CONFIRM|"

# Two cycle-end paths can fire for the same physical cycle seconds apart. A real
# next cycle has to run min_cycle_minutes first, so anything inside this window
# with the same programme and near-identical duration is the same cycle.
DUPLICATE_WINDOW_SECONDS = 180
DUPLICATE_DURATION_SLACK_MIN = 0.6


# =========================================================================
# Record building and the duplicate-save guard
# =========================================================================

def build_cycle_record(
    ts: str,
    predicted: str,
    predicted_temperature,
    confirmed: str,
    confirmed_temperature,
    duration_min: float,
    energy_kwh: float,
    heating_bursts: int,
    max_power_w: float,
    spin_rpm=None,
    user_confirmed: bool = False,
    spin_user_confirmed: bool = False,
    duration_source=None,
    end_reason=None,
    idle_min=None,
    confirmed_by=None,
    effective_end_at=None,
    detected_at=None,
    completion_class=None,
    valid_for_learning=None,
    validation_flags=None,
    transition_path=None,
    programme_key_used_for_validation=None,
    profile_version=None,
    validation_version=None,
    selected_options=None,
    cost_kr=None,
    vibration=None,
    actor_start=None,
    actor_empty=None,
    emptied_ts=None,
):
    """Build one v2 feedback record. Optional fields are omitted rather than
    written as null, except started_by/attribution.start which are always present.

    `ts` and `emptied_ts` are pre-formatted local ISO strings from the caller, so
    this stays free of any clock or timezone dependency.
    """
    record = {
        "ts": ts,
        "duration_min": round(duration_min, 1),
        "energy_kwh": round(energy_kwh, 3),
        "heating_bursts": heating_bursts,
        "max_power_w": round(max_power_w, 0),
        "predicted": predicted,
        "predicted_temperature": wcls.temp_for_storage(predicted_temperature),
        "confirmed": confirmed,
        "confirmed_temperature": wcls.temp_for_storage(confirmed_temperature),
        "programme_user_confirmed": user_confirmed,
        "spin_user_confirmed": spin_user_confirmed,
    }
    if confirmed_by:
        record["confirmed_by"] = confirmed_by
    if spin_rpm is not None:
        record["spin_rpm"] = spin_rpm
    if duration_source:
        record["duration_source"] = duration_source
    if end_reason:
        record["end_reason"] = end_reason
    if idle_min is not None and idle_min >= 0:
        record["idle_min"] = round(idle_min, 1)
    if effective_end_at:
        record["effective_end_at"] = effective_end_at
    if detected_at:
        record["detected_at"] = detected_at
    if completion_class:
        record["completion_class"] = completion_class
    if valid_for_learning is not None:
        record["valid_for_learning"] = valid_for_learning
    if validation_flags is not None:
        record["validation_flags"] = list(validation_flags)
    if transition_path:
        record["transition_path"] = transition_path
    if programme_key_used_for_validation:
        record["programme_key_used_for_validation"] = programme_key_used_for_validation
    if profile_version:
        record["profile_version"] = profile_version
    if validation_version:
        record["validation_version"] = validation_version
    if selected_options is not None and selected_options:
        record["selected_options"] = dict(selected_options)
    if cost_kr is not None:
        record["cost_kr"] = round(cost_kr, 2)
    if vibration is not None:
        record["vibration"] = vibration
    # started_by/attribution.start: written unconditionally (plain null when unknown is
    # correct JSON, unlike the entity-attribute None-dropping bug noted above).
    record["started_by"] = (actor_start or {}).get("person")
    record["attribution"] = {"start": actor_start}
    if actor_empty is not None:
        record["emptied_by"] = (actor_empty or {}).get("person")
        record["emptied_ts"] = emptied_ts
        record["attribution"]["empty"] = actor_empty
    return record


def is_duplicate_cycle(last: dict, record: dict, gap_s: float) -> bool:
    """True when `record` is the same physical cycle as `last`, saved again by a
    second cycle-end path. Wall-clock durations grow with the detection gap, so the
    tolerance is the gap plus rounding slack."""
    same_programme = (
        last.get("confirmed") == record["confirmed"]
        and last.get("confirmed_temperature") == record["confirmed_temperature"]
    )
    duration_close = abs(float(last["duration_min"]) - record["duration_min"]) <= gap_s / 60.0 + DUPLICATE_DURATION_SLACK_MIN
    return 0 <= gap_s <= DUPLICATE_WINDOW_SECONDS and same_programme and duration_close


# =========================================================================
# Turning saved records into learned durations and centroids
# =========================================================================

def aggregate_cycles(cycles, profiles: dict):
    """Fold saved cycle records into per-programme duration buckets and power-signature
    centroids.

    Only user-confirmed, valid_for_learning records count - an unconfirmed guess can
    carry a wrong programme, and a quarantined record a wrong duration.

    Returns (buckets, centroids, skipped_unconfirmed) where buckets is
    {"prog|temp": {"durations": [...], "correct": int, "total": int, "prog":, "temp":}}
    and centroids is {"prog|temp": {"rate":, "heating_bursts":, "n":}}.
    """
    buckets: dict = {}
    signatures = []      # (learn_key, rate kWh/min, heating_bursts)
    skipped_unconfirmed = 0

    for rec in cycles:
        confirmed = rec.get("confirmed", "")
        predicted = rec.get("predicted", "")
        conf_temp = wcls.temp_from_storage(rec.get("confirmed_temperature"))
        pred_temp = wcls.temp_from_storage(rec.get("predicted_temperature"))
        dur = rec.get("duration_min")
        user_confirmed = rec.get("programme_user_confirmed", rec.get("user_confirmed", False))
        valid_for_learning = rec.get("valid_for_learning", False)  # Only learn from validated cycles

        if not confirmed or not isinstance(dur, (int, float)):
            continue
        # Only use user-confirmed cycles for learning - unconfirmed (guessed) programmes
        # can be wrong (e.g. false finishes with wrong duration).
        if not user_confirmed:
            skipped_unconfirmed += 1
            continue
        # Only use records with valid_for_learning == True for learned durations and centroids.
        if not valid_for_learning:
            continue

        learn_key = wp.learn_key_for(profiles, confirmed, conf_temp)
        pred_key = wp.learn_key_for(profiles, predicted, pred_temp)

        if learn_key not in buckets:
            buckets[learn_key] = {"durations": [], "correct": 0, "total": 0, "prog": confirmed, "temp": conf_temp}
        buckets[learn_key]["durations"].append(dur)
        buckets[learn_key]["total"] += 1
        if learn_key == pred_key:
            buckets[learn_key]["correct"] += 1

        profile = wp.get_profile(profiles, confirmed, conf_temp)
        if profile.get("heats") and dur and dur > 0:
            energy_kwh = rec.get("energy_kwh")
            bursts = rec.get("heating_bursts")
            if isinstance(energy_kwh, (int, float)) and energy_kwh >= 0:
                rate = energy_kwh / dur
                signatures.append((learn_key, rate, bursts if isinstance(bursts, (int, float)) else 0))

    # Build history centroids keyed by "prog|temp"
    centroids: dict = {}
    sig_by_key: dict = {}
    for key, rate, bursts in signatures:
        sig_by_key.setdefault(key, []).append((rate, bursts))
    for key, pts in sig_by_key.items():
        n = len(pts)
        centroids[key] = {
            "rate": sum(r for r, _ in pts) / n,
            "heating_bursts": sum(b for _, b in pts) / n,
            "n": n,
        }
    return (buckets, centroids, skipped_unconfirmed)


def apply_learned_sample(learned: dict, centroids: dict, learn_key: str,
                         duration_min: float, energy_kwh: float, heating_bursts,
                         heats: bool):
    """Fold one newly-saved cycle into the in-memory learned durations and centroids.

    Mutates both dicts in place and returns the new running average duration, so the
    caller can log it. The centroid is only touched for programmes that heat, since
    the signature match keys off energy rate.
    """
    prev = learned.get(learn_key, {"n": 0, "avg": duration_min})
    n_new = prev["n"] + 1
    avg_new = (prev["avg"] * prev["n"] + duration_min) / n_new
    learned[learn_key] = {"n": n_new, "avg": avg_new}

    if heats and duration_min and duration_min > 0:
        centroid_key = learn_key
        rate_new = energy_kwh / duration_min
        if centroid_key not in centroids:
            centroids[centroid_key] = {"rate": rate_new, "heating_bursts": float(heating_bursts), "n": 1}
        else:
            old = centroids[centroid_key]
            n = old["n"] + 1
            centroids[centroid_key] = {
                "rate": (old["rate"] * old["n"] + rate_new) / n,
                "heating_bursts": (old["heating_bursts"] * old["n"] + heating_bursts) / n,
                "n": n,
            }
    return avg_new


def remove_learned_sample(learned: dict, centroids: dict, learn_key: str,
                          duration_min, energy_kwh, heating_bursts):
    """Back one cycle out of the in-memory learned durations and centroids, for when a
    false Unemptied is retracted. Mutates both dicts in place; drops the key entirely
    when the last sample goes."""
    if learn_key in learned:
        old = learned[learn_key]
        n = old["n"] - 1
        if n <= 0:
            del learned[learn_key]
        else:
            avg_new = (old["avg"] * old["n"] - duration_min) / n
            learned[learn_key] = {"n": n, "avg": avg_new}
    if learn_key in centroids and duration_min and duration_min > 0:
        old = centroids[learn_key]
        n = old["n"] - 1
        if n <= 0:
            del centroids[learn_key]
        else:
            rate_removed = energy_kwh / duration_min
            centroids[learn_key] = {
                "rate": (old["rate"] * old["n"] - rate_removed) / n,
                "heating_bursts": (old["heating_bursts"] * old["n"] - heating_bursts) / n,
                "n": n,
            }


# =========================================================================
# Idempotent migration of older records
# =========================================================================

def migrate_records(cycles, classify, profile_version: str = PROFILE_VERSION,
                    validation_version: str = VALIDATION_VERSION):
    """Add completion_class, valid_for_learning and validation_flags to records that
    predate them, in place.

    `classify` is called with the same keyword arguments as
    WasherMonitor._classify_cycle_completion. Records already carrying both fields at
    the current versions are counted as unchanged and left alone; records without a
    numeric duration are skipped entirely.

    Returns the counts dict (completed, interrupted, suspect, learnable, quarantined,
    unchanged).
    """
    counts = {"completed": 0, "interrupted": 0, "suspect": 0, "learnable": 0, "quarantined": 0, "unchanged": 0}
    for rec in cycles:
        if rec.get("completion_class") and rec.get("valid_for_learning") is not None:
            if rec.get("profile_version") == profile_version and rec.get("validation_version") == validation_version:
                counts["unchanged"] += 1
                continue
        dur = rec.get("duration_min")
        if not isinstance(dur, (int, float)):
            continue
        confirmed = rec.get("confirmed", "")
        conf_temp = wcls.temp_from_storage(rec.get("confirmed_temperature"))
        pred = rec.get("predicted", "")
        pred_temp = wcls.temp_from_storage(rec.get("predicted_temperature"))
        transition_path = rec.get("end_reason") or rec.get("transition_path") or "low_power_detected"
        if transition_path not in wcls.KNOWN_TRANSITION_PATHS:
            transition_path = "low_power_detected"
        classification = classify(
            run_minutes=float(dur),
            energy_kwh=float(rec.get("energy_kwh", 0) or 0),
            heating_bursts=int(rec.get("heating_bursts", 0) or 0),
            max_power_w=float(rec.get("max_power_w", 0) or 0),
            predicted=pred,
            predicted_temperature=pred_temp,
            confirmed=confirmed,
            confirmed_temperature=conf_temp,
            transition_path=transition_path,
            spin_rpm=rec.get("spin_rpm"),
            user_confirmed_override=rec.get("programme_user_confirmed", rec.get("user_confirmed", False)),
        )
        rec["completion_class"] = classification["completion_class"]
        rec["valid_for_learning"] = classification["valid_for_learning"]
        rec["validation_flags"] = classification["validation_flags"]
        rec["transition_path"] = classification["end_reason"]
        rec["programme_key_used_for_validation"] = classification.get("programme_key_used_for_validation", "")
        rec["profile_version"] = profile_version
        rec["validation_version"] = validation_version
        counts[classification["completion_class"]] = counts.get(classification["completion_class"], 0) + 1
        if classification["valid_for_learning"]:
            counts["learnable"] += 1
        else:
            counts["quarantined"] += 1
    return counts


# =========================================================================
# Presence-gated confirm push (feature: ask whoever is home to confirm the
# programme with one tap when a cycle ends unconfirmed but worth learning)
# =========================================================================

def should_send_confirm_push(record: dict, enabled: bool) -> bool:
    """Gate for the confirm push: only nag when enabled, the cycle actually
    completed (not an abort/suspect), and no human has confirmed it yet
    (checks the new programme_confirmed_by_human field and the legacy
    programme_user_confirmed field - either being true means don't ask)."""
    if not enabled:
        return False
    if record.get("programme_confirmed_by_human") or record.get("programme_user_confirmed"):
        return False
    if record.get("completion_class") != "completed":
        return False
    return True


def encode_confirm_action(ts: str, prog: str, temp) -> str:
    """Build the WASHER_CONFIRM|<ts>|<prog>|<temp> action id for the confirm-push
    button. temp must already be in storage format (e.g. '40', 'cold'); None -> '-'."""
    return f"{CONFIRM_ACTION_PREFIX}{ts}|{prog}|{temp if temp else '-'}"


def parse_confirm_action(action: str):
    """Parse a WASHER_CONFIRM|<ts>|<prog>|<temp> action id back to (ts, prog, temp).
    temp is storage format or None ('-' -> None). Returns None if malformed."""
    if not action or not action.startswith(CONFIRM_ACTION_PREFIX):
        return None
    parts = action.split("|")
    if len(parts) != 4:
        return None
    _, ts, prog, temp = parts
    if not ts or not prog:
        return None
    return (ts, prog, None if temp == "-" else temp)
