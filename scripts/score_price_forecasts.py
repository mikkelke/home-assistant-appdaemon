#!/usr/bin/env python3
"""Score the DK2 price forecasts on the tape that apps/energy/price_forecast_tape.py writes.

    python3 scripts/score_price_forecasts.py TAPE.jsonl
    ssh mke@10.21.0.5 'python3 - /data/appdaemon/apps/energy/price_forecast_tape.jsonl' < scripts/score_price_forecasts.py

Every price is first put on one footing, the spot price ex VAT in DKK/kWh: EUR prices at the
DKK/EUR rate of the latest Energi Data Service line, all_in lines backed out with the tariffs they
carry, spot = (price - tariffs_incl_vat[local hour]) / (1 + vat) - (supplier addon + addon_ex_vat).

A forecast counts as it stood at an origin: the source's latest line fetched at most
--max-age-hours before the origin (or up to GRACE_S after it). D+1 is scored from the
pre-publication origin, D+2..D+5 from the post-publication origin, days counted from the origin's
date; a day already published at the origin is never scored. Persistence forecasts the latest day
published at the origin, hour by hour on the wall clock.

Per scored day: hourly MAE and bias of the spot price (ore/kWh), Spearman rank correlation over
the day's hours, and block regret: the --block-hours block the forecast calls cheapest, priced on
the real all-in prices, minus the real cheapest block, for a --kwh run, in kr. A forecast hour's
all-in price is the real one moved by the spot error times (1 + vat), so every forecast meets the
same tariffs. Cheapest day: how often the forecast's cheapest day by mean spot among D+2..D+5, from
the post-publication origin, is the real one (chance 25 %).

A source is adopted when its mean block regret is below persistence's on the same days at D+1, D+2
and D+3, each over at least --min-days days.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import defaultdict
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

TZ = ZoneInfo("Europe/Copenhagen")
HOUR_S = 3600
GRACE_S = 300
DKK_PER_KWH = {"DKK/kWh": 1.0, "DKK/MWh": 0.001, "ore/kWh": 0.01}
EUR_PER_KWH = {"EUR/MWh": 0.001, "EUR/kWh": 1.0}
DEFAULT_DKK_PER_EUR = 7.46
ALL_IN_RANGE = (-5.0, 15.0)
PERSISTENCE = "persistence"
PRE_LEADS, POST_LEADS = (1,), (2, 3, 4, 5)
LEADS = (("D+1", (1,)), ("D+2", (2,)), ("D+3-5", (3, 4, 5)))
ADOPT_LEADS = (1, 2, 3)


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    value = float(value)
    return value if math.isfinite(value) else None


def _epoch(text):
    """Epoch seconds of an ISO time with an offset, else None."""
    try:
        moment = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return int(moment.timestamp()) if moment.tzinfo else None


def _local(epoch):
    return datetime.fromtimestamp(epoch, TZ)


def at(day, clock):
    return int(datetime.combine(day, clock, TZ).timestamp())


def day_hours(day):
    """Epochs of a local day's hours: 23, 24 or 25 of them."""
    return list(range(at(day, time(0)), at(day + timedelta(days=1), time(0)), HOUR_S))


def _table(value):
    if isinstance(value, list) and len(value) == 24:
        table = [_number(item) for item in value]
        if None not in table:
            return table
    return None


def points_of(record):
    """{hour epoch: value} of a line's points; a malformed point is skipped."""
    found = {}
    for item in record.get("points") or []:
        if isinstance(item, list) and len(item) == 2:
            hour, value = _epoch(item[0]), _number(item[1])
            if hour is not None and value is not None:
                found[hour] = value
    return found


def read_tape(lines):
    """(records, malformed count): JSON-object lines with a fetched_at and points; blank lines skipped."""
    records, malformed = [], 0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
        except ValueError:
            malformed += 1
            continue
        if isinstance(record, dict) and _epoch(record.get("fetched_at")) is not None and isinstance(record.get("points"), list):
            records.append(record)
        else:
            malformed += 1
    return records, malformed


