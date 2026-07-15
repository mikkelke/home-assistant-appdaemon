# home-assistant-appdaemon

[![GitHub](https://img.shields.io/badge/GitHub-public-181717?logo=github)](https://github.com/mikkelke/home-assistant-appdaemon)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

AppDaemon apps for my Home Assistant smart home. This repo is the **source of truth**;
the box runs a deployed copy under `/data/appdaemon/apps`.

## Layout

- `apps/<domain>/` — one directory per domain (lights, sonos, climate, appliances, tv,
  weather, transit, intercom, blinds, rober2, rutines, notify), each app as
  `<name>.py` + `<name>.yaml` (AppDaemon auto-reloads on change).
- `apps/lights/darkness_calculator.*` — the per-room dark/bright brain every lights app
  consumes (semantic middle layer: published `binary_sensor.dark_*` / `sensor.room_state_*`
  with `reason` attributes).
- `apps/climate/smart_cooling.*` — closed-loop bedroom pre-cool for a seasonal portable AC,
  with a self-learning night-coast model.
- `apps/appliances/docs/` — appliance-manual facts distilled to markdown for AI use
  (the copyrighted PDFs themselves stay out of the repo).
- `scripts/deploy.sh` — compile-check, unit tests, rsync tracked files to the box,
  verify the reload.

## Deploy workflow

Edit locally → `scripts/deploy.sh` → the script py-compiles everything, runs the
unit tests (`apps/*/tests/`, stdlib `unittest`), rsyncs **only git-tracked files**
(runtime state on the box is never touched), then watches the AppDaemon log to
confirm the app actually re-initialized.

Note: AppDaemon only restarts an app when its **parsed yaml args** change — comment-only
yaml edits do not trigger a reload; the deploy script warns when no reload is observed.

Note: the box `appdaemon.yaml` must carry `exclude_dirs: [tests]` — AppDaemon imports
every `.py` under the apps tree, and a failing import of a test module can abort
loading of *other* new apps (bit us 2026-07-12: `apps/security/` never started until
the tests dirs were excluded).

## Deliberately not in this repo

- `apps/people/` — contains personal data of third parties.
- Runtime state (`*_state.json`, `*feedback*.json`), recorded data (CSV), logs.
- Media: appliance manuals (copyright), floor-plan sketches, maps.
- Secrets live in the box-side `secrets.yaml` / `appdaemon.yaml` (outside this
  tree, see below); nothing in this repo authenticates to anything.

## Secrets

API keys live in `/data/appdaemon/secrets.yaml` on the box (mode 600, never in this
repo) and are referenced from app yaml with `!secret <name>` — e.g. the Rejseplanen
`access_id` in `apps/transit/transit_alarm.yaml`. AppDaemon loads the secrets file
from its config dir automatically; adding a new secret = add the line on the box,
reference it in yaml, deploy.
