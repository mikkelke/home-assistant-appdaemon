# tests/test_run_profiles.py - run_profile_model.py rules and the RunProfiles app around them.
# Run from repo root: python3 -m unittest discover -s apps/appliances/tests -q

from __future__ import annotations

import json
import math
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone
from pathlib import Path

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

import run_profile_model as m  # noqa: E402
import run_profiles as rp  # noqa: E402

FIXTURES = Path(__file__).resolve().parent / "fixtures"
T0 = datetime(2026, 10, 1, 18, 0, tzinfo=timezone.utc).timestamp()
SOURCES = {
    "washer": {"state_entity": "sensor.washer_state", "power_entity": "sensor.washer_plug_power", "onset_w": 18.0},
    "dishwasher": {"state_entity": "sensor.dishwasher_state", "power_entity": "sensor.dw_power", "onset_w": 8.0},
    "dryer": {"state_entity": "sensor.dryer_state", "power_entity": "sensor.dryer_plug_power", "onset_w": 8.0},
}


def iso(epoch):
    return datetime.fromtimestamp(epoch, timezone.utc).isoformat()


def remove_literals(val):
    """AppDaemon 4.5.13 utils.clean_http_kwargs, reproduced for the attribute tree."""
    if val is True:
        return "true"
    if isinstance(val, str):
        return val
    if isinstance(val, dict):
        return {k: remove_literals(v) for k, v in val.items() if v not in (None, False)}
    if isinstance(val, (list, tuple)):
        return [remove_literals(v) for v in val if v not in (None, False)]
    return val


class FakeCycle:
    """One synthetic cycle: a feedback record plus the state and power history behind it."""

    def __init__(self, appliance, start, watts, programme="eco", temperature=None, short=None, human=False,
                 lead_rows=(), energy=None, ts_extra_s=0, **record):
        self.appliance = appliance
        self.start = start
        self.watts = list(watts)
        duration = len(self.watts)
        self.power_rows = [{"state": str(w), "last_changed": iso(t)} for t, w in lead_rows]
        self.power_rows += [{"state": str(w), "last_changed": iso(start + 60 * i)} for i, w in enumerate(self.watts)]
        self.power_rows.append({"state": "0.0", "last_changed": iso(start + 60 * duration)})
        ts = start + 60 * duration + ts_extra_s
        kwh = sum(self.watts) / 60.0 / 1000.0
        self.record = {
            "ts": iso(ts),
            "duration_min": duration,
            "energy_kwh": round(kwh if energy is None else energy, 4),
            "confirmed": programme,
            "programme_confirmed_by_human": human,
        }
        if temperature is not None:
            self.record["confirmed_temperature"] = temperature
        if short is not None:
            self.record["short"] = short
        if appliance == "dishwasher":
            self.record["end_reason"] = "low_power_detected"
        self.record.update(record)
        self.state_rows = [
            {"state": "Off", "last_changed": iso(start - 3600), "last_updated": iso(start - 3600), "attributes": {}},
            {"state": "Running", "last_changed": iso(start), "last_updated": iso(start + 1),
             "attributes": {"cycle_start_time": iso(start)}},
        ]


def heater_watts(minutes=60, heater_at=(10, 40), heater_len=8, base=30.0, heater=2000.0):
    w = [base] * minutes
    for h in heater_at:
        for i in range(h, min(minutes, h + heater_len)):
            w[i] = heater
    return w


def extract(cycle):
    src = SOURCES[cycle.appliance]
    ts = m.record_ts(cycle.record)
    cst = m.find_cycle_start(cycle.state_rows, ts, m.expected_start(cycle.appliance, cycle.record))
    entry = m.extract_power(cycle.power_rows, cst, m.cycle_end(cycle.appliance, cycle.record, cst), src["onset_w"])
    entry["fp"] = m.extraction_fingerprint(cycle.record, src)
    return entry


def entries_for(cycles):
    return {m.cache_key(c.appliance, c.record): extract(c) for c in cycles}


# ---------------------------------------------------------------------------------------
# Model
# ---------------------------------------------------------------------------------------

