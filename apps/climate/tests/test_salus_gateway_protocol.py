# tests/test_salus_gateway_protocol.py - pure protocol/crypto/parsing module, ZERO
# appdaemon imports (see salus_gateway_protocol.py's module docstring), so no stub
# needed here. Cipher fixtures below were captured by cross-validating GatewayCipher
# byte-for-byte against pyit600.encryptor.IT600Encryptor (the real, cryptography-backed
# vendored client) during development - see the deploy report - so this suite proves
# wire-compatibility without needing the `cryptography` package installed to run.
# Run from repo root: python3 -m unittest discover -s apps/climate/tests -q

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import salus_gateway_protocol as proto  # noqa: E402


class SBoxIsAPermutation(unittest.TestCase):
    """Cheap, independent sanity check on the hand-transcribed table: catches a
    duplicate/omitted byte immediately, regardless of whether the cipher output
    happens to look plausible."""

    def test_sbox_is_a_bijection_of_0_255(self):
        self.assertEqual(sorted(proto._SBOX), list(range(256)))

    def test_inv_sbox_inverts_sbox(self):
        for i, v in enumerate(proto._SBOX):
            self.assertEqual(proto._INV_SBOX[v], i)


class AesKnownAnswer(unittest.TestCase):
    """Official FIPS-197 Appendix C.3 AES-256 test vector - the raw block cipher,
    independent of the Salus-specific key derivation/CBC/padding below."""

    KEY = bytes.fromhex("000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f")
    PLAINTEXT = bytes.fromhex("00112233445566778899aabbccddeeff")
    CIPHERTEXT = bytes.fromhex("8ea2b7ca516745bfeafc49904b496089")

    def test_key_expansion_uses_14_rounds_for_a_32_byte_key(self):
        _, rounds = proto._key_expansion(self.KEY)
        self.assertEqual(rounds, 14)

    def test_encrypt_block_matches_official_vector(self):
        words, rounds = proto._key_expansion(self.KEY)
        self.assertEqual(proto._encrypt_block(self.PLAINTEXT, words, rounds), self.CIPHERTEXT)

    def test_decrypt_block_matches_official_vector(self):
        words, rounds = proto._key_expansion(self.KEY)
        self.assertEqual(proto._decrypt_block(self.CIPHERTEXT, words, rounds), self.PLAINTEXT)

    def test_unsupported_key_length_raises(self):
        with self.assertRaises(ValueError):
            proto._key_expansion(b"short")


class Pkcs7(unittest.TestCase):
    def test_pad_then_unpad_roundtrips_every_length_across_two_blocks(self):
        for n in range(0, 33):
            data = bytes([7]) * n  # arbitrary n-byte payload
            padded = proto._pkcs7_pad(data)
            self.assertEqual(len(padded) % 16, 0)
            self.assertGreater(len(padded), 0)
            self.assertEqual(proto._pkcs7_unpad(padded), data)

    def test_unpad_rejects_empty_input(self):
        with self.assertRaises(ValueError):
            proto._pkcs7_unpad(b"")

    def test_unpad_rejects_corrupted_padding(self):
        # Last byte claims "5 pad bytes", but the preceding 4 padding bytes aren't 5.
        bad = bytes([1, 2, 3, 4, 9, 9, 9, 9, 5])
        with self.assertRaises(ValueError):
            proto._pkcs7_unpad(bad)


class CbcRoundTrip(unittest.TestCase):
    KEY = bytes(range(32))
    IV = bytes(range(16, 32))

    def test_roundtrips_across_a_range_of_lengths(self):
        for n in (0, 1, 15, 16, 17, 31, 32, 100):
            data = bytes([(i * 7) % 256 for i in range(n)])
            ct = proto.cbc_encrypt(data, self.KEY, self.IV)
            self.assertEqual(len(ct) % 16, 0)
            self.assertEqual(proto.cbc_decrypt(ct, self.KEY, self.IV), data)

    def test_decrypt_rejects_non_block_aligned_ciphertext(self):
        with self.assertRaises(ValueError):
            proto.cbc_decrypt(b"short", self.KEY, self.IV)

    def test_decrypt_rejects_empty_ciphertext(self):
        with self.assertRaises(ValueError):
            proto.cbc_decrypt(b"", self.KEY, self.IV)


