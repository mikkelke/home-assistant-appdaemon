#!/usr/bin/env bash
# Deploy AppDaemon apps to the HA box and verify the reload.
#
# Usage: scripts/deploy.sh
#   Deploys ALL git-tracked files under apps/ (rsync only sends content changes).
#   Requires origin/master to be an ancestor of HEAD and warns on unpushed commits.
#   Dirty tracked apps/ files require DEPLOY_ALLOW_DIRTY=1. Uncommitted box drift
#   requires DEPLOY_FORCE=1; box content matching recent commits is safe to update.
#
# Native production_mode must be true. The remote helper serializes cooperating
# deploys, journals pending work before transfer, verifies bytes, then signals
# SIGUSR2 and requires successful initialization of every affected app.
# No container restart is performed. Failed work remains pending for a retry.
set -euo pipefail

HOST="mke@10.21.0.5"

cd "$(git rev-parse --show-toplevel)"

# Refuse to overwrite changes from another checkout with an older tree.
if ! timeout 20 git fetch -q origin master; then
  echo "WARNING: could not fetch origin master within 20 seconds; using the existing origin/master ref. Fetch successfully before deploying if that ref may be stale." >&2
fi
if ! git merge-base --is-ancestor origin/master HEAD; then
  echo "FATAL: this tree lacks origin/master commits that are live on the box. Rebase on origin/master before deploying so those changes are preserved." >&2
  exit 1
fi
if ! git merge-base --is-ancestor HEAD origin/master; then
  echo "WARNING: HEAD has commits not on origin/master. Push before or right after deploying so other checkouts can include these changes." >&2
fi
if ! git diff --quiet -- apps/ || ! git diff --cached --quiet -- apps/; then
  if [ "${DEPLOY_ALLOW_DIRTY:-}" != "1" ]; then
    echo "FATAL: tracked files under apps/ have staged or unstaged changes. Commit them before deploying so other checkouts can preserve your work, or set DEPLOY_ALLOW_DIRTY=1 to deploy these uncommitted changes." >&2
    exit 1
  fi
  echo "WARNING: deploying uncommitted apps/ changes because DEPLOY_ALLOW_DIRTY=1. Commit and push them so other checkouts can preserve your work." >&2
fi

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
python3 -m unittest discover -s scripts/tests -p 'test_appdaemon_deploy.py' -q
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

# 3. Build an immutable byte manifest before taking the remote deploy lock.
LIST=$(mktemp)
ITEMS=$(mktemp)
MANIFEST=$(mktemp)
cleanup() {
  if [ -n "${CONTROL_IN:-}" ]; then exec {CONTROL_IN}>&-; fi
  if [ -n "${CONTROL_OUT:-}" ]; then exec {CONTROL_OUT}<&-; fi
  rm -f "$LIST" "$ITEMS" "$MANIFEST"
}
trap cleanup EXIT
git ls-files apps > "$LIST"
mapfile -t TRACKED < "$LIST"
python3 scripts/appdaemon_deploy.py --manifest > "$MANIFEST"