class IntegrationTest(unittest.TestCase):
    def test_hold_last_value_and_unavailable_is_zero(self):
        samples = [(0.0, 600.0), (30.0, None), (90.0, 1200.0), (150.0, 0.0)]
        dwh, unavailable = m.integrate_minutes(samples, 0.0, 180.0)
        # minute 0: 30 s @600 W = 5 Wh; minutes 1 and 2: 30 s @1200 W = 10 Wh each
        self.assertEqual(dwh, [50, 100, 100])
        self.assertEqual(unavailable, 60.0)

    def test_reading_held_across_minutes_without_time_limit(self):
        dwh, _ = m.integrate_minutes([(0.0, 60.0)], 0.0, 600.0)
        self.assertEqual(dwh, [10] * 10)

    def test_appdaemon_datetime_rows_parse_like_rest_strings(self):
        dt = datetime(2026, 10, 1, 20, 0, tzinfo=timezone.utc)
        rows = [{"state": "12.5", "last_changed": dt}, {"state": "unavailable", "last_changed": iso(dt.timestamp() + 60)}]
        self.assertEqual(m.power_samples(rows), [(dt.timestamp(), 12.5), (dt.timestamp() + 60, None)])

    def test_window_starts_mid_reading(self):
        dwh, _ = m.integrate_minutes([(-300.0, 120.0), (60.0, 0.0)], 0.0, 120.0)
        self.assertEqual(dwh, [20, 0])


class OnsetTest(unittest.TestCase):
    def test_previous_cycle_anti_crease_blip_ignored(self):
        b = 10_000.0
        samples = [(b - 400, 0.0), (b - 170, 60.0), (b - 160, 0.0), (b - 20, 50.0), (b + 600, 40.0)]
        self.assertEqual(m.detect_onset(samples, b, 18.0), b - 20)

    def test_pre_start_blip_inside_window_ignored(self):
        b = 10_000.0
        samples = [(b - 900, 0.0), (b - 150, 300.0), (b - 145, 0.0), (b - 10, 500.0)]
        self.assertEqual(m.detect_onset(samples, b, 8.0), b - 10)

    def test_dryer_delay_start_pulses_ignored(self):
        b = 10_000.0
        samples = [(b - 3600, 150.0), (b - 3590, 0.0), (b - 500, 150.0), (b - 490, 0.0), (b + 30, 600.0)]
        self.assertEqual(m.detect_onset(samples, b, 8.0), b + 30)

    def test_short_dips_stay_one_burst(self):
        b = 10_000.0
        samples = [(b - 900, 0.0), (b - 60, 40.0), (b - 40, 5.0), (b - 10, 40.0)]
        self.assertEqual(m.detect_onset(samples, b, 18.0), b - 60)

    def test_running_before_window_has_no_onset(self):
        b = 10_000.0
        samples = [(b - 3600, 600.0), (b + 300, 650.0)]
        self.assertIsNone(m.detect_onset(samples, b, 8.0))

    def test_no_activity_has_no_onset(self):
        self.assertIsNone(m.detect_onset([(0.0, 2.0)], 1000.0, 8.0))


class CycleStartTest(unittest.TestCase):
    def test_rewritten_start_not_trusted_over_record_timing(self):
        start = T0
        rows = [
            {"state": "Running", "last_updated": iso(start), "attributes": {"cycle_start_time": iso(start)}},
            {"state": "Running", "last_updated": iso(start + 30), "attributes": {"cycle_start_time": iso(start - 9 * 3600)}},
        ]
        rec = {"ts": iso(start + 170 * 60), "duration_min": 166.0, "effective_end_at": iso(start + 166 * 60)}
        self.assertEqual(m.find_cycle_start(rows, m.record_ts(rec), m.expected_start("washer", rec)), start)
        self.assertEqual(m.find_cycle_start(rows, m.record_ts(rec)), start - 9 * 3600)

    def test_start_far_from_record_timing_rejected(self):
        rows = [{"state": "Running", "last_updated": iso(T0), "attributes": {"cycle_start_time": iso(T0)}}]
        rec = {"ts": iso(T0 + 7200), "duration_min": 60.0}
        self.assertIsNone(m.find_cycle_start(rows, m.record_ts(rec), m.expected_start("dryer", rec)))

    def test_dishwasher_has_no_expected_start(self):
        self.assertIsNone(m.expected_start("dishwasher", {"ts": iso(T0), "duration_min": 230}))


