"""MaTwinWatch - reloads Music Assistant's Sonos provider when its HA twins stop mirroring Sonos.

Each pair maps a raw Sonos player (media_player.<room>_2) to its Music Assistant twin
(media_player.<room>). A pair is mismatched while the raw player is `playing` and the twin
reads anything else except unavailable/unknown: an unavailable twin is a different failure
that a reload makes worse. When one pair has been mismatched without a break for hold_s,
the app asks the host to reload MA's Sonos provider, pushes notify_target once and adds an
admin row to the house timeline. MA queue playback on Sonos stops briefly; that is accepted.

After a reload the app gives up (one push, no more reloads) when either:
  - at verify_s after the reload a playing raw player still has a twin that is not playing
    (unavailable included: MA did not come back), or
  - a pair is mismatched for hold_s again within cooldown_s of the reload.
It re-arms once no playing raw player has a non-playing twin. At most max_per_day reloads
in any 24 h.

Reload mechanism: the app creates request_file (O_EXCL, empty). The host-side systemd path
unit box/systemd/ma-restart.path sees it, and ma-restart.service deletes it and runs
box/systemd/ma-sonos-reload, which calls MA's config/providers/reload for the Sonos provider
and falls back to `docker restart musicassistant`; the host never reads the file or writes
into this tree.
A request still present after pending_s means the host unit is not running: the app deletes
it, pushes the reset command and gives up.

Reload history persists in ma_twin_watch_state.json so the cooldown and daily cap survive
AppDaemon restarts. All timestamps are epoch seconds.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

IGNORED_TWIN_STATES = (None, "unavailable", "unknown")
DAY_S = 86400
RESET_COMMAND = ("sudo systemctl reset-failed ma-restart.service ma-restart.path"
                 " && sudo systemctl start ma-restart.path")


def _ts(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except (TypeError, ValueError):
        return None


def mismatched(raw_state, twin_state):
    """Raw plays while the twin shows another real state."""
    return raw_state == "playing" and twin_state not in IGNORED_TWIN_STATES and twin_state != "playing"


def not_mirrored(raw_state, twin_state):
    """Raw plays and the twin does not, unavailable included (the post-reload check)."""
    return raw_state == "playing" and twin_state != "playing"


def track(since, observations, now):
    """New {raw: mismatch start} from {raw: (raw_state, raw_changed, twin_state, twin_changed)}.

    A pair keeps its earliest start while it stays mismatched, so a twin moving idle -> paused
    does not restart the clock. A new mismatch starts at the later last_changed of the two
    entities (exact after an AppDaemon restart), or now when neither is readable.
    """
    out = {}
    for raw, (raw_state, raw_changed, twin_state, twin_changed) in observations.items():
        if not mismatched(raw_state, twin_state):
            continue
        if raw in since:
            out[raw] = since[raw]
        else:
            changed = [t for t in (_ts(raw_changed), _ts(twin_changed)) if t is not None]
            out[raw] = min(now, max(changed)) if changed else now
    return out


def longest(since, now):
    """(raw, seconds) of the longest current mismatch, or (None, 0)."""
    if not since:
        return None, 0.0
    raw = min(since, key=since.get)
    return raw, now - since[raw]


def decide(state, now, held_s, mirrors, cfg):
    """One of: None, "restart", "persisted", "verify_failed", "verified", "rearm".

    state: {"restarts": [ts...], "gave_up": bool, "verify_at": ts|None}
    held_s: seconds of the longest current mismatch; mirrors: no playing raw has a
    non-playing twin. cfg: hold_s, cooldown_s, max_per_day.
    """
    restarts = state.get("restarts") or []
    last = restarts[-1] if restarts else None
    if state.get("gave_up"):
        return "rearm" if mirrors else None
    verify_at = state.get("verify_at")
    if verify_at is not None and now >= verify_at:
        return "verified" if mirrors else "verify_failed"
    if held_s < cfg["hold_s"]:
        return None
    if last is not None and now - last < cfg["cooldown_s"]:
        return "persisted"
    if sum(1 for t in restarts if now - t < DAY_S) >= cfg["max_per_day"]:
        return None
    return "restart"


class MaTwinWatch(hass.Hass):
    def initialize(self):
        a = self.args
        self.pairs = dict(a["pairs"])
        self.request_file = Path(a.get("request_file", "/conf/host-requests/ma-restart.request"))
        self.notify_target = a.get("notify_target", "mikkel")
        self.cfg = {
            "hold_s": int(a.get("hold_s", 60)),
            "cooldown_s": int(a.get("cooldown_s", 1800)),
            "max_per_day": int(a.get("max_per_day", 3)),
        }
        self.verify_s = int(a.get("verify_s", 600))
        self.pending_s = int(a.get("pending_s", 60))
        self.state_file = Path(a.get("state_file") or Path(__file__).with_name("ma_twin_watch_state.json"))
        self._notifier = self.get_app("MobileNotifier")
        self._since = {}
        self._capped_logged = False
        self.state = self._load()
        self.run_every(self._check, "now+30", int(a.get("check_s", 30)))

        now = time.time()
        recent = sum(1 for t in self.state["restarts"] if now - t < DAY_S)
        if not self.request_file.parent.is_dir():
            self.log(f"MaTwinWatch NOT armed: {self.request_file.parent} is missing (host unit not installed?)",
                     level="WARNING")
        else:
            mode = "gave up, waiting for the twins to mirror" if self.state["gave_up"] else "armed"
            self.log(f"MaTwinWatch {mode}: {len(self.pairs)} pairs, Sonos provider reload after {self.cfg['hold_s']} s "
                     f"mismatch, cooldown {self.cfg['cooldown_s']} s, {recent}/{self.cfg['max_per_day']} "
                     f"reloads in 24 h, request {self.request_file}")

    # ---------- state ----------

    def _load(self):
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            data = {}
        return {
            "restarts": [float(t) for t in data.get("restarts") or []][-10:],
            "gave_up": bool(data.get("gave_up")),
            "verify_at": data.get("verify_at"),
            "requested_at": data.get("requested_at"),
        }

    def _save(self):
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state))
        os.replace(tmp, self.state_file)

    # ---------- loop ----------

    def _observe(self):
        obs = {}
        for raw, twin in self.pairs.items():
            r = self.get_state(raw, attribute="all") or {}
            t = self.get_state(twin, attribute="all") or {}
            obs[raw] = (r.get("state"), r.get("last_changed"), t.get("state"), t.get("last_changed"))
        return obs

    def _check(self, kwargs):
        now = time.time()
        obs = self._observe()
        self._since = track(self._since, obs, now)
        if self._request_stuck(now):
            return
        raw, held = longest(self._since, now)
        mirrors = not any(not_mirrored(o[0], o[2]) for o in obs.values())
        action = decide(self.state, now, held, mirrors, self.cfg)
        if action is None:
            if held >= self.cfg["hold_s"] and not self.state["gave_up"] and not self._capped_logged:
                self._capped_logged = True
                self.log(f"{raw} mismatched {held / 60:.0f} min but {self.cfg['max_per_day']} reloads "
                         f"in 24 h already: not reloading", level="WARNING")
            return
        self._capped_logged = False
        getattr(self, f"_on_{action}")(now, raw, held, obs)

    def _on_restart(self, now, raw, held, obs):
        twin = self.pairs[raw]
        twin_state = obs[raw][2]
        try:
            fd = os.open(self.request_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            os.close(fd)
        except FileExistsError:
            self.log(f"Reload request {self.request_file} already pending", level="WARNING")
            return
        except OSError as e:
            self.log(f"Cannot write reload request {self.request_file}: {e}", level="ERROR")
            return
        self.state["restarts"] = (self.state["restarts"] + [now])[-10:]
        self.state["verify_at"] = now + self.verify_s
        self.state["requested_at"] = now
        self._save()
        self._since = {}
        room = self._room(twin)
        self.log(f"Reloading Music Assistant's Sonos provider: {raw} playing, {twin} {twin_state} for {held / 60:.1f} min",
                 level="WARNING")
        self._report(f"Reloaded Music Assistant's Sonos provider: {room} played on Sonos but showed {twin_state}")
        self._push("Reloaded Music Assistant's Sonos provider",
                   f"{room} played on Sonos but Music Assistant showed {twin_state} for {held / 60:.0f} min, "
                   "so automations saw the wrong state. Music Assistant playback may stop for a moment.",
                   "ma_twin_watch")

    def _on_verified(self, now, raw, held, obs):
        self.state["verify_at"] = None
        self._save()
        self.log("Music Assistant twins mirror the Sonos speakers again after the Sonos provider reload")

    def _on_verify_failed(self, now, raw, held, obs):
        bad = next(r for r, o in obs.items() if not_mirrored(o[0], o[2]))
        self._give_up(bad, obs[bad][2], f"{self.verify_s // 60} min after the reload")

    def _on_persisted(self, now, raw, held, obs):
        self._give_up(raw, obs[raw][2], f"again {held / 60:.0f} min within {self.cfg['cooldown_s'] // 60} min of the reload")

    def _give_up(self, raw, twin_state, when):
        self.state["gave_up"] = True
        self.state["verify_at"] = None
        self._save()
        room = self._room(self.pairs[raw])
        self.log(f"Music Assistant Sonos provider reload did not help: {raw} playing, {self.pairs[raw]} {twin_state} "
                 f"{when}. No more reloads until the twins mirror again", level="WARNING")
        self._push("Music Assistant Sonos reload did not help",
                   f"{room} still plays on Sonos but Music Assistant shows {twin_state}. "
                   "No more automatic reloads until it follows again.",
                   "ma_twin_watch")

    def _on_rearm(self, now, raw, held, obs):
        self.state["gave_up"] = False
        self._save()
        self.log("Music Assistant twins mirror again: MaTwinWatch re-armed")

    def _request_stuck(self, now):
        """True (and gives up) when the host left a request unconsumed for pending_s."""
        requested = self.state.get("requested_at")
        if requested is None:
            return False
        if not self.request_file.exists():
            self.state["requested_at"] = None
            self._save()
            self.log(f"Host picked up the reload request within {now - requested:.0f} s")
            return False
        if now - requested < self.pending_s:
            return True
        try:
            self.request_file.unlink()
        except OSError as e:
            self.log(f"Cannot remove stale request {self.request_file}: {e}", level="ERROR")
        self.state.update(requested_at=None, verify_at=None, gave_up=True)
        self.state["restarts"] = self.state["restarts"][:-1]
        self._save()
        self.log(f"Host did not pick up the reload request in {self.pending_s} s: ma-restart.path is not "
                 f"running. On the box: {RESET_COMMAND}", level="ERROR")
        self._push("Music Assistant Sonos reload did not run",
                   f"The box did not pick up the reload request. On the box: {RESET_COMMAND}",
                   "ma_twin_watch")
        return True

    # ---------- output ----------

    def _room(self, twin):
        name = self.get_state(twin, attribute="friendly_name")
        return name or twin.split(".", 1)[-1].replace("_", " ").capitalize()

    def _report(self, text):
        try:
            self.fire_event("house_events_report", text=text, icon="mdi:restart", audience="admin")
        except Exception as e:
            self.log(f"house_events_report failed: {e}", level="WARNING")

    def _push(self, title, message, tag):
        self.create_task(self._send(title, message, tag))

    async def _send(self, title, message, tag):
        try:
            sent = await self._notifier.notify(title=title, message=message, target=self.notify_target,
                                               data={"tag": tag})
        except Exception as e:
            self.log(f"notify failed: {e}", level="WARNING")
            return
        if not sent:
            self.log(f"Push '{title}' reached no one", level="WARNING")