class GatewayCipherFixtures(unittest.TestCase):
    """Concrete (euid, plaintext, ciphertext) triples captured from a real cross-check
    against pyit600.encryptor.IT600Encryptor (cryptography-backed) - see the deploy
    report. Proves GatewayCipher is wire-compatible without needing `cryptography`
    installed to run this suite."""

    FIXTURES = [
        (
            "ABCDEF0123456789",
            '{"requestAttr": "readall"}',
            "f8de990a049b402c87dda18a4cf3a2ada9ece15b91d5c3c41ae770d8ae833b2e",
        ),
        (
            "0000000000000000",
            '{"requestAttr": "deviceid", "id": [{"data": {"UniID": "001e5e0902916bfb", "Endpoint": 1}}]}',
            "64ab31e7df8f66daa543d8548ffaacaa9f79c611555fe9e7b092e2277a68df7"
            "695d3255b8e200704b60490c02401e6390afae4c0972ac2c2d941d70a7428fa"
            "6a3a056fe32505049f1e8924089a0019328fbe78e1acf18043b06fb8c034710c7d",
        ),
        (
            "aB3xZ9k2Mn7pQ1rS",
            "hello",
            "71089b74df76f1fb57fe43394f1ae5d8",
        ),
    ]

    def test_encrypt_matches_captured_ciphertext(self):
        for euid, plaintext, ct_hex in self.FIXTURES:
            cipher = proto.GatewayCipher(euid)
            self.assertEqual(cipher.encrypt(plaintext).hex(), ct_hex)

    def test_decrypt_recovers_captured_plaintext(self):
        for euid, plaintext, ct_hex in self.FIXTURES:
            cipher = proto.GatewayCipher(euid)
            self.assertEqual(cipher.decrypt(bytes.fromhex(ct_hex)), plaintext)

    def test_euid_is_lowercased_before_hashing(self):
        # "Salus-<lower(euid)>" - an upper vs lower-case token must derive the same key.
        lower = proto.GatewayCipher("abcdef0123456789")
        upper = proto.GatewayCipher("ABCDEF0123456789")
        pt = "same key either way"
        self.assertEqual(lower.encrypt(pt), upper.encrypt(pt))


class RegisterIsActive(unittest.TestCase):
    """The ErrorCodeWC_d trap: it's a hex-STRING aggregate ("0000" when healthy), and
    bool("0000") is True in Python (non-empty string) - a naive truthy check would
    report a phantom fault on every healthy wiring centre. Confirmed against the real
    gateway's live sIT600WC.ErrorCodeWC_d value during development (see deploy report)."""

    def test_bare_bool_of_the_healthy_string_would_be_wrong(self):
        # Documents WHY the naive check is wrong - guards a regression back to bool(value).
        self.assertTrue(bool("0000"))

    def test_all_zero_hex_string_is_not_active(self):
        self.assertFalse(proto.register_is_active("0000"))

    def test_hex_string_with_any_set_bit_is_active(self):
        self.assertTrue(proto.register_is_active("0001"))
        self.assertTrue(proto.register_is_active("8000"))
        self.assertTrue(proto.register_is_active("0100"))

    def test_numeric_zero_is_not_active(self):
        self.assertFalse(proto.register_is_active(0))

    def test_numeric_one_is_active(self):
        self.assertTrue(proto.register_is_active(1))