class EnergyCheckTest(unittest.TestCase):
    def test_matching_cycle_accepted(self):
        c = FakeCycle("washer", T0, heater_watts(), programme="bomuld", temperature="60")
        self.assertIsNone(m.entry_rejection(c.record, extract(c)))

    def test_energy_mismatch_rejected(self):
        c = FakeCycle("washer", T0, heater_watts(), programme="bomuld", temperature="60", energy=0.2)
        self.assertEqual(m.entry_rejection(c.record, extract(c)), "energy_mismatch")

    def test_unavailable_over_five_percent_rejected(self):
        c = FakeCycle("dryer", T0, [600.0] * 60, programme="ekspres__skabstoert")
        for row in c.power_rows[20:24]:
            row["state"] = "unavailable"
        entry = extract(c)
        self.assertEqual(m.entry_rejection(c.record, entry), "unavailable")

    def test_record_eligibility(self):
        base = {"ts": iso(T0), "duration_min": 60, "energy_kwh": 0.5}
        self.assertIsNone(m.record_eligibility(base))
        self.assertEqual(m.record_eligibility({**base, "energy_kwh": None}), "no_energy")
        self.assertEqual(m.record_eligibility({**base, "energy_kwh": 0.05}), "low_energy")
        self.assertEqual(m.record_eligibility({**base, "completion_class": "interrupted"}), "interrupted")
        self.assertEqual(m.record_eligibility({**base, "selected_options": {"soak": "on"}}), "soak")
        self.assertIsNone(m.record_eligibility({**base, "selected_options": {"soak": "off"}}))


class IdentityTest(unittest.TestCase):
    def test_washer_temperature_key(self):
        self.assertEqual(m.programme_identity("washer", {"confirmed": "bomuld", "confirmed_temperature": "60"}), "bomuld|60")
        self.assertEqual(m.programme_identity("washer", {"confirmed": "bomuld", "confirmed_temperature": "60°C"}), "bomuld|60")
        self.assertEqual(m.programme_identity("washer", {"confirmed": "strygelet", "confirmed_temperature": None}), "strygelet")
        self.assertIsNone(m.programme_identity("washer", {"confirmed": "unknown"}))

    def test_dishwasher_short_option(self):
        self.assertEqual(m.programme_identity("dishwasher", {"confirmed": "eco", "short": True}), "eco_short")
        self.assertEqual(m.programme_identity("dishwasher", {"confirmed": "quick", "short": True}), "quick_short")
        self.assertEqual(m.programme_identity("dishwasher", {"confirmed": "eco", "short": False}), "eco")
        self.assertEqual(m.programme_identity("dishwasher", {"confirmed": "eco"}), "eco")
        self.assertEqual(m.programme_identity("dishwasher", {"confirmed": "gentle", "short": True}), "gentle")

    def test_public_key_and_labels(self):
        self.assertEqual(m.public_key("bomuld|60"), "bomuld_60")
        self.assertEqual(m.programme_label("washer", "bomuld|60", {"bomuld": "Bomuld"}), "Bomuld 60°C")
        self.assertEqual(m.programme_label("dishwasher", "eco_short", {"eco": "ECO"}), "ECO short")
        self.assertEqual(m.programme_label("dryer", "ekspres__skabstoert", {}), "Ekspres - skabstoert")

    def test_human_confirmed(self):
        self.assertTrue(m.human_confirmed({"programme_user_confirmed": True}))
        self.assertTrue(m.human_confirmed({"programme_confirmed_by_human": True}))
        self.assertFalse(m.human_confirmed({"confirmed": "eco"}))


def build(appliance, cycles, now=None, **kw):
    records = [c.record for c in cycles]
    return m.build_appliance(appliance, records, entries_for(cycles), SOURCES[appliance],
                             now if now is not None else T0 + 400 * 86400, **kw)


def programme(block, key):
    return next((p for p in block["programmes"] if p["key"] == key), None)


