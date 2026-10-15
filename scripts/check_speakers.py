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
  - family_room_volume_sync.yaml speakers (registry MA ids with <id>_2 twins) + tv_speaker
  - music_attribution.yaml raw_to_base_map pairs
  - dashboard (optional, if repo present): speakers.ts ids

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

    fv_fname = "apps/sonos/family_room_volume_sync.yaml"
    fv = next(iter(load(fv_fname).values()))
    fv_speakers = fv.get("speakers") or []
    for s in fv_speakers:
        check(s in ma, f"{fv_fname}: speaker {s} is not a registry Music Assistant entity")
        check(pairs.get(f"{s}_2") == s,
              f"{fv_fname}: speaker {s} has no registry native twin {s}_2")
    check(fv.get("tv_speaker") in fv_speakers,
          f"{fv_fname}: tv_speaker {fv.get('tv_speaker')} not in speakers")

    mus = next(iter(load("apps/sonos/music_attribution.yaml").values()))
    mus_map = mus.get("raw_to_base_map") or {}
    check(mus_map == pairs,
          f"music_attribution raw_to_base_map drift: only-in-yaml="
          f"{sorted(set(mus_map.items()) - set(pairs.items()))} "
          f"missing={sorted(set(pairs.items()) - set(mus_map.items()))}")

    # Dashboard (best effort - skipped when the repo is not checked out here)
    sp_ts = os.path.join(DASHBOARD, "src/config/speakers.ts")
    if os.path.exists(sp_ts):
        src = open(sp_ts).read()
        ts_ids = set(re.findall(r"id:\s*'([^']+)'", src))
        check(ts_ids == ma,
              f"dashboard speakers.ts != registry: only-in-ts={sorted(ts_ids - ma)} "
              f"missing={sorted(ma - ts_ids)}")

    if errors:
        print("SPEAKER REGISTRY DRIFT:")
        for e in errors:
            print(" -", e)
        return 1
    print(f"speaker registry OK ({len(reg)} speakers, all consumers in sync)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
