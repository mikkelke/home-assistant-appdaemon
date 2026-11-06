# tests/test_washer_early_pick_label.py - a programme picked in the first 10 minutes, before the classifier has a
# guess, stays on the card through the Running ticks instead of reverting to the classifier's "Unknown".
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import unittest
from datetime import timedelta

import test_washer_restart_survival as trs  # installs the appdaemon stub

NOW = trs.NOW
START = NOW - timedelta(minutes=3)
CONFIRM = "input_select.washer_confirmed_programme"
TEMPERATURE = "input_select.washer_temperature"


def early_app(confirmed):
    payload = trs.make_store_payload(state="Running", start_time=START, cycle_id="cycle-early-pick",
                                     detected_programme="unknown", detected_temperature="")
    app = trs.make_full_init_app(
        sensor_state=None, helper_state="Off", power_watts=200, store_payload=payload, now=NOW,
        extra_args={"confirm_entity": CONFIRM, "temperature_entity": TEMPERATURE},
    )
    app.states[CONFIRM] = "Bomuld" if confirmed else "Auto (unconfirmed)"
    app.states[TEMPERATURE] = "40°C"
    app.programme_confirmed_by_user = confirmed
    return app


class EarlyPickStaysOnTheCard(unittest.TestCase):
    def test_a_programme_picked_before_minute_ten_is_the_label_the_tick_publishes(self):
        app = early_app(confirmed=True)
        app._check_energy_finish({})
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_label"], "Bomuld")

    def test_on_auto_the_label_is_still_the_classifiers(self):
        app = early_app(confirmed=False)
        app._check_energy_finish({})
        self.assertEqual(trs.last_publish(app)["attributes"]["programme_label"], "Unknown")


if __name__ == "__main__":
    unittest.main()
