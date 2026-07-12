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

# 3. sync only tracked files
STAMP=$(ssh -o BatchMode=yes "$HOST" "date '+%Y-%m-%d %H:%M:%S'")
LIST=$(mktemp)
git ls-files apps > "$LIST"
rsync -rltz --checksum --no-perms --no-owner --no-group --files-from="$LIST" . "$HOST:/data/appdaemon/"
rm -f "$LIST"
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
