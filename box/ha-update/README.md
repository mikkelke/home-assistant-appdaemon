# ha-update-restart

Restarts Home Assistant once for a staged update batch, only while the flat is empty, and checks the result. The agent that stages the updates arms a run; the box does the waiting, so no chat session has to stay open.

The run happens when all of these hold:

1. A run is armed (`/data/ha-update/armed.json`).
2. Local time is 08:00-22:00.
3. `zone.home` is `0` and has been for at least 10 minutes. A HA restart resets `last_changed`, so the 10 minutes count from the later of the last departure and the last HA start.

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

## Use

```
sudo ha-update-restart arm --touched gls,parcel_aggregator --note "GLS 2.0.1, Parcel Aggregator 3.0.0" \
    --session "claude --resume <id>"
sudo ha-update-restart arm --touched gls --core 2026.11.3 --rollback-image ha-rollback:2026.11.1
sudo ha-update-restart run --dry   # window and every check against the running HA, changes nothing
sudo ha-update-restart status
```

`--touched` takes integration domains (the `custom_components/<domain>` folder name). Arming again replaces the armed batch. To disarm, `sudo rm /data/ha-update/armed.json`.

Before a dry run on staged but not yet loaded HACS updates, expect the repairs check to fail on their `restart_required` issues; that is the check working.

## Install

```
sudo install -m 0755 ha-update-restart /usr/local/sbin/ha-update-restart
sudo install -m 0644 ha-update-restart.service ha-update-restart.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now ha-update-restart.timer
```

Python 3 standard library on the host. The HA calls run inside the `homeassistant` container (its aiohttp) with the token of the `ha-mcp` container, so both containers must be running.
