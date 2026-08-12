"""
HousemateSleepMode - server-side per-person sleep booleans for the housemates
(Kristine, Claudia), derived only from signals HA can see itself: person.*, the
companion app's battery_state sensor, and the room PIR. Nothing here depends on
automations running ON the housemates' phones - Mikkel cannot install or maintain
those.

Why this app exists (both confirmed 2026-08-12):

- Kristine already had input_boolean.kristine_sleep_mode plus an HA automation
  ("Kristine sleep mode control", id 1743352283897), yet her sleep mode read OFF
  through the whole night of 2026-08-07 while her phone charged from 23:30. That
  automation never reads her battery sensor: it is driven entirely by
  input_boolean.kristines_iphone_charging, which only her iPhone's own iOS Shortcuts
  write (logbook context_user_id = her mobile user). That night the phone posted
  turn_on and turn_off 19 ms apart (2026-08-06T21:30:26Z), leaving the helper off
  while sensor.kristine_iphone_2_battery_state read Charging until 04:50Z. An
  edge-triggered phone-side push with no reconciliation loses the whole night to one
  dropped/duplicated event - so this app reads the battery sensor directly and needs
  no phone-side cooperation. The HA automation must be DISABLED at deploy time;
  until then last-writer-wins is accepted (see _on_sleep_boolean, which adopts
  foreign writes instead of fighting them).
- Claudia had no sleep entity at all, so she never counted as sleeping anywhere.
  Her input_boolean.claudia_sleep_mode is created by this app at startup
  (create-if-missing) via the HA WebSocket API - the REST config API has no
  input_boolean route (POST /api/config/input_boolean/config/x returns 404,
  verified 2026-08-12), so WS input_boolean/create is the only server-side way.

Signals (per person, config-driven):
  ON  when: person state == "home"  AND  battery_state in (Charging, Full)  AND
      inside the night window (default 21:00-10:00 local)  AND  the room PIR has
      been quiet for >= quiet_minutes (default 10). "Active then quiet" needs no
      special case - the quiet clock simply restarts whenever the PIR shows motion,
      so a person who came home, moved around and settled satisfies it naturally.
  OFF when (deliberately simple and conservative - nothing else clears it):
      battery_state leaves Charging/Full for >= unplug_minutes (default 5), OR
      person leaves home for >= leave_grace_minutes (default 10 - person.kristine
      flaps at the home-zone boundary, so a short away blip must not clear her
      night), OR the night window ends (hard morning end).
      PIR blips, door openings and sensor dropouts all HOLD state: unknown/
      unavailable on the battery or tracker is never treated as "unplugged" or
      "left" (the companion sensors go stale exactly when the phone sleeps).

Robustness choices, stated rather than implied:
- A PIR stuck unavailable counts as quiet once quiet_minutes have passed since its
  last observed motion: home + charging + night window still carry the signal, and
  a dead PIR must not permanently disable someone's sleep mode.
- Restart survival needs no state file: the current boolean state IS the verdict at
  init (adopted as-is), and the away/unplug clocks are re-seeded from HA's own
  last_changed timestamps, so an AppDaemon restart mid-night neither clears nor
  re-arms anything spuriously.
- Foreign writes to the boolean (the legacy HA automation until it is disabled, or
  a human in the UI): adopted as the new verdict, never reverted. A foreign OFF
  additionally arms a re-arm block so this app does not switch it back on a minute
  later while the ON conditions still hold; the block releases once any ON
  condition breaks (unplugged / left home / window end), i.e. at the next natural
  boundary.

Durations use naive local datetimes from self.datetime() (repo idiom, e.g.
bedroom_blind_control.py); the only DST-fragile stretch (02:00-03:00) sits inside
the night window where no sustain longer than unplug_minutes matters - worst case a
clock runs one hour long/short once a year in a direction that merely delays a
clear.
"""

import collections
from datetime import datetime, time as dtime

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


# States in which an entity tells us nothing - never evidence FOR or AGAINST.
_UNOBSERVABLE = frozenset({None, "unknown", "unavailable", "none", ""})


def _norm(state):
    """Lower-cased, stripped state string ('' for None) - the companion battery
    sensor capitalizes ("Charging"/"Not Charging"/"Full") and this app must not
    care."""
    if state is None:
        return ""
    return str(state).strip().lower()


def _in_window(t, start, end):
    """True when time-of-day t lies in [start, end), handling windows that wrap
    midnight (21:00-10:00)."""
    if start <= end:
        return start <= t < end
    return t >= start or t < end


def _parse_hhmm(value, fallback):
    """'21:00' or '21:00:00' -> datetime.time. Falls back rather than raising so a
    yaml typo degrades to the documented default instead of a dead app."""
    try:
        parts = [int(p) for p in str(value).split(":")]
        return dtime(parts[0], parts[1] if len(parts) > 1 else 0)
    except (ValueError, IndexError, TypeError):
        return fallback


