# Shared home contracts and resident outcomes

Read this through the [system map](HOME_SYSTEM.md). This is a selective source index,
not an entity export or evidence of current deployment. Effective app YAML owns mappings;
the linked tests establish fixture behavior. HA scripts, integrations, physical placement
and runtime state can require evidence outside these repositories.

For a change, identify **need -> evidence -> decision owner -> consumers -> action/reminder
-> completion evidence**. Preserve the distinction between an observed fact, an inference,
a resident's confirmation and a successful physical action.

## 1. Room activity and bed occupancy

- **Owner/interface:** [RoomActive](../apps/presence/room_active.py), its
  [witness YAML](../apps/presence/room_active.yaml), `binary_sensor.<zone>_active` and
  [room_active_read](../apps/presence/room_active_read.py). Activity does not identify a person.
- **Consumers:** lighting, [FollowMe](../apps/sonos/follow_me.yaml), sleep/wake,
  [attribution](../apps/presence/actor_attribution.py) and
  [visitor evidence](../apps/presence/untracked_visitor.py). Check direct subscriptions as
  well as reader callers. Local raw witnesses in family-room hallway handoffs are intentional.
- **Unknown/freshness:** publication age (`computed_at`) and unreadable witnesses (`stale`)
  are separate. Positive activity, proven absence and no opinion need different responses.
  Total witness loss publishes unknown; positive evidence wins over partial loss, and mixed
  off/unreadable preserves the existing union policy. Unknown -> off recovery requires a new
  full settle interval, guarded against callbacks from cancelled timers. Fresh unknown reads
  as no opinion without fallback; stale/missing publication uses the reader's live fallback.
  The bed reader deliberately treats a cloud mat's unknown as no report; unavailable is
  different. Never substitute whole-room activity for bed occupancy or use it as a head count.
- **Proof:** [publisher tests](../apps/presence/tests/test_room_active.py),
  [reader tests](../apps/presence/tests/test_room_active_read.py), then tests of each affected
  consumer. Ordinary exit, quiet occupancy, data loss and recovery are separate cases.
  [Lighting uncertainty tests](../apps/lights/tests/test_lighting_presence_unknown.py) and
  [kitchen/hall music tests](../apps/sonos/tests/test_follow_me_kitchen_trust.py) exercise
  consumer holds, recovery listeners and mixed evidence. Ship these guards with the producer.
- **Room labels:** [DarknessCalculator](../apps/lights/darkness_calculator.py) publishes
  `Occupied|Empty|Unknown (Dark|Bright)` independently of brightness. Its `occupied` attribute
  is the string `true`, `false` or `unknown`, with a separate `occupancy_reason`. Semantic
  activity inputs use the shared reader above; combined raw groups retain the bounded union
  policy. [Lighting actions](../apps/lights/lighting_actions.py) requires a valid `presence_ts`
  no older than 300 seconds before reading cached occupancy. Unknown occupancy leaves lights
  unchanged, including when brightness changes. Explicit local occupancy keeps its own owner.
  [Room-label tests](../apps/lights/tests/test_room_state_occupancy.py) cover publication,
  serialization, freshness and recovery. This source contract does not establish deployment.

## 2. Personal sleep and household night

- **Owners:** [Mikkel sleep](../apps/rutines/mikkel_sleep_mode.py),
  [housemate sleep](../apps/presence/housemate_sleep_mode.py) and
  [HouseNightMode](../apps/presence/house_night_mode.py). Personal evidence differs by resident.
- **Meaning:** a personal sleep boolean is not the household lighting latch. A brief bed exit
  must not wake shared-space lighting. Manual holds and natural boundaries belong to the latch.
- **Consumers:** [family-room policy](../apps/lights/family_room_lights.yaml),
  [wake routine](../apps/rutines/wakeup_bedroom.yaml) and notification policy. Dashboard
  [motion conditions](../../home-assistant-hakit-dashboard/src/components/Home/MotionConditions.tsx),
  solar sky appearance and TTS quiet hours have different purposes.
- **Proof:** [house-night tests](../apps/presence/tests/test_house_night_mode.py),
  [housemate tests](../apps/presence/tests/test_housemate_sleep_mode.py),
  [wake replay](../apps/rutines/tests/test_wakeup_replay.py) and
  [shared lighting sleepers](../apps/lights/tests/test_family_room_sleepers.py).

## 3. Lock state and secure entry

- **Owners:** [LockHealth](../apps/security/lock_health.py) arbitrates lock twins;
  [EntryTruth](../apps/security/entry_truth.py) combines the effective lock with the contact.
  `sensor.apartment_lock` and `binary_sensor.apartment_entry_secure` answer different questions.
- **Boundary:** secure requires evidence for both closed and locked. The documented BLE
  fallback applies when the lock arbiter is unavailable; it cannot establish door closure.
  Read `reason`, source and door evidence when evaluating uncertainty.
