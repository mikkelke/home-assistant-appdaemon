# tests/test_salus_gateway_diagnostics.py - the AppDaemon app layer (scheduling, gateway
# I/O orchestration, set_state publishing). Protocol/crypto/parsing correctness lives in
# test_salus_gateway_protocol.py; this file mocks that module's I/O boundary (_post_read)
# rather than re-proving the cipher. Same __new__ + monkeypatched-callables harness as the
# other apps/climate/tests files; async methods tested via IsolatedAsyncioTestCase, matching
# climate_alarm.py/salus_health.py.
#
# aiohttp is installed in the AppDaemon container (verified) but NOT on every dev/CI
# machine this suite runs on - a minimal fake is installed into sys.modules only when the
# real package isn't importable, same technique as the appdaemon stub below, so this file
# exercises the real aiohttp wherever it happens to be available.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

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

try:
    import aiohttp  # noqa: F401
except ImportError:
    class _FakeClientTimeout:
        def __init__(self, total=None):
            self.total = total

    class _FakeClientSession:
        """Never actually used to send anything in this suite - every test replaces
        app._post_read (orchestration tests) or drives app._post_read directly against
        its own fake session (PostRead below). Exists only so `import aiohttp` and
        `aiohttp.ClientSession(...)`/`aiohttp.ClientTimeout(...)` succeed at all."""

        def __init__(self, timeout=None):
            self.timeout = timeout

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    _fake_aiohttp = types.ModuleType("aiohttp")
    _fake_aiohttp.ClientTimeout = _FakeClientTimeout
    _fake_aiohttp.ClientSession = _FakeClientSession
    sys.modules["aiohttp"] = _fake_aiohttp

import salus_gateway_diagnostics as sgd  # noqa: E402
import salus_gateway_protocol as proto  # noqa: E402


def _base_app(**overrides):
    """SalusGatewayDiagnostics instance without running AppDaemon's initialize() -
    knobs set directly, matching the make_app() helpers in the sibling test files."""
    app = sgd.SalusGatewayDiagnostics.__new__(sgd.SalusGatewayDiagnostics)
    app.host = "10.0.0.50"
    app.port = 80
    app.euid = "ABCDEF0123456789"
    app.poll_minutes = 5
    app.request_timeout_s = 20
    app._cipher = proto.GatewayCipher(app.euid)
    app.log = MagicMock()
    app.set_state = AsyncMock()
    app.create_task = MagicMock()
    for k, v in overrides.items():
        setattr(app, k, v)
    return app


class InitializeGuard(unittest.TestCase):
    """The credential-blank safety net: idle quietly rather than hammering a
    placeholder host every cycle - see module docstring."""

    def _init_app(self, args):
        app = sgd.SalusGatewayDiagnostics.__new__(sgd.SalusGatewayDiagnostics)
        app.args = dict(args)
        app.log = MagicMock()
        app.run_every = MagicMock()
        app.initialize()
        return app

    def test_blank_host_does_not_schedule_polling(self):
        app = self._init_app({"host": "", "euid": "ABCDEF0123456789"})
        app.run_every.assert_not_called()
        app.log.assert_called_once()

    def test_missing_host_key_does_not_schedule_polling(self):
        app = self._init_app({"euid": "ABCDEF0123456789"})
        app.run_every.assert_not_called()

    def test_blank_euid_does_not_schedule_polling(self):
        app = self._init_app({"host": "10.0.0.50", "euid": ""})
        app.run_every.assert_not_called()

    def test_configured_host_and_euid_schedules_polling(self):
        app = self._init_app({"host": "10.0.0.50", "euid": "ABCDEF0123456789", "poll_minutes": 5})
        app.run_every.assert_called_once()
        cb, start, interval = app.run_every.call_args[0]
        self.assertEqual(cb, app._on_poll_tick)
        self.assertEqual(start, "now+30")
        self.assertEqual(interval, 300)

    def test_poll_minutes_is_floored_at_60_seconds(self):
        app = self._init_app({"host": "10.0.0.50", "euid": "ABCDEF0123456789", "poll_minutes": 0.1})
        _, _, interval = app.run_every.call_args[0]
        self.assertEqual(interval, 60)

    def test_default_port_is_80(self):
        app = self._init_app({"host": "10.0.0.50", "euid": "ABCDEF0123456789"})
        self.assertEqual(app.port, 80)

    def test_cipher_derives_from_configured_euid(self):
        app = self._init_app({"host": "10.0.0.50", "euid": "ABCDEF0123456789"})
        self.assertEqual(app._cipher.encrypt("x"), proto.GatewayCipher("ABCDEF0123456789").encrypt("x"))


