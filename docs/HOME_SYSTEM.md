# Whole-home system map

Review baseline, 2026-10-23: AppDaemon `de67630`, dashboard `51d4d1f`.
Subsequent source changes and validation are recorded in the [implementation plan](HOME_SYSTEM_PLAN.md).
This is an agent reading guide, not proof of deployment or current household state.
The baseline did not audit live Home Assistant configuration, versions, registry data or logs.
Later checks are scoped explicitly in the plan; they do not establish deployment.
Dashboard links assume the sibling checkout `../home-assistant-hakit-dashboard` exists.

## Purpose and reading order

The home should help residents **decide, remind and do**: interpret the situation, offer a useful
choice or timely reminder, and perform actions that can actually be automated. A reminder to
close a window is a useful output in its own right. Keep human actions distinct from actuations.

Start with a resident journey below, follow its semantic entities to their producers, then read
effective YAML and the dashboard consumer. Read the relevant tests before changing behavior.
The directory tree indexes implementation domains; these journeys index how the home is used.

```text
Physical evidence + HA context
  -> domain-owned semantic state, with reason/provenance where published
  -> domain decision + actuator ownership
  -> action / reminder / dashboard explanation
  -> resident command or feedback -> updated state and future decisions
```

Home Assistant connects both repositories. AppDaemon publishes interpreted entities and calls
services; the dashboard reads state and issues resident commands. Some HA scripts, helpers and
integrations live outside these repositories, so a source trace can stop at that boundary.

## Shared reality and its owners

The [contract index](HOME_CONTRACTS.md) links meanings, owners, consumers, uncertainty,
resident outcomes and tests for the first eight groups. Read it before changing a shared signal.

| Meaning / producer | Published interface | Main readers and purpose |
|---|---|---|
| [RoomActive](../apps/presence/room_active.py), [witness configuration](../apps/presence/room_active.yaml) | `binary_sensor.<zone>_active` | Lights, music, sleep and wake routines share room evidence. Use [room_active_read](../apps/presence/room_active_read.py). |
| [HouseNightMode](../apps/presence/house_night_mode.py) | `input_boolean.house_night_mode` | Shared-space lighting preserves night behavior through a brief bed exit. |
| [DarknessCalculator](../apps/lights/darkness_calculator.py) | `binary_sensor.dark_*`, `sensor.room_state_*` | Lighting decisions and room descriptions; see [lighting standard](../apps/lights/LIGHTING_STANDARD.md). |
| [RoomFeel](../apps/climate/room_feel.py) | `sensor.<room>_feel` | Thermal interpretation with source exclusions, stale status and reasons; dashboard room story. |
| [SmartCooling](../apps/climate/smart_cooling.py), [BedroomComfort](../apps/climate/bedroom_comfort.py) | `sensor.sleep_plan`, `sensor.smart_cooling_status`, `sensor.bedroom_comfort` | Cooling, wake/blind decisions and the shared sleep-comfort explanation; follow the existing direction of ownership. |
| [LockHealth](../apps/security/lock_health.py) -> [EntryTruth](../apps/security/entry_truth.py) | `sensor.apartment_lock` -> `binary_sensor.apartment_entry_secure` | Distinguishes effective lock state from a securely closed entrance. |
| [Appliance monitors](../apps/appliances/README_appliance_state_entities.md) | `sensor.washer_state`, `sensor.dryer_state`, `sensor.dishwasher_state` and attributes | Dashboard progress, completion reminders, activity feed, maintenance and learning. |
| [Coffee publishing](../apps/coffee/coffee_publish.py) | Seven versioned `sensor.coffee_*` entities | [Dashboard contract](../../home-assistant-hakit-dashboard/src/coffee/entities.ts) and resident commands. |
| [WeatherOpeningAlert](../apps/weather/weather_opening_alert.py) | `weather_opening_alert_*` entities | Opening-risk messages, pushes and dashboard alerts; [weather contract](../apps/weather/README.md). |
| [HouseEvents](../apps/home_pulse/house_events.py) | `sensor.house_events`, `house_events_report` event | Explains meaningful cause/effect and reliable human attribution. |
| [HouseUpcoming](../apps/home_pulse/house_upcoming.py) | `sensor.house_upcoming`, [row schema](../apps/home_pulse/upcoming_core.py) | Upcoming maintenance, train and rain needs; daily maintenance reminder from the same conditions. |
| [HeatingController](../apps/climate/heating_controller.py) | `sensor.heating_zone_*`, `sensor.heating_controller` | **Shadow planning** and advice: planned setpoints are not thermostat writes. |

