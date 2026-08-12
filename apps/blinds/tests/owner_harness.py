"""Shared test harness for BedroomBlindOwner - a fully faked AppDaemon surface
around the REAL initialize(), used by the owner's own tests and by the wakeup
yield tests (which drive a real owner end-to-end). Not named test_* on purpose:
unittest discovery must not collect it."""

from __future__ import annotations

import os
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

if "appdaemon.plugins.hass.hassapi" not in sys.modules:
    ad = types.ModuleType("appdaemon")
    plugins = types.ModuleType("appdaemon.plugins")
    hassmod = types.ModuleType("appdaemon.plugins.hass")
    hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
    hassapi.Hass = object
    sys.modules["appdaemon"] = ad
    sys.modules["appdaemon.plugins"] = plugins
    sys.modules["appdaemon.plugins.hass"] = hassmod
    sys.modules["appdaemon.plugins.hass.hassapi"] = hassapi

import bedroom_blind_owner as bbo  # noqa: E402

AD_UID = "f4fda494358943beaf8ad2c70db099f8"
COVER = "cover.bedroom_blind"
NOW = datetime(2026, 8, 12, 6, 0, 0, tzinfo=timezone.utc)


def _fresh_path():
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    return path


def make_owner(args=None, position=100, now=NOW, state_file=None):
    """BedroomBlindOwner built via the REAL initialize() against a faked AD surface."""
    app = bbo.BedroomBlindOwner.__new__(bbo.BedroomBlindOwner)
    app.args = {
        "appdaemon_user_id": AD_UID,
        "state_file": state_file or _fresh_path(),
        "shade_state_file": "/nonexistent/shade_state.json",
        **(args or {}),
    }
    app._now = now
    app.get_now = lambda: app._now
    app._pos = position
    app._ctx = None

    def get_state(entity, attribute=None, **kw):
        if attribute == "current_position":
            return app._pos
        if attribute == "all":
            return {"context": {"user_id": app._ctx}}
        return None

    app.get_state = get_state
    app.listeners = []
    app.listen_state = lambda cb, entity, **kw: app.listeners.append((cb, entity, kw))
    app.scheduled = []

    def run_in(cb, delay, **kw):
        handle = object()
        app.scheduled.append((cb, delay, kw, handle))
        return handle

    app.run_in = run_in
    app.timer_running = lambda h: any(h is s[3] for s in app.scheduled)

    def cancel_timer(h):
        app.scheduled[:] = [s for s in app.scheduled if s[3] is not h]

    app.cancel_timer = cancel_timer
    app.log_lines = []
    app.log = lambda msg, **kw: app.log_lines.append(str(msg))
    app.set_state_calls = []
    app.set_state = lambda entity, **kw: app.set_state_calls.append((entity, kw))
    app.cover_commands = []

    def call_service(service, **kw):
        app.cover_commands.append((service, kw))

    app.call_service = call_service
    app.initialize()
    return app


def positions_commanded(app):
    return [kw["position"] for s, kw in app.cover_commands if s == "cover/set_cover_position"]


def report(app, pos, ctx=None, advance_s=5):
    """One position report from the cover, the way the motor emits them (~every 5 s)."""
    app._now = app._now + timedelta(seconds=advance_s)
    app._pos = pos
    app._ctx = ctx
    app._on_cover_change(COVER, "current_position", None, str(pos), {})


def settle(app):
    """The motor went silent for manual_settle_seconds."""
    pending = [s for s in app.scheduled if s[0] == app._motor_settled]
    assert len(pending) == 1, f"expected exactly one settle timer, got {len(pending)}"
    cb, _delay, kw, handle = pending[0]
    app.scheduled[:] = [s for s in app.scheduled if s[3] is not handle]
    app._now = app._now + timedelta(seconds=app.manual_settle_s)
    cb(kw)
