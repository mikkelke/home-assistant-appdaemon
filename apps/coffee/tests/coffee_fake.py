# tests/coffee_fake.py - CoffeeMachine on a fake AppDaemon surface: states with listeners, events with
# HA's metadata, a clock that runs the app's timers, service calls with canned answers, and a replay of
# tapes (timed states, events and dashboard ops). Shared by the app tests.

import asyncio
import json
import math
import sys
import tempfile
import types
from datetime import datetime, timezone
from pathlib import Path

import yaml

APPS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(APPS / "coffee"))
sys.path.insert(0, str(APPS / "appliances"))
_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for _name, _mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(_name, _mod)

import coffee_machine as cm  # noqa: E402

CONFIG = yaml.safe_load((APPS / "coffee" / "coffee_machine.yaml").read_text())["CoffeeMachine"]
T0 = datetime(2026, 10, 20, 5, 40, tzinfo=timezone.utc).timestamp()
TABLET = CONFIG["tablet_user_id"]
MIKKEL = "ff5112b8da6f4a28b3c3dfaf758f372d"
KRISTINE = "5bee78148825439488b70f92565e6c26"


def iso(ts):
    return datetime.fromtimestamp(ts, timezone.utc).isoformat()


def pour(ml, pause, pattern=3, after=2, flow=3.0):
    return {"volume_ml": ml, "temperature_c": 93.0, "pattern": pattern, "flow_rate": flow, "pause_s": pause,
            "agitate_before": 2, "agitate_after": after}


USUAL = {"id": "81696", "name": "4 Pour Recipe", "dose_g": 15.0, "water_ratio": 17.0, "grinder_size": 46.0,
         "grinder_size_enabled": 1, "rpm": 120, "pour_count": 4, "cup_type": 2, "bypass_water_enabled": 2,
         "ratio": "1:17", "grind_size": 46.0, "grinder_speed_rpm": 120,
         "pours": [pour(45, 15, after=1), pour(80, 15, after=1, flow=3.5), pour(65, 12, flow=3.5),
                   pour(65, 5, pattern=1, flow=3.5)]}
OTHER = {**USUAL, "id": "7654321", "name": "Iced", "dose_g": 18.0, "water_ratio": 15.0, "ratio": "1:15",
         "pours": [pour(90, 30), pour(180, 0)]}


def maybe_async(value):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return value

    async def wrap():
        return value

    return wrap()


def ok(response=None):
    return {"success": True, "result": {"response": response}, "ad_status": "OK"}


class Notifier:
    def __init__(self):
        self.sent = []
        self.deliver = 1

    async def notify(self, **kwargs):
        self.sent.append(kwargs)
        return self.deliver


class Attribution:
    def __init__(self):
        self.users = {MIKKEL: "mikkel", KRISTINE: "kristine"}
        self.sole = None
        self.calls = []

    def attribute(self, event, at=None, user_id=None):
        self.calls.append((event, user_id))
        if user_id and user_id in self.users:
            return {"person": self.users[user_id], "method": "dashboard_action"}
        if self.sole:
            return {"person": self.sole, "method": "sole_occupant"}
        return {"person": None, "method": "unknown", "reason": "multi_occupant"}


def base_states(now):
    def full(state, attributes=None, changed=None):
        return {"state": state, "attributes": dict(attributes or {}), "last_changed": iso(changed or now - 3600),
                "context": {"user_id": None}}
    return {
        "sensor.pour_over_plug_power": full("2.2"),
        "switch.pour_over_plug": full("on"),
        "switch.xbloom_studio_connect": full("off"),
        "sensor.xbloom_studio_status_updated": full(iso(now - 3600)),
        "sensor.xbloom_studio_scale_weight": full("0.0"),
        "sensor.xbloom_studio_water_dispensed": full("0.0"),
        "event.xbloom_studio_brew_event": full(iso(now - 7200), {"event_type": "brew_done"}),
        "sensor.xbloom_studio_brew_status": full("idle"),
        "binary_sensor.xbloom_studio_brew_paused": full("off"),
        "sensor.xbloom_studio_machine_status": full("ok"),
        "sensor.xbloom_studio_current_pour": full("0", {"total_pours": None}),
        "select.xbloom_studio_recipe": full("unknown", {"options": []}),
        "person.mikkel": full("home"),
        "person.kristine": full("home"),
        "person.claudia": full("not_home"),
        "binary_sensor.oppopad4pro_interactive": full("off", changed=now - 900),
        CONFIG["bermuda_distance"]: full("6.0"),
        CONFIG["bermuda_area"]: full("Bedroom"),
    }


