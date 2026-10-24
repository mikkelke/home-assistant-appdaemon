# Plan: a home agents can understand and residents can rely on

2026-10-23. Plan developed with an independent GPT-6 Astra architecture review and
integrated with the [whole-home source map](HOME_SYSTEM.md).
Review baseline: AppDaemon `de67630`, dashboard `51d4d1f`; these are not the current
implementation revisions. Recheck HEAD and effective configuration before rollout.
The progress record below distinguishes implemented source from deployment evidence.

## Completed rollout, 2026-10-24

Repository write access and SSH connectivity are now available. The full current suite passed:
**5,552 tests across 18 domains**, including the three socket tests blocked in the original
environment. AST parsing, the speaker-registry check and whitespace validation also pass.
Implementation commit `e7cd353` and registry cleanup `51aaa94` were pushed to master and
deployed through `scripts/deploy.sh`. The final content check matched **421 tracked app files**
on the box. The original access failures below are historical, not remaining blockers.

Live verification confirmed fresh initialization of all ten directly changed apps, plus
`bedroom_lights` and `resident_room_lights_off`, which consume the changed presence reader.
All nine room-activity publications advanced their timestamps across successive snapshots.
Natural presence changes reached the running lighting callbacks, and eleven checked light
groups retained both their state and transition timestamp through reload and observation.
EntryTruth exposed the new explicit door-evidence attributes. No artificial sensor events,
announcements, lock operations or blind movements were used as tests.

The first deploy exposed a scanner error: the build-time speaker registry was treated as an
app because it lived under `apps/`. It now lives at
[`config/sonos_speakers.yaml`](../config/sonos_speakers.yaml). The reviewed old deployed file
was moved outside the watched app tree, retaining a backup. After that cleanup, the observed
scan/runtime window contained no new error records across the app logs. The comment-only
follow-up deploy correctly required no app reload.

These checks verify deployment, ongoing publication and observed ordinary operation. No
natural total witness outage occurred in the observation window: outage/recovery behavior
remains verified by the focused tests, not by a deliberately induced household outage.

## Implementation progress, 2026-10-23

- **Package 1:** [system map](HOME_SYSTEM.md), [eight contract groups and scenario checks](HOME_CONTRACTS.md)
  are written, with source links, consumer boundaries and residual uncertainty. The appliance
  state guide now distinguishes persistent standard Lovelace helpers from custom dashboard sensors.
- **Package 2:** EntryTruth source and new tests require confirmed closed + locked for secure,
  preserve effective-lock ownership/BLE fallback, and exercise unknown evidence and recovery.
- **Package 3:** default Sonos routing protects all three residents' private rooms. All eight
  sleep combinations are covered; explicit-target/alarm and quiet-hours policies are preserved.
  Speaker registry and checker are aligned with the new group.
- **Package 4:** producer, reader and affected consumer guards are implemented locally. The
  bounded policy publishes unknown only when every witness is unreadable, preserves positive
  activity and mixed off/unreadable behavior, and requires full settle on unknown -> off recovery.
  A fresh unknown reads as no opinion without fallback. Producer and affected lighting, music,
  sleep-quiet and visitor-exit guards ship together. Partial stale evidence and direct consumers
  after a publisher stops remain limitations. DarknessCalculator's unknown -> Empty occupancy
  label is a deferred presentation gap. Targeted suites pass. Heavy review found one cross-app
  conflict: shared-space lighting could extinguish an existing dishwasher signal while kitchen
  presence was unknown. That path now preserves an already-on signal, with regressions for
  unknown, initially-off and release on explicit absence/task completion. The reviewer verified
  the correction independently and reported no remaining blocking findings.
- **Package 5:** the scenario table links existing fixture evidence to remaining physical/runtime
  proof. Guest ambiguity and blind-owner fallback remain stated boundaries, not solved guarantees.
- **Package 6:** an independent agent given only README and the repositories correctly traced
  a nighttime bathroom trip, washer completion and an unavailable entry contact to owners,
  consumers and tests, with explicit uncertainty. Its finding that the appliance guide's UI
  advice was too broad prompted the clarification above. This checks source comprehension,
  not successful household operation.

Validation reported passing suites: security **59**, notify **120**, safety **166**, TV **83**,
presence **378**, Sonos **246**, and final lighting **359** tests, plus the registry check for
**9** speakers. The broad run executed **5,501** tests across **18** domains: all suites passed
except three existing washer-plug ReadDeadline tests requiring local network sockets denied by
the execution environment. The final island-signal correction adds three tests and was verified
by the full lighting suite separately. Existing unrelated coroutine warnings remain in climate
and presence tests. These results are not proof of deployed operation.
Documentation changes are checked by resolving relative links and `git diff --check`.

