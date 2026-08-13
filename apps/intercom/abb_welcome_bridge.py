"""ABB Welcome bridge - additive observer next to the ESP intercom (2026-08-12).

The flat's doorbell is handled by the ESP bus tap ("intercomproxy") via
apps/intercom/intercom.py. That app is SECURITY-CRITICAL and stays authoritative:
it announces rings and performs the auto-open unlocks. This app adds the NEW
capabilities the ABB Welcome cloud/SIP integration brings, and it must degrade to
"features missing" - never "doorbell broken" - so every external touchpoint is
try/except-isolated and nothing here ever calls a lock, button, or switch service.

What the ABB integration provides (verified against its source and live history):
- HA bus event `abb_welcome_ring` fired by the integration's SIP listener the
  instant an INVITE arrives. Payload: caller_uri, caller_user, station_id,
  station, station_name, call_id, received_at (epoch float). Realtime - measured
  ~50 ms AHEAD of the ESP ring sensors on both of today's rings.
- binary_sensor...intercom_ringing: same SIP trigger, ON for a 5 s hold, with the
  ring payload as attributes (backup intake if the bus event is missed).
- event...intercom / sensor...last_event: CLOUD-POLLED (30 s interval) portal
  events: ring / door-open / call-answered / call-terminated / call-missed /
  light (+ screenshot, filtered out of the event entity). call-missed arrives
  with station_id="" - door attribution must come from the preceding ring.
  NOTE: within one poll batch the event entity fires events NEWEST-FIRST, so
  arrival order here is not chronological; we key on each event's own timestamp.
- image...latest_screenshot: JPEG the gateway captures when someone rings,
  delivered by the same 30 s poll: measured 7 s and 32 s after today's two rings.
  At ring time the entity still holds the PREVIOUS visitor's photo - every
  consumer below checks captured_at freshness before trusting it.

Station -> door mapping (evidence, 2026-08-12, two independent correlations):
  ESP back ring  14:24:44.256Z <-> abb ring station 100000001 "Outdoor Station 1-1"
  ESP front ring 16:35:28.774Z <-> abb ring station 100000002 "Outdoor Station 2-1"
  => 100000001 = back door, 100000002 = front door (also consistent with the
  integration option default_unlock_station_id=100000002 - the front door is the
  one the household buzzes most). Configured in abb_welcome_bridge.yaml; used for
  LABELS ONLY, and re-verified continuously by the comparator matrix below.

Features:
1. MISSED-CALL PUSH - the ABB portal logs call-missed for every unanswered SIP
   call, INCLUDING rings the house answered by firing the ESP relay (auto-open is
   on ~95% of the time; verified on today's 14:24 ring: auto_open on, back-door
   lock "unlocking" at ring+9 s, portal still logged Call Missed). So a missed
   push fires ONLY when no unlock evidence exists near the ring: physical lock
   activity (lock.intercomproxy_* -> unlocking/unlocked, which also covers a
   housemate buzzing from the wall unit) within suppress_window_s of the ring.
   Genuine miss => one push per portal event (event_id-deduped, persisted) with
   the doorbell snapshot attached, plus a house_events_report feed entry.
2. RING SNAPSHOT ATTACHMENT - ring_attachment_data() hands intercom.py a
   companion-app image payload for its existing auto-open pushes. The image URL
   is the image entity's entity_picture (token-signed /api/image_proxy/... path,
   verified to serve the JPEG without other auth; relative URLs resolve through
   the companion app's own HA connection, so they render off-LAN too). Because
   of the poll lag the photo can be one visitor stale at push time - accepted
   and documented; the missed-call push and the archive use freshness checks.
3. RING COMPARATOR - phase-2 evidence. Every ring seen by either side is paired
   within pair_window_s; results are counted (rings_both / rings_esp_only /
   rings_abb_only + lag stats), logged grep-friendly ("RING-CMP ..."), published
   on sensor.abb_esp_ring_agreement, and persisted across restarts.
4. RING IMAGE ARCHIVE - every ring episode (and standalone missed call) saves
   the gateway snapshot to /www/abb_doorbell (= /local/abb_doorbell/, same
   exposure as the vacuum's rober2_maps) with an atomically-written index.json
   the dashboard can browse. One file per ring EPISODE: the episode holds the
   newest FRESH snapshot (captured_at >= ring start) and writes once, at episode
   close, when the auto-opened/missed/answered classification is known. A
   snapshot probe runs a few seconds after the ring (knob), but the real capture
   normally comes from the image entity's own update 7-32 s later; stale frames
   (previous visitor) are never archived - a missing photo is honest, the wrong
   person is misleading. Retention: retain_days / max_files pruned on every
   write, WARNING above archive_warn_mb.

Nothing here depends on intercom.py, and intercom.py's only dependency on this
app is an optional, lazily-resolved, exception-swallowed attachment lookup.
"""

import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

# ---------------------------------------------------------------------------
# Pure module-level helpers - unit-testable without an AppDaemon runtime
# (tests/test_abb_welcome_bridge.py), mirroring intercom.py's layout.
# ---------------------------------------------------------------------------

# Portal event types (integration's event.py EVENT_TYPES, minus "screenshot"
# which the event entity filters out before firing).
EVENT_RING = "ring"
EVENT_MISSED = "call-missed"
EVENT_ANSWERED = "call-answered"

INDEX_VERSION = 1
JPEG_MAGIC = b"\xff\xd8"
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # sanity cap on a single doorbell frame
MAX_PROCESSED_IDS = 50  # missed-call event_id dedupe ring buffer


def classify_episode(unlock_seen, missed_seen, answered_seen):
    """Final event_type tag for a ring episode, ordered by what actually matters:
    the door opening trumps the portal's "missed" (auto-open answers the door
    without anyone picking up the SIP call, so the portal logs call-missed for
    rings that went fine - verified live 2026-08-12 14:24 and 16:35)."""
    if unlock_seen:
        return "ring_auto_opened"
    if answered_seen:
        return "ring_answered"
    if missed_seen:
        return "ring_missed"
    return "ring"


