# home-assistant-appdaemon

[![GitHub](https://img.shields.io/badge/GitHub-public-181717?logo=github)](https://github.com/mikkelke/home-assistant-appdaemon)
[![License: MIT](https://img.shields.io/badge/License-MIT-blue.svg)](LICENSE)

AppDaemon apps for my Home Assistant smart home. This repo is the **source of truth**;
the box runs a deployed copy under `/data/appdaemon/apps`.

Start with the [whole-home system map](docs/HOME_SYSTEM.md) for resident journeys,
shared entity contracts, dashboard connections, and the boundaries agents must preserve.
The [contract index](docs/HOME_CONTRACTS.md) connects the first eight groups to their
consumers, uncertainty rules, resident outcomes and tests.

## Layout

- `apps/<domain>/` — one directory per domain (lights, sonos, climate, appliances, tv,
  weather, transit, intercom, blinds, rober2, rutines, notify), each app as
  `<name>.py` + `<name>.yaml` (reloaded explicitly by the deploy script).
- `apps/lights/darkness_calculator.*` — the per-room dark/bright brain every lights app
  consumes (semantic middle layer: published `binary_sensor.dark_*` / `sensor.room_state_*`
  with `reason` attributes).
- `apps/climate/smart_cooling.*` — closed-loop bedroom pre-cool for a seasonal portable AC,
  with a self-learning night-coast model.
- `apps/appliances/docs/` — appliance-manual facts distilled to markdown for AI use
  (the copyrighted PDFs themselves stay out of the repo).
- `config/sonos_speakers.yaml` — build-time speaker registry checked before deploy;
  kept outside `apps/` so AppDaemon does not try to load it as an app.
- `scripts/deploy.sh` — compile-check, unit tests, rsync tracked files to the box,
  verify the reload.

## Deploy workflow

Run `bash scripts/deploy.sh` from the checkout to deploy. It compiles Python,
runs the app and deploy unit tests, checks speaker and log-stream declarations,
then rsyncs **only git-tracked files under `apps/`**. It uses checksums without
copying timestamps, so unchanged files are untouched. Runtime files and private
box-only apps are preserved; removing a file from git does not delete it on the box.

The box's private `appdaemon.yaml` must set `appdaemon.production_mode: true` and
`appdaemon.exclude_dirs: [tests]`. Native production mode skips periodic source
scans; startup and Home Assistant reconnect scans still run. Activate production
mode explicitly before using this deploy flow. After acquiring the deploy lock,
the helper enforces this configured mode through AppDaemon's native service
before allowing transfer. A refused or unreachable service fails the deploy
before transfer. The helper never changes the private configuration file.

`scripts/appdaemon_deploy.py` runs inside the existing AppDaemon container. It
holds a deploy lock across transfer and verification, records pending work under
`/data/appdaemon/.deploy/` (outside the synced apps), and checks every transferred
file against an immutable SHA256 manifest **before** sending `SIGUSR2` to PID 1.
It resolves affected apps using the installed AppDaemon config/import loaders,
including module importers, shared-module instances and app dependencies. It
subscribes to authenticated `app_initialized` events before the signal and
requires a fresh successful completion event and `idle` state for every affected
app. An initialization-start log or unrelated app reload cannot pass this check.
It also checks fresh bytes in the configured file-backed `error_log`: AppDaemon
can otherwise initialize successfully with stale arguments after skipping a bad
YAML file. Private error contents stay on the box. Existing errors in unrelated
apps are reported without failing this deploy.

Failed transfer, content verification, initialization, or timeout leaves the
journal pending. Rerun the same command after correcting the failure: pending
apps are rechecked even when rsync has no new bytes to send. YAML argument
changes invalidate only their consumer modules (plus dependencies), including
key removals that AppDaemon 4.5.13 otherwise misses. Documentation, tests and
YAML comment-only edits need no app reload. An unreferenced Python helper is
byte-verified without claiming that a scan completed.

AppDaemon 4.5.13 cannot reliably apply new dictionary-valued config keys, app
removal/disable, or changes to global-module/sequence definitions through this
verified path. These changes fail explicitly and retain pending work for a
controlled restart and independent verification; the script never restarts the
container automatically. A new app subdirectory may also need a restart to
establish its Python import path. After an independently verified restart,
archive the pending journal before starting a fresh deploy transaction.

Run the deploy unit tests without contacting the box:
`python3 -m unittest discover -s scripts/tests -p test_appdaemon_deploy.py -q`.

## Private archives

The doorbell archive (`apps/intercom/abb_welcome_bridge.*`, plus the ABB Welcome
integration's ring clips) and the Rober2 map archive (`apps/rober2/control.py`) live
under HA's media folder, `/data/homeassistant/media/{abb_doorbell,rober2_maps}` on the
box, mounted at `/media` in both the HA and AppDaemon containers. HA serves it at
`/media/local/...` to signed-in users only; the dashboard reads each `index.json`
through a `shell_command` and signs the files it shows.

- Never write either archive under `www/`: HA serves `www/` at `/local/` to anyone,
  from the internet too.
- Restore rule: HA backups and file copies taken before the move carry
  `www/abb_doorbell` and `www/rober2_maps`. After restoring one, move both out of
  `www/` straight away (to `/data/homeassistant/archive-www-backup-<date>/`), and
  check that `/local/abb_doorbell/index.json` returns 404.
- `scripts/cutover_private_archives.sh` moves both archives out of `www/` (`--dry-run`
  first) and prints the rollback.
- Both archives keep a year: `retain_days` in `abb_welcome_bridge.yaml`,
  `map_retention_days` in `rober2.yaml`.

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