def make_app(state_dir=None, now=T0, states=None, recipes=(USUAL, OTHER), **args):
    app = cm.CoffeeMachine.__new__(cm.CoffeeMachine)
    state_dir = Path(state_dir or tempfile.mkdtemp(prefix="coffee_"))
    app.args = {**CONFIG, "media_dir": str(state_dir / "media"), "ledger_file": str(state_dir / "coffee_feedback.json"),
                "state_file": str(state_dir / "coffee_state.json"), **args}
    app.dir = state_dir
    app.now = now
    app.states = states if states is not None else base_states(now)
    app.published, app.history, app.logs, app.calls, app.fired = {}, [], [], [], []
    app.state_listeners, app.event_listeners = [], []
    app.timers, app.tasks = {}, []
    app.switch_follows, app.switch_changes = True, []
    app.next_handle = [0]
    app.notifier, app.attribution = Notifier(), Attribution()
    app.responses = {"xbloom/list_recipes": ok({"recipes": list(recipes)}),
                     "xbloom/start_brew": ok({"run_id": "r1"})}

    def handle():
        app.next_handle[0] += 1
        return f"h{app.next_handle[0]}"

    def get_state(entity, attribute=None, **kw):
        value = app.states.get(entity)
        if attribute == "all":
            return json.loads(json.dumps(value)) if value is not None else None
        return None if value is None else value["state"]

    def set_state(entity, state=None, attributes=None, replace=False, **kw):
        app.published[entity] = (state, attributes, replace)
        app.history.append((app.now, entity, state, attributes))

    def listen_state(callback, entity, attribute=None, **kw):
        app.state_listeners.append((entity, attribute, callback))

    def listen_event(callback, event=None, **kw):
        app.event_listeners.append((event, kw, callback))

    def run_every(callback, start, interval, **kw):
        h = handle()
        offset = float(str(start).split("+")[1]) if "+" in str(start) else interval
        app.timers[h] = [app.now + offset, interval, callback, kw]
        return h

    def run_in(callback, delay, **kw):
        h = handle()
        app.timers[h] = [app.now + delay, None, callback, kw]
        return maybe_async(h)

    def cancel_timer(h, **kw):
        app.timers.pop(h, None)

    def call_service(service, **data):
        app.calls.append((service, data))
        if service in ("switch/turn_on", "switch/turn_off") and app.switch_follows:
            app.switch_changes.append((data.get("entity_id"), "on" if service.endswith("on") else "off"))
        return maybe_async(app.responses.get(service, ok()))

    def fire_event(event, **data):
        app.fired.append((event, data))

    app.get_state = get_state
    app.set_state = set_state
    app.listen_state = listen_state
    app.listen_event = listen_event
    app.run_every = run_every
    app.run_daily = lambda *a, **kw: handle()
    app.run_in = run_in
    app.cancel_timer = cancel_timer
    app.create_task = lambda coro: app.tasks.append(coro)
    app.call_service = call_service
    app.fire_event = fire_event
    app.get_app = lambda name: {"MobileNotifier": app.notifier, "ActorAttribution": app.attribution}.get(name)
    app.get_now_ts = lambda: app.now
    app.log = lambda msg, level="INFO", **kw: app.logs.append((level, msg))
    app.initialize()
    if recipes:
        app._refresh_recipes({})
        drain(app)
    return app