def lag_ms(esp_at, abb_at):
    """Comparator lag for a paired ring: positive = ABB saw it AFTER the ESP,
    negative = ABB first. None when either side is missing."""
    if esp_at is None or abb_at is None:
        return None
    return (abb_at - esp_at).total_seconds() * 1000.0


def door_slug(door, station_id):
    """Filename-safe door tag: "front door" -> front, unmapped -> station_<id>."""
    if door:
        slug = re.sub(r"[^a-z0-9]+", "_", str(door).lower()).strip("_")
        slug = re.sub(r"_door$", "", slug)
        if slug:
            return slug
    if station_id:
        return f"station_{station_id}"
    return "unknown"


def build_index_entry(ring_at, station_id, door, event_type, filename, url_prefix):
    """One index.json row - the exact shape the dashboard gallery will consume:
    {"ts","datetime","station","door","event_type","filename","url"}.
    ts = epoch seconds of the ring (sortable int); datetime = the same instant
    as a local-time ISO string (human-facing, like the archive filenames)."""
    return {
        "ts": int(ring_at.timestamp()),
        "datetime": ring_at.astimezone().isoformat(timespec="seconds"),
        "station": station_id or "",
        "door": door or "",
        "event_type": event_type,
        "filename": filename,
        "url": f"{url_prefix.rstrip('/')}/{filename}",
    }


def prune_images(entries, now_ts, retain_days, max_files):
    """Split index entries (newest-first) into (keep, drop) by both retention
    bounds - same bounded-file discipline as forecast_log/rober2 maps."""
    cutoff = now_ts - retain_days * 86400
    keep, drop = [], []
    for i, entry in enumerate(entries):
        ts = entry.get("ts", 0)
        if i < max_files and isinstance(ts, (int, float)) and ts >= cutoff:
            keep.append(entry)
        else:
            drop.append(entry)
    return keep, drop


def parse_iso_ts(raw):
    """Parse the integration's ISO timestamps ("...Z" or offset-aware)."""
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


