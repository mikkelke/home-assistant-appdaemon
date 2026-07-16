"""
Radio watchdog - one self-resume attempt when a radio stream dies underneath us.

2026-07-08: DR P3's HLS playlist 404'd mid-evening and every grouped speaker
fell silent 4 s later; nothing in the house noticed. This app watches the raw
Sonos players (they expose ``media_content_id``) and, when a RADIO stream that
has been playing for a while drops to idle on the group coordinator, retries
the same stream ONCE and tells the user what happened either way.

Deliberately conservative:
  - radio content only (x-sonosapi / DR / akamaized URIs), never queues/Spotify
  - only the group coordinator (members follow their coordinator)
  - stream must have played >= min_play_minutes (a quick stop is a human)
  - one attempt per player per cooldown_minutes
  - every action is notified - silence from this app means it did nothing
"""

from datetime import datetime

import appdaemon.plugins.hass.hassapi as hass

RADIO_MARKERS = ("x-sonosapi-hls", "x-sonosapi-stream", "x-rincon-mp3radio",
                 "dr.dk", "akamaized")


def looks_like_radio(content_id):
    cid = str(content_id or "")
    return any(m in cid for m in RADIO_MARKERS)


class RadioWatchdog(hass.Hass):
    def initialize(self):
        a = self.args.get
        self.players = list(a("players", []))
        self.min_play_minutes = float(a("min_play_minutes", 10))
        self.cooldown_minutes = float(a("cooldown_minutes", 60))
        self.check_delay_s = int(a("check_delay_seconds", 12))
        self.enabled = bool(a("enabled", True))
        self.notify_target = a("notify_target", "mikkel")

        self._playing_since = {}
        self._last_content = {}
        self._last_attempt = {}
        # get_app must be resolved in sync init - async context returns a Task.
        self._notifier = self.get_app("MobileNotifier")

        for p in self.players:
            self.listen_state(self._on_player, p, player=p)

    def _on_player(self, entity, attribute, old, new, kwargs):
        player = kwargs.get("player") or entity
        if new == "playing" and old != "playing":
            self._playing_since[player] = datetime.now()
        if new == "playing":
            cid = self.get_state(player, attribute="media_content_id")
            if cid:
                self._last_content[player] = cid
        if old == "playing" and new in ("idle", "off") and self.enabled:
            self.run_in(self._check_dead, self.check_delay_s, player=player)

    def _check_dead(self, kwargs):
        self.create_task(self._check_dead_async(kwargs.get("player")))

    async def _check_dead_async(self, player):
        try:
            cid = self._last_content.get(player)
            if not looks_like_radio(cid):
                return
            state = await self.get_state(player)
            if state not in ("idle", "off"):
                return  # something else started - not our business
            started = self._playing_since.get(player)
            if not started or (datetime.now() - started).total_seconds() < self.min_play_minutes * 60:
                return  # short session - assume a human stopped it
            members = await self.get_state(player, attribute="group_members")
            if isinstance(members, (list, tuple)) and members and members[0] != player:
                return  # not the coordinator
            last = self._last_attempt.get(player)
            if last and (datetime.now() - last).total_seconds() < self.cooldown_minutes * 60:
                return
            self._last_attempt[player] = datetime.now()

            self.log(f"radio died on {player} ({cid}) - attempting one resume")
            await self.call_service("media_player/play_media", entity_id=player,
                                    media_content_id=cid, media_content_type="music")
            self.run_in(self._verify_resume, 15, player=player, cid=cid)
            # Explain the invisible self-heal to the dashboard's Home activity feed -
            # a silently-restarted radio is otherwise indistinguishable from "it never stopped".
            try:
                room = player.split(".", 1)[-1].replace("_", " ").capitalize()
                await self.fire_event(
                    "house_events_report",
                    cause="Radio stream went silent",
                    effect=f"Restarting the radio on {room}",
                    icon="mdi:radio",
                )
            except Exception:
                pass
        except Exception as e:
            self.log(f"watchdog check failed for {player}: {e}", level="ERROR")

    def _verify_resume(self, kwargs):
        self.create_task(self._verify_async(kwargs.get("player"), kwargs.get("cid")))

    async def _verify_async(self, player, cid):
        try:
            ok = (await self.get_state(player)) == "playing"
            msg = (f"Radio stream died on {self.friendly_name(player)} - resumed it."
                   if ok else
                   f"Radio stream died on {self.friendly_name(player)} and the retry "
                   f"failed too (stream itself is likely down).")
            await self._notifier.notify(title="Radio watchdog", message=msg,
                                        target=self.notify_target)
            self.log(f"resume on {player}: {'ok' if ok else 'failed'}")
        except Exception as e:
            self.log(f"watchdog verify failed: {e}", level="ERROR")
