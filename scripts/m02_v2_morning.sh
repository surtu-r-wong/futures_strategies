#!/bin/bash
# M02 v2 morning target package — Debian crontab, 08:30 on weekdays.
# Produces <targets>/<T>/ for today's trading date T with the native 027
# PRODUCTION calculation (forward_inputs.m02_daily). A package that is not
# `ready` (e.g. the five-exchange frontier has not reached the signal day)
# gets one retry at M02_V2_RETRY_AT (default 0905), still well before 09:30;
# a later package would land after the execution time and the nightly would
# record target_missing anyway. Not a trading day -> exit 0, nothing written.
#
# Env:
#   M02_V2_ROOT       required — holds targets/, logs/ (shared by shadow+prod)
#   M02_V2_CALENDAR   required — calendar JSON covering today (signal date source)
#   M02_V2_STORE / M02_V2_ACCOUNT  optional — held contracts for the expiry precheck
#   M02_V2_RETRY_AT   optional HHMM, default 0905; <= start minute = no retry
#   M02_V2_TODAY      optional override (rehearsal only)
set -u
umask 077
root=${M02_V2_ROOT:?M02_V2_ROOT is required}
calendar=${M02_V2_CALENDAR:?M02_V2_CALENDAR is required}
repo=$(cd "$(dirname "$0")/.." && pwd)
mkdir -p "$root/logs" "$root/targets"
exec >>"$root/logs/morning-$(date +%F).log" 2>&1
exec 9>"$root/morning.lock"
flock -n 9 || { echo "[skip] $(date -Is) another morning run holds the lock"; exit 0; }
today=${M02_V2_TODAY:-$(date +%F)}
extra=()
if [ -n "${M02_V2_STORE:-}" ] && [ -f "$M02_V2_STORE" ]; then
  extra=(--store "$M02_V2_STORE" --account "${M02_V2_ACCOUNT:?M02_V2_ACCOUNT with M02_V2_STORE}")
fi

run() {
  # 2 GB address-space cap (native window + panel peak ~0.75 GB RSS); one
  # BLAS thread so the run never competes with the PG backend on this box.
  ( ulimit -v 2000000
    OPENBLAS_NUM_THREADS=1 OMP_NUM_THREADS=1 MKL_NUM_THREADS=1 \
      "$repo/.venv/bin/python" -B -m forward_inputs.m02_daily \
      --trade-date "$today" --calendar "$calendar" \
      --output-root "$root/targets" "${extra[@]}" )
  local rc=$?
  echo "[run] $(date -Is) trade_date=$today rc=$rc"
  return "$rc"
}

cd "$repo" || exit 9
run
rc=$?
retry_at=${M02_V2_RETRY_AT:-0905}
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
