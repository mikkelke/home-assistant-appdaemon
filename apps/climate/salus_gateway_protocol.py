"""
Salus iT600 gateway wire protocol -- pure logic, no I/O, no AppDaemon imports.

ZERO network/AppDaemon dependencies here, same split as climate_model.py: this module
holds the cipher, the request/response envelope shapes and the raw-register parsing;
salus_gateway_diagnostics.py (the app) owns all the actual HTTP calls and self.set_state
publishing. Testable without a running AppDaemon or a live gateway.

Why a from-scratch AES here instead of importing the vendored HA integration's client
(~/repositories/homeassistant_salus/custom_components/salus/pyit600): its encryptor.py
depends on the `cryptography` package, which is installed inside Home Assistant's own
container but confirmed ABSENT from the AppDaemon container (this app's whole reason
to exist is to survive an HA-side integration swap, so depending on an HA-side package
would reintroduce the same coupling by another name). The gateway's request/response
SHAPE (readall -> filter -> batched deviceid, URL, JSON envelope, "status": "success"
check) is still reused/ported from that client, near-verbatim -- only the encryption
primitive is reimplemented, and AES-128/256 is a fully specified, unchanging algorithm
(FIPS-197), not a reverse-engineered guess.

The gateway's cipher (see pyit600's IT600Encryptor): a fixed IV, and a key formed from
an MD5 digest of "Salus-<lowercased euid>" PADDED with 16 zero bytes to 32 bytes total.
32 bytes selects the AES-256 key schedule/round count (14 rounds) even though only the
first 16 bytes carry any entropy -- a quirk of the gateway's own protocol, reproduced
here byte-for-byte because that's what a real gateway expects on the wire, not a choice
made in this code. Cross-validated byte-for-byte against the real, cryptography-backed
IT600Encryptor during development (see the deploy report); tests/test_salus_gateway_protocol.py
pins both an official FIPS-197 AES-256 known-answer vector (the raw block cipher) and
concrete (euid, plaintext, ciphertext) fixtures captured from that cross-validation (the
full key-derivation + CBC + PKCS7 pipeline) so nobody needs `cryptography` installed to
run the test suite.
"""

from __future__ import annotations

import hashlib
import json
import re

# ------------------------------------------------------------- AES (FIPS-197)

