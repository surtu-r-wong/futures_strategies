# Futures Minute Archive Ingestion — Design

**Status:** design validated 2026-08-12, implementation not started.

**Goal:** Load the local minute-bar archive (`期货数据/1m`, 392,586 CSVs / 73.2 GiB /
~640M rows / 2005 → 2026-08) into a new TimescaleDB hypertable
`public.futures_minute` on the Debian primary, so that futures_strategies,
spread_analyzer, backtest_system and the dashboards can all query it.

**Non-goal:** the daily archive (`期货数据/1d`). Daily gaps stay with the Wind EOD
backfill; the daily comparison run on 2026-08-12 was validation only. See
"Prior validation" below.

---

## Decisions

| # | Decision | Rationale |
|---|---|---|
| 1 | New table `public.futures_minute`; leave `market_data_minute` untouched | Different semantics — `market_data_minute` is a Wind realtime-snapshot table (`last_price`, no close/OI/amount, `timestamp without time zone`, 2026-01-22→ only). Coexistence is zero-risk; the overlapping 2026 window becomes a free cross-check. |
| 2 | Not on the sync chain | `market_data_minute` sets the precedent: a `public` table absent from sync-worker `config.yaml` is ignored. No Pi5 impact, no `sync_state` row, no config change. |
| 3 | Keep `volume = 0` bars | Dropping them makes "no trade this minute" indistinguishable from "archive lacks this range". Fidelity wins; the cost is disk, which decision 6 handles. |
| 4 | `bar_time` = `bob` (bar open), `timestamptz` | The archive carries `+08:00`; discarding the offset is lossy. Differs from `market_data_minute` on purpose. A bar stamped 09:00 covers `[09:00, 09:01)`. |
| 5 | CZCE keeps the 4-digit month code (`AP2605`) | 3-digit CZCE codes (`AP605`, as `futures_daily` stores them) collide across decades. 4-digit is unambiguous; the join rule to `futures_daily` is documented instead. |
| 6 | Repartition by month locally, then load month-by-month and compress each chunk immediately | Without it, peak uncompressed footprint is ~98 GB against 167 GB free. See "Why repartitioning is mandatory". |
| 7 | Move the data by external drive, not over the network | The ThinkPad→Debian link measured 5 MB/s (Tailscale direct, but over WAN). Matches `SCHEMA_CHANGES.md` §D: files >500 MB go by drive. |

---

## Schema

```sql
CREATE TABLE public.futures_minute (
    bar_time      timestamptz      NOT NULL,   -- bob, bar open, Asia/Shanghai
    symbol        text             NOT NULL,   -- uppercase, 4-digit month code
    exchange      text             NOT NULL,   -- SHFE/DCE/CZCE/CFFEX/INE/GFEX
    open          double precision,
    high          double precision,
    low           double precision,
    close         double precision,
    volume        double precision,
    amount        double precision,            -- 成交额
    open_interest double precision,            -- archive column `position`
    PRIMARY KEY (symbol, bar_time)
);

SELECT create_hypertable('public.futures_minute', 'bar_time',
                         chunk_time_interval => INTERVAL '1 month');

ALTER TABLE public.futures_minute SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'bar_time'
);
```

No `add_compression_policy` — chunks are compressed explicitly as each month
lands (decision 6). A policy can be added afterwards for future appends.

Sizing: ~640M rows ≈ 72 GB heap + 26 GB PK index uncompressed; expected
8–12 GB after compression. Debian has 167 GB free of 221 GB, 15 GB RAM,
PostgreSQL 15.17, TimescaleDB 2.23.1.

---

## Why repartitioning is mandatory

`1m/2005-2024/<exchange>/<product>.zip` packs **one contract's entire life** per
file — `LC2409.csv` spans 2023-09 → 2024-09. Loading file-by-file writes into a
dozen-plus monthly chunks at once, so no chunk is ever "finished" and nothing can
be compressed until the whole load ends. That path peaks at ~98 GB uncompressed
against 167 GB free, before compression's own working space.

`1m/2025/YYYYMM/` and `1m/2026MM/` are packed per-day and already align.

Repartitioning by month first makes every chunk finishable, holding peak
uncompressed footprint to a single month (~0.5–1 GB).

