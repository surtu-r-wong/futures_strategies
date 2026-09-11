#!/bin/bash
# Daily next-open targets for the CICSF027 basis-momentum index strategy.
# Run on the morning of T+1 after public.futures_daily has T's rows (03:10+).
# Usage: scripts/citic027_daily.sh <capital_cny> [as_of=YYYY-MM-DD]
#
# Same shape as scripts/carry_tsindex_daily.sh and the same coverage gate: the
# run ends on the earliest max(trade_date) across the five commodity exchanges,
# recomputed every time, never the overall max. DCE is delivered by hand and
# usually lands a day late, and a run past its coverage would read every DCE
# product as having no bar.
#
# The window reaches back 900 days. The volatility scale needs 252 index-return
# days, each needing weights from the day before, which need R=150 traded days
# of chain returns -- about 402 trading days at minimum against roughly 615 in
# 900 calendar days. The carry runner shipped with 60 days on 2026-09-11, read
# its volatility at 4.13% against a converged 4.77% and over-levered the book by
# 15.6% with nothing erroring. That failure mode is silent, so the margin here
# is deliberate and the run aborts rather than emitting an unsized book.
#
# After a run, check the sheet: scripts/check_citic027_sheet.py <prefix>
set -u
cd "$(dirname "$0")/.." || exit 9
CAPITAL=${1:?capital in CNY required}
AS_OF=${2:-$(date +%F)}
mkdir -p output/targets
LOG=output/targets/citic027_daily.log
COVERAGE=$(mktemp)
END=$(.venv/bin/python -m cta_carry.coverage --as-of "$AS_OF" 2> "$COVERAGE")
RC=$?
cat "$COVERAGE" | tee -a "$LOG" >&2
rm -f "$COVERAGE"
if [ "$RC" -ne 0 ] || [ -z "$END" ]; then
  echo "[abort] $(date -Is) as_of=$AS_OF coverage check failed rc=$RC" | tee -a "$LOG" >&2
  exit 8
fi
PREFIX=output/targets/citic027_$(echo "$END" | tr -d -)
echo "[start] $(date -Is) as_of=$AS_OF end=$END capital=$CAPITAL commit=$(git rev-parse --short HEAD)" >> "$LOG"
.venv/bin/python -m citic_index.daily \
  --capital "$CAPITAL" --end "$END" --output-prefix "$PREFIX" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[exit] $(date -Is) end=$END rc=$RC" >> "$LOG"
exit "$RC"