def drain(app):
    """Runs queued coroutines and the zero-delay callbacks they hand back, until idle. A switch the app turned
    on or off follows, as HA's would."""
    while True:
        while app.tasks:
            asyncio.run(app.tasks.pop(0))
        while app.switch_changes:
            entity, state = app.switch_changes.pop(0)
            if (app.states.get(entity) or {}).get("state") != state:
                set_entity(app, entity, state)
        due = [(h, t) for h, t in app.timers.items() if t[1] is None and t[0] <= app.now]
        if not due and not app.tasks:
            return
        for h, timer in sorted(due, key=lambda item: item[1][0]):
            app.timers.pop(h, None)
            timer[2](timer[3])


def advance(app, seconds, step=1.0):
    """Moves the clock forward, firing every timer that falls due in order."""
    end = app.now + seconds
    while True:
        drain(app)
        pending = [t[0] for t in app.timers.values() if t[0] <= end]
        if not pending:
            break
        app.now = max(app.now, min(pending))
        for h, timer in sorted(list(app.timers.items()), key=lambda item: item[1][0]):
            if h not in app.timers or timer[0] > app.now:
                continue
            if timer[1] is None:
                app.timers.pop(h)
            else:
                timer[0] += timer[1]
            timer[2](timer[3])
    app.now = end
    drain(app)


def set_entity(app, entity, state, attributes=None, user_id=None):
    """A state change as HA reports it to the app's listeners."""
    old = app.states.get(entity)
    kept = attributes if attributes is not None else (old or {}).get("attributes") or {}
    new = {"state": str(state), "attributes": dict(kept),
           "last_changed": iso(app.now), "context": {"user_id": user_id}}
    app.states[entity] = new
    for listened, attribute, callback in list(app.state_listeners):
        if listened != entity:
            continue
        if attribute == "all":
            callback(entity, "all", old, json.loads(json.dumps(new)), {})
        elif attribute:
            before = ((old or {}).get("attributes") or {}).get(attribute)
            after = new["attributes"].get(attribute)
            if before != after:
                callback(entity, attribute, before, after, {})
        elif (old or {}).get("state") != new["state"]:
            callback(entity, "state", (old or {}).get("state"), new["state"], {})
    for event, kw, callback in list(app.event_listeners):
        if event == "state_changed" and kw.get("entity_id") == entity:
            callback("state_changed", {"entity_id": entity, "old_state": old, "new_state": new,
                                       "metadata": {"context": {"user_id": user_id}}}, {})
    drain(app)


def fire(app, event, data=None, user_id=None):
    payload = {**(data or {}), "metadata": {"context": {"user_id": user_id, "id": "x"}, "origin": "LOCAL"}}
    for name, kw, callback in list(app.event_listeners):
        if name == event:
            callback(event, json.loads(json.dumps(payload)), {})
    drain(app)


def op(app, name, data=None, user_id=TABLET, as_text=True):
    payload = {"op": name, "data": json.dumps(data or {}) if as_text else (data or {})}
    fire(app, "coffee_command", payload, user_id)


def replay(app, tape, start=None):
    """Plays `tape` [(seconds from start, step)] in order, the clock running between steps."""
    start = app.now if start is None else start
    for offset, step in sorted(tape, key=lambda item: item[0]):
        if start + offset > app.now:
            advance(app, start + offset - app.now)
        kind = step[0]
        if kind == "state":
            set_entity(app, *step[1:])
        elif kind == "event":
            fire(app, *step[1:])
        elif kind == "op":
            op(app, *step[1:])
        elif kind == "call":
            step[1](app)


def last(app, entity):
    state, attributes, _ = app.published[entity]
    return state, attributes


def ledger(app):
    return json.loads((app.dir / "coffee_feedback.json").read_text())["cycles"]


def event_state(app, kind, **attrs):
    return ("state", "event.xbloom_studio_brew_event", iso(app.now), {"event_type": kind, **attrs})