_SBOX = (
    0x63, 0x7C, 0x77, 0x7B, 0xF2, 0x6B, 0x6F, 0xC5, 0x30, 0x01, 0x67, 0x2B, 0xFE, 0xD7, 0xAB, 0x76,
    0xCA, 0x82, 0xC9, 0x7D, 0xFA, 0x59, 0x47, 0xF0, 0xAD, 0xD4, 0xA2, 0xAF, 0x9C, 0xA4, 0x72, 0xC0,
    0xB7, 0xFD, 0x93, 0x26, 0x36, 0x3F, 0xF7, 0xCC, 0x34, 0xA5, 0xE5, 0xF1, 0x71, 0xD8, 0x31, 0x15,
    0x04, 0xC7, 0x23, 0xC3, 0x18, 0x96, 0x05, 0x9A, 0x07, 0x12, 0x80, 0xE2, 0xEB, 0x27, 0xB2, 0x75,
    0x09, 0x83, 0x2C, 0x1A, 0x1B, 0x6E, 0x5A, 0xA0, 0x52, 0x3B, 0xD6, 0xB3, 0x29, 0xE3, 0x2F, 0x84,
    0x53, 0xD1, 0x00, 0xED, 0x20, 0xFC, 0xB1, 0x5B, 0x6A, 0xCB, 0xBE, 0x39, 0x4A, 0x4C, 0x58, 0xCF,
    0xD0, 0xEF, 0xAA, 0xFB, 0x43, 0x4D, 0x33, 0x85, 0x45, 0xF9, 0x02, 0x7F, 0x50, 0x3C, 0x9F, 0xA8,
    0x51, 0xA3, 0x40, 0x8F, 0x92, 0x9D, 0x38, 0xF5, 0xBC, 0xB6, 0xDA, 0x21, 0x10, 0xFF, 0xF3, 0xD2,
    0xCD, 0x0C, 0x13, 0xEC, 0x5F, 0x97, 0x44, 0x17, 0xC4, 0xA7, 0x7E, 0x3D, 0x64, 0x5D, 0x19, 0x73,
    0x60, 0x81, 0x4F, 0xDC, 0x22, 0x2A, 0x90, 0x88, 0x46, 0xEE, 0xB8, 0x14, 0xDE, 0x5E, 0x0B, 0xDB,
    0xE0, 0x32, 0x3A, 0x0A, 0x49, 0x06, 0x24, 0x5C, 0xC2, 0xD3, 0xAC, 0x62, 0x91, 0x95, 0xE4, 0x79,
    0xE7, 0xC8, 0x37, 0x6D, 0x8D, 0xD5, 0x4E, 0xA9, 0x6C, 0x56, 0xF4, 0xEA, 0x65, 0x7A, 0xAE, 0x08,
    0xBA, 0x78, 0x25, 0x2E, 0x1C, 0xA6, 0xB4, 0xC6, 0xE8, 0xDD, 0x74, 0x1F, 0x4B, 0xBD, 0x8B, 0x8A,
    0x70, 0x3E, 0xB5, 0x66, 0x48, 0x03, 0xF6, 0x0E, 0x61, 0x35, 0x57, 0xB9, 0x86, 0xC1, 0x1D, 0x9E,
    0xE1, 0xF8, 0x98, 0x11, 0x69, 0xD9, 0x8E, 0x94, 0x9B, 0x1E, 0x87, 0xE9, 0xCE, 0x55, 0x28, 0xDF,
    0x8C, 0xA1, 0x89, 0x0D, 0xBF, 0xE6, 0x42, 0x68, 0x41, 0x99, 0x2D, 0x0F, 0xB0, 0x54, 0xBB, 0x16,
)
# Derived, not transcribed: eliminates a second hand-copied table as a source of error.
_INV_SBOX = [0] * 256
for _i, _v in enumerate(_SBOX):
    _INV_SBOX[_v] = _i
_INV_SBOX = tuple(_INV_SBOX)


def _xtime(a: int) -> int:
    a <<= 1
    if a & 0x100:
        a ^= 0x11B
    return a & 0xFF


def _gmul(a: int, b: int) -> int:
    """Multiply two bytes in GF(2^8) with AES's reduction polynomial (peasant
    multiplication) -- used by MixColumns/InvMixColumns."""
    p = 0
    for _ in range(8):
        if b & 1:
            p ^= a
        hi = a & 0x80
        a = (a << 1) & 0xFF
        if hi:
            a ^= 0x1B
        b >>= 1
    return p & 0xFF


def _key_expansion(key: bytes):
    """Rijndael key schedule generalized over Nk in {4, 6, 8} words (AES-128/192/256).
    Returns (words, rounds). AES-256 (Nk=8) has an extra SubWord-only step every 4th
    word beyond the RotWord+SubWord+Rcon step every Nk-th word."""
    nk = len(key) // 4
    if nk not in (4, 6, 8):
        raise ValueError(f"unsupported AES key length: {len(key)} bytes")
    rounds = nk + 6
    nb = 4
    words = [list(key[4 * i:4 * i + 4]) for i in range(nk)]
    rcon = 1
    for i in range(nk, nb * (rounds + 1)):
        temp = list(words[i - 1])
        if i % nk == 0:
            temp = temp[1:] + temp[:1]
            temp = [_SBOX[b] for b in temp]
            temp[0] ^= rcon
            rcon = _xtime(rcon)
        elif nk > 6 and i % nk == 4:
            temp = [_SBOX[b] for b in temp]
        words.append([words[i - nk][j] ^ temp[j] for j in range(4)])
    return words, rounds


def _add_round_key(state: bytearray, words, round_idx: int) -> None:
    for c in range(4):
        word = words[round_idx * 4 + c]
        for r in range(4):
            state[r + 4 * c] ^= word[r]