class BuildTest(unittest.TestCase):
    def washes(self, n, start_day=0, **kw):
        return [FakeCycle("washer", T0 + (start_day + i) * 86400, heater_watts(base=30.0 + i), programme="bomuld",
                          temperature="60", **kw) for i in range(n)]

    def test_outlier_three_times_energy_dropped(self):
        cycles = self.washes(8)
        cycles.append(FakeCycle("washer", T0 + 20 * 86400, [w * 3 for w in heater_watts()], programme="bomuld",
                                temperature="60"))
        prog = programme(build("washer", cycles), "bomuld_60")
        self.assertEqual(prog["n"], 8)

    def test_mean_keeps_energy(self):
        cycles = self.washes(6)
        pool_kwh = sum(sum(m.integrate_minutes(m.power_samples(c.power_rows), c.start, c.start + 3600)[0])
                       for c in cycles) / 10000 / len(cycles)
        prog = programme(build("washer", cycles), "bomuld_60")
        self.assertLess(abs(prog["kwh"] - pool_kwh) / pool_kwh, 0.01)
        self.assertEqual(prog["kwh"], round(sum(m.decode_profile(prog["p"])) / 10000, 3))

    def test_latest_thirty_per_key(self):
        cycles = self.washes(35)
        prog = programme(build("washer", cycles), "bomuld_60")
        self.assertEqual(prog["n"], 30)

    def test_below_five_cycles_not_published(self):
        block = build("washer", self.washes(4))
        self.assertIsNone(programme(block, "bomuld_60"))
        self.assertEqual(block["programmes"], [])

    def test_minority_programme_survives_majority(self):
        eco = [FakeCycle("dishwasher", T0 + i * 86400, heater_watts(230, (26, 80), 8, base=12.0), programme="eco",
                         short=False) for i in range(20)]
        express = [FakeCycle("dishwasher", T0 + (30 + i) * 86400, heater_watts(74, (10,), 20, base=12.0, heater=2100.0),
                             programme="eco", short=True) for i in range(5)]
        block = build("dishwasher", eco + express)
        self.assertEqual(programme(block, "eco")["n"], 20)
        self.assertEqual(programme(block, "eco_short")["n"], 5)
        self.assertEqual(programme(block, "typical")["n"], 25)
        self.assertEqual(block["programmes"][0]["key"], "typical")

    def test_label_source(self):
        mixed = self.washes(3, human=True) + self.washes(3, start_day=10)
        self.assertEqual(programme(build("washer", mixed), "bomuld_60")["label_source"], "mixed")
        self.assertEqual(programme(build("washer", self.washes(5, human=True)), "bomuld_60")["label_source"], "confirmed")
        self.assertEqual(programme(build("washer", self.washes(5)), "bomuld_60")["label_source"], "inferred")

    def test_unknown_programme_feeds_typical_only(self):
        cycles = self.washes(3) + [FakeCycle("washer", T0 + (10 + i) * 86400, heater_watts(), programme="unknown")
                                   for i in range(3)]
        block = build("washer", cycles)
        self.assertEqual([p["key"] for p in block["programmes"]], ["typical"])
        self.assertEqual(block["profiled"], 6)

    def test_ordering_by_recent_use(self):
        cycles = self.washes(5) + [FakeCycle("washer", T0 + (10 + i) * 86400, heater_watts(), programme="strygelet")
                                   for i in range(7)]
        block = build("washer", cycles, now=T0 + 30 * 86400)
        self.assertEqual([p["key"] for p in block["programmes"]], ["typical", "strygelet", "bomuld_60"])