The initial attempt was blocked by network restrictions and a read-only `.git/index.lock`.
Those restrictions were removed on 2026-10-24; the full test run, commit/push, combined
producer/consumer deployment and live verification are recorded in the completed rollout above.
A scoped HA configuration search for the exact `apartment_entry_secure` identifier returned no
matches in automations, scripts, scenes, helpers or dashboards, with no partial result reported.
That does not cover AppDaemon, Node-RED or templates outside that search. Validate remaining
consumers when changing this interface again; the search does not establish their absence.

## Outcome and constraints

A new agent should be able to trace a resident's need across sensors, interpretation,
decision ownership, actions/reminders, dashboard and completion evidence without private
memory or conversation history. The home should handle the selected situations consistently,
including missing data and competing requests.

Use compact documentation plus bounded changes and scenario checks. The source review does
not establish that documentation is the largest functional gap or that a central controller
is needed. Preserve domain ownership and intentional exceptions while testing those claims.
Source-level risks below are established; their frequency and deployed consequences are not.

## 1. Describe the first contracts and resident journeys

Extend the existing map with a compact contract table; split it into `HOME_CONTRACTS.md`
only if keeping it in the map makes that entrypoint unwieldy. Start with eight groups:
room/bed, personal sleep/house night, lock/entry, notification audience, blind ownership,
room comfort/sleep plan, appliance state, and upcoming tasks.

Each contract records owner, meaning, material consumers, freshness/unknown behavior,
actions/reminders, manual precedence, completion evidence and tests. Link effective YAML
and code; do not copy thresholds or enumerate every HA entity. Mark shadow/advisory outputs.
Explicitly record configuration and integrations that live outside these repositories.

First journeys: a nighttime bathroom trip, an appliance finishing, and uncertain entry status.
Explain where resident confirmation, sensor inference and physical effect differ.

**Acceptance:** Each journey identifies the decision owner and crosses both repositories
where relevant, with source links, known exceptions and checks. No new inferred behavior is
presented as established runtime reality.

**Work:** fast-worker documentation, with a heavy review of ownership and evidence.

## 2. Make unknown entry evidence explicit

Scope: [entry_truth.py](../apps/security/entry_truth.py) and a new
`apps/security/tests/test_entry_truth.py` (not present at the review baseline).

The review baseline tested `door == "on"` and then `locked and not door_open`, so unknown
contact evidence can publish secure. Adopt and test an explicit truth table:

- Secure requires a confirmed closed contact and a confirmed locked lock.
- A known open contact or known unlocked lock establishes not secure.
- Missing decisive evidence produces unknown with a reason.

Resolve indeterminate lock states explicitly in the table. Preserve LockHealth ownership
and the documented BLE fallback; do not change lock/unlock commands in this package.
The [dashboard lock resolver](../../home-assistant-hakit-dashboard/src/utils/apartmentLock.ts)
uses lock state rather than EntryTruth and must retain that distinction.

**Acceptance:** Test on/off/unknown/unavailable/missing contacts against the relevant lock
states, fallback and recovery. Unknown contact never establishes secure. Check external HA
consumers before rollout; do not imply that dashboard lock behavior was already incorrect.

**Work:** heavy review of the truth table, then fast-worker implementation and focused tests.

## 3. Give every resident equivalent default Sonos sleep protection

Scope: [SonosNotifier](../apps/notify/sonos_notifier.py), its
[configuration](../apps/notify/sonos_notifier.yaml),
[tests](../apps/notify/tests/test_sonos_notifier.py),
[speaker registry](../config/sonos_speakers.yaml) and
[registry checker](../scripts/check_speakers.py).

At the review baseline, default routing read two sleep signals and the third resident's speaker
was also in the family group. Extend the existing local routing with that resident's sleep signal and
private group, keeping registry tags and checker aligned.

**Acceptance:** Realistic three-resident fixtures cover all eight asleep/awake combinations.
Default ordinary announcements exclude each known sleeper's private speaker. Define separate
expectations for quiet hours, unavailable helpers, explicit targets and alarm exceptions;
do not silently change fire/TV callers that specify their own targets.

**Work:** fast-worker implementation, heavy review of exceptions. Independent of package 2.

## 4. Resolve presence uncertainty without breaking its consumers

Begin with [RoomActive](../apps/presence/room_active.py),
[its reader](../apps/presence/room_active_read.py) and their tests. At the review baseline,
fresh `off` could coexist with `stale="true"` even on total evidence loss, and the reader
accepted it. Fresh publication and readable sensor evidence are separate facts.