These are entrypoints, not an exhaustive inventory. YAML determines entity mappings and enabled
behavior; Python defaults and older comments can describe different assumptions.

## Facts that must not be substituted

- **Home:** `person.*` / `zone.home` concerns apartment-level presence, not room occupancy.
- **Room active:** combined evidence says a room is in use; it does not identify a person.
- **Bed occupied:** bed-tier witnesses answer a separate question. The shared reader documents
  bed-specific `unknown` handling; an unreadable witness must not casually become an empty bed.
- **Person asleep:** resident sleep models have different evidence; read their configuration.
- **House night:** a latched household policy, not a live synonym for one person's sleep boolean.
- **Actor:** [ActorAttribution](../apps/presence/actor_attribution.py) uses HA context first, then
  guarded presence inference. Unknown is a valid result, not permission to choose a resident.

`room_active_read.active()` returns `True`, `False` or `None`; callers must preserve the distinction.
Freshness checks use `computed_at`, with fallback evidence read from the producer's configuration.
The publisher's witness handling and a consumer's confidence guard are different layers.
At the review baseline, RoomActive could publish fresh `off` with `stale="true"` when all
witnesses were unreadable; the reader's fresh path accepted that as absence. Updated source
publishes unknown on total loss, preserves positive evidence and mixed negative/unreadable
behavior, and requires a full new settle interval on recovery to negative evidence. Fresh
unknown stays no opinion in the reader; see [validation and rollout status](HOME_SYSTEM_PLAN.md).
Partial stale evidence and direct subscriptions after publisher failure need separate treatment.
Some lights deliberately use local raw witnesses for hallway handoffs rather than whole-room
bed-inclusive occupancy. Do not replace those exceptions mechanically; read the policy comments
in [family-room YAML](../apps/lights/family_room_lights.yaml).

## Physical reality and names

The [dashboard entity map](../../home-assistant-hakit-dashboard/src/config/entities.ts) records
shared physical heating zones and historical area aliases. Several room controls can refer to
one physical heating zone; a room's displayed name need not equal its immutable HA area ID.
The [sensor-map document model](../../home-assistant-hakit-dashboard/src/sensorMap/mapDoc.ts)
connects physical placement to HA system-data documents. The actual household floor plan,
proposal records and BLE fingerprints are runtime data, not a floor plan to infer from code.

## Resident journeys

### 1. Someone goes to bed, briefly gets up, then wakes

Room/bed evidence -> personal sleep model -> latched house night -> lighting policy.
[HouseNightMode](../apps/presence/house_night_mode.py) keeps a short nighttime trip from being
interpreted as the whole house waking. [WakeupBedroom](../apps/rutines/wakeup_bedroom.py)
coordinates its own light/blind/music behavior with shared sleep/cooling signals.
Read [wake configuration](../apps/rutines/wakeup_bedroom.yaml) for the actual dependencies.

Several apps want the bedroom blind. [BlindArbiter](../apps/blinds/blind_arbiter.py) defines
manual > visitor > ventilation > wake > shade; [BlindOwner](../apps/blinds/bedroom_blind_owner.py)
coordinates ownership. An active, time-limited manual claim outranks automatic requests while
the owner is functioning. Callers have deliberate direct-write fallbacks when that owner is
missing or fails; see [wake fallback](../apps/rutines/wakeup_bedroom.py). Ownership is therefore
not an unconditional guarantee across outages.
Check the cover's documented position convention before interpreting a percentage.

