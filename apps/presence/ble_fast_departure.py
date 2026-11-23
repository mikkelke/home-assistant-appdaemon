"""Log possible early departures and their Wi-Fi outcomes for one resident."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass


MISSING = (None, "unavailable", "")
SILENT = "unknown"
DOOR_STATES = ("off", "on")
TRANSMITTING = "Transmitting"
DOOR_WINDOW_S = 300
SILENCE_S = 120
REVIEW_S = 15 * 60


@dataclass
class Attempt:
    door_opened_at: datetime
    ble_unheard_at: datetime
    candidate_at: datetime | None = None


class BleFastDeparture(hass.Hass):
    """An open door can arm a new BLE gap while Wi-Fi still reports home.

    Callbacks are pinned in the app config so timers and state changes are handled in order.
    Only a candidate and its resolution are logged. Recorder retains the input history.
    """

    def initialize(self):
        args = self.args
        self.resident = args["resident"]
        self.door = args["door"]
        self.area = args["area_sensor"]
        self.wifi = args["tracker"]
        self.switch = args["shadow_switch"]
        self.transmitter = args.get("transmitter")
        self.log_file = Path(args.get("log_file") or Path(__file__).with_name("ble_fast_departure_log.jsonl"))
        self._opened_at = None
        self._attempt = None
        self._timer = None
        for entity in (self.door, self.area, self.wifi, self.switch, self.transmitter):
            if entity:
                self.listen_state(self._on_change, entity)
        self.log(f"BLE fast departure for {self.resident} in shadow mode ({self.switch})")

    def _on_change(self, entity, attribute, old, new, kwargs):
        if old == new:
            return
        if entity == self.door:
            if new not in DOOR_STATES:
                self._disarm("door_unavailable")
            elif old == "off" and new == "on" and self._attempt is None and self._can_arm():
                self._opened_at = self._now()
        elif entity == self.area:
            self._on_area(old, new)
        elif entity == self.wifi and new != "home":
            self._on_wifi_departure(new)
        elif entity == self.switch and new != "on":
            self._disarm("shadow_disabled")
        elif entity == self.transmitter and new != TRANSMITTING:
            self._disarm("transmitter_not_running")

    def _on_area(self, old, new):
        if new in MISSING:
            self._disarm("ble_unavailable")
        elif self._heard(new):
            wifi = self.get_state(self.wifi)
            if wifi == "home" and self._can_measure():
                self._finish("ble_recovered_while_wifi_home")
            else:
                self._disarm("wifi_or_monitor_changed_before_ble_recovery")
        elif new == SILENT and self._heard(old) and self._opened_at is not None:
            now = self._now()
            since_opened = (now - self._opened_at).total_seconds()
            if since_opened <= DOOR_WINDOW_S and self._can_measure() and self.get_state(self.wifi) == "home":
                self._attempt = Attempt(self._opened_at, now)
                self._timer = self.run_in(self._after_silence, SILENCE_S, gap=now.isoformat())
            else:
                self._opened_at = None

    def _on_wifi_departure(self, new):
        self._opened_at = None
        attempt = self._attempt
        if attempt is None or attempt.candidate_at is None:
            self._finish("wifi_changed_before_candidate")
        elif new in (*MISSING, SILENT):
            self._finish("wifi_unavailable")
        elif (self._can_measure() and self._held_since(self.area, SILENT, attempt.ble_unheard_at)
              and (self._now() - attempt.candidate_at).total_seconds() <= REVIEW_S):
            self._finish("wifi_confirmed", wifi=new)
        else:
            self._finish("wifi_changed_without_continuous_ble_silence")

    def _after_silence(self, kwargs):
        attempt = self._current_attempt(kwargs)
        if attempt is None or attempt.candidate_at is not None:
            return
        self._timer = None
        now = self._now()
        if not self._gap_is_current(attempt):
            self._disarm("inputs_changed_before_candidate")
            return
        remaining = SILENCE_S - (now - attempt.ble_unheard_at).total_seconds()
        if remaining > 0:
            self._timer = self.run_in(self._after_silence, remaining, gap=kwargs["gap"])
            return
        attempt.candidate_at = now
        self._opened_at = None
        self._write(attempt, "candidate", now)
        self._timer = self.run_in(self._review_ended, REVIEW_S, gap=kwargs["gap"])

    def _review_ended(self, kwargs):
        if self._current_attempt(kwargs) is not None:
            self._timer = None
            self._finish("wifi_not_confirmed_within_window")

    def _current_attempt(self, kwargs):
        attempt = self._attempt
        if attempt is not None and attempt.ble_unheard_at.isoformat() == kwargs["gap"]:
            return attempt
        return None

    def _can_arm(self):
        return self._can_measure() and self.get_state(self.wifi) == "home" and self._heard(self.get_state(self.area))

    def _can_measure(self):
        return (self.get_state(self.switch) == "on" and self.get_state(self.door) in DOOR_STATES
                and (self.transmitter is None or self.get_state(self.transmitter) == TRANSMITTING))

    def _gap_is_current(self, attempt):
        # State updates can already be in HA's cache while their callbacks are still queued.
        return (self.get_state(self.door) in DOOR_STATES
                and self._held_since(self.area, SILENT, attempt.ble_unheard_at)
                and self._held_since(self.wifi, "home", attempt.door_opened_at)
                and self._held_since(self.switch, "on", attempt.door_opened_at)
                and (self.transmitter is None
                     or self._held_since(self.transmitter, TRANSMITTING, attempt.door_opened_at)))

    def _held_since(self, entity, expected, since):
        snapshot = self.get_state(entity, attribute="all")
        if not snapshot or snapshot.get("state") != expected:
            return False
        try:
            changed = datetime.fromisoformat(snapshot.get("last_changed"))
        except (TypeError, ValueError):
            return False
        return changed.tzinfo is not None and changed <= since

    @staticmethod
    def _heard(state):
        return state not in (*MISSING, SILENT)

    def _now(self):
        return self.get_now().astimezone(timezone.utc)

    def _disarm(self, reason):
        self._opened_at = None
        self._finish(reason)

    def _finish(self, outcome, wifi=None):
        attempt, self._attempt = self._attempt, None
        if self._timer is not None:
            self.cancel_timer(self._timer, silent=True)
            self._timer = None
        if attempt is not None and attempt.candidate_at is not None:
            self._write(attempt, outcome, self._now(), wifi=wifi)

    def _write(self, attempt, event, now, wifi=None):
        row = {"resident": self.resident, "event": event, "at": now.isoformat(),
               "door_opened_at": attempt.door_opened_at.isoformat(),
               "ble_unheard_at": attempt.ble_unheard_at.isoformat(),
               "candidate_at": attempt.candidate_at.isoformat()}
        if wifi is not None:
            row["wifi_state"] = wifi
            row["wifi_lead_s"] = round((now - attempt.candidate_at).total_seconds(), 1)
        try:
            with self.log_file.open("a", encoding="utf-8") as log:
                log.write(json.dumps(row) + "\n")
        except OSError as error:
            self.log(f"Could not write {self.log_file}: {error}", level="WARNING")
        self.log(f"BLE fast departure shadow for {self.resident}: {event}")