Map direct consumers before changing the contract: family-room lighting, DarknessCalculator,
FollowMe, HousemateSleepMode, ActorAttribution and UntrackedVisitor, plus reader callers.
A reader-only change cannot cover direct subscriptions; changing every output to unknown
would also have broad effects.

First characterize all-unreadable, mixed off/unreadable, positive on evidence, old publication,
startup and recovery. Decide per action what establishes safe absence. Then migrate one
consumer group at a time. Preserve positive evidence, settle timing, bed-specific unknown
semantics and intentional raw witnesses used for hallway transitions.

**Acceptance:** Complete evidence loss cannot alone trigger absence actions in migrated
consumers. Normal inactivity still works; a normally silent bed mat does not permanently hold
lights or music on. Tests cover entry, still occupancy, exit and outage recovery. List remaining
direct consumers explicitly until migration is complete.

**Work:** heavy model owns policy and compatibility design; fast-worker implements narrow
consumer groups with review between them. This is the highest compatibility-risk package.

## 5. Verify complete situations and unresolved boundaries

Use existing domain tests and add focused cases where coverage is missing; no new generic
orchestration or test platform is a prerequisite.

| Situation | Required evidence and expectation |
|---|---|
| Someone sleeps and briefly gets up | Personal sleep, house-night lighting and notification routing keep their distinct policies. |
| Presence/contact data disappears | Uncertainty does not become proven absence or secure entry; recovery restores normal behavior. |
| Guest and resident are home together | Explicit user identity differs from inferred attribution; visitor=none does not prove no guest. |
| Manual blind meets wake/shade | An active manual claim wins with a functioning owner; expiry, owner failure and direct fallback have separate expectations. |
| A task/reminder ends | Inferred Emptied, resident Done and physical effect remain distinct evidence; Undo and expiry work. |

Important boundaries: UntrackedVisitor ends a visit on resident arrival, so connecting that
signal alone cannot solve attribution around guests. Any attribution change must preserve
event-time evidence and the no-AppDaemon-calls rule inside `attribute()`. Blind manual claims
are time-limited; retain intentional fallback behavior unless a separate design changes it.

**Acceptance:** Each selected situation has an owner, competing-request rules, a test or
explicit runtime check, and stated residual uncertainty. Turn only demonstrated gaps into
further implementation tasks.

**Work:** heavy model resolves policy; fast-worker adds focused tests and bounded fixes.

## 6. Prove agent understanding and verify later rollouts

Give a fresh agent only README and the repositories, without this conversation or private
memory. Ask it to trace a nighttime bathroom trip or appliance completion: identify signal
owners, material consumers, manual precedence, missing-data behavior and relevant tests.

**Acceptance:** Correct source references and explicit uncertainty. Passing Markdown link
checks alone does not prove understanding. Run an initial attempt after package 1 so feedback
can improve the index early, then repeat only after material contract changes.

Implement and roll out packages separately. Inspect effective HA configuration and external
consumers first; afterward compare expected published states, logs and ordinary use. Do not
use dashboard taps as proof of successful help. This plan initiates no physical lock/blind
actions or artificial announcements.

## Order and validation

Package 1 establishes the vocabulary. Packages 2 and 3 can proceed independently afterward.
Prepare package 4's design in parallel, but review compatibility before changing consumers.
Package 5 follows each affected contract, and package 6 starts early and closes the work.
Keep file ownership explicit when parallel work is assigned.

Relevant implementation validation commands, from the AppDaemon root:

```sh
python3 -B -m unittest discover -s apps/security/tests -p 'test_entry_truth.py'
python3 -B -m unittest discover -s apps/security/tests -p 'test_lock_health.py'
python3 -B -m unittest discover -s apps/notify/tests -p 'test_sonos_notifier.py'
python3 -B scripts/check_speakers.py
python3 -B -m unittest discover -s apps/presence/tests -p 'test_room_active*.py'
python3 -B -m unittest discover -s apps/lights/tests -p 'test_lighting_presence_unknown.py'
```

Run relevant lighting, music, sleep/wake, attribution and blind suites for each changed consumer.
From the dashboard root, preserve the lock interface with:

```sh
npm test -- src/utils/__tests__/apartmentLock.test.ts src/components/QuickAccess/__tests__/ApartmentLock.test.ts
npx tsc -b --noEmit
```

For dashboard source changes also use its prescribed lint/build checks. Use `npx vite build`
for a check; `npm run build` invokes repository-wide formatting through prebuild and belongs
to the documented production build workflow. The original plan was based on source review;
implementation test results and deployment limitations are recorded in the dated progress above.
