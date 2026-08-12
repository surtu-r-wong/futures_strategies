#!/usr/bin/env bash
#
# Stage 2 of the futures_minute ingestion: load the per-year files produced by
# stage1_repartition.sh into public.futures_minute on the Debian primary.
#
# Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
#
# Run ON the Debian box, inside tmux, in the post-close window (15:00-21:00):
#
#   export PGPASSWORD=...
#   ./stage2_load.sh "/media/.../futures_minute_stage1"
#
# Years load in order and each year's chunks are compressed as soon as that year
# lands, which is what holds the uncompressed footprint to one year (~11 GB)
# instead of ~98 GB for the whole table.
#
# Resumable: a year whose row count already matches the manifest is skipped, so
# an interrupted run can simply be restarted.

set -euo pipefail

DATA=${1:?usage: stage2_load.sh <dir with YYYY.zst + manifest.tsv>}
PGHOST_=${PGHOST_:-127.0.0.1}   # TCP, not the unix socket: peer auth rejects admin
PGUSER_=${PGUSER_:-admin}
PGDB_=${PGDB_:-market_monitor}

: "${PGPASSWORD:?export PGPASSWORD before running}"
[ -f "$DATA/manifest.tsv" ] || { echo "no manifest.tsv in $DATA" >&2; exit 1; }

psql_() { psql -h "$PGHOST_" -U "$PGUSER_" -d "$PGDB_" -v ON_ERROR_STOP=1 "$@"; }

echo "[$(date +%T)] target: $PGUSER_@$PGHOST_/$PGDB_"
psql_ -At -c "SELECT 'table present: ' || to_regclass('public.futures_minute')"

total_loaded=0
# Read the manifest on fd 3, not stdin: the loop body runs psql, and one of
# those calls is a COPY ... FROM STDIN. Keeping the two streams apart means a
# stray reader can never eat the manifest out from under the loop.
while IFS=$'\t' read -r year rows raw zst sha <&3; do
    [ "$year" = "year" ] && continue
    file="$DATA/$year.zst"
    [ -f "$file" ] || { echo "  !! missing $file" >&2; exit 1; }

    have=$(psql_ -At -c "SELECT count(*) FROM public.futures_minute
                         WHERE bar_time >= '$year-01-01'::timestamptz
                           AND bar_time <  '$((year+1))-01-01'::timestamptz")
    if [ "$have" = "$rows" ]; then
        echo "[$(date +%T)] $year already loaded ($rows rows), skipping"
        total_loaded=$((total_loaded + have))
        continue
    fi
    if [ "$have" != "0" ]; then
        echo "[$(date +%T)] $year partial ($have of $rows) — clearing and reloading"
        psql_ -q -c "DELETE FROM public.futures_minute
                     WHERE bar_time >= '$year-01-01'::timestamptz
                       AND bar_time <  '$((year+1))-01-01'::timestamptz"
    fi

    echo "[$(date +%T)] $year verifying checksum ..."
    actual=$(sha256sum "$file" | cut -d' ' -f1)
    [ "$actual" = "$sha" ] || { echo "  !! checksum mismatch on $file" >&2; exit 1; }

    echo "[$(date +%T)] $year loading $rows rows ..."
    zstd -dc "$file" | psql_ -q -c "COPY public.futures_minute
        (bar_time, symbol, exchange, open, high, low, close,
         volume, amount, open_interest) FROM STDIN"

    got=$(psql_ -At -c "SELECT count(*) FROM public.futures_minute
                        WHERE bar_time >= '$year-01-01'::timestamptz
                          AND bar_time <  '$((year+1))-01-01'::timestamptz")
    [ "$got" = "$rows" ] || { echo "  !! $year loaded $got, expected $rows" >&2; exit 1; }

    echo "[$(date +%T)] $year compressing chunks ..."
    psql_ -At -c "SELECT count(compress_chunk(c, if_not_compressed => true))
                  FROM show_chunks('public.futures_minute',
                                   newer_than => '$year-01-01'::timestamptz,
                                   older_than => '$((year+1))-01-01'::timestamptz) c"

    total_loaded=$((total_loaded + got))
    echo "[$(date +%T)] $year done — $total_loaded rows so far; disk: $(df -h / | tail -1 | awk '{print $4}') free"
done 3< "$DATA/manifest.tsv"

echo "[$(date +%T)] all years loaded: $total_loaded rows"
psql_ -c "SELECT pg_size_pretty(hypertable_size('public.futures_minute')) AS size,
                 count(*) AS chunks
          FROM timescaledb_information.chunks
          WHERE hypertable_name = 'futures_minute'"
