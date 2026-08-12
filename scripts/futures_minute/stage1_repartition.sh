#!/usr/bin/env bash
#
# Stage 1 of the futures_minute ingestion: turn 期货数据/1m into one
# COPY-ready zstd file per year, ready to be carried to the Debian primary.
#
# Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
#
#   SAMPLE=2000 ./stage1_repartition.sh     # smoke test on 2000 source files
#   ./stage1_repartition.sh                 # full run (~392,586 files, 73 GiB)
#
# Everything streams: one mawk process reads every CSV concatenated together
# and routes rows to per-year files. Nothing is held in memory beyond one
# source file's dedup keys.

set -euo pipefail

HERE=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
ARCHIVE=${ARCHIVE:-/home/elfbob/claude-code/futures_strategies/期货数据/1m}
OUT=${OUT:-/home/elfbob/claude-code/futures_strategies/output/futures_minute_stage1}
ZSTD_LEVEL=${ZSTD_LEVEL:-3}
SAMPLE=${SAMPLE:-0}

RAW="$OUT/raw"
LOG="$OUT/stage1.log"
MANIFEST="$OUT/manifest.tsv"

for tool in mawk zstd; do
    command -v "$tool" >/dev/null || { echo "missing: $tool" >&2; exit 1; }
done
[ -d "$ARCHIVE" ] || { echo "no archive at $ARCHIVE" >&2; exit 1; }

# The raw pass writes ~55 GiB (the emitted rows drop eob/type/sequence).
avail=$(df -BG --output=avail "$(dirname "$OUT")" | tail -1 | tr -dc '0-9')
need=$([ "$SAMPLE" -gt 0 ] && echo 2 || echo 80)
if [ "$avail" -lt "$need" ]; then
    echo "need ~${need}G free, have ${avail}G" >&2
    exit 1
fi

rm -rf "$RAW"
mkdir -p "$RAW"
: > "$LOG"

echo "[$(date +%T)] listing source files ..." | tee -a "$LOG"
LIST="$OUT/files.lst"
find "$ARCHIVE" -type f -iname '*.csv' | sort > "$LIST"
if [ "$SAMPLE" -gt 0 ]; then
    # Spread the sample across packages so the bulk, the 2025 daily files and
    # the 2026 daily files are all represented.
    awk -v n="$SAMPLE" -v total="$(wc -l < "$LIST")" \
        'NR % int((total/n)+1) == 0' "$LIST" > "$LIST.sample"
    mv "$LIST.sample" "$LIST"
fi
echo "[$(date +%T)] $(wc -l < "$LIST") files to read" | tee -a "$LOG"

echo "[$(date +%T)] transforming ..." | tee -a "$LOG"
# One mawk process for the whole corpus: see transform.awk on why that matters.
tr '\n' '\0' < "$LIST" | xargs -0 cat \
    | mawk -v OUTDIR="$RAW" -f "$HERE/transform.awk" 2>> "$LOG"

echo "[$(date +%T)] compressing ..." | tee -a "$LOG"
printf 'year\trows\traw_bytes\tzst_bytes\tsha256\n' > "$MANIFEST"
for f in "$RAW"/*.tsv; do
    year=$(basename "$f" .tsv)
    rows=$(wc -l < "$f")
    raw=$(stat -c %s "$f")
    zstd -q -"$ZSTD_LEVEL" -T0 -f "$f" -o "$OUT/$year.zst"
    zst=$(stat -c %s "$OUT/$year.zst")
    sha=$(sha256sum "$OUT/$year.zst" | cut -d' ' -f1)
    printf '%s\t%s\t%s\t%s\t%s\n' "$year" "$rows" "$raw" "$zst" "$sha" >> "$MANIFEST"
    echo "[$(date +%T)]   $year  $rows rows  $((raw/1024/1024)) MiB -> $((zst/1024/1024)) MiB" | tee -a "$LOG"
    rm -f "$f"
done
rmdir "$RAW"

echo "[$(date +%T)] done. manifest:" | tee -a "$LOG"
column -t "$MANIFEST" | tee -a "$LOG"
awk -F'\t' 'NR>1 {r+=$2; z+=$4} END {printf "TOTAL %d rows, %.1f GiB compressed\n", r, z/1024/1024/1024}' \
    "$MANIFEST" | tee -a "$LOG"