class PollTick(unittest.TestCase):
    def test_dispatches_to_create_task(self):
        app = _base_app()
        app._on_poll_tick({})
        app.create_task.assert_called_once()


class AsyncPollCycle(unittest.IsolatedAsyncioTestCase):
    """_async_poll: any failure anywhere in the cycle is swallowed and logged, never
    raised - see module docstring's polling-discipline section."""

    async def test_fetch_failure_is_swallowed_and_logged(self):
        app = _base_app()
        app._fetch_raw_devices = AsyncMock(side_effect=RuntimeError("gateway busy"))
        app._publish = AsyncMock()
        await app._async_poll()
        app._publish.assert_not_called()
        app.log.assert_called_once()

    async def test_no_relevant_devices_logs_and_publishes_nothing(self):
        app = _base_app()
        app._fetch_raw_devices = AsyncMock(return_value=[])
        app._publish = AsyncMock()
        await app._async_poll()
        app._publish.assert_not_called()
        app.log.assert_called_once()

    async def test_publishes_each_returned_record(self):
        app = _base_app()
        records = [
            {
                "sZDO": {"DeviceName": '{"deviceName": "Bedroom Thermostat"}'},
                "sIT600TH": {"BatteryLevel": 4},
                "sZDOInfo": {"OnlineStatus_i": 1},
            },
            {
                "sZDO": {"DeviceName": '{"deviceName": "Control Centre"}'},
                "sBasicS": {"ModelIdentifier": "it600WC"},
                "sIT600WC": {"ErrorCodeWC_d": "0000"},
                "sZDOInfo": {"OnlineStatus_i": 1},
            },
        ]
        app._fetch_raw_devices = AsyncMock(return_value=records)
        published_slugs = []

        async def fake_publish(diag):
            published_slugs.append(diag["slug"])

        app._publish = fake_publish
        await app._async_poll()
        self.assertEqual(published_slugs, ["bedroom_thermostat", "control_centre"])

    async def test_one_unparseable_record_does_not_block_the_others(self):
        app = _base_app()
        good_record = {
            "sZDO": {"DeviceName": '{"deviceName": "Bedroom Thermostat"}'},
            "sIT600TH": {"BatteryLevel": 4},
        }
        app._fetch_raw_devices = AsyncMock(return_value=["not-a-dict", good_record])
        published_slugs = []

        async def fake_publish(diag):
            published_slugs.append(diag["slug"])

        app._publish = fake_publish
        await app._async_poll()
        self.assertEqual(published_slugs, ["bedroom_thermostat"])
        app.log.assert_called()