# 3a. pre-deploy drift guard: tracked box files must match the working tree,
# HEAD, or a recent commit. Other content may be someone's uncommitted work;
# abort before overwriting it unless DEPLOY_FORCE=1 explicitly allows it.
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

  # A box file that merely lags HEAD (matches an older commit of that file) is the NORMAL
  # pre-deploy state, not drift - warning on it every deploy would teach everyone to ignore
  # the block. Second pass, drifted candidates only: match the box hash against the file's
  # last 20 historical blobs; a hit downgrades to one quiet "behind" line. Only content
  # matching NO recent commit blocks the deploy unless DEPLOY_FORCE=1.
  behind=()
  true_drift=()
  for f in "${drifted[@]}"; do
    match=""
    for c in $(git log -20 --format=%H -- "$f"); do
      hh=$(git show "${c}:${f}" 2>/dev/null | sha256sum) || continue
      if [ "${hh%% *}" = "${BOXMAP[$f]}" ]; then match="$c"; break; fi
    done
    if [ -n "$match" ]; then
      behind+=("$f ($(git rev-parse --short "$match"))")
    else
      true_drift+=("$f")
    fi
  done

  if [ "${#behind[@]}" -gt 0 ]; then
    echo "box behind (older commit content, will be updated): ${#behind[@]} file(s)"
    for f in "${behind[@]}"; do
      echo "  behind: $f"
    done
  fi
  if [ "${#true_drift[@]}" -gt 0 ]; then
    if [ "${DEPLOY_FORCE:-}" = "1" ]; then
      echo "WARNING: box content drift detected in ${#true_drift[@]} file(s), matching neither this tree, HEAD nor recent commits. Overwriting because DEPLOY_FORCE=1; get the live uncommitted work committed and rebase to preserve it." >&2
    else
      echo "FATAL: box content drift detected in ${#true_drift[@]} file(s), matching neither this tree, HEAD nor recent commits. Someone's uncommitted work is live; get it committed and rebase before deploying, or set DEPLOY_FORCE=1 to overwrite it." >&2
    fi
    for f in "${true_drift[@]}"; do
      echo "  DRIFT: $f" >&2
    done
    if [ "${DEPLOY_FORCE:-}" != "1" ]; then exit 1; fi
  fi
fi

# Keep one SSH process alive across transfer, holding the on-box flock. The
# helper source is sent on stdin, so it needs no installation and carries no
# private config. Its next stdin line is the immutable tracked-byte manifest.
BOOT='import base64,sys; exec(compile(base64.b64decode(sys.stdin.readline()), "appdaemon_deploy.py", "exec"))'
coproc DEPLOY { ssh -o BatchMode=yes "$HOST" "docker exec -i appdaemon python -u -c '$BOOT'"; }
HELPER_PID=$DEPLOY_PID
exec {CONTROL_IN}>&"${DEPLOY[1]}"
exec {CONTROL_OUT}<&"${DEPLOY[0]}"
COPROC_IN=${DEPLOY[1]}
COPROC_OUT=${DEPLOY[0]}
exec {COPROC_IN}>&-
exec {COPROC_OUT}<&-
{ base64 -w0 scripts/appdaemon_deploy.py; printf '\n'; cat "$MANIFEST"; } >&"$CONTROL_IN"
if ! IFS= read -r -u "$CONTROL_OUT" READY; then
  echo "FAIL: deploy helper exited before transfer" >&2
  wait "$HELPER_PID" || true
  exit 1
fi
python3 -c 'import json,sys; d=json.loads(sys.argv[1]); print("Deploy lock acquired (pending retry)" if d.get("retry") else "Deploy lock acquired") if d.get("status")=="ready" else sys.exit("FAIL: "+d.get("error","unexpected deploy response"))' "$READY"

rc=0
rsync -rlz --checksum --delay-updates --no-perms --no-owner --no-group \
  --out-format='%i %n' --files-from="$LIST" . "$HOST:/data/appdaemon/" >"$ITEMS" || rc=$?
CHANGED=$(grep -c '^[<>]f' "$ITEMS" || true)
sed -n 's/^[<>]f[^ ]* /  changed: /p' "$ITEMS"
echo "synced (tracked files only): $CHANGED file(s) changed on the box"
# Every nonzero rsync result is a failure, including exit 23. The helper does
# not signal until transfer succeeded AND every manifest digest matches.
printf '{"returncode":%s}\n' "$rc" >&"$CONTROL_IN"
if ! IFS= read -r -u "$CONTROL_OUT" RESULT; then
  echo "FAIL: deploy helper disconnected; pending work will be retried" >&2
  wait "$HELPER_PID" || true
  exit 1
fi
helper_rc=0
wait "$HELPER_PID" || helper_rc=$?
python3 -c 'import json,sys; d=json.loads(sys.argv[1]);
if d.get("status")!="ok": sys.exit("FAIL: "+d.get("error","unexpected deploy response"))
print("OK: post-deploy content verified (%s files)" % d["verified"])
print("OK: successful initialization: "+", ".join(d["initialized"]) if d["reload"] else "OK: no functional app changes; no reload required")' "$RESULT"
exit "$helper_rc"
