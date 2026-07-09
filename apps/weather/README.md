# `appdaemon/apps/weather`

AppDaemon apps driven by **outside conditions** (weather station, wind, rain).

| App | Module | Purpose |
|-----|--------|---------|
| Easterly wind monitor | `easterly_wind_monitor` | Wind-direction / easterly monitoring (see `easterly_wind_monitor.yaml`). |
| Weather opening alert | `weather_opening_alert` | Rain + wind exposure + open windows/rooftop doors → synthetic `weather_opening_alert_*` entities for dashboards (e.g. Home Pulse). |

**Climate / room temperature** apps live in [`../climate/`](../climate/) (`climate_alarm`, etc.).

## Weather opening alert

- **Implementation spec:** [WEATHER_OPENING_ALERT_PLAN.md](WEATHER_OPENING_ALERT_PLAN.md)
- **UI consumer:** [Home Pulse design](../home_pulse/HOME_PULSE.md) (dashboard reads `weather_opening_alert_*` and climate entities).
- **Config:** [`weather_opening_alert.yaml`](weather_opening_alert.yaml) — each opening uses **`bearing` + `area`** + **`rooftop`**. Logic ignores any optional `facade` key; **bearing** is the source of truth for wind bands.

### Synthetic entities (`set_state`)

| Entity | Type | Active / idle |
|--------|------|----------------|
| `binary_sensor.weather_opening_alert_active` | binary_sensor | `on` if any alert, else `off` |
| `sensor.weather_opening_alert_priority` | sensor | `rooftop_rain`, `window_rain`, or `none` |
| `sensor.weather_opening_alert_reason` | sensor | Short text; **idle: `""`** |
| `sensor.weather_opening_alert_target_area` | sensor | Area key, e.g. `living_room`; **idle: `""`** |
| `binary_sensor.weather_opening_alert_rooftop_rain` | binary_sensor | Rooftop rain condition (see plan) |
| `binary_sensor.weather_opening_alert_window_rain` | binary_sensor | Window rain (includes 2 min clear hysteresis) |
| `sensor.weather_opening_alert_window_rain_reason` | sensor | Window reason when window alert path active; else `""` |

### Thresholds (defaults in YAML)

- Rain ≥ **0.5 mm/h**; rooftop sustain **2 min**, clear after rain below **1 min** (or any rooftop door closed).
- Window: rain + wind in **bearing ± 30°** + (speed ≥ **5 km/h** or gust ≥ **10 km/h**); combined sustain **4 min** (`max(open_ready, rain_since, wind_since)`); clear **2 min** after conditions fail.
- Wind speed/gust: normalized to **km/h** from `unit_of_measurement` when it looks like **mph**, **m/s**, **knots**, **ft/s**; otherwise values are assumed km/h (GW2000A default).

### Restart seeding

If AppDaemon restarts while conditions are already true, “since” timestamps are seeded from HA **`last_changed`** on the relevant entities (rain, contacts, wind sensors for wind-in-band) so alerts are not delayed by a full sustain period again. See plan §5.

### Exposure tuning

Change **`exposure_band_degrees`** (half-width, default 30) in YAML. **335°** window uses north wrap (see plan §7 / §11).

### Phase 2 — cold-opening

Not implemented here. Belongs under **`climate/`** (e.g. extend `climate_alarm`). See [HOME_PULSE.md](../home_pulse/HOME_PULSE.md).

### Manual verification

Follow **§11** in [WEATHER_OPENING_ALERT_PLAN.md](WEATHER_OPENING_ALERT_PLAN.md) (rooftop, single window, multi-window tie-break, 335° wrap, restart seeding, idle empty strings).

**Quick checklist (not automated here):**

1. **Log** – `appdaemon.yaml` should define `weather_opening_alert_log` → e.g. `/conf/logs/weather_opening_alert.log` (same `log:` name as in `weather_opening_alert.yaml`).
2. **335° wrap** – Only Kristines room window open + sustained rain/wind; wind **330°** / **350°** in band, **10°** out.
3. **Multi-window tie-break** – Two qualifying windows; winner = earlier `open_since`, else `room_priority` order in YAML.
