"""
HouseNightMode - one latched household-level "it is night" boolean,
input_boolean.house_night_mode, for the lighting stack to consume instead of
input_boolean.mikkel_sleep_mode.

The incident this fixes (2026-08-07 04:14-04:44): the lighting stack read Mikkel's
personal sleep boolean as "the household is in night mode", so a 4 a.m. trip out of
bed flipped the whole house out of night mode - the family-room app went full
"turn on all" for whatever moved, nine on/off cycles until he was back in bed at
04:44. A person getting up is not the house waking up. This app latches night ON at
the first sleeper and only clears it in the morning.

LATCH ON: the first per-person sleep boolean (Mikkel, Kristine, Claudia) turning on
inside 20:00-04:00 local - plus, at 20:00 itself, anyone already asleep counts (an
early sleeper whose ON edge predates the window still makes it night at 20:00).

CLEAR (whichever comes first):
- Mikkel's sleep mode continuously OFF for >= 15 min counted INSIDE 04:30-12:00 -
  minutes off before 04:30 do not count (a 04:26 wake-up needs to hold until 04:45,
  which is exactly why the 2026-08-07 04:14-04:44 trip, back in bed at 04:44, must
  NOT clear it), or
- the hard clear at 09:30.

MANUAL FLIPS ARE RESPECTED: any state change this app did not write itself (human
in the UI, a scene, anything) is held until the next natural boundary rather than
fought. A manual OFF suppresses re-latching until the next latch-window start
(20:00); a manual ON restarts the sustained-clear clock, so it survives at least
clear_sustain_minutes and falls at the next natural clear (sustained rule or the
09:30 hard clear). Own writes are recognized via a short-lived expectation queue,
not via context (AppDaemon callbacks do not carry HA context).

input_boolean.house_night_mode is created at startup if missing, via the HA
WebSocket API (REST /api/config has no input_boolean route - POST returns 404,
verified 2026-08-12). Creation is idempotent: existence is checked first, and a
given name is never created twice (a repeat create would mint house_night_mode_2).

All clock math uses naive local datetimes from self.datetime() (repo idiom). The
sustained-clear window 04:30-12:00 sits entirely past the 02:00-03:00 DST switch
hours, and the latch is edge-based with no duration math, so DST cannot corrupt
either rule.

Consumption note: this app only produces the signal. Pointing the lighting stack
(family-room app etc.) at house_night_mode is a separate, deliberate step.
"""