class FetchRawDevices(unittest.IsolatedAsyncioTestCase):
    """Orchestration only - _post_read itself is exercised in PostRead below. Proves
    the request-minimizing shape the module docstring requires: one readall, then AT
    MOST one combined deviceid batching every relevant device together."""

    async def test_readall_then_one_combined_deviceid_request(self):
        app = _base_app()
        readall_response = {
            "id": [
                {"data": {"UniID": "a"}, "sIT600TH": {}},
                {"data": {"UniID": "b"}, "sBasicS": {"ModelIdentifier": "it600WC"}},
                {"data": {"UniID": "c"}, "sIASZS": {}},  # irrelevant - not a thermostat/WC
            ]
        }
        detail_response = {"id": [{"detail": "here"}]}
        calls = []

        async def fake_post_read(session, body):
            calls.append(body)
            return readall_response if body["requestAttr"] == "readall" else detail_response

        app._post_read = fake_post_read
        result = await app._fetch_raw_devices()

        self.assertEqual(result, detail_response["id"])
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0], {"requestAttr": "readall"})
        self.assertEqual(
            calls[1]["id"],
            [{"data": {"UniID": "a"}}, {"data": {"UniID": "b"}}],
        )

    async def test_no_relevant_devices_skips_the_second_request_entirely(self):
        app = _base_app()
        readall_response = {"id": [{"data": {"UniID": "c"}, "sIASZS": {}}]}
        calls = []

        async def fake_post_read(session, body):
            calls.append(body)
            return readall_response

        app._post_read = fake_post_read
        result = await app._fetch_raw_devices()

        self.assertEqual(result, [])
        self.assertEqual(len(calls), 1)


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def read(self):
        return self._body


class _FakeSession:
    def __init__(self, response_body: bytes):
        self._response_body = response_body
        self.posted = []

    def post(self, url, data=None, headers=None):
        self.posted.append({"url": url, "data": data, "headers": headers})
        return _FakeResponse(self._response_body)


class PostRead(unittest.IsolatedAsyncioTestCase):
    async def test_encrypts_request_posts_and_decrypts_unwraps_response(self):
        app = _base_app()
        response_body = {"status": "success", "id": [{"data": {"UniID": "x"}}]}
        session = _FakeSession(app._cipher.encrypt(json.dumps(response_body)))

        result = await app._post_read(session, {"requestAttr": "readall"})

        self.assertEqual(result, response_body)
        self.assertEqual(len(session.posted), 1)
        self.assertEqual(session.posted[0]["url"], f"http://{app.host}:{app.port}/deviceid/read")
        sent_plaintext = json.loads(app._cipher.decrypt(session.posted[0]["data"]))
        self.assertEqual(sent_plaintext, {"requestAttr": "readall"})

    async def test_raises_gateway_response_error_on_non_success_status(self):
        app = _base_app()
        session = _FakeSession(app._cipher.encrypt(json.dumps({"status": "failed"})))
        with self.assertRaises(proto.GatewayResponseError):
            await app._post_read(session, {"requestAttr": "readall"})