class Tape:
    """The tape's published prices, and every forecast line on the spot footing, oldest first."""

    def __init__(self, records, supplier_addon=0.012, vat=0.25):
        self.supplier_addon, self.vat = supplier_addon, vat
        self.spot, self.all_in, self.seen, self.tariffs = {}, {}, {}, {}
        self.rates = []
        self.forecasts = defaultdict(list)
        self.lines, self.unusable = defaultdict(int), defaultdict(int)
        ordered = sorted(records, key=lambda record: _epoch(record["fetched_at"]))
        for record in ordered:
            self.lines[str(record.get("source"))] += 1
            if record.get("kind") == "actual":
                self._actual(record)
        for record in ordered:
            if record.get("kind") == "forecast":
                spot = self.to_spot(record)
                if spot:
                    self.forecasts[str(record.get("source"))].append((_epoch(record["fetched_at"]), spot))
                else:
                    self.unusable[str(record.get("source"))] += 1

    def _actual(self, record):
        fetched, scale, basis = _epoch(record["fetched_at"]), DKK_PER_KWH.get(record.get("unit")), record.get("basis")
        if scale is None or basis not in ("spot", "all_in"):
            self.unusable[str(record.get("source"))] += 1
            return
        table = _table(record.get("tariffs_incl_vat"))
        for hour, value in points_of(record).items():
            value *= scale
            if basis == "spot":
                self.spot[hour] = value
            elif ALL_IN_RANGE[0] <= value <= ALL_IN_RANGE[1]:
                self.all_in[hour] = value
                if table:
                    self.tariffs[hour] = table
            else:
                continue
            self.seen[hour] = min(self.seen.get(hour, fetched), fetched)
        rate = _number(record.get("dkk_per_eur"))
        if rate:
            self.rates.append((fetched, rate))

    def rate_at(self, moment):
        """DKK per EUR of the latest actual line at or before moment, else of the first one."""
        before = [rate for fetched, rate in self.rates if fetched <= moment]
        if before:
            return before[-1]
        return self.rates[0][1] if self.rates else DEFAULT_DKK_PER_EUR

    def to_spot(self, record):
        """{hour: spot DKK/kWh} of a forecast line, or None when its unit or basis is unknown."""
        unit, basis = record.get("unit"), record.get("basis")
        if unit in DKK_PER_KWH:
            scale = DKK_PER_KWH[unit]
        elif unit in EUR_PER_KWH:
            scale = EUR_PER_KWH[unit] * self.rate_at(_epoch(record["fetched_at"]))
        else:
            return None
        points = {hour: value * scale for hour, value in points_of(record).items()}
        if basis == "spot":
            return points
        table = _table(record.get("tariffs_incl_vat"))
        if basis != "all_in" or table is None:
            return None
        vat = _number(record.get("vat"))
        vat = self.vat if vat is None else vat
        addon = self.supplier_addon + (_number(record.get("addon_ex_vat")) or 0.0)
        return {hour: (value - table[_local(hour).hour]) / (1 + vat) - addon for hour, value in points.items()}

    def backout_errors(self):
        """ore/kWh errors of backing the published all-in prices out to spot, against EDS spot."""
        return [((price - self.tariffs[hour][_local(hour).hour]) / (1 + self.vat) - self.supplier_addon - self.spot[hour]) * 100
                for hour, price in self.all_in.items() if hour in self.tariffs and hour in self.spot]

    def published(self, day, moment):
        return all(self.seen.get(hour, math.inf) <= moment + GRACE_S for hour in day_hours(day))

    def truth(self, day):
        """(hours, spot, all-in) of a local day, or None while any hour lacks either price."""
        hours = day_hours(day)
        if all(hour in self.spot and hour in self.all_in for hour in hours):
            return hours, [self.spot[hour] for hour in hours], [self.all_in[hour] for hour in hours]
        return None

    def persistence(self, known, hours):
        """Day `known`'s spot prices at the wall-clock hours of `hours`, or None if one is missing."""
        values = []
        for hour in hours:
            twin = at(known, _local(hour).time())
            if twin not in self.spot:
                return None
            values.append(self.spot[twin])
        return values

    def forecast_at(self, source, moment, max_age_s):
        found = None
        for fetched, points in self.forecasts.get(source, []):
            if moment - max_age_s <= fetched <= moment + GRACE_S:
                found = points
        return found


def ranks(values):
    """Ranks from 0; tied values share their mean rank."""
    order = sorted(range(len(values)), key=values.__getitem__)
    result = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        for k in range(i, j + 1):
            result[order[k]] = (i + j) / 2
        i = j + 1
    return result


