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
#
# The basis-momentum leg runs at weight 0.20 (user's call, 2026-09-11); window,
# coverage and cadence stay at their defaults (500 / 0.9 / monthly), which is
# the configuration the sensitivity sweep was run at.
#
# The run starts 900 days before the cutoff, not 60. With the leg off the sheet
# was window-invariant -- a 60-day and a 400-day start agreed to the last digit,
# because rank weights carry no path dependence. The leg breaks that twice over:
# its strict-history gate needs 500 traded days of chain returns before it emits
# anything, and `vol_scale` is 0.15 over the annualised vol of the last 252
# shadow returns, so the gate has to have been open across that whole window or
# the estimate mixes carry-only days with blended ones. A 60-day start left the
# gate shut until 57 traded days before the signal date, read the vol as 4.13%
# instead of 4.77% and over-levered the book by 15.6% -- 47 of 49 rows off, one
# by 42 lots -- the vol window held 57 blended days where it needed 252. A
# 900-day start opens the gate on 2024-03-05 and leaves 614 blended days behind
# the window; a 400-day start leaves only 30, which already reproduces the
# 900-day sheet lot for lot but has no room for a late-listing or a gap.
#
# `signals.bmom_ready` is still worth a look after a run: on a healthy day it is
# True for every product with enough listed history (44 of 49 on 2026-09-10, the
# five refusals being listings younger than 500 days). An all-False column means
# the gate starved and the blend silently fell back to carry alone.
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
START=$(date -d "$END -900 days" +%F)
PREFIX=output/targets/tsindex_$(echo "$END" | tr -d -)
echo "[start] $(date -Is) as_of=$AS_OF end=$END capital=$CAPITAL commit=$(git rev-parse --short HEAD)" >> "$LOG"
.venv/bin/python -m cta_carry --source public-pg --start "$START" --end "$END" \
  --near-leg near_dominant --weighting rank_linear --no-stop-loss --no-trend-filter \
  --carry-window 90 --liquidity-measure open_interest_value \
  --liquidity-window 20 --liquidity-threshold 2e9 \
  --exclude-products CU,BC,AL,AO,AD,ZN,PB,NI,SN,SS,AU,AG,PT,PD,EC,PK,CJ,AP,JD \
  --missing-open-policy defer \
  --basis-momentum-weight 0.20 \
  --emit-next-targets --capital "$CAPITAL" \
  --output-prefix "$PREFIX" 2>&1 | tee -a "$LOG"
RC=${PIPESTATUS[0]}
echo "[exit] $(date -Is) end=$END rc=$RC" >> "$LOG"
exit "$RC"
