"""
cycle_store.py - Durable on-disk persistence for appliance cycle state.

Why this exists: washer_monitor.py, dishwasher_monitor.py, and dryer_monitor.py each
publish their live cycle state to an AppDaemon `set_state` entity (sensor.washer_state,
sensor.dishwasher_state, sensor.dryer_state). Home Assistant ERASES those entities on
every HA restart - an AppDaemon `set_state` value does not survive a HA core restart -
and everything the entity's attributes carried goes with it, including the cycle clock
(cycle start time, cycle number, accumulated energy, ...). A wash or dry that is
genuinely running when HA restarts silently reverts to "Off" the moment the entity is
recreated. This module is the fix: a small, stdlib-only, atomic-write JSON store that
keeps a shadow copy of that cycle state on disk, so it survives both a HA restart
(which only erases entities, not files) and an AppDaemon restart.

Each monitor owns exactly one file and MUST name it with a `_state.json` suffix (e.g.
`washer_state.json`) - `.gitignore` already excludes `*_state.json`, and
`scripts/deploy.sh` only rsyncs git-tracked files, so a runtime cycle-state file can
never accidentally get committed, and a stale committed value can never get shipped as
a "seed" on deploy.

This module has no AppDaemon dependency at all - it does not import or subclass
`hassapi.Hass` - so it can be unit-tested with plain stdlib unittest and used by the
three monitors as a plain collaborator. Callers pass in their own `self.log`-shaped
callable (`log(message, level="INFO")`); see `CycleStore.__init__`.
"""

import json
import os
from datetime import datetime, timezone
from pathlib import Path

# Bump this whenever the on-disk payload shape changes in a way older code can't read
# safely. load() rejects any file whose stamped schema doesn't match what the caller
# expects, rather than guessing at a migration.
DEFAULT_SCHEMA = 1


def _noop_log(message, level="INFO"):
    """Default logger used when the caller doesn't supply one, so CycleStore can be
    constructed and unit-tested with zero AppDaemon dependencies."""
    return None


def format_utc(dt):
    """Format a datetime as UTC ISO-8601 with a 'Z' suffix and second precision,
    e.g. '2026-08-12T10:15:30Z' - the same wire format the appliance monitors already
    use for their own state-entity timestamps.

    A tz-aware `dt` is converted to UTC. A naive `dt` is handled the way
    datetime.astimezone() always handles naive input: presumed to be in the system's
    local timezone. Returns "" for `dt is None`, and never raises for any real
    datetime input.
    """
    if dt is None:
        return ""
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def parse_utc(s):
    """Parse an ISO-8601 timestamp string to a tz-aware UTC datetime.

    Accepts both a 'Z' suffix and an explicit offset ('+02:00' / '-05:00'). A string
    with no offset at all is assumed to already be UTC, matching the appliance
    monitors' own convention for their state-entity attributes. Returns None for
    anything that isn't a non-empty string, or that doesn't parse - never raises.

    The tz-aware guarantee matters: a naive datetime slipping into a monitor's
    comparison path (e.g. `now - stored_dt`) has previously crashed with a
    naive-vs-aware TypeError. Every datetime this function hands back is safe to
    compare against `datetime.now(timezone.utc)`.
    """
    if not s or not isinstance(s, str):
        return None
    try:
        text = s.strip().replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except (ValueError, TypeError):
        return None