---

## Pipeline

### Stage 1 — local repartition (ThinkPad)

Runs where the data already is, and keeps hours of parsing off the production DB
box (15 GB RAM, also running market-monitor collectors and sync-worker).

1. Stream all 392,586 CSVs. Match `*.[cC][sS][vV]` — 85,256 files in
   `1m/2025/202503`–`202507` use uppercase `.CSV`.
2. Normalise: uppercase symbol, carry `exchange`, `bob` → `bar_time`,
   `position` → `open_interest`.
3. Bucket rows by month.
4. Per bucket: `LC_ALL=C sort` (external, bounded memory) on `(symbol, bar_time)`,
   then drop duplicate keys keeping the first row — O(1) memory once sorted.
   The daily archive had 174 files with every row duplicated exactly twice; the
   minute archive must be assumed to share the defect.
5. Write zstd-compressed COPY-format text, one file per month (~260 files,
   8–12 GiB total). Sorting also matches `compress_segmentby`/`compress_orderby`,
   improving both load speed and compression ratio.
6. Emit a manifest (per source file: rows read, rows kept, target months) for
   resumability and for the Stage 3 row-count check.

Memory discipline per `pandas-pg-memory-pitfalls`: streaming only, never a whole
year in memory, `RLIMIT_AS` cap, staged RSS logging.

### Stage 2 — load on Debian

Carry the Stage 1 output on an external drive. Keep it **on the drive** — do not
copy into Debian's internal disk, which is reserved for PG.

```bash
# in tmux, so an ssh drop does not kill the run; window 15:00–21:00 (post-close)
for m in /mnt/usb/futures_minute/*.zst; do
  zstd -dc "$m" \
    | PGPASSWORD=... psql -h 127.0.0.1 -U admin -d market_monitor \
        -c "COPY public.futures_minute FROM STDIN"
  # then compress that month's chunk before moving on
done
```

`psql -h 127.0.0.1 -U admin` is required: the unix socket rejects `admin` under
peer auth. Verified 2026-08-12.

Failure of any month is isolated —
`DELETE FROM public.futures_minute WHERE bar_time >= $m AND bar_time < $m+1month`
and redo that month.

Estimated: ~10 min to copy the drive, 60–90 min of COPY, plus per-chunk
compression.

### Stage 3 — verification

1. Row counts per month against the Stage 1 manifest.
2. Roll minute bars up to daily (`time_bucket('1 day', bar_time)`, first/max/min/last,
   sum volume/amount) and compare against `public.futures_daily` on the overlapping
   contract-days — the same comparison harness already used for the daily archive.
   Expect agreement in line with the daily result (OHLC 99.88–99.99% exact).
3. Cross-check the 2026-01-22 → 2026-08-11 overlap against `market_data_minute`.

---

## Operational guardrails

- Not a synced object — confirmed absent from `sync_state`. No two-end DDL, no
  `config.yaml` edit, no Pi5 work. Re-confirm with
  `SELECT * FROM sync_state WHERE schema_name='public' AND table_name='futures_minute'`
  before starting (must return no rows).
- `CREATE TABLE` in `public` is the only DDL. Nothing existing is altered or
  dropped, so no pre-dump of existing data is required.
- Watch `df -h /` on Debian between months.
- Run post-close (15:00–21:00); collectors write to the same instance.

---

## Prior validation (2026-08-12)

The daily archive was compared against `public.futures_daily` in full before this
design. Findings that bear on the minute work:

- Value agreement is high: OHLC 99.88–99.99% exactly equal over 1.53M
  contract-days; the only >1% close differences (66) are rows where the database
  holds `close = 0` on 2026-01-05.
- `amount` (archive) vs `turnover` (database) agree only 58.7% exactly, 98.0%
  within 1e-3 — a precision/convention difference. Do not use them for strict
  equality checks in Stage 3.
- 174 of 5,243 daily files contain every row exactly twice. Stage 1 step 4 exists
  because of this.
- `futures_daily` is missing six whole trading days inside its own range —
  2026-03-03, 03-06, 03-09, 03-10, 03-11, 03-12 — separate from the post-04-29
  freeze. Relevant to the Wind backfill range, not to this design.