def _sub_bytes(state: bytearray, sbox) -> None:
    for i in range(16):
        state[i] = sbox[state[i]]


def _shift_rows(state: bytearray) -> None:
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[r:] + row[:r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _inv_shift_rows(state: bytearray) -> None:
    for r in range(1, 4):
        row = [state[r + 4 * c] for c in range(4)]
        row = row[-r:] + row[:-r]
        for c in range(4):
            state[r + 4 * c] = row[c]


def _mix_columns(state: bytearray) -> None:
    for c in range(4):
        a0, a1, a2, a3 = (state[4 * c], state[1 + 4 * c], state[2 + 4 * c], state[3 + 4 * c])
        state[4 * c] = _gmul(a0, 2) ^ _gmul(a1, 3) ^ a2 ^ a3
        state[1 + 4 * c] = a0 ^ _gmul(a1, 2) ^ _gmul(a2, 3) ^ a3
        state[2 + 4 * c] = a0 ^ a1 ^ _gmul(a2, 2) ^ _gmul(a3, 3)
        state[3 + 4 * c] = _gmul(a0, 3) ^ a1 ^ a2 ^ _gmul(a3, 2)


def _inv_mix_columns(state: bytearray) -> None:
    for c in range(4):
        a0, a1, a2, a3 = (state[4 * c], state[1 + 4 * c], state[2 + 4 * c], state[3 + 4 * c])
        state[4 * c] = _gmul(a0, 14) ^ _gmul(a1, 11) ^ _gmul(a2, 13) ^ _gmul(a3, 9)
        state[1 + 4 * c] = _gmul(a0, 9) ^ _gmul(a1, 14) ^ _gmul(a2, 11) ^ _gmul(a3, 13)
        state[2 + 4 * c] = _gmul(a0, 13) ^ _gmul(a1, 9) ^ _gmul(a2, 14) ^ _gmul(a3, 11)
        state[3 + 4 * c] = _gmul(a0, 11) ^ _gmul(a1, 13) ^ _gmul(a2, 9) ^ _gmul(a3, 14)


def _encrypt_block(block: bytes, words, rounds: int) -> bytes:
    state = bytearray(block)
    _add_round_key(state, words, 0)
    for rnd in range(1, rounds):
        _sub_bytes(state, _SBOX)
        _shift_rows(state)
        _mix_columns(state)
        _add_round_key(state, words, rnd)
    _sub_bytes(state, _SBOX)
    _shift_rows(state)
    _add_round_key(state, words, rounds)
    return bytes(state)


def _decrypt_block(block: bytes, words, rounds: int) -> bytes:
    state = bytearray(block)
    _add_round_key(state, words, rounds)
    for rnd in range(rounds - 1, 0, -1):
        _inv_shift_rows(state)
        _sub_bytes(state, _INV_SBOX)
        _add_round_key(state, words, rnd)
        _inv_mix_columns(state)
    _inv_shift_rows(state)
    _sub_bytes(state, _INV_SBOX)
    _add_round_key(state, words, 0)
    return bytes(state)


def _pkcs7_pad(data: bytes, block_size: int = 16) -> bytes:
    pad_len = block_size - (len(data) % block_size)
    return data + bytes([pad_len]) * pad_len


def _pkcs7_unpad(data: bytes) -> bytes:
    if not data:
        raise ValueError("cannot unpad empty data")
    pad_len = data[-1]
    if pad_len < 1 or pad_len > 16 or pad_len > len(data):
        raise ValueError("invalid PKCS7 padding")
    if data[-pad_len:] != bytes([pad_len]) * pad_len:
        raise ValueError("invalid PKCS7 padding")
    return data[:-pad_len]


def cbc_encrypt(plaintext: bytes, key: bytes, iv: bytes) -> bytes:
    words, rounds = _key_expansion(key)
    padded = _pkcs7_pad(plaintext)
    prev = iv
    out = bytearray()
    for i in range(0, len(padded), 16):
        block = bytes(x ^ y for x, y in zip(padded[i:i + 16], prev))
        enc = _encrypt_block(block, words, rounds)
        out += enc
        prev = enc
    return bytes(out)


def cbc_decrypt(ciphertext: bytes, key: bytes, iv: bytes) -> bytes:
    if len(ciphertext) % 16 != 0 or not ciphertext:
        raise ValueError("ciphertext must be a non-empty multiple of the 16-byte block size")
    words, rounds = _key_expansion(key)
    prev = iv
    out = bytearray()
    for i in range(0, len(ciphertext), 16):
        block = ciphertext[i:i + 16]
        dec = _decrypt_block(block, words, rounds)
        out += bytes(x ^ y for x, y in zip(dec, prev))
        prev = block
    return _pkcs7_unpad(bytes(out))


# ------------------------------------------------------------- Salus wire cipher

# Fixed IV, byte-for-byte from pyit600.encryptor.IT600Encryptor.
_GATEWAY_IV = bytes([0x88, 0xA6, 0xB0, 0x79, 0x5D, 0x85, 0xDB, 0xFC,
                     0xE6, 0xE0, 0xB3, 0xE9, 0xA6, 0x29, 0x65, 0x4B])


def _derive_key(euid: str) -> bytes:
    """MD5("Salus-<lowercased euid>") + 16 zero bytes -> 32-byte AES-256 key, exactly
    as pyit600.encryptor.IT600Encryptor derives it. Never log the input or the output:
    the euid IS the gateway's credential (see the app's module docstring)."""
    return hashlib.md5(f"Salus-{euid.lower()}".encode("utf-8")).digest() + bytes(16)


class GatewayCipher:
    """Encrypt/decrypt gateway request/response bodies. Same constructor input and the
    same two methods as pyit600.encryptor.IT600Encryptor (encrypt(str) -> bytes,
    decrypt(bytes) -> str), so it's a drop-in mental model for anyone who already knows
    that class - just without the `cryptography` dependency (see module docstring)."""

    def __init__(self, euid: str):
        self._key = _derive_key(euid)

    def encrypt(self, plaintext: str) -> bytes:
        return cbc_encrypt(plaintext.encode("utf-8"), self._key, _GATEWAY_IV)

    def decrypt(self, ciphertext: bytes) -> str:
        return cbc_decrypt(ciphertext, self._key, _GATEWAY_IV).decode("utf-8")


# ------------------------------------------------------------- request/response envelope

class GatewayResponseError(Exception):
    """The gateway answered but the decrypted envelope wasn't a success - same check as
    pyit600's IT600Gateway._make_encrypted_request."""


def readall_body() -> dict:
    """The gateway's own "give me every device, minimally" request. Real per-device
    detail (BatteryLevel, Error* registers, ...) is NOT in this response - only enough
    to classify devices (see is_relevant_record) and their `data` identity handle for a
    follow-up deviceid request. Mirrors pyit600.gateway's own readall/deviceid two-step."""
    return {"requestAttr": "readall"}


def deviceid_body(records) -> dict:
    """Batched full-detail request for a list of readall records. Thermostats and the
    wiring centre are requested TOGETHER in one call (see the app's module docstring on
    why request count is minimized) - the gateway's `id` list accepts any mix of device
    `data` handles regardless of device type, same as every deviceid call pyit600.gateway
    itself makes."""
    return {"requestAttr": "deviceid", "id": [{"data": r["data"]} for r in records]}


def unwrap_response(parsed) -> dict:
    if not isinstance(parsed, dict) or parsed.get("status") != "success":
        raise GatewayResponseError(f"gateway did not return success: {parsed!r}")
    return parsed


# ------------------------------------------------------------- device classification

def is_thermostat_record(record: dict) -> bool:
    return isinstance(record, dict) and "sIT600TH" in record


def is_wiring_centre_record(record: dict) -> bool:
    if not isinstance(record, dict):
        return False
    basic = record.get("sBasicS") or {}
    return basic.get("ModelIdentifier") == "it600WC"


def is_relevant_record(record: dict) -> bool:
    """Readall-pass filter: only thermostats (sIT600TH present) and the wiring centre
    (sBasicS.ModelIdentifier == "it600WC") get a follow-up deviceid read - the same
    classification pyit600.gateway uses for its climate/binary_sensor platforms,
    applied to the same minimal readall records."""
    return is_thermostat_record(record) or is_wiring_centre_record(record)


# ------------------------------------------------------------- naming

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name: str) -> str:
    """Lowercase snake_case slug from a device's human name - close enough to Home
    Assistant's own slugify that e.g. "Control Centre" -> "control_centre" lines up
    with the entity-id fragment the salus integration itself already produces for the
    SAME physical device, so salus_health.py's room labels read identically regardless
    of which source published the entity."""
    s = _SLUG_RE.sub("_", (name or "").strip().lower())
    return s.strip("_") or "device"


