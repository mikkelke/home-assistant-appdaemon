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

# 2c. undeclared log-stream guard. Custom `log:` streams must be declared in appdaemon.yaml,
# which lives ONLY on the box - an app yaml naming an undeclared stream instantiation-crashes
# that app on deploy (AppInstantiationError: User defined log ... not found). Bit three times
# in five days (ForecastLog 08-09, AbbWelcomeBridge 08-12, BedPresenceCompare 08-13), each a
# new app copying the pattern from a neighbour whose stream pre-exists. Compare against the
# box's live declarations so the failure happens HERE, before the sync.
declared=$(ssh mke@10.21.0.5 "grep -oE '^  [a-z_]+_log:' /data/appdaemon/appdaemon.yaml" | tr -d ' :')
used=$(grep -rhE '^  log: *[a-z_]+ *$' apps/*/*.yaml | sed 's/.*log: *//' | sort -u)
for stream in $used; do
  if ! echo "$declared" | grep -qx "$stream"; then
    echo "FATAL: apps yaml references log stream '$stream' not declared in the box appdaemon.yaml" >&2
    grep -rln "log: *$stream" apps/*/*.yaml >&2
    exit 1
  fi
done
echo "log-stream guard OK"

# 3. sync only tracked files
# STAMP must be in the AppDaemon log's timezone (Europe/Copenhagen), not the box
# clock (UTC): a UTC stamp sits 2h behind the log in summer, so the verify window
# would include 2h of history and count stale initializations as fresh reloads.
STAMP=$(ssh -o BatchMode=yes "$HOST" "TZ=Europe/Copenhagen date '+%Y-%m-%d %H:%M:%S'")
LIST=$(mktemp)
ITEMS=$(mktemp)
git ls-files apps > "$LIST"
mapfile -t TRACKED < "$LIST"

# 3a. pre-deploy drift guard: every tracked file on the box should currently
# match either the last commit or the working tree we're about to send. A
# file matching neither means an earlier deploy (e.g. a stale sibling
# worktree) silently reverted it - undetected on the box 08-13 through 08-19
# until it caused a production incident. This never aborts: deploying is
# exactly what fixes drift. It only makes drift loud instead of silent.
if [ "${#TRACKED[@]}" -gt 0 ]; then
  BOXHASHES=$(printf '%s\n' "${TRACKED[@]}" | ssh -o BatchMode=yes "$HOST" '
    cd /data/appdaemon || exit 1
    while IFS= read -r f; do
      if [ -f "$f" ]; then sha256sum -- "$f"; fi
    done')
  WTHASHES=$(sha256sum -- "${TRACKED[@]}") || true
  HEADHASHES=""
  for f in "${TRACKED[@]}"; do
    hh=$(git show "HEAD:$f" 2>/dev/null | sha256sum) || continue
    HEADHASHES+="${hh%% *}  $f"$'\n'
  done

  declare -A BOXMAP WTMAP HEADMAP
  while read -r h p; do if [ -n "$p" ]; then BOXMAP["$p"]="$h"; fi; done <<< "$BOXHASHES"
  while read -r h p; do if [ -n "$p" ]; then WTMAP["$p"]="$h"; fi; done <<< "$WTHASHES"
  while read -r h p; do if [ -n "$p" ]; then HEADMAP["$p"]="$h"; fi; done <<< "$HEADHASHES"

  drifted=()
  for f in "${TRACKED[@]}"; do
    b="${BOXMAP[$f]:-}"
    if [ -z "$b" ]; then continue; fi  # not on the box yet: a new file, not drift
    if [ "$b" != "${WTMAP[$f]:-}" ] && [ "$b" != "${HEADMAP[$f]:-}" ]; then
      drifted+=("$f")
    fi
  done

  if [ "${#drifted[@]}" -gt 0 ]; then
    echo ""
    echo "=================================================================="
    echo "WARNING: box content drift detected - ${#drifted[@]} file(s) match"
    echo "         neither git HEAD nor the working tree about to be deployed:"
    for f in "${drifted[@]}"; do
      echo "  DRIFT: $f on box matches neither working tree nor HEAD"
    done
    echo "=================================================================="
    echo ""
  fi
fi

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

# post-deploy content verification: confirm the box now holds exactly the
# bytes we just sent, not just that rsync's own checksum agreed mid-transfer.
# Anything else touching /data/appdaemon between the sync and this check
# means the box isn't running what we think we shipped - the same class of
# silent failure behind the 08-13/08-19 drift incident. Called from every
# exit path below (this no-change short circuit, the crash-recovery success
# arm, and the normal flow-through at the end) so none of them can skip it.
verify_box_content() {
  if [ "${#TRACKED[@]}" -gt 0 ]; then
    BOXHASHES2=$(printf '%s\n' "${TRACKED[@]}" | ssh -o BatchMode=yes "$HOST" '
      cd /data/appdaemon || exit 1
      while IFS= read -r f; do
        if [ -f "$f" ]; then sha256sum -- "$f"; fi
      done')
    WTHASHES2=$(sha256sum -- "${TRACKED[@]}") || true

    declare -A BOXMAP2 WTMAP2
    while read -r h p; do if [ -n "$p" ]; then BOXMAP2["$p"]="$h"; fi; done <<< "$BOXHASHES2"
    while read -r h p; do if [ -n "$p" ]; then WTMAP2["$p"]="$h"; fi; done <<< "$WTHASHES2"

    mismatched=()
    for f in "${TRACKED[@]}"; do
      if [ "${BOXMAP2[$f]:-}" != "${WTMAP2[$f]:-}" ]; then
        mismatched+=("$f")
      fi
    done

    if [ "${#mismatched[@]}" -gt 0 ]; then
      echo "FAIL: ${#mismatched[@]} file(s) on the box do not match what was just deployed:"
      for f in "${mismatched[@]}"; do
        echo "  FAIL: $f"
      done
      exit 1
    fi
    echo "OK: post-deploy verification - ${#TRACKED[@]} file(s) confirmed on the box"
  fi
}

if [ "$CHANGED" -eq 0 ] && [ "$rc" -eq 0 ]; then
  echo "OK: no content changes - AppDaemon untouched, nothing to reload"
  verify_box_content
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
  verify_box_content
  exit 0
fi

if [ "${RELOADS:-0}" -eq 0 ]; then
  echo "WARNING: content changed but AppDaemon reloaded nothing. If you changed code/args,"
  echo "         something is off; if you changed only yaml comments, AD ignores it."
  echo "         Force with: ssh $HOST 'docker restart appdaemon'"
else
  echo "OK: $RELOADS app initialization(s) observed"
fi

# 5. post-deploy content verification (normal flow-through path; see
# verify_box_content above - also called from the no-change short circuit
# in step 4 and from the crash-recovery success arm above)
verify_box_content
