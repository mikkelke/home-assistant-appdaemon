"""Unit tests for cover_util (no AppDaemon runtime)."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_BLINDS_DIR = Path(__file__).resolve().parents[1]
if str(_BLINDS_DIR) not in sys.path:
    sys.path.insert(0, str(_BLINDS_DIR))

import cover_util  # noqa: E402


class FakeHass:
    """Minimal Hass stub: only ``get_state(entity, attribute=...)`` is used.

    ``pos`` becomes the ``current_position`` attribute value verbatim (so a
    test can pass an int, a numeric string, ``None`` or a junk string). Set
    ``has_attr=False`` to simulate a cover with no ``current_position`` at all.
    """

    def __init__(self, pos=100, has_attr=True, raise_on_get=False):
        self._pos = pos
        self._has_attr = has_attr
        self._raise = raise_on_get

    def get_state(self, entity_id, attribute=None):
        if self._raise:
            raise RuntimeError("boom")
        if attribute == "current_position":
            return self._pos if self._has_attr else None
        return None


ENTITY = "cover.bedroom_blind"

# (position, expected is_closed at threshold=95) for a normal (closed_is_100) cover.
_NORMAL_CASES = [
    (100, True),
    (99, True),   # low-battery park - the case the old ``>= 100`` check missed
    (96, True),
    (95, True),
    (94, False),
    (38, False),
    (0, False),
]

# Same positions for an inverted cover (closed_is_100=False): closed near 0.
_INVERTED_CASES = [
    (100, False),
    (99, False),
    (96, False),
    (95, False),
    (94, False),
    (38, False),
    (0, True),
]


class TestPosition(unittest.TestCase):
    def test_reads_int(self):
        self.assertEqual(cover_util.position(FakeHass(96), ENTITY), 96)

    def test_reads_numeric_string(self):
        self.assertEqual(cover_util.position(FakeHass("99"), ENTITY), 99)

    def test_reads_float_string_truncates(self):
        self.assertEqual(cover_util.position(FakeHass("99.0"), ENTITY), 99)

    def test_reads_float(self):
        self.assertEqual(cover_util.position(FakeHass(38.7), ENTITY), 38)

    def test_none_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass(None), ENTITY))
        self.assertEqual(cover_util.position(FakeHass(None), ENTITY, default=0), 0)

    def test_unknown_string_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass("unknown"), ENTITY))
        self.assertIsNone(cover_util.position(FakeHass("unavailable"), ENTITY))

    def test_junk_string_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass("open"), ENTITY))

    def test_missing_attribute_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass(has_attr=False), ENTITY))

    def test_missing_entity_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass(100), None))

    def test_get_state_raises_returns_default(self):
        self.assertIsNone(cover_util.position(FakeHass(raise_on_get=True), ENTITY))

    def test_bool_is_not_a_position(self):
        # bool is an int subclass; must not be read as 1/0 position.
        self.assertIsNone(cover_util.position(FakeHass(True), ENTITY))


class TestIsClosedNormal(unittest.TestCase):
    """closed_is_100=True (default)."""

    def test_threshold_95_matrix(self):
        for pos, expected in _NORMAL_CASES:
            with self.subTest(pos=pos):
                self.assertEqual(cover_util.is_closed(FakeHass(pos), ENTITY), expected)

    def test_99_with_threshold_95_is_closed(self):
        """The headline regression: a blind parked at 99% must read as closed."""
        self.assertTrue(cover_util.is_closed(FakeHass(99), ENTITY, threshold=95))

    def test_99_with_hardcoded_100_would_be_open(self):
        """Sanity: the old semantics (threshold=100) would call 99% *open*."""
        self.assertFalse(cover_util.is_closed(FakeHass(99), ENTITY, threshold=100))

    def test_custom_threshold_passthrough(self):
        self.assertTrue(cover_util.is_closed(FakeHass(90), ENTITY, threshold=90))
        self.assertFalse(cover_util.is_closed(FakeHass(89), ENTITY, threshold=90))

    def test_unavailable_is_not_closed(self):
        self.assertFalse(cover_util.is_closed(FakeHass(None), ENTITY))
        self.assertFalse(cover_util.is_closed(FakeHass("unknown"), ENTITY))
        self.assertFalse(cover_util.is_closed(FakeHass(has_attr=False), ENTITY))


class TestIsClosedInverted(unittest.TestCase):
    """closed_is_100=False - closed near 0 (mirrors wakeup's pos==0 test)."""

    def test_threshold_95_matrix(self):
        for pos, expected in _INVERTED_CASES:
            with self.subTest(pos=pos):
                self.assertEqual(
                    cover_util.is_closed(FakeHass(pos), ENTITY, closed_is_100=False),
                    expected,
                )

    def test_unavailable_is_not_closed(self):
        self.assertFalse(
            cover_util.is_closed(FakeHass(None), ENTITY, closed_is_100=False)
        )


class TestIsOpen(unittest.TestCase):
    def test_normal_matrix_is_complement_when_known(self):
        for pos, closed in _NORMAL_CASES:
            with self.subTest(pos=pos):
                self.assertEqual(cover_util.is_open(FakeHass(pos), ENTITY), not closed)

    def test_inverted_matrix_is_complement_when_known(self):
        for pos, closed in _INVERTED_CASES:
            with self.subTest(pos=pos):
                self.assertEqual(
                    cover_util.is_open(FakeHass(pos), ENTITY, closed_is_100=False),
                    not closed,
                )

    def test_unavailable_is_neither_open_nor_closed(self):
        hass = FakeHass(None)
        self.assertFalse(cover_util.is_open(hass, ENTITY))
        self.assertFalse(cover_util.is_closed(hass, ENTITY))


if __name__ == "__main__":
    unittest.main()
