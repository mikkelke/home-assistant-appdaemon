# Lighting standard (AppDaemon room apps)

## Global standard (all rooms)

**Contract — two independent questions:**
- *Is the room dark?* — `darkness_calculator` only, from environment (sun elevation, smoothed
  outdoor lux, rain, indoor daylight with lamp lux subtracted). Rolling for every zone, 24/7,
  so the state exists **before** anyone enters. Occupancy is never an input.
- *Do we need light?* — light apps: `occupied AND dark → on`; `not occupied → off`;
  `occupied AND bright → off`.

| Layer | Role |
|-------|------|
| **`darkness_calculator.yaml`** | Per-zone `outdoor_dark`/`outdoor_bright` envelope, `indoor_min_bright` sanity, lamp lux offsets, hold times |
| **`darkness_calculator`** | 4-rule state machine (see its docstring); **pushes** `sensor.room_state_*`, `sensor.darkness_*` |
| **`lighting_actions` / `room_state_darkness`** | Read push; map to on/off |
| **`*_lights.py`** | Listen to **push only**; room-specific blockers on top |

### Behaviour (logic)

| Phase | What happens |
|-------|----------------|
| **Enter** | PIR → **calculator only** → pushes `Occupied` + pre-existing dark/bright → light app runs **on that push** |
| **Inside** | Environment changes → calculator pushes confirmed dark/bright → lights adjust **gently** |
| **Leave** | PIR off → calculator pushes `Empty` → lights **off** on push |

Light apps **do not** listen to PIR for dark/bright. No entry delay. No raw lux in light apps.

### Anti-flap contract (2026-06)

Lights act on **confirmed** state only. `pending_target` (a flip currently blocked by a hold)
may only *help keep light on*: `pending_dark` → treated as dark (early ON, blocks auto-off);
`pending_bright` is **ignored** — an unconfirmed bright must never turn lights off.
Never reintroduce pending-driven turn-off; it was the main cause of light flapping.

### APIs

- `lighting_actions.register_room_state_push_listeners()` — state, `occupied`, `pending_target`, optional `sensor.darkness_*`
- `lighting_actions.apply_global_lighting()` — global on/off from pushed `room_state`
- `room_state_darkness.evaluate_auto_on/off()` — confirmed state (+ `pending_dark` as dark)

### Outdoor brightness

Dark/bright comes from **smoothed outdoor lux** (lamp-immune) with the indoor daylight sanity
check confirming bright (blinds/facade). Never reintroduce a second cutoff in light apps.

## Room-specific (not a second dark/bright system)

| Room | Extra |
|------|--------|
| **Bedroom** | Effective occupancy: bathroom door + in-bed (not only `room_state` label); sleep; blind; bed vs ceiling |
| **Bathroom** | Sleep block; bath spot in bright rooms |
| **Office** | Overnight guest block; manual switch |
| **Family room** | Island, sleep, doors, adaptive lighting; PIR off still used for kitchen handoff / latches only |
| **Guest bathroom** | Presence-only (ignores darkness) |

## Defaults when unknown

| Apps | `default_dark` |
|------|----------------|
| bedroom, bathroom | `True` |
| claudias_room | `False` |
