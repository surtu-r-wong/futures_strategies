#!/bin/bash
# Daily next-open targets for the term-structure index configuration.
# Run on the morning of T+1 after public.futures_daily has T's rows (03:10+).
# Usage: scripts/carry_tsindex_daily.sh <capital_cny> [as_of=YYYY-MM-DD]
#
# The run ends on the earliest max(trade_date) across the commodity exchanges
# (cta_carry.coverage), never on the overall max: DCE is delivered by hand and
# usually lands a day late, and a run past its coverage would read every DCE
# product as a signal exit. When an exchange lags, the targets are those of the
# last complete day and the log says so; re-run after the delivery.
set -u
cd "$(dirname "$0")/.." || exit 9
CAPITAL=${1:?capital in CNY required}
AS_OF=${2:-$(date +%F)}
mkdir -p output/targets
LOG=output/targets/daily.log
COVERAGE=$(mktemp)
END=$(.venv/bin/python -m cta_carry.coverage --as-of "$AS_OF" 2> "$COVERAGE")
RC=$?
cat "$COVERAGE" | tee -a "$LOG" >&2
rm -f "$COVERAGE"
if [ "$RC" -ne 0 ] || [ -z "$END" ]; then
  echo "[abort] $(date -Is) as_of=$AS_OF coverage check failed rc=$RC" | tee -a "$LOG" >&2
  exit 8
fi
START=$(date -d "$END -60 days" +%F)
PREFIX=output/targets/tsindex_$(echo "$END" | tr -d -)
echo "[start] $(date -Is) as_of=$AS_OF end=$END capital=$CAPITAL commit=$(git rev-parse --short HEAD)" >> "$LOG"
.venv/bin/python -m cta_carry --source public-pg --start "$START" --end "$END" \
  --near-leg near_dominant --weighting rank_linear --no-stop-loss --no-trend-filter \
  --carry-window 90 --liquidity-measure open_interest_value \
  --liquidity-window 20 --liquidity-threshold 2e9 \
  --exclude-products CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD \
  --missing-open-policy defer \
  --emit-next-targets --capital "$CAPITAL" \
  --output-prefix "$PREFIX" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[exit] $(date -Is) end=$END rc=$RC" >> "$LOG"
exit "$RC"