def _parse_iso_local(s):
    """HA last_changed (UTC ISO with offset) -> naive LOCAL datetime, for seeding
    clocks across restarts. None on anything unparsable."""
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is not None:
            dt = dt.astimezone().replace(tzinfo=None)
        return dt
    except (ValueError, TypeError):
        return None


class HousemateSleepMode(hass.Hass):
    # Class-level defaults so a bare __new__() test instance is well-defined before
    # initialize() - same pattern as ActorAttribution in this directory.
    night_window = (dtime(21, 0), dtime(10, 0))
    quiet_seconds = 10 * 60
    unplug_seconds = 5 * 60
    on_battery_states = frozenset({"charging", "full"})
    helper_retry_delays = (30, 60, 120, 300)
    appdaemon_config_path = "/conf/appdaemon.yaml"

    def initialize(self):
        a = self.args.get
        self.night_window = (
            _parse_hhmm(a("night_window_start", "21:00"), dtime(21, 0)),
            _parse_hhmm(a("night_window_end", "10:00"), dtime(10, 0)),
        )
        self.quiet_seconds = int(a("quiet_minutes", 10)) * 60
        self.unplug_seconds = int(a("unplug_minutes", 5)) * 60
        default_grace = int(a("leave_grace_minutes", 10)) * 60
        self.on_battery_states = frozenset(
            _norm(s) for s in a("on_battery_states", ["charging", "full"])
        )
        self.helper_retry_delays = list(a("helper_retry_delays", [30, 60, 120, 300]))

        self._people = {}
        for cfg in a("people", []):
            key = cfg["name"]
            self._people[key] = {
                "key": key,
                "person_entity": cfg["person_entity"],
                "battery_entity": cfg["battery_entity"],
                "pir_entity": cfg["pir_entity"],
                "sleep_entity": cfg["sleep_entity"],
                "helper_name": cfg.get("helper_name"),
                "leave_grace_seconds": int(
                    cfg.get("leave_grace_minutes", default_grace / 60)
                ) * 60,
                # runtime state
                "verdict": False,
                "person_state": None,
                "battery_state": "",
                "pir_state": None,
                "last_motion": None,       # naive local dt of last PIR "on"
                "battery_off_since": None,  # naive local dt battery left Charging/Full
                "away_since": None,         # naive local dt person went explicitly away
                "rearm_block": False,       # set by a foreign OFF; see module docstring
                "expected_writes": collections.deque(),  # (value, local dt) we wrote
            }
            self._seed_person(self._people[key])
            self.listen_state(self._on_person, cfg["person_entity"], person=key)
            self.listen_state(self._on_battery, cfg["battery_entity"], person=key)
            self.listen_state(self._on_pir, cfg["pir_entity"], person=key)
            self.listen_state(self._on_sleep_boolean, cfg["sleep_entity"], person=key)

        # Create-if-missing for the sleep booleans (Claudia's does not exist yet).
        # Deferred so initialize() never blocks on HA being fully up.
        self._created_helper_names = set()
        self.run_in(self._ensure_helpers, 20)

        self.run_every(self._tick, "now+30", int(a("tick_seconds", 60)))

        self.log(
            f"HousemateSleepMode initialized for {sorted(self._people)}: window "
            f"{self.night_window[0].strftime('%H:%M')}-{self.night_window[1].strftime('%H:%M')}, "
            f"quiet>={self.quiet_seconds // 60}m, unplug>={self.unplug_seconds // 60}m, "
            f"leave-grace>={default_grace // 60}m",
            level="INFO",
        )

    # ------------------------------------------------------------------ seeding

    def _seed_person(self, p):
        """Adopt current reality at startup: the boolean state IS the verdict, and
        any already-broken ON condition gets its clock seeded from HA's own
        last_changed so a restart neither forgets a running clock nor restarts it
        from zero."""
        now = self._now_local()
        p["person_state"] = self._safe_state(p["person_entity"])
        p["battery_state"] = _norm(self._safe_state(p["battery_entity"]))
        p["pir_state"] = self._safe_state(p["pir_entity"])
        p["verdict"] = self._safe_state(p["sleep_entity"]) == "on"

        if p["pir_state"] == "on":
            p["last_motion"] = now
        else:
            p["last_motion"] = (
                _parse_iso_local(self._safe_state(p["pir_entity"], attribute="last_changed"))
                or now
            )
        if p["verdict"]:
            if self._explicit_away(p["person_state"]):
                p["away_since"] = (
                    _parse_iso_local(self._safe_state(p["person_entity"], attribute="last_changed"))
                    or now
                )
            if self._explicit_unplugged(p["battery_state"]):
                p["battery_off_since"] = (
                    _parse_iso_local(self._safe_state(p["battery_entity"], attribute="last_changed"))
                    or now
                )

    def _safe_state(self, entity, **kw):
        try:
            return self.get_state(entity, **kw)
        except Exception:
            return None

    # ---------------------------------------------------------------- listeners

    def _on_person(self, entity, attribute, old, new, kwargs):
        try:
            p = self._people[kwargs["person"]]
            p["person_state"] = new
            if new == "home":
                p["away_since"] = None
            elif self._explicit_away(new) and p["away_since"] is None:
                p["away_since"] = self._now_local()
            # unknown/unavailable: hold whatever clock state exists - a tracker
            # dropout is not a departure, and mid-grace it is not a return either.
            self._evaluate(p)
        except Exception as e:
            self.log(f"HousemateSleepMode person handler failed for {entity}: {e}", level="ERROR")

    def _on_battery(self, entity, attribute, old, new, kwargs):
        try:
            p = self._people[kwargs["person"]]
            p["battery_state"] = _norm(new)
            if p["battery_state"] in self.on_battery_states:
                p["battery_off_since"] = None
            elif self._explicit_unplugged(p["battery_state"]) and p["battery_off_since"] is None:
                p["battery_off_since"] = self._now_local()
            # unknown/unavailable: hold - a phone dropping off wifi overnight must
            # not count as unplugging, and mid-clock it must not reset the clock.
            self._evaluate(p)
        except Exception as e:
            self.log(f"HousemateSleepMode battery handler failed for {entity}: {e}", level="ERROR")

    def _on_pir(self, entity, attribute, old, new, kwargs):
        try:
            p = self._people[kwargs["person"]]
            p["pir_state"] = new
            if new == "on":
                p["last_motion"] = self._now_local()
            self._evaluate(p)
        except Exception as e:
            self.log(f"HousemateSleepMode pir handler failed for {entity}: {e}", level="ERROR")

    def _on_sleep_boolean(self, entity, attribute, old, new, kwargs):
        """Foreign writes (legacy Kristine automation until it is disabled at deploy
        time, or a human in the UI) are adopted, never reverted - last-writer-wins.
        A foreign OFF also arms the re-arm block so we do not flip it straight back
        on while the ON conditions still hold."""
        try:
            p = self._people[kwargs["person"]]
            self._expire_writes(p)
            if p["expected_writes"] and p["expected_writes"][0][0] == new:
                p["expected_writes"].popleft()
                return
            wanted = new == "on"
            if p["verdict"] == wanted:
                return
            p["verdict"] = wanted
            if not wanted:
                p["rearm_block"] = True
            else:
                # Adopted ON: our OFF rules govern it from here, so seed clocks for
                # any condition that is already broken.
                now = self._now_local()
                if self._explicit_away(p["person_state"]) and p["away_since"] is None:
                    p["away_since"] = now
                if self._explicit_unplugged(p["battery_state"]) and p["battery_off_since"] is None:
                    p["battery_off_since"] = now
            self.log(
                f"{p['key']}: sleep boolean set {new!r} by someone else - adopting"
                + (" (re-arm blocked until an ON condition breaks)" if not wanted else ""),
                level="INFO",
            )
        except Exception as e:
            self.log(f"HousemateSleepMode boolean handler failed for {entity}: {e}", level="ERROR")

    def _tick(self, kwargs):
        try:
            for p in self._people.values():
                self._evaluate(p)
        except Exception as e:
            self.log(f"HousemateSleepMode tick failed: {e}", level="ERROR")

    # --------------------------------------------------------------- evaluation

    def _evaluate(self, p, now=None):
        now = now or self._now_local()
        t = now.time()
        self._maybe_release_rearm(p, t)

        if not p["verdict"]:
            if (
                not p["rearm_block"]
                and _in_window(t, *self.night_window)
                and p["person_state"] == "home"
                and p["battery_state"] in self.on_battery_states
                and self._room_quiet(p, now)
            ):
                self._write(p, True, "home + charging + night window + room quiet")
            return

        if not _in_window(t, *self.night_window):
            self._write(p, False, "night window ended")
        elif (
            p["battery_off_since"] is not None
            and (now - p["battery_off_since"]).total_seconds() >= self.unplug_seconds
        ):
            self._write(p, False, f"unplugged for >={self.unplug_seconds // 60} min")
        elif (
            p["away_since"] is not None
            and (now - p["away_since"]).total_seconds() >= p["leave_grace_seconds"]
        ):
            self._write(p, False, f"left home for >={p['leave_grace_seconds'] // 60} min")

    def _room_quiet(self, p, now):
        if p["pir_state"] == "on":
            return False
        if p["last_motion"] is None:
            return False
        return (now - p["last_motion"]).total_seconds() >= self.quiet_seconds

    def _maybe_release_rearm(self, p, t):
        """A foreign OFF holds until the next natural boundary: any ON condition
        breaking (unplugged, left home, window end). PIR is deliberately not a
        release - the room goes active/quiet many times a night."""
        if not p["rearm_block"]:
            return
        if (
            p["battery_state"] not in self.on_battery_states
            or self._explicit_away(p["person_state"])
            or not _in_window(t, *self.night_window)
        ):
            p["rearm_block"] = False
            self.log(f"{p['key']}: re-arm block released", level="INFO")

    def _write(self, p, value, reason):
        p["verdict"] = value
        p["expected_writes"].append(("on" if value else "off", self._now_local()))
        self.call_service(
            "input_boolean/turn_on" if value else "input_boolean/turn_off",
            entity_id=p["sleep_entity"],
        )
        self.log(
            f"{p['key']}: sleep mode {'ON' if value else 'OFF'} - {reason} "
            f"(person={p['person_state']!r}, battery={p['battery_state']!r}, "
            f"pir={p['pir_state']!r})",
            level="INFO",
        )

    def _expire_writes(self, p):
        """Drop expectation entries older than 15 s - a service call that never
        produced a state change must not swallow a later, genuinely foreign one."""
        now = self._now_local()
        while p["expected_writes"] and (now - p["expected_writes"][0][1]).total_seconds() > 15:
            p["expected_writes"].popleft()

    @staticmethod
    def _explicit_away(state):
        """not_home or a named zone - never unknown/unavailable."""
        return _norm(state) not in _UNOBSERVABLE and state != "home"

    def _explicit_unplugged(self, normed):
        return normed not in _UNOBSERVABLE and normed not in self.on_battery_states

    def _now_local(self):
        """Naive local datetime (repo idiom - see bedroom_blind_control.py).
        Falls back to datetime.now() on a bare test instance."""
        try:
            return self.datetime()
        except Exception:
            return datetime.now()

    # ------------------------------------------------- helper creation (setup)

    def _ensure_helpers(self, kwargs):
        """Create-if-missing for each person's sleep boolean, then verify. HA has no
        REST config route for input_boolean (POST /api/config/input_boolean/config/x
        -> 404, verified 2026-08-12), so creation goes through the WebSocket API's
        input_boolean/create, whose object id is the slugified name -
        "Claudia sleep mode" -> input_boolean.claudia_sleep_mode. Never creates the
        same name twice (a second create would mint claudia_sleep_mode_2), so a
        verification that keeps failing logs an ERROR for a human instead of
        spawning duplicates."""
        attempt = int((kwargs or {}).get("attempt", 0))
        pending = False
        for p in self._people.values():
            if self._safe_state(p["sleep_entity"]) is not None:
                continue  # exists (any state incl. unavailable) - nothing to do
            if not p["helper_name"]:
                self.log(
                    f"{p['key']}: {p['sleep_entity']} does not exist and no "
                    f"helper_name is configured - cannot create it",
                    level="ERROR",
                )
                continue
            pending = True
            if p["helper_name"] in self._created_helper_names:
                continue  # created earlier; waiting for it to appear in AD's view
            try:
                result = self._create_helper_ws(p["helper_name"], "mdi:sleep")
                self._created_helper_names.add(p["helper_name"])
                created = f"input_boolean.{result.get('id')}"
                if created != p["sleep_entity"]:
                    self.log(
                        f"{p['key']}: created {created} but expected "
                        f"{p['sleep_entity']} - fix helper_name/sleep_entity in yaml "
                        f"(nothing deleted automatically)",
                        level="ERROR",
                    )
                else:
                    self.log(f"{p['key']}: created {created}", level="INFO")
            except Exception as e:
                self.log(
                    f"{p['key']}: creating {p['sleep_entity']} failed: {e}",
                    level="WARNING",
                )
        if not pending:
            if attempt:
                self.log("all sleep booleans exist - helper setup verified", level="INFO")
            return
        delays = self.helper_retry_delays
        if attempt < len(delays):
            self.run_in(self._ensure_helpers, delays[attempt], attempt=attempt + 1)
        else:
            self.log(
                "gave up creating/verifying missing sleep booleans after "
                f"{len(delays)} retries - the app keeps running for the ones that exist",
                level="ERROR",
            )

    def _create_helper_ws(self, name, icon):
        """input_boolean/create over the HA WebSocket API. Runs inside a worker-
        thread callback (run_in), so asyncio.run() is safe - no event loop exists in
        this thread. aiohttp ships with AppDaemon itself."""
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
        /conf/appdaemon.yaml (this app runs in the same container; same extraction
        the deploy tooling uses). The token is returned, never logged."""
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
