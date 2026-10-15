"""Safe writes and reads of the monitors' <appliance>_feedback.json, the run ledger HouseUpcoming and
RunProfiles read. Stdlib only.

write_json replaces the file in one step, through a tmp file in the same directory, so a crash or a
reader never meets half of one. load_for_append is how a monitor reads the history before adding a
record: a file that exists but cannot be read as a history is set aside as <name>.corrupt-<UTC
timestamp> first, so the empty history the next record starts is not a silent loss.
"""

import json
import os
from datetime import datetime, timezone


def new_history():
    return {"version": 2, "cycles": []}


def write_json(path, data):
    """Writes `data` as the whole content of `path`. Raises on any failure, and the file is then as it
    was: the old one or the new one, never half of either."""
    tmp = f"{path}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def set_aside(path, now=None):
    """Renames the unusable file `path` to <path>.corrupt-<UTC timestamp> and returns the new name. Never
    takes the name of a file already set aside."""
    stamp = (now or datetime.now(timezone.utc)).strftime("%Y%m%dT%H%M%SZ")
    target, count = f"{path}.corrupt-{stamp}", 0
    while os.path.exists(target):
        count += 1
        target = f"{path}.corrupt-{stamp}-{count}"
    os.rename(path, target)
    return target


def load_for_append(path, log=None):
    """The history in the feedback file `path`, to add a record to: a new empty one when there is no file.
    A file that exists but is no history is set aside first and logged, and the history starts empty.
    None when it cannot be set aside either: the caller must not write then, or the history it cannot
    read would be overwritten."""
    log = log or (lambda message, level="INFO": None)
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return new_history()
    except Exception as e:
        reason = repr(e)
    else:
        if isinstance(data, dict) and isinstance(data.setdefault("cycles", []), list):
            return data
        reason = "not a feedback history"
    try:
        kept = set_aside(path)
    except OSError as e:
        log(f"Feedback file {path} cannot be read ({reason}) or set aside ({e!r}); not saving the new record",
            level="WARNING")
        return None
    log(f"Feedback file {path} could not be read ({reason}); kept as {kept}, starting a new history",
        level="WARNING")
    return new_history()