- **Consumers:** [dashboard lock resolver](../../home-assistant-hakit-dashboard/src/utils/apartmentLock.ts)
  and its hook use the lock contract for lock controls. Do not replace it with secure-entry
  state. Search live HA consumers separately before changing entry semantics.
- **Proof:** [entry truth table, fallback and recovery](../apps/security/tests/test_entry_truth.py),
  [lock tests](../apps/security/tests/test_lock_health.py) and dashboard
  [resolver tests](../../home-assistant-hakit-dashboard/src/utils/__tests__/apartmentLock.test.ts).

## 4. Reminder audience and interruption policy

- **Owners:** [MobileNotifier](../apps/notify/mobile_notifier.py) and
  [SonosNotifier](../apps/notify/sonos_notifier.py), with their YAML. A drinker, inferred actor,
  person to notify and currently present resident are not interchangeable identities.
- **Policy:** inspect category plus target for pushes; home-target filtering does not imply
  identical filtering for a direct person target. For TTS distinguish ordinary default routing,
  explicit speakers, quiet-hours override and alarm callers.
  Default routing adds each awake resident's private room to shared speakers; it protects all
  three known sleepers. Unknown sleep helpers retain the existing quiet-hours-dependent rule.
- **Topology:** [speaker registry](../config/sonos_speakers.yaml) owns speaker facts and tags;
  [check_speakers](../scripts/check_speakers.py) detects duplicated-list drift.
- **Proof:** [mobile tests](../apps/notify/tests/test_mobile_notifier.py) and
  [TTS tests](../apps/notify/tests/test_sonos_notifier.py). A successful routing decision is
  not proof that a speaker played or that the reminder helped.

## 5. Shared blind ownership

- **Owner:** [BlindOwner](../apps/blinds/bedroom_blind_owner.py) applies
  [arbitration](../apps/blinds/blind_arbiter.py): manual > visitor > ventilation > wake > shade.
  Follow the owner's YAML and claim lifetime rather than copying its priorities elsewhere.
- **Boundaries:** manual claims expire. A new visitor claim clears an older manual hold;
  manual input during an ongoing valid visitor claim still wins. Callers have direct-write fallback when
  the owner fails or is missing. A requested target is not observed cover movement; position
  conventions and actual reported state matter.
- **Consumers:** [wake](../apps/rutines/wakeup_bedroom.py),
  [solar shade](../apps/blinds/bedroom_solar_shade.py),
  [visitor lifecycle](../apps/presence/untracked_visitor.py) and manual control paths.
- **Proof:** [arbiter](../apps/blinds/tests/test_blind_arbiter.py),
  [owner](../apps/blinds/tests/test_bedroom_blind_owner.py) and
  [wake yield](../apps/rutines/tests/test_wakeup_blind_yield.py) tests, including owner failure.

## 6. Room comfort and sleep planning

- **Owners:** [RoomFeel](../apps/climate/room_feel.py),
  [SmartCooling](../apps/climate/smart_cooling.py),
  [BedroomComfort](../apps/climate/bedroom_comfort.py) and shared
  [climate model](../apps/climate/climate_model.py). Follow existing plan ownership direction.
- **Interfaces/consumers:** `sensor.<room>_feel`, `sensor.sleep_plan` and bedroom comfort feed
  dashboard [room stories](../../home-assistant-hakit-dashboard/src/utils/roomClimateStory.ts),
  wake/blind decisions and notifications. Do not derive competing comfort physics in a card.
- **Modes:** [HeatingController](../apps/climate/heating_controller.py) writes thermostat
  setpoints only for configured `live_zones` while its live switch is on; other zones stay
  in shadow. Each zone publishes its live status. An open window lowers a live zone's setpoint.
  Source freshness and excluded sensors belong to the interpretation, not just the temperature.
- **Proof:** [RoomFeel](../apps/climate/tests/test_room_feel.py),
  [cooling](../apps/climate/tests/test_smart_cooling.py) and relevant wake tests.

## 7. Machine tasks and resident feedback

- **Owners:** [appliance state guide](../apps/appliances/README_appliance_state_entities.md)
  and [coffee publisher](../apps/coffee/coffee_publish.py). A finished programme may leave an
  outstanding human task. Sensor-inferred Emptied does not prove physical unloading.
- **Identity/commands:** [ActorAttribution](../apps/presence/actor_attribution.py) accepts
  context identity or bounded inference. Guest absence is not established by `visitor=none`:
  [UntrackedVisitor](../apps/presence/untracked_visitor.py) ends visits at resident arrival.
  Coffee's [command adapter](../../home-assistant-hakit-dashboard/src/coffee/command.ts)
  preserves HA user context through `script.coffee_command`.
- **Other decision owner:** the dashboard [start planner](../../home-assistant-hakit-dashboard/src/energy/startPlan.ts)
  calculates price and feasibility. Trace both repositories when changing start advice.
