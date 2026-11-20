"""
RainNow - the one "is it raining now" of the house: ``binary_sensor.raining``.

Lights (darkness_calculator), the opening alert, the room displays and the dashboard sky read
this entity instead of deciding for themselves, so no two of them can disagree. The rule is
apply_rain: the roof station's piezo RATE measures water, and the piezo CONTACT may only extend
an episode the rate has already confirmed.

Publish contract:
  - ``binary_sensor.raining``  on/off, device_class moisture
      ``reason``    why, in words ("measured 1.2 mm/h", "piezo still wet after measured rain",
                    "piezo tap without measured rain, ignored", "rain rate unreadable", "dry")
      ``rate_mmh``  the piezo rate as read (None when unreadable)
      ``piezo``     the piezo contact's state as HA reports it

Restart survival: an HA restart erases every set_state entity. AppDaemon terminates and
re-creates its apps around such a restart, so initialize() republishes at once; for an entity
that vanishes without an app restart, every periodic tick writes it again when HA no longer
has it. At start the contact alone is never enough: rain is only the measured rate until a
measurable reading has confirmed the contact episode in progress.
"""

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

ENTITY = "binary_sensor.raining"
CONTACT = "binary_sensor.gw2000a_rain_state_piezo"
RATE = "sensor.gw2000a_rain_rate_piezo"
RATE_MIN_MMH = 0.5


def apply_rain(contact_on, rate, confirmed, rate_min=RATE_MIN_MMH):
    """Fold the piezo CONTACT and the piezo RATE into one honest "is it raining".

    Returns (raining, confirmed, reason). ``confirmed`` is true while the contact episode in
    progress has seen measured water; hand it back in on the next call.

    binary_sensor.gw2000a_rain_state_piezo is not a rain sensor. The WS90's piezo plate
    reports an impact - a fly, a leaf, a bird, the mount shaking - and HA publishes that
    as a rain binary. Measured over the 60 days to 2026-08-12: it went ON 375 times for
    142 h total, and in 298 of those episodes the rain RATE never left 0.0 mm/h AND the
    rain accumulator never moved a single 0.1 mm tick - zero water. 60 of those phantom
    onsets happened under more than 200 W/m2 of sunshine, and their median relative
    humidity was 77% with a 4.1 K dew-point spread, so they are not dew either.

    That matters because rain is one of the two gloomy triggers in darkness_calculator:
    while "raining", family_room's DARK bar goes 2500 -> 5500 lx (gloomy_dark_multiplier)
    and the dark->bright hold doubles. The other trigger (overcast) already covers a dim
    sky with the sun above 20 deg, so the phantom's own damage lands BELOW that - mornings,
    evenings, winter, exactly when the lamps matter. Over those same 60 days it put 7.3 h
    of daylight across 14 days into DARK, worst 113 min on 2026-06-17 - lamps on in a room
    the sky was lighting perfectly well, because something tapped the plate. The opening
    alert would have announced "rain on the roof" over Sonos and pushed every phone for it:
    terrace door 1 alone stood open for 1533 of those contact-on minutes, 1467 of them
    with no water.

    The rate sensor is the instrument that actually measures water: it quantises to
    0.6 mm/h, and every non-zero reading was corroborated by the accumulator. It also
    catches rain the contact misses - 12.6% of measurable-rate samples arrived while the
    contact said "off", including 22.2 mm/h on 2026-06-27.

    So: measured rate wins outright, and the contact may only extend an episode that the
    rate has already confirmed (a real shower's rate dips to 0 mid-episode, and the sky
    stays wet and grey after the last measurable tick - median 2 min, p90 60 min). An
    unconfirmed contact is ignored. Confirmation costs ~1 min: across the 77 real
    episodes the rate crossed the bar a median 1.0 min after the contact closed.

    Replaying THIS rule over those 60 days of recorded piezo history: 142.1 h of
    contact-on becomes 44.4 h of rain, all 77 real episodes survive (none lost), 99.6 h
    of phantom is dropped, and four short stretches of rate-only rain that the contact
    never reported at all are gained.
    """
    measurable = rate is not None and rate >= rate_min
    if measurable:
        confirmed = True
    elif not contact_on:
        confirmed = False
    raining = measurable or (contact_on and confirmed)
    if measurable:
        reason = f"measured {rate:g} mm/h"
    elif raining:
        reason = "piezo still wet after measured rain"
    elif rate is None:
        reason = "rain rate unreadable"
    elif contact_on:
        reason = "piezo tap without measured rain, ignored"
    else:
        reason = "dry"
    return raining, confirmed, reason


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class RainNow(hass.Hass):
    def initialize(self):
        a = self.args
        self.entity = a.get("entity", ENTITY)
        self.contact_entity = a.get("contact_entity", CONTACT)
        self.rate_entity = a.get("rate_entity", RATE)
        self.rate_min = float(a.get("rate_min_mmh", RATE_MIN_MMH))
        self.periodic_s = int(a.get("periodic_seconds", 60))

        self._confirmed = False  # measured water seen in the CURRENT contact episode
        self._published = None   # (raining, reason, rate, contact) as last handed to HA

        self.listen_state(self._on_source, self.contact_entity)
        self.listen_state(self._on_source, self.rate_entity)
        self._evaluate()
        self.run_every(self._tick, f"now+{self.periodic_s}", self.periodic_s)
        self.log(
            f"RainNow: {self.entity} from {self.rate_entity} >= {self.rate_min:g} mm/h, "
            f"extended by {self.contact_entity} once confirmed",
            level="INFO",
        )

    def _read(self, entity):
        try:
            return self.get_state(entity)
        except Exception:
            return None

    def _on_source(self, entity, attribute, old, new, kwargs):
        self._evaluate()

    def _tick(self, kwargs):
        self._evaluate()

    def _evaluate(self):
        contact = self._read(self.contact_entity)
        rate = _number(self._read(self.rate_entity))
        raining, self._confirmed, reason = apply_rain(contact == "on", rate, self._confirmed, self.rate_min)
        self._publish(raining, reason, rate, contact)

    def _publish(self, raining, reason, rate, contact):
        snapshot = (raining, reason, rate, contact)
        if snapshot == self._published and self._read(self.entity) is not None:
            return
        try:
            self.set_state(
                self.entity,
                state="on" if raining else "off",
                attributes={
                    "friendly_name": "Raining",
                    "device_class": "moisture",
                    "icon": "mdi:weather-rainy",
                    "reason": reason,
                    "rate_mmh": rate,
                    "piezo": contact,
                },
                replace=True,
                check_existence=False,
            )
        except Exception as e:
            self.log(f"Publish failed: {e!r}", level="WARNING")
            return
        if self._published is None or self._published[0] != raining:
            self.log(f"{'Raining' if raining else 'Not raining'}: {reason}", level="INFO")
        self._published = snapshot
