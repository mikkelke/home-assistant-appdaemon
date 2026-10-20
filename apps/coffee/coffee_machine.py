"""CoffeeMachine - the xBloom Studio in the house: one owner for each brew's lifecycle, its 1 Hz curve,
the brew ledger, the usual's baseline, the beans container, the Bluetooth link, the cup on the scale, who
made it and who is told, the maintenance counts and the advisor. Publishes sensor.coffee_state, _live,
_baseline, _beans, _brews, _recipes and _advice (contract v1, see coffee_publish.py) and the files under
/media/coffee.

Inputs: the plug (sensor.pour_over_plug_power), the xBloom integration's entities and bus events, the
dashboard's coffee_command events (script.coffee_command fires them in the caller's context: the HA
user is data.metadata.context.user_id), the ready push's rating buttons, and Bermuda and presence for
who. A value from a state callback is never replaced by a later read: AppDaemon 4.5.13's store can lag.

Files: coffee_feedback.json (the ledger, one record per brew that ground; HouseUpcoming counts its
maintenance runs from it), coffee_state.json (container, usual, advice, link suppression and the
brew in flight, saved every CHECKPOINT_S), /media/coffee/brews/<id>.json (each brew's 1 Hz curve),
/media/coffee/bands/<baseline id>.json and /media/coffee/recent.json (60 days at 2 s with the bands
the brews were compared with; read by the dashboard through shell_command.coffee_recent).

A brew in flight survives a restart when the checkpoint is under RESUME_MAX_S old and the machine is
still awake; otherwise it is closed as stopped, abandoned. HA restarts wipe set_state entities, so
plugin_started republishes all seven.
"""

import asyncio
import json
import os
import re
from datetime import datetime, time, timezone
from pathlib import Path

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import coffee_advice as ca
import coffee_baseline as cb
import coffee_beans as cbeans
import coffee_connect as cconn
import coffee_cup as ccup
import coffee_fsm as cf
import coffee_plan as cp
import coffee_power as cpow
import coffee_publish as pub
import coffee_who as cw
import feedback_io  # apps/appliances - AppDaemon puts every app dir on sys.path

STATE_VERSION = 1
LEDGER_VERSION = 1
CHECKPOINT_S = 10
HOUSE_TICK_S = 10
GUESS_S = 30
WHO_AFTER_S = 30
CONNECT_WAIT_S = 15
CONNECT_REPEAT_S = 30
START_CALL_TIMEOUT_S = 40
SERVICE_TIMEOUT_S = 20
ADVISOR_TIMEOUT_S = 60
RECIPES_DELAY_S = 20
PENDING_NOTIFY_S = 1800
TABLET_IDLE_S = 120
FRAME_AGE_S = 60
WAKE_CHECK_S = 5
WAKE_NUDGES = 2
RESTORED = (None, "unavailable", "unknown")
VARIANTS = ("usual", "base", "tweak", "other")
PEOPLE = cw.PEOPLE
OPS = ("start", "cup_confirmed", "cancel", "stop", "pause", "resume", "connect", "notify", "picked_up", "rate",
       "beans_add", "beans_undo", "set_usual", "trial_accept", "trial_undo", "trial_keep", "trial_revert",
       "advice_dismiss", "advisor_reply")
RATE_PREFIX = "COFFEE_RATE|"
ENTITIES = ("coffee_state", "coffee_live", "coffee_baseline", "coffee_beans", "coffee_brews", "coffee_recipes",
            "coffee_advice")


def ascii_text(value, limit=300):
    return str(value).encode("ascii", "replace").decode("ascii")[:limit]


def command_data(raw):
    """The `data` of a coffee_command, a dict whether HA handed it over parsed or as JSON text; None when
    it is neither."""
    if isinstance(raw, dict):
        return raw
    if raw in (None, ""):
        return {}
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return None
        return parsed if isinstance(parsed, dict) else None
    return None


def event_user(data):
    meta = (data or {}).get("metadata") if isinstance(data, dict) else None
    context = meta.get("context") if isinstance(meta, dict) else None
    user = context.get("user_id") if isinstance(context, dict) else None
    return str(user).replace("-", "").lower() if user else None


def number_in(value, low, high, whole=False):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not low <= number <= high:
        return None
    if whole:
        return int(round(number)) if abs(number - round(number)) < 1e-6 else None
    return number


def clock(ts):
    """m:ss of a duration in seconds."""
    ts = int(round(ts))
    return f"{ts // 60}:{ts % 60:02d}"


def response_data(envelope, *path):
    """The service response under result.response of AppDaemon's envelope, followed down `path`."""
    if not isinstance(envelope, dict) or not envelope.get("success"):
        return None
    node = (envelope.get("result") or {}).get("response")
    for key in path:
        node = node.get(key) if isinstance(node, dict) else None
    return node


