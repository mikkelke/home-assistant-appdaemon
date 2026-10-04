# The plug-read surface of WasherMonitor for hand-built test apps (WasherMonitor.__new__ fixtures): a PlugPoller that
# never starts its thread, driven by a fake monotonic clock, plus the knobs the finish decisions read.
import washer_plug as wplug


class FakeClock:
    def __init__(self, t=0.0):
        self.t = float(t)

    def __call__(self):
        return self.t


def attach_plug(app, reads=(), clock=None, poll_s=wplug.POLL_S):
    """Give `app` a thread-less PlugPoller whose ring holds `reads` ((monotonic_s, watts) pairs, watts None for a
    failed read) and whose clock stands at the newest read unless `clock` is given."""
    clock = clock or FakeClock(reads[-1][0] if reads else 0.0)
    app._plug = wplug.PlugPoller(lambda: 0.0, poll_s=poll_s, clock=clock)
    app._plug.start = lambda: None
    for ts, w in reads:
        app._plug._ring.append((ts, w))
        if w is not None:
            app._plug._last_ok = ts
            app._plug._peak = max(app._plug._peak, w)
    app.plug_host = "plug.invalid"
    app.plug_poll_s = poll_s
    app.plug_unreachable_push_minutes = 10
    app.wash_activity_watts = wplug.ACTIVITY_W
    app._plug_offline_pushed = False
    app._plug_outage_pushed = getattr(app, "_plug_outage_pushed", False)
    app._activity_seen = getattr(app, "_activity_seen", False)
    app._spin_end_at = None
    app._spin_end_checked_at = None
    app.energy_check_timer = getattr(app, "energy_check_timer", None)
    app.energy_check_interval = getattr(app, "energy_check_interval", 30)
    return clock


def grid(t0, watts_list, poll_s=wplug.POLL_S):
    """Reads at t0, t0+poll_s, ... with the given watts (None = failed read)."""
    return [(t0 + i * poll_s, w) for i, w in enumerate(watts_list)]
