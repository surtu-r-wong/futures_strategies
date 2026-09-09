#!/bin/bash
# Daily next-open targets for the term-structure index configuration.
# Run on the morning of T+1 after public.futures_daily has T's rows (03:10+).
# Usage: scripts/carry_tsindex_daily.sh <capital_cny> [end_date=YYYY-MM-DD]
set -u
cd "$(dirname "$0")/.." || exit 9
CAPITAL=${1:?capital in CNY required}
END=${2:-$(date +%F)}
START=$(date -d "$END -60 days" +%F)
mkdir -p output/targets
PREFIX=output/targets/tsindex_$(echo "$END" | tr -d -)
LOG=output/targets/daily.log
echo "[start] $(date -Is) end=$END capital=$CAPITAL commit=$(git rev-parse --short HEAD)" >> "$LOG"
.venv/bin/python -m cta_carry --source public-pg --start "$START" --end "$END" \
  --near-leg near_dominant --weighting rank_linear --no-stop-loss --no-trend-filter \
  --carry-window 90 --liquidity-measure open_interest_value \
  --liquidity-window 20 --liquidity-threshold 2e9 \
  --exclude-products CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD \
  --missing-open-policy defer \
  --emit-next-targets --capital "$CAPITAL" \
  --output-prefix "$PREFIX" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[exit] $(date -Is) rc=$RC" >> "$LOG"
exit "$RC"
