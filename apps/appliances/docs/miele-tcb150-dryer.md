# Miele TCB 150 WP — heat-pump dryer (distilled reference)

> **Why this doc exists:** compact interoperability reference for the AppDaemon
> appliance monitors (`dryer_monitor.py`, `dryer_programmes.yaml`). It distils only the
> operational facts the automations rely on — programme names, durations, energy figures,
> and behaviours that show up in the power curve. It is **not** a substitute for the
> operating instructions. Source: *Miele TCB150 WP.pdf* (Danish manual, M.-Nr. 11 396 001),
> manual © Miele & Cie. KG.

## Model

- **TCB 150 WP**: freestanding heat-pump condenser dryer, 7.0 kg (dry weight), drum 120 L.
- Energy class A++ (208 kWh/yr @ 160 cycles), noise 66 dB(A), refrigerant R290 (flammable).
- Condensate: 4.8 L container **or** external drain hose (max head 1.0 m / run 4.0 m).
  Manual recommends external drain for Bomuld / Bomuld eco. Full container pauses drying.
- Voltage / connected load / fuse: only on the nameplate ("se typeskiltet") — not in the manual.

## Programmes (Danish names as used by the apps)

YAML keys compose `programme__dryness[__skane]`; labels are `"<Programme> - <Dryness>[ - Skåne +]"`.

| Programme (manual) | Max load* | Dryness levels | Skåne + |
|---|---|---|---|
| Bomuld eco (manual: "Bomuld" + eco-arrow glyph) | 7.0 kg | Skabstørt only | no |
| Bomuld | 7.0 kg | Ekstra tørt, Skabstørt, Strygetørt, Rulletørt | selectable |
| Strygelet | 4.0 kg | Skabstørt, Strygetørt | selectable |
| Finvask | 2.5 kg | Skabstørt, Strygetørt | **always on** (fixed) |
| Finish uld | 2.0 kg | none (fixed short air cycle) | no |
| Skjorter | 2.0 kg | Skabstørt, Strygetørt | selectable |
| Ekspres | 4.0 kg | all four | no |
| Denim | 3.0 kg | Skabstørt, Strygetørt | selectable |
| Sengetøj | 4.0 kg | all four | no |
| Imprægnering | 2.5 kg | Skabstørt only (extra fixing phase) | no |
| Udglatning | 1.0 kg | Skabstørt, Strygetørt | **always on** (fixed) |
| Varm luft | 7.0 kg | timed, 10-min steps (app uses 20–120 min) | selectable |

\* dry-textile weight. Dryness levels: **Ekstra tørt** (extra dry), **Skabstørt** (cupboard
dry), **Strygetørt** (iron dry), **Rulletørt** (mangle dry). Bomuld eco is the EN 61121 /
EU 392/2012 label programme.

## Published consumption (manual "Forbrugsdata", EN 61121 conditions)

Columns: load (kg), washer spin (rpm), residual moisture, energy, duration.

| Programme | kg | rpm | rest % | kWh | min |
|---|---|---|---|---|---|
| Bomuld eco | 7.0 | 1000 | 60 | 1.70 | 155 |
| Bomuld eco (half) | 3.5 | 1000 | 60 | 0.96 | 95 |
| Bomuld Skabstørt | 7.0 | 1200 | 53 | 1.50 | 140 |
| Bomuld Skabstørt | 7.0 | 1400 | 50 | 1.45 | 133 |
| Bomuld Skabstørt | 7.0 | 1600 | 44 | 1.30 | 118 |
| Bomuld Skabstørt + Skåne + | 7.0 | 1000 | 60 | 1.75 | 165 |
| Bomuld Strygetørt | 7.0 | 1000/1200/1400/1600 | 60/53/50/44 | 1.25/1.10/1.00/0.85 | 120/105/98/83 |
| Strygelet Skabstørt | 4.0 | 1200 | 40 | 0.50 | 65 |
| Strygelet Skabstørt + Skåne + | 4.0 | 1200 | 40 | 0.50 | 66 |
| Finvask Skabstørt | 2.5 | 800 | 50 | 0.50 | 65 |
| Finish uld | 2.0 | 1000 | 50 | 0.02 | 5 |
| Skjorter Skabstørt | 2.0 | 600 | 60 | 0.46 | 60 |
| Ekspres Skabstørt | 4.0 | 1000 | 60 | 0.85 | 100 |
| Denim Skabstørt | 3.0 | 900 | 60 | 0.95 | 115 |
| Imprægnering Skabstørt | 2.5 | 800 | 50 | 0.75 | 95 |

No figures are published for Ekstra tørt / Rulletørt / Sengetøj / Udglatning / Varm luft.
The household washer (WEA 035) tops out at 1400 rpm, so the 1600-rpm rows never apply.

## Standby / power characteristics (state detection)

- Off-state (Po) **0.30 W**; left-on (Pl) **0.30 W**, left-on duration 15 min.
- Dryer **switches itself off automatically** after programme end (after anti-crease),
  and after 15 min idle if never started. Display/sensor keys dim after 10 min of a
  running programme (Start key blinks) — dim panel ≠ off.
- After a power cut, the interrupted programme **resumes automatically** when power returns.
- Manual explicitly forbids feeding it through an external timer/peak-load switch —
  never cut a smart-plug mid-cycle (self-ignition risk before cool-down completes).

## Behaviours the monitor cares about

- **PerfectDry** moisture sensing recalculates remaining time after start → displayed ETA
  jumps; timed programmes (Finish uld, Varm luft) have no sensing. Very small or already-dry
  loads fall back to a fixed-time run.
- **Cooling phase** ends every programme (target 55 °C, settable 40–55 °C); the programme
  is not finished until cooling is done.
- **Antikrøl (anti-crease):** after end, drum pulses periodically for up to **2 h**
  (settable: off / 1 h / 2 h, default 2 h). Exception: **Finish uld has no anti-crease**.
  Expect small motor blips in the power curve after "finish".
- **Forvalg (delay start):** up to 24 h; countdown in 1-h then 1-min steps; **drum turns
  briefly once per hour** while waiting (anti-crease pulses *before* the real start).
  Not combinable with Varm luft.
- Programme selection is locked once running (turning the selector only shows a warning);
  aborting requires selector → off. Add-laundry pauses the drum; blocked during the
  cooling phase and in Imprægnering.
- Dryness bias for Bomuld and Strygelet is user-programmable ±3 steps (affects duration
  vs the published table). Bomuld eco is never affected by bias/Memory.
- Clogged fluff/plinth filters lengthen cycles and can abort with a fault; wait 1 h after
  moving the machine before starting (heat-pump oil).

## Extraction gaps / YAML notes

- Digits shown in the 7-segment display font (Varm luft min/max, delay-start minimum,
  settings values) are vector glyphs with no text layer — unreadable in the PDF.
- `dryer_programmes.yaml` matches the manual everywhere it publishes numbers, except
  `strygelet__skabstoert` uses 66 min (manual: 65; Skåne + row: 66) — deliberate rounding.
  `bomuld__skabstoert` (140 min/1.50 kWh) is the 1200-rpm row.
