"""
PresenceStuckWatch - alerts when a room's presence sensor looks "stuck" on with nobody
actually there, plus a per-room presence/light on-time metric with a same-day anomaly alert.

Why: the bathroom Tuya radar+PIR (binary_sensor.bathroom_presence_presence) had its static
radar sensitivity raised 7 -> 9 (2026-09-23) so it stops dropping a person standing still at
the mirror. The flip side of that fix: at 9 it can latch ON in an empty room, and nobody
notices because they have already left - the light just stays on. This app is the safety net
for that risk, not a replacement for the sensor tuning.

Config-driven, one entry per room (see presence_stuck_watch.yaml) - only the bathroom is
configured today; more rooms can be added without a code change.

STUCK detection (evaluated on every relevant state change plus a 60s run_every tick), while
`presence` reads "on":
  1. nobody_home: every configured person entity has read something other than "home"
     continuously for >= nobody_home_min minutes.
  2. elsewhere: exactly one person is "home", some OTHER room_active zone
     (binary_sensor.<zone>_active, the zone list read live from RoomActive.zones, minus this
     room's own zone and ignore_zones) turned ON after this room's last observed motion, and
     presence has stayed on >= elsewhere_min minutes since. The person walked out and was
     seen elsewhere, but this room still claims someone.
  3. long_hold: presence continuously on >= long_hold_min minutes AND no motion for
     >= long_hold_no_motion_min minutes.
Exactly one push per stuck episode (an episode runs from presence turning on to it turning
off again) via MobileNotifier, target="user" (Mikkel only) - never a repeat nag while the
same episode stays stuck, whichever rule first tripped it.

Metrics: sensor.<room>_presence_stats, state = today's average presence-session length in
minutes. Per-day totals (sessions, on-minutes, longest session, light on-minutes, stuck
episodes) persist to presence_stuck_watch_state.json next to this file - atomic tmp+replace,
same pattern as lock_health.py/house_events.py - so a restart never wipes history. Local-
midnight rollover keeps the last 14 days; a session or light-on stretch spanning midnight is
split at the boundary rather than credited whole to one day. At 23:55 local, a day whose
on-minutes exceed max(60, 2.5x the trailing prior-day average) triggers one more push - only
once at least 3 prior days of data exist, so a freshly added room stays quiet until it has a
baseline.

Publish contract: AppDaemon 4.5.13's set_state silently drops any attribute value that is
exactly None/False/0 - see room_active.py's module docstring for the reference incident.
Every numeric attribute here is therefore published as a non-empty STRING ("0"/"0.0" survive,
a bare 0 vanishes); last_stuck_at is the literal string "never" until the first episode.
Republished on every 60s tick and on plugin_started (HA restart wipes set_state entities),
not just on change, so the entity is never stale for long after a restart.

Restart seeding: last_motion_at is derived from the motion entity's own last_changed (HA only
records changes, so a currently small/large reading needs this to know how long it has held).
presence_on_since/light_on_since instead seed conservatively from "now" if already on at
startup - the HouseNightMode convention: the true on-since time is unknowable across a
restart, and counting from now only delays a real alert (capped at long_hold_min late), never
manufactures a false one straight after a reload.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

_RETAIN_DAYS = 14


def _fresh_day():
    return {
        "sessions": 0,
        "presence_on_min": 0.0,
        "max_session_min": 0.0,
        "light_on_min": 0.0,
        "stuck_episodes": 0,
    }


def _parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


class PresenceStuckWatch(hass.Hass):
    # Class-level defaults so bare __new__() test instances are well-defined - same pattern
    # as RoomActive/HouseNightMode in this directory.
    persons: list = []
    rooms: dict = {}

    def initialize(self):
        a = self.args.get
        self.persons = list(a("persons", []))
        self.rooms = self._parse_rooms(a("rooms", {}))

        self._notifier = self.get_app("MobileNotifier")
        self._room_active_app = self.get_app("RoomActive")
        if self._room_active_app is None:
            self.log(
                "PresenceStuckWatch: RoomActive app not found - the 'elsewhere' rule is "
                "disabled for every room until it loads",
                level="WARNING",
            )

        self._state_file = Path(__file__).with_name("presence_stuck_watch_state.json")
        self._room_state = {room: self._fresh_room_state() for room in self.rooms}
        self._metrics = {room: {} for room in self.rooms}
        self._today = {}
        self._nobody_home_since = None
        self._load_metrics()

        now = self._now_local()
        self._recompute_nobody_home(now)

        for room, cfg in self.rooms.items():
            self._roll_to(room, now)
            self._seed_room(room, cfg, now)
            self.listen_state(self._on_presence_change, cfg["presence"], room=room)
            self.listen_state(self._on_motion_change, cfg["motion"], room=room)
            self.listen_state(self._on_light_change, cfg["light"], room=room)
            for zone in self._other_zones(cfg):
                self.listen_state(
                    self._on_other_zone_on, f"binary_sensor.{zone}_active", room=room, new="on"
                )

        for person in self.persons:
            self.listen_state(self._on_person_change, person)

        self.listen_event(self._on_plugin_started, "plugin_started")
        # "now+N", not "now" - run_every(cb, "now", N) does NOT fire immediately (first call
        # is now+N, see appdaemon-deploy memory); moot here either way since the loop below
        # already does an immediate first publish.
        self.run_every(self._tick, "now+60", 60)
        self.run_daily(self._on_anomaly_check, dtime(23, 55))

        self._publish_all(now)

        self.log(
            f"PresenceStuckWatch initialized: watching {len(self.rooms)} room(s): "
            f"{sorted(self.rooms)}",
            level="INFO",
        )

    # ---------- config parsing ----------

    @staticmethod
    def _parse_rooms(raw):
        rooms = {}
        for room, cfg in (raw or {}).items():
            cfg = cfg or {}
            rooms[str(room)] = {
                "presence": cfg["presence"],
                "motion": cfg["motion"],
                "light": cfg["light"],
                "zone": cfg.get("zone", str(room)),
                "ignore_zones": list(cfg.get("ignore_zones", []) or []),
                "nobody_home_min": float(cfg.get("nobody_home_min", 5)),
                "elsewhere_min": float(cfg.get("elsewhere_min", 10)),
                "long_hold_min": float(cfg.get("long_hold_min", 60)),
                "long_hold_no_motion_min": float(cfg.get("long_hold_no_motion_min", 30)),
            }
        return rooms

    @staticmethod
    def _fresh_room_state():
        return {
            "presence_on_since": None,
            "last_motion_at": None,
            "elsewhere_since": None,
            "light_on_since": None,
            "stuck": False,
            "stuck_since": None,
            "last_stuck_at": None,
        }

    def _other_zones(self, cfg):
        """room_active zone names this room should watch for the elsewhere rule - every
        RoomActive zone except this room's own and its configured ignore_zones. Empty
        (rule permanently inert) when RoomActive isn't loaded."""
        zones = set()
        if self._room_active_app is not None:
            try:
                zones = set(self._room_active_app.zones.keys())
            except Exception:
                zones = set()
        exclude = {cfg["zone"]} | set(cfg.get("ignore_zones", []))
        return sorted(zones - exclude)

    # ---------- restart seeding ----------

    def _seed_room(self, room, cfg, now):
        st = self._room_state[room]
        if self.get_state(cfg["motion"]) in ("small", "large"):
            elapsed = self._elapsed_since_last_changed(cfg["motion"], now)
            st["last_motion_at"] = (now - timedelta(seconds=elapsed)) if elapsed is not None else now
        if self.get_state(cfg["presence"]) == "on":
            # Conservative restart seed (HouseNightMode convention): the true on-since time
            # is unknowable across a restart, so count from now - at worst a stuck alert
            # runs up to long_hold_min late, the safer direction versus a false alarm right
            # after every AppDaemon reload.
            st["presence_on_since"] = now
        if self.get_state(cfg["light"]) == "on":
            st["light_on_since"] = now

    def _elapsed_since_last_changed(self, entity, now):
        """Seconds between `entity`'s last_changed and `now`, computed via epoch
        (manual_override_timeout.py convention: `.timestamp()` on both sides) so it never
        trips over naive-vs-aware datetime mismatches - last_changed arrives tz-aware from
        HA, `now` is this app's naive-local clock. None if last_changed is missing/unparseable."""
        try:
            last_changed = self.get_state(entity, attribute="last_changed")
            if not last_changed:
                return None
            lc = datetime.fromisoformat(str(last_changed))
            return max(0.0, now.timestamp() - lc.timestamp())
        except (ValueError, TypeError):
            return None

    # ---------- listeners ----------

    def _on_presence_change(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room") if isinstance(kwargs, dict) else None
        if not room:
            return
        try:
            now = self._now_local()
            st = self._room_state[room]
            if new == "on" and old != "on":
                st["presence_on_since"] = now
                st["elsewhere_since"] = None
                self._evaluate_room(room, now)
            elif new == "off" and old != "off":
                self._close_presence_session(room, now)
        except Exception as e:
            self.log(
                f"PresenceStuckWatch presence-change handler failed for {room} ({entity}): {e}",
                level="ERROR",
            )

    def _on_motion_change(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room") if isinstance(kwargs, dict) else None
        if not room:
            return
        try:
            if new in ("small", "large"):
                st = self._room_state[room]
                st["last_motion_at"] = self._now_local()
                # Fresh motion contradicts any "seen elsewhere" evidence gathered before it.
                st["elsewhere_since"] = None
        except Exception as e:
            self.log(
                f"PresenceStuckWatch motion-change handler failed for {room} ({entity}): {e}",
                level="ERROR",
            )

    def _on_light_change(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room") if isinstance(kwargs, dict) else None
        if not room:
            return
        try:
            now = self._now_local()
            st = self._room_state[room]
            if new == "on" and old != "on":
                st["light_on_since"] = now
            elif new == "off" and old != "off" and st["light_on_since"] is not None:
                self._roll_to(room, now)
                day = self._metrics[room][self._today[room]]
                day["light_on_min"] += (now - st["light_on_since"]).total_seconds() / 60
                st["light_on_since"] = None
                self._save_metrics()
        except Exception as e:
            self.log(
                f"PresenceStuckWatch light-change handler failed for {room} ({entity}): {e}",
                level="ERROR",
            )

    def _on_other_zone_on(self, entity, attribute, old, new, kwargs):
        room = kwargs.get("room") if isinstance(kwargs, dict) else None
        if not room:
            return
        try:
            st = self._room_state[room]
            if st["elsewhere_since"] is None:
                now = self._now_local()
                st["elsewhere_since"] = now
                self._evaluate_room(room, now)
        except Exception as e:
            self.log(
                f"PresenceStuckWatch other-zone handler failed for {room} ({entity}): {e}",
                level="ERROR",
            )

    def _on_person_change(self, entity, attribute, old, new, kwargs):
        try:
            now = self._now_local()
            self._recompute_nobody_home(now)
            for room in self.rooms:
                self._evaluate_room(room, now)
        except Exception as e:
            self.log(f"PresenceStuckWatch person-change handler failed ({entity}): {e}", level="ERROR")

    def _on_plugin_started(self, event_name, data, kwargs):
        try:
            self._publish_all(self._now_local())
        except Exception as e:
            self.log(f"PresenceStuckWatch plugin_started republish failed: {e}", level="ERROR")

    def _tick(self, kwargs):
        try:
            now = self._now_local()
            self._recompute_nobody_home(now)
            for room in self.rooms:
                self._evaluate_room(room, now)
                self._publish(room, now)
        except Exception as e:
            self.log(f"PresenceStuckWatch tick failed: {e}", level="ERROR")

    # ---------- nobody-home tracking (global - shared by every room) ----------

    def _recompute_nobody_home(self, now):
        if any(self.get_state(p) == "home" for p in self.persons):
            self._nobody_home_since = None
        elif self._nobody_home_since is None:
            self._nobody_home_since = now

    def _persons_home_count(self):
        return sum(1 for p in self.persons if self.get_state(p) == "home")

    # ---------- stuck evaluation ----------

    def _minutes_since_motion(self, room, now):
        st = self._room_state[room]
        if st["last_motion_at"] is not None:
            return (now - st["last_motion_at"]).total_seconds() / 60
        if st["presence_on_since"] is not None:
            # No motion ever observed this session - conservatively treat "no motion" as
            # running since the session itself started.
            return (now - st["presence_on_since"]).total_seconds() / 60
        return None

    def _evaluate_room(self, room, now):
        cfg = self.rooms[room]
        st = self._room_state[room]
        if self.get_state(cfg["presence"]) != "on":
            return
        if st["presence_on_since"] is None:
            # Defensive: presence reads on but we never saw the edge.
            st["presence_on_since"] = now

        rule = None
        if self._nobody_home_since is not None:
            away_min = (now - self._nobody_home_since).total_seconds() / 60
            if away_min >= cfg["nobody_home_min"]:
                rule = "nobody_home"

        if (
            rule is None
            and self._persons_home_count() == 1
            and st["elsewhere_since"] is not None
            and (st["last_motion_at"] is None or st["elsewhere_since"] > st["last_motion_at"])
        ):
            elsewhere_min = (now - st["elsewhere_since"]).total_seconds() / 60
            if elsewhere_min >= cfg["elsewhere_min"]:
                rule = "elsewhere"

        if rule is None:
            hold_min = (now - st["presence_on_since"]).total_seconds() / 60
            if hold_min >= cfg["long_hold_min"]:
                no_motion_min = self._minutes_since_motion(room, now)
                if no_motion_min is not None and no_motion_min >= cfg["long_hold_no_motion_min"]:
                    rule = "long_hold"

        if rule and not st["stuck"]:
            st["stuck"] = True
            st["stuck_since"] = now
            self._alert_stuck(room, rule, now)

    def _close_presence_session(self, room, now):
        st = self._room_state[room]
        if st["presence_on_since"] is not None:
            self._roll_to(room, now)
            day = self._metrics[room][self._today[room]]
            minutes = (now - st["presence_on_since"]).total_seconds() / 60
            day["sessions"] += 1
            day["presence_on_min"] += minutes
            day["max_session_min"] = max(day["max_session_min"], minutes)
            st["presence_on_since"] = None
            self._save_metrics()
        if st["stuck"]:
            stuck_minutes = (now - st["stuck_since"]).total_seconds() / 60 if st["stuck_since"] else 0.0
            self.log(
                f"PresenceStuckWatch: {room} presence-stuck episode cleared after "
                f"{stuck_minutes:.1f} min (presence back off)",
                level="INFO",
            )
        st["stuck"] = False
        st["stuck_since"] = None
        st["elsewhere_since"] = None
        self._publish(room, now)

    # ---------- stuck alerting ----------

    def _alert_stuck(self, room, rule, now):
        st = self._room_state[room]
        self._roll_to(room, now)
        day = self._metrics[room][self._today[room]]
        day["stuck_episodes"] += 1
        st["last_stuck_at"] = now
        message = self._stuck_message(room, rule, now)
        self.log(f"PresenceStuckWatch: {room} presence STUCK ({rule}) - {message}", level="WARNING")
        self.create_task(self._push(f"{self._pretty(room)} presence stuck?", message, f"{room} stuck"))
        self._save_metrics()
        self._publish(room, now)

    def _stuck_message(self, room, rule, now):
        st = self._room_state[room]
        pretty = self._pretty(room)
        if rule == "nobody_home":
            away_min = (now - self._nobody_home_since).total_seconds() / 60
            return f"nobody home for {away_min:.0f} min but {pretty} presence is still on"
        if rule == "elsewhere":
            elsewhere_min = (now - st["elsewhere_since"]).total_seconds() / 60
            return f"presence seen elsewhere {elsewhere_min:.0f} min ago but {pretty} presence is still on"
        if rule == "long_hold":
            hold_min = (now - st["presence_on_since"]).total_seconds() / 60
            no_motion_min = self._minutes_since_motion(room, now) or 0.0
            return f"{pretty} presence has been on {hold_min:.0f} min with no motion for {no_motion_min:.0f} min"
        return f"{pretty} presence looks stuck ({rule})"

    async def _push(self, title, message, log_context):
        try:
            await self._notifier.notify(title=title, message=message, target="user")
        except Exception as e:
            self.log(f"PresenceStuckWatch: {log_context} notify failed: {e}", level="WARNING")

    # ---------- anomaly alert ----------

    def _on_anomaly_check(self, kwargs):
        now = self._now_local()
        for room in self.rooms:
            try:
                self._check_anomaly(room, now)
            except Exception as e:
                self.log(f"PresenceStuckWatch anomaly check failed for {room}: {e}", level="ERROR")

    def _check_anomaly(self, room, now):
        prior = self._prior_days(room)
        if len(prior) < 3:
            return
        avg_prior_min = sum(d["presence_on_min"] for d in prior) / len(prior)
        threshold = max(60.0, 2.5 * avg_prior_min)
        today_on_min = self._today_totals(room, now)["presence_on_min"]
        if today_on_min > threshold:
            message = f"{today_on_min:.0f} min vs normal {avg_prior_min:.0f} min"
            self.log(
                f"PresenceStuckWatch: {room} presence unusually long today - {message}",
                level="WARNING",
            )
            self.create_task(
                self._push(f"{self._pretty(room)} presence unusually long today", message, f"{room} anomaly")
            )

    # ---------- metrics: rollover / accumulation ----------

    def _roll_to(self, room, now):
        """Ensure self._metrics[room] has a bucket for `now`'s local calendar day, rolling
        over (and splitting any live in-progress presence/light session exactly at the local
        midnight boundary) if the day has advanced since the last call. A no-op, cheap either
        way, so callers can call this unconditionally before touching today's bucket."""
        today_str = now.strftime("%Y-%m-%d")
        days = self._metrics.setdefault(room, {})
        cur = self._today.get(room)
        if cur == today_str:
            days.setdefault(today_str, _fresh_day())
            return
        if cur is not None:
            day = days.setdefault(cur, _fresh_day())
            midnight = datetime.combine(now.date(), dtime())
            st = self._room_state[room]
            if st["presence_on_since"] is not None and st["presence_on_since"] < midnight:
                minutes = (midnight - st["presence_on_since"]).total_seconds() / 60
                day["presence_on_min"] += minutes
                day["max_session_min"] = max(day["max_session_min"], minutes)
                st["presence_on_since"] = midnight
            if st["light_on_since"] is not None and st["light_on_since"] < midnight:
                minutes = (midnight - st["light_on_since"]).total_seconds() / 60
                day["light_on_min"] += minutes
                st["light_on_since"] = midnight
        days.setdefault(today_str, _fresh_day())
        self._today[room] = today_str
        self._prune_old_days(room)
        self._save_metrics()

    def _prune_old_days(self, room):
        days = self._metrics.get(room, {})
        if len(days) <= _RETAIN_DAYS:
            return
        for key in sorted(days)[:-_RETAIN_DAYS]:
            del days[key]

    def _today_totals(self, room, now):
        """Today's bucket plus any still-open presence/light session, live - so a session
        that has not closed yet (e.g. an ongoing stuck episode) still counts toward today's
        totals rather than waiting for it to end."""
        self._roll_to(room, now)
        day = self._metrics[room][self._today[room]]
        st = self._room_state[room]
        sessions = day["sessions"]
        presence_on_min = day["presence_on_min"]
        max_session = day["max_session_min"]
        light_on_min = day["light_on_min"]
        sessions_for_avg = sessions
        if st["presence_on_since"] is not None:
            elapsed = (now - st["presence_on_since"]).total_seconds() / 60
            presence_on_min += elapsed
            max_session = max(max_session, elapsed)
            sessions_for_avg += 1
        if st["light_on_since"] is not None:
            light_on_min += (now - st["light_on_since"]).total_seconds() / 60
        avg_session_min = (presence_on_min / sessions_for_avg) if sessions_for_avg else 0.0
        return {
            "sessions": sessions,
            "presence_on_min": presence_on_min,
            "max_session_min": max_session,
            "light_on_min": light_on_min,
            "avg_session_min": avg_session_min,
        }

    def _prior_days(self, room, limit=7):
        """Up to the `limit` most recent calendar days that actually have data, excluding
        today (which is still live/unfinished) - skips gaps (downtime) rather than trying to
        zero-fill them."""
        days = self._metrics.get(room, {})
        today = self._today.get(room)
        keys = sorted(k for k in days if k != today)[-limit:]
        return [days[k] for k in keys]

    def _avg_session_min_7d(self, room):
        prior = [d for d in self._prior_days(room) if d["sessions"] > 0]
        if not prior:
            return 0.0
        return sum(d["presence_on_min"] / d["sessions"] for d in prior) / len(prior)

    def _light_on_min_7d_avg(self, room):
        prior = self._prior_days(room)
        if not prior:
            return 0.0
        return sum(d["light_on_min"] for d in prior) / len(prior)

    # ---------- publish ----------

    def _publish_all(self, now):
        for room in self.rooms:
            self._publish(room, now)

    def _publish(self, room, now):
        totals = self._today_totals(room, now)
        day = self._metrics[room][self._today[room]]
        last_stuck = self._room_state[room].get("last_stuck_at")
        attributes = {
            "sessions_today": str(totals["sessions"]),
            "presence_on_min_today": f"{totals['presence_on_min']:.1f}",
            "max_session_min_today": f"{totals['max_session_min']:.1f}",
            "light_on_min_today": f"{totals['light_on_min']:.1f}",
            "avg_session_min_7d": f"{self._avg_session_min_7d(room):.1f}",
            "light_on_min_7d_avg": f"{self._light_on_min_7d_avg(room):.1f}",
            "stuck_episodes_today": str(day["stuck_episodes"]),
            "last_stuck_at": last_stuck.isoformat() if last_stuck else "never",
            "unit_of_measurement": "min",
        }
        entity_id = f"sensor.{room}_presence_stats"
        try:
            self.set_state(
                entity_id, state=f"{totals['avg_session_min']:.1f}", replace=True, attributes=attributes
            )
        except Exception as e:
            self.log(f"PresenceStuckWatch publish failed for {room}: {e}", level="WARNING")

    # ---------- persistence (atomic tmp + os.replace, house_events/lock_health pattern) ----------

    def _save_metrics(self):
        try:
            payload = {"rooms": {}}
            for room in self.rooms:
                last_stuck = self._room_state[room].get("last_stuck_at")
                payload["rooms"][room] = {
                    "days": self._metrics.get(room, {}),
                    "last_stuck_at": last_stuck.isoformat() if last_stuck else None,
                }
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps(payload))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"PresenceStuckWatch state save failed: {e}", level="WARNING")

    def _load_metrics(self):
        try:
            raw = self._state_file.read_text()
        except FileNotFoundError:
            return
        except Exception as e:
            self.log(f"PresenceStuckWatch state load failed: {e}", level="WARNING")
            return
        try:
            data = json.loads(raw)
        except Exception as e:
            self.log(f"PresenceStuckWatch state load failed: invalid JSON - {e}", level="WARNING")
            return

        rooms = (data or {}).get("rooms") or {}
        for room, blob in rooms.items():
            if room not in self.rooms:
                continue  # config dropped this room - ignore its stale history
            blob = blob or {}
            days = {}
            for date_str, raw_day in (blob.get("days") or {}).items():
                if not isinstance(raw_day, dict):
                    continue
                day = _fresh_day()
                for key in day:
                    val = raw_day.get(key)
                    if isinstance(val, (int, float)) and not isinstance(val, bool):
                        day[key] = val
                days[date_str] = day
            self._metrics[room] = days
            last_stuck = _parse_iso(blob.get("last_stuck_at"))
            if last_stuck is not None:
                self._room_state[room]["last_stuck_at"] = last_stuck

    # ---------- misc ----------

    def _now_local(self):
        """Naive local datetime (repo idiom - see house_night_mode.py). Falls back to
        datetime.now() on a bare test instance."""
        try:
            return self.datetime()
        except Exception:
            return datetime.now()

    @staticmethod
    def _pretty(room):
        return room.replace("_", " ").capitalize()
