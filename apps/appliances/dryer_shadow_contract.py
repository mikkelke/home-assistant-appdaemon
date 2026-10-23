"""Read-only shadow diagnostics. Equality here never certifies production readiness.

The runtime can observe the legacy entity, but not its notifier/ledger effects.
Replays can compare explicit effect expectations using the same missing/different
vocabulary. No tolerance or debounce hides an observed attribute difference.
"""

import hashlib
import json
from itertools import zip_longest
from math import isfinite

from cycle_store import parse_utc


ACTIVE_FIELDS = (
    "detected_programme", "derived_programme_key", "programme_label",
    "cycle_start_time", "started_at_display", "programme_duration_min",
    "elapsed_minutes", "progress_pct", "estimated_remaining_min",
    "estimated_end_time", "energy_at_start", "energy_used",
    "started_by", "started_by_method",
)
COMPLETED_FIELDS = (
    "run_time_minutes", "energy_used", "derived_programme_key",
    "started_by", "started_by_method",
)
OPTIONAL_FIELDS = ("overrun_min", "keep_fresh_detected", "end_reason", "idle_min", "cycle_complete")


def validated_completed(value):
    """An optional durable presentation snapshot cannot inject arbitrary state attributes."""
    if not isinstance(value, dict):
        return {}
    types = {"run_time_minutes": "number", "energy_used": "number", "idle_min": "number",
             "derived_programme_key": str, "end_reason": str,
             "cycle_complete": bool, "keep_fresh_detected": bool}
    result = {}
    for key, expected in types.items():
        if key not in value:
            continue
        item = value[key]
        if expected == "number":
            try:
                valid = item is None or (type(item) in (int, float) and isfinite(item) and item >= 0)
            except OverflowError:
                valid = False
            if not valid:
                return {}
        elif not isinstance(item, expected):
            return {}
        result[key] = item
    return result


def validated_emptying_context(value):
    if not isinstance(value, dict) or not isinstance(value.get("source"), str):
        return {}
    if value.get("user_id") is not None and not isinstance(value["user_id"], str):
        return {}
    try:
        valid_clock = isinstance(value.get("observed_at"), str) and parse_utc(value["observed_at"]) is not None
    except (OverflowError, ValueError):
        valid_clock = False
    if not valid_clock:
        return {}
    if value.get("resolution") != "unresolved":
        return {}
    return {key: value.get(key) for key in ("source", "user_id", "observed_at", "resolution")}


def input_fingerprint(config, profiles, learned):
    """Identity of the effective boot inputs, independent of filenames and JSON order."""
    encoded = json.dumps(
        {"config": config, "profiles": profiles, "learned": learned},
        sort_keys=True, separators=(",", ":"), allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compare_attributes(shadow_state, shadow_attrs, live_state, live_attrs):
    """Immediate diagnostic of explicit presentation fields, including missing on BOTH sides.

    Exact values are deliberate: sampling skew remains visible, never silently
    accepted as parity. Compare fields only in the same lifecycle state.
    """
    if live_state in (None, "unknown", "unavailable"):
        return {"status": "unobserved", "differences": {}}
    if shadow_state != live_state:
        return {"status": "state_mismatch", "differences": {
            "state": {"kind": "different", "shadow": shadow_state, "live": live_state},
        }}
    required = ACTIVE_FIELDS if shadow_state in ("Running", "Paused") else (
        COMPLETED_FIELDS if shadow_state in ("Unemptied", "Emptied") else ()
    )
    if shadow_state == "Emptied":
        required += ("emptied_by", "emptied_by_method")
    fields = set(required) | {key for key in OPTIONAL_FIELDS if key in shadow_attrs or key in live_attrs}
    differences = {}
    for key in sorted(fields):
        in_shadow, in_live = key in shadow_attrs, key in live_attrs
        if not in_shadow or not in_live:
            kind = "missing_both" if not in_shadow and not in_live else (
                "missing_shadow" if not in_shadow else "missing_live"
            )
        elif shadow_attrs[key] != live_attrs[key]:
            kind = "different"
        else:
            continue
        differences[key] = {"kind": kind}
        if in_shadow:
            differences[key]["shadow"] = shadow_attrs[key]
        if in_live:
            differences[key]["live"] = live_attrs[key]
    return {"status": "different" if differences else "match", "differences": differences}


def compare_effect_intents(actual, expected):
    """Ordered replay comparison; None means no observation, never an empty successful trace."""
    if expected is None:
        return {"status": "unobserved", "differences": []}
    missing = object()
    differences = []
    for index, (got, want) in enumerate(zip_longest(actual, expected, fillvalue=missing)):
        if got == want:
            continue
        difference = {"index": index, "kind": "different"}
        if got is missing:
            difference["kind"] = "missing_shadow"
        else:
            difference["shadow"] = got
        if want is missing:
            difference["kind"] = "unexpected_shadow"
        else:
            difference["expected"] = want
        differences.append(difference)
    return {"status": "different" if differences else "match", "differences": differences}
