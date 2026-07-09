# Miele WEA 035 WCS Active — washing machine (distilled reference)

> **Why this doc exists:** compact interoperability reference for the AppDaemon
> appliance monitors (`washer_monitor.py`, `washer_programmes.yaml`). It distils only the
> operational facts the automations rely on — programme names, durations, energy figures,
> and behaviours that show up in the power curve. It is **not** a substitute for the
> operating instructions. Source: *Miele WEA 035 WCS Active.pdf* (Danish manual,
> M.-Nr. 11 592 880), manual © Miele & Cie. KG.

## Model

- **WEA 035 WCS Active**: freestanding front-loader, 7.0 kg, max 1400 rpm,
  850×596×636 mm, ~85 kg. CapDosing, AddLoad, no display beyond time readout.
- **Cold-water fed only** (manual forbids hot-water connection) → any temperature above
  "Cold" means the internal heater runs; heating dominates the power curve.
- Off-state consumption **0.30 W**. Voltage / connected load / fuse: nameplate only.
- Water 100–1000 kPa; drain pump head max 1.0 m. First run must be an empty Bomuld
  cycle without detergent (calibration).

## Programmes (Danish names as used by the apps)

| Programme (manual) | Temp range | Max spin | Max load | Extras (manual) |
|---|---|---|---|---|
| ECO 40-60 (YAML label "ECO") | 40–60 °C (fixed pair) | 1400 | 7.0 kg | Vand +, Iblødsætning |
| Bomuld | 90 °C → Cold | 1400 | 7.0 kg | Vand +, Iblødsætning, Forvask (selector position "Med forvask") |
| Strygelet | 60 °C → Cold | 1200 | 3.5 kg | Vand +, Iblødsætning |
| Uld  | 40 °C → Cold | 1200 | 2.0 kg | — |
| Finvask | 40 °C → Cold | 900 | 2.0 kg | Vand +, Iblødsætning |
| Ekspres 20 (YAML label "Ekspres") | 40 °C → Cold | 1200 | 3.5 kg | Kort auto-active |
| Mørkt/Denim | 60 °C → Cold | 1200 | 3.0 kg | Vand + |
| Outdoor | 40 °C → Cold | 900 | 2.5 kg | Vand + (no fabric softener) |
| Imprægnering | 40 °C fixed | 1200 | 2.5 kg | — (thermal after-treatment advised) |
| Pumpe/Centrifugering | — | 1400 | — | drain-only when spin set to "no spin" |
| Kun skyl/stivelse | — | 1400 | 7.0 kg | Vand + → second rinse |

Options: **Kort** (shorter wash), **Vand +** (higher level and/or extra rinse —
behaviour configurable), **Iblødsætning** (soak 30 min–2 h in 30-min steps, default
30 min), **Forvask** (Bomuld only). Two options max, combos limited.

## Published consumption (manual "Forbrugsdata", p. 62)

| Programme | Load | kWh | Water L | Time | Max textile °C | Rest % | rpm |
|---|---|---|---|---|---|---|---|
| ECO 40-60 (EU label prog.) | 7.0 kg | 0.70 | 55 | 3:19 | 38 | 51 | 1400 |
| ECO 40-60 | 3.5 kg | 0.47 | 50 | 2:39 | 35 | 51 | 1400 |
| ECO 40-60 | 2.0 kg | 0.27 | 26 | 2:39 | 29 | 54 | 1400 |
| Bomuld 60 | 7.0 kg | 1.20 | 50 | 2:29 | 54 | 53 | 1400 |
| Bomuld 20 | 7.0 kg | 0.35 | 69 | 2:39 | 24 | 53 | 1400 |
| Strygelet 30 | 3.5 kg | 0.45 | 52 | 1:59 | 33 | 30 | 1200 |
| Ekspres 20 (40 °C, Kort) | 3.5 kg | 0.34 | 30 | 0:20 | 27 | 60 | 1200 |
| Uld 30 | 2.0 kg | 0.23 | 35 | 0:39 | — | — | 1200 |

Note the "max temp reached in textiles" column: a "60 °C" selection actually peaks
around 54 °C — temperature selections are targets, not guarantees.

## Behaviours the monitor cares about

- **Antikrøl (anti-crease):** drum keeps moving up to **30 min** after programme end
  (setting: on by default, can be disabled). **Exception: Uld has none.** Door can be
  opened at any time during it; afterwards the machine **switches off and unlocks the
  door automatically**. Display elements stay lit only the first 15 min.
- **Forvalg (delay start):** 30 min–24 h; 30-min steps below 3 h, 1-h steps above.
  Not available in Pumpe/Centrifugering and Imprægnering. Duration is shown only after
  the delay elapses.
- **AddLoad:** door can be opened mid-programme unless water temp > 55 °C or the water
  level is too high (lock symbol).
- **Child lock:** programme/temp/spin/options are frozen once started; moving the
  selector mid-run just flags a warning until moved back. Abort = selector → off.
- **Spin behaviour:** interval spins occur after the main wash and between rinses;
  lowering the final spin also lowers interval spins. Detected imbalance automatically
  reduces the final spin (wet laundry, shorter/lower final power spike).
  Options: Flydeslut (rinse hold) and "no spin" (drain + antikrøl, sometimes an extra rinse).
- **Extra rinse triggers:** too much foam; final spin < 700 rpm (Bomuld); Vand +
  (depending on configuration). Rinses: ECO 2–3; Bomuld 2 at ≥60 °C, 3 below 60 °C
  (up to 5 with extras).
- **Energy saving:** display-off after 10 min of a running programme is **disabled by
  default** on this model; when idle the machine auto-powers down (wake by turning the
  selector). If switched off mid-programme with water in the drum, standby does not
  fully power off (safety monitoring stays live).
- Settings that stretch durations vs the table: Ekstra forvasketid Bomuld (+6/9/12 min),
  Iblødsætningstid (up to 2 h), Skånevask (Bomuld/Strygelet), Vand Plus level,
  Maks. vandstand skyl, Afkøling af vaskevand (cools wash water at end of the heating
  phase for Bomuld ≥ 70 °C), Temperatursænkning (altitude cap 80 °C).
- Hygiene reminder lamp expects a ≥ 60 °C wash now and then (manual: Bomuld 90 °C monthly).

## Extraction gaps / YAML notes

- Heater wattage / connected load: nameplate only; the 7-segment display digits in the
  PDF have no text layer (programme durations above were recovered from the layout table).
- `washer_programmes.yaml` deltas vs manual:
  - YAML labels "ECO" and "Ekspres" vs machine names **"ECO 40-60"** and **"Ekspres 20"**.
  - `eco` energy 0.78 kWh vs manual 0.70 (7 kg); other `max_energy_kwh` values also sit
    slightly above the manual figures — deliberate validation headroom.
  - `kun_skyl_stivelse.supports_water_plus: false`, but the manual explicitly allows
    Vand + there (gives a second rinse).
  - Max spin per programme and cold-capable temperature ranges match the manual exactly.