def device_name(record: dict) -> str:
    """The device's human name straight off the wire. sZDO.DeviceName is itself a
    JSON-encoded {"deviceName": "..."} string (same double-encoding pyit600.gateway
    unwraps); falls back to the device's UniID if the name is missing or malformed."""
    raw = (record.get("sZDO") or {}).get("DeviceName")
    if raw:
        try:
            parsed = json.loads(raw)
            name = parsed.get("deviceName") if isinstance(parsed, dict) else None
            if name:
                return name
        except (TypeError, ValueError):
            pass
    return str((record.get("data") or {}).get("UniID", "device"))


# ------------------------------------------------------------- fault registers

def register_is_active(value) -> bool:
    """True if a sIT600TH/sIT600WC Error* register value indicates an active fault.

    Most of these registers are plain 0/1 ints, where bool(value) is correct. The
    wiring centre's ErrorCodeWC_d is different: a hex-STRING aggregate ("0000" at
    baseline), and bool("0000") is True in Python since it's a non-empty string - a
    naive truthy test would report a phantom fault on every healthy wiring centre.
    Stripping "0" characters leaves an all-zero string empty (falsy) while any string
    carrying a set bit keeps a non-zero character (truthy). Independently reproduces
    pyit600.gateway._is_fault_register_active's fix (that vendored function is only
    ever applied to the binary_sensor path there, not the climate `errors` list it
    also builds - see the deploy report for why this app doesn't reuse that helper).
    """
    if isinstance(value, str):
        return value.strip("0") != ""
    return bool(value)


