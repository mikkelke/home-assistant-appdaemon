# Miele G 5050 Vi Active — dishwasher (distilled reference)

> **Why this doc exists:** compact interoperability reference for the AppDaemon
> appliance monitors (`dishwasher_monitor.py`, `dishwasher_programmes.yaml`). It distils
> only the operational facts the automations rely on — programme names, durations, energy
> figures, and behaviours that show up in the power curve. It is **not** a substitute for
> the operating instructions. Source: *Miele G5050 VI Active.pdf* (Danish manual, HG07,
> M.-Nr. 11 533 411, covers G 5050/5052/5055/5072/5074/5077), manual © Miele & Cie. KG.

## Model

- **G 5050 Vi Active**: fully-integrated 60 cm dishwasher, 13/14 place settings
  (14 with cutlery tray), no display on the front — five programme positions + two
  option buttons.
- Water: cold or hot supply up to 60 °C, 50–1000 kPa. Built-in softener (regenerates
  with filter salt). Waterproof system active even when "off" (as long as plugged in).
- Voltage / connected load / fuse: only on the nameplate ("se typeskiltet") — not in the manual.

## Programmes

The apps use English labels; the Danish manual names differ for two of them:

| YAML label | Manual name (Danish) | Panel position | Wash / final rinse °C |
|---|---|---|---|
| ECO | ECO | ECO | 44 / 64 |
| Auto | Auto 45–65 °C | Auto | 45–65 / 65 (sensor-variable) |
| Gentle | **Automatic skåne 45 °C** | 45 °C | 45 / 70 (GlassCare flow) |
| QuickPowerWash | QuickPowerWash 65 °C | 65 °C | 65 / 65 (no pre-rinse) |
| Intensive | **Intensiv 75 °C** | 75 °C | 75 / 70 |

## Published consumption (manual "Programoversigt")

Dual values = model with cutlery tray / with cutlery basket. Times are h:min.

| Programme | kWh (cold 15 °C) | kWh (hot 60 °C) | Water L | Time (cold) | Time (hot) |
|---|---|---|---|---|---|
| ECO | 0.95/0.93 | 0.65/0.64 | 8.9 | 3:54/3:57 | 3:47 |
| Auto 45–65 °C | 0.75–1.45 | 0.45–0.80 | 6.0–16.0 | 1:48–3:33 | 1:39–3:14 |
| Automatic skåne 45 °C | 1.10 | 0.55 | 13.5 | 2:29 | 2:12 |
| QuickPowerWash 65 °C | 1.20 | 0.70 | 11.0 | 0:58 | 0:58 |
| Intensiv 75 °C | 1.55 | 0.95 | 14.0 | 3:00 | 2:46 |
| QuickPowerWash + Ekspres | — (no heating) | — | — | **0:13** (salt-rinse only) | — |

- ECO is the EU energy-label reference programme. Auto's range spans part load/lightly
  soiled → full load/heavily soiled.
- First-time programme selection displays an average duration assuming **cold-water**
  connection; the controller then corrects per cycle.

## Options

- **Ekspres** (panel button): shortens the programme by raising temperature/energy.
  Sticky per programme until changed — **except in ECO** (always resets).
  On QuickPowerWash it degenerates to the 13-min unheated rinse used to flush spilled
  salt brine after refilling the salt container.
- Settings-menu extras, each sticky for **all programmes except ECO** until disabled:
  **Opvask Plus** (longer + hotter wash), **Tørring Plus** (longer + hotter final rinse),
  **2. mellemskyl** (second interim rinse, more water/time).
- **Timer (delay start):** 30 min–24 h; set in 30-min steps up to 3 h, then 1-h steps.
  Countdown: 1-h steps above 10 h, 1-min steps below. Indicator lamps go dark a few
  minutes after arming (energy saving) — a dark panel does not mean idle.

## Power / state-detection quirks

- **EnergiManagement:** the machine powers itself off **10 min after the last key press
  or after programme end** (never mid-programme, never while a fault is shown).
  Standby draw is not published in the manual (EPREL has the label data).
- Programme end = signal tone (off by delivery default) — the fault buzzer is always on.
- **Softener regeneration every 9th cycle:** the following programme uses +4.4 L water,
  +0.015 kWh and runs ~3 min longer (figures for ECO @ 14 °dH) — expect periodic
  duration/energy outliers.
- Duration adapts each run to inlet temperature, load size, soil, detergent type and the
  regeneration cycle — displayed time for the same programme varies.
- **Opening the door pauses the programme**; it resumes a few seconds after closing.
  To change programme: power off/on within the first minutes and reselect.
- Waterproof fault (F1x): the **drain pump keeps running even with the door open**.
- Hygiene: manual recommends one Intensiv 75 °C run per month when mostly running
  < 50 °C programmes.

## Extraction gaps / YAML notes

- Connected load, fuse and standby watts: nameplate/EPREL only.
- `dishwasher_programmes.yaml` deltas vs manual:
  - `intensive`: YAML 150 min / 1.2 kWh, manual (cold water) **3:00 = 180 min / 1.55 kWh**
    (hot-water figures: 2:46 / 0.95). YAML values look empirical, not manual-derived.
  - `quick.duration_short_min: 14` vs manual's 13 min salt-rinse.
  - `eco.duration_short_min: 74` (ECO + Ekspres) is not published in the manual at all.
  - Labels "Gentle"/"Intensive" are house conventions — the machine/manual say
    "Automatic skåne 45 °C" / "Intensiv 75 °C".
