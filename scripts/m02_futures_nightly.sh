#!/bin/bash
# M02 futures nightly advance — Debian crontab entry (2026-10-27 migration
# letter). Runs the gated catch-up driver scripts/m02_futures_nightly.py;
# a FAIL gets exactly one same-night retry (default 21:35, sleeping to the
# absolute minute like the fx-hkd-daily ring). PASS/SKIP exit 0, FAIL exit 2.
# Everything is logged under the state dir; the store, feed and verdicts all
# live there too, so release dirs stay immutable and rollback = flip symlink.
#
# Env:
#   M02_FUTURES_STATE_DIR  required — account.sqlite/feed/verdicts/logs home
#   M02_FUTURES_RETRY_AT   optional HHMM, default 2135; <= start minute = no retry
#   M02_FUTURES_EXTRA_ARGS optional extra driver args (rehearsal: --today ...)
set -u
umask 077
state_dir=${M02_FUTURES_STATE_DIR:?M02_FUTURES_STATE_DIR is required}
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$state_dir/logs" "$state_dir/verdicts" "$state_dir/feed"
exec >>"$state_dir/logs/$(date +%F).log" 2>&1
exec 9>"$state_dir/nightly.lock"
flock -n 9 || { echo "[skip] $(date -Is) another instance holds the lock"; exit 0; }

py="$repo/.venv/bin/python"

run() {
  # shellcheck disable=SC2086  # EXTRA_ARGS is intentionally word-split
  "$py" -B "$repo/scripts/m02_futures_nightly.py" \
    --state "$state_dir/account.sqlite" \
    --feed "$state_dir/feed/latest.json" \
    --verdict-dir "$state_dir/verdicts" \
    ${M02_FUTURES_EXTRA_ARGS:-}
  local rc=$?
  echo "[run] $(date -Is) rc=$rc"
  return "$rc"
}

run
rc=$?
retry_at=${M02_FUTURES_RETRY_AT:-2135}
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
