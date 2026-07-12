#!/usr/bin/env python3
"""Speaker-registry drift checker.

apps/sonos/speakers.yaml is the source of record for the Sonos fleet. This
script cross-checks every consumer that still hardcodes speaker facts and
exits non-zero on drift, so a rename/move gets flagged at deploy time instead
of surfacing as a half-broken house (the 2026-07 office rename touched ~10
surfaces by hand; the checker exists so the next one cannot be missed).

Checked:
  - state_reset.yaml   speaker list + per-speaker default volumes
  - follow_me.yaml     player list (registry MA entities; extra zones allowed)
  - group_manager.yaml player list
  - audiocast_manager.yaml raw_to_base_map pairs + line-in RINCON
  - dashboard (optional, if repo present): speakers.ts ids, audiocast.ts RINCON

Run: python3 scripts/check_speakers.py   (deploy.sh runs it before rsync)
"""

import os
import re
import sys

import yaml


class _L(yaml.SafeLoader):
    pass


_L.add_multi_constructor("!", lambda loader, suffix, node: None)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.expanduser(
    os.environ.get("DASHBOARD_REPO", "~/repositories/home-assistant-hakit-dashboard"))

errors = []


def load(path):
    with open(os.path.join(ROOT, path)) as f:
        return yaml.load(f, Loader=_L)


def check(cond, msg):
    if not cond:
        errors.append(msg)


def main():
    reg = load("apps/sonos/speakers.yaml")["speakers"]
    ma = {v["ma_entity"] for v in reg.values()}
    pairs = {v["raw_entity"]: v["ma_entity"] for v in reg.values()}
    volumes = {v["ma_entity"]: v["default_volume"] for v in reg.values()}
    linein = [v["rincon"] for v in reg.values() if v.get("line_in_host")]
    check(len(linein) == 1, f"registry must have exactly one line_in_host, found {len(linein)}")

    sr = load("apps/sonos/state_reset.yaml")
    sr_cfg = next(iter(sr.values()))
    sr_players = set(sr_cfg.get("all_speakers") or sr_cfg.get("speakers") or [])
    if sr_players:
        check(sr_players == ma,
              f"state_reset players != registry: only-in-yaml={sorted(sr_players - ma)} "
              f"missing={sorted(ma - sr_players)}")
    for ent, vol in (sr_cfg.get("speaker_volumes") or {}).items():
        check(ent in volumes, f"state_reset volume for unknown speaker {ent}")
        if ent in volumes:
            check(abs(float(vol) - float(volumes[ent])) < 0.001,
                  f"volume drift {ent}: state_reset {vol} vs registry {volumes[ent]}")

    for fname in ("apps/sonos/follow_me.yaml", "apps/sonos/group_manager.yaml"):
        cfg = next(iter(load(fname).values()))
        players = [p for p in (cfg.get("players") or cfg.get("all_speakers") or [])
                   if str(p).startswith("media_player.")]
        for p in players:
            check(p in ma or p == "media_player.rooftop",
                  f"{fname}: player {p} not in registry")

    ac = next(iter(load("apps/sonos/audiocast_manager.yaml").values()))
    ac_map = ac.get("raw_to_base_map") or {}
    check(ac_map == pairs,
          f"audiocast raw_to_base_map drift: only-in-yaml="
          f"{sorted(set(ac_map.items()) - set(pairs.items()))} "
          f"missing={sorted(set(pairs.items()) - set(ac_map.items()))}")
    uri = str(ac.get("audiocast_uri_prefix") or "")
    check(uri.endswith(linein[0]) if linein else False,
          f"audiocast URI {uri} != line_in_host rincon {linein}")

    # Dashboard (best effort - skipped when the repo is not checked out here)
    sp_ts = os.path.join(DASHBOARD, "src/config/speakers.ts")
    if os.path.exists(sp_ts):
        src = open(sp_ts).read()
        ts_ids = set(re.findall(r"id:\s*'([^']+)'", src))
        check(ts_ids == ma,
              f"dashboard speakers.ts != registry: only-in-ts={sorted(ts_ids - ma)} "
              f"missing={sorted(ma - ts_ids)}")
        aud = open(os.path.join(DASHBOARD, "src/config/audiocast.ts")).read()
        m = re.search(r"x-rincon-stream:(RINCON_\w+)", aud)
        check(bool(m) and linein and m.group(1) == linein[0],
              "dashboard audiocast.ts RINCON != registry line_in_host")

    if errors:
        print("SPEAKER REGISTRY DRIFT:")
        for e in errors:
            print(" -", e)
        return 1
    print(f"speaker registry OK ({len(reg)} speakers, all consumers in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
