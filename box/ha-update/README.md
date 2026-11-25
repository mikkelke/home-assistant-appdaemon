# ha-update-restart

Restarts Home Assistant once for a staged update batch, only while the flat is empty, and checks the result. The agent that stages the updates arms a run; the box does the waiting, so no chat session has to stay open.

The run happens when all of these hold:

1. A run is armed (`/data/ha-update/armed.json`).
2. `zone.home` is `0` and has been for at least 10 minutes. A HA restart resets `last_changed`, so the 10 minutes count from the later of the last departure and the last HA start.
3. Nobody is heading home: every resident GPS fix (`device_tracker.of9p`, `device_tracker.kristine_iphone`, `device_tracker.claudias_iphone`) is more than 1.5 km from `zone.home` after subtracting its `gps_accuracy`. Any age counts, since the iPhones only report after a move of about 500 m. A tracker without a fix is skipped and is used as soon as it reports one.
4. No parcel is close: nothing in `sensor.parcel_aggregator_incoming_parcels` is `out_for_delivery`, or due today with `planned_from` within 60 minutes (an open window or a passed ETA counts as close). An unreadable list blocks.

A restart takes arrival auto-unlock and the intercom down for 2-3 minutes, which is what gates 3 and 4 protect. `status` and `run --dry` print each gate's verdict, and every run JSON carries them under `gates`.

Then it checks the configuration (`check_config`), copies the compose file to `docker-compose.yaml.bak-ha-update-<stamp>`, tars `.storage` to `storage-backup-ha-update-<stamp>.tar.gz` (the last 5 of each are kept), stops HA with a 120 s grace period and recreates the container once. When HA answers it waits for AppDaemon's namespace restart plus 3 minutes, then verifies:

| Check | Pass |
| --- | --- |
| core | `RUNNING`, and the armed version if `--core` was given |
| repairs | none created since the restart, no `restart_required` left |
| z2m | `binary_sensor.zigbee2mqtt_bridge_connection_state` is `on` |
| AppDaemon | reconnected, apps re-initialised, no ERROR line after that |
| touched domains | every enabled config entry `loaded` |
| HA log | no ERROR/CRITICAL line from a touched domain |

If HA is not `RUNNING` 10 minutes after the recreate and the run was armed with `--core`, the rollback image is retagged to the compose image, `.storage` is restored from the tarball and HA is recreated again.

Each run writes `/data/ha-update/runs/<stamp>.json` and `last.json` and consumes the arm file, whatever the result. The Acer watch `agent-watch-ha-update-restart` reads those files: a pass only leaves a report, a failure sends a Signal message.

## Host rounds

`arm --host` turns the batch into an OS round, run under the same gates: backups (compose, `.storage`, and the compose files of every `--project`), `apt-get update`, `full-upgrade` and `autoremove --purge` (Ubuntu keeps the running and the newest kernel, so the previous one stays in GRUB; log in `runs/<stamp>-apt.log`), `docker compose up -d` for the `--recreate` services and each `--project`, a clean HA stop, then a reboot. `ha-update-restart-boot.service` runs `post-boot` at the next boot: it waits for HA, restarts zigbee2mqtt when `mqtt` was recreated, and adds host checks to the usual ones: running kernel is the newest installed, no `reboot-required`, `apt-get -s full-upgrade` has nothing left (phased packages do not count), every container that ran before is running and not unhealthy, recreated services run the staged image, no dead Z-Wave node, every Kuma monitor up, Traefik answers for ha, ma, rethink and the Ziti router SNI, and no Traefik `ERR` since boot. A host round has no automatic rollback; the previous kernel is the fallback. `run --dry --host` shows the plan and runs every check against the running box.

```
sudo ha-update-restart arm --host --recreate mqtt,zwavejs2mqtt --project /data/opengym --touched mqtt,zwave_js --note "..."
```

SSH to the box rides Ziti, so it drops while Traefik or the ziti router restarts and during the reboot.

## Use

```
sudo ha-update-restart arm --touched gls,parcel_aggregator --note "GLS 2.0.1, Parcel Aggregator 3.0.0" \
    --session "claude --resume <id>"
sudo ha-update-restart arm --touched gls --core 2026.11.3 --rollback-image ha-rollback:2026.11.1
sudo ha-update-restart run --dry   # gates and every check against the running HA, changes nothing; works unarmed
sudo ha-update-restart status
```

`--touched` takes integration domains (the `custom_components/<domain>` folder name). Arming again replaces the armed batch. To disarm, `sudo rm /data/ha-update/armed.json`.

Before a dry run on staged but not yet loaded HACS updates, expect the repairs check to fail on their `restart_required` issues; that is the check working.

## Tests

```
python3 -m unittest discover -s box/ha-update/tests
```

## Install

```
sudo install -m 0755 ha-update-restart /usr/local/sbin/ha-update-restart
sudo install -m 0644 ha-update-restart.service ha-update-restart.timer ha-update-restart-boot.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ha-update-restart.timer
sudo systemctl enable ha-update-restart-boot.service
```

Python 3 standard library on the host. The HA calls run inside the `homeassistant` container (its aiohttp) with the token of the `ha-mcp` container, so both containers must be running.
