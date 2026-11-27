# CookAssistant: agent command API

CookAssistant owns the Anova bath and the CookPerfect probe. An agent drives a cook by calling
`script.cook_command` with `op` and `data` (a JSON string). The script fires the `cook_command`
event in the caller's context. Read `sensor.cook_state` before and after every command; a command
that does not fit the current phase is ignored and logged (`cook_command ignored: ...`).

```
script.cook_command  op: probe_preset  data: '{"key": "chicken_thigh"}'
```

## Who is told

Every push goes to one person, the cook's owner. The owner is set by any command:

1. `owner` in `data` (`mikkel`, `kristine`, `claudia`) wins.
2. Otherwise the caller's HA user, resolved by ActorAttribution (the agent account resolves to Mikkel).
3. A cook nobody commanded goes to the one person `sensor.house_scenarios` names for the current
   (or last 30 min) cooking session, from phones in the kitchen, else to Mikkel.

Steps with a `say` text are also announced on the kitchen Sonos (SonosNotifier, quiet hours
respected) only while the owner's `person.*` is `home`. The owner is cleared when the cook closes.

## Safe default

A probe cook starts by itself when the probe reads, with no target. Nothing arms a pull alarm until
an agent sets one. Once the probe is in meat, the owner gets one push "Probe in the meat · no target
set" and `sensor.cook_state` shows `next_kind: no_target`, `next_label: No target set`, and no
`probe_target`, `probe_pull` or `probe_serve`.

A plan sent while the probe lane is `off` (preset, steps, hold, owner) is kept for a probe cook that
starts within 6 h of the last such command, and dropped when that cook closes. Every other cook
starts without a plan.

## Probe ops

| # | op | data | phases | effect |
|---|----|------|--------|--------|
| 1 | `probe_preset` | `{"key": "<preset>"}` or `{"key": "custom", "serve": 83}` | off, tracking, near | Sets serve/pull/rest. Custom pulls at serve - 3. |
| 2 | `probe_steps` | `{"steps": [step, ...]}` (max 10; `[]` clears) | any | Replaces the step plan. A step id that already fired never fires again in this cook. |
| 3 | `probe_hold` | `{"guest_push": "...", "guest_say": "...", "ceiling": 85}` or `{"cancel": true}` | off, tracking, near, pull | Hold for a guest (below). |
| 4 | `probe_pulled` | `{}` | pull | Starts the rest on the coldest needle. |
| 5 | `probe_done` | `{}` | any but off | Closes the probe cook. |

Presets (serve / pull / rest min): beef 54/50/15, lamb 58/54/15, pork 63/60/15, chicken (breast)
66/64/10, duck 54/51/10, fish 50/48/5, chicken_thigh 80/78/5, custom.

A step is `{"id": "olives", "core": 65, "push": "Kom oliven i fadet", "say": "Add the olives now."}`
or `{"id": "pan", "before_pull": 3, "push": "...", "say": "..."}`:

- `id`: `[a-z0-9_-]{1,32}`, unique in the plan.
- Exactly one trigger: `core` (°C, fires when the core reaches it) or `before_pull` (minutes, fires
  when the predicted pull is that close or the pull temperature is reached; needs a target).
- `push`: the push title, Danish, at most 140 characters; the message is the core temperature.
- `say`: optional English kitchen announcement, at most 200 characters.
- Steps fire only while the probe is in meat and before the pull is tapped, once per cook.

Measured on the 26.11.2026 thigh cook: the pull prediction ran early, by 2.4 min at 5 min before
the pull, 1.5 min at 4 min, 0.6 min at 3 min. A `before_pull` step therefore fires a few minutes
early; use `core` triggers when the timing matters.

### Hold for a guest

`probe_hold` is for a dish kept warm until a guest arrives. While holding:

1. At the pull temperature the push says "Pull temperature reached · 78.5°", "Holding until the
   guest rings" instead of "Pull the roast now".
2. Core at or above `ceiling` gives one push and announcement "The core is past 85 degrees."
3. The first street intercom ring (`binary_sensor.abb_welcome_gateway_intercom_ringing` on, or a
   fresh `ring` on `event.abb_welcome_gateway_intercom`) records `probe_guest_at`. The guest step
   (`guest_push`, `guest_say`) fires then if the pull temperature is reached, otherwise at the pull.

A ring without a hold is ignored. Any ring while holding counts as the guest, couriers included.

## Kitchen timer

| # | op | data | effect |
|---|----|------|--------|
| 6 | `timer_start` | `{"minutes": 45, "push": "Tjek ovnen", "say": "Check the oven."}` | One timer; a new start replaces it. `push` defaults to "Kitchen timer done". The push carries the core temperature while a probe reads. |
| 7 | `timer_cancel` | `{}` | Cancels it. |

## Bath ops

| # | op | data | effect |
|---|----|------|--------|
| 8 | `bath_start` | `{"target": 58}` | Sets the Anova and starts heating. |
| 9 | `bag_in` | `{"minutes": 150}` | Starts the cook clock (never the Anova timer). |
| 10 | `bath_extend` | `{"minutes": 30}` | Adds time. |
| 11 | `chill` / `bath_stop` / `silence` / `bath_clear` | `{}` | Ice bath, stop, silence low water, close. |

## sensor.cook_state for agents

Besides the phases and temperatures: `probe_target` ("1" when a pull target is set), `probe_owner`,
`bath_owner`, `probe_steps` (`[{"id", "on": "core 65" | "pull -3 min", "fired": ISO or ""}]`),
`probe_hold`, `probe_hold_ceiling`, `probe_guest_at`, `timer_at`, `timer_owner`.

## Example: oven chicken thighs with a late guest

```
probe_preset  {"key": "chicken_thigh"}
probe_steps   {"steps": [
                {"id": "olives", "core": 65, "push": "Kernen er 65°. Kom oliven i fadet.", "say": "Add the olives now."},
                {"id": "oven_down", "core": 76, "push": "Skru ovnen ned på 75°.", "say": "Turn the oven down to seventy five."}]}
probe_hold    {"guest_push": "Gæsten er her. Tag fadet ud og sæt panden på 7.", "guest_say": "The guest is here. Take the dish out and start the pan.", "ceiling": 85}
timer_start   {"minutes": 40, "push": "Tjek kartoflerne.", "say": "Check the potatoes."}
```