### 2. A resident runs and empties a machine

Power, door and programme evidence -> appliance state/progress -> dashboard and completion
notification -> emptied detection -> feedback and maintenance history.
Distinguish evidence levels: an inferred `Emptied` state (for example from a door opening)
is not direct proof of physical unloading. A resident's Done tap, a sensor inference and an
observed device effect are different kinds of completion evidence.
The [state-entity guide](../apps/appliances/README_appliance_state_entities.md) explains why
AppDaemon owns `sensor.*_state`; do not put a competing HA template on the same entity.
Actor attribution can enrich the run later; [HouseEvents](../apps/home_pulse/house_events.py)
only names a person when the attribution method is accepted.

The dashboard also helps choose a cheaper start. Its
[start planner](../../home-assistant-hakit-dashboard/src/energy/startPlan.ts) calculates cost
and feasibility in the client. This is actual planning logic, so “all decisions are backend”
would be an inaccurate model. Changes to shared advice must trace both implementations.

### 3. Rain approaches an open terrace door or window

Weather + contact + exposure -> [opening-risk decision](../apps/weather/weather_opening_alert.py)
-> alert/reminder. Forecast rain and open-door context also feed
[HouseUpcoming](../apps/home_pulse/house_upcoming.py). Closing the opening is a resident task.
Weather exposure belongs to weather; thermal consequences belong to climate.
Read the [weather README](../apps/weather/README.md) and effective YAML for thresholds and debounce.
The [older Home Pulse note](../apps/HOME_PULSE.md) covers this slice, not the whole home.

### 4. The home notices a chore before it becomes a problem

Appliance run history, robot part life, battery history or plant evidence -> HouseUpcoming row
-> dashboard task sheet / maintenance push -> Done or Undo -> persisted task anchor.
`house_upcoming_done` and `house_upcoming_undo` close the human feedback loop.
Rows carry audience and a renewable `expires` lease: the dashboard must drop stale rows after
publisher failure. [Upcoming schema](../apps/home_pulse/upcoming_core.py) owns that contract.

### 5. A resident makes coffee and gives feedback

Machine/power evidence + resident command -> coffee state -> seven version-1 entities ->
dashboard progress/advice -> cup confirmation, pickup or rating -> updated brew history.
The [command adapter](../../home-assistant-hakit-dashboard/src/coffee/command.ts) uses
`script.coffee_command` to preserve HA caller context across the integration boundary.
[Coffee identity](../apps/coffee/coffee_who.py) separates the drinker from the notification target;
an unknown target must not become a guessed recipient.

## Dashboard and feedback entrypoints

- [Dashboard composition](../../home-assistant-hakit-dashboard/src/components/Dashboard/Dashboard.tsx):
  HomeHeader -> PulseRow -> HomePulse -> RoomGrid connects household summary to room controls.
- [Entity constants](../../home-assistant-hakit-dashboard/src/config/entities.ts): shared semantic
  contracts, including sleep-plan verdict, entry truth, activity feed and upcoming rows.
- [Room feel](../../home-assistant-hakit-dashboard/src/utils/roomFeel.ts) and
  [climate story](../../home-assistant-hakit-dashboard/src/utils/roomClimateStory.ts): presentation
  of backend interpretation; do not invent competing comfort physics in a card.
- [Upcoming view](../../home-assistant-hakit-dashboard/src/home/upcoming.ts): connects pending
  needs, appliance/robot activity and room indications.
- [UI usage report](../scripts/ui_usage_report.py): recorder-based control/open counts from
  `dashboard_ui_tap`. Counts show interaction, not satisfaction or proof of automation success.
