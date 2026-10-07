"""Dishwasher signal **only**: while the dishwasher is Unemptied **and** the kitchen is occupied, the island
shows a green signal. Everywhere else ``light.island_lights`` belongs to ``FamilyRoomLights`` in its normal
layout (AL main on, AL SG off, island light 1 manual control released). This app is the **only** place that
uses ``island_lights_sg`` + ``island_light_1`` for the signal.

- **Bright** (family room confirmed bright): full ``light.island_lights`` green, both AL switches off.
- **Dark**: ``light.island_lights`` off; ``light.island_lights_sg`` on (normal AL) + ``light.island_light_1`` green (manual).

Prefer **in-place color/power** via ``turn_on`` (brightness + ``hs_color``) when lights are already on - avoid ``turn_off``/``turn_on`` churn when switching dishwasher *mode* or when the island was already lit by family room lighting. Hue/ZHA often map ``rgb_color`` to ``color_temp`` in HA state while the lamp still looks chromatic; ``hs_color`` keeps entity state aligned with what you see.

**Hand-back** (kitchen presence clears while Unemptied, or the dishwasher leaves Unemptied; see
``_hand_back_island``): the app never powers an island light on and only powers one off when it powered the
full group up from off itself in the bright path. Any other lit island bulb is recolored to normal with
``adaptive_lighting/apply`` (``turn_on_lights=False``) and left to ``FamilyRoomLights``, which alone decides
on/off. The AL layout restore and recolor run ``_HAND_BACK_SETTLE_S`` seconds later: AL only adapts lights
HA reports as on, and HA's state for a Zigbee group trails a command, so adapting right after
``FamilyRoomLights`` switched the island off would switch it back on.

**Unemptied with no kitchen presence** is the normal layout: nothing is applied and no light is touched.

**Startup:** if the AL switches are still in a signal layout (anything other than main-on/SG-off) while the
kitchen is empty or the dishwasher is not Unemptied, the same hand-back runs, so a missed transition or AD
restart cannot leave island light 1 green.

**Presence trust:** kitchen mmWave-only presence while the kitchen speaker plays is SUSPECT
(``presence_trust.py``) - the signal is never *applied* on ghost presence (no green lights for
nobody), but an already-applied signal is held untouched: suspect still counts as presence for
the hold, and the real end paths (composite off / leaving Unemptied) are unchanged.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import presence_trust
import room_state_darkness

# Delay before the hand-back touches AL; see the module docstring.
_HAND_BACK_SETTLE_S = 3


class DishwasherIslandSignal(hass.Hass):
    """Dishwasher signal scenario only - SG + bulb 1 are not used by FamilyRoomLights."""

    def initialize(self):
        self._dishwasher = self.args["dishwasher_state_entity"]
        self._pir = self.args["kitchen_pir_entity"]
        self._signal_light = self.args["signal_light_entity"]
        self._full_island = self.args["full_island_light_entity"]
        self._island_sg_light = self.args.get("island_sg_light_entity")
        self._room_state = self.args.get("room_state_text_entity")
        self._al_main = self.args["adaptive_lighting_main_switch"]
        self._al_sg = self.args["adaptive_lighting_sg_switch"]
        self._unemptied = str(self.args.get("unemptied_option") or "Unemptied")
        self._brightness = int(self.args.get("signal_brightness_pct") or 100)
        # Saturated green in HS space; avoids rgb->CT mismatch on Hue (HA "warm" while bulb still looks green).
        hs = self.args.get("signal_hs")
        if hs is not None:
            self._hs = [int(hs[0]), int(hs[1])]
        else:
            self._hs = [120, 100]
        # True only while the full group is on because _apply_bright_full_signal powered it up from off. The
        # hand-back switches the group off again in that case only; a group that was already lit (family room
        # lighting) is never turned off by this app.
        self._island_powered_by_signal = False
        # presence_trust duration knob (minutes); None -> helper default.
        self._suspect_after_minutes = self.args.get("presence_suspect_after_minutes")

        self.listen_state(self._on_dishwasher_state, self._dishwasher)
        self.listen_state(self._on_pir, self._pir)
        if self._room_state:
            try:
                self.listen_state(self._on_room_state, self._room_state)
                self.listen_state(self._on_room_state, self._room_state, attribute="pending_target")
            except Exception as e:
                self.log(f"room state listener failed: {e}", level="WARNING")

        self.run_in(self._startup_sync, 3)

    def _startup_sync(self, _kwargs):
        try:
            self._sync_signal()
            # Missed Unemptied->Off (HA restart, AD down) leaves AL in dishwasher mode while state is already Off.
            if not self._is_unemptied() and not self._al_is_normal_not_unemptied():
                self.log(
                    "startup: dishwasher not Unemptied but AL not main-on/SG-off - reconciling island signal",
                    level="WARNING",
                )
                self._sync_signal(leaving_unemptied=True)
        except Exception as e:
            self.log(f"startup sync failed: {e}", level="ERROR")

    def _is_unemptied(self):
        st = self.get_state(self._dishwasher)
        if st is None or st in ("unknown", "unavailable"):
            return False
        return str(st).strip() == self._unemptied

    def _pir_on(self):
        return self.get_state(self._pir) == "on"

    def _kitchen_presence_suspect(self):
        """Ghost check on the raw kitchen composite (see presence_trust.py). Never raises."""
        try:
            return presence_trust.presence_suspect(
                self,
                "kitchen",
                self._pir,
                suspect_after_minutes=getattr(self, "_suspect_after_minutes", None),
            ).suspect
        except Exception:
            return False

    def _is_family_room_bright(self):
        if not self._room_state:
            return False
        return room_state_darkness.is_confirmed_bright(
            self,
            self._room_state,
            self.args.get("darkness_confirmed_sensor_entity"),
            default_when_unknown=False,
        )

    def _al_is_normal_not_unemptied(self):
        """Expected AL layout when dishwasher is not signalling (see yaml: main on, SG off)."""
        try:
            return (
                self.get_state(self._al_main) == "on"
                and self.get_state(self._al_sg) == "off"
            )
        except Exception:
            return False

    def _signal_light_turn_on_kwargs(self):
        return {"brightness_pct": self._brightness, "hs_color": self._hs}

    def _on_dishwasher_state(self, entity, attribute, old, new, kwargs):
        try:
            u = str(self._unemptied).strip()
            o = str(old).strip() if old is not None else ""
            n = str(new).strip() if new is not None else ""
            leaving_unemptied = (o == u) and (n != u)
            self._sync_signal(leaving_unemptied=leaving_unemptied)
        except Exception as e:
            self.log(f"dishwasher state handler: {e}", level="ERROR")

    def _on_pir(self, entity, attribute, old, new, kwargs):
        try:
            if self._is_unemptied():
                self._sync_signal()
        except Exception as e:
            self.log(f"pir handler: {e}", level="ERROR")

    def _on_room_state(self, entity, attribute, old, new, kwargs):
        try:
            if self._is_unemptied() and self._pir_on():
                self._sync_signal()
        except Exception as e:
            self.log(f"room state handler: {e}", level="ERROR")

    def _set_al_not_unemptied(self):
        try:
            if self.get_state(self._al_sg) == "on":
                self.turn_off(self._al_sg)
            if self.get_state(self._al_main) != "on":
                self.turn_on(self._al_main)
        except Exception as e:
            self.log(f"AL not-unemptied failed: {e}", level="ERROR")

    def _both_al_off(self):
        try:
            if self.get_state(self._al_main) == "on":
                self.turn_off(self._al_main)
            if self.get_state(self._al_sg) == "on":
                self.turn_off(self._al_sg)
        except Exception as e:
            self.log(f"both AL off failed: {e}", level="ERROR")

    def _clear_full_island_if_on(self):
        try:
            if self.get_state(self._full_island) == "on":
                self.turn_off(self._full_island)
        except Exception as e:
            self.log(f"clear full island: {e}", level="DEBUG")

    def _release_dark_solo_manual_control(self):
        """Drop SG manual hold on bulb1 without powering lights."""
        try:
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_sg,
                lights=[self._signal_light],
                manual_control=False,
            )
        except Exception as e:
            self.log(f"release dark solo manual: {e}", level="DEBUG")

    def _island_lit(self):
        """True when the full group, the SG group or bulb1 reads on."""
        for ent in (self._full_island, self._island_sg_light, self._signal_light):
            if ent and self.get_state(ent) == "on":
                return True
        return False

    def _all_signal_lights_off(self):
        """Turn off every light this app uses for the dishwasher signal."""
        self._clear_full_island_if_on()
        for ent in (self._signal_light, self._island_sg_light):
            if not ent:
                continue
            try:
                if self.get_state(ent) == "on":
                    self.turn_off(ent)
            except Exception as e:
                self.log(f"off signal light {ent}: {e}", level="DEBUG")

    def _prep_from_dark_solo_for_bright_green(self):
        """Drop dark-solo SG state without ``apply`` on bulb1 (avoids a white flash before full-group green)."""
        try:
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_sg,
                lights=[self._signal_light],
                manual_control=False,
            )
            if self._island_sg_light and self.get_state(self._island_sg_light) == "on":
                self.turn_off(self._island_sg_light)
        except Exception as e:
            self.log(f"prep dark solo for bright: {e}", level="DEBUG")

    def _clear_manual_main_signal_bulb(self):
        try:
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_main,
                lights=[self._signal_light],
                manual_control=False,
            )
        except Exception as e:
            self.log(f"clear manual main bulb: {e}", level="DEBUG")

    def _apply_bright_full_signal(self):
        if not self._is_unemptied() or not self._pir_on():
            return
        try:
            # If the full group is already on (e.g. family room lighting), only recolor - do not power-cycle.
            # Read before anything below changes the island: only a group powered up from off is ours to switch off.
            was_off = self.get_state(self._full_island) != "on"
            self._prep_from_dark_solo_for_bright_green()
            self._both_al_off()
            self.turn_on(self._full_island, **self._signal_light_turn_on_kwargs())
            if was_off:
                self._island_powered_by_signal = True
        except Exception as e:
            self.log(f"apply bright full signal failed: {e}", level="ERROR")

    def _dark_solo_layout_already_applied(self):
        """Skip redundant reapplies when ``_sync_signal`` fires repeatedly while already in dark solo.

        Judged by the AL layout, not the full group: ``light.island_lights`` reads on whenever any member is lit.
        """
        try:
            if self.get_state(self._al_main) != "off" or self.get_state(self._al_sg) != "on":
                return False
            if self.get_state(self._signal_light) != "on":
                return False
            if self._island_sg_light and self.get_state(self._island_sg_light) != "on":
                return False
            return True
        except Exception:
            return False

    def _apply_dark_solo_signal(self):
        if not self._is_unemptied() or not self._pir_on():
            return
        if self._dark_solo_layout_already_applied():
            self._island_powered_by_signal = False
            return
        self._island_powered_by_signal = False
        try:
            # Dark layout still requires the full group off; one turn_off when it was on is unavoidable.
            self._clear_full_island_if_on()
            if self.get_state(self._al_main) == "on":
                self.turn_off(self._al_main)
            if self.get_state(self._al_sg) != "on":
                self.turn_on(self._al_sg)
            # AL switch does not power lights - must turn on the SG *light* group (only used in this app).
            if self._island_sg_light and self.get_state(self._island_sg_light) != "on":
                self.turn_on(self._island_sg_light)
            try:
                self.call_service(
                    "adaptive_lighting/apply",
                    entity_id=self._al_sg,
                    turn_on_lights=True,
                )
            except Exception as e:
                self.log(f"apply SG AL after turn_on: {e}", level="DEBUG")
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_sg,
                lights=[self._signal_light],
                manual_control=True,
            )
            # Recolor signal bulb without turn_off when it is already on (e.g. re-entry).
            self.turn_on(self._signal_light, **self._signal_light_turn_on_kwargs())
        except Exception as e:
            self.log(f"apply dark solo signal failed: {e}", level="ERROR")

    def _hand_back_island(self):
        """End the signal: the island belongs to FamilyRoomLights again.

        Never powers a light on. Only a full group this app powered up from off (bright path) is switched off
        here; any other lit island bulb is recolored to normal by ``_restore_normal_layout`` and left to
        FamilyRoomLights. Cheap and idempotent when no signal layout is applied.
        """
        try:
            if self._island_powered_by_signal:
                self._island_powered_by_signal = False
                self.log("island hand-back: switching off the full group the signal powered up", level="INFO")
                self._all_signal_lights_off()
            self.run_in(self._restore_normal_layout, _HAND_BACK_SETTLE_S)
        except Exception as e:
            self.log(f"island hand-back failed: {e}", level="ERROR")

    def _restore_normal_layout(self, _kwargs=None):
        """Normal layout (AL main on, SG off, bulb1 manual control released), then recolor any lit island bulb.

        No-op when the layout is already normal or the signal is showing again. Never powers a light on:
        ``turn_on_lights=False`` makes AL skip bulbs HA reports as off.
        """
        try:
            if self._is_unemptied() and self._pir_on():
                return
            if self._al_is_normal_not_unemptied():
                return
            self._release_dark_solo_manual_control()
            self._clear_manual_main_signal_bulb()
            self._set_al_not_unemptied()
            recolor = self._island_lit()
            if recolor:
                self.call_service(
                    "adaptive_lighting/apply",
                    entity_id=self._al_main,
                    lights=[self._full_island],
                    turn_on_lights=False,
                )
            self.log(f"island hand-back: normal AL layout restored (recolor lit island: {recolor})", level="INFO")
        except Exception as e:
            self.log(f"restore normal layout failed: {e}", level="ERROR")

    def _sync_signal(self, leaving_unemptied=False):
        if not self._is_unemptied():
            if leaving_unemptied:
                self._hand_back_island()
            return

        if not self._pir_on():
            self._hand_back_island()
            return

        if self._kitchen_presence_suspect():
            # Asymmetric trust: ghost presence must not light the signal (no apply);
            # an already-applied signal is held as-is - suspect still counts as
            # presence, so only the real end paths above may hand the island back.
            self.log(
                "kitchen presence SUSPECT (mmWave-only + speaker playing) - holding island signal state",
                level="DEBUG",
            )
            return

        if self._is_family_room_bright():
            self._apply_bright_full_signal()
        else:
            self._apply_dark_solo_signal()
