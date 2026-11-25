"""Gate logic of ha-update-restart; no SSH, containers or Home Assistant."""

import datetime as dt
import importlib.machinery
import importlib.util
from pathlib import Path
import unittest

LOADER = importlib.machinery.SourceFileLoader("ha_update_restart", str(Path(__file__).parents[1] / "ha-update-restart"))
SPEC = importlib.util.spec_from_loader(LOADER.name, LOADER)
hur = importlib.util.module_from_spec(SPEC)
LOADER.exec_module(hur)

AT = dt.datetime(2026, 11, 25, 10, 0, tzinfo=hur.CPH)
HOME = {"state": "0", "last_changed": "2026-11-25T08:30:00+00:00",
        "attributes": {"latitude": 55.6681, "longitude": 12.53468}}


def fix(lat, lon, accuracy=10):
    return {"attributes": {"latitude": lat, "longitude": lon, "gps_accuracy": accuracy}}


def parcel(status, start=None, end=None):
    return {"carrier": "GLS", "barcode": "1", "status": status, "planned_from": start, "planned_to": end}


def incoming(*parcels):
    return {"attributes": {"parcels": list(parcels)}}


class EmptyGate(unittest.TestCase):
    def test_empty_long_enough(self):
        self.assertTrue(hur.empty_gate(HOME, AT)[0])

    def test_someone_home(self):
        self.assertFalse(hur.empty_gate({**HOME, "state": "1"}, AT)[0])

    def test_empty_too_briefly(self):
        self.assertFalse(hur.empty_gate({**HOME, "last_changed": "2026-11-25T08:55:00+00:00"}, AT)[0])


class GpsGate(unittest.TestCase):
    def test_far_fix_passes(self):
        ok, text = hur.gps_gate(HOME, {"Kristine": fix(55.3653, 10.4268)})
        self.assertTrue(ok)
        self.assertIn("Kristine", text)

    def test_near_fix_blocks(self):
        ok, text = hur.gps_gate(HOME, {"Mikkel": fix(55.6700, 12.5400)})
        self.assertFalse(ok)
        self.assertIn("BLOCK", text)

    def test_accuracy_eats_the_margin(self):
        # About 1.8 km away, but a 500 m accuracy circle reaches inside 1.5 km.
        self.assertFalse(hur.gps_gate(HOME, {"Mikkel": fix(55.6843, 12.5347, accuracy=500)})[0])
        self.assertTrue(hur.gps_gate(HOME, {"Mikkel": fix(55.6843, 12.5347, accuracy=50)})[0])

    def test_tracker_without_fix_is_skipped(self):
        ok, text = hur.gps_gate(HOME, {"Claudia": {"state": "unknown", "attributes": {}}, "Kristine": None})
        self.assertTrue(ok)
        self.assertIn("Claudia no fix", text)

    def test_any_near_tracker_blocks(self):
        self.assertFalse(hur.gps_gate(HOME, {"Kristine": fix(55.3653, 10.4268), "Mikkel": fix(55.669, 12.535)})[0])


class DeliveryGate(unittest.TestCase):
    def test_nothing_incoming(self):
        self.assertTrue(hur.delivery_gate(incoming(), AT)[0])

    def test_out_for_delivery_blocks(self):
        self.assertFalse(hur.delivery_gate(incoming(parcel("out_for_delivery")), AT)[0])

    def test_window_within_an_hour_blocks(self):
        self.assertFalse(hur.delivery_gate(
            incoming(parcel("in_transit", "2026-11-25T09:45:00+00:00", "2026-11-25T11:00:00+00:00")), AT)[0])

    def test_open_window_blocks(self):
        self.assertFalse(hur.delivery_gate(
            incoming(parcel("in_transit", "2026-11-25T07:00:00+00:00", "2026-11-25T12:00:00+00:00")), AT)[0])

    def test_later_today_passes(self):
        self.assertTrue(hur.delivery_gate(
            incoming(parcel("in_transit", "2026-11-25T16:00:00+00:00", "2026-11-25T21:00:00+00:00")), AT)[0])

    def test_window_already_over_passes(self):
        self.assertTrue(hur.delivery_gate(
            incoming(parcel("in_transit", "2026-11-25T06:00:00+00:00", "2026-11-25T08:00:00+00:00")), AT)[0])

    def test_tomorrow_passes(self):
        self.assertTrue(hur.delivery_gate(incoming(parcel("in_transit", "2026-11-26T08:30:00+00:00")), AT)[0])

    def test_delivered_is_ignored(self):
        self.assertTrue(hur.delivery_gate(incoming(parcel("delivered", "2026-11-25T09:10:00+00:00")), AT)[0])

    def test_unreadable_list_blocks(self):
        self.assertFalse(hur.delivery_gate(None, AT)[0])


if __name__ == "__main__":
    unittest.main()
