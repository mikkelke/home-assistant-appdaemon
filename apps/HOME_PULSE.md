# Home Pulse – UI and data sources

For the current whole-home architecture, start with the [system map](../docs/HOME_SYSTEM.md).
This document describes the earlier weather/opening-alert slice of Home Pulse; its suggested
priorities are design history, not a complete description of the current dashboard.

**Home Pulse** names the dashboard experience. Its backend now includes
[`home_pulse/house_events.py`](home_pulse/house_events.py) (explanations of what happened) and
[`home_pulse/house_upcoming.py`](home_pulse/house_upcoming.py) (upcoming needs and reminders).
Weather and thermal decisions still belong to their respective domains below.

Backend logic is split by **source of truth**:

| Layer | Folder | Role |
|-------|--------|------|
| **Weather-driven opening risk** | [`apps/weather/`](weather/) | Rain, wind, facade exposure, open windows/rooftop doors → `weather_opening_alert_*` entities (see [`weather/README.md`](weather/README.md)). |
| **Thermal / comfort** | [`apps/climate/`](climate/) | Room heating vs threshold, open-window context → e.g. `climate_alarm` (`input_boolean.climate_alarm_active`, …). Future **cold-opening** for the same UI belongs here, not under `weather/`. |
| **Home Pulse UI** | (Lovelace / custom frontend) | Subscribes to the entities above; picks what to show first (priority). |

---

## Entities the UI should read (V1 rain / wind)

Implemented by **`WeatherOpeningAlert`** via `set_state` (see plan for exact IDs and attributes):

| Entity | Role |
|--------|------|
| `binary_sensor.weather_opening_alert_active` | Any weather-opening alert |
| `sensor.weather_opening_alert_priority` | `rooftop_rain` \| `window_rain` \| `none` |
| `sensor.weather_opening_alert_reason` | Short text for the user |
| `sensor.weather_opening_alert_target_area` | Room/zone key for targeting UI |

**When idle:** `priority` = `none`; `reason` and `target_area` = `""` (empty string). See [`weather/README.md`](weather/README.md).

Optional per-type binaries/reason sensors are listed in [`weather/README.md`](weather/README.md).

**Climate side (existing):** `input_boolean.climate_alarm_active`, `input_text.climate_alarm_message` from **ClimateAlarm**.

---

## Combined priority for the dashboard

Suggested order (highest first):

1. **Rooftop rain** (`weather_opening_alert_*` priority `rooftop_rain`)
2. **Window rain** (`window_rain`)
3. **Climate / cold-opening** (from `climate_alarm` or a future climate app—not `weather_opening_alert`)

The UI can merge `weather_opening_alert_priority` with climate alarm state to decide a single “headline” alert.

---

## Rules (conceptual; exact minutes in implementation plan)

- **Rooftop:** Open rooftop contact + sustained rain (no wind-direction check).
- **Window:** Open window + sustained rain + wind from exposure band (±30° around bearing) + minimum wind speed/gust.
- **Cold-opening (Phase 2, `climate/`):** Room below thermostat threshold + related opening open for a sustained period. **Do not** use `sensor.temperture_difference` for this (bedroom sensor disagreement, not “cold from opening”). See room–opening mapping notes in ClimateAlarm config / older design sections below.

**Numeric thresholds and debounce** for rain/wind alerts: authoritative values live in the app config [`weather_opening_alert.yaml`](weather/weather_opening_alert.yaml).

---

## Wind / rain exposure model

- Wind direction = direction wind is **from** (meteorological). Window **bearing** = outward-facing. Window is exposed when wind direction falls in **bearing ± 30°**, with 0/360 wrap (e.g. 335° → 305°–360° and 0°–5°).
- Rooftop doors: rain only; ignore wind direction for that alert type.

---

## Historical note

An earlier draft described a **single AppDaemon app `HomePulse`**. That was superseded:
weather alerts live in `weather/weather_opening_alert.py`, thermal alerts in
`climate/climate_alarm.py`, and the current `home_pulse/` directory contains the separate
HouseEvents and HouseUpcoming apps and their helpers/tests.
