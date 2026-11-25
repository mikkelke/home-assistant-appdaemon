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


class HostRound(unittest.TestCase):
    def test_newest_kernel_sorts_numerically(self):
        names = ["/boot/vmlinuz-7.0.0-38-generic", "/boot/vmlinuz-7.0.0-44-generic", "/boot/vmlinuz-7.0.0-9-generic"]
        self.assertEqual(hur.newest_kernel(names), "7.0.0-44-generic")

    def test_newest_kernel_none(self):
        self.assertIsNone(hur.newest_kernel([]))

    def test_apt_pending_counts_upgrades_and_new(self):
        self.assertEqual(hur.apt_pending("x\n34 upgraded, 1 newly installed, 0 to remove and 1 not upgraded.\n"), 35)

    def test_apt_pending_ignores_phased(self):
        text = "The following upgrades have been deferred due to phasing:\n  libsqlite3-0\n" \
               "0 upgraded, 0 newly installed, 0 to remove and 1 not upgraded.\n"
        self.assertEqual(hur.apt_pending(text), 0)

    def test_apt_pending_unparsable(self):
        self.assertIsNone(hur.apt_pending("E: could not get lock"))

    def test_container_problems(self):
        rows = [{"Names": "a", "State": "running", "HealthStatus": "healthy"},
                {"Names": "b", "State": "running", "HealthStatus": "none"},
                {"Names": "c", "State": "exited", "HealthStatus": "none"},
                {"Names": "d", "State": "running", "HealthStatus": "unhealthy"},
                {"Names": "e", "State": "exited", "HealthStatus": "none"}]
        self.assertEqual(hur.container_problems(["a", "b", "c", "d", "f"], rows),
                         ["c exited", "d unhealthy", "f missing"])


class AppDaemonReconnect(unittest.TestCase):
    START = dt.datetime(2026, 11, 25, 12, 29, tzinfo=hur.CPH)

    def test_namespace_restart(self):
        lines = ["2026-11-25 12:28:00.000000 ERROR HASS: Websocket connection lost",
                 "2026-11-25 12:30:05.000000 INFO HASS: Connected to Home Assistant 2026.11.3 with aiohttp websocket",
                 "2026-11-25 12:31:00.000000 INFO AppDaemon: Processing restart for plugin namespace 'default'",
                 "2026-11-25 12:31:01.000000 INFO AppDaemon: Calling initialize() for x"]
        got = hur.ad_summary(lines, self.START)
        self.assertTrue(got["namespace_restart"])
        self.assertEqual((got["initialized"], got["errors"]), (1, []))

    def test_cold_start_after_reboot(self):
        lines = ["2026-11-25 12:30:00.000000 INFO AppDaemon: AppDaemon Version 4.5.16 starting",
                 "2026-11-25 12:30:05.000000 INFO HASS: Connected to Home Assistant 2026.11.3 with aiohttp websocket",
                 "2026-11-25 12:30:40.000000 INFO AppDaemon: Calling initialize() for x",
                 "2026-11-25 12:30:41.000000 ERROR x: boom"]
        got = hur.ad_summary(lines, self.START)
        self.assertTrue(got["namespace_restart"])
        self.assertEqual(got["initialized"], 1)
        self.assertEqual(len(got["errors"]), 1)

    def test_nothing_yet(self):
        self.assertFalse(hur.ad_summary(["2026-11-25 12:00:00.000000 INFO old"], self.START)["namespace_restart"])


if __name__ == "__main__":
    unittest.main()
