# Apartment wind-exposure description for Home Assistant alert logic

## General assumptions

- Bearings below are the outward-facing direction of each opening.
- Use meteorological wind direction: a window is "wind-exposed" when the wind comes from roughly the same direction as the window/door faces.
- Use broad tolerance bands of about +/- 30 degrees for initial logic because the apartment is in a dense urban environment and wind will be deflected by nearby buildings.
- Rooftop doors are a higher-priority rain risk than windows.

## Openings and bearings

- `binary_sensor.bathroom_window_contact`: ~70° (ENE)
- `binary_sensor.bedroom_window_contact`: ~70° (ENE)
- `binary_sensor.dining_room_window_1_contact`: ~70° (ENE)

- `binary_sensor.living_room_window_contact`: ~155° (SSE)
- `binary_sensor.kitchen_window_contact`: ~155° (SSE)
- `binary_sensor.dining_room_window_2_contact`: ~155° (SSE)
- `binary_sensor.dining_room_window_3_contact`: ~155° (SSE)

- `binary_sensor.kristines_room_window_contact`: ~335° (NNW)

- `binary_sensor.rooftop_door_1_contact`: ~245° (WSW/SW)
- `binary_sensor.rooftop_door_2_contact`: ~245° (WSW/SW)

## Suggested exposure bands

- **70° openings:** trigger on wind directions roughly 40° to 100°
- **155° openings:** trigger on wind directions roughly 125° to 185°
- **335° openings:** trigger on wind directions roughly 305° to 5° (wrap around north)
- **245° rooftop doors:** trigger on wind directions roughly 215° to 275°

## Daylight and illuminance (darkness calculator)

Window directions and facade groups above are also the right physical context when interpreting **indoor lux** for `darkness_calculator` (`sensor.darkness_*`, thresholds in `apps/lights/darkness_calculator.yaml`). Sun hits **ENE (~70°)** and **SSE (~155°)** openings at different times and intensities, so per-room FP300 sensors will not match each other even when “it’s bright outside.”

- **Bedroom** and **dining window 1** sit on **ENE**; **kitchen**, **living room**, and **dining windows 2–3** sit on **SSE**. The **family_room** zone in AppDaemon averages kitchen + living room + dining illuminance — i.e. it mixes **both** main daylight facades, so the zone average is a deliberate **whole-area** signal, not a single-window orientation.
- Threshold tuning (e.g. from HA history/statistics) should be read together with this layout: hysteresis and “stuck between dark/bright” behavior can differ by room because lux scale and sun exposure differ by facade.

## Facade groups

### ENE facade (~70°)

- bathroom window
- bedroom window
- dining room window 1

### SSE facade (~155°)

- living room window
- kitchen window
- dining room window 2
- dining room window 3

### NNW facade (~335°)

- Kristines room window

### WSW/SW facade (~245°)

- rooftop door 1
- rooftop door 2

## Alert policy

- **Rooftop doors open + live rain** = high alert, regardless of wind direction.
- **Windows open + live rain** = lower alert, but only if the wind direction matches the window's exposure band.
- If desired, wind speed or gust thresholds can be added so light rain with weak wind does not trigger a window-rain alert.

## Existing HA/backend context

- There is already backend climate/opening logic in Home Assistant:
  - `automation.kristines_room_climate_control`
  - Claudias Room (ex office): the setpoint logic was ported to AppDaemon
    (`apps/climate/claudias_room_climate.py`, July 2026); the former
    family-room climate automation no longer exists
- `sensor.temperture_difference` exists, but its exact meaning should be verified before using it in final alert logic.
- **Family-room/opening logic** currently appears to involve:
  - kitchen_window_contact
  - dining_room_window_1_contact
  - dining_room_window_2_contact
  - dining_room_window_3_contact
  - living_room_window_contact
  - rooftop_door_1_contact
- **Office logic** appears tied to rooftop_door_2_contact.
- **Kristines room logic** appears tied to kristines_room_window_contact.

## Recommended implementation direction

- Keep the reasoning in Home Assistant backend/template entities.
- Expose simple alert entities to the dashboard, such as:
  - `binary_sensor.home_pulse_rooftop_rain_alert`
  - `binary_sensor.home_pulse_window_rain_alert`
  - `sensor.home_pulse_window_rain_reason`
  - `binary_sensor.home_pulse_cold_opening_alert`
  - `sensor.home_pulse_cold_opening_reason`
