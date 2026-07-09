"""Dishwasher signal **only** (Unemptied + kitchen PIR). Family room lighting uses ``light.island_lights`` only
elsewhere - this app is the **only** place that uses ``island_lights_sg`` + ``island_light_1`` for the green signal.

- **Bright** (family room not dark): full ``light.island_lights`` green, both AL switches off.
- **Dark**: ``light.island_lights`` off; ``light.island_lights_sg`` on (normal AL) + ``light.island_light_1`` green (manual).

Prefer **in-place color/power** via ``turn_on`` (brightness + ``hs_color``) when lights are already on - avoid ``turn_off``/``turn_on`` churn when switching dishwasher *mode* or when the island was already lit by family room lighting. Hue/ZHA often map ``rgb_color`` to ``color_temp`` in HA state while the lamp still looks chromatic; ``hs_color`` keeps entity state aligned with what you see.

When not Unemptied, cleanup runs when **leaving** Unemptied (see ``_sync_signal``). **Startup:** if HA already says not Unemptied but AL switches are still in the dishwasher layout (anything other than main-on/SG-off), we run the same cleanup so a missed transition or AD restart cannot leave island light 1 green while ``sensor.dishwasher_state`` is Off.

**Unemptied with no kitchen PIR:** all island signal lights off and both AL switches off. Lights are only driven while ``kitchen_pir`` is on (bright green or dark solo). Enabling the SG AL switch during "idle" previously caused Adaptive Lighting to power bulbs with nobody present.

**PIR off race:** On kitchen PIR ``off``, we must only call ``turn_off(light.island_lights)`` when this app
had turned the full group on (bright + Unemptied green). In dark-solo mode the full group is already off;
``FamilyRoomLights`` may turn it on in the same tick - an unconditional clear would wipe that and leave
only ``island_light_1`` from the dark-solo cleanup path.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

import room_state_darkness


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
        # True only while _apply_bright_full_signal has the full group on (green). Used so PIR-off does not
        # turn_off(light.island_lights) when dark-solo left the group off and FamilyRoomLights just turned it on.
        self._dishwasher_full_island_active = False

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
        """Drop SG manual hold on bulb1 without powering lights (used when PIR clears)."""
        try:
            self.call_service(
                "adaptive_lighting/set_manual_control",
                entity_id=self._al_sg,
                lights=[self._signal_light],
                manual_control=False,
            )
        except Exception as e:
            self.log(f"release dark solo manual: {e}", level="DEBUG")

    def _clear_dark_solo_signal(self):
        """Release dark-solo manual control and hand bulb1 back to SG AL (leaving Unemptied only)."""
        try:
            self._release_dark_solo_manual_control()
            if self.get_state(self._signal_light) == "on":
                self.call_service(
                    "adaptive_lighting/apply",
                    entity_id=self._al_sg,
                    lights=[self._signal_light],
                    turn_on_lights=True,
                )
        except Exception as e:
            self.log(f"clear dark solo: {e}", level="DEBUG")

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
            self._prep_from_dark_solo_for_bright_green()
            self._both_al_off()
            self.turn_on(self._full_island, **self._signal_light_turn_on_kwargs())
            self._dishwasher_full_island_active = True
        except Exception as e:
            self.log(f"apply bright full signal failed: {e}", level="ERROR")

    def _dark_solo_layout_already_applied(self):
        """Skip redundant reapplies when ``_sync_signal`` fires repeatedly while already in dark solo."""
        try:
            if self.get_state(self._full_island) != "off":
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
            self._dishwasher_full_island_active = False
            return
        self._dishwasher_full_island_active = False
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

    def _sync_signal(self, leaving_unemptied=False):
        if not self._is_unemptied():
            if leaving_unemptied:
                self._clear_full_island_if_on()
                self._dishwasher_full_island_active = False
                self._clear_dark_solo_signal()
                self._clear_manual_main_signal_bulb()
                if self._island_sg_light:
                    try:
                        if self.get_state(self._island_sg_light) == "on":
                            self.turn_off(self._island_sg_light)
                    except Exception as e:
                        self.log(f"leave unemptied: off SG light: {e}", level="DEBUG")
                self._set_al_not_unemptied()
                try:
                    if self.get_state(self._signal_light) == "on":
                        self.call_service(
                            "adaptive_lighting/apply",
                            entity_id=self._al_main,
                            lights=[self._signal_light],
                            turn_on_lights=True,
                        )
                except Exception as e:
                    self.log(f"apply main after leave unemptied: {e}", level="DEBUG")
            return

        if not self._pir_on():
            if self._dishwasher_full_island_active:
                self._clear_full_island_if_on()
            self._dishwasher_full_island_active = False
            self._release_dark_solo_manual_control()
            self._all_signal_lights_off()
            self._both_al_off()
            return

        if self._is_family_room_bright():
            self._apply_bright_full_signal()
        else:
            self._apply_dark_solo_signal()
