"""EsphomeLinkWatch - reloads an ESPHome device's Home Assistant connection when it hangs.

Each entry in probes is one entity of an ESPHome device that reports while the device is
connected. When a probe has read unavailable for stale_s seconds, the app reloads the
device's config entry (homeassistant.reload_config_entry on the probe), at most once per
cooldown_s and at most max_reloads times per episode; the episode ends when the probe reads
anything else. A device that is really off just gets those few reloads and a warning.
Checked every check_s seconds, so a probe that was already unavailable at startup counts too.
"""

from datetime import datetime, timezone

import appdaemon.plugins.hass.hassapi as hass  # type: ignore


def _ts(value):
    try:
        return datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def due(state, last_changed, now, stale_s, last_reload, cooldown_s, reloads, max_reloads):
    """Whether a probe should get a reload now."""
    if state != "unavailable":
        return False
    since = _ts(last_changed)
    if since is None or now - since < stale_s:
        return False
    if reloads >= max_reloads:
        return False
    return last_reload is None or now - last_reload >= cooldown_s


class EsphomeLinkWatch(hass.Hass):
    def initialize(self):
        self.probes = list(self.args["probes"])
        self.stale_s = int(self.args.get("stale_s", 120))
        self.cooldown_s = int(self.args.get("cooldown_s", 600))
        self.max_reloads = int(self.args.get("max_reloads", 3))
        self._last_reload = {}
        self._reloads = {}
        self.run_every(self._check, "now+30", int(self.args.get("check_s", 30)))

    def _check(self, kwargs):
        now = datetime.now(timezone.utc).timestamp()
        for probe in self.probes:
            full = self.get_state(probe, attribute="all") or {}
            state = full.get("state")
            if state not in (None, "unavailable"):
                if self._reloads.get(probe):
                    self.log(f"{probe} is back ({state}) after {self._reloads[probe]} reload(s)")
                self._reloads[probe] = 0
                continue
            reloads = self._reloads.get(probe, 0)
            if not due(state, full.get("last_changed"), now, self.stale_s,
                       self._last_reload.get(probe), self.cooldown_s, reloads, self.max_reloads):
                continue
            self._last_reload[probe] = now
            self._reloads[probe] = reloads + 1
            level = "WARNING" if reloads + 1 == self.max_reloads else "INFO"
            self.log(f"{probe} unavailable for over {self.stale_s} s: reloading its ESPHome "
                     f"connection ({reloads + 1}/{self.max_reloads})", level=level)
            self.call_service("homeassistant/reload_config_entry", entity_id=probe)
