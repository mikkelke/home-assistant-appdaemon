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
EVENT_DOOR_OPEN = "door-open"

INDEX_VERSION = 1
JPEG_MAGIC = b"\xff\xd8"
MAX_IMAGE_BYTES = 5 * 1024 * 1024  # sanity cap on a single doorbell frame
MAX_PROCESSED_IDS = 50  # missed-call event_id dedupe ring buffer
UNLOCK_EVENT_TTL_S = 600  # prune bound for stored unlock timestamps, and the sanity bound on a last_changed reading in _unlock_edge_at (get_state can serve a stale value - see the appdaemon-stale-state-store gotcha)
UNLOCK_EVENT_MAX_PER_DOOR = 20  # cap so a long uptime with many rings/re-rings cannot grow a door's unlock list unbounded


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
    as a local-time ISO string (human-facing, like the archive filenames).
    filename may be "" for a clip-only entry (ring whose snapshot never arrived
    but whose recording did): url must then be "" too, never a bare directory
    URL the dashboard would try to <img> render."""
    return {
        "ts": int(ring_at.timestamp()),
        "datetime": ring_at.astimezone().isoformat(timespec="seconds"),
        "station": station_id or "",
        "door": door or "",
        "event_type": event_type,
        "filename": filename,
        "url": f"{url_prefix.rstrip('/')}/{filename}" if filename else "",
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
        # TTS engine for speaking THROUGH a live recording (media-source URL fed to
        # abb_welcome/play_audio, which injects into the open stream's talkback leg
        # instead of dialing its own call). Empty disables in-recording voices.
        self.voice_tts_entity = str(self.args.get("voice_tts_entity", "tts.piper"))
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
        self.clip_seconds = int(self.args.get("clip_seconds", 15))
        # Record AT the ring (user 2026-08-19): the dial goes out immediately for
        # every clip door (native_ring_clips=false only - see _open_episode), and
        # all voices yield to it (_recording_in_flight). The station's own ~5 s
        # ring call may refuse the first dial - a refusal is silent (camera.record
        # succeeds as a command, the stream never opens), so _confirm_clip_recording
        # checks the camera entered "recording" at +10 s (anchored to the dial, not
        # to the now-offloaded call returning - see _start_clip) and redials once.
        # Worst case the clip starts ~+11-12 s; best case ~+1-2 (dial + stream-open
        # is the physical floor - there is no pre-ring buffer).
        self.clip_delay_s = int(self.args.get("clip_delay_s", 0))

        self.clip_record_dir = str(self.args.get("clip_record_dir", "/config/www/abb_doorbell")).rstrip("/")
        self.station_by_door = {v: k for k, v in self.station_doors.items()}

        # Native ring-clip recorder (2026-08-24, integration-side change landing
        # alongside this one - see abb_welcome_ring_clip below). true = the
        # integration itself records the ring call and fires that event once the
        # mp4 lands; _start_clip is never scheduled and every native-only branch
        # below activates instead. false (the default when this key is absent, so
        # a code deploy that lands ahead of the integration change is a no-op) =
        # the app behaves EXACTLY as it does today.
        self.native_ring_clips = bool(self.args.get("native_ring_clips", False))

        # --- integration health watchdog (2026-08-13, found live during the feature
        # audit: stream workers leaked by failed camera.record attempts retried 404s
        # for 90 minutes and flipped the front camera unavailable; one
        # homeassistant.reload_config_entry cured it). SIP listener not "registered"
        # or a policy camera unavailable, sustained health_unhealthy_s, means ABB is
        # deaf while the ESP still hears - exactly the silent divergence the
        # comparator cannot afford. Self-heal by reloading the config entry (LockHealth
        # pattern: act, verify, page), at most once per health_heal_cooldown_s, with a
        # push to Mikkel on every heal attempt and on recovery. The ESP doorbell is
        # untouched by all of this - reloading ABB can never cost a ring.
        self.health_sip_entity = self.args.get(
            "health_sip_entity", "sensor.abb_welcome_gateway_sip_listener"
        )
        self.health_unhealthy_s = int(self.args.get("health_unhealthy_s", 600))
        self.health_heal_cooldown_s = int(self.args.get("health_heal_cooldown_s", 3600))
        raw_health_notify = self.args.get("health_notify", ["mikkel"])
        self.health_notify = list(raw_health_notify) if isinstance(raw_health_notify, (list, tuple)) else [str(raw_health_notify)]
        self._health_bad_since = None
        self._health_last_heal = None
        self._health_healing = False
        # Our own outbound SIP activity, seen back through ABB's own reporting, must
        # never count as visitor evidence. Two windows guard that:
        # - _self_call_until[door]: an announce or clip record is dialing that door's
        #   station; the portal logs OUR call as call-answered (gateway answers
        #   instantly), which would flip an ignored ring from missed to answered.
        # - _ignore_abb_rings_until: a watchdog config-entry reload bounces the
        #   ringing sensor into one phantom, unattributed ring (seen live 08:46:01
        #   today: door=? station=? -> rings_abb_only polluted by the heal itself).
        self._self_call_until = {}
        self._ignore_abb_rings_until = None

        # --- auto-open-OFF ring fallback (2026-08-13, user: "The notification if auto
        # is not on should be with open or reject" + door voices for wait/no-answer/
        # reject). Policy: with auto-open on, none of this runs - the door opens and
        # announces as before. With auto-open off, the first ring of an episode sends
        # everyone home an actionable push (Open / Reject), the door hears a short
        # acknowledgment, and whichever comes first wins: Open unlocks via the ESP
        # lock (the SAME path as the dashboard's door buttons - never the ABB button
        # entities), Reject speaks reject_message, silence for no_answer_after_s
        # speaks no_answer_message. Any voice knob set to "" disables that voice.
        self.ring_ack_message = str(self.args.get("ring_ack_message", "One moment, please."))
        self.no_answer_message = str(self.args.get(
            "no_answer_message", "Sorry, no one can answer the door right now."))
        self.reject_message = str(self.args.get(
            "reject_message", "Sorry, we cannot open the door right now."))
        self.no_answer_after_s = int(self.args.get("no_answer_after_s", 45))
        self.ring_action_window_s = int(self.args.get("ring_action_window_s", 180))
        self.lock_by_door = {v: k for k, v in self.esp_lock_doors.items()}
        self._pending_actions = {}  # action id -> shared entry (open+reject ids share one)

        # --- archive knobs ---
        self.archive_dir = Path(self.args.get("archive_dir", "/www/abb_doorbell"))
        self.archive_url_prefix = self.args.get("archive_url_prefix", "/local/abb_doorbell")
        self.retain_days = int(self.args.get("retain_days", 90))
        self.max_files = int(self.args.get("max_files", 2000))
        self.archive_warn_mb = int(self.args.get("archive_warn_mb", 500))

        # --- state ---
        self.episodes = {}  # stable episode id -> episode dict (open episodes only)
        self._episode_seq = 0
        self.unlock_events = {}  # door label -> list[datetime] of recent unlocking/unlocked edges (pruned/capped in _remember_unlock)
        self._last_announce_at = {}  # door label -> datetime of last spoken announcement
        self.mobile_notifier = self._get_mobile_notifier()
        self._state_file = Path(__file__).with_name("abb_welcome_bridge_state.json")
        persisted = self._load_state()
        self.counters = persisted.get("counters", {"rings_both": 0, "rings_esp_only": 0, "rings_abb_only": 0})
        # Door-open comparator keys (2026-08-13) - older state files predate them.
        for key in ("door_opens_both", "door_opens_abb_only"):
            self.counters.setdefault(key, 0)
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
            # Fired by the integration's native ring-clip recorder once its mp4 is
            # finalized (native_ring_clips knob; see _on_native_ring_clip). Always
            # registered - the handler itself no-ops when the knob is off, so a
            # stray event before the flag flips can never touch episode state.
            self.listen_event(self._on_native_ring_clip, "abb_welcome_ring_clip")
        except Exception as e:
            self.log(f"Native ring-clip listener failed: {e}", level="WARNING")
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
            self.listen_event(self._on_notification_action, "mobile_app_notification_action")
        except Exception as e:
            self.log(f"Notification action listener failed: {e}", level="WARNING")

        try:
            self.archive_dir.mkdir(parents=True, exist_ok=True)
        except Exception as e:
            self.log(f"Archive dir {self.archive_dir} unavailable: {e} - archiving disabled until it appears", level="WARNING")

        # Health watchdog tick. Minutely like the other reconcile ticks; the state
        # machine inside is cheap (two get_states and a couple of datetimes).
        try:
            self.run_every(self._health_tick, "now+90", 60)
        except Exception as e:
            self.log(f"Health tick failed to schedule: {e}", level="WARNING")

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
        if (side == "abb" and not door and not station_id
                and self._ignore_abb_rings_until is not None
                and at <= self._ignore_abb_rings_until):
            # A watchdog reload bounces the ringing sensor into exactly this shape:
            # unattributed (door=? station=?), moments after the reload. A REAL ring
            # in the grace window still counts - the bus event carries its station
            # and the ESP side is untouched by the reload.
            self.log(f"Ignoring unattributed ABB ring during post-reload grace ({source})", level="INFO")
            return
        side_key = f"{side}_at"
        episode = self._match_episode(side_key, door, at)
        if episode is None:
            episode = self._open_episode(door, station_id, at)
            self._maybe_ring_fallback(episode)
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
            "clip_filename": None,  # set by _start_clip (or _on_native_ring_clip) once a clip exists
            "clip_started_at": None,  # when the record dial went out (voices yield until it ends)
            "voice_spoken": False,  # one "the door is open" sentence per episode (native + legacy dedup)
            "voice_dispatched": False,  # a native voice retry chain already started (2nd unlock confirm must not start another)
            "action_push_sent": False,  # auto-open-off Open/Reject push went out
            "rejected": False,  # a human pressed Reject on that push
            "no_answer_spoken": False,  # the door already got the no-answer message
            "closed": False,
        }
        self.episodes[episode_id] = episode
        try:
            self.run_in(self._pair_check, self.pair_window_s, episode_id=episode_id)
            self.run_in(self._probe_snapshot, self.snapshot_probe_s, episode_id=episode_id)
            self.run_in(self._close_episode, self.episode_close_s, episode_id=episode_id)
            if self.native_ring_clips and self.clip_cameras.get(door):
                # The integration's own recorder handles the ring clip end-to-end
                # (see _on_native_ring_clip) - this bridge places no dial of its
                # own. It DOES place a continuation dial ~2.5 s after the ring call
                # ends (to film the visitor entering), which the ABB portal logs as
                # call-answered; open the self-call window NOW, at the ring, since
                # (unlike the fallback below) there is no later dial of ours to
                # anchor it to.
                self._self_call_until[door] = at + timedelta(seconds=45)
            elif self.clip_seconds > 0 and self.clip_cameras.get(door):
                # RECORD AT THE RING (user 2026-08-19: "as soon as someone rings we
                # start the recording - we want to look at who they are no matter
                # what path we continue in"). Video outranks the voice: while a
                # recording is in flight the announce/ack are SKIPPED (they share
                # the station's single call slot, and speaking over a recording
                # kills its video - probed 2026-08-13). The station may refuse a
                # record dial while its own ring call is still active, so
                # _confirm_clip_recording checks the camera actually entered
                # "recording" and retries once if not.
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
                    "door_opens_both": self.counters.get("door_opens_both", 0),
                    "door_opens_abb_only": self.counters.get("door_opens_abb_only", 0),
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
                at = parse_iso_ts(attrs.get("timestamp")) or self.get_now()
                episode = self._episode_near(at)
                if episode:
                    # Our own dials (announce TTS, clip record) also show up here as
                    # call-answered - the gateway answers us instantly. Counting that
                    # would flip an ignored ring from missed to answered and swallow
                    # the missed push, so evidence inside a self-call window is ours.
                    until = self._self_call_until.get(episode["door"])
                    if until is not None and at <= until:
                        self.log(f"Ignoring call-answered from our own dial ({episode['door']})", level="INFO")
                        return
                    episode["answered"] = True
            elif event_type == EVENT_DOOR_OPEN:
                # Door-open comparator: does ABB's portal see the openings the ESP
                # performs? The ESP unlock always precedes the portal report (it IS
                # the opener), so pairing against recent unlock evidence at arrival
                # time is sound. Phase-2 evidence alongside the ring comparator.
                at = parse_iso_ts(attrs.get("timestamp")) or self.get_now()
                paired = any(abs((at - t).total_seconds()) <= 45
                             for events in self.unlock_events.values() for t in events)
                self.counters["door_opens_both" if paired else "door_opens_abb_only"] += 1
                self._save_state()
                self._publish_agreement()
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
            if episode is not None and (episode.get("rejected") or episode.get("action_push_sent")):
                # The Open/Reject push already put this ring in front of everyone
                # home - a second "missed" push would be noise, and after a Reject
                # it would be plain wrong (the house DID answer, with a no).
                self.log(
                    f"MISSED-SUPPRESSED door={door or '?'} - handled by the ring push "
                    f"(rejected={episode.get('rejected')})", level="INFO",
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
        """Unlock evidence within +/- suppress_window_s of the ring, CLOSEST to
        the ring itself (not merely the first match); a door label narrows it to
        that door's lock, otherwise any lock counts. Scans every stored
        timestamp: a visitor who rings and is buzzed at repeatedly can leave
        several in-window unlocks followed by a later, out-of-window one (real
        case, 2026-08-24 18:11, 5 rings: unlocks at ring+6.1/+20.5/+23.7/+31.8 s)
        - remembering only the newest per door used to make that a false
        "ring_missed" (and a false missed-call push, via _decide_missed)."""
        best, best_age = None, None
        for lock_door, events in self.unlock_events.items():
            if door and lock_door != door:
                continue
            for at in events:
                age = abs((at - ring_at).total_seconds())
                if age <= self.suppress_window_s and (best_age is None or age < best_age):
                    best, best_age = at, age
        return best

    def _unlock_edge_at(self, entity):
        """Best timestamp for an unlock edge: the lock's OWN last_changed when
        it is sane, else our processing time (get_now()).

        last_changed is preferred because the callback itself can run late -
        measured live 2026-08-24: the pinned thread was blocked ~29 s behind a
        camera.record dial, so a real unlock at ring+4.9 s got stamped at
        ring+31.1 s (processing time), landing just outside suppress_window_s
        and archiving a ring that auto-opened fine as "ring_missed". But
        AppDaemon 4.5.13's get_state can itself serve a stale cached value (see
        the appdaemon-stale-state-store gotcha), so last_changed is trusted only
        when it parses, is not in the future, and is no more than
        UNLOCK_EVENT_TTL_S old - any other outcome, including an exception from
        the lookup itself, falls back to get_now() and never breaks the unlock
        path."""
        now = self.get_now()
        try:
            changed = parse_iso_ts(self.get_state(entity, attribute="last_changed"))
            if changed is not None:
                age = (now - changed).total_seconds()
                if 0 <= age <= UNLOCK_EVENT_TTL_S:
                    return changed
        except Exception as e:
            self.log(f"last_changed lookup failed for {entity}: {e}", level="DEBUG")
        return now

    def _remember_unlock(self, door, entity):
        """Append an unlock edge for `door` and prune the list: drop anything
        older than UNLOCK_EVENT_TTL_S and cap it at UNLOCK_EVENT_MAX_PER_DOOR so
        a long uptime with many rings can never grow this unbounded. A LIST, not
        a single timestamp - see _unlock_near's docstring for why one timestamp
        per door used to lose real in-window unlocks."""
        at = self._unlock_edge_at(entity)
        events = self.unlock_events.setdefault(door, [])
        events.append(at)
        cutoff = self.get_now() - timedelta(seconds=UNLOCK_EVENT_TTL_S)
        events[:] = [t for t in events if t >= cutoff][-UNLOCK_EVENT_MAX_PER_DOOR:]

    def _on_lock_activity(self, entity, attribute, old, new, kwargs):
        try:
            if new in ("unlocking", "unlocked"):
                door = self.esp_lock_doors.get(entity, entity)
                self._remember_unlock(door, entity)
                # The door opened by SOME path (auto-open, dashboard, push action):
                # a still-pending Open/Reject push is resolved - kill its buttons
                # everywhere so a late press cannot buzz the door a second time.
                stale = [aid for aid, e in self._pending_actions.items() if e["door"] == door]
                for aid in stale:
                    self._pending_actions.pop(aid, None)
                if stale:
                    self._clear_ring_push(door)
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
        sentence, never the unlock: this runs strictly after the lock command.

        native_ring_clips=true takes a wholly different branch below: it never places
        the temporary-call dial (announce OR the in-recording play_audio single-shot) -
        see _native_voice_retry."""
        try:
            camera = self.announce_cameras.get(door)
            if not camera:
                return
            now = self.get_now()
            if self.native_ring_clips:
                # The integration's own recorder owns the station's one call slot
                # from the ring call through its continuation dial - a second dial
                # here (the temporary-call announce) would collide with either, so
                # it is never placed while a ring for this door is still open.
                # Instead play_audio is retried into whichever of those two calls
                # happens to be open (_native_voice_retry). voice_spoken/
                # voice_dispatched (set on the episode) keep this to one spoken
                # sentence even though two unlock attempts can both confirm and
                # call this method for the same ring.
                episode = self._latest_open_episode(door, now, self.announce_ring_window_s)
                if episode is None:
                    return
                self.run_in(self._native_voice_retry, 0, episode_id=episode["id"], door=door,
                            camera=camera, message=self.announce_message, attempt=1)
                return
            # Video first (user 2026-08-19): a recording in flight owns the station's
            # one call slot - speaking now would fail AND kill the clip's video, so
            # the sentence is skipped for this ring. The visitor was buzzed in; the
            # clip of who they are matters more than telling them so.
            if self._recording_in_flight(door, now):
                # Speak THROUGH the recording's own call: play_audio injects PCM into
                # the open stream's talkback leg (integration requires talkback_ready,
                # i.e. exactly this situation), so voice and video run together (user
                # 2026-08-20: "so we cannot run and send a message voice/sound?" - we
                # can, this way). Best-effort: any failure is a log line and the ring
                # keeps its video; never fall back to the temporary-call announce here,
                # THAT is the collision that kills the clip. voice_spoken caps it at
                # one sentence per episode - two unlock attempts can both confirm and
                # land here (2026-08-24: that used to double-fire, since this branch
                # alone skipped the cooldown check below).
                episode = self._latest_open_episode(door, now)
                if episode is not None and episode.get("voice_spoken"):
                    return
                if self._voice_into_recording(door, camera, self.announce_message) and episode is not None:
                    episode["voice_spoken"] = True
                self._last_announce_at[door] = now
                return
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
            self._self_call_until[door] = now + timedelta(seconds=20)
            self.call_service(
                "abb_welcome/announce",
                entity_id=camera,
                message=self.announce_message,
            )
            self.log(f"ANNOUNCE door={door} camera={camera} message={self.announce_message!r}")
        except Exception as e:
            self.log(f"Announce failed for {door}: {e}", level="WARNING")

    def _recording_in_flight(self, door, now):
        """True while a ring episode for this door has a recording underway - from the
        camera.record dial until clip_seconds (+ a teardown margin) have passed. Used
        to make every voice yield to the video: the station has ONE call slot, and the
        clip wins it (user 2026-08-19).

        native_ring_clips: the integration answers and records the ring call from
        ~ring+0.3 s, and may hold a continuation dial after it - but this bridge only
        learns of any of that when the finished mp4's event lands, so clip_started_at
        is still None while the recording is actually live. Count the window as busy
        from the RING itself then, or _door_voice's ack at +2 s would dial a second
        call straight into the recorder's."""
        margin = 5
        for ep in self.episodes.values():
            if ep.get("door") != door or ep.get("closed"):
                continue
            started = ep.get("clip_started_at")
            if started is None and self.native_ring_clips and self.clip_cameras.get(door):
                started = ep.get("started_at")
            if started is not None and (now - started).total_seconds() < self.clip_seconds + margin:
                return True
        return False

    def _voice_into_recording(self, door, camera, message):
        """Inject a spoken sentence into the LIVE recording call via play_audio's
        talkback leg. The media id is HA's TTS media-source URL; the integration
        resolves it to PCM and writes it into the already-open stream, so no second
        SIP call exists and the clip's video survives. Failure costs the sentence
        only - logged, never raised, and never retried with the temporary-call
        announce (that second call is what kills the video). Returns True/False
        (2026-08-24: the native retry path needs to know whether to try again -
        see _native_voice_retry)."""
        if not self.voice_tts_entity:
            return False
        try:
            from urllib.parse import quote
            media_id = f"media-source://tts/{self.voice_tts_entity}?message={quote(message)}"
            self.call_service(
                "abb_welcome/play_audio",
                entity_id=camera,
                media={"media_content_id": media_id},
            )
            self.log(f"VOICE-IN-RECORDING door={door} message={message!r}", level="INFO")
            return True
        except Exception as e:
            self.log(f"Voice-into-recording failed for {door} ({message!r}): {e}", level="INFO")
            return False

    def _start_clip(self, kwargs):
        """FALLBACK PATH ONLY (native_ring_clips=false - see _open_episode, which
        never schedules this at all when the integration's own recorder is doing
        the job). Start recording AT the ring (user 2026-08-19: who they are
        outranks every other path). Pull a short mp4 via HA's native camera.record
        (probe-verified 2026-08-13: h264 640x480, needs arm_streaming first).
        Best-effort by design - the PHOTO is the guaranteed artifact and the clip
        only ever adds; every failure path is a log line, never a lost photo.
        Voices no longer precede the recording - they YIELD to it
        (_recording_in_flight) - so the old announce-clearance defer is gone. The
        station may refuse the record dial while its own ring call is still up:
        _confirm_clip_recording re-checks the camera actually entered "recording"
        and retries once.

        The arm_streaming + camera.record pair is offloaded to the executor
        (2026-08-24, measured live: camera/record is a BLOCKING call_service that
        returns only when the recording itself finishes or fails, ~23-29 s -
        every other ring callback on this pinned thread queued up behind it, and
        _confirm_clip_recording never got a fair look at the camera until the dial
        was long over). Episode/self-call bookkeeping is claimed BEFORE the dial
        goes out - that state stays pinned-thread-owned - and _confirm_clip_recording
        is scheduled right here too, anchored to the DIAL, not to whenever the
        (now backgrounded) calls happen to return."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode["closed"] or episode.get("clip_filename"):
            return
        try:
            door = episode["door"]
            camera = self.clip_cameras.get(door)
            if not camera:
                return
            stamp = episode["started_at"].astimezone().strftime("%Y%m%d_%H%M%S")
            filename = f"abb_clip_{stamp}_{door_slug(door, episode['station_id'])}.mp4"
            arm = {"duration": self.clip_seconds + 25}
            station = episode["station_id"] or self.station_by_door.get(door)
            if station:
                # Restrict the armed window to this station so a HomeKit/Scrypted
                # probe cannot ride it into a call at the other door (schema warning).
                arm["station_id"] = station
            episode["clip_filename"] = filename
            episode["clip_started_at"] = self.get_now()
            self._self_call_until[door] = self.get_now() + timedelta(seconds=self.clip_seconds + 20)
            self.log(f"CLIP-START door={door} file={filename} ({self.clip_seconds}s) {self._ring_rel(episode)}"
                     + ("  [retry]" if kwargs.get("retried") else ""))
            self.submit_to_executor(self._dial_clip, arm, camera, filename)
            if not kwargs.get("retried"):
                self.run_in(self._confirm_clip_recording, 10, episode_id=episode["id"])
        except Exception as e:
            self.log(f"Clip start failed for {episode.get('id')}: {e} {self._ring_rel(episode)}", level="WARNING")

    def _dial_clip(self, arm_kwargs, camera, filename):
        """Executor-thread body of the fallback record dial: arm_streaming then
        camera.record, in that order, exactly as _start_clip always did - just off
        the pinned thread now (see its comment: camera.record blocks ~23-29 s).
        Self-logs any failure, the same pattern intercom.py's sonos_notifier.notify()
        offload uses - once submitted, this runs detached from _start_clip's own
        try/except, so nothing else would ever see or log an exception raised here."""
        try:
            self.call_service("abb_welcome/arm_streaming", **arm_kwargs)
            self.call_service(
                "camera/record",
                entity_id=camera,
                filename=f"{self.clip_record_dir}/{filename}",
                duration=self.clip_seconds,
                lookback=0,
            )
        except Exception as e:
            self.log(f"Clip dial failed for {filename}: {e}", level="WARNING")

    def _confirm_clip_recording(self, kwargs):
        """10 s after the record dial went out (anchored to the dial itself, not to
        when the now-offloaded arm+record calls return - see _start_clip): is the
        camera actually recording? At-the-ring starts race the station's own ring
        call for its single call slot, and a refused dial fails SILENTLY
        (camera.record succeeds as a command; the stream just never opens). The
        camera entity flips to "recording" when the stream is real - if it has
        not, clear the claim and dial once more, now that the ring call has had
        time to clear. One retry only: a second refusal means the station is
        genuinely busy (someone answered the call) and the photo carries the
        episode."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode["closed"] or not episode.get("clip_filename"):
            return
        try:
            camera = self.clip_cameras.get(episode["door"])
            if not camera:
                return
            state = self.get_state(camera)
            if state == "recording":
                return
            self.log(f"CLIP-RETRY door={episode['door']} - camera state {state!r} 10 s "
                     f"after the dial (station busy with its own ring call?) {self._ring_rel(episode)}",
                     level="INFO")
            episode["clip_filename"] = None
            episode["clip_started_at"] = None
            self.run_in(self._start_clip, 1, episode_id=episode["id"], retried=True)
        except Exception as e:
            self.log(f"Clip confirm failed for {episode.get('id')}: {e} {self._ring_rel(episode)}", level="WARNING")

    def _ring_rel(self, episode, at=None):
        """`+X.Xs` since episode["started_at"] (the ring) - cheap ring-relative
        timing for clip-path log lines, since those are exactly what raced the
        blocked-thread bug this whole file of comments is about."""
        try:
            return f"+{((at or self.get_now()) - episode['started_at']).total_seconds():.1f}s"
        except Exception:
            return "+?s"

    def _latest_open_episode(self, door, now=None, window_s=None):
        """Most recently opened, still-OPEN episode for a door - optionally bounded
        to the last window_s of now. Used both by the native ring-clip event
        (any open episode; the mp4 can land up to ~40 s after the ring) and by
        the native voice gate (bounded to announce_ring_window_s, same as the
        legacy ring_recent check it replaces)."""
        candidates = [ep for ep in self.episodes.values()
                      if ep.get("door") == door and not ep.get("closed")]
        if window_s is not None:
            now = now or self.get_now()
            candidates = [ep for ep in candidates
                          if (now - ep["started_at"]).total_seconds() <= window_s]
        if not candidates:
            return None
        return max(candidates, key=lambda e: e["started_at"])

    def _on_native_ring_clip(self, event_name, data, kwargs):
        """native_ring_clips=true: the integration's own ring-clip recorder fires
        this once per ring, after its mp4 is finalized - anywhere from ~10 to
        ~40 s after the ring, always before the episode closes (episode_close_s
        defaults to 75 s). Attaches straight onto the matching open episode; the
        existing episode-close/ARCHIVE code (_close_episode/_archive_write) needs
        nothing more than clip_filename set - it already just stats
        self.archive_dir/clip_filename, and that is the SAME directory the
        integration writes to (clip_record_dir/archive_dir are two container
        mount aliases for one host path, exactly as they already are for the
        fallback dial's own file)."""
        try:
            if not self.native_ring_clips:
                return  # defense in depth: a stray event must never touch state when off
            data = data or {}
            if data.get("reason") != "ring":
                return  # a "service" clip (manual arm/record) is not a ring artifact
            station_id = str(data.get("station_id", "") or "")
            door = self.station_doors.get(station_id)
            episode = self._latest_open_episode(door) if door else None
            ok = bool(data.get("ok"))
            if episode is None or not ok:
                self.log(
                    f"CLIP-NATIVE dropped station={station_id or '?'} door={door or '?'} "
                    f"ok={ok} episode={'none' if episode is None else episode['id']}", level="INFO",
                )
                return
            filename = data.get("filename") or ""
            started_at = parse_iso_ts(data.get("started_at")) or episode["started_at"]
            episode["clip_filename"] = filename
            episode["clip_started_at"] = started_at
            self.log(
                f"CLIP-NATIVE door={door} file={filename} duration={data.get('duration_s')}s "
                f"frames={data.get('frames')} segments={data.get('segments')} {self._ring_rel(episode)}",
                level="INFO",
            )
        except Exception as e:
            self.log(f"Native ring-clip handling failed: {e}", level="WARNING")

    def _native_voice_retry(self, kwargs):
        """native_ring_clips=true voice path: retry play_audio into whichever call
        happens to be open, since this bridge places no dial of its own anymore.
        play_audio only works while a station call with talkback is open; the
        ring call's own media ends ~+3 s after the ring and the integration's
        continuation dial has no media until ~+8 s, so most attempts in between
        fail harmlessly. Attempt 1 is scheduled at delay=0 by _maybe_announce,
        then every 2 s, 5 attempts total, stopping on the first success.

        Each attempt's call_service is offloaded to the executor via
        _native_voice_attempt so it never blocks this pinned thread (same pattern
        as intercom.py's sonos_notifier offload in _handle_trigger). This method
        itself only ever runs ON the pinned thread (it is a run_in callback), so
        the next attempt is always scheduled here, UNCONDITIONALLY - an executor
        Future's result cannot drive scheduling from the worker thread, so every
        attempt just re-checks voice_spoken and no-ops once it is true.

        voice_dispatched is a SEPARATE guard from voice_spoken: a ring episode can
        trigger _maybe_announce more than once (two unlock attempts both
        confirming - the exact double-fire this flag pair exists to close), so
        only the FIRST call may start a chain. voice_spoken alone cannot do that
        job too: it has to stay false through up to 4 failing attempts for the
        retry to have any point."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode["closed"] or episode.get("voice_spoken"):
            return
        attempt = kwargs.get("attempt", 1)
        door = kwargs.get("door")
        if attempt == 1:
            if episode.get("voice_dispatched"):
                return
            episode["voice_dispatched"] = True
        try:
            self.submit_to_executor(self._native_voice_attempt, episode["id"], door,
                                    kwargs.get("camera"), kwargs.get("message"), attempt)
        except Exception as e:
            self.log(f"Native voice dispatch failed for {door} (attempt {attempt}): {e}", level="WARNING")
        if attempt < 5:
            try:
                self.run_in(self._native_voice_retry, 2, episode_id=episode["id"], door=door,
                            camera=kwargs.get("camera"), message=kwargs.get("message"), attempt=attempt + 1)
            except Exception as e:
                self.log(f"Native voice retry scheduling failed for {door}: {e}", level="WARNING")

    def _native_voice_attempt(self, episode_id, door, camera, message, attempt):
        """Executor-thread body of one native-voice attempt (see _native_voice_retry
        for why this is offloaded). On success, flips this episode's voice_spoken
        flag directly - a bool dict-item write, safe cross-thread the same way
        _capture_for_episode's snapshot write already is."""
        if self._voice_into_recording(door, camera, message):
            episode = self.episodes.get(episode_id)
            if episode is not None:
                episode["voice_spoken"] = True
            self.log(f"VOICE-NATIVE door={door} attempt={attempt}/5 succeeded", level="INFO")

    # ------------------------------------------------------------------
    # Auto-open-OFF ring fallback: Open/Reject push + door voices
    # ------------------------------------------------------------------
    def _auto_open_on(self):
        try:
            return self.get_state(self.auto_open_entity) == "on"
        except Exception:
            return False  # unreadable -> treat as off; the push is the safe default

    def _ring_push_tag(self, door):
        return f"abb_ring_{door_slug(door, '')}"

    def _maybe_ring_fallback(self, episode):
        """First ring of a fresh episode with auto-open OFF: everyone home gets an
        actionable push (Open / Reject), the door hears a short acknowledgment, and
        whichever comes first resolves it - Open unlocks via the ESP lock (the same
        path as the dashboard's door buttons, never the ABB button entities), Reject
        speaks reject_message, sustained silence speaks no_answer_message. Voices go
        only to doors in announce_cameras (back door pending its voice verification);
        the push works for both doors either way."""
        try:
            door = episode["door"]
            if not door or self._auto_open_on():
                return
            # Only a DELIVERED push may later suppress the missed-call push: with no
            # notifier the household saw nothing, and the miss must still be a miss.
            episode["action_push_sent"] = bool(self.mobile_notifier)
            entry = {
                "door": door,
                "episode_id": episode["id"],
                "until": episode["started_at"] + timedelta(seconds=self.ring_action_window_s),
                "open_id": f"ABB_RING_OPEN_{episode['id']}",
                "reject_id": f"ABB_RING_REJECT_{episode['id']}",
                "consumed": False,
            }
            self._pending_actions[entry["open_id"]] = entry
            self._pending_actions[entry["reject_id"]] = entry
            if self.mobile_notifier:
                self.create_task(self.mobile_notifier.notify(
                    title=f"Someone is ringing the {door}",
                    message="Auto-open is off - open for them?",
                    target=self.notify_target,
                    data={
                        "tag": self._ring_push_tag(door),
                        "actions": [
                            {"action": entry["open_id"], "title": "Open"},
                            {"action": entry["reject_id"], "title": "Reject"},
                        ],
                    },
                ))
                self.log(f"RING-PUSH door={door} episode={episode['id']} (auto-open off)", level="INFO")
            if self.ring_ack_message and self.announce_cameras.get(door):
                self.run_in(self._ring_ack, 2, episode_id=episode["id"])
            if self.no_answer_message and self.announce_cameras.get(door):
                self.run_in(self._ring_no_answer, self.no_answer_after_s, episode_id=episode["id"])
        except Exception as e:
            self.log(f"Ring fallback failed for {episode.get('id')}: {e}", level="WARNING")

    def _ring_ack(self, kwargs):
        """ring+2s: tell the visitor they were heard while the humans decide.
        Unverified whether the announce dial succeeds while the station is still
        ringing - _door_voice retries once, and by +8 s the ring hold is over."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode["closed"] or episode.get("rejected") or episode["answered"]:
            return
        if self._unlock_near(episode["door"], episode["started_at"]) is not None:
            return  # somebody already buzzed them in; the unlock announce speaks
        if self._auto_open_on():
            return
        self._door_voice(episode["door"], self.ring_ack_message)

    def _ring_no_answer(self, kwargs):
        """no_answer_after_s after the ring: nobody pressed anything, nobody opened,
        nobody picked up - the door says so and the stale push is cleared."""
        episode = self.episodes.get(kwargs.get("episode_id"))
        if episode is None or episode.get("rejected") or episode["answered"]:
            return
        if self._unlock_near(episode["door"], episode["started_at"]) is not None:
            return
        episode["no_answer_spoken"] = True
        self._door_voice(episode["door"], self.no_answer_message)
        self._clear_ring_push(episode["door"])

    def _door_voice(self, door, message, allow_retry=True):
        """Speak at a door's station, best-effort. Sets the self-call window (the
        portal logs our dial as call-answered). One retry, because the still-ringing
        call can hold the station. Yields entirely to a live recording (user
        2026-08-19: video outranks every voice) - speaking over one kills its video.
        The no-answer message at +45 s lands after the clip ends, so it still works."""
        camera = self.announce_cameras.get(door)
        if not camera or not message:
            return
        now = self.get_now()
        if self._recording_in_flight(door, now):
            self._voice_into_recording(door, camera, message)
            self._last_announce_at[door] = now
            return
        self._last_announce_at[door] = now
        self._self_call_until[door] = now + timedelta(seconds=20)
        try:
            self.call_service("abb_welcome/announce", entity_id=camera, message=message)
            self.log(f"DOOR-VOICE door={door} message={message!r}", level="INFO")
        except Exception as e:
            if allow_retry:
                self.log(f"Door voice busy for {door} ({e}) - retrying in 6s", level="INFO")
                try:
                    self.run_in(self._door_voice_retry, 6, door=door, message=message)
                except Exception:
                    pass
            else:
                self.log(f"Door voice failed for {door}: {e}", level="WARNING")

    def _door_voice_retry(self, kwargs):
        self._door_voice(kwargs.get("door"), kwargs.get("message"), allow_retry=False)

    def _on_notification_action(self, event_name, data, kwargs):
        try:
            action = (data or {}).get("action") or ""
            entry = self._pending_actions.get(action)
            if entry is None:
                return
            # One decision per ring: both buttons die together.
            self._pending_actions.pop(entry["open_id"], None)
            self._pending_actions.pop(entry["reject_id"], None)
            door = entry["door"]
            if entry["consumed"] or self.get_now() > entry["until"]:
                self.log(f"RING-ACTION ignored (expired/duplicate) door={door}", level="INFO")
                return
            entry["consumed"] = True
            episode = self.episodes.get(entry["episode_id"])
            if action == entry["open_id"]:
                lock = self.lock_by_door.get(door)
                if lock:
                    self.log(f"RING-ACTION OPEN door={door} via {lock}", level="INFO")
                    self.call_service("lock/unlock", entity_id=lock)
                else:
                    self.log(f"RING-ACTION OPEN door={door}: no ESP lock mapped", level="WARNING")
            else:
                self.log(f"RING-ACTION REJECT door={door}", level="INFO")
                if episode is not None:
                    episode["rejected"] = True
                self._door_voice(door, self.reject_message)
            self._clear_ring_push(door)
        except Exception as e:
            self.log(f"Ring action handling failed: {e}", level="WARNING")

    def _clear_ring_push(self, door):
        """Withdraw the Open/Reject push from every phone once the ring is resolved
        (companion convention: message "clear_notification" + the same tag)."""
        if not self.mobile_notifier or not door:
            return
        try:
            self.create_task(self.mobile_notifier.notify(
                title="clear",
                message="clear_notification",
                target=self.notify_target,
                data={"tag": self._ring_push_tag(door)},
            ))
        except Exception as e:
            self.log(f"Ring push clear failed: {e}", level="DEBUG")

    # ------------------------------------------------------------------
    # Integration health watchdog (self-healing; see the init comment)
    # ------------------------------------------------------------------
    def _health_problems(self):
        """Current list of ABB health complaints - empty means healthy. Only
        EXPLICIT bad states count: an unreadable sensor or a missing entity is
        no-evidence and must not trigger a reload (arbitration rule of the
        house: dropouts hold state, they never act)."""
        problems = []
        if self.health_sip_entity:
            try:
                sip = self.get_state(self.health_sip_entity)
            except Exception:
                sip = None
            if sip is not None and sip not in ("registered", "unknown"):
                problems.append(f"SIP listener {sip!r}")
        for camera in sorted(set(self.announce_cameras.values()) | set(self.clip_cameras.values())):
            try:
                cam_state = self.get_state(camera)
            except Exception:
                cam_state = None
            if cam_state == "unavailable":
                problems.append(f"{camera} unavailable")
        return problems

    def _health_tick(self, kwargs):
        try:
            now = self.get_now()
            problems = self._health_problems()
            if not problems:
                if self._health_bad_since is not None:
                    down_s = int((now - self._health_bad_since).total_seconds())
                    self.log(f"ABB health recovered after {down_s}s", level="INFO")
                    if self._health_healing:
                        self._health_push("ABB intercom recovered",
                                          "The ABB integration is healthy again after the reload.")
                self._health_bad_since = None
                self._health_healing = False
                return
            if self._health_bad_since is None:
                self._health_bad_since = now
                self.log(f"ABB health degraded: {'; '.join(problems)} - "
                         f"reload in {self.health_unhealthy_s}s unless it recovers", level="WARNING")
                return
            if (now - self._health_bad_since).total_seconds() < self.health_unhealthy_s:
                return
            if (self._health_last_heal is not None
                    and (now - self._health_last_heal).total_seconds() < self.health_heal_cooldown_s):
                return  # one heal (and one page) per cooldown while it stays down
            self._health_last_heal = now
            self._health_healing = True
            detail = "; ".join(problems)
            self.log(f"ABB unhealthy for {int((now - self._health_bad_since).total_seconds())}s "
                     f"({detail}) - reloading the config entry", level="WARNING")
            # The reload bounces the ringing sensor into one phantom, unattributed
            # ring (live 08:46:01 today) - grace-drop those so the heal cannot
            # pollute the comparator it exists to protect.
            self._ignore_abb_rings_until = now + timedelta(seconds=90)
            self.call_service("homeassistant/reload_config_entry",
                              entity_id=self.health_sip_entity)
            self._health_push(
                "ABB intercom went deaf",
                f"{detail}. Reloaded the integration; the ESP doorbell is unaffected either way.",
            )
        except Exception as e:
            self.log(f"Health tick failed: {e}", level="WARNING")

    def _health_push(self, title, message):
        if not self.mobile_notifier:
            self.log(f"Health push skipped (no notifier): {message}", level="WARNING")
            return
        try:
            self.create_task(self.mobile_notifier.notify(
                title=title, message=message, target=self.health_notify,
            ))
        except Exception as e:
            self.log(f"Health push failed: {e}", level="WARNING")

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
            elif episode.get("clip_filename"):
                # No snapshot, but a recording was requested: the clip must not die
                # with the photo. The 2026-08-13 13:06 front ring did exactly this -
                # the gateway never produced a screenshot, so the 378 KB clip sat
                # orphaned on disk and the ring never reached the gallery at all.
                self.submit_to_executor(
                    self._archive_write,
                    None,
                    episode["started_at"].isoformat(),
                    episode["station_id"],
                    episode["door"],
                    event_type,
                    episode["clip_filename"],
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
        """raw=None means clip-only: no photo arrived for the episode, so no jpg is
        written and the entry carries filename=""/url="" (the gallery renders a
        video placeholder tile). If the clip then turns out missing/empty too,
        there is nothing to show and no entry is written at all."""
        try:
            ring_at = parse_iso_ts(ring_iso) or datetime.now(timezone.utc)
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            stamp = ring_at.astimezone().strftime("%Y%m%d_%H%M%S")
            filename = ""
            if raw is not None:
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
            if not filename and not entry.get("clip_url"):
                self.log("Neither photo nor clip survived for this ring - nothing archived", level="WARNING")
                return
            self._update_index(entry)
            what = f"{filename} ({len(raw)} bytes)" if filename else f"clip-only {clip_filename}"
            self.log(f"ARCHIVE saved {what}"
                     + (f" + clip {clip_filename}" if filename and entry.get("clip_url") else ""), level="INFO")
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
        forecast_log/rober2 maps. Executor thread only.

        The dedup key is filename-or-clip: a clip-only entry has filename "",
        and deduping on bare filename would make every new clip-only entry
        swallow all previous ones."""
        entry_key = entry.get("filename") or entry.get("clip_filename") or ""
        images = [
            i for i in self._load_index()
            if (i.get("filename") or i.get("clip_filename") or "") != entry_key
        ]
        images.insert(0, entry)
        images.sort(key=lambda i: i.get("ts", 0), reverse=True)
        now_ts = datetime.now(timezone.utc).timestamp()
        keep, drop = prune_images(images, now_ts, self.retain_days, self.max_files)
        for stale in drop:
            try:
                if stale.get("filename"):
                    (self.archive_dir / str(stale["filename"])).unlink(missing_ok=True)
                if stale.get("clip_filename"):
                    (self.archive_dir / str(stale["clip_filename"])).unlink(missing_ok=True)
            except Exception as e:
                self.log(f"Prune failed for {stale.get('filename')}: {e}", level="DEBUG")
        self._sweep_orphans(keep, now_ts)
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

    def _sweep_orphans(self, keep, now_ts):
        """Unlink bridge-named archive files (abb_*.jpg/.mp4) referenced by NO index
        entry once they are a day old. Retention pruning only ever deletes files its
        dropped entries point at, so a file that never got an entry (the pre-fix
        clip-orphan path, a crash between file write and index write) lived forever.
        Only abb_* names are candidates - anything a human parked in the directory
        is not ours to delete - and the 24 h grace covers any in-flight recording.
        Executor thread only (called from _update_index)."""
        try:
            referenced = {i.get("filename") for i in keep} | {i.get("clip_filename") for i in keep}
            for f in self.archive_dir.glob("abb_*"):
                if f.suffix not in (".jpg", ".mp4") or f.name in referenced:
                    continue
                try:
                    if now_ts - f.stat().st_mtime > 86400:
                        f.unlink()
                        self.log(f"Pruned orphan {f.name} (never indexed)", level="INFO")
                except OSError:
                    pass
        except Exception as e:
            self.log(f"Orphan sweep failed: {e}", level="DEBUG")

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
