#!/bin/bash
# M02 v2 nightly advance — Debian crontab 21:05, one retry at 21:35.
# Same shape as m02_futures_nightly.sh (v1): flock, per-day log, a FAIL gets
# exactly one same-night retry. PASS/SKIP/SEALED exit 0, FAIL exit 2.
#
# Env:
#   M02_V2_STATE_DIR    required — account.sqlite / verdicts / logs (/ feed)
#   M02_V2_ACCOUNT      required — account id in that store
#   M02_V2_TARGETS      required — morning target packages root
#   M02_V2_FEED         optional — feed path (unset for the shadow: no export)
#   M02_V2_RETRY_AT     optional HHMM, default 2135
#   M02_V2_EXTRA_ARGS   optional extra driver args (--dry-run-only, --today ...)
set -u
umask 077
state_dir=${M02_V2_STATE_DIR:?M02_V2_STATE_DIR is required}
account=${M02_V2_ACCOUNT:?M02_V2_ACCOUNT is required}
targets=${M02_V2_TARGETS:?M02_V2_TARGETS is required}
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$state_dir/logs" "$state_dir/verdicts"
exec >>"$state_dir/logs/$(date +%F).log" 2>&1
exec 9>"$state_dir/nightly.lock"
flock -n 9 || { echo "[skip] $(date -Is) another instance holds the lock"; exit 0; }
if [ ! -f "$state_dir/account.sqlite" ]; then
  # the production line is installed before its genesis (11-13); until the
  # store exists there is nothing to advance
  echo "[skip] $(date -Is) no store at $state_dir/account.sqlite (pre-genesis)"
  exit 0
fi
feed=()
[ -n "${M02_V2_FEED:-}" ] && feed=(--feed "$M02_V2_FEED")

run() {
  # shellcheck disable=SC2086  # EXTRA_ARGS is intentionally word-split
  "$repo/.venv/bin/python" -B "$repo/scripts/m02_v2_nightly.py" \
    --state "$state_dir/account.sqlite" --account "$account" \
    --targets-root "$targets" --verdict-dir "$state_dir/verdicts" \
    "${feed[@]}" ${M02_V2_EXTRA_ARGS:-}
  local rc=$?
  echo "[run] $(date -Is) rc=$rc"
  return "$rc"
}

run
rc=$?
retry_at=${M02_V2_RETRY_AT:-2135}
if [ "$rc" -ne 0 ]; then
  target=$((10#${retry_at:0:2} * 60 + 10#${retry_at:2:2}))
  nowm=$((10#$(date +'%H') * 60 + 10#$(date +'%M')))
  if [ "$nowm" -lt "$target" ]; then
    echo "[retry] $(date -Is) scheduling one retry at $retry_at"
    sleep $(((target - nowm) * 60))
    run
    rc=$?
  fi
fi
echo "[exit] $(date -Is) rc=$rc"
exit "$rc"