def spearman(a, b):
    """Spearman's rank correlation, or None when either side has no spread."""
    ra, rb = ranks(a), ranks(b)
    ma, mb = statistics.fmean(ra), statistics.fmean(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb))
    va, vb = sum((x - ma) ** 2 for x in ra), sum((y - mb) ** 2 for y in rb)
    return cov / math.sqrt(va * vb) if va > 0 and vb > 0 else None


def cheapest_block(prices, length):
    """Start index of the cheapest run of `length` consecutive prices; the earliest wins a tie."""
    best = start = None
    for i in range(len(prices) - length + 1):
        mean = sum(prices[i:i + length]) / length
        if best is None or mean < best - 1e-12:
            best, start = mean, i
    return start


def day_score(forecast, spot, all_in, kwh=1.5, block=3, vat=0.25):
    """One day's metrics; the lists hold the day's hours in order, spot and all-in in DKK/kWh."""
    errors = [f - a for f, a in zip(forecast, spot)]
    guessed = [price + error * (1 + vat) for price, error in zip(all_in, errors)]

    def cost(start):
        return sum(all_in[start:start + block]) / block

    return {
        "mae": statistics.fmean(abs(error) for error in errors) * 100,
        "bias": statistics.fmean(errors) * 100,
        "rho": spearman(forecast, spot),
        "regret": (cost(cheapest_block(guessed, block)) - cost(cheapest_block(all_in, block))) * kwh,
        "mean": statistics.fmean(forecast),
        "actual_mean": statistics.fmean(spot),
    }


def score(tape, pre=time(12, 40), post=time(15, 30), max_age_s=3 * HOUR_S, kwh=1.5, block=3, vat=0.25):
    """({(source, lead): [(day, metrics, persistence's metrics on that day)]}, {source: [cheapest-day hits]})."""
    samples, picks = defaultdict(list), defaultdict(list)
    origins = sorted({_local(fetched).date() for lines in tape.forecasts.values() for fetched, _ in lines})
    for origin in origins:
        for clock, leads in ((pre, PRE_LEADS), (post, POST_LEADS)):
            moment = at(origin, clock)
            known = next((day for day in (origin + timedelta(days=1), origin) if tape.published(day, moment)), None)
            if known is None:
                continue
            days = {}
            for lead in leads:
                day = origin + timedelta(days=lead)
                truth = tape.truth(day) if day > known else None
                persisted = tape.persistence(known, truth[0]) if truth else None
                if persisted is not None:
                    base = day_score(persisted, truth[1], truth[2], kwh, block, vat)
                    samples[(PERSISTENCE, lead)].append((day, base, base))
                    days[lead] = (day, truth, base)
            for source in sorted(tape.forecasts):
                points = tape.forecast_at(source, moment, max_age_s)
                if points is None:
                    continue
                means = {}
                for lead, (day, (hours, spot, all_in), base) in days.items():
                    if all(hour in points for hour in hours):
                        metrics = day_score([points[hour] for hour in hours], spot, all_in, kwh, block, vat)
                        samples[(source, lead)].append((day, metrics, base))
                        means[lead] = (metrics["mean"], metrics["actual_mean"])
                if leads == POST_LEADS and len(means) == len(POST_LEADS):
                    picks[source].append(min(means, key=lambda lead: means[lead][0]) == min(means, key=lambda lead: means[lead][1]))
    return samples, picks


def _mean(values):
    values = [value for value in values if value is not None]
    return statistics.fmean(values) if values else None


def _fmt(value, spec):
    return "-" if value is None else format(value, spec)


def verdict(samples, source, min_days=30):
    """("ADOPT" | "no" | "not yet", [(lead, days, mean regret, persistence's mean regret)])."""
    parts = []
    for lead in ADOPT_LEADS:
        items = samples.get((source, lead), [])
        parts.append((lead, len(items), _mean(m["regret"] for _, m, _ in items), _mean(b["regret"] for _, _, b in items)))
    if any(days < min_days for _, days, _, _ in parts):
        return "not yet", parts
    return ("ADOPT" if all(regret < base for _, _, regret, base in parts) else "no"), parts