- [MobileNotifier](../apps/notify/mobile_notifier.py) and
  [SonosNotifier](../apps/notify/sonos_notifier.py): transport, audience and interruption policy
  deserve inspection whenever changing when or whom a household reminder reaches.

## Gaps to investigate, not established runtime failures

1. Household membership is repeated. Sonos previously tracked only two sleep signals; its
   [notification YAML](../apps/notify/sonos_notifier.yaml) now protects all three residents,
   with tests for all eight sleep combinations. [House-night YAML](../apps/presence/house_night_mode.yaml)
   also tracks three. [Robot control](../apps/rober2/control.py) explicitly lists two people **and**
   checks `zone.home`; that difference alone does not prove it cleans around an occupant.
   Record intended recipients, sleep protection and exceptions before unifying anything.
   The former Sonos policy could include the third resident's private speaker while they slept.
   The source fix preserves explicit-target/alarm exceptions; local tests do not prove deployment.
2. [UntrackedVisitor](../apps/presence/untracked_visitor.py) exists, while ActorAttribution
   documents untracked humans as a residual ambiguity. Its direct attribution path does not
   consume that visitor signal. Review this boundary before expanding per-person learning.
   Visitor tracking ends a visit on a resident's arrival; it is not a complete guest inventory
   while residents are home, so connecting it alone cannot prove attribution is unambiguous.
3. Operational modes need visible labels in future indexes: actuating, advisory, shadow and
   diagnostic are different promises. Heating's planned output is the clearest example.
4. Cross-domain behavior is easier to find through these journeys than through file names.
   Extend a relevant journey when adding a consumer or changing an ownership boundary.
5. Dashboard [motion conditions](../../home-assistant-hakit-dashboard/src/components/Home/MotionConditions.tsx)
   use sleep signals or clock-based night, while the [sky](../../home-assistant-hakit-dashboard/src/home/sky.ts)
   uses solar elevation. These, the backend house-night latch and Sonos quiet hours serve
   different purposes. Make those purposes explicit before treating differences as a defect. The
   [activity decoder](../../home-assistant-hakit-dashboard/src/home/houseEvents.ts) also uses icons
   to infer event type; an icon change can therefore change behavior as well as appearance.
6. Unknown-data behavior needs scenario checks, beyond interface documentation. The updated
   RoomActive contract and affected consumer guards address total loss and recovery together;
   mixed unreadability and stopped publishers remain separate limitations. EntryTruth tested `door == "on"`
   and then `locked and not door_open` in the review baseline. The updated EntryTruth now
   requires confirmed closed + locked, and publishes unknown for unresolved evidence; its
   new truth-table, fallback and recovery tests cover that boundary. This is not evidence of
   a deployed incident or proof that the fix has been deployed.

## Keeping this map useful

See the [implementation plan](HOME_SYSTEM_PLAN.md) for staged contract documentation,
targeted behavior changes, scenario checks and a fresh-agent comprehension check.

The [contract inventory](HOME_CONTRACTS.md) now links **owner, meaning/states, reason,
freshness/unknown policy, audience, effects, consumers and tests** for eight initial groups.
Extend those entries when a shared signal changes rather than duplicating its interpretation.
Preserve domain owners and existing arbitration as the starting point. This source review has
not established a need for a new central controller, nor that documentation is the largest
remaining functional gap. Check concrete scenarios for competing actions and unresolved needs.
Link contracts to code rather than copying thresholds or residents' live routines here.

For each change, trace producer -> shared state -> every consumer in both repositories ->
resident outcome, then verify effective YAML, relevant tests and actual deployment separately.
For each journey, record the decision owner, competing requests, missing/stale-data behavior,
expected action or reminder and observable completion evidence. Start scenario checks with a
sleeping resident receiving an announcement, a guest present alongside a resident, unavailable
presence/door sensors, and conflicting manual/automatic blind requests.
Unit tests under each domain's `tests/` and dashboard `__tests__/` establish behavior in fixtures;
only current HA state/logs and observed use establish what the home is doing now.