class ActiveErrors(unittest.TestCase):
    def test_scans_both_th_and_wc_sections_and_skips_healthy_registers(self):
        record = {
            "sIT600TH": {"Error01": 0, "Error02": 1, "BatteryLevel": 4},
            "sIT600WC": {"Error10": 0, "ErrorCodeWC_d": "0000"},
        }
        self.assertEqual(proto.active_errors(record), ["Error02"])

    def test_error_code_wc_d_healthy_string_excluded_end_to_end(self):
        record = {"sIT600WC": {"ErrorCodeWC_d": "0000", "Error10": 0}}
        self.assertEqual(proto.active_errors(record), [])

    def test_error_code_wc_d_with_set_bit_included_end_to_end(self):
        record = {"sIT600WC": {"ErrorCodeWC_d": "0002", "Error10": 0}}
        self.assertEqual(proto.active_errors(record), ["ErrorCodeWC_d"])

    def test_no_relevant_sections_is_empty(self):
        self.assertEqual(proto.active_errors({}), [])


class DeviceClassification(unittest.TestCase):
    def test_thermostat_record(self):
        record = {"sIT600TH": {"BatteryLevel": 4}}
        self.assertTrue(proto.is_thermostat_record(record))
        self.assertFalse(proto.is_wiring_centre_record(record))
        self.assertTrue(proto.is_relevant_record(record))

    def test_wiring_centre_record(self):
        record = {"sBasicS": {"ModelIdentifier": "it600WC"}}
        self.assertFalse(proto.is_thermostat_record(record))
        self.assertTrue(proto.is_wiring_centre_record(record))
        self.assertTrue(proto.is_relevant_record(record))

    def test_unrelated_record_is_not_relevant(self):
        record = {"sIASZS": {"ErrorIASZSAlarmed1": 0}, "sBasicS": {"ModelIdentifier": "SW600"}}
        self.assertFalse(proto.is_relevant_record(record))

    def test_non_dict_input_is_safe(self):
        self.assertFalse(proto.is_thermostat_record(None))
        self.assertFalse(proto.is_wiring_centre_record(None))
        self.assertFalse(proto.is_relevant_record(None))


class Slugify(unittest.TestCase):
    def test_room_and_thermostat_name(self):
        self.assertEqual(proto.slugify("Bedroom Thermostat"), "bedroom_thermostat")

    def test_control_centre_matches_integration_naming(self):
        # Same slug the salus integration itself derives for the SAME physical device -
        # see salus_gateway_protocol.slugify's docstring.
        self.assertEqual(proto.slugify("Control Centre"), "control_centre")

    def test_collapses_punctuation_and_strips_edges(self):
        self.assertEqual(proto.slugify("  Claudia's  Room! "), "claudia_s_room")

    def test_empty_or_none_falls_back_to_device(self):
        self.assertEqual(proto.slugify(""), "device")
        self.assertEqual(proto.slugify(None), "device")


class DeviceName(unittest.TestCase):
    def test_reads_json_encoded_device_name(self):
        record = {"sZDO": {"DeviceName": '{"deviceName": "Bedroom Thermostat"}'}}
        self.assertEqual(proto.device_name(record), "Bedroom Thermostat")

    def test_malformed_json_falls_back_to_uniid(self):
        record = {"sZDO": {"DeviceName": "not json"}, "data": {"UniID": "001e5e0902916bfb"}}
        self.assertEqual(proto.device_name(record), "001e5e0902916bfb")

    def test_missing_section_falls_back_to_uniid(self):
        record = {"data": {"UniID": "001e5e0902916bfb"}}
        self.assertEqual(proto.device_name(record), "001e5e0902916bfb")

    def test_nothing_at_all_falls_back_to_device(self):
        self.assertEqual(proto.device_name({}), "device")


