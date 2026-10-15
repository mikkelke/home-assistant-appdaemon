"""PriceForecastTape - writes down what each DK2 price forecast said and which prices were then
published, so the forecasts can be scored against each other (scripts/score_price_forecasts.py).

Nothing else keeps these forecasts: the recorder does not store the forecast attributes, and the
web sources overwrite their files. Each run appends one JSON line per source to tape_file:

    {"v", "run", "trigger", "fetched_at", "source", "kind": "forecast" | "actual", "unit", "basis",
     "includes", "points": [[hour start UTC, value], ...], plus per-source fields}

basis "spot" is the spot price alone: ex VAT, no tariffs, no supplier addon. basis "all_in" is
(spot + supplier addon + addon_ex_vat) x (1 + vat) + tariffs_incl_vat[local hour]; such lines carry
the 24 tariffs (incl. VAT, by local hour) the source applied, and `vat` when the source states it.
Points are hourly means; an hour is kept only when its slots cover all of it.

Sources: Carnot (the price entity's forecast attribute, all-in), Stromligning, Smartere Elforbrug,
Energy Price Forecast EU (keyless, 48 h) and Open Spot Forecast (its forecast entity, skipped while
absent). Actuals: the price entity's raw_today/raw_tomorrow (all-in) and Energi Data Service
DayAheadPrices (spot). A run happens at each of run_times, once when tomorrow's prices reach the
price entity, and once after start. A source that fails is logged and left out of that run.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import statistics
import threading
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import appdaemon.plugins.hass.hassapi as hass  # type: ignore

TZ = ZoneInfo("Europe/Copenhagen")
HOUR_S = 3600
SCHEMA = 1
STARTUP_DELAY_S = 90
RUN_TIMES = ("06:45:00", "12:40:00", "15:30:00", "20:45:00")

STROMLIGNING_URL = "https://stromligning.dk/api/prices"
SMARTERE_URL = "https://raw.githubusercontent.com/solmoller/Spotprisprognose/main/prognose.json"
EPF_URL = "https://api.energypriceforecast.eu/api/v1/home-assistant/prices"
EDS_URL = "https://api.energidataservice.dk/dataset/DayAheadPrices"

SPOT = "spot price only: ex VAT, no tariffs, no supplier addon"
ALL_IN = "all-in: (spot + supplier addon + addon_ex_vat) x (1 + vat) + tariffs_incl_vat[local hour]"

RESOLUTION_MINUTES = {"15m": 15, "30m": 30, "60m": 60, "1h": 60}
PER_KWH = {"kWh": 1.0, "MWh": 0.001, "Wh": 1000.0}

_APPEND_LOCK = threading.Lock()


# -- parsing helpers ------------------------------------------------------------------------


def _dig(obj, *keys):
    for key in keys:
        obj = obj.get(key) if isinstance(obj, dict) else None
    return obj


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _utc(value, naive_tz=timezone.utc):
    """Aware UTC datetime from an ISO string, a datetime or epoch seconds, else None. A time
    without an offset is read in naive_tz."""
    if isinstance(value, datetime):
        moment = value
    elif _number(value) is not None:
        return datetime.fromtimestamp(float(value), timezone.utc)
    elif isinstance(value, str):
        try:
            moment = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=naive_tz)
    return moment.astimezone(timezone.utc)


def _iso(moment):
    return moment.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if moment else None


def _slot_minutes(starts, default=60):
    """A series' slot length: its smallest gap between distinct starts, at most an hour."""
    seconds = sorted({int(start.timestamp()) for start in starts})
    gaps = [b - a for a, b in zip(seconds, seconds[1:])]
    return min(60, min(gaps) // 60) if gaps else default


def hourly(slots):
    """{hour start epoch: mean} from (start, minutes, value) slots. An hour is kept only when its
    slots cover exactly its 60 minutes, so a partial or doubled hour is dropped, never guessed."""
    acc = {}
    for start, minutes, value in slots:
        if start is None or value is None or not isinstance(minutes, int) or minutes <= 0:
            continue
        epoch = int(start.timestamp())
        hour = epoch - epoch % HOUR_S
        covered, total = acc.get(hour, (0, 0.0))
        acc[hour] = (covered + minutes, total + value * minutes)
    return {hour: total / covered for hour, (covered, total) in acc.items() if covered == 60}


def _points(hours):
    return [[_iso(datetime.fromtimestamp(hour, timezone.utc)), round(value, 6)]
            for hour, value in sorted(hours.items())]


def make_line(source, kind, unit, basis, hours, **fields):
    includes = SPOT if basis == "spot" else ALL_IN
    line = {"source": source, "kind": kind, "unit": unit, "basis": basis, "includes": includes}
    line.update({key: value for key, value in fields.items() if value is not None})
    line["points"] = _points(hours)
    return line


def _hour_price_slots(entries):
    """Slots from the price entity's [{"hour", "price"}] lists."""
    found = []
    for entry in entries if isinstance(entries, list) else []:
        if isinstance(entry, dict):
            start, price = _utc(entry.get("hour")), _number(entry.get("price"))
            if start is not None and price is not None:
                found.append((start, price))
    minutes = _slot_minutes([start for start, _ in found])
    return [(start, minutes, price) for start, price in found]


# -- sources ----------------------------------------------------------------------------------


def tariff_table(tariffs):
    """The price entity's tariffs as 24 values incl. VAT by local hour (the hour's tariff plus every
    additional tariff), or None when incomplete."""
    if not isinstance(tariffs, dict):
        return None
    by_hour, extra = tariffs.get("tariffs"), tariffs.get("additional_tariffs") or {}
    if not isinstance(by_hour, dict) or not isinstance(extra, dict):
        return None
    added = [_number(value) for value in extra.values()]
    table = [_number(by_hour.get(str(hour), by_hour.get(hour))) for hour in range(24)]
    if None in added or None in table:
        return None
    return [round(value + sum(added), 6) for value in table]


def _price_unit(attrs):
    return "ore/kWh" if attrs.get("use_cent") is True else "DKK/kWh"


def parse_carnot(attrs):
    """Carnot's forecast as the price entity shows it: all-in, with the tariffs it was priced on."""
    if not isinstance(attrs, dict):
        return None
    hours = hourly(_hour_price_slots(attrs.get("forecast")))
    table = tariff_table(attrs.get("tariffs"))
    if not hours or table is None:
        return None
    return make_line("carnot", "forecast", _price_unit(attrs), "all_in", hours, tariffs_incl_vat=table)


def parse_price_actuals(attrs):
    """The price entity's published all-in prices: raw_today, plus raw_tomorrow once tomorrow_valid."""
    if not isinstance(attrs, dict):
        return None
    entries = list(attrs.get("raw_today") or [])
    if attrs.get("tomorrow_valid") is True:
        entries += list(attrs.get("raw_tomorrow") or [])
    hours = hourly(_hour_price_slots(entries))
    if not hours:
        return None
    return make_line("eds_sensor", "actual", _price_unit(attrs), "all_in", hours,
                     tariffs_incl_vat=tariff_table(attrs.get("tariffs")))


def parse_stromligning(resp):
    """The forecast slots of /api/prices?forecast=true; details.electricity.value is the spot price
    ex VAT in kr/kWh. Published slots are left to the actual sources."""
    prices = resp.get("prices") if isinstance(resp, dict) else None
    slots = []
    for item in prices if isinstance(prices, list) else []:
        if not isinstance(item, dict) or item.get("forecast") is not True:
            continue
        if _dig(item, "details", "electricity", "unit") not in (None, "kr/kWh"):
            continue
        slots.append((_utc(item.get("date")), RESOLUTION_MINUTES.get(item.get("resolution")),
                      _number(_dig(item, "details", "electricity", "value"))))
    hours = hourly(slots)
    return make_line("stromligning", "forecast", "DKK/kWh", "spot", hours) if hours else None


def parse_smartere(resp, area="DK2"):
    """Smartere Elforbrug's prognose.json: EUR/MWh per UTC hour per area; "Forecast created" is
    Danish local time."""
    series = resp.get(area) if isinstance(resp, dict) else None
    found = []
    for item in series if isinstance(series, list) else []:
        if isinstance(item, dict):
            start, price = _utc(item.get("Time")), _number(item.get("Price"))
            if start is not None and price is not None:
                found.append((start, price))
    minutes = _slot_minutes([start for start, _ in found])
    hours = hourly([(start, minutes, price) for start, price in found])
    if not hours:
        return None
    created = _utc(resp.get("Forecast created"), naive_tz=TZ)
    return make_line("smartere", "forecast", "EUR/MWh", "spot", hours, created=_iso(created))


def parse_epf(resp):
    """Energy Price Forecast EU's price series: the model's forecast entries (base price, ex VAT)."""
    unit = _dig(resp, "unit")
    entries = _dig(resp, "entries")
    if unit not in ("DKK/kWh", "EUR/kWh") or not isinstance(entries, list):
        return None
    slots = []
    for item in entries:
        if not isinstance(item, dict) or item.get("source") != "forecast" or item.get("unit", unit) != unit:
            continue
        start, end = _utc(item.get("start")), _utc(item.get("end"))
        minutes = int((end - start).total_seconds() // 60) if start and end else item.get("slot_minutes")
        slots.append((start, minutes, _number(item.get("value"))))
    hours = hourly(slots)
    if not hours:
        return None
    created = _utc(_dig(resp, "source", "firestore", "updated_at"))
    return make_line("epf", "forecast", unit, "spot", hours, created=_iso(created))


def parse_osf(attrs, table=None):
    """Open Spot Forecast's ML forecast entity. Its prices are (spot + tariff + surcharge) x (1 + vat)
    in its unit: without tariffs that inverts exactly to spot; with tariffs (Stromligning's, which
    match the price entity's) the line is all-in on `table`, the price entity's tariffs of the same
    run. Confirmed prices at the head of the series are skipped."""
    if not isinstance(attrs, dict):
        return None
    predictions = attrs.get("predictions")
    unit = attrs.get("unit") or attrs.get("unit_of_measurement")
    slots = []
    if isinstance(predictions, dict):
        unit = predictions.get("unit") or unit
        minutes = predictions.get("interval_minutes")
        skip = predictions.get("known_count") or 0
        for index, (start, price) in enumerate(zip(predictions.get("s") or [], predictions.get("t") or [])):
            if index >= skip:
                slots.append((_utc(start), minutes, _number(price)))
    elif isinstance(predictions, list):
        for item in predictions:
            if not isinstance(item, dict) or item.get("source") == "actual":
                continue
            start, end = _utc(item.get("start")), _utc(item.get("end"))
            minutes = int((end - start).total_seconds() // 60) if start and end else None
            slots.append((start, minutes, _number(item.get("price"))))
            unit = item.get("unit") or unit
    currency, _, per = str(unit or "").partition("/")
    if currency != "DKK" or per not in PER_KWH:
        return None
    vat, surcharge = _number(attrs.get("vat")) or 0.0, _number(attrs.get("surcharge")) or 0.0
    hours = hourly(slots)
    if not hours:
        return None
    scale = PER_KWH[per]
    model = {"is_ml_model": attrs.get("is_ml_model")} if isinstance(attrs.get("is_ml_model"), bool) else {}
    if attrs.get("includes_tariffs") is True:
        if table is None:
            return None
        return make_line("osf", "forecast", "DKK/kWh", "all_in", {h: v * scale for h, v in hours.items()},
                         tariffs_incl_vat=table, vat=vat, addon_ex_vat=round(surcharge * scale, 6), **model)
    spot = {hour: (value / (1 + vat) - surcharge) * scale for hour, value in hours.items()}
    return make_line("osf", "forecast", "DKK/kWh", "spot", spot, **model)


def parse_eds_dayahead(resp):
    """Energi Data Service DayAheadPrices records: DKK/MWh per UTC slot, to hourly DKK/kWh, with the
    DKK/EUR rate the records imply."""
    records = resp.get("records") if isinstance(resp, dict) else None
    found, rates = [], []
    for record in records if isinstance(records, list) else []:
        if not isinstance(record, dict):
            continue
        start = _utc(record.get("TimeUTC"))
        dkk, eur = _number(record.get("DayAheadPriceDKK")), _number(record.get("DayAheadPriceEUR"))
        if start is None or dkk is None:
            continue
        found.append((start, dkk / 1000))
        if eur is not None and abs(eur) >= 1:
            rates.append(dkk / eur)
    minutes = _slot_minutes([start for start, _ in found])
    hours = hourly([(start, minutes, price) for start, price in found])
    if not hours:
        return None
    rate = round(statistics.median(rates), 5) if rates else None
    return make_line("eds_spot", "actual", "DKK/kWh", "spot", hours, dkk_per_eur=rate)


# -- the tape file ----------------------------------------------------------------------------


def append_jsonl(path, records):
    """Append records as one strict-JSON line each, in a single write; returns how many were
    written. A record that is not strict JSON (NaN, infinity, unserialisable) is skipped. When the
    file does not end in a newline - an earlier write cut short - the batch starts on a fresh line,
    so a record is never glued to the fragment."""
    lines = []
    for record in records:
        try:
            lines.append(json.dumps(record, separators=(",", ":"), ensure_ascii=True, allow_nan=False))
        except (TypeError, ValueError):
            continue
    if not lines:
        return 0
    data = ("\n".join(lines) + "\n").encode("ascii")
    folder = os.path.dirname(os.path.abspath(path))
    os.makedirs(folder, exist_ok=True)
    with _APPEND_LOCK:
        fd = os.open(path, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
        try:
            size = os.fstat(fd).st_size
            if size and os.pread(fd, 1, size - 1) != b"\n":
                data = b"\n" + data
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            os.fsync(fd)
        finally:
            os.close(fd)
    return len(lines)


def stamp(line, run, trigger):
    return {"v": SCHEMA, "run": run, "trigger": trigger, **line}


def _ascii(text):
    return str(text).encode("ascii", "replace").decode("ascii")


def _span(line):
    points = line.get("points") or []
    if not points:
        return "0 h"
    last = datetime.fromisoformat(points[-1][0].replace("Z", "+00:00")).astimezone(TZ)
    return f"{len(points)} h to {last:%d.%m %H}:00"


# -- the app ----------------------------------------------------------------------------------


class PriceForecastTape(hass.Hass):
    def initialize(self):
        a = self.args
        self.tape_file = str(a.get("tape_file") or Path(__file__).resolve().parent / "price_forecast_tape.jsonl")
        self.price_entity = a.get("price_entity", "sensor.energi_data_service")
        self.osf_entity = a.get("osf_entity", "sensor.open_spot_forecast_dk2_price_forecast_ml")
        self.area = str(a.get("price_area", "DK2"))
        self.grid = str(a.get("stromligning_grid", "radius_c"))
        self.product = str(a.get("stromligning_product", "vindstoed_vestroem"))
        self.timeout_s = float(a.get("timeout_seconds", 20))
        self._running = False
        self._publish_day = None

        times = [str(t) for t in (a.get("run_times") or RUN_TIMES)]
        for clock in times:
            self.run_daily(self._on_timer, time.fromisoformat(clock), trigger=clock[:5])
        self.listen_state(self._on_tomorrow_valid, self.price_entity, attribute="tomorrow_valid")
        self.run_in(self._on_timer, STARTUP_DELAY_S, trigger="startup")
        self.log(f"PriceForecastTape: runs at {', '.join(t[:5] for t in times)} and on publish -> {self.tape_file}")

    def _on_timer(self, kwargs):
        self.create_task(self._run(kwargs.get("trigger", "timer")))

    def _on_tomorrow_valid(self, entity, attribute, old, new, kwargs):
        today = datetime.now(TZ).date()
        if new is True and old is not True and self._publish_day != today:
            self._publish_day = today
            self.create_task(self._run("publish"))

    async def _run(self, trigger):
        if self._running:
            self.log(f"Tape run {trigger} skipped: the previous run is still going", level="WARNING")
            return
        self._running = True
        try:
            await self._record(trigger)
        except Exception as e:
            self.log(f"Tape run {trigger} failed: {_ascii(repr(e))}", level="WARNING")
        finally:
            self._running = False

    async def _record(self, trigger):
        run = _iso(datetime.now(timezone.utc))
        outcomes = []

        price_attrs = await self._attributes(self.price_entity)
        read_at = _iso(datetime.now(timezone.utc))
        table = tariff_table((price_attrs or {}).get("tariffs"))
        for name, parse in (("carnot", parse_carnot), ("eds_sensor", parse_price_actuals)):
            outcomes.append(self._parsed(name, parse, price_attrs, read_at))

        if self.osf_entity:
            osf_attrs = await self._attributes(self.osf_entity)
            if osf_attrs is None:
                outcomes.append(("osf", None, "absent"))
            else:
                outcomes.append(self._parsed("osf", lambda attrs: parse_osf(attrs, table), osf_attrs,
                                             _iso(datetime.now(timezone.utc))))

        outcomes += await self._fetch_web()

        lines, notes = [], []
        for name, line, problem in outcomes:
            if line is not None:
                lines.append(stamp(line, run, trigger))
                notes.append(f"{name} {_span(line)}")
            else:
                notes.append(f"{name} {problem}")
                if problem != "absent":
                    self.log(f"Tape {trigger}: {name} {problem}", level="WARNING")
        written = await self.run_in_executor(append_jsonl, self.tape_file, lines)
        self.log(f"Tape {trigger}: wrote {written} lines - {'; '.join(notes)}")

    def _parsed(self, name, parse, payload, fetched_at):
        """(name, line or None, problem): parse errors stay with their source."""
        try:
            line = parse(payload)
        except Exception as e:
            return name, None, f"failed: {_ascii(repr(e))}"
        if line is None:
            return name, None, "had no usable data"
        return name, {"fetched_at": fetched_at, **line}, None

    async def _attributes(self, entity_id):
        try:
            state = await asyncio.wait_for(self.get_state(entity_id, attribute="all"), timeout=self.timeout_s)
        except Exception as e:
            self.log(f"Tape: reading {entity_id} failed: {_ascii(repr(e))}", level="WARNING")
            return None
        attrs = state.get("attributes") if isinstance(state, dict) else None
        return attrs if isinstance(attrs, dict) else None

    async def _fetch_web(self):
        import aiohttp

        now = datetime.now(timezone.utc)
        today = now.astimezone(TZ).date()
        requests = (
            ("stromligning", STROMLIGNING_URL, {
                "supplierId": self.grid, "priceArea": self.area, "productId": self.product, "forecast": "true",
                "from": _iso(now.replace(minute=0, second=0, microsecond=0)), "to": _iso(now + timedelta(days=8)),
            }, parse_stromligning),
            ("smartere", SMARTERE_URL, None, lambda resp: parse_smartere(resp, self.area)),
            ("epf", EPF_URL, {
                "country": self.area.lower(), "hours": "48", "mode": "forecast_only", "resolution": "native",
                "price_mode": "base",
            }, parse_epf),
            ("eds_spot", EDS_URL, {
                "start": f"{today - timedelta(days=1)}T00:00", "end": f"{today + timedelta(days=2)}T00:00",
                "filter": json.dumps({"PriceArea": [self.area]}), "sort": "TimeUTC asc",
            }, parse_eds_dayahead),
        )
        timeout = aiohttp.ClientTimeout(total=self.timeout_s)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            return await asyncio.gather(*(self._fetch(session, *request) for request in requests))

    async def _fetch(self, session, name, url, params, parse):
        try:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    return name, None, f"HTTP {resp.status}"
                payload = await resp.json(content_type=None)
        except Exception as e:
            return name, None, f"failed: {_ascii(repr(e))}"
        return self._parsed(name, parse, payload, _iso(datetime.now(timezone.utc)))