def report(tape, samples, picks, malformed=0, kwh=1.5, block=3, min_days=30, pre=time(12, 40), post=time(15, 30)):
    out = []
    scored = sorted({day for items in samples.values() for day, _, _ in items})
    out.append(f"tape: {sum(tape.lines.values())} lines, {malformed} malformed; "
               + ", ".join(f"{source} {count}" for source, count in sorted(tape.lines.items())))
    if tape.unusable:
        out.append("unusable lines: " + ", ".join(f"{source} {count}" for source, count in sorted(tape.unusable.items())))
    errors = tape.backout_errors()
    if errors:
        out.append(f"back-out check, price entity all-in -> spot vs EDS spot: n={len(errors)} "
                   f"MAE {statistics.fmean(abs(e) for e in errors):.2f} ore/kWh, max {max(abs(e) for e in errors):.2f}")
    if not scored:
        out.append("no scored days yet")
        return "\n".join(out)
    out.append(f"scored days {scored[0]} .. {scored[-1]}; D+1 from {pre:%H:%M}, D+2..D+5 from {post:%H:%M}; "
               f"spot ex VAT in ore/kWh; regret in kr per {kwh:g} kWh over the cheapest {block} h block, all-in")
    out.append("")
    out.append(f"{'lead':<6} {'source':<14} {'n':>4} {'MAE':>7} {'bias':>7} {'Spearman':>9} {'regret':>8} {'persist':>8} {'regret<=0.05':>13}")
    sources = [PERSISTENCE] + sorted(tape.forecasts)
    for name, leads in LEADS:
        for source in sources:
            items = [item for lead in leads for item in samples.get((source, lead), [])]
            if not items:
                continue
            mean = {key: _mean(m[key] for _, m, _ in items) for key in ("mae", "bias", "rho", "regret")}
            base = None if source == PERSISTENCE else _mean(b["regret"] for _, _, b in items)
            within = sum(1 for _, m, _ in items if m["regret"] <= 0.05) / len(items)
            out.append(f"{name:<6} {source:<14} {len(items):>4} {mean['mae']:>7.1f} {mean['bias']:>7.1f} "
                       f"{_fmt(mean['rho'], '.2f'):>9} {mean['regret']:>8.3f} {_fmt(base, '.3f'):>8} {within:>13.0%}")
    out.append("")
    out.append("cheapest day by mean spot among D+2..D+5 (chance 25%): " + (", ".join(
        f"{source} {sum(hits)}/{len(hits)} ({sum(hits) / len(hits):.0%})" for source, hits in sorted(picks.items()) if hits) or "no full windows yet"))
    out.append("")
    out.append(f"adopt when block regret is below persistence's on the same days at D+1, D+2 and D+3, each over >= {min_days} days:")
    for source in sources[1:]:
        result, parts = verdict(samples, source, min_days)
        detail = "  ".join(f"D+{lead} {_fmt(regret, '.3f')} vs {_fmt(base, '.3f')} ({days} d)" for lead, days, regret, base in parts)
        out.append(f"  {source:<14} {detail}  -> {result}")
    return "\n".join(out)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Score the DK2 price forecast tape.")
    parser.add_argument("tape", help="the JSONL tape")
    parser.add_argument("--pre-origin", default="12:40", help="local time before publication; D+1 is scored from here")
    parser.add_argument("--post-origin", default="15:30", help="local time after publication; D+2..D+5 are scored from here")
    parser.add_argument("--max-age-hours", type=float, default=3.0, help="oldest forecast line an origin takes")
    parser.add_argument("--kwh", type=float, default=1.5, help="energy of the block run")
    parser.add_argument("--block-hours", type=int, default=3, help="length of the block run")
    parser.add_argument("--supplier-addon", type=float, default=0.012, help="kr/kWh ex VAT the price entity adds to spot")
    parser.add_argument("--vat", type=float, default=0.25, help="VAT rate inside all-in prices")
    parser.add_argument("--min-days", type=int, default=30, help="days each lead needs before a verdict")
    args = parser.parse_args(argv)
    pre, post = time.fromisoformat(args.pre_origin), time.fromisoformat(args.post_origin)
    with open(args.tape, encoding="utf-8") as f:
        records, malformed = read_tape(f)
    tape = Tape(records, args.supplier_addon, args.vat)
    samples, picks = score(tape, pre, post, int(args.max_age_hours * HOUR_S), args.kwh, args.block_hours, args.vat)
    print(report(tape, samples, picks, malformed, args.kwh, args.block_hours, args.min_days, pre, post))
    return 0


if __name__ == "__main__":
    sys.exit(main())