def synthetic_brew(brew_id, power_rows=None, start_at=8.0, bloom_at=28.0, enjoy_at=235.0, pickup_at=300.0,
                   ours=True):
    """A 4-pour brew the way the integration reports one over a held Connect session: our start (or a press on
    the machine), the grinder, the bloom and pours, BREW_END, ENJOY and the completion, then the cup lifted.
    Cup and water follow the usual's plan; `power_rows` (real plug readings) replay the plug from the
    grinder on."""
    tape = []
    pours = [(0, 45), (32, 80), (75, 65), (110, 65)]
    if ours:
        tape.append((0, ("op", "start", {"variant": "usual"}, TABLET)))
    tape.append((1, ("state", "switch.xbloom_studio_connect", "on")))
    tape.append((3, ("event", "xbloom_connect_ready", {})))
    context = {"context": {"coffee_brew_id": brew_id}} if ours else {}
    if ours:
        tape.append((6, ("event", "xbloom_brew_started", {"recipe_name": "4 Pour Recipe", "total_pours": 4,
                                                           "run_id": "r1", **context})))
    tape.append((start_at, ("state", "sensor.xbloom_studio_brew_status", "grinding")))
    for second in range(3, int(pickup_at) + 30, 2):
        tape.append((second + 0.5, ("call", lambda app: set_entity(app, "sensor.xbloom_studio_status_updated",
                                                                   iso(app.now)))))
    for i, step in enumerate(range(int(start_at), int(start_at) + 14)):
        tape.append((step + 0.2, ("state", "sensor.xbloom_studio_scale_weight", f"{min(15.0, i * 1.2):.1f}")))
    tape.append((bloom_at - 1, ("state", "sensor.xbloom_studio_scale_weight", "0.0")))
    tape.append((bloom_at - 1, ("state", "sensor.xbloom_studio_water_dispensed", "0.0")))
    for index, (at, ml) in enumerate(pours):
        tape.append((bloom_at + at, ("call", lambda app, index=index: set_entity(
            app, "event.xbloom_studio_brew_event", iso(app.now), {"event_type": "pour_started", "pour_index": index}))))
    tape.append((bloom_at + 0.1, ("state", "sensor.xbloom_studio_brew_status", "brewing")))
    total = 0.0
    water_at = {}
    for at, ml in pours:
        for s in range(int(math.ceil(ml / 3.0)) + 1):
            water_at[bloom_at + at + s] = min(total + 3.0 * s, total + ml)
        total += ml
    level, levels = 0.0, {}
    for t in range(int(bloom_at), int(enjoy_at) + 1):
        if t in water_at:
            level = water_at[t]
        levels[t] = level
        tape.append((t + 0.3, ("state", "sensor.xbloom_studio_water_dispensed", f"{level:.1f}")))
        cup = min(217.4, 0.853 * levels.get(t - 6, 0.0))
        tape.append((t + 0.6, ("state", "sensor.xbloom_studio_scale_weight", f"{cup:.1f}")))
    tape.append((bloom_at + 145, ("call", lambda app: set_entity(app, "event.xbloom_studio_brew_event", iso(app.now),
                                                                 {"event_type": "brew_ended"}))))
    tape.append((enjoy_at, ("state", "sensor.xbloom_studio_scale_weight", "217.4")))
    tape.append((enjoy_at + 0.1, ("call", lambda app: set_entity(app, "event.xbloom_studio_brew_event", iso(app.now),
                                                                 {"event_type": "brew_done", "recipe_name": ""}))))
    tape.append((enjoy_at + 0.2, ("state", "sensor.xbloom_studio_brew_status", "done")))
    tape.append((enjoy_at + 1, ("event", "xbloom_brew_completed", {
        "recipe_id": "1234567", "recipe_name": "4 Pour Recipe", "dose_g": 15.0, "cup_type": 1, "outcome": "confirmed",
        "duration_s": int(enjoy_at - bloom_at), "run_id": "r1", **context})))
    tape.append((pickup_at, ("state", "sensor.xbloom_studio_scale_weight", "0.4")))
    if power_rows:
        first = power_rows[0][0]
        grinder = next(ts for ts, value in power_rows if 12 <= float(value) < 100)
        for ts, value in power_rows:
            offset = start_at + ts - grinder
            if offset >= 0.5:
                tape.append((offset, ("state", "sensor.pour_over_plug_power", value)))
        del first
    return tape