class AbbWelcomeBridge(hass.Hass):
    def initialize(self):
        # --- ABB entities (READ-ONLY - this app never actuates anything ABB) ---
        self.abb_event_entity = self.args.get("abb_event_entity", "event.abb_welcome_gateway_intercom")
        self.abb_ringing_sensor = self.args.get("abb_ringing_sensor", "binary_sensor.abb_welcome_gateway_intercom_ringing")
        self.abb_image_entity = self.args.get("abb_image_entity", "image.abb_welcome_gateway_latest_screenshot")
        self.abb_ring_bus_event = self.args.get("abb_ring_bus_event", "abb_welcome_ring")

        # --- ESP side (observed only; intercom.py remains the actor) ---
        self.esp_door_sensors = dict(self.args.get("esp_door_sensors", {}))  # entity -> door label
        self.esp_lock_doors = dict(self.args.get("esp_lock_doors", {}))  # lock entity -> door label
        self.auto_open_entity = self.args.get("auto_open_boolean", "input_boolean.auto_open_intercom")

        # --- door mapping (labels only; comparator matrix re-verifies it) ---
        self.station_doors = {str(k): v for k, v in dict(self.args.get("station_doors", {})).items()}

        # --- knobs ---
        self.pair_window_s = int(self.args.get("pair_window_s", 10))
        self.debounce_s = int(self.args.get("debounce_s", 5))  # per-side re-ring fold, matches intercom.py
        self.episode_close_s = int(self.args.get("episode_close_s", 75))  # > worst poll lag (30 s cycle) + missed delivery
        self.suppress_window_s = int(self.args.get("suppress_window_s", 30))  # unlock-near-ring => not a real miss
        self.missed_decision_delay_s = int(self.args.get("missed_decision_delay_s", 15))  # unlock evidence lands by ring+9 s (measured); wait it out
        self.missed_max_age_s = int(self.args.get("missed_max_age_s", 900))  # ignore replayed/stale portal events
        self.snapshot_probe_s = int(self.args.get("snapshot_probe_s", 6))  # early probe; the poll usually beats it by lagging
        self.notify_target = self.args.get("notify_target", "home")

        # --- announce-after-unlock knobs (user + integration agreement 2026-08-12) ---
        # When the house auto-opens for a ring, tell the person at the door. Policy lives
        # HERE, capability lives in the integration (abb_welcome.announce, unattended TTS).
        # Front door only for now: the back door reportedly has a built-in voice of its own,
        # unverified - two overlapping voices would be worse than none. The gate is the
        # PHYSICAL unlock edge (lock.intercomproxy_* -> unlocking/unlocked), not the unlock
        # request: speak only when the door demonstrably opened. Ring-gated so a dashboard
        # unlock with nobody outside never talks to the street.
        raw_announce = self.args.get("announce_after_unlock", {}) or {}
        self.announce_cameras = {str(k): str(v) for k, v in raw_announce.items()} if isinstance(raw_announce, dict) else {}
        self.announce_message = str(self.args.get("announce_message", "The door is open."))
        self.announce_cooldown_s = int(self.args.get("announce_cooldown_s", 90))
        self.announce_ring_window_s = int(self.args.get("announce_ring_window_s", 60))

        # --- ring clip knobs (2026-08-13, "thumbnail and opening that give the video") ---
        # HA's native camera.record works against the integration's RTSP layer, but ONLY
        # while arm_streaming is active, and announce/record are mutually exclusive on the
        # station (probed live 2026-08-13 08:20: announce mid-record raised "an RTSP camera
        # stream is already in use" AND the recording lost its video). So the clip starts
        # clip_delay_s after the ring - by then an auto-open announce (unlock at ring+1-2 s,
        # ~4 s of TTS) has finished talking - and slips once more if the announce just spoke.
        raw_clip = self.args.get("clip_cameras", {}) or {}
        self.clip_cameras = {str(k): str(v) for k, v in raw_clip.items()} if isinstance(raw_clip, dict) else {}
        self.clip_seconds = int(self.args.get("clip_seconds", 10))
        self.clip_delay_s = int(self.args.get("clip_delay_s", 8))
        # archive_dir as HA's own container sees it: the record service writes there,
        # this app (whose /www is the same directory) stats the result at episode close.
        self.clip_record_dir = str(self.args.get("clip_record_dir", "/config/www/abb_doorbell")).rstrip("/")
        self.station_by_door = {v: k for k, v in self.station_doors.items()}

        # --- archive knobs ---
        self.archive_dir = Path(self.args.get("archive_dir", "/www/abb_doorbell"))
        self.archive_url_prefix = self.args.get("archive_url_prefix", "/local/abb_doorbell")
        self.retain_days = int(self.args.get("retain_days", 90))
        self.max_files = int(self.args.get("max_files", 2000))
        self.archive_warn_mb = int(self.args.get("archive_warn_mb", 500))

        # --- state ---
        self.episodes = {}  # stable episode id -> episode dict (open episodes only)
        self._episode_seq = 0
        self.last_unlock_at = {}  # door label -> datetime of last unlocking/unlocked edge
        self._last_announce_at = {}  # door label -> datetime of last spoken announcement
        self.mobile_notifier = self._get_mobile_notifier()
        self._state_file = Path(__file__).with_name("abb_welcome_bridge_state.json")
        persisted = self._load_state()
        self.counters = persisted.get("counters", {"rings_both": 0, "rings_esp_only": 0, "rings_abb_only": 0})
        self.lag_stats = persisted.get("lag", {"sum_ms": 0.0, "n": 0, "last_ms": None})
        self.processed_missed_ids = list(persisted.get("processed_missed_ids", []))[-MAX_PROCESSED_IDS:]
        self.station_door_matrix = persisted.get("station_door_matrix", {})

        # --- listeners: each registration isolated so one bad entity name costs
        # one feature, not the app ---
        for entity, door in self.esp_door_sensors.items():
            try:
                # new="on" only: replay edges (unavailable->on) still count as rings
                # for OBSERVATION - we never unlock, so the replay risk intercom.py
                # guards against does not apply here, and skipping them would skew
                # the comparator (see the listen_state cloud-edge gotcha).
                self.listen_state(self._on_esp_ring, entity, new="on", door=door)
            except Exception as e:
                self.log(f"ESP ring listener failed for {entity}: {e}", level="WARNING")
        try:
            self.listen_event(self._on_abb_bus_ring, self.abb_ring_bus_event)
        except Exception as e:
            self.log(f"ABB bus-ring listener failed: {e}", level="WARNING")
        try:
            # Backup intake only: the bus event and this sensor share one SIP
            # trigger, so _register_ring's per-side fold absorbs the duplicate.
            self.listen_state(self._on_abb_ringing_edge, self.abb_ringing_sensor, new="on", attribute="all")
        except Exception as e:
            self.log(f"ABB ringing-sensor listener failed: {e}", level="WARNING")
        try:
            self.listen_state(self._on_abb_event, self.abb_event_entity, attribute="all")
        except Exception as e:
            self.log(f"ABB event-entity listener failed: {e}", level="WARNING")
        try:
            self.listen_state(self._on_image_update, self.abb_image_entity, attribute="all")
        except Exception as e:
            self.log(f"ABB image listener failed: {e}", level="WARNING")
        for lock_entity in self.esp_lock_doors:
            try:
                self.listen_state(self._on_lock_activity, lock_entity)
            except Exception as e:
                self.log(f"Lock listener failed for {lock_entity}: {e}", level="WARNING")

        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log(f"Archive dir {self.archive_dir} unavailable: {e} - archiving disabled until it appears", level="WARNING")

        self._publish_agreement()
        self.log(
            f"ABB Welcome bridge initialized (stations: {self.station_doors}; "
            f"counters: {self.counters})", level="INFO",
        )

    def _get_mobile_notifier(self):
        # get_app must be resolved in sync init - async context returns a Task.
        try:
            notifier = self.get_app("MobileNotifier")
            if not notifier:
                self.log("MobileNotifier app not found; missed-call pushes will only be logged.", level="WARNING")
            return notifier
        except Exception as e:
            self.log(f"Error getting MobileNotifier app: {e}. Missed-call pushes will only be logged.", level="WARNING")
            return None

    # ------------------------------------------------------------------
    # Public seam for intercom.py (called cross-thread from its sync
    # callbacks; must stay fast, sync, and non-raising).
    # ------------------------------------------------------------------
    def ring_attachment_data(self):
        """Companion-app image payload for a ring push, or None.

        Returns {"data": {"image": <entity_picture>}} - MobileNotifier merges the
        inner dict into the notify service's data, which is the documented
        companion-app attachment syntax for both Android and iOS. The URL is
        relative, so the app fetches it over its own HA connection (works
        off-LAN). Freshness caveat: the gateway's screenshot arrives on a 30 s
        cloud poll (measured 7-32 s after today's rings), so at auto-open push
        time (~ring+3-10 s) this usually still shows the PREVIOUS visitor.
        Attached anyway - a slightly stale doorbell photo beats none - and the
        missed-call push + archive (which can wait) apply real freshness checks.
        """
        try:
            url = self.get_state(self.abb_image_entity, attribute="entity_picture")
            if url:
                return {"data": {"image": url}}
        except Exception as e:
            try:
                self.log(f"ring_attachment_data failed: {e}", level="DEBUG")
            except Exception:
                pass
        return None

    # ------------------------------------------------------------------
    # Ring intake (three sources -> one _register_ring)
    # ------------------------------------------------------------------
    def _on_esp_ring(self, entity, attribute, old, new, kwargs):
        try:
            self._register_ring("esp", kwargs.get("door"), "", self.get_now(), f"esp:{entity}")
        except Exception as e:
            self.log(f"ESP ring handling failed: {e}", level="WARNING")

    def _on_abb_bus_ring(self, event_name, data, kwargs):
        try:
            data = data or {}
            station_id = str(data.get("station_id", "") or "")
            at = None
            received = data.get("received_at")
            if isinstance(received, (int, float)):
                at = datetime.fromtimestamp(received, tz=timezone.utc)
            self._register_ring("abb", self.station_doors.get(station_id), station_id,
                                at or self.get_now(), "abb:bus")
        except Exception as e:
            self.log(f"ABB bus ring handling failed: {e}", level="WARNING")

    def _on_abb_ringing_edge(self, entity, attribute, old, new, kwargs):
        try:
            attrs = (new or {}).get("attributes", {}) if isinstance(new, dict) else {}
            station_id = str(attrs.get("station_id", "") or "")
            self._register_ring("abb", self.station_doors.get(station_id), station_id,
                                self.get_now(), "abb:sensor")
        except Exception as e:
            self.log(f"ABB ringing-sensor handling failed: {e}", level="WARNING")

    def _register_ring(self, side, door, station_id, at, source):
        """Fold a ring edge into an episode (creating one if needed).

        Pairing is TIME-first: an ABB ring attaches to an open episode still
        missing its ABB side within pair_window_s (and vice versa), preferring a
        door-label match. That is what lets the comparator accumulate
        station<->door mapping evidence even for unmapped stations.
        """
        side_key = f"{side}_at"
        episode = self._match_episode(side_key, door, at)
        if episode is None:
            episode = self._open_episode(door, station_id, at)
        if episode[side_key] is not None:
            # Same side again while the episode is open: the same visitor bouncing
            # the button (or the ringing-sensor backup echoing the bus event) -
            # fold it, counting it as a re-ring only outside the debounce window.
            if (at - episode[side_key]).total_seconds() >= self.debounce_s:
                episode["re_rings"] += 1
            return
        episode[side_key] = at
        if station_id and not episode["station_id"]:
            episode["station_id"] = station_id
        if door and not episode["door"]:
            episode["door"] = door
        if not episode["door"] and episode["station_id"]:
            episode["door"] = self.station_doors.get(episode["station_id"])
        self.log(
            f"RING-SEEN side={side} source={source} door={episode['door'] or '?'} "
            f"station={episode['station_id'] or '?'} episode={episode['id']}", level="INFO",
        )
        if episode["esp_at"] and episode["abb_at"]:
            self._score_pairing(episode)

    def _match_episode(self, side_key, door, at):
        """Open episode this edge belongs to: prefer pairing (episode missing this
        side, started within pair_window_s, doors compatible), else fold into the
        episode that already saw this side (re-ring). None = open a new one."""
        pair_candidate = None
        fold_candidate = None
        for episode in self.episodes.values():
            age = (at - episode["started_at"]).total_seconds()
            if age < -2 or age > self.episode_close_s:
                continue
            if door and episode["door"] and episode["door"] != door:
                continue  # different door = different visitor
            if episode[side_key] is None:
                if age <= self.pair_window_s:
                    if door and episode["door"] == door:
                        return episode  # exact door match wins immediately
                    if pair_candidate is None:
                        pair_candidate = episode
            elif fold_candidate is None:
                fold_candidate = episode
        return pair_candidate or fold_candidate

    def _open_episode(self, door, station_id, at):
        self._episode_seq += 1
        episode_id = f"ep{self._episode_seq}"
        episode = {
            "id": episode_id,
            "door": door,
            "station_id": station_id or "",
            "started_at": at,
            "esp_at": None,
            "abb_at": None,
            "re_rings": 0,
            "missed_event": None,
            "answered": False,
            "push_sent": False,
            "scored": False,
            "snapshot": None,  # {"bytes","captured_at","event_id"}
            "clip_filename": None,  # set by _start_clip once a recording was requested
            "closed": False,
        }
        self.episodes[episode_id] = episode
        try:
            self.run_in(self._pair_check, self.pair_window_s, episode_id=episode_id)
            self.run_in(self._probe_snapshot, self.snapshot_probe_s, episode_id=episode_id)
            self.run_in(self._close_episode, self.episode_close_s, episode_id=episode_id)
            if self.clip_cameras and self.clip_seconds > 0:
                self.run_in(self._start_clip, self.clip_delay_s, episode_id=episode_id)
        except Exception as e:
            self.log(f"Episode timers failed for {episode_id}: {e}", level="WARNING")
        return episode

    # ------------------------------------------------------------------
    # Comparator (phase-2 evidence machine)
    # ------------------------------------------------------------------
    def _score_pairing(self, episode):
        if episode["scored"]:
            return
        episode["scored"] = True
        lag = lag_ms(episode["esp_at"], episode["abb_at"])
        self.counters["rings_both"] += 1
        if lag is not None:
            self.lag_stats["sum_ms"] += lag
            self.lag_stats["n"] += 1
            self.lag_stats["last_ms"] = round(lag, 1)
        if episode["station_id"]:
            row = self.station_door_matrix.setdefault(episode["station_id"], {})
            esp_door = episode["door"] or "unknown"
            row[esp_door] = row.get(esp_door, 0) + 1
        lag_txt = f"{lag:.1f} ({'abb_first' if lag < 0 else 'esp_first'})" if lag is not None else "?"
        self.log(
            f"RING-CMP MATCH door={episode['door'] or '?'} "
            f"station={episode['station_id'] or '?'} lag_ms={lag_txt}", level="INFO",
        )
        self._publish_agreement()
        self._save_state()

    def _pair_check(self, kwargs):
        """pair_window_s after an episode opened: anything still one-sided is a
        divergence worth counting - that is the whole point of running two ears."""
        try:
            episode = self.episodes.get(kwargs.get("episode_id"))
            if episode is None or episode["scored"]:
                return
            episode["scored"] = True
            if episode["esp_at"] and not episode["abb_at"]:
                self.counters["rings_esp_only"] += 1
                self.log(
                    f"RING-CMP ESP-ONLY door={episode['door'] or '?'} "
                    f"waited_ms={self.pair_window_s * 1000}", level="WARNING",
                )
            elif episode["abb_at"] and not episode["esp_at"]:
                self.counters["rings_abb_only"] += 1
                self.log(
                    f"RING-CMP ABB-ONLY station={episode['station_id'] or '?'} "
                    f"door={episode['door'] or '?'} waited_ms={self.pair_window_s * 1000}",
                    level="WARNING",
                )
            self._publish_agreement()
            self._save_state()
        except Exception as e:
            self.log(f"Pair check failed: {e}", level="WARNING")

    def _publish_agreement(self):
        try:
            total = (self.counters["rings_both"] + self.counters["rings_esp_only"]
                     + self.counters["rings_abb_only"])
            mean = (self.lag_stats["sum_ms"] / self.lag_stats["n"]) if self.lag_stats["n"] else None
            self.set_state(
                "sensor.abb_esp_ring_agreement",
                state=str(total),
                attributes={
                    "friendly_name": "ABB/ESP ring agreement",
                    "icon": "mdi:bell-check",
                    "rings_both": self.counters["rings_both"],
                    "rings_esp_only": self.counters["rings_esp_only"],
                    "rings_abb_only": self.counters["rings_abb_only"],
                    "mean_lag_ms": round(mean, 1) if mean is not None else None,
                    "last_lag_ms": self.lag_stats["last_ms"],
                    "lag_samples": self.lag_stats["n"],
                    "lag_convention": "positive = ABB after ESP",
                    "station_door_matrix": self.station_door_matrix,
                },
                replace=True,
            )
        except Exception as e:
            self.log(f"Agreement publish failed: {e}", level="WARNING")

    # ------------------------------------------------------------------
    # Cloud events (poll-lagged): missed / answered
    # ------------------------------------------------------------------
    def _on_abb_event(self, entity, attribute, old, new, kwargs):
        try:
            attrs = (new or {}).get("attributes", {}) if isinstance(new, dict) else {}
            event_type = attrs.get("event_type")
            if event_type == EVENT_MISSED:
                self._handle_missed(attrs)
            elif event_type == EVENT_ANSWERED:
                episode = self._episode_near(parse_iso_ts(attrs.get("timestamp")) or self.get_now())
                if episode:
                    episode["answered"] = True
            # ring events also arrive here (poll-lagged); the realtime intake
            # already covers them, and a 7-32 s late duplicate would misread as
            # a re-ring, so they are deliberately NOT registered.
        except Exception as e:
            self.log(f"ABB event handling failed: {e}", level="WARNING")

    def _episode_near(self, at, window_s=120):
        best, best_age = None, None
        for episode in self.episodes.values():
            age = abs((at - episode["started_at"]).total_seconds())
            if age <= window_s and (best_age is None or age < best_age):
                best, best_age = episode, age
        return best

    def _handle_missed(self, attrs):
        event_id = attrs.get("event_id") or ""
        portal_ts = parse_iso_ts(attrs.get("timestamp"))
        now = self.get_now()
        if event_id and event_id in self.processed_missed_ids:
            return  # replay (HA restart restores the event entity's last event)
        if portal_ts and (now - portal_ts).total_seconds() > self.missed_max_age_s:
            self.log(f"Ignoring stale call-missed {event_id} from {attrs.get('timestamp')}", level="INFO")
            return
        if event_id:
            self.processed_missed_ids = (self.processed_missed_ids + [event_id])[-MAX_PROCESSED_IDS:]
            self._save_state()
        anchor = portal_ts or now
        episode = self._episode_near(anchor)
        if episode is not None:
            if episode["missed_event"] is not None:
                return  # one decision per episode
            episode["missed_event"] = {"event_id": event_id, "at": anchor}
            ring_at = episode["started_at"]
        else:
            ring_at = anchor
        # Do not decide yet: the intercom's unlock attempts run at ring+1/4/7 s and
        # the ESP lock's state lands ~ring+9 s (measured 14:24 today) - later than
        # the earliest missed delivery (+7 s). Wait until the evidence window closed.
        decide_in = max((ring_at + timedelta(seconds=self.missed_decision_delay_s) - now).total_seconds(), 2)
        try:
            self.run_in(self._decide_missed, decide_in,
                        episode_id=episode["id"] if episode else None,
                        event_id=event_id, anchor_iso=anchor.isoformat())
        except Exception as e:
            self.log(f"Missed-call decision scheduling failed: {e}", level="WARNING")

    def _decide_missed(self, kwargs):
        try:
            episode = self.episodes.get(kwargs.get("episode_id")) if kwargs.get("episode_id") else None
            anchor = parse_iso_ts(kwargs.get("anchor_iso")) or self.get_now()
            ring_at = episode["started_at"] if episode else anchor
            door = episode["door"] if episode else None
            station_id = episode["station_id"] if episode else ""
            unlock_at = self._unlock_near(door, ring_at)
            auto_open = None
            try:
                auto_open = self.get_state(self.auto_open_entity)
            except Exception:
                pass
            if unlock_at is not None:
                # The house answered the door (auto-open or a manual buzz) - the
                # portal's "missed" only means nobody picked up the SIP call.
                self.log(
                    f"MISSED-SUPPRESSED door={door or '?'} ring={ring_at.isoformat()} "
                    f"unlock={unlock_at.isoformat()} auto_open={auto_open} "
                    f"event_id={kwargs.get('event_id')}", level="INFO",
                )
                return
            if episode is not None:
                if episode["push_sent"]:
                    return
                episode["push_sent"] = True
            self.log(
                f"MISSED-CONFIRMED door={door or '?'} ring={ring_at.isoformat()} "
                f"auto_open={auto_open} event_id={kwargs.get('event_id')}", level="INFO",
            )
            self._send_missed_push(door, station_id, ring_at)
            if episode is None:
                # No ring episode (bridge restarted / both realtime ears missed it):
                # archive the miss standalone so the visitor is still traceable.
                self._archive_standalone_missed(anchor, station_id)
        except Exception as e:
            self.log(f"Missed-call decision failed: {e}", level="WARNING")

    def _unlock_near(self, door, ring_at):
        """Unlock evidence within +/- suppress_window_s of the ring; a door label
        narrows it to that door's lock, otherwise any lock counts."""
        for lock_door, at in self.last_unlock_at.items():
            if door and lock_door != door:
                continue
            if abs((at - ring_at).total_seconds()) <= self.suppress_window_s:
                return at
        return None

    def _on_lock_activity(self, entity, attribute, old, new, kwargs):
        try:
            if new in ("unlocking", "unlocked"):
                door = self.esp_lock_doors.get(entity, entity)
                self.last_unlock_at[door] = self.get_now()
                self._maybe_announce(door)
        except Exception as e:
            self.log(f"Lock activity handling failed: {e}", level="WARNING")

    def _maybe_announce(self, door):
        """Speak "the door is open" at the door that was just unlocked for a ring.

        Fires only when ALL of: the door has an announce camera configured (front only
        today - the back door's own built-in voice is unverified, and two overlapping
        voices are worse than none); a ring EPISODE for that door started within
        announce_ring_window_s (a dashboard unlock with nobody outside must not talk to
        the street); and the per-door cooldown has passed (the ESP fires unlocking AND
        unlocked edges seconds apart - one visitor, one sentence). Failure costs the
        sentence, never the unlock: this runs strictly after the lock command."""
        try:
            camera = self.announce_cameras.get(door)
            if not camera:
                return
            now = self.get_now()
            ring_recent = any(
                ep.get("door") == door
                and not ep.get("closed")
                and (now - ep["started_at"]).total_seconds() <= self.announce_ring_window_s
                for ep in self.episodes.values()
            )
            if not ring_recent:
                return
            last = self._last_announce_at.get(door)
            if last is not None and (now - last).total_seconds() < self.announce_cooldown_s:
                return
            self._last_announce_at[door] = now
            self.call_service(
                "abb_welcome/announce",
                entity_id=camera,
                message=self.announce_message,
            )
            self.log(f"ANNOUNCE door={door} camera={camera} message={self.announce_message!r}")
        except Exception as e:
            self.log(f"Announce failed for {door}: {e}", level="WARNING")

    def _start_clip(self, kwargs):
        """clip_delay_s after the ring: pull a short mp4 of the visitor via HA's native
        camera.record (probe-verified 2026-08-13: h264 640x480, needs arm_streaming
        first). Best-effort by design - the PHOTO is the guaranteed artifact and the
        clip only ever adds; every failure path is a log line, never a lost photo.
        Never runs while the announce could still be talking: announce and record
        refuse each other station-side, so a fresh announcement pushes the recording
        back once instead of losing both. The reverse race (a manual unlock landing
        mid-recording) costs only that announce - the door itself still opens."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode["closed"] or episode.get("clip_filename"):
            return
        try:
            door = episode["door"]
            camera = self.clip_cameras.get(door)
            if not camera:
                return
            last_spoken = self._last_announce_at.get(door)
            if (last_spoken is not None and not kwargs.get("retried")
                    and (self.get_now() - last_spoken).total_seconds() < 8):
                self.run_in(self._start_clip, 5, episode_id=episode["id"], retried=True)
                return
            stamp = episode["started_at"].astimezone().strftime("%Y%m%d_%H%M%S")
            filename = f"abb_clip_{stamp}_{door_slug(door, episode['station_id'])}.mp4"
            arm = {"duration": self.clip_seconds + 25}
            station = episode["station_id"] or self.station_by_door.get(door)
            if station:
                # Restrict the armed window to this station so a HomeKit/Scrypted
                # probe cannot ride it into a call at the other door (schema warning).
                arm["station_id"] = station
            self.call_service("abb_welcome/arm_streaming", **arm)
            self.call_service(
                "camera/record",
                entity_id=camera,
                filename=f"{self.clip_record_dir}/{filename}",
                duration=self.clip_seconds,
                lookback=0,
            )
            episode["clip_filename"] = filename
            self.log(f"CLIP-START door={door} file={filename} ({self.clip_seconds}s)")
        except Exception as e:
            self.log(f"Clip start failed for {episode.get('id')}: {e}", level="WARNING")

    def _send_missed_push(self, door, station_id, ring_at):
        label = door or (f"station {station_id}" if station_id else "door")
        message = f"Someone rang the {label} and nobody answered."
        attachment = self.ring_attachment_data()
        if self.mobile_notifier:
            try:
                self.create_task(self.mobile_notifier.notify(
                    title="Missed doorbell ring",
                    message=message,
                    target=self.notify_target,
                    data=attachment,
                ))
            except Exception as e:
                self.log(f"Missed-call push failed: {e}", level="WARNING")
        else:
            self.log(f"Missed-call push skipped (no notifier): {message}", level="WARNING")
        # House feed - cosmetic, guarded, never blocks anything (intercom.py contract)
        try:
            self.fire_event(
                "house_events_report",
                cause=f"Someone rang the {label}",
                effect="Nobody answered - sent a snapshot to everyone home",
                icon="mdi:bell-off",
            )
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="DEBUG")

    # ------------------------------------------------------------------
    # Snapshot capture + archive
    # ------------------------------------------------------------------
    def _on_image_update(self, entity, attribute, old, new, kwargs):
        """The image entity updated - if a ring episode is open and the frame is
        fresh (captured at/after the ring), capture it. This is the NORMAL path:
        the gateway's screenshot rides the 30 s cloud poll, 7-32 s behind the ring."""
        try:
            self._try_capture("image-update")
        except Exception as e:
            self.log(f"Image-update capture failed: {e}", level="WARNING")

    def _probe_snapshot(self, kwargs):
        """Early probe a few seconds after the ring (knob snapshot_probe_s): only
        useful when the poll happened to land already; freshness gating makes a
        stale hit harmless."""
        try:
            self._try_capture("probe")
        except Exception as e:
            self.log(f"Snapshot probe failed: {e}", level="WARNING")

    def _try_capture(self, reason):
        open_eps = [e for e in self.episodes.values() if not e["closed"]]
        if not open_eps:
            return
        try:
            full = self.get_state(self.abb_image_entity, attribute="all") or {}
        except Exception as e:
            self.log(f"Image entity read failed: {e}", level="DEBUG")
            return
        attrs = full.get("attributes", {}) if isinstance(full, dict) else {}
        captured_at = parse_iso_ts(attrs.get("captured_at"))
        event_id = attrs.get("event_id") or ""
        if captured_at is None:
            return
        # Fresh = captured at/after an episode's ring (3 s clock slack). A stale
        # frame is the PREVIOUS visitor - never archived: a missing photo is
        # honest, the wrong person is misleading. When several episodes are open
        # (both doors rang within episode_close_s), the frame belongs to the MOST
        # RECENT ring at/before the capture - the gateway shoots 1-3 s after the
        # ring, so that is the ring that produced it.
        eligible = [e for e in open_eps
                    if captured_at >= e["started_at"] - timedelta(seconds=3)]
        if not eligible:
            return
        episode = max(eligible, key=lambda e: e["started_at"])
        snap = episode["snapshot"]
        if snap is not None and (snap["event_id"] == event_id or snap["captured_at"] >= captured_at):
            return
        # Fetch off-thread: the HTTP read may take seconds and this app's
        # pinned thread must stay free for ring callbacks.
        try:
            self.submit_to_executor(self._capture_for_episode, episode["id"], event_id, captured_at.isoformat())
        except Exception as e:
            self.log(f"Snapshot fetch dispatch failed ({reason}): {e}", level="WARNING")

    def _capture_for_episode(self, episode_id, event_id, captured_iso):
        try:
            raw = self._fetch_image_bytes()
            if not raw:
                return
            episode = self.episodes.get(episode_id)
            if episode is None or episode["closed"]:
                return
            # Keep the NEWEST fresh frame: a later screenshot in the same episode
            # (re-ring) replaces the earlier - the visitor at the door last wins.
            episode["snapshot"] = {
                "bytes": raw,
                "captured_at": parse_iso_ts(captured_iso),
                "event_id": event_id,
            }
            self.log(f"Snapshot captured for episode {episode_id} ({len(raw)} bytes, event {event_id})", level="INFO")
        except Exception as e:
            self.log(f"Snapshot capture failed for {episode_id}: {e}", level="WARNING")

    def _fetch_image_bytes(self):
        """GET the image entity's bytes over the HA API (executor thread only).

        Route verified live 2026-08-12: /api/image_proxy/<entity>?token=<the
        entity's access_token attribute> serves the JPEG with no other auth
        (HTTP 200, image/jpeg). The access token is read fresh each fetch (HA
        rotates it) and never logged. No Authorization header is needed, so no
        long-lived secret is handled here at all.
        """
        try:
            token = self.get_state(self.abb_image_entity, attribute="access_token")
        except Exception:
            token = None
        if not token:
            return None
        url = f"http://localhost:8123/api/image_proxy/{self.abb_image_entity}?token={token}"
        try:
            with urllib.request.urlopen(url, timeout=10) as resp:
                raw = resp.read(MAX_IMAGE_BYTES + 1)
        except (urllib.error.URLError, OSError, ValueError) as e:
            self.log(f"Image fetch failed: {e}", level="WARNING")
            return None
        if len(raw) > MAX_IMAGE_BYTES or not raw.startswith(JPEG_MAGIC):
            self.log(f"Image fetch rejected (size={len(raw)}, jpeg={raw[:2] == JPEG_MAGIC})", level="WARNING")
            return None
        return raw

    def _close_episode(self, kwargs):
        """episode_close_s after the ring: classification is final (unlock evidence
        by +9 s, missed/answered by ~+35 s worst-case poll) - write the archive
        entry once, then drop the episode."""
        episode = self.episodes.pop(kwargs.get("episode_id"), None)
        if episode is None or episode["closed"]:
            return
        episode["closed"] = True
        try:
            unlock_at = self._unlock_near(episode["door"], episode["started_at"])
            event_type = classify_episode(unlock_at is not None,
                                          episode["missed_event"] is not None,
                                          episode["answered"])
            self.log(
                f"EPISODE-CLOSE id={episode['id']} door={episode['door'] or '?'} type={event_type} "
                f"esp={episode['esp_at'] is not None} abb={episode['abb_at'] is not None} "
                f"snapshot={'yes' if episode['snapshot'] else 'no'} re_rings={episode['re_rings']}",
                level="INFO",
            )
            if episode["snapshot"]:
                self.submit_to_executor(
                    self._archive_write,
                    episode["snapshot"]["bytes"],
                    episode["started_at"].isoformat(),
                    episode["station_id"],
                    episode["door"],
                    event_type,
                    episode.get("clip_filename"),
                )
            else:
                self.log(f"EPISODE-CLOSE id={episode['id']}: no fresh snapshot arrived - nothing archived", level="WARNING")
        except Exception as e:
            self.log(f"Episode close failed for {episode.get('id')}: {e}", level="WARNING")

    def _archive_standalone_missed(self, anchor, station_id):
        """Missed call with no observed ring episode: archive the current frame if
        it plausibly belongs to this miss (captured within 5 min)."""
        try:
            captured_at = parse_iso_ts(self.get_state(self.abb_image_entity, attribute="captured_at"))
            if captured_at is None or abs((captured_at - anchor).total_seconds()) > 300:
                self.log("Standalone missed call: no plausibly-fresh snapshot - nothing archived", level="INFO")
                return
            self.submit_to_executor(self._fetch_and_archive_standalone, anchor.isoformat(), station_id)
        except Exception as e:
            self.log(f"Standalone missed archive failed: {e}", level="WARNING")

    def _fetch_and_archive_standalone(self, anchor_iso, station_id):
        try:
            raw = self._fetch_image_bytes()
            if raw:
                self._archive_write(raw, anchor_iso, station_id,
                                    self.station_doors.get(station_id), "call-missed")
        except Exception as e:
            self.log(f"Standalone missed fetch failed: {e}", level="WARNING")

    # --- disk layer (executor thread; blocking IO stays off the callback threads) ---
    def _archive_write(self, raw, ring_iso, station_id, door, event_type, clip_filename=None):
        try:
            ring_at = parse_iso_ts(ring_iso) or datetime.now(timezone.utc)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = ring_at.astimezone().strftime("%Y%m%d_%H%M%S")
            filename = f"abb_ring_{stamp}_{door_slug(door, station_id)}_{event_type}.jpg"
            path = self.archive_dir / filename
            tmp = path.with_name(path.name + ".tmp")
            tmp.write_bytes(raw)
            os.replace(tmp, path)
            entry = build_index_entry(ring_at, station_id, door, event_type,
                                      filename, self.archive_url_prefix)
            if clip_filename:
                # The recording was requested at ring+clip_delay_s and runs clip_seconds;
                # episode close (75 s) is long after HA finalized the mp4, so a single
                # stat decides. A missing or empty file means the record failed
                # (e.g. the station delivered no video, seen live 2026-08-13) - the
                # entry stays photo-only and any 0-byte turd is removed.
                clip_path = self.archive_dir / clip_filename
                try:
                    clip_ok = clip_path.stat().st_size > 0
                except OSError:
                    clip_ok = False
                if clip_ok:
                    entry["clip_filename"] = clip_filename
                    entry["clip_url"] = f"{self.archive_url_prefix.rstrip('/')}/{clip_filename}"
                else:
                    clip_path.unlink(missing_ok=True)
                    self.log(f"Clip {clip_filename} never landed - photo-only entry", level="INFO")
            self._update_index(entry)
            self.log(f"ARCHIVE saved {filename} ({len(raw)} bytes)"
                     + (f" + clip {clip_filename}" if entry.get("clip_url") else ""), level="INFO")
        except Exception as e:
            self.log(f"Archive write failed: {e}", level="WARNING")

    def _load_index(self):
        index_file = self.archive_dir / "index.json"
        try:
            data = json.loads(index_file.read_text())
            images = data.get("images")
            if isinstance(images, list):
                return [i for i in images if isinstance(i, dict)]
        except FileNotFoundError:
            pass
        except Exception as e:
            self.log(f"Archive index unreadable, starting fresh: {e}", level="WARNING")
        return []

    def _update_index(self, entry):
        """Read-modify-write of index.json + retention pruning. Atomic (tmp +
        os.replace) because the dashboard may fetch mid-write - same contract as
        forecast_log/rober2 maps. Executor thread only."""
        images = [i for i in self._load_index() if i.get("filename") != entry["filename"]]
        images.insert(0, entry)
        images.sort(key=lambda i: i.get("ts", 0), reverse=True)
        now_ts = datetime.now(timezone.utc).timestamp()
        keep, drop = prune_images(images, now_ts, self.retain_days, self.max_files)
        for stale in drop:
            try:
                (self.archive_dir / str(stale.get("filename", ""))).unlink(missing_ok=True)
                if stale.get("clip_filename"):
                    (self.archive_dir / str(stale["clip_filename"])).unlink(missing_ok=True)
            except Exception as e:
                self.log(f"Prune failed for {stale.get('filename')}: {e}", level="DEBUG")
        index_file = self.archive_dir / "index.json"
        tmp = index_file.with_name(index_file.name + ".tmp")
        payload = {
            "version": INDEX_VERSION,
            "updated": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "images": keep,
        }
        tmp.write_text(json.dumps(payload))
        os.replace(tmp, index_file)
        self._check_archive_size()

    def _check_archive_size(self):
        try:
            total = sum(f.stat().st_size for f in self.archive_dir.glob("abb_*")
                        if f.suffix in (".jpg", ".mp4"))
            if total > self.archive_warn_mb * 1024 * 1024:
                self.log(
                    f"Doorbell archive is {total / 1024 / 1024:.0f} MB "
                    f"(warn cap {self.archive_warn_mb} MB) - lower retain_days/max_files",
                    level="WARNING",
                )
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Counter persistence (restarts must not zero the phase-2 evidence)
    # ------------------------------------------------------------------
    def _load_state(self):
        try:
            return json.loads(self._state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            self.log(f"bridge state load failed: {e}", level="WARNING")
            return {}

    def _save_state(self):
        try:
            tmp = self._state_file.with_name(self._state_file.name + ".tmp")
            tmp.write_text(json.dumps({
                "counters": self.counters,
                "lag": self.lag_stats,
                "processed_missed_ids": self.processed_missed_ids,
                "station_door_matrix": self.station_door_matrix,
            }))
            os.replace(tmp, self._state_file)
        except Exception as e:
            self.log(f"bridge state save failed: {e}", level="WARNING")