class DurationTest(unittest.TestCase):
    def test_run_min_is_profile_length_and_done_min_p90(self):
        cycles = []
        for i, extra in enumerate([0, 0, 0, 0, 2, 2, 4, 4, 6, 10]):
            watts = heater_watts(60 + extra)
            cycles.append(FakeCycle("dryer", T0 + i * 86400, watts, programme="ekspres__skabstoert"))
        prog = programme(build("dryer", cycles), "ekspres__skabstoert")
        self.assertEqual(prog["run_min"], len(m.decode_profile(prog["p"])))
        completions = sorted(60 + e for e in [0, 0, 0, 0, 2, 2, 4, 4, 6, 10])
        self.assertEqual(prog["done_min"], math.ceil(m.percentile(completions, 0.9)))

    def test_dishwasher_done_includes_drying_tail(self):
        cycles = []
        for i in range(6):
            watts = heater_watts(230, (26, 80), 8, base=12.0) + [0.0] * 20
            c = FakeCycle("dishwasher", T0 + i * 86400, watts, programme="eco", short=False)
            c.record["duration_min"] = 230
            cycles.append(c)
        prog = programme(build("dishwasher", cycles), "eco")
        self.assertEqual(prog["done_min"], 250)
        self.assertLessEqual(prog["run_min"], 230)

    def test_dishwasher_door_opened_finish_untrusted(self):
        cycles = [FakeCycle("dishwasher", T0 + i * 86400, heater_watts(230, (26, 80), 8, base=12.0), programme="eco",
                            end_reason="door_opened_first") for i in range(6)]
        prog = programme(build("dishwasher", cycles), "eco")
        self.assertNotIn("done_min", prog)

    def test_finish_before_energy_untrusted(self):
        c = FakeCycle("washer", T0, heater_watts(120, (10, 100)), programme="bomuld", temperature="40")
        entry = extract(c)
        self.assertIsNotNone(m.completion_minutes("washer", c.record, entry))
        early = dict(entry, dwh=entry["dwh"] + [500] * 60)
        self.assertIsNone(m.completion_minutes("washer", c.record, early))


class TransportTest(unittest.TestCase):
    def test_zeros_survive_string_encoding(self):
        p = [0, 0, 120, 0, 0, 340, 0, 5, 0]
        self.assertEqual(m.decode_profile(m.encode_profile(p)), p)
        attrs = {"schema": 1, "appliances": {"x": {"programmes": [{"key": "eco", "p": m.encode_profile(p)}]}}}
        self.assertEqual(remove_literals(attrs), attrs)

    def test_numeric_zero_would_be_stripped(self):
        attrs = {"p": [0, 0, 100, 0, 200], "n": 0}
        self.assertNotEqual(remove_literals(attrs), attrs)
        self.assertFalse(m.transport_safe(attrs))

    def test_built_attributes_are_transport_safe(self):
        cycles = [FakeCycle("dishwasher", T0 + i * 86400, [0.0] * 3 + heater_watts(100, (26, 80), 8, base=12.0),
                            programme="eco") for i in range(6)]
        block = build("dishwasher", cycles)
        attrs = m.build_attributes({"dishwasher": block, "dryer": build("dryer", [])})
        self.assertTrue(m.transport_safe(attrs))
        self.assertEqual(remove_literals(attrs), attrs)
        self.assertNotIn("records", attrs["appliances"]["dryer"])

    def test_payload_trim_drops_least_used_first(self):
        def prog(key, use, size):
            return {"key": key, "label": key, "n": 5, "kwh": 1.0, "run_min": size, "label_source": "inferred",
                    "p": ",".join(["123"] * size), "_use": use}
        blocks = {
            "washer": {"programmes": [prog("typical", 50, 400), prog("bomuld_60", 30, 400), prog("uld", 1, 400)]},
            "dryer": {"programmes": [prog("typical", 20, 400), prog("ekspres", 5, 400)]},
        }
        full = m.payload_bytes(m.build_attributes(blocks, max_bytes=10**6))
        one_programme = full // 5
        attrs = m.build_attributes(blocks, max_bytes=full - 2 * one_programme)
        self.assertLessEqual(m.payload_bytes(attrs), full - 2 * one_programme)
        keys = {a: [p["key"] for p in b["programmes"]] for a, b in attrs["appliances"].items()}
        self.assertEqual(keys, {"washer": ["typical"], "dryer": ["typical"]})
        attrs = m.build_attributes(blocks, max_bytes=full - one_programme)
        keys = {a: [p["key"] for p in b["programmes"]] for a, b in attrs["appliances"].items()}
        self.assertEqual(keys, {"washer": ["typical", "bomuld_60"], "dryer": ["typical"]})
        self.assertNotIn("_use", attrs["appliances"]["washer"]["programmes"][0])


