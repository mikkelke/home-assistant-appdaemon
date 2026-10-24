#!/usr/bin/env python3
"""Speaker-registry drift checker.

config/sonos_speakers.yaml is the source of record for the Sonos fleet. This
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
  - sonos_notifier.yaml  TTS target groups (registry `tts:` tags all/family/kristine/ms/claudia)
  - fire_safety.yaml     house-wide announcement rooms + media pause rooms, kitchen entity
  - living_room_tv_control.yaml announcement speakers (registry MA entities, never a native twin)
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
    reg = load("config/sonos_speakers.yaml")["speakers"]
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

    tts_rooms = {}
    for v in reg.values():
        for tag in v.get("tts") or []:
            tts_rooms.setdefault(tag, set()).add(v["ma_entity"])
    raw = set(pairs)

    sn_fname = "apps/notify/sonos_notifier.yaml"
    sn = next(iter(load(sn_fname).values()))
    for key, tag in (("tts_group_all", "all"), ("tts_group_family_rooms", "family"),
                     ("tts_group_kristine", "kristine"), ("tts_group_ms", "ms"),
                     ("tts_group_claudia", "claudia")):
        got, want = set(sn.get(key) or []), tts_rooms.get(tag, set())
        check(got == want,
              f"{sn_fname}: {key} != registry tts:{tag}: only-in-yaml={sorted(got - want)} "
              f"missing={sorted(want - got)}")

    fs_fname = "apps/safety/fire_safety.yaml"
    fs = next(iter(load(fs_fname).values()))
    got, want = set(fs.get("sonos_all_entities") or []), tts_rooms.get("all", set())
    check(got == want,
          f"{fs_fname}: sonos_all_entities != registry tts:all: only-in-yaml={sorted(got - want)} "
          f"missing={sorted(want - got)}")
    check(fs.get("sonos_kitchen_entity") in ma,
          f"{fs_fname}: sonos_kitchen_entity {fs.get('sonos_kitchen_entity')} is not a registry MA entity")
    pause = set(fs.get("media_pause_players") or [])
    check(pause & raw == set(), f"{fs_fname}: media_pause_players names native twins {sorted(pause & raw)}")
    check(pause & ma == ma,
          f"{fs_fname}: media_pause_players missing speakers {sorted(ma - pause)}")

    tv_fname = "apps/tv/living_room_tv_control.yaml"
    tv = next(iter(load(tv_fname).values()))
    announce = set(tv.get("announce_speakers_living") or [])
    check(announce and announce <= ma,
          f"{tv_fname}: announce_speakers_living {sorted(announce)} are not registry MA entities")

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