class Publish(unittest.IsolatedAsyncioTestCase):
    """AppDaemon 4.5.13's set_state silently drops any (possibly nested) attribute/state
    value equal to 0 or False - see module docstring. Every numeric value must survive
    as a string, and every call carries replace=True."""

    FULL_DIAG = {
        "slug": "bedroom_thermostat",
        "battery_level": 4,
        "rssi": -55,
        "lqi": 200,
        "online": True,
        "errors": [],
    }

    def _entity_ids(self, app):
        return [c.args[0] for c in app.set_state.call_args_list]

    def _call_for(self, app, entity_id):
        return next(c for c in app.set_state.call_args_list if c.args[0] == entity_id)

    async def test_full_reading_publishes_all_five_entities(self):
        app = _base_app()
        await app._publish(self.FULL_DIAG)
        self.assertEqual(
            sorted(self._entity_ids(app)),
            sorted([
                "sensor.salus_bedroom_thermostat_battery",
                "sensor.salus_bedroom_thermostat_rssi",
                "sensor.salus_bedroom_thermostat_lqi",
                "sensor.salus_bedroom_thermostat_connectivity",
                "sensor.salus_bedroom_thermostat_problem",
            ]),
        )

    async def test_battery_state_and_attributes(self):
        app = _base_app()
        await app._publish(self.FULL_DIAG)
        call = self._call_for(app, "sensor.salus_bedroom_thermostat_battery")
        self.assertEqual(call.kwargs["state"], "80")
        self.assertEqual(call.kwargs["attributes"]["raw_level"], "4")
        self.assertEqual(call.kwargs["attributes"]["device_class"], "battery")
        self.assertEqual(call.kwargs["attributes"]["unit_of_measurement"], "%")

    async def test_none_fields_are_not_published_holding_last_value(self):
        app = _base_app()
        diag = {
            "slug": "control_centre", "battery_level": None, "rssi": -27, "lqi": None,
            "online": True, "errors": [],
        }
        await app._publish(diag)
        entity_ids = self._entity_ids(app)
        self.assertNotIn("sensor.salus_control_centre_battery", entity_ids)
        self.assertNotIn("sensor.salus_control_centre_lqi", entity_ids)
        self.assertIn("sensor.salus_control_centre_rssi", entity_ids)
        # connectivity/problem always publish when the device appeared in the poll at all
        self.assertIn("sensor.salus_control_centre_connectivity", entity_ids)
        self.assertIn("sensor.salus_control_centre_problem", entity_ids)

    async def test_online_none_does_not_publish_connectivity(self):
        app = _base_app()
        diag = {
            "slug": "x_thermostat", "battery_level": None, "rssi": None, "lqi": None,
            "online": None, "errors": [],
        }
        await app._publish(diag)
        self.assertNotIn("sensor.salus_x_thermostat_connectivity", self._entity_ids(app))

    async def test_zero_battery_level_survives_as_the_string_zero(self):
        app = _base_app()
        diag = {
            "slug": "x_thermostat", "battery_level": 0, "rssi": 0, "lqi": 0,
            "online": False, "errors": [],
        }
        await app._publish(diag)
        battery_call = self._call_for(app, "sensor.salus_x_thermostat_battery")
        self.assertEqual(battery_call.kwargs["state"], "0")
        self.assertIsInstance(battery_call.kwargs["state"], str)
        self.assertEqual(battery_call.kwargs["attributes"]["raw_level"], "0")
        rssi_call = self._call_for(app, "sensor.salus_x_thermostat_rssi")
        self.assertEqual(rssi_call.kwargs["state"], "0")
        lqi_call = self._call_for(app, "sensor.salus_x_thermostat_lqi")
        self.assertEqual(lqi_call.kwargs["state"], "0")

    async def test_offline_connectivity_state_is_off(self):
        app = _base_app()
        diag = {
            "slug": "x_thermostat", "battery_level": None, "rssi": None, "lqi": None,
            "online": False, "errors": [],
        }
        await app._publish(diag)
        conn_call = self._call_for(app, "sensor.salus_x_thermostat_connectivity")
        self.assertEqual(conn_call.kwargs["state"], "off")

    async def test_every_call_uses_replace_true(self):
        app = _base_app()
        await app._publish(self.FULL_DIAG)
        for c in app.set_state.call_args_list:
            self.assertTrue(c.kwargs.get("replace"))

    async def test_problem_on_state_carries_errors_attribute(self):
        app = _base_app()
        diag = {
            "slug": "bedroom_thermostat", "battery_level": None, "rssi": None, "lqi": None,
            "online": None, "errors": ["Error07", "ErrorCodeWC_d"],
        }
        await app._publish(diag)
        call = self._call_for(app, "sensor.salus_bedroom_thermostat_problem")
        self.assertEqual(call.kwargs["state"], "on")
        self.assertEqual(call.kwargs["attributes"]["errors"], ["Error07", "ErrorCodeWC_d"])

    async def test_healthy_problem_state_is_off_with_empty_errors(self):
        app = _base_app()
        diag = {
            "slug": "bedroom_thermostat", "battery_level": None, "rssi": None, "lqi": None,
            "online": None, "errors": [],
        }
        await app._publish(diag)
        call = self._call_for(app, "sensor.salus_bedroom_thermostat_problem")
        self.assertEqual(call.kwargs["state"], "off")
        self.assertEqual(call.kwargs["attributes"]["errors"], [])

    async def test_publish_failure_is_caught_and_logged_not_raised(self):
        app = _base_app()
        app.set_state.side_effect = RuntimeError("boom")
        await app._publish(self.FULL_DIAG)  # must not raise
        app.log.assert_called()


if __name__ == "__main__":
    unittest.main()