class CycleStore:
    """Owns exactly one on-disk JSON file holding one appliance's in-progress cycle
    state, so that state can be restored after HA erases the set_state entity it was
    living in, or after AppDaemon itself restarts.

    `path` is the full path to that file (str or pathlib.Path both accepted - the
    three monitors already build their state-file paths as
    `Path(__file__).with_name(...)`, matching lock_health.py / intercom.py /
    house_events.py). `appliance` is a short identifying string (e.g. "washer"),
    stamped into every save() and checked on every load(), so a misconfigured path
    can never make load() silently hand back another appliance's data. `log` is an
    optional AppDaemon-shaped callable, `log(message, level="INFO")`; when omitted,
    all logging is a no-op.
    """

    def __init__(self, path, appliance, log=None, schema=DEFAULT_SCHEMA):
        self._path = Path(path)
        self._appliance = appliance
        self._log = log if callable(log) else _noop_log
        self._schema = schema

    def _safe_log(self, message, level="WARNING"):
        # A broken logging callable must never turn a store failure into a crash.
        try:
            self._log(message, level=level)
        except Exception:
            pass

    def save(self, payload):
        """Atomically persist `payload` for this appliance.

        Writes to `<path>.tmp`, flushes, fsyncs, then os.replace()s onto the real
        path, so a crash or power loss mid-write can never leave a truncated file
        for the next load() to choke on - the previous good file, if any, is left
        completely untouched until the final, atomic rename. fsync (not just
        flush) matters here specifically because this store exists to survive
        unexpected termination, unlike the plain tmp+replace state files elsewhere
        in this repo that only need to survive a clean process exit.

        The envelope keys `schema`, `appliance`, and `saved_at` are stamped by this
        method and always win over any same-named keys in `payload` - a caller
        cannot spoof them. `saved_at` is always "now", in UTC.

        Returns True on success, False on any failure. Never raises; a leftover
        `.tmp` from a failed attempt is cleaned up before returning.
        """
        tmp_path = self._path.with_name(self._path.name + ".tmp")
        try:
            envelope = dict(payload) if payload else {}
            envelope["schema"] = self._schema
            envelope["appliance"] = self._appliance
            envelope["saved_at"] = format_utc(datetime.now(timezone.utc))
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(envelope, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._path)
            return True
        except Exception as e:
            self._safe_log(f"CycleStore save failed for {self._appliance}: {e}")
            try:
                if tmp_path.exists():
                    tmp_path.unlink()
            except Exception:
                pass
            return False

    def load(self):
        """Load and validate this appliance's on-disk payload.

        Returns the stored dict on success. Returns None, never raises, for every
        one of: missing file, unreadable file, invalid or truncated JSON, a
        non-dict top level, a missing/mismatched `schema`, or a mismatched
        `appliance`. Each of those failure paths logs exactly one WARNING, except
        a plain missing file - the normal state on first boot after adding this
        store, and after every clear() - which logs nothing.
        """
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                raw = f.read()
        except FileNotFoundError:
            return None
        except Exception as e:
            self._safe_log(f"CycleStore load failed for {self._appliance}: {e}")
            return None

        try:
            data = json.loads(raw)
        except Exception as e:
            self._safe_log(f"CycleStore load failed for {self._appliance}: invalid JSON - {e}")
            return None

        if not isinstance(data, dict):
            self._safe_log(
                f"CycleStore load failed for {self._appliance}: top-level JSON is not an object"
            )
            return None

        if data.get("schema") != self._schema:
            # ascii(), not repr()/!r: these values came straight off disk and, on a
            # corrupted file, could contain non-ASCII content repr() would pass
            # through unescaped - the house rule is every string reaching a log.
            self._safe_log(
                f"CycleStore load failed for {self._appliance}: schema mismatch "
                f"(file={ascii(data.get('schema'))}, expected={ascii(self._schema)})"
            )
            return None

        if data.get("appliance") != self._appliance:
            self._safe_log(
                f"CycleStore load failed for {self._appliance}: appliance mismatch "
                f"(file={ascii(data.get('appliance'))})"
            )
            return None

        return data

    def clear(self):
        """Remove this store's on-disk file (cycle finished normally, or state was
        explicitly reset). Returns True once the file is gone, including when it
        was already absent - that's the ordinary steady state between cycles, not
        an error. Returns False, after logging one WARNING, only if a file existed
        and removing it failed. Never raises.
        """
        try:
            os.remove(self._path)
            return True
        except FileNotFoundError:
            return True
        except Exception as e:
            self._safe_log(f"CycleStore clear failed for {self._appliance}: {e}")
            return False
