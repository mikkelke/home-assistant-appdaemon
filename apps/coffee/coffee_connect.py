"""When the coffee app holds the machine's Bluetooth link (switch.xbloom_studio_connect). Pure.

The machine takes one link, and the xBloom app is connected for some brews, so the link is taken on an
event and never on a level: the power rising through 10 W while Connect is off, a start or connect
op, or the app starting while the machine works. Never on the machine waking up. A failed connect is
retried RETRIES times, RETRY_S apart, while its trigger still holds. Connect goes off once no brew is
under way and QUIET_OFF_S have passed since the last of: the link going on, a brew ending or being
picked up, an op. A person switching Connect off stops the automatic triggers for SUPPRESS_S.
"""

QUIET_OFF_S = 600
SUPPRESS_S = 1800
RETRIES = 2
RETRY_S = 20
AUTO = ("power_rise", "init_working")
TRIGGERS = AUTO + ("op_start", "op_connect")


class ConnectPolicy:
    def __init__(self, data=None):
        data = data if isinstance(data, dict) else {}
        self.suppress_until = data.get("suppress_until")
        self.last_activity = data.get("last_activity")
        self.kind = None
        self.retries_left = 0
        self.retry_at = None

    def to_dict(self):
        return {"suppress_until": self.suppress_until, "last_activity": self.last_activity}

    def activity(self, now):
        self.last_activity = now

    def suppressed(self, now):
        return self.suppress_until is not None and now < self.suppress_until

    def trigger(self, kind, now, switch_on):
        """'on' when the link should be taken now, else None."""
        if kind not in TRIGGERS:
            return None
        self.activity(now)
        if kind in AUTO and self.suppressed(now):
            return None
        if switch_on:
            return None
        self.kind, self.retries_left, self.retry_at = kind, RETRIES, None
        return "on"

    def failed(self, now):
        if self.kind is not None and self.retries_left > 0:
            self.retry_at = now + RETRY_S

    def connected(self, now):
        self.kind, self.retries_left, self.retry_at = None, 0, None
        self.activity(now)

    def person_off(self, now):
        self.suppress_until = now + SUPPRESS_S
        self.kind, self.retries_left, self.retry_at = None, 0, None

    def tick(self, now, switch_on, busy, holds):
        """'on', 'off' or None. `busy`: a brew is starting, under way or ready. `holds(kind)`: whether
        the trigger behind a pending retry still holds."""
        if self.retry_at is not None and now >= self.retry_at:
            self.retry_at = None
            if not switch_on and self.kind is not None and holds(self.kind):
                self.retries_left -= 1
                return "on"
            self.kind = None
        if switch_on and not busy and (self.last_activity is None or now - self.last_activity >= QUIET_OFF_S):
            return "off"
        return None
