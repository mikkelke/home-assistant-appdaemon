# Washer / dishwasher / dryer state — Template loops & UI persistence

## Persistent UI entities (`input_select`)

These helpers exist in HA storage and **survive restarts**. Standard **Lovelace / badges**
can use them for a persistent main state string:

| Entity | Friendly name | Options |
|--------|---------------|---------|
| `input_select.washer_state` | Washer state | Off, Running, Paused, Unemptied, Emptied |
| `input_select.dishwasher_state` | Dishwasher state | Same |
| `input_select.dryer_state` | Dryer state | Same |

Icons: washing machine / dishwasher / tumble dryer (MDI).

**AppDaemon** still uses **`sensor.washer_state`**, **`sensor.dishwasher_state`**, **`sensor.dryer_state`** for logic and **attributes** (progress, times, etc.). Those sensors are recreated when AppDaemon runs; they are not the same as the `input_select` rows above.

The custom HAKit dashboard reads the semantic sensors directly, including the state string
and attributes: see [WasherCard](../../../home-assistant-hakit-dashboard/src/components/Washer/WasherCard.tsx).
The helper recommendation is for persistent standard Lovelace displays, not a requirement to
replace that dashboard's sensor contract. See the [shared home contracts](../../docs/HOME_CONTRACTS.md).

**Monitor apps sync automatically:** whenever the state string changes on `sensor.*_state`, they call **`input_select/select_option`** on the matching helper. Default mapping: `sensor.washer_state` → `input_select.washer_state` (same suffix). Override in app YAML with optional **`ui_state_entity`** (use `""` to disable sync).

For **progress / ETA** in the UI, keep reading **attributes** from `sensor.*_state` once AppDaemon has published them.

---

## Symptom (old Template helpers)

Log spam:

`Template loop detected ... entity_id=sensor.dryer_state ... Template[{{ this.state | default('off') }}]`

Same could affect **`sensor.washer_state`** and **`sensor.dishwasher_state`**.

## Cause

Template helpers with:

```jinja2
{{ this.state | default('off') }}
```

**read their own state** while the Template integration updates on that entity. When **AppDaemon** updates attributes, HA re-renders the template → **loop detected**.

Do **not** recreate that pattern. Use **`input_select`** when standard Lovelace needs a
persistent state string, and let AppDaemon own the semantic sensors (see above).

## Fix summary

1. **Remove** the old Template entries (*Washer / Dishwasher / Dryer state*) from **Settings → Devices & services → Template** (or disable via `homeassistant.disable_config_entry`).
2. For persistent standard Lovelace state after restart, use **`input_select.washer_state`**
   (and dishwasher/dryer). The custom dashboard continues to read `sensor.*_state`.
3. **Restart AppDaemon** so `sensor.*_state` exists again with full attributes.

### HA MCP notes

- **`ha_delete_config_entry`** may fail (`Unknown command` on some builds); delete in UI if needed.
- **`homeassistant.disable_config_entry`** with `config_entry_id` works to unload bad Template entries.
- Helpers were created with **`ha_config_set_helper`** (`input_select`, options list, `initial: Off`).

## If you ever need a *display-only* template

Use a **different** `entity_id` and only reference **`states('sensor.dryer_state')`** (or `input_select.dryer_state`) — never **`this.state`** on the same entity AppDaemon updates.
