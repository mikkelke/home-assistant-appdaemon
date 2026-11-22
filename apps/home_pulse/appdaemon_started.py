"""AppDaemonStarted - tells Home Assistant each time AppDaemon starts its apps.

At every initialize, which runs when AppDaemon starts or reconnects to Home Assistant, the
app sets sensor.appdaemon_started to the start time in epoch seconds. Home Assistant counts
those changes and pushes when AppDaemon restarts too often; the counting and the push live in
Home Assistant (automation "AppDaemon watchdog"), so they keep working while AppDaemon loops.
"""

import time

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

ENTITY = "sensor.appdaemon_started"


class AppDaemonStarted(hass.Hass):
    def initialize(self):
        self.set_state(ENTITY, state=int(time.time()), attributes={
            "friendly_name": "AppDaemon started",
            "icon": "mdi:restart",
        })