class FingerprintTest(unittest.TestCase):
    def test_fingerprint_covers_extraction_inputs_only(self):
        rec = {"ts": iso(T0), "duration_min": 60, "energy_kwh": 0.5, "confirmed": "eco"}
        src = SOURCES["washer"]
        fp = m.extraction_fingerprint(rec, src)
        self.assertEqual(fp, m.extraction_fingerprint({**rec, "confirmed": "bomuld"}, src))
        self.assertNotEqual(fp, m.extraction_fingerprint({**rec, "duration_min": 61}, src))
        self.assertNotEqual(fp, m.extraction_fingerprint({**rec, "effective_end_at": iso(T0)}, src))
        self.assertNotEqual(fp, m.extraction_fingerprint(rec, {**src, "onset_w": 10.0}))
        old = m.EXTRACTION_VERSION
        try:
            m.EXTRACTION_VERSION = old + 1
            self.assertNotEqual(fp, m.extraction_fingerprint(rec, src))
        finally:
            m.EXTRACTION_VERSION = old

    def test_stale_entry_ignored(self):
        c = FakeCycle("washer", T0, heater_watts(), programme="bomuld", temperature="60")
        entries = entries_for([c])
        self.assertIsNotNone(m.current_entry("washer", c.record, entries, SOURCES["washer"]))
        changed = {**c.record, "duration_min": 61}
        self.assertIsNone(m.current_entry("washer", changed, entries, SOURCES["washer"]))

    def test_corrections_re_evaluated_both_ways(self):
        cycles = [FakeCycle("washer", T0 + i * 86400, heater_watts(), programme="bomuld", temperature="60")
                  for i in range(5)]
        entries = entries_for(cycles)
        records = [dict(c.record) for c in cycles]
        now = T0 + 30 * 86400
        self.assertEqual(programme(m.build_appliance("washer", records, entries, SOURCES["washer"], now), "bomuld_60")["n"], 5)
        records[0]["completion_class"] = "interrupted"
        self.assertIsNone(programme(m.build_appliance("washer", records, entries, SOURCES["washer"], now), "bomuld_60"))
        records[0]["completion_class"] = "completed"
        self.assertEqual(programme(m.build_appliance("washer", records, entries, SOURCES["washer"], now), "bomuld_60")["n"], 5)
        records[1]["confirmed"] = "eco"
        block = m.build_appliance("washer", records, entries, SOURCES["washer"], now)
        self.assertIsNone(programme(block, "bomuld_60"))
        self.assertEqual(programme(block, "typical")["n"], 5)


class RealDishwasherEcoTest(unittest.TestCase):
    def test_real_eco_recording(self):
        fx = json.loads((FIXTURES / "dishwasher_eco_2026_10_11.json").read_text())
        rec = fx["record"]
        base = m.parse_epoch(fx["power_base"])
        rows = [{"state": "unavailable" if w is None else str(w), "last_changed": iso(base + t)} for t, w in fx["power"]]
        ts = m.record_ts(rec)
        cst = m.find_cycle_start(fx["state_rows"], ts, m.expected_start("dishwasher", rec))
        self.assertIsNotNone(cst)
        entry = m.extract_power(rows, cst, m.cycle_end("dishwasher", rec, cst), 8.0)
        entry["fp"] = m.extraction_fingerprint(rec, SOURCES["dishwasher"])
        self.assertEqual(entry["status"], "ok")
        self.assertIsNone(m.entry_rejection(rec, entry))
        dwh = entry["dwh"]
        self.assertLess(abs(sum(dwh) / 10000 - rec["energy_kwh"]) / rec["energy_kwh"], 0.01)
        first = max(range(0, 60), key=lambda i: dwh[i])
        second = max(range(60, len(dwh)), key=lambda i: dwh[i])
        self.assertTrue(25 <= first <= 35, first)
        self.assertTrue(78 <= second <= 95, second)
        self.assertGreater(dwh[26], 100)
        self.assertGreater(dwh[80], 100)
        cycle = m.make_cycle("dishwasher", rec, entry)
        self.assertTrue(248 <= cycle["completion"] <= 250, cycle["completion"])
        p = m.profile_from_cycles([cycle])
        self.assertEqual(m.decode_profile(m.encode_profile(p)), p)
        self.assertIn(0, p)