class CoffeeMachine(hass.Hass):
    def initialize(self):
        a = self.args
        here = Path(__file__).resolve().parent
        self.power_entity = a.get("power_entity", "sensor.pour_over_plug_power")
        self.plug_entity = a.get("plug_entity", "switch.pour_over_plug")
        self.connect_entity = a.get("connect_entity", "switch.xbloom_studio_connect")
        self.heartbeat_entity = a.get("heartbeat_entity", "sensor.xbloom_studio_status_updated")
        self.scale_entity = a.get("scale_entity", "sensor.xbloom_studio_scale_weight")
        self.water_entity = a.get("water_entity", "sensor.xbloom_studio_water_dispensed")
        self.event_entity = a.get("event_entity", "event.xbloom_studio_brew_event")
        self.status_entity = a.get("status_entity", "sensor.xbloom_studio_brew_status")
        self.paused_entity = a.get("paused_entity", "binary_sensor.xbloom_studio_brew_paused")
        self.fault_entity = a.get("fault_entity", "sensor.xbloom_studio_machine_status")
        self.pour_entity = a.get("pour_entity", "sensor.xbloom_studio_current_pour")
        self.recipe_select = a.get("recipe_select", "select.xbloom_studio_recipe")
        self.usual_name = a.get("usual_name", "4 Pour Recipe")
        self.tablet_user = str(a.get("tablet_user_id") or "").replace("-", "").lower()
        self.tablet_notify = a.get("tablet_notify", "notify/mobile_app_oppopad4pro")
        self.tablet_interactive = a.get("tablet_interactive", "binary_sensor.oppopad4pro_interactive")
        self.bermuda_distance = a.get("bermuda_distance")
        self.bermuda_area = a.get("bermuda_area")
        self.bermuda_person = a.get("bermuda_person", "person.mikkel")
        self.person_entities = list(a.get("person_entities") or ["person.mikkel", "person.kristine", "person.claudia"])
        self.sole_occupancy = bool(a.get("sole_occupancy", True))
        self.advisor_entity = str(a.get("advisor_entity") or "").strip()
        self.push_link = a.get("push_link", "/dashboard-rooftop/home#room=kitchen")
        self.media_dir = Path(a.get("media_dir") or "/media/coffee")
        self.ledger_file = Path(a.get("ledger_file") or here / "coffee_feedback.json")
        self.state_file = Path(a.get("state_file") or here / "coffee_state.json")

        self._notifier = self.get_app("MobileNotifier")
        self._handoff = {}
        self._published = {}
        self._live_timer = None
        self._ledger_stamp = None
        self._records = []
        self._guess = (0.0, None)
        self._pending_start = None
        self._connect_asked = None
        self._link_ready = False
        self._cup_timer = None
        self._waking = None

        saved = self._load_state()
        self._beans = cbeans.Beans(saved.get("beans"))
        self._advice = ca.Advice(saved.get("advice"), enabled=bool(self.advisor_entity))
        self._connect = cconn.ConnectPolicy(saved.get("connect"))
        self._usual_id = saved.get("usual_id")
        self._pending_notify = saved.get("pending_notify") if isinstance(saved.get("pending_notify"), dict) else None
        self._recipes = [r for r in (saved.get("recipes") or []) if isinstance(r, dict) and r.get("pours")]
        self._baseline = saved.get("baseline") if isinstance(saved.get("baseline"), dict) else None
        self._tracker = cf.Tracker.from_dict(saved.get("tracker"))
        self._cup = ccup.Cup()
        self._power = cpow.PowerBrew()
        self._power.restore(saved.get("power"))
        self._meter = cpow.MachineMeter()
        self._tracks = {"distance": cw.Track(), "area": cw.Track()}
        self._values = {}

        now = self._now()
        for entity in [self.plug_entity, self.connect_entity, self.status_entity, self.paused_entity, self.fault_entity,
                       self.pour_entity, self.bermuda_person, self.tablet_interactive] + self.person_entities:
            self._values[entity] = self._read(entity)
        self._meter.plug_on = (self._state_of(self.plug_entity) or "on") == "on"
        power = self._read(self.power_entity)
        self._meter.update(now, (power or {}).get("state"))
        if self._machine(now) in ("asleep", "off"):
            self._cup.asleep()
        for key, entity in (("distance", self.bermuda_distance), ("area", self.bermuda_area)):
            if entity:
                full = self._read(entity)
                if full:
                    self._tracks[key].add(now, full.get("state"))

        self.listen_state(self._on_power, self.power_entity)
        self.listen_state(self._on_value, self.plug_entity)
        self.listen_event(self._on_connect_change, "state_changed", entity_id=self.connect_entity)
        self.listen_state(self._on_frame, self.heartbeat_entity)
        self.listen_state(self._on_scale, self.scale_entity)
        self.listen_state(self._on_water, self.water_entity)
        self.listen_state(self._on_brew_event, self.event_entity, attribute="all")
        self.listen_state(self._on_brew_status, self.status_entity)
        self.listen_state(self._on_paused, self.paused_entity)
        self.listen_state(self._on_fault, self.fault_entity)
        self.listen_state(self._on_value, self.pour_entity, attribute="all")
        self.listen_state(self._on_recipe_options, self.recipe_select, attribute="options")
        for entity in dict.fromkeys([self.bermuda_person, self.tablet_interactive] + self.person_entities):
            self.listen_state(self._on_value, entity, attribute="all")
        for entity in (self.bermuda_distance, self.bermuda_area):
            if entity:
                self.listen_state(self._on_bermuda, entity)
        for event in ("xbloom_brew_started", "xbloom_brew_completed", "xbloom_brew_failed", "xbloom_brew_stopped",
                      "xbloom_brew_timeout", "xbloom_machine_brew_started", "xbloom_connect_ready",
                      "xbloom_connect_failed", "xbloom_connect_stopped", "xbloom_connect_auto_stopped"):
            self.listen_event(self._on_xbloom, event)
        self.listen_event(self._on_command, "coffee_command")
        self.listen_event(self._on_push_action, "mobile_app_notification_action")
        self.listen_event(self._on_plugin_started, "plugin_started")
        self.run_every(self._on_house_tick, f"now+{HOUSE_TICK_S}", HOUSE_TICK_S)
        self.run_daily(self._on_midnight, time(0, 5))
        self.run_in(self._refresh_recipes, RECIPES_DELAY_S)

        self._ensure_files()
        self._load_ledger(force=True)
        self._resume(now)
        if self._switch_on():
            self._link_ready = True
            self._connect.activity(now)
        if self._machine(now) == "working":
            self._connect_trigger("init_working", now)
        self._recompute_baseline(now)
        self._write_recent(now)
        self._publish_all(force=True)
        self._save_state()
        self._info(f"CoffeeMachine initialized - {len(self._records)} brews on record, {len(self._recipes)} recipes, "
                 f"state {self._tracker.phase() or 'none'}, advisor {'on' if self.advisor_entity else 'off'}")

    def terminate(self):
        self._save_state()

    # -- small readers ---------------------------------------------------------------------------

    def _now(self):
        return self.get_now_ts()

    def _now_dt(self):
        return datetime.fromtimestamp(self._now(), timezone.utc)

    def _read(self, entity):
        if not entity:
            return None
        try:
            full = self.get_state(entity, attribute="all")
        except Exception as e:
            self._warn(f"Read of {entity} failed: {e!r}")
            return None
        return full if isinstance(full, dict) else None

    def _state_of(self, entity):
        return (self._values.get(entity) or {}).get("state")

    def _warn(self, message):
        self.log(ascii_text(message), level="WARNING")

    def _info(self, message):
        self.log(ascii_text(message))

    def _switch_on(self):
        return self._state_of(self.connect_entity) == "on"

    def _machine(self, now):
        return self._meter.state(now)

    def _link(self, now):
        """A held session is silent while the machine idles, so the link is the Connect switch and the
        integration's connect events; only a running brew is judged by its frames, which a brew on its own
        link sends too."""
        if self._tracker.phase() in cf.RUNNING:
            if self._tracker.fresh(now):
                return "connected"
            return "connecting" if self._switch_on() and not self._link_ready else "not_connected"
        if self._switch_on():
            return "connected" if self._link_ready else "connecting"
        return "not_connected"

    def _usual(self):
        return cp.by_id(self._recipes, self._usual_id)

    def _person_for(self, user_id, at=None):
        """The resident whose HA account `user_id` is, through ActorAttribution; None for anyone else."""
        if not user_id:
            return None
        verdict = self._attribution("coffee_command", at, user_id)
        return verdict.get("person") if verdict.get("method") == "dashboard_action" else None

    def _attribution(self, event, at=None, user_id=None):
        try:
            app = self.get_app("ActorAttribution")
        except Exception:
            app = None
        if app is None:
            return {}
        when = datetime.fromtimestamp(at, timezone.utc) if at is not None else None
        try:
            verdict = app.attribute(event, at=when, user_id=user_id)
        except Exception as e:
            self._warn(f"ActorAttribution failed: {e!r}")
            return {}
        return verdict if isinstance(verdict, dict) else {}

    # -- inputs ------------------------------------------------------------------------------------

    def _on_value(self, entity, attribute, old, new, kwargs):
        if isinstance(new, dict):
            self._values[entity] = new
        else:
            self._values[entity] = {**(self._values.get(entity) or {}), "state": new,
                                    "last_changed": cf.iso(self._now())}
        if entity == self.plug_entity:
            now = self._now()
            machine_before = self._machine(now)
            self._meter.plug_on = new == "on"
            self._machine_moved(machine_before, now)
            self._publish_state()
        elif entity == self.pour_entity and self._tracker.brew is not None:
            total = cpow.watts(((new if isinstance(new, dict) else {}).get("attributes") or {}).get("total_pours"))
            if total and not self._tracker.brew.get("pours_total"):
                self._tracker.brew["pours_total"] = int(total)

    def _on_power(self, entity, attribute, old, new, kwargs):
        now = self._now()
        machine_before = self._machine(now)
        self._meter.update(now, new)
        for event in self._power.feed(now, new):
            self._apply(self._tracker.power(event))
        machine = self._machine(now)
        if machine == "working" and machine_before != "working" and cpow.watts(old) is not None:
            self._connect_trigger("power_rise", now)
        self._machine_moved(machine_before, now)
        if machine != machine_before:
            self._publish_state()

    def _machine_moved(self, before, now):
        """The machine zeroes its scale when it wakes; asleep or off it shows nothing."""
        machine = self._machine(now)
        if machine in ("asleep", "off") and before not in ("asleep", "off"):
            self._cup_event(self._cup.asleep(), now, "the machine sleeps")
        elif machine not in ("asleep", "off") and before in ("asleep", "off"):
            self._waking = None
            self._cup_event(self._cup.zero(), now, "the machine woke")

    def _on_connect_change(self, event_name, data, kwargs):
        new_state = (data or {}).get("new_state") or {}
        old_state = (data or {}).get("old_state") or {}
        now = self._now()
        self._values[self.connect_entity] = new_state
        user = ((new_state.get("context") or {}).get("user_id") or "").replace("-", "").lower()
        if new_state.get("state") != "on":
            self._link_ready = False
            self._cup_event(self._cup.unseen(), now, "the link is off")
        if new_state.get("state") == "off" and old_state.get("state") == "on":
            if user and self._person_for(user) is not None:
                self._connect.person_off(now)
                self._info(f"Connect switched off by {self._person_for(user)}: no automatic connect for "
                         f"{cconn.SUPPRESS_S // 60} min")
        elif new_state.get("state") == "on":
            self._connect.activity(now)
        self._publish_state()

    def _on_frame(self, entity, attribute, old, new, kwargs):
        now = self._now()
        stamp = cf.epoch(new)
        if stamp is None or abs(now - stamp) > FRAME_AGE_S:
            return
        was = self._link(now)
        self._tracker.frame(now)
        if self._link(now) != was:
            self._publish_state()

    def _on_scale(self, entity, attribute, old, new, kwargs):
        grams = cpow.watts(new)
        if grams is None or old in RESTORED:
            return
        now = self._now()
        was = self._link(now)
        self._apply(self._tracker.reading_cup(now, grams))
        self._cup_event(self._cup.reading(now, grams), now)
        self._cup_settle_later()
        if self._link(now) != was:
            self._publish_state()

    def _on_water(self, entity, attribute, old, new, kwargs):
        ml = cpow.watts(new)
        if ml is not None and old not in RESTORED:
            self._tracker.reading_water(self._now(), ml)

    def _on_brew_event(self, entity, attribute, old, new, kwargs):
        new = new if isinstance(new, dict) else {}
        attrs = new.get("attributes") or {}
        kind = attrs.get("event_type")
        now = self._now()
        when = cf.epoch(new.get("state"))
        if when is None or now - when > FRAME_AGE_S:
            return
        when = min(when, now)
        self._tracker.frame(now)
        if kind == "pour_started":
            index = cpow.watts(attrs.get("pour_index"))
            if index is not None:
                self._apply(self._tracker.pour(when, int(index)))
        elif kind == "brew_started":
            self._apply(self._tracker.bloom(when))
        elif kind == "brew_ended":
            self._tracker.brew_end(when)
        elif kind == "brew_done":
            self._apply(self._tracker.enjoy(when))

    def _on_brew_status(self, entity, attribute, old, new, kwargs):
        now = self._now()
        self._values[entity] = {"state": new}
        if new == "grinding":
            self._apply(self._tracker.grinding_seen(now, "machine"))
        elif new == "brewing":
            self._apply(self._tracker.bloom(now))
        elif new == "done":
            self._apply(self._tracker.enjoy(now))
        elif new == "idle" and old in ("grinding", "brewing"):
            self._apply(self._tracker.status_idle(now))

    def _on_paused(self, entity, attribute, old, new, kwargs):
        self._values[entity] = {"state": new}
        if new in ("on", "off"):
            self._apply(self._tracker.paused(new == "on", self._now()))

    def _on_fault(self, entity, attribute, old, new, kwargs):
        self._values[entity] = {"state": new}
        self._apply(self._tracker.fault(new, self._now()))
        self._publish_state()

    def _on_bermuda(self, entity, attribute, old, new, kwargs):
        key = "distance" if entity == self.bermuda_distance else "area"
        self._tracks[key].add(self._now(), new)

    def _on_xbloom(self, event_name, data, kwargs):
        data = data if isinstance(data, dict) else {}
        now = self._now()
        context = data.get("context") if isinstance(data.get("context"), dict) else {}
        brew_id = context.get("coffee_brew_id")
        run_id = data.get("run_id")
        if event_name == "xbloom_brew_started":
            self._apply(self._tracker.accepted(brew_id, run_id, data.get("recipe_name"), now))
        elif event_name == "xbloom_brew_failed":
            self._apply(self._tracker.refused(brew_id, run_id, data.get("reason"), now))
        elif event_name == "xbloom_brew_completed":
            self._apply(self._tracker.completed(data, now))
        elif event_name == "xbloom_brew_stopped":
            self._apply(self._tracker.stopped(brew_id, run_id, data.get("by"), bool(data.get("ground")), now))
        elif event_name == "xbloom_brew_timeout":
            if self._tracker.phase() in cf.RUNNING and (brew_id is None or brew_id == self._tracker.brew["id"]):
                self._apply(self._tracker.abandon(now))
        elif event_name == "xbloom_machine_brew_started":
            self._apply(self._tracker.grinding_seen(now, "machine"))
        elif event_name == "xbloom_connect_ready":
            self._link_ready = True
            self._connect.connected(now)
            self._send_pending_start()
            self._nudge(now)
            self._publish_state()
        elif event_name == "xbloom_connect_failed":
            self._link_ready = False
            self._connect.failed(now)
            self._send_pending_start()
            self._cup_event(self._cup.unseen(), now, "the link failed")
            self._publish_state()
        elif event_name in ("xbloom_connect_stopped", "xbloom_connect_auto_stopped"):
            self._link_ready = False
            self._cup_event(self._cup.unseen(), now, "the link stopped")
            self._publish_state()

    def _on_plugin_started(self, event_name, data, kwargs):
        self._publish_all(force=True)

    def _on_recipe_options(self, entity, attribute, old, new, kwargs):
        if old != new:
            self._refresh_recipes({})

    # -- timers ------------------------------------------------------------------------------------

    def _on_house_tick(self, kwargs):
        now = self._now()
        for event in self._power.tick(now):
            self._apply(self._tracker.power(event))
        if not self._tracker.busy():
            self._apply(self._tracker.tick(now))
        action = self._connect.tick(now, self._switch_on(), self._tracker.busy(), self._trigger_holds)
        if action:
            self._set_connect(action == "on")
        if self._load_ledger():
            self._publish("coffee_brews")
        if self._tracker.busy():
            self._save_state()
        self._publish_state()

    def _on_live_tick(self, kwargs):
        now = self._now()
        effects = self._tracker.tick(now)
        brew = self._tracker.brew
        if brew is not None and brew["phase"] == "brewing" and self._tracker.curve is not None:
            second = int(now - self._tracker.curve.t0)
            if second % CHECKPOINT_S == 0 or brew.get("verdict") is None:
                base = brew.get("baseline")
                cup = self._tracker.curve.cup[-1] if self._tracker.curve.cup else None
                verdict = cb.verdict(base, second, cup)
                if verdict is not None and verdict != brew.get("verdict"):
                    brew["verdict"] = verdict
                    effects.append("state")
        self._apply(effects)
        if not self._tracker.busy():
            self._stop_live_timer()

    def _start_live_timer(self):
        if self._live_timer is None:
            self._live_timer = self.run_every(self._on_live_tick, "now+1", 1)

    def _stop_live_timer(self):
        if self._live_timer is not None:
            try:
                self.cancel_timer(self._live_timer)
            except Exception as e:
                self._warn(f"Cancelling the live timer failed: {e!r}")
            self._live_timer = None

    def _on_midnight(self, kwargs):
        self._write_recent(self._now())
        self._publish("coffee_brews", force=True)

    # -- effects ------------------------------------------------------------------------------------

    def _apply(self, effects):
        if not effects:
            return
        brew = self._tracker.brew
        for effect in dict.fromkeys(effects):
            if effect == "started":
                self._on_started(brew)
            elif effect == "source" and brew and brew["source"] == "machine":
                self._wake_tablet(brew)
            elif effect == "ready":
                self._on_ready(brew)
            elif effect == "closed":
                self._on_closed(brew)
            elif effect == "start_failed":
                self._on_start_failed(brew)
            elif effect == "picked_up":
                self._upsert_record(brew, picked_up_at=cf.iso(brew.get("picked_up_ts")))
                self._connect.activity(self._now())
                self._cup_note(self._cup.lifted(), "picked up")
            elif effect == "expired":
                self._pending_start = None
            elif effect == "dropped":
                self._pending_start = None
                self._info(f"Start {self._tracker.dropped[0]} dropped: {self._tracker.dropped[1]}")
            elif effect == "fault":
                self._fault_push(brew)
        if "live" in effects or "ready" in effects or "closed" in effects or "expired" in effects:
            self._publish("coffee_live", force=True)
        if "state" in effects:
            self._publish_state()
        if self._tracker.busy():
            self._start_live_timer()
        if any(effect != "live" for effect in effects):
            self._save_state()

    def _on_started(self, brew):
        now = self._now()
        self._connect.activity(now)
        self._cup_note(self._cup.zero(), "a brew started")
        if brew.get("recipe") is None:
            recipe = cp.by_name(self._recipes, brew.get("recipe_name"))
            usual = self._usual()
            if recipe is not None:
                brew["recipe"], brew["recipe_match"] = recipe, "inferred"
                brew["variant"] = "usual" if usual and recipe["id"] == usual["id"] else "other"
            else:
                brew["recipe_match"], brew["variant"] = "unknown", brew.get("variant") or "unknown"
        recipe = brew.get("recipe")
        usual = self._usual()
        if recipe is not None:
            brew["recipe_key"] = cp.recipe_key(recipe)
            brew["pours_total"] = brew.get("pours_total") or len(recipe["pours"])
        if brew.get("dose_g") is None:
            if recipe is not None and brew.get("recipe_match") == "sent":
                brew["dose_g"], brew["dose_basis"] = recipe["dose_g"], "sent"
            elif usual is not None:
                brew["dose_g"], brew["dose_basis"] = usual["dose_g"], "usual"
        brew["bean"] = self._beans.bean()
        if brew.get("variant") in ("usual", "base", "trial", "tweak"):
            brew["baseline"] = cb.frozen(self._baseline)
        self._take_pending_chip(brew, now)
        self._resolve_who(brew, final=False)
        self.run_in(self._on_who_final, WHO_AFTER_S, brew_id=brew["id"])
        if brew["source"] == "machine":
            self._wake_tablet(brew)
        self._info(f"Brew {brew['id']} started: source {brew['source']}, "
                 f"{(recipe or {}).get('name') or brew.get('recipe_name') or 'unknown recipe'}, "
                 f"variant {brew.get('variant')}, who {brew.get('who')}")

    def _take_pending_chip(self, brew, now):
        pending, self._pending_notify = self._pending_notify, None
        if pending and "chip" not in brew and now - (cf.epoch(pending.get("at")) or 0) <= PENDING_NOTIFY_S:
            brew["chip"] = pending.get("person")

    def _on_who_final(self, kwargs):
        brew = self._tracker.brew
        if brew is not None and brew["id"] == kwargs.get("brew_id"):
            before = (brew.get("who"), brew.get("notify"))
            self._resolve_who(brew, final=True)
            if (brew.get("who"), brew.get("notify")) != before:
                self._publish_state()
                self._save_state()

    def _resolve_who(self, brew, final):
        t0 = brew.get("requested_ts") or brew.get("started_ts") or self._now()
        start_person = self._person_for(brew.get("user_id"), t0) if brew.get("user_id") else None
        bermuda_person = None
        if self.bermuda_distance and brew.get("who_method") not in ("dashboard_action", "chip"):
            person_state = self._track_person_state()
            confident, evidence = cw.bermuda(person_state, self._tracks["distance"], self._tracks["area"], t0)
            brew["who_evidence"] = evidence
            if confident:
                bermuda_person = "mikkel"
        sole = None
        if self.sole_occupancy and self._anyone_home():
            verdict = self._attribution("coffee_start", t0)
            if verdict.get("method") == "sole_occupant":
                sole = verdict.get("person")
        who, method = cw.resolve(start_person, brew.get("chip"), bermuda_person, sole, self.sole_occupancy)
        brew["who"], brew["who_method"] = who, method
        brew["notify"] = cw.notify_target(who, brew.get("chip"))
        if final:
            brew["who_final"] = True

    def _track_person_state(self):
        return self._state_of(self.bermuda_person)

    def _anyone_home(self):
        return any(self._state_of(entity) == "home" for entity in self.person_entities)

    def _on_start_failed(self, brew):
        self._pending_start = None
        if brew.get("trial"):
            self._advice.trial_finished(brew["id"], complete=False)
            self._publish("coffee_advice")
        self._connect.activity(self._now())
        self._info(f"Brew {brew['id']} did not start: {brew.get('start_error')}")

    def _finalize(self, brew):
        """Totals, band and deltas of a brew that is over, from its curve."""
        curve = self._tracker.curve
        end = brew.get("ready_ts") or brew.get("stopped_ts") or self._now()
        final = {}
        if curve is not None:
            final = curve.totals(brew.get("enjoy_ts") or end, brew.get("brew_end_ts"))
            if brew.get("phase") == "ready":
                brew["in_band"] = cb.in_band(brew.get("baseline"), curve.cup)
            final["paused_s"] = curve.paused_s()
        completed = brew.get("completed") or {}
        if "total_s" not in final and isinstance(completed.get("duration_s"), (int, float)):
            final["total_s"] = int(completed["duration_s"])
        brew["final"] = final
        brew["deltas"] = cb.deltas(final, brew.get("baseline")) if brew.get("phase") == "ready" else {}

    def _on_ready(self, brew):
        now = self._now()
        if brew["source"] != "power":
            self._cup_note(self._cup.put_on(), "the brew is ready")
        self._finalize(brew)
        self._write_curve(brew)
        self._debit(brew)
        if brew.get("trial"):
            self._advice.trial_finished(brew["id"], complete=True)
        push = self._ready_push(brew)
        self._upsert_record(brew)
        self._connect.activity(now)
        self._recompute_baseline(now)
        self._write_recent(now)
        self._publish("coffee_brews", force=True)
        self._publish("coffee_beans")
        self._publish("coffee_advice")
        if push:
            self.create_task(self._send_push(*push))
        self._house_event(brew)
        final = brew["final"]
        self._info(f"Brew {brew['id']} ready: {final.get('cup_g')} g, {final.get('total_s')} s, coverage "
                 f"{final.get('coverage')}, notify {brew.get('notify')}")

    def _on_closed(self, brew):
        now = self._now()
        self._pending_start = None
        self._finalize(brew)
        if brew.get("trial"):
            self._advice.trial_finished(brew["id"], complete=False)
        self._connect.activity(now)
        if brew.get("started_ts") is None:
            self._info(f"Brew {brew['id']} stopped before it started")
            return
        if brew.get("bloom_ts"):
            self._write_curve(brew)
        self._debit(brew)
        self._upsert_record(brew)
        self._write_recent(now)
        self._publish("coffee_brews", force=True)
        self._publish("coffee_beans")
        self._publish("coffee_advice")
        self._info(f"Brew {brew['id']} stopped: {brew.get('stop_reason')}, ground {bool(brew.get('ground'))}")

    def _debit(self, brew):
        if brew.get("ground") and brew.get("dose_g"):
            if self._beans.debit(brew["id"], brew["dose_g"], self._now_dt()):
                brew["debited_g"] = brew["dose_g"]

    # -- the ledger --------------------------------------------------------------------------------

    def _ensure_files(self):
        for path in (self.media_dir / "brews", self.media_dir / "bands"):
            try:
                path.mkdir(parents=True, exist_ok=True)
            except OSError as e:
                self._warn(f"Could not create {path}: {e!r}")
        if not self.ledger_file.exists():
            try:
                feedback_io.write_json(str(self.ledger_file), {"version": LEDGER_VERSION, "cycles": []})
            except OSError as e:
                self._warn(f"Could not create {self.ledger_file.name}: {e!r}")

    def _load_ledger(self, force=False):
        """Reads the ledger again once the file has changed; True when it did."""
        try:
            stat = self.ledger_file.stat()
        except OSError:
            return False
        stamp = (stat.st_mtime_ns, stat.st_size)
        if not force and stamp == self._ledger_stamp:
            return False
        try:
            data = json.loads(self.ledger_file.read_text())
            cycles = data.get("cycles") if isinstance(data, dict) else None
        except Exception as e:
            self._warn(f"Could not read {self.ledger_file.name}: {e!r}")
            self._ledger_stamp = stamp
            return False
        self._records = [r for r in cycles or [] if isinstance(r, dict) and r.get("id")]
        self._ledger_stamp = stamp
        return True

    def _record_fields(self, brew):
        recipe = brew.get("recipe")
        final = brew.get("final") or {}
        end_ts = brew.get("ready_ts") or brew.get("stopped_ts") or brew.get("phase_ts")
        start_ts = brew.get("started_ts") or brew.get("requested_ts") or end_ts
        outcome = brew.get("outcome") or "stopped"
        record = {
            "id": brew["id"], "ts": cf.iso(end_ts), "duration_min": round(max(0.0, end_ts - start_ts) / 60, 2),
            "completion_class": "complete" if outcome in ("complete", "unobserved") else "interrupted",
            "outcome": outcome, "source": brew.get("source"), "run_id": brew.get("run_id"),
            "started_at": cf.iso(start_ts), "first_bloom_at": cf.iso(brew.get("bloom_ts")),
            "brew_end_at": cf.iso(brew.get("brew_end_ts")), "enjoy_at": cf.iso(brew.get("enjoy_ts")),
            "picked_up_at": cf.iso(brew.get("picked_up_ts")),
            "recipe": None if recipe is None else {
                "id": recipe.get("id"), "name": recipe.get("name"), "key": brew.get("recipe_key"),
                "dose_g": recipe.get("dose_g"), "ratio": recipe.get("ratio"), "grind": recipe.get("grind"),
                "rpm": recipe.get("rpm"), "cup_type": recipe.get("cup_type"), "pours": recipe.get("pours")},
            "recipe_match": brew.get("recipe_match") or "unknown", "variant": brew.get("variant") or "unknown",
            "trial": brew.get("trial"), "bean": brew.get("bean"), "dose_g": brew.get("dose_g"),
            "dose_basis": brew.get("dose_basis"), "debited_g": brew.get("debited_g", 0),
            "who": brew.get("who") or "none", "who_method": brew.get("who_method"),
            "who_evidence": brew.get("who_evidence"), "notify": brew.get("notify") or "none",
            "push": brew.get("push") or "none", "faults": brew.get("faults") or [],
            "stop_reason": brew.get("stop_reason"), "baseline_id": (brew.get("baseline") or {}).get("id"),
            "deltas": brew.get("deltas") or {}, "in_band": brew.get("in_band"),
        }
        for key in ("total_s", "drawdown_s", "cup_g", "water_ml", "coverage", "paused_s"):
            if final.get(key) is not None:
                record[key] = final[key]
        return {k: v for k, v in record.items() if v is not None}

    def _upsert_record(self, brew=None, brew_id=None, **fields):
        """Writes the brew's record (or `fields` onto the record `brew_id`) into the ledger file, read again
        first so a record added beside the app (the backfill) is kept."""
        history = feedback_io.load_for_append(str(self.ledger_file), log=lambda m, level="INFO": self._warn(m))
        if history is None:
            return None
        history["version"] = LEDGER_VERSION
        cycles = history["cycles"]
        target = brew["id"] if brew is not None else brew_id
        index = next((i for i, r in enumerate(cycles) if isinstance(r, dict) and r.get("id") == target), None)
        if brew is not None and index is None:
            record = {**self._record_fields(brew), **fields}
            cycles.append(record)
        elif index is not None:
            record = cycles[index]
            if brew is not None:
                record.update({k: v for k, v in self._record_fields(brew).items() if k not in ("rating", "rated_by",
                                                                                              "rated_at")})
            record.update(fields)
        else:
            return None
        cycles.sort(key=lambda r: r.get("started_at") or r.get("ts") or "")
        try:
            feedback_io.write_json(str(self.ledger_file), history)
        except Exception as e:
            self._warn(f"Ledger write failed: {e!r}")
            return None
        self._load_ledger(force=True)
        return record

    # -- files under /media/coffee -----------------------------------------------------------------

    def _write_json(self, path, data):
        """Compact JSON, replacing the file in one step."""
        tmp = Path(f"{path}.tmp")
        try:
            tmp.write_text(json.dumps(data, separators=(",", ":")))
            os.replace(tmp, path)
            return True
        except Exception as e:
            self._warn(f"Write of {path} failed: {e!r}")
            return False

    def _read_json(self, path):
        try:
            return json.loads(Path(path).read_text())
        except FileNotFoundError:
            return None
        except Exception as e:
            self._warn(f"Read of {path} failed: {e!r}")
            return None

    def _curve_path(self, brew_id):
        return self.media_dir / "brews" / f"{brew_id}.json"

    def _write_curve(self, brew):
        curve = self._tracker.curve
        if curve is None:
            return
        self._write_json(self._curve_path(brew["id"]), curve.file_data(brew["id"]))
        base = brew.get("baseline")
        if base and base.get("id") and cb.ready(base):
            path = self.media_dir / "bands" / f"{base['id']}.json"
            if not path.exists():
                self._write_json(path, pub.band_doc({**base, "plan_water": self._plan_of(brew)["water"]}))

    def _curves(self, ids):
        out = {}
        for brew_id in ids:
            if not re.fullmatch(r"c\d+", str(brew_id)):
                continue
            data = self._read_json(self._curve_path(brew_id))
            if isinstance(data, dict):
                out[brew_id] = data
        return out

    def _write_recent(self, now):
        since = now - pub.BREWS_DAYS * 86400
        recent = [r for r in self._records if (cf.epoch(r.get("started_at")) or 0) >= since]
        curves = self._curves([r["id"] for r in recent if r.get("first_bloom_at")])
        bands = {}
        for baseline_id in {r.get("baseline_id") for r in recent if r.get("baseline_id")}:
            if re.fullmatch(r"bl[0-9a-f]+", baseline_id):
                band = self._read_json(self.media_dir / "bands" / f"{baseline_id}.json")
                if isinstance(band, dict):
                    bands[baseline_id] = band
        self._write_json(self.media_dir / "recent.json", pub.recent_doc(recent, curves, bands, now))

    # -- the baseline ------------------------------------------------------------------------------

    def _plan_of(self, brew=None):
        recipe = (brew or {}).get("recipe") if brew else self._usual()
        if not recipe:
            return {"water": [], "pours": None, "total_s": None}
        return {"water": cp.plan_water(recipe), "pours": cp.plan_pours(recipe), "total_s": cp.plan_total_s(recipe)}

    def _recompute_baseline(self, now):
        usual = self._usual()
        if usual is None and self._recipes:
            match = cp.by_name(self._recipes, self.usual_name)
            if match is not None:
                self._usual_id = match["id"]
                usual = match
        bean = self._beans.bean()
        if usual is None:
            self._baseline = cb.none_baseline(cf.iso(now))
        else:
            key = cp.recipe_key(usual)
            members = cb.cohort(self._records, key, (bean or {}).get("id"))
            curves = self._curves([r["id"] for r in members])
            self._baseline = cb.compute(self._records, curves, usual, key, (bean or {}).get("id"), self._plan_of(),
                                        cf.iso(now))
        self._publish("coffee_baseline")

    # -- the start ---------------------------------------------------------------------------------

    def _op_start(self, user, data, now):
        if self._tracker.active():
            self._info(f"Start ignored: a brew is {self._tracker.phase()}")
            return
        variant = data.get("variant") or "usual"
        if variant not in VARIANTS:
            self._warn(f"Start ignored: variant {variant!r}")
            return
        usual = self._usual()
        recipe = cp.by_id(self._recipes, data.get("recipe_id")) if variant == "other" else usual
        overrides = {}
        if variant == "tweak":
            for key, low, high, whole in (("dose", 5, 25, False), ("ratio", 5, 25, False), ("grind_size", 1, 80, True)):
                if data.get(key) is not None:
                    value = number_in(data.get(key), low, high, whole)
                    if value is None:
                        self._warn(f"Start ignored: {key} {data.get(key)!r}")
                        return
                    overrides[key] = value
        trial = None
        grind = self._advice.trial_grind(variant)
        if grind is not None and usual is not None:
            overrides, variant, trial = {"grind_size": int(grind)}, "trial", self._advice.trial_text()
        brew_id = cf.brew_id_for(now)
        if self._tracker.last and self._tracker.last.get("id") == brew_id:
            brew_id = cf.brew_id_for(now + 1)
        source = "tablet" if user and user == self.tablet_user else "phone"
        fields = {"user_id": user, "variant": variant, "recipe_match": "sent", "trial": trial}
        if recipe is not None:
            used = cp.with_overrides(recipe, overrides.get("dose"), overrides.get("ratio"), overrides.get("grind_size"))
            fields.update(recipe=used, recipe_name=recipe["name"], dose_g=used["dose_g"], dose_basis="sent")
        waiting = recipe is not None and self._cup.state != "present"
        effects = (self._tracker.await_cup if waiting else self._tracker.request)(brew_id, now, source, **fields)
        if self._tracker.brew is not None and self._tracker.brew["id"] == brew_id:
            self._take_pending_chip(self._tracker.brew, now)
            self._resolve_who(self._tracker.brew, final=False)
        self._apply(effects)
        if recipe is None:
            self._apply(self._tracker.refused(brew_id, None, "recipe_not_found", now))
            return
        service = {"recipe_name": recipe["name"], "context": {"coffee_brew_id": brew_id}, **overrides}
        self._pending_start = {"brew_id": brew_id, "service": service, "sent": False}
        if waiting:
            self._info(f"Start {brew_id}: {recipe['name']} waits for a cup (cup {self._cup.state})")
            self._wake_machine(now)
            return
        self._go(now)

    def _go(self, now):
        """The start leaves: over the link at once when it is up, else once it is or CONNECT_WAIT_S pass."""
        brew, pending = self._tracker.brew, self._pending_start
        if brew is None or brew["phase"] != "starting" or not pending or pending["brew_id"] != brew["id"]:
            return
        if brew.get("trial"):
            self._advice.trial_started(brew["id"])
            self._publish("coffee_advice")
        self._info(f"Start {brew['id']}: {brew.get('recipe_name')}, variant {brew.get('variant')}, "
                   f"from the {brew['source']}")
        if self._switch_on() and self._link_ready:
            self._send_pending_start()
            return
        self._connect_trigger("op_start", now)
        self.run_in(self._on_connect_wait, CONNECT_WAIT_S, brew_id=brew["id"])

    def _on_connect_wait(self, kwargs):
        if self._pending_start and self._pending_start["brew_id"] == kwargs.get("brew_id"):
            self._send_pending_start()

    def _send_pending_start(self):
        pending = self._pending_start
        if not pending or pending["sent"] or self._tracker.phase() != "starting":
            return
        if self._tracker.brew["id"] != pending["brew_id"]:
            return
        pending["sent"] = True
        self.create_task(self._call_start(pending["brew_id"], pending["service"]))

    async def _call_start(self, brew_id, service):
        answer = None
        try:
            envelope = await asyncio.wait_for(self.call_service("xbloom/start_brew", return_response=True, **service),
                                              timeout=START_CALL_TIMEOUT_S)
            if isinstance(envelope, dict) and envelope.get("success"):
                answer = "duplicate" if response_data(envelope, "duplicate") else "ok"
            else:
                answer = "no_answer"
        except Exception as e:
            answer = f"error {e!r}"
        self._handoff[f"start:{brew_id}"] = answer
        await self.run_in(self._on_start_answer, 0, brew_id=brew_id)

    def _on_start_answer(self, kwargs):
        brew_id = kwargs.get("brew_id")
        answer = self._handoff.pop(f"start:{brew_id}", None)
        if answer == "ok" or self._tracker.phase() != "starting" or self._tracker.brew["id"] != brew_id:
            return
        self._warn(f"Start {brew_id}: start_brew answered {answer}")
        reason = "machine_busy" if answer == "duplicate" else "no_answer"
        self._apply(self._tracker.refused(brew_id, None, reason, self._now()))

    # -- the link ----------------------------------------------------------------------------------

    def _trigger_holds(self, kind):
        now = self._now()
        if kind in ("power_rise", "init_working"):
            return self._machine(now) == "working"
        if kind == "op_start":
            return self._tracker.phase() == "starting"
        return self._machine(now) != "off"

    def _connect_trigger(self, kind, now):
        if self._connect.trigger(kind, now, self._switch_on()) == "on":
            self._set_connect(True)

    def _wake_machine(self, now):
        """Takes the link; a sleeping machine is nudged awake over it, up to WAKE_NUDGES times WAKE_CHECK_S apart.
        Connecting usually wakes it, and back_to_home does when the connect alone did not."""
        self._connect_trigger("op_connect", now)
        if self._machine(now) == "asleep":
            self._waking = {"left": WAKE_NUDGES}
            if self._switch_on() and self._link_ready:
                self._nudge(now)

    def _nudge(self, now):
        waking = self._waking
        if waking is None:
            return
        if self._machine(now) != "asleep" or waking["left"] <= 0 or not (self._switch_on() and self._link_ready):
            self._waking = None
            return
        waking["left"] -= 1
        self._info("Nudging the machine awake")
        self._service("xbloom/back_to_home")
        self.run_in(self._on_wake_check, WAKE_CHECK_S)

    def _on_wake_check(self, kwargs):
        self._nudge(self._now())

    def _set_connect(self, on):
        now = self._now()
        asked = self._connect_asked
        if not on and asked and not asked[0] and now - asked[1] < CONNECT_REPEAT_S:
            return
        self._connect_asked = (on, now)
        self._info(f"Connect {'on' if on else 'off'}")
        self._service("switch/turn_on" if on else "switch/turn_off", entity_id=self.connect_entity)

    def _service(self, service, **data):
        self.create_task(self._call_service(service, data))

    async def _call_service(self, service, data):
        try:
            envelope = await asyncio.wait_for(self.call_service(service, **data), timeout=SERVICE_TIMEOUT_S)
        except Exception as e:
            self.log(ascii_text(f"{service} failed: {e!r}"), level="WARNING")
            return None
        if isinstance(envelope, dict) and envelope.get("success") is False:
            self.log(ascii_text(f"{service} was refused: {(envelope.get('error') or {}).get('message')}"),
                     level="WARNING")
        return envelope

    # -- the cup -------------------------------------------------------------------------------------

    def _cup_note(self, changed, why=None):
        if changed:
            grams = self._cup.grams
            self._info(f"Cup {self._cup.state}: {why}" if why else f"Cup {self._cup.state} at {grams:.1f} g")
        return changed

    def _cup_event(self, changed, now, why=None):
        """A change of the cup's state outside a brew's effects. A start that waited for a cup goes out once one is
        present."""
        if not self._cup_note(changed, why):
            return
        self._publish_state()
        self._save_state()
        if self._cup.state == "present" and self._tracker.phase() == "awaiting_cup":
            self._apply(self._tracker.cup_on(now))
            self._go(now)

    def _cup_settle_later(self):
        due = self._cup.due()
        if due is not None and self._cup_timer is None:
            self._cup_timer = self.run_in(self._on_cup_settle, max(0.1, due - self._now()))

    def _on_cup_settle(self, kwargs):
        self._cup_timer = None
        now = self._now()
        self._cup_event(self._cup.settle(now), now)
        self._cup_settle_later()

    # -- who is told ---------------------------------------------------------------------------------

    def _ready_push(self, brew):
        """(brew id, person, title, message, data) of the ready push, None when nobody is to be told."""
        person = brew.get("notify")
        if person not in PEOPLE or brew["source"] == "power" or not (brew.get("enjoy_ts") or brew.get("completed")):
            brew["push"] = "none"
            return None
        final = brew.get("final") or {}
        parts = []
        if final.get("cup_g") is not None:
            parts.append(f"{int(round(final['cup_g']))} g")
        if final.get("total_s"):
            parts.append(clock(final["total_s"]))
        label = {"usual": "the usual", "base": "the usual", "tweak": "tweaked",
                 "trial": f"trial grind {(brew.get('recipe') or {}).get('grind')}"}.get(brew.get("variant"))
        label = label or ((brew.get("recipe") or {}).get("name") if brew.get("variant") == "other" else None)
        if label:
            parts.append(label)
        actions = [{"action": f"{RATE_PREFIX}{brew['id']}|{taste}", "title": taste.capitalize()}
                   for taste in ca.TASTES]
        return (brew["id"], person, "Coffee's ready", ", ".join(parts) or "Your coffee is ready",
                {"data": {"tag": f"coffee_{brew['id']}", "actions": actions}})

    async def _send_push(self, brew_id, person, title, message, data):
        delivered = 0
        try:
            delivered = await self._notifier.notify(title=title, message=message, target=[person], data=data,
                                                    link=self.push_link)
        except Exception as e:
            self.log(ascii_text(f"Ready push failed: {e!r}"), level="WARNING")
        self._handoff[f"push:{brew_id}"] = delivered or 0
        await self.run_in(self._on_push_sent, 0, brew_id=brew_id)

    def _on_push_sent(self, kwargs):
        brew_id = kwargs.get("brew_id")
        delivered = self._handoff.pop(f"push:{brew_id}", 0)
        push = "sent" if delivered else "failed"
        brew = self._tracker.brew
        if brew is not None and brew["id"] == brew_id:
            brew["push"] = push
            self._publish_state()
        self._upsert_record(brew_id=brew_id, push=push)
        self._info(f"Ready push for {brew_id}: {push}")

    def _fault_push(self, brew):
        if brew is None or "no_water" not in brew.get("faults", []) or brew.get("fault_pushed"):
            return
        if brew["phase"] not in cf.RUNNING or brew.get("notify") not in PEOPLE:
            return
        brew["fault_pushed"] = True
        self.create_task(self._send_fault_push(brew["notify"]))

    async def _send_fault_push(self, person):
        try:
            await self._notifier.notify(title="Coffee: no water", message="The xBloom reports its water tank is empty.",
                                        target=[person], data={"data": {"tag": "coffee_fault"}}, link=self.push_link)
        except Exception as e:
            self.log(ascii_text(f"Fault push failed: {e!r}"), level="WARNING")

    def _house_event(self, brew):
        payload = {"icon": "mdi:coffee", "text": "Coffee ready"}
        cup = (brew.get("final") or {}).get("cup_g")
        if cup is not None:
            payload["effect"] = f"{int(round(cup))} g"
        name = cw.display_name(brew.get("who"))
        if name:
            payload["by"] = name
        try:
            self.fire_event("house_events_report", **payload)
        except Exception as e:
            self._warn(f"house_events_report failed: {e!r}")

    def _wake_tablet(self, brew):
        if brew.get("woke_tablet"):
            return
        full = self._values.get(self.tablet_interactive) or {}
        state = full.get("state")
        since = cf.epoch(full.get("last_changed"))
        if state != "off" or since is None or self._now() - since < TABLET_IDLE_S:
            return
        brew["woke_tablet"] = True
        self._info(f"Waking the tablet for {brew['id']}")
        self.create_task(self._wake(brew["id"]))

    async def _wake(self, brew_id):
        for data in ({"message": "command_screen_on"},
                     {"message": "command_webview",
                      "data": {"command": f"/dashboard-rooftop/home#coffee&takeover={brew_id}"}}):
            await self._call_service(self.tablet_notify, data)

    # -- commands ------------------------------------------------------------------------------------

    def _on_command(self, event_name, data, kwargs):
        data = data if isinstance(data, dict) else {}
        op = data.get("op")
        payload = command_data(data.get("data"))
        user = event_user(data)
        if op not in OPS or payload is None:
            self._warn(f"coffee_command ignored: op {ascii_text(op, 40)!r}")
            return
        now = self._now()
        self._connect.activity(now)
        handler = getattr(self, f"_op_{op}")
        handler(user, payload, now)

    def _op_stop(self, user, data, now):
        if self._tracker.phase() == "awaiting_cup":
            self._op_cancel(user, data, now)
        elif self._tracker.active():
            self._apply(self._tracker.stop_asked(now))
            self._service("xbloom/stop_brew")

    def _op_cancel(self, user, data, now):
        effects = self._tracker.drop_wait(now, "cancelled")
        if not effects:
            self._info(f"Cancel ignored: the brew is {self._tracker.phase() or 'none'}")
            return
        self._apply(effects)

    def _op_cup_confirmed(self, user, data, now):
        if self._tracker.phase() != "awaiting_cup" or self._cup.state == "absent":
            self._info(f"Cup confirmation ignored: brew {self._tracker.phase() or 'none'}, cup {self._cup.state}")
            return
        self._cup.put_on()
        self._cup_event(True, now, "confirmed")

    def _op_pause(self, user, data, now):
        if self._tracker.phase() in ("grinding", "brewing"):
            self._service("xbloom/brew_pause")

    def _op_resume(self, user, data, now):
        if self._tracker.phase() == "paused":
            self._service("xbloom/brew_resume")

    def _op_connect(self, user, data, now):
        self._wake_machine(now)

    def _op_notify(self, user, data, now):
        person = data.get("person")
        if person not in PEOPLE and person != "none":
            self._warn(f"notify ignored: {ascii_text(person, 40)!r}")
            return
        brew = self._tracker.brew
        if brew is not None and brew["phase"] in cf.BUSY:
            brew["chip"] = person
            self._resolve_who(brew, final=brew.get("who_final", False))
            if brew["phase"] == "ready":
                self._upsert_record(brew)
        else:
            self._pending_notify = {"person": person, "at": cf.iso(now)}
        self._publish_state()
        self._save_state()

    def _op_picked_up(self, user, data, now):
        self._apply(self._tracker.picked_up(data.get("brew_id"), now))

    def _op_rate(self, user, data, now):
        self._rate(data.get("brew_id"), data.get("taste"), self._person_for(user, now), now)

    def _rate(self, brew_id, taste, person, now):
        if taste not in ca.TASTES or not isinstance(brew_id, str):
            self._warn(f"Rating ignored: {ascii_text(brew_id, 40)!r} {ascii_text(taste, 20)!r}")
            return
        record = self._upsert_record(brew_id=brew_id, rating=taste, rated_by=person or "none", rated_at=cf.iso(now))
        if record is None:
            self._warn(f"Rating ignored: no brew {ascii_text(brew_id, 40)}")
            return
        self._info(f"Brew {brew_id} rated {taste}")
        self._publish("coffee_brews", force=True)
        self._maybe_ask(now)

    def _on_push_action(self, event_name, data, kwargs):
        action = (data or {}).get("action") if isinstance(data, dict) else None
        if not isinstance(action, str) or not action.startswith(RATE_PREFIX):
            return
        parts = action.split("|")
        if len(parts) != 3:
            self._warn(f"Rating action ignored: {ascii_text(action, 60)}")
            return
        now = self._now()
        record = next((r for r in self._records if r.get("id") == parts[1]), None)
        person = self._person_for(event_user(data), now) or (record or {}).get("notify")
        self._rate(parts[1], parts[2], person if person in PEOPLE else None, now)

    def _op_beans_add(self, user, data, now):
        op_id = self._beans.add(data.get("grams"), self._now_dt(), bean_id=data.get("bean_id"),
                                new_name=data.get("new_name"))
        if op_id is None:
            self._warn(f"beans_add ignored: {ascii_text(data, 120)}")
            return
        self._advice.bean_changed(self._beans.bean_id)
        self._info(f"Beans: added {data.get('grams')} g of {(self._beans.bean() or {}).get('name')}")
        self._after_beans(now)

    def _op_beans_undo(self, user, data, now):
        if not self._beans.undo(data.get("op_id"), self._now_dt()):
            self._warn(f"beans_undo ignored: {ascii_text(data.get('op_id'), 40)}")
            return
        self._info("Beans: add undone")
        self._after_beans(now)

    def _after_beans(self, now):
        self._recompute_baseline(now)
        self._publish("coffee_beans")
        self._publish("coffee_brews", force=True)
        self._publish("coffee_advice")
        self._save_state()

    def _op_set_usual(self, user, data, now):
        recipe = cp.by_id(self._recipes, data.get("recipe_id"))
        if recipe is None:
            self._warn(f"set_usual ignored: {ascii_text(data.get('recipe_id'), 40)}")
            return
        self._usual_id = recipe["id"]
        self._info(f"Usual is now {recipe['name']}")
        self._recompute_baseline(now)
        self._publish("coffee_recipes")
        self._publish("coffee_beans")
        self._save_state()

    def _op_trial_accept(self, user, data, now):
        self._advice_op(self._advice.accept(data.get("advice_id")), "trial_accept")

    def _op_trial_undo(self, user, data, now):
        self._advice_op(self._advice.undo(data.get("advice_id")), "trial_undo")

    def _op_trial_revert(self, user, data, now):
        self._advice_op(self._advice.revert(data.get("advice_id")), "trial_revert")

    def _op_advice_dismiss(self, user, data, now):
        self._advice_op(self._advice.dismiss(data.get("advice_id")), "advice_dismiss")

    def _advice_op(self, done, op):
        if not done:
            self._warn(f"{op} ignored in advice state {self._advice.state}")
            return
        self._info(f"Advice: {op}")
        self._publish("coffee_advice")
        self._save_state()

    def _op_advisor_reply(self, user, data, now):
        who = self._person_for(user, now)
        if not self._advice.reply(data.get("advice_id"), data.get("text"), who, cf.iso(now)):
            self._warn("advisor_reply ignored")
            return
        self._ask(now)

    def _op_trial_keep(self, user, data, now):
        grind = self._advice.keep(data.get("advice_id"))
        usual = self._usual()
        if grind is None or usual is None:
            self._warn("trial_keep ignored")
            return
        self.create_task(self._keep_grind(usual["name"], int(grind)))

    async def _keep_grind(self, name, grind):
        error = None
        try:
            envelope = await asyncio.wait_for(self.call_service("xbloom/get_recipe", name=name, return_response=True),
                                              timeout=SERVICE_TIMEOUT_S)
            recipe = response_data(envelope, "recipe")
            if not isinstance(recipe, dict):
                raise ValueError("recipe not found")
            recipe = {**recipe, "grind_size": grind, "grinder_size": grind}
            envelope = await asyncio.wait_for(
                self.call_service("xbloom/update_recipe", recipe_json=json.dumps(recipe), return_response=True),
                timeout=SERVICE_TIMEOUT_S)
            answer = response_data(envelope) or {}
            if not answer.get("ok"):
                raise ValueError(answer.get("error") or "update refused")
        except Exception as e:
            error = repr(e)
        self._handoff["keep"] = error
        await self.run_in(self._on_kept, 0)

    def _on_kept(self, kwargs):
        error = self._handoff.pop("keep", None)
        if error:
            self._advice.keep_failed(error)
            self._warn(f"trial_keep failed: {error}")
        else:
            self._advice.kept()
            self._info("Advice: trial kept, the usual's grind is changed")
            self._refresh_recipes({})
        self._publish("coffee_advice")
        self._save_state()

    # -- the advisor ---------------------------------------------------------------------------------

    def _maybe_ask(self, now):
        usual = self._usual()
        bean = self._beans.bean()
        if usual is None or bean is None:
            return
        evidence = ca.rated_evidence(self._records, bean["id"], cp.recipe_key(usual))
        if self._advice.should_ask(evidence):
            self._ask(now)

    def _ask(self, now):
        usual = self._usual()
        bean = self._beans.bean()
        if usual is None or bean is None or not self.advisor_entity:
            return
        evidence = ca.rated_evidence(self._records, bean["id"], cp.recipe_key(usual))
        base = {k: self._baseline.get(k) for k in ("n", "total_s_median", "cup_g_median", "drawdown_s_median")
                if (self._baseline or {}).get(k) is not None}
        request = self._advice.asking(evidence, usual, bean, base, cf.iso(now), usual.get("grind"))
        advice_id = f"a{int(now)}"
        self._publish("coffee_advice")
        self._save_state()
        self.create_task(self._call_advisor(advice_id, request))

    async def _call_advisor(self, advice_id, request):
        data, error = None, None
        try:
            envelope = await asyncio.wait_for(
                self.call_service("ai_task/generate_data", entity_id=self.advisor_entity, return_response=True,
                                  hass_timeout=ADVISOR_TIMEOUT_S, **request),
                timeout=ADVISOR_TIMEOUT_S + 5)
            data = response_data(envelope, "data")
            if not isinstance(data, dict):
                error = "no answer" if not isinstance(envelope, dict) or envelope.get("success") is not False else \
                    str((envelope.get("error") or {}).get("message") or "refused")
        except Exception as e:
            error = repr(e)
        self._handoff[f"advice:{advice_id}"] = (data, error)
        await self.run_in(self._on_advice, 0, advice_id=advice_id)

    def _on_advice(self, kwargs):
        advice_id = kwargs.get("advice_id")
        data, error = self._handoff.pop(f"advice:{advice_id}", (None, "lost"))
        if self._advice.state != "thinking":
            return
        if error:
            self._advice.failed(error)
            self._warn(f"Advisor failed: {error}")
        else:
            state = self._advice.answered(data, cf.iso(self._now()), advice_id)
            self._info(f"Advisor answered: {state}")
        self._publish("coffee_advice")
        self._save_state()

    # -- recipes -------------------------------------------------------------------------------------

    def _refresh_recipes(self, kwargs):
        self.create_task(self._fetch_recipes())

    async def _fetch_recipes(self):
        recipes = None
        try:
            envelope = await asyncio.wait_for(self.call_service("xbloom/list_recipes", return_response=True),
                                              timeout=SERVICE_TIMEOUT_S)
            recipes = response_data(envelope, "recipes")
        except Exception as e:
            self.log(ascii_text(f"Recipe list failed: {e!r}"), level="WARNING")
        self._handoff["recipes"] = recipes
        await self.run_in(self._on_recipes, 0)

    def _on_recipes(self, kwargs):
        raw = self._handoff.pop("recipes", None)
        if not isinstance(raw, list):
            return
        recipes = [r for r in (cp.normalize(item) for item in raw) if r is not None]
        if recipes == self._recipes:
            return
        self._recipes = recipes
        if self._usual() is None:
            match = cp.by_name(recipes, self.usual_name)
            self._usual_id = match["id"] if match else self._usual_id
        self._info(f"Recipes: {len(recipes)} in the library, usual {(self._usual() or {}).get('name') or 'none'}")
        self._recompute_baseline(self._now())
        self._publish("coffee_recipes")
        self._publish("coffee_beans")
        self._save_state()

    # -- restart -------------------------------------------------------------------------------------

    def _resume(self, now):
        brew = self._tracker.brew
        if brew is None:
            return
        age = now - (cf.epoch(self._saved_at) or 0)
        phase = brew["phase"]
        if phase == "awaiting_cup":
            self._apply(self._tracker.abandon(now))
            return
        if phase in cf.ACTIVE:
            if age < cf.RESUME_MAX_S and self._machine(now) in ("awake", "working"):
                self._info(f"Brew {brew['id']} resumed after a restart ({int(age)} s)")
                self._start_live_timer()
                return
            self._info(f"Brew {brew['id']} closed after a restart ({int(age)} s, machine {self._machine(now)})")
            self._apply(self._tracker.abandon(now))
            return
        self._apply(self._tracker.tick(now))

    # -- publishing ----------------------------------------------------------------------------------

    def _publish_all(self, force=False):
        for key in ENTITIES:
            if key == "coffee_state":
                self._publish_state(force=force)
            else:
                self._publish(key, force=force)

    def _eta(self, brew, now):
        if brew is None or not brew.get("bloom_ts") or brew["phase"] not in ("grinding", "brewing"):
            return None, None
        curve = self._tracker.curve
        paused = curve.paused_s(now) if curve is not None else 0
        plan_total = cp.plan_total_s(brew["recipe"]) if brew.get("recipe") else None
        return cb.eta(brew["bloom_ts"], brew.get("baseline"), plan_total, paused)

    def _notify_guess(self, now):
        if self._tracker.brew is not None:
            return None
        person = None
        if self.bermuda_distance:
            confident, _ = cw.bermuda(self._track_person_state(), self._tracks["distance"], self._tracks["area"], now)
            person = "mikkel" if confident else None
        if person is None and self.sole_occupancy and self._anyone_home():
            verdict = self._attribution("coffee_guess")
            person = verdict.get("person") if verdict.get("method") == "sole_occupant" else None
        return person or "none"

    def _publish_state(self, force=False):
        now = self._now()
        brew = self._tracker.brew
        eta, basis = self._eta(brew, now)
        pending = None
        if self._pending_notify and now - (cf.epoch(self._pending_notify.get("at")) or 0) <= PENDING_NOTIFY_S:
            pending = self._pending_notify.get("person")
        guess = None
        if brew is None and self._machine(now) in ("awake", "working"):
            if now - self._guess[0] >= GUESS_S:
                self._guess = (now, self._notify_guess(now))
            guess = self._guess[1]
        state, attrs = pub.state_entity(brew, self._tracker.last, now, self._machine(now), self._meter.watts,
                                        self._link(now), self._tracker.frame_at, self._state_of(self.fault_entity),
                                        guess, pending, (brew or {}).get("verdict"), eta, basis, self._cup.state)
        compare = {k: v for k, v in attrs.items() if k not in ("power_w", "link_seen_at")}
        if not force and self._published.get("coffee_state") == (state, compare):
            return
        self._published["coffee_state"] = (state, compare)
        self._set("sensor.coffee_state", state, attrs)

    def _publish(self, key, force=False):
        now = self._now()
        if key == "coffee_live":
            state, attrs = self._live_payload(now)
        elif key == "coffee_baseline":
            state, attrs = pub.baseline_entity(self._baseline)
        elif key == "coffee_beans":
            usual = self._usual()
            state, attrs = pub.beans_entity(self._beans.view(usual["dose_g"] if usual else None,
                                                             self._now_dt()))
        elif key == "coffee_brews":
            bean = self._beans.bean()
            changed = self._ledger_stamp[0] / 1e9 if self._ledger_stamp else None
            state, attrs = pub.brews_entity(self._records, now, (bean or {}).get("id"), self._last_curve(), changed)
        elif key == "coffee_recipes":
            state, attrs = pub.recipes_entity([cp.recipe_entry(r) for r in self._recipes], self._usual())
        else:
            state, attrs = pub.advice_entity(self._advice.view((self._beans.bean() or {}).get("id")))
        if not force and self._published.get(key) == (state, attrs):
            return
        self._published[key] = (state, attrs)
        self._set(f"sensor.{key}", state, attrs)

    def _live_payload(self, now):
        brew = self._tracker.brew
        if brew is not None and brew["phase"] in ("grinding", "brewing", "paused") and brew["source"] != "power":
            plan = self._plan_of(brew)
            return pub.live_entity(brew, self._tracker.curve, now, self._tracker.fresh(now), self._tracker.frame_at,
                                   plan["water"], plan["pours"])
        last = brew if brew is not None else self._tracker.last
        if last is not None and last.get("bloom_ts") and last.get("source") != "power":
            ended = self._read_json(self._curve_path(last["id"]))
            if isinstance(ended, dict):
                plan = self._plan_of(last)
                return pub.live_entity(None, None, now, False, None, plan["water"], plan["pours"], ended=ended)
        return pub.live_entity(None, None, now, False, None, [], None)

    def _last_curve(self):
        for record in reversed(self._records):
            if record.get("outcome") == "complete" and record.get("first_bloom_at"):
                data = self._read_json(self._curve_path(record["id"]))
                return (data or {}).get("cup") or None
        return None

    def _set(self, entity, state, attrs):
        try:
            self.set_state(entity, state=state, attributes=attrs, replace=True)
        except Exception as e:
            self._warn(f"Publish of {entity} failed: {e!r}")

    # -- persistence ---------------------------------------------------------------------------------

    def _load_state(self):
        self._saved_at = None
        try:
            data = json.loads(self.state_file.read_text())
        except FileNotFoundError:
            return {}
        except Exception as e:
            self._warn(f"Could not read {self.state_file.name}: {e!r}")
            return {}
        if not isinstance(data, dict) or data.get("version") != STATE_VERSION:
            return {}
        self._saved_at = data.get("saved_at")
        return data

    def _save_state(self):
        data = {"version": STATE_VERSION, "saved_at": cf.iso(self._now()), "usual_id": self._usual_id,
                "beans": self._beans.to_dict(), "advice": self._advice.to_dict(), "connect": self._connect.to_dict(),
                "pending_notify": self._pending_notify, "recipes": self._recipes, "baseline": self._baseline,
                "tracker": self._tracker.to_dict(), "power": self._power.snapshot()}
        try:
            feedback_io.write_json(str(self.state_file), data)
        except Exception as e:
            self._warn(f"State file write failed: {e!r}")