class ExtractDiagnostics(unittest.TestCase):
    def test_thermostat_full_reading(self):
        record = {
            "sZDO": {"DeviceName": '{"deviceName": "Bedroom Thermostat"}'},
            "sIT600TH": {"BatteryLevel": 4, "Error01": 0},
            "sIT600I": {"LastMessageRSSI_d": -55, "LastMessageLQI_d": 200},
            "sZDOInfo": {"OnlineStatus_i": 1},
        }
        diag = proto.extract_diagnostics(record)
        self.assertEqual(diag["slug"], "bedroom_thermostat")
        self.assertEqual(diag["battery_level"], 4)
        self.assertEqual(diag["rssi"], -55)
        self.assertEqual(diag["lqi"], 200)
        self.assertTrue(diag["online"])
        self.assertEqual(diag["errors"], [])

    def test_rssi_and_lqi_intermittently_absent_come_back_none(self):
        # Observed live on real, healthy thermostats (sIT600I section simply missing
        # from that read) - the app must hold the last published value, not flap to
        # unknown. See module docstring.
        record = {
            "sZDO": {"DeviceName": '{"deviceName": "Bathroom Thermostat"}'},
            "sIT600TH": {"BatteryLevel": 5},
            "sZDOInfo": {"OnlineStatus_i": 1},
        }
        diag = proto.extract_diagnostics(record)
        self.assertIsNone(diag["rssi"])
        self.assertIsNone(diag["lqi"])

    def test_wiring_centre_has_no_battery(self):
        record = {
            "sZDO": {"DeviceName": '{"deviceName": "Control Centre"}'},
            "sBasicS": {"ModelIdentifier": "it600WC"},
            "sIT600WC": {"Error10": 0, "ErrorCodeWC_d": "0000"},
            "sIT600I": {"LastMessageRSSI_d": -27, "LastMessageLQI_d": 255},
            "sZDOInfo": {"OnlineStatus_i": 1},
        }
        diag = proto.extract_diagnostics(record)
        self.assertEqual(diag["slug"], "control_centre")
        self.assertIsNone(diag["battery_level"])
        self.assertEqual(diag["rssi"], -27)
        self.assertTrue(diag["online"])
        self.assertEqual(diag["errors"], [])

    def test_offline_device(self):
        record = {
            "sZDO": {"DeviceName": '{"deviceName": "Family Room Thermostat"}'},
            "sIT600TH": {"BatteryLevel": 3},
            "sZDOInfo": {"OnlineStatus_i": 0},
        }
        diag = proto.extract_diagnostics(record)
        self.assertFalse(diag["online"])

    def test_missing_zdo_info_online_is_none_not_false(self):
        # Absence must mean "hold last value", never a false "offline" report.
        record = {"sZDO": {"DeviceName": '{"deviceName": "X Thermostat"}'}, "sIT600TH": {"BatteryLevel": 3}}
        diag = proto.extract_diagnostics(record)
        self.assertIsNone(diag["online"])

    def test_active_fault_surfaces_in_errors(self):
        record = {
            "sZDO": {"DeviceName": '{"deviceName": "Kristines Room Thermostat"}'},
            "sIT600TH": {"BatteryLevel": 4, "Error07": 1},
            "sZDOInfo": {"OnlineStatus_i": 1},
        }
        diag = proto.extract_diagnostics(record)
        self.assertEqual(diag["errors"], ["Error07"])


class Envelope(unittest.TestCase):
    def test_readall_body(self):
        self.assertEqual(proto.readall_body(), {"requestAttr": "readall"})

    def test_deviceid_body_extracts_data_handles(self):
        records = [{"data": {"UniID": "a"}}, {"data": {"UniID": "b", "Endpoint": 2}}]
        body = proto.deviceid_body(records)
        self.assertEqual(body["requestAttr"], "deviceid")
        self.assertEqual(body["id"], [{"data": {"UniID": "a"}}, {"data": {"UniID": "b", "Endpoint": 2}}])

    def test_unwrap_response_returns_dict_on_success(self):
        parsed = {"status": "success", "id": []}
        self.assertIs(proto.unwrap_response(parsed), parsed)

    def test_unwrap_response_raises_on_non_success_status(self):
        with self.assertRaises(proto.GatewayResponseError):
            proto.unwrap_response({"status": "failed"})

    def test_unwrap_response_raises_on_non_dict(self):
        with self.assertRaises(proto.GatewayResponseError):
            proto.unwrap_response(None)


if __name__ == "__main__":
    unittest.main()