# ---------------------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------------------

class FakeHA:
    def __init__(self):
        self.rows = {}
        self.fail = set()
        self.calls = []

    def add(self, cycle, power_entity, state_entity):
        self.rows.setdefault(state_entity, []).extend(cycle.state_rows)
        self.rows.setdefault(power_entity, []).extend(cycle.power_rows)

    def get_history(self, entity_id, start_time, end_time, **kw):
        self.calls.append((entity_id, kw))
        if entity_id in self.fail:
            return None
        lo, hi = start_time.timestamp(), end_time.timestamp()
        rows = sorted(self.rows.get(entity_id, []), key=lambda r: m.parse_epoch(r.get("last_updated") or r["last_changed"]))
        before = [r for r in rows if m.parse_epoch(r.get("last_updated") or r["last_changed"]) <= lo]
        inside = [r for r in rows if lo < m.parse_epoch(r.get("last_updated") or r["last_changed"]) <= hi]
        head = [dict(before[-1], last_changed=iso(lo), last_updated=iso(lo))] if before else []
        return [head + inside]


def make_app(tmp, ha, appliances=("washer",), **args):
    app = rp.RunProfiles.__new__(rp.RunProfiles)
    cfg = {}
    for name in appliances:
        cfg[name] = {**SOURCES[name], "feedback_file": os.path.join(tmp, f"{name}_feedback.json"),
                     "programmes_file": os.path.join(tmp, f"{name}_programmes.yaml")}
    app.args = {"cache_file": os.path.join(tmp, "run_profiles_cache_state.json"), "appliances": cfg, **args}
    app.published = []
    app.scheduled = []
    app.logs = []
    app.entity_state = {}
    app.get_history = ha.get_history
    app.log = lambda msg, level="INFO": app.logs.append((level, msg))
    app.run_in = lambda cb, delay, **kw: app.scheduled.append(("in", delay))
    app.run_every = lambda cb, start, interval, **kw: app.scheduled.append(("every", interval))

    def set_state(entity, state=None, attributes=None, replace=False, check_existence=True):
        app.published.append((state, attributes))
        app.entity_state[entity] = state

    app.set_state = set_state
    app.get_state = lambda entity, **kw: app.entity_state.get(entity)
    app.initialize()
    return app


def write_feedback(tmp, appliance, cycles):
    path = os.path.join(tmp, f"{appliance}_feedback.json")
    with open(path, "w") as f:
        json.dump({"version": 1, "cycles": [c.record for c in cycles]}, f)
    return path


class AppTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.ha = FakeHA()

    def washes(self, n, start_day=0):
        cycles = [FakeCycle("washer", T0 + (start_day + i) * 86400, heater_watts(), programme="bomuld", temperature="60")
                  for i in range(n)]
        for c in cycles:
            self.ha.add(c, SOURCES["washer"]["power_entity"], SOURCES["washer"]["state_entity"])
        return cycles

    def test_backfill_newest_first_with_budget_and_backlog(self):
        cycles = self.washes(12)
        write_feedback(self.tmp, "washer", cycles)
        app = make_app(self.tmp, self.ha, records_per_run=5)
        app._scan()
        keys = sorted(app._cache["entries"])
        self.assertEqual(len(keys), 5)
        newest = sorted((c.record["ts"] for c in cycles), reverse=True)[:5]
        self.assertEqual(sorted(k.split("|", 1)[1] for k in keys), sorted(newest))
        self.assertIn(("in", rp.BACKLOG_DELAY_S), app.scheduled)
        app._scan()
        app._scan()
        self.assertEqual(len(app._cache["entries"]), 12)
        prog = programme(app.published[-1][1]["appliances"]["washer"], "bomuld_60")
        self.assertEqual(prog["n"], 12)

    def test_uses_full_state_history_and_minimal_power_history(self):
        write_feedback(self.tmp, "washer", self.washes(1))
        app = make_app(self.tmp, self.ha)
        app._scan()
        self.assertEqual(self.ha.calls[0], ("sensor.washer_state", {"significant_changes_only": "0"}))
        self.assertEqual(self.ha.calls[1], ("sensor.washer_plug_power", {"minimal_response": True, "no_attributes": True}))

    def test_history_failure_is_retried_later_without_blocking_others(self):
        cycles = self.washes(6)
        write_feedback(self.tmp, "washer", cycles)
        app = make_app(self.tmp, self.ha)
        self.ha.fail.add("sensor.washer_plug_power")
        app._scan()
        self.assertEqual(app._cache["entries"], {})
        self.assertEqual(len(app._retry_at), 6)
        self.ha.fail.clear()
        app._scan()
        self.assertEqual(app._cache["entries"], {})
        for key in app._retry_at:
            app._retry_at[key] = 0
        app._scan()
        self.assertEqual(len(app._cache["entries"]), 6)

    def test_unreadable_feedback_keeps_last_good_and_cache(self):
        cycles = self.washes(6)
        path = write_feedback(self.tmp, "washer", cycles)
        app = make_app(self.tmp, self.ha)
        app._scan()
        good = app.published[-1][1]
        with open(path, "w") as f:
            f.write('{"cycles": [')
        app._scan()
        self.assertEqual(len(app._cache["entries"]), 6)
        self.assertEqual(app.published[-1][1], good)
        write_feedback(self.tmp, "washer", cycles[:5])
        app._scan()
        self.assertEqual(len(app._cache["entries"]), 5)
        self.assertEqual(programme(app.published[-1][1]["appliances"]["washer"], "bomuld_60")["n"], 5)

    def test_publish_from_cache_on_start_only_on_change_and_when_missing(self):
        write_feedback(self.tmp, "washer", self.washes(6))
        app = make_app(self.tmp, self.ha)
        app._scan()
        n = len(app.published)
        app._scan()
        self.assertEqual(len(app.published), n)
        restarted = make_app(self.tmp, self.ha)
        self.assertEqual(restarted.published[0], (app.published[-1][0], app.published[-1][1]))
        restarted.entity_state.clear()
        restarted._ensure_published()
        self.assertEqual(len(restarted.published), 2)
        restarted._ensure_published()
        self.assertEqual(len(restarted.published), 2)

    def test_label_change_republishes_without_refetch(self):
        cycles = self.washes(10)
        write_feedback(self.tmp, "washer", cycles)
        app = make_app(self.tmp, self.ha)
        app._scan()
        calls = len(self.ha.calls)
        for c in cycles[:5]:
            c.record["confirmed"] = "eco"
            c.record["confirmed_temperature"] = None
        write_feedback(self.tmp, "washer", cycles)
        app._scan()
        self.assertEqual(len(self.ha.calls), calls)
        keys = [p["key"] for p in app.published[-1][1]["appliances"]["washer"]["programmes"]]
        self.assertEqual(sorted(keys), ["bomuld_60", "eco", "typical"])

    def test_scans_never_overlap(self):
        write_feedback(self.tmp, "washer", self.washes(3))
        app = make_app(self.tmp, self.ha)
        app._scanning = True
        app._scan()
        self.assertEqual(self.ha.calls, [])

    def test_never_writes_feedback_files(self):
        path = write_feedback(self.tmp, "washer", self.washes(6))
        before = (Path(path).read_bytes(), os.stat(path).st_mtime_ns)
        app = make_app(self.tmp, self.ha)
        app._scan()
        app._scan()
        self.assertEqual((Path(path).read_bytes(), os.stat(path).st_mtime_ns), before)
        self.assertEqual(sorted(os.listdir(self.tmp)), ["run_profiles_cache_state.json", "washer_feedback.json"])

    def test_published_attributes_survive_appdaemon_cleaning(self):
        write_feedback(self.tmp, "washer", self.washes(6))
        app = make_app(self.tmp, self.ha)
        app._scan()
        attrs = app.published[-1][1]
        self.assertEqual(remove_literals(attrs), attrs)
        self.assertEqual(attrs["schema"], 1)
        self.assertEqual(attrs["unit"], "dWh/min")


if __name__ == "__main__":
    unittest.main()
