#!/usr/bin/env bash
# Deploy AppDaemon apps to the HA box and verify the reload.
#
# Usage: scripts/deploy.sh
#   Deploys ALL git-tracked files under apps/ (rsync only sends changes).
#
# Safety properties:
#   - every tracked .py must compile locally before anything is sent
#   - only git-tracked files are synced: runtime state (*_state.json, feedback
#     JSONs), logs and legacy .bak files on the box are never touched
#   - after sync, the AppDaemon log is watched: reports which apps re-initialized
#     and any fresh ERROR lines; warns when NO reload happened (AppDaemon only
#     restarts an app when its parsed yaml args change)
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
git ls-files apps > "$LIST"
# apps/apps.yaml on the box is root-owned: rsync can transfer every byte and still
# exit 23 because setting that one file's mtime is EPERM for mke. A times-only
# failure must not kill the deploy before the reload check; any other rsync error
# still aborts. Real cure: ssh $HOST 'sudo chown mke /data/appdaemon/apps/apps.yaml'
rc=0
rsync -rltz --checksum --no-perms --no-owner --no-group --files-from="$LIST" . "$HOST:/data/appdaemon/" || rc=$?
rm -f "$LIST"
if [ "$rc" -eq 23 ]; then
  echo "WARNING: rsync exit 23 (attrs only, see errors above) - continuing to verify"
elif [ "$rc" -ne 0 ]; then
  exit "$rc"
fi
echo "synced (tracked files only)"

# 4. verify reload
sleep 12
RELOADS=$(ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP\" '\$0 >= d' $LOG | grep -c 'Calling initialize' || true")
ssh -o BatchMode=yes "$HOST" "awk -v d=\"$STAMP\" '\$0 >= d' $LOG | grep -E 'Calling initialize|ERROR' | tail -15" || true
if [ "${RELOADS:-0}" -eq 0 ]; then
  echo "WARNING: AppDaemon reloaded nothing. If you changed code/args, something is off;"
  echo "         if you changed only yaml comments, AD ignores it. Force with:"
  echo "         ssh $HOST 'docker restart appdaemon'"
else
  echo "OK: $RELOADS app initialization(s) observed"
fi