- **Proof:** [attribution](../apps/presence/tests/test_actor_attribution.py),
  [washer door-aware completion](../apps/appliances/tests/test_washer_door_aware_finish.py),
  [coffee publishing](../apps/coffee/tests/test_coffee_publish.py) and relevant UI tests.

## 8. Upcoming needs and explanations

- **Owners:** [HouseUpcoming](../apps/home_pulse/house_upcoming.py) publishes
  `sensor.house_upcoming`; [HouseEvents](../apps/home_pulse/house_events.py) publishes
  `sensor.house_events`. An upcoming need and an explanation of a past action are distinct.
- **Freshness/audience:** [row schema](../apps/home_pulse/upcoming_core.py) owns the renewable
  `expires` lease and audience fields. The dashboard must drop expired needs and respect
  their audience; display is not proof of delivery or resolution.
- **Feedback:** Done/Undo persist task anchors. Inferred watering or unloading, a Done tap,
  and verified physical work are different evidence. An event report must describe a real
  action/transition, not startup reassertion. UI event type currently partly depends on icons.
- **Proof:** [upcoming](../apps/home_pulse/tests/test_house_upcoming.py),
  [events](../apps/home_pulse/tests/test_house_events.py) and dashboard contract tests.

## Read and verify a whole situation

| Situation and expected behavior | Existing fixture evidence | Remaining live / physical proof |
|---|---|---|
| Nighttime bathroom trip: personal sleep may change without waking shared-space lighting; ordinary TTS protects known sleepers' private rooms. | [House-night latch](../apps/presence/tests/test_house_night_mode.py), [shared lighting sleepers](../apps/lights/tests/test_family_room_sleepers.py), [eight TTS sleep combinations and exceptions](../apps/notify/tests/test_sonos_notifier.py). | Observe real room evidence, night lighting and ordinary routing after deployment; explicit alarm targets have separate policy. No artificial announcements are required. |
| Washer finishes: completion leaves an outstanding unloading task; a named recipient requires evidence. | [Door-aware completion](../apps/appliances/tests/test_washer_door_aware_finish.py), [manual resolution/context](../apps/appliances/tests/test_appliance_force_emptied.py), [actor inference](../apps/presence/tests/test_actor_attribution.py), [activity feed](../apps/home_pulse/tests/test_house_events.py). | Observe a normal run through UI, reminder and resolution. A door opening or Emptied inference does not prove physical unloading. |
| Entry evidence disappears: unknown is not secure; lock controls remain a separate contract. | [Truth table/outage/recovery](../apps/security/tests/test_entry_truth.py), [lock arbitration](../apps/security/tests/test_lock_health.py), [dashboard resolver](../../home-assistant-hakit-dashboard/src/utils/__tests__/apartmentLock.test.ts). | Verify actual contact/lock publication and deployed external consumers. Do not operate the physical lock merely to validate documentation. |
| Guest and resident coexist: direct HA context and inferred actor remain distinct; unknown is allowed. | [Actor attribution](../apps/presence/tests/test_actor_attribution.py), [visitor lifecycle](../apps/presence/tests/test_untracked_visitor.py). | Visitor=none cannot prove no guest, particularly after a resident arrives. No complete guest-aware attribution policy was implemented. |
| Manual blind meets wake/shade: a live manual claim wins while the owner works; expiry and owner failure differ. | [Arbitration](../apps/blinds/tests/test_blind_arbiter.py), [ownership/expiry](../apps/blinds/tests/test_bedroom_blind_owner.py), [wake refusal and direct fallback](../apps/rutines/tests/test_wakeup_blind_yield.py). | Confirm physical position and ownership state during ordinary use. An unavailable owner retains intentional direct-write fallback; precedence is not guaranteed through that failure. |
| A maintenance task ends: Done/Undo update anchors; expired rows disappear. | [Upcoming task integration](../apps/home_pulse/tests/test_house_upcoming.py), [row lease contract](../apps/home_pulse/tests/test_upcoming_core.py). | A Done tap is resident confirmation, not independent verification of the physical work. Check deployed audience and lease handling. |

For every situation, state what changes automatically, what remains a resident action, which
manual choice wins, and what happens when evidence or the responsible app disappears.
Check existing tests before adding new ones. Verify ordinary use and outage/recovery separately.
Use invented fixtures; do not publish household logs or runtime exports.

RoomActive uncertainty and its affected direct consumers form one compatibility package:
total witness loss publishes unknown, positive evidence is preserved, and negative recovery
requires full settle; fresh unknown yields no opinion in the reader. See the plan's validation
record for source-review and deployment status.
Mixed off/unreadable evidence, direct subscriptions after publisher failure and the darkness
occupancy label are separate limitations. This table does not claim every outage path is fixed.