def active_errors(record: dict) -> list:
    """Sorted list of active Error* register NAMES across whichever of sIT600TH/
    sIT600WC sections this device carries (a device normally has at most one)."""
    th = record.get("sIT600TH") or {}
    wc = record.get("sIT600WC") or {}
    return sorted(
        key
        for section in (th, wc)
        for key, value in section.items()
        if key.startswith("Error") and register_is_active(value)
    )


# ------------------------------------------------------------- per-device extraction

def _coerce_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def extract_diagnostics(record: dict) -> dict:
    """Pull exactly the fields this app publishes out of one gateway "deviceid"
    response record (a full per-device status dict). Fields this app cannot currently
    read come back None, never a guess - the app layer treats None as "hold whatever
    was last published" (rssi/lqi are documented as intermittently absent even on a
    healthy device; see the app's module docstring).

    Returns a plain dict: slug, battery_level (0-5 or None), rssi (or None),
    lqi (or None), online (bool or None), errors (list, always present).
    """
    th = record.get("sIT600TH") or {}
    it600i = record.get("sIT600I") or {}
    zdo_info = record.get("sZDOInfo") or {}

    battery_level = _coerce_int(th.get("BatteryLevel")) if is_thermostat_record(record) else None
    rssi = _coerce_int(it600i.get("LastMessageRSSI_d"))
    lqi = _coerce_int(it600i.get("LastMessageLQI_d"))

    online = None
    if "OnlineStatus_i" in zdo_info:
        online = zdo_info["OnlineStatus_i"] == 1

    return {
        "slug": slugify(device_name(record)),
        "battery_level": battery_level,
        "rssi": rssi,
        "lqi": lqi,
        "online": online,
        "errors": active_errors(record),
    }
