# /conf/apps/sonos/audiocast_manager.py

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


class AudiocastManager(hass.Hass):
    """
    Watches the Chromecast Audio behind the "Audiocast" feed (CCA -> line-in on
    Kristines room Era 100 -> x-rincon-stream pulled by any Sonos player; the
    dashboard side lives in src/config/audiocast.ts).

    1) Cast stopped -> stop the feed everywhere:
       When the cast session ends (state in stopped_states, re-checked after
       stopped_confirm_seconds), every Sonos player currently on the Audiocast
       stream is stopped, then handed to SonosStateReset via the
       sonos_reset_speakers event for ungroup + default-volume restore.
       'paused' is deliberately ignored - a paused cast keeps the group intact.
    2) Cast volume pinned at 100%:
       Loudness is controlled per Sonos speaker; the CCA line-out must stay at
       line level. Any volume change on the cast entity snaps back to 1.0.
    """

    def initialize(self):
        self.cast_entity = self.args["cast_entity"]
        self.uri_prefix = self.args["audiocast_uri_prefix"]
        self.raw_to_base = dict(self.args["raw_to_base_map"])
        self.stopped_states = set(self.args.get("stopped_states", ["off", "idle"]))
        self.confirm_sec = int(self.args.get("stopped_confirm_seconds", 20))
        self.enforce_volume = bool(self.args.get("enforce_cast_volume", True))
        # Generation counter invalidates stale confirm timers without cancelling
        # (same pattern as state_reset.py inactivity timers)
        self._stop_generation = 0

        self.listen_state(self._on_cast_state, self.cast_entity)
        if self.enforce_volume:
            self.listen_state(self._on_cast_volume, self.cast_entity, attribute="volume_level")
            self.run_in(self._pin_cast_volume, 5)

        self.log(
            f"AudiocastManager watching {self.cast_entity} "
            f"(uri_prefix={self.uri_prefix}, stopped_states={sorted(self.stopped_states)}, "
            f"confirm={self.confirm_sec}s, enforce_volume={self.enforce_volume})",
            level="INFO",
        )

    # ---------- cast session end -> stop feed players, then targeted reset ----------

    def _on_cast_state(self, entity, attribute, old, new, kwargs):
        if new in self.stopped_states:
            if old in self.stopped_states:
                return  # off<->idle shuffle; keep the pending confirm timer alive
            self._stop_generation += 1
            self.log(
                f"{entity}: {old} -> {new}; confirming cast stop in {self.confirm_sec}s",
                level="INFO",
            )
            self.run_in(self._confirmed_stop, self.confirm_sec, generation=self._stop_generation)
        else:
            self._stop_generation += 1  # resumed/paused/unavailable: invalidate pending stop
            if new == "playing" and self.enforce_volume:
                self._pin_cast_volume({})

    def _confirmed_stop(self, kwargs):
        if kwargs.get("generation") != self._stop_generation:
            return  # cast state changed since scheduling; stale timer
        state = self.get_state(self.cast_entity)
        if state not in self.stopped_states:
            self.log(f"Cast resumed ({state}) before confirm elapsed; skipping stop", level="INFO")
            return

        playing_raw = self._audiocast_players()
        if not playing_raw:
            self.log("Cast stopped; no Sonos players on the Audiocast feed", level="INFO")
            return

        self.log(f"Cast stopped; stopping Audiocast on: {', '.join(playing_raw)}", level="INFO")
        for eid in playing_raw:
            try:
                self.call_service("media_player/media_stop", entity_id=eid)
            except Exception as exc:
                self.log(f"media_stop failed for {eid}: {exc}", level="WARNING")

        targets = sorted({self._base_id(eid) for eid in playing_raw})
        # SonosStateReset does ungroup + default volumes + unmute (targets = MA base ids)
        self.fire_event("sonos_reset_speakers", targets=targets)
        self.log(f"Fired sonos_reset_speakers for: {', '.join(targets)}", level="INFO")

    def _base_id(self, raw_eid):
        mapped = self.raw_to_base.get(raw_eid)
        if mapped:
            return mapped
        return raw_eid[:-2] if raw_eid.endswith("_2") else raw_eid

    def _audiocast_players(self):
        """Raw Sonos entities currently on the Audiocast x-rincon stream."""
        players = []
        for eid in self.raw_to_base:
            try:
                if self.get_state(eid) not in ("playing", "buffering", "paused"):
                    continue
                content = self.get_state(eid, attribute="media_content_id") or ""
                if isinstance(content, str) and content.startswith(self.uri_prefix):
                    players.append(eid)
            except Exception as exc:
                self.log(f"state read failed for {eid}: {exc}", level="DEBUG")
        return players

    # ---------- cast volume pinned to 100% ----------

    def _on_cast_volume(self, entity, attribute, old, new, kwargs):
        if new is None:
            return
        try:
            vol = float(new)
        except (TypeError, ValueError):
            return
        if vol < 0.99:
            self.log(f"Cast volume {vol:.2f} -> pinning back to 1.0", level="INFO")
            self._pin_cast_volume({})

    def _pin_cast_volume(self, kwargs):
        state = self.get_state(self.cast_entity)
        if state in (None, "unavailable", "unknown"):
            return  # nothing to set while the device is away
        vol = self.get_state(self.cast_entity, attribute="volume_level")
        try:
            if vol is not None and float(vol) >= 0.99:
                return
        except (TypeError, ValueError):
            pass
        try:
            self.call_service("media_player/volume_set", entity_id=self.cast_entity, volume_level=1.0)
        except Exception as exc:
            self.log(f"volume_set failed for {self.cast_entity}: {exc}", level="WARNING")
