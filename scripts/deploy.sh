#!/usr/bin/env bash
# Deploy AppDaemon apps to the HA box and verify the reload.
#
# Usage: scripts/deploy.sh
#   Deploys ALL git-tracked files under apps/ (rsync only sends content changes).
#
# Safety properties:
#   - every tracked .py must compile locally before anything is sent
#   - only git-tracked files are synced: runtime state (*_state.json, feedback
#     JSONs), logs and legacy .bak files on the box are never touched
#   - only content changes are written (--checksum, no -t): unchanged files are
#     left completely alone, so local mtime churn (worktrees, rebases, fresh
#     clones) no longer makes AppDaemon re-read every yaml and restart all apps
#   - changed files are staged in per-dir .~tmp~ dirs on the box and renamed
#     into place together at the end of the transfer (--delay-updates): the 1s
#     config scan can't catch a half-synced tree; each rename is atomic and
#     AppDaemon skips dot-dirs, so the staging area is invisible to it
#   - after sync, the AppDaemon log is watched: reports which apps re-initialized
#     and any fresh ERROR lines; warns when content changed but NO reload
#     happened (AppDaemon only restarts an app when its parsed yaml args change)
#   - if the config scan crashed (AppDaemon 4.5.x deep_compare does data[k], so
#     a yaml gaining a new dict-valued key raises KeyError; the freshly-read
#     config is dropped and every later scan of that file crashes the same way),
#     the only cure is a restart: deploy.sh restarts appdaemon and re-verifies
set -euo pipefail

HOST="mke@10.21.0.5"
LOG="/data/appdaemon/logs/appdaemon.log"

cd "$(git rev-parse --show-toplevel)"

# 1. compile gate
mapfile -t pyfiles < <(git ls-files 'apps/**/*.py' 'apps/*.py')
for f in "${pyfiles[@]}"; do
  PYTHONDONTWRITEBYTECODE=1 python3 -m py_compile "$f"
done
echo "py_compile OK (${#pyfiles[@]} files)"

# 2. unit tests (stdlib unittest, no external deps) - every apps/*/tests dir
for tdir in apps/*/tests; do
  [ -d "$tdir" ] && python3 -m unittest discover -s "$tdir" -q
done
echo "unit tests OK"

# 2b. speaker registry drift guard
python3 scripts/check_speakers.py

# 3. sync only tracked files
# STAMP must be in the AppDaemon log's timezone (Europe/Copenhagen), not the box
# clock (UTC): a UTC stamp sits 2h behind the log in summer, so the verify window
# would include 2h of history and count stale initializations as fresh reloads.
STAMP=$(ssh -o BatchMode=yes "$HOST" "TZ=Europe/Copenhagen date '+%Y-%m-%d %H:%M:%S'")
LIST=$(mktemp)
ITEMS=$(mktemp)
git ls-files apps > "$LIST"
# apps.yaml on the box was root-owned until 2026-07-16 (chowned to mke since).
# Keep tolerating an attrs-only rsync failure (exit 23): it must not kill the
# deploy before the reload check. Any other rsync error still aborts.
rc=0
rsync -rlz --checksum --delay-updates --no-perms --no-owner --no-group \
  --out-format='%i %n' --files-from="$LIST" . "$HOST:/data/appdaemon/" >"$ITEMS" || rc=$?
CHANGED=$(grep -c '^[<>]f' "$ITEMS" || true)
sed -n 's/^[<>]f[^ ]* /  changed: /p' "$ITEMS"
rm -f "$LIST" "$ITEMS"
if [ "$rc" -eq 23 ]; then
  echo "WARNING: rsync exit 23 (attrs only, see errors above) - continuing to verify"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
echo "synced (tracked files only): $CHANGED file(s) changed on the box"

# 4. verify reload
if [ "$CHANGED" -eq 0 ] && [ "$rc" -eq 0 ]; then
  echo "OK: no content changes - AppDaemon untouched, nothing to reload"
  exit 0
fi
sleep 12
RELOADS=$(ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP\" '\$0 >= d' $LOG | grep -c 'Calling initialize' || true")
ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP\" '\$0 >= d' $LOG | grep -E 'Calling initialize|ERROR' | tail -15" || true

# 4b. config-scan crash check: when this fires, AppDaemon dropped the freshly-
# read config (the deployed yaml is NOT live) and every later scan of that file
# crashes the same way. A full restart is the only way back to a clean store.
CRASHES=$(ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP\" '\$0 >= d' $LOG | grep -c 'Unexpected error during utility' || true")
if [ "${CRASHES:-0}" -gt 0 ]; then
  echo "ERROR: AppDaemon's config scan crashed $CRASHES time(s) during this deploy"
  echo "       (deep_compare KeyError - a yaml gained a new dict-valued key)."
  echo "       The new config was NOT stored; restarting appdaemon to heal..."
  ssh -o BatchMode=yes "$HOST" "docker restart appdaemon" >/dev/null
  STAMP2=$(ssh -o BatchMode=yes "$HOST" "TZ=Europe/Copenhagen date '+%Y-%m-%d %H:%M:%S'")
  sleep 25
  RELOADS2=$(ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP2\" '\$0 >= d' $LOG | grep -c 'Calling initialize' || true")
  CRASHES2=$(ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP2\" '\$0 >= d' $LOG | grep -c 'Unexpected error during utility' || true")
  if [ "${RELOADS2:-0}" -gt 0 ] && [ "${CRASHES2:-0}" -eq 0 ]; then
    echo "OK: appdaemon restarted clean, $RELOADS2 app initialization(s)"
  else
    echo "ERROR: appdaemon still unhealthy after restart (inits=$RELOADS2, crashes=$CRASHES2) - check $LOG"
    exit 1
  fi
  exit 0
fi

if [ "${RELOADS:-0}" -eq 0 ]; then
  echo "WARNING: content changed but AppDaemon reloaded nothing. If you changed code/args,"
  echo "         something is off; if you changed only yaml comments, AD ignores it."
  echo "         Force with: ssh $HOST 'docker restart appdaemon'"
else
  echo "OK: $RELOADS app initialization(s) observed"
fi