import collections
from datetime import datetime, time as dtime, timedelta

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def _in_window(t, start, end):
    """True when time-of-day t lies in [start, end), handling windows that wrap
    midnight (20:00-04:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _parse_hhmm(value, fallback):
    try:
        parts = [int(p) for p in str(value).split(":")]
        return dtime(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError, TypeError):
        return fallback


class HouseNightMode(hass.Hass):
    # Class-level defaults so bare __new__() test instances are well-defined -
    # same pattern as ActorAttribution in this directory.
    house_entity = "input_boolean.house_night_mode"
    helper_name = "House night mode"
    sleep_entities = (
        "input_boolean.mikkel_sleep_mode",
        "input_boolean.kristine_sleep_mode",
        "input_boolean.claudia_sleep_mode",
    )
    mikkel_sleep_entity = "input_boolean.mikkel_sleep_mode"
    latch_window = (dtime(20, 0), dtime(4, 0))
    clear_window = (dtime(4, 30), dtime(12, 0))
    clear_sustain_seconds = 15 * 60
    hard_clear_time = dtime(9, 30)
    helper_retry_delays = (30, 60, 120, 300)
    appdaemon_config_path = "/conf/appdaemon.yaml"
    _mikkel_off_since = None
    _manual_hold = None  # ("on"|"off", set_at local dt) - see module docstring

    def initialize(self):
        a = self.args.get
        self.house_entity = a("house_entity", "input_boolean.house_night_mode")
        self.helper_name = a("helper_name", "House night mode")
        self.sleep_entities = list(a("sleep_entities", [
            "input_boolean.mikkel_sleep_mode",
            "input_boolean.kristine_sleep_mode",
            "input_boolean.claudia_sleep_mode",
        ]))
        self.mikkel_sleep_entity = a("mikkel_sleep_entity", "input_boolean.mikkel_sleep_mode")
        self.latch_window = (
            _parse_hhmm(a("latch_window_start", "20:00"), dtime(20, 0)),
            _parse_hhmm(a("latch_window_end", "04:00"), dtime(4, 0)),
        )
        self.clear_window = (
            _parse_hhmm(a("clear_window_start", "04:30"), dtime(4, 30)),
            _parse_hhmm(a("clear_window_end", "12:00"), dtime(12, 0)),
        )
        self.clear_sustain_seconds = int(a("clear_sustain_minutes", 15)) * 60
        self.hard_clear_time = _parse_hhmm(a("hard_clear_time", "09:30"), dtime(9, 30))
        self.helper_retry_delays = list(a("helper_retry_delays", [30, 60, 120, 300]))

        self._manual_hold = None
        self._pending_writes = collections.deque()  # (value, local dt) we wrote
        # Conservative restart seed: if Mikkel is off right now, count from now -
        # at worst the clear runs up to 15 min late, and 09:30 caps it anyway.
        self._mikkel_off_since = (
            self._now_local() if self.get_state(self.mikkel_sleep_entity) == "off" else None
        )

        for entity in self.sleep_entities:
            self.listen_state(self._on_sleep_change, entity)
        if self.mikkel_sleep_entity not in self.sleep_entities:
            self.listen_state(self._on_sleep_change, self.mikkel_sleep_entity)
        self.listen_state(self._on_house_change, self.house_entity)

        self.run_daily(self._on_latch_window_start, self.latch_window[0])
        self.run_daily(self._on_hard_clear, self.hard_clear_time)
        self.run_every(self._tick, "now+30", int(a("tick_seconds", 60)))

        self._created_helper = False
        self.run_in(self._ensure_helper, 20)

        self.log(
            f"HouseNightMode initialized: latch {self.latch_window[0].strftime('%H:%M')}-"
            f"{self.latch_window[1].strftime('%H:%M')} on first of {len(self.sleep_entities)} "
            f"sleepers; clear when {self.mikkel_sleep_entity} off >="
            f"{self.clear_sustain_seconds // 60}m inside "
            f"{self.clear_window[0].strftime('%H:%M')}-{self.clear_window[1].strftime('%H:%M')}, "
            f"hard clear {self.hard_clear_time.strftime('%H:%M')}",
            level="INFO",
        )

    # ---------------------------------------------------------------- listeners

    def _on_sleep_change(self, entity, attribute, old, new, kwargs):
        try:
            now = self._now_local()
            if entity == self.mikkel_sleep_entity:
                if new == "off" and old == "on":
                    self._mikkel_off_since = now
                elif new == "on":
                    self._mikkel_off_since = None
            if new == "on" and old != "on":
                self._try_latch(now)
        except Exception as e:
            self.log(f"HouseNightMode sleep-change handler failed for {entity}: {e}", level="ERROR")

    def _on_house_change(self, entity, attribute, old, new, kwargs):
        """Classify each change as ours (expected) or foreign (manual). Foreign
        flips become a hold - see module docstring."""
        try:
            now = self._now_local()
            while self._pending_writes and (now - self._pending_writes[0][1]).total_seconds() > 15:
                self._pending_writes.popleft()
            if self._pending_writes and self._pending_writes[0][0] == new:
                self._pending_writes.popleft()
                return
            if new not in ("on", "off"):
                return
            self._manual_hold = (new, now)
            self.log(
                f"house_night_mode set {new!r} by someone else - holding their value "
                f"until the next natural {'latch' if new == 'off' else 'clear'} boundary",
                level="INFO",
            )
        except Exception as e:
            self.log(f"HouseNightMode house-change handler failed: {e}", level="ERROR")

    def _on_latch_window_start(self, kwargs):
        """20:00: an early sleeper whose ON edge predates the window still makes it
        night at the window start."""
        try:
            self._try_latch(self._now_local())
        except Exception as e:
            self.log(f"HouseNightMode latch-window-start failed: {e}", level="ERROR")

    def _on_hard_clear(self, kwargs):
        try:
            if self.get_state(self.house_entity) == "on":
                self._write_house(False, f"hard clear at {self.hard_clear_time.strftime('%H:%M')}")
                self._manual_hold = None
        except Exception as e:
            self.log(f"HouseNightMode hard clear failed: {e}", level="ERROR")

    def _tick(self, kwargs):
        try:
            self._evaluate_clear(self._now_local())
        except Exception as e:
            self.log(f"HouseNightMode tick failed: {e}", level="ERROR")

    # -------------------------------------------------------------- latch/clear

    def _try_latch(self, now):
        if self.get_state(self.house_entity) != "off":
            return  # already on, or the helper does not exist yet
        if not _in_window(now.time(), *self.latch_window):
            return
        if self._off_hold_active(now):
            return
        sleepers = [e for e in self.sleep_entities if self.get_state(e) == "on"]
        if not sleepers:
            return
        self._write_house(True, f"first sleeper inside the latch window ({sleepers[0]})")

    def _evaluate_clear(self, now):
        if self.get_state(self.house_entity) != "on":
            return
        if not _in_window(now.time(), *self.clear_window):
            return
        mik = self.get_state(self.mikkel_sleep_entity)
        if mik == "on":
            self._mikkel_off_since = None
            return
        if mik != "off":
            return  # unavailable/unknown is not evidence he is up
        if self._mikkel_off_since is None:
            # Off, but we never saw the edge (restart): count from now.
            self._mikkel_off_since = now
        effective = self._mikkel_off_since
        window_start_today = now.replace(
            hour=self.clear_window[0].hour, minute=self.clear_window[0].minute,
            second=0, microsecond=0,
        )
        if window_start_today > effective:
            effective = window_start_today  # only minutes inside 04:30-12:00 count
        hold = self._manual_hold
        if hold and hold[0] == "on" and hold[1] > effective:
            effective = hold[1]  # a manual ON gets a fresh sustain clock
        if (now - effective).total_seconds() >= self.clear_sustain_seconds:
            self._write_house(
                False,
                f"Mikkel's sleep mode off >={self.clear_sustain_seconds // 60} min "
                f"inside the clear window",
            )
            self._manual_hold = None

    def _off_hold_active(self, now):
        """A manual OFF suppresses latching until the next latch-window start
        strictly after it was set (i.e. for the rest of that night)."""
        hold = self._manual_hold
        if not hold or hold[0] != "off":
            return False
        expiry = hold[1].replace(
            hour=self.latch_window[0].hour, minute=self.latch_window[0].minute,
            second=0, microsecond=0,
        )
        if expiry <= hold[1]:
            expiry += timedelta(days=1)
        if now >= expiry:
            self._manual_hold = None
            return False
        return True

    def _write_house(self, value, reason):
        self._pending_writes.append(("on" if value else "off", self._now_local()))
        self.call_service(
            "input_boolean/turn_on" if value else "input_boolean/turn_off",
            entity_id=self.house_entity,
        )
        self.log(f"house_night_mode {'ON' if value else 'OFF'} - {reason}", level="INFO")

    def _now_local(self):
        """Naive local datetime (repo idiom - see bedroom_blind_control.py).
        Falls back to datetime.now() on a bare test instance."""
        try:
            return self.datetime()
        except Exception:
            return datetime.now()

    # ------------------------------------------------- helper creation (setup)
    # Duplicated from housemate_sleep_mode.py per this repo's convention of
    # per-app copies of small helpers rather than cross-app imports.

    def _ensure_helper(self, kwargs):
        """Create-if-missing for input_boolean.house_night_mode, then verify. See
        housemate_sleep_mode.py's _ensure_helpers for why this is WS-only and why a
        name is never created twice."""
        attempt = int((kwargs or {}).get("attempt", 0))
        try:
            exists = self.get_state(self.house_entity) is not None
        except Exception:
            exists = False
        if exists:
            if attempt or self._created_helper:
                self.log(f"{self.house_entity} exists - helper setup verified", level="INFO")
            return
        if not self._created_helper:
            try:
                result = self._create_helper_ws(self.helper_name, "mdi:weather-night")
                self._created_helper = True
                created = f"input_boolean.{result.get('id')}"
                if created != self.house_entity:
                    self.log(
                        f"created {created} but expected {self.house_entity} - fix "
                        f"helper_name/house_entity in yaml (nothing deleted automatically)",
                        level="ERROR",
                    )
                else:
                    self.log(f"created {created}", level="INFO")
            except Exception as e:
                self.log(f"creating {self.house_entity} failed: {e}", level="WARNING")
        delays = self.helper_retry_delays
        if attempt < len(delays):
            self.run_in(self._ensure_helper, delays[attempt], attempt=attempt + 1)
        else:
            self.log(
                f"gave up creating/verifying {self.house_entity} after {len(delays)} "
                f"retries - latch/clear will start working once it exists",
                level="ERROR",
            )

    def _create_helper_ws(self, name, icon):
        """input_boolean/create over the HA WebSocket API - runs in a worker-thread
        callback, so asyncio.run() is safe. aiohttp ships with AppDaemon."""
        import asyncio

        import aiohttp

        ha_url, token = self._ha_credentials()
        if not token:
            raise RuntimeError(f"no HA token found in {self.appdaemon_config_path}")
        ws_url = ha_url.rstrip("/") + "/api/websocket"

        async def _create():
            timeout = aiohttp.ClientTimeout(total=20)
            async with aiohttp.ClientSession(timeout=timeout) as session:
                async with session.ws_connect(ws_url) as ws:
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_required":
                        raise RuntimeError(f"unexpected WS hello: {msg.get('type')!r}")
                    await ws.send_json({"type": "auth", "access_token": token})
                    msg = await ws.receive_json()
                    if msg.get("type") != "auth_ok":
                        raise RuntimeError("HA WebSocket auth failed")
                    await ws.send_json(
                        {"id": 1, "type": "input_boolean/create", "name": name, "icon": icon}
                    )
                    msg = await ws.receive_json()
                    if not msg.get("success"):
                        raise RuntimeError(f"input_boolean/create failed: {msg.get('error')}")
                    return msg["result"]

        return asyncio.run(_create())

    def _ha_credentials(self):
        """ha_url + token from AppDaemon's own HASS plugin section in
        /conf/appdaemon.yaml. The token is returned, never logged."""
        ha_url = self.args.get("ha_url") if getattr(self, "args", None) else None
        token = None
        try:
            with open(self.appdaemon_config_path) as f:
                for line in f:
                    s = line.strip()
                    if token is None and s.startswith("token:"):
                        token = s.split("token:", 1)[1].strip()
                    elif ha_url is None and s.startswith("ha_url:"):
                        ha_url = s.split("ha_url:", 1)[1].strip()
        except Exception as e:
            self.log(f"could not read {self.appdaemon_config_path}: {e}", level="WARNING")
        return (ha_url or "http://localhost:8123"), token
