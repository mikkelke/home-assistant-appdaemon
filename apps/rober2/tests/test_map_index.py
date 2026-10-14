# tests/test_map_index.py - the Rober2 map archive's index.json: both writers
# replace it atomically, a fresh index is never built beside older maps, the
# cleanup deletes nothing when the index is missing, unreadable or invalid, and
# otherwise keeps every map younger than map_retention_days.
# The appdaemon package is stubbed and the REAL control module is imported;
# instances are built bare via __new__.
# Run from repo root: python3 -m unittest discover -s apps/rober2/tests -q

import asyncio
import json
import os
import re
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock

_hassapi = types.ModuleType("appdaemon.plugins.hass.hassapi")
_hassapi.Hass = object
for name, mod in (
    ("appdaemon", types.ModuleType("appdaemon")),
    ("appdaemon.plugins", types.ModuleType("appdaemon.plugins")),
    ("appdaemon.plugins.hass", types.ModuleType("appdaemon.plugins.hass")),
    ("appdaemon.plugins.hass.hassapi", _hassapi),
):
    sys.modules.setdefault(name, mod)

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import control  # noqa: E402

PNG = b"\x89PNG\r\n\x1a\n" + b"x" * 32


def _entry(room, stamp, iso):
    filename = f"rober2_map_{stamp}_{room}.png"
    return {"filename": filename, "timestamp": stamp, "datetime": iso, "room": room,
            "url": f"{control.MAP_URL_PREFIX}/{filename}"}


def _aged(room, days):
    """An index entry saved `days` ago, as the app writes it (house wall clock, no offset)."""
    saved = datetime.now() - timedelta(days=days)
    return _entry(room, saved.strftime("%Y%m%d_%H%M%S"), saved.isoformat())


class MapIndexTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.dir = Path(self.tmp.name) / "rober2_maps"
        self.dir.mkdir()
        self.index = self.dir / "index.json"
        self.app = control.Rober2Control.__new__(control.Rober2Control)
        self.app.map_save_dir = str(self.dir)
        self.app.map_retention_days = 365
        self.logs = []
        self.app.log = lambda msg, level="INFO": self.logs.append((level, msg))

    def _maps(self, *entries):
        for e in entries:
            (self.dir / e["filename"]).write_bytes(PNG)
        return list(entries)

    def _write_index(self, maps):
        self.index.write_text(json.dumps({"maps": maps}, indent=2))

    def _update(self, filename, room):
        (self.dir / filename).write_bytes(PNG)
        asyncio.run(self.app._update_map_index(filename, "20261014_120000", room))

    def _cleanup(self):
        asyncio.run(self.app.cleanup_old_map_images({}))

    def _files(self):
        return sorted(p.name for p in self.dir.iterdir() if control.is_map_file(p.name))

    def _warned(self, text):
        return any(level == "WARNING" and text in msg for level, msg in self.logs)

    # --- the index writer ---

    def test_new_entry_goes_first_with_its_media_url(self):
        older = self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        self._write_index(older)
        self._update("rober2_map_20261014_120000_kitchen.png", "kitchen")
        maps = json.loads(self.index.read_text())["maps"]
        self.assertEqual([m["room"] for m in maps], ["kitchen", "hallway"])
        self.assertEqual(maps[0]["url"], "/media/local/rober2_maps/rober2_map_20261014_120000_kitchen.png")
        self.assertFalse((self.dir / "index.json.tmp").exists())

    def test_first_map_ever_starts_the_index(self):
        self._update("rober2_map_20261014_120000_kitchen.png", "kitchen")
        maps = json.loads(self.index.read_text())["maps"]
        self.assertEqual([m["filename"] for m in maps], ["rober2_map_20261014_120000_kitchen.png"])

    def test_a_failed_write_leaves_the_old_index_whole(self):
        older = self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        self._write_index(older)
        before = self.index.read_text()

        def half_then_crash(data, f, **kwargs):
            f.write('{"maps": [')
            raise OSError("disk full")

        with mock.patch.object(control.json, "dump", side_effect=half_then_crash):
            self._update("rober2_map_20261014_120000_kitchen.png", "kitchen")
        self.assertEqual(self.index.read_text(), before)
        self.assertTrue(self._warned("Error updating map index"))

    def test_an_invalid_index_is_never_replaced_by_a_fresh_one(self):
        self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        for broken in ('{"maps": [', '{"images": []}', "[]", ""):
            with self.subTest(index=broken):
                self.index.write_text(broken)
                self._update("rober2_map_20261014_120000_kitchen.png", "kitchen")
                self.assertEqual(self.index.read_text(), broken)
                self.assertTrue(self._warned("Map index unusable"))

    def test_a_missing_index_beside_older_maps_is_not_recreated(self):
        self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        self._update("rober2_map_20261014_120000_kitchen.png", "kitchen")
        self.assertFalse(self.index.exists())
        self.assertTrue(self._warned("Map index missing beside 1 maps"))

    # --- the cleanup ---

    def test_cleanup_keeps_every_map_of_the_last_year_whatever_the_room(self):
        young = [_aged("kitchen", d) for d in (1, 40, 120, 250, 364)]
        old = [_aged("kitchen", 366), _aged("hallway", 500)]
        self._maps(*young, *old)
        self._write_index(young + old)
        self._cleanup()
        self.assertEqual(self._files(), sorted(e["filename"] for e in young))
        maps = json.loads(self.index.read_text())["maps"]
        self.assertEqual([m["filename"] for m in maps], [e["filename"] for e in young])
        self.assertFalse(any(level == "WARNING" for level, _ in self.logs))
        self.assertIn(("INFO", "Map cleanup: deleted 2 map image(s) older than 365 days"), self.logs)

    def test_cleanup_leaves_index_and_disk_agreeing(self):
        on_disk = _aged("hallway", 3)
        gone = _aged("living_room", 5)  # its file was deleted by hand
        self._maps(on_disk)
        young_orphan = _aged("bedroom", 30)["filename"]
        old_orphan = _aged("bathroom", 400)["filename"]
        for name in (young_orphan, old_orphan):
            (self.dir / name).write_bytes(PNG)
        (self.dir / "notes.txt").write_text("not ours")
        self._write_index([on_disk, gone, on_disk])
        self._cleanup()
        maps = json.loads(self.index.read_text())["maps"]
        self.assertEqual([m["filename"] for m in maps], [on_disk["filename"]])
        self.assertEqual(self._files(), sorted([on_disk["filename"], young_orphan]))
        self.assertTrue((self.dir / "notes.txt").exists())
        self.assertFalse((self.dir / "index.json.tmp").exists())

    def test_cleanup_follows_the_configured_retention(self):
        entries = [_aged("kitchen", 10), _aged("kitchen", 40)]
        self._maps(*entries)
        self._write_index(entries)
        self.app.map_retention_days = 30
        self._cleanup()
        self.assertEqual(self._files(), [entries[0]["filename"]])

    def test_an_undatable_entry_is_kept(self):
        entry = {"filename": "rober2_map_kitchen.png", "datetime": "soon", "room": "kitchen",
                 "url": f"{control.MAP_URL_PREFIX}/rober2_map_kitchen.png"}
        (self.dir / entry["filename"]).write_bytes(PNG)
        self._write_index([entry])
        self._cleanup()
        self.assertEqual(self._files(), [entry["filename"]])
        self.assertEqual(json.loads(self.index.read_text())["maps"], [entry])

    def test_a_year_of_maps_fits_the_index(self):
        entries = [_aged("kitchen", d / 5) for d in range(1, 1825)]  # five rooms a day
        self._maps(*entries)
        self._write_index(entries)
        self._update("rober2_map_29991231_000000_hallway.png", "hallway")
        self.assertEqual(len(json.loads(self.index.read_text())["maps"]), 1825)
        self._cleanup()
        self.assertEqual(len(json.loads(self.index.read_text())["maps"]), 1825)
        self.assertEqual(len(self._files()), 1825)

    def test_retention_lives_in_the_yaml(self):
        text = (Path(__file__).resolve().parent.parent / "rober2.yaml").read_text()
        self.assertRegex(text, re.compile(r"^  map_retention_days: 365$", re.M))

    def test_cleanup_deletes_nothing_without_a_usable_index(self):
        entries = self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"),
                             _entry("hallway", "20261012_080000", "2026-10-12T08:00:00"))
        names = sorted(e["filename"] for e in entries)
        for broken in ('{"maps": [', '{"maps": {}}', "null", ""):
            with self.subTest(index=broken):
                self.index.write_text(broken)
                self._cleanup()
                self.assertEqual(self._files(), names)
                self.assertEqual(self.index.read_text(), broken)
                self.assertTrue(self._warned("Map cleanup skipped, nothing deleted"))

    def test_cleanup_deletes_nothing_when_the_index_is_missing(self):
        entries = self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        self._cleanup()
        self.assertEqual(self._files(), [entries[0]["filename"]])
        self.assertFalse(self.index.exists())
        self.assertTrue(self._warned("index.json is missing beside saved maps"))

    def test_cleanup_deletes_nothing_when_the_index_cannot_be_read(self):
        entries = self._maps(_entry("hallway", "20261013_080000", "2026-10-13T08:00:00"))
        self.index.mkdir()  # reading it raises IsADirectoryError
        self._cleanup()
        self.assertEqual(self._files(), [entries[0]["filename"]])
        self.assertTrue(self._warned("Map cleanup skipped, nothing deleted"))

    def test_an_empty_directory_without_an_index_is_quiet(self):
        self._cleanup()
        self.assertEqual(self.logs, [])

    def test_read_map_index_separates_absent_from_broken(self):
        self.assertIsNone(control.read_map_index(str(self.index)))
        self.index.write_text('{"maps": []}')
        self.assertEqual(control.read_map_index(str(self.index)), {"maps": []})
        self.index.write_text("{")
        with self.assertRaises(control.MapIndexError):
            control.read_map_index(str(self.index))


if __name__ == "__main__":
    unittest.main()
