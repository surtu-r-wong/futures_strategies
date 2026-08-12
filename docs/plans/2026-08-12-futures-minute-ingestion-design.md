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
| 3 | Keep `volume = 0` bars | Dropping them makes "no trade this minute" indistinguishable from "archive lacks this range". Fidelity wins; the cost is disk, which decision 6 handles. **But see "Bar density is not uniform" — the vendor stops emitting these after 2024, so that distinction only holds for the older data.** |
| 4 | `bar_time` = `bob` (bar open), `timestamptz` | The archive carries `+08:00`; discarding the offset is lossy. Differs from `market_data_minute` on purpose. A bar stamped 09:00 covers `[09:00, 09:01)`. |
| 5 | CZCE keeps the 4-digit month code (`AP2605`) | 3-digit CZCE codes (`AP605`, as `futures_daily` stores them) collide across decades. 4-digit is unambiguous; the join rule to `futures_daily` is documented instead. |
| 6 | Repartition by **year** locally, then load year-by-year and compress those chunks immediately | Without it, peak uncompressed footprint is ~98 GB against 167 GB free. See "Why repartitioning is mandatory". Year buckets hold the peak to ~11 GB — ample against 167 GB — while keeping the output to 22 files, which one `mawk` process can route to without hitting a pipe limit. |
| 7 | Move the data by external drive, not over the network | The ThinkPad→Debian link measured 5 MB/s (Tailscale direct, but over WAN). Matches `SCHEMA_CHANGES.md` §D: files >500 MB go by drive. |
| 8 | Exclude vendor continuous series (`9999`/`9998`/`8888`/`0000`) | They appear only in the 2025+ daily packages, never in the 2005-2024 bulk, so admitting them yields a series that silently begins in 2025. The project generates its own continuous contracts under documented rules into `continuous_contract_ohlc`; a third vendor rule would only create ambiguity. Measured at ~3.7% of rows. |
| 9 | No global sort — per-file dedup only | A `(symbol, bar_time)` key never spans files: the 2005-2024 packages stop at 2024-12 and the 2025/2026 packages hold one symbol-day each. Verified. Timescale also reorders rows itself when building compressed batches, so pre-sorting buys nothing. This removes hours of external sort. |

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

`1m/2025/YYYYMM/YYYYMMDD/` and `1m/2026MM/YYYYMMDD/` are packed per symbol-day
and already align.

Repartitioning by year first makes every chunk finishable, holding peak
uncompressed footprint to a single year (~11 GB at the densest) instead of 98 GB.

---

## Pipeline

### Stage 1 — local repartition (ThinkPad)

Runs where the data already is, and keeps hours of parsing off the production DB
box (15 GB RAM, also running market-monitor collectors and sync-worker).

`scripts/futures_minute/stage1_repartition.sh` + `transform.awk`.

Every CSV is concatenated into **one** `mawk` process, which routes rows to
`<year>.tsv` and then zstd-compresses each. A single process is what makes
routing with `>` correct — awk truncates a redirect target once per process and
appends thereafter, so any batching (`xargs` splitting the file list) would
silently truncate earlier output.

Because the files arrive concatenated, file boundaries are recognised by the
header line (`/exchange,symbol,open,close/`) rather than by `FNR`. That also
tolerates the UTF-8 BOM the 2025 headers carry. The header match is what resets
the per-file dedup table, so it is load-bearing for memory: a missed header
would let that table grow across all 640M rows. The smoke test asserts
`files=` equals the input count for exactly this reason.

Per-file dedup is enough (decision 9); there is no sort step.

Source column order is `open,close,high,low` — not OHLC. Verified end-to-end
against a row with four distinct values.

Output: 22 zstd files, ~6–8 GiB, plus `manifest.tsv` (year, rows, raw bytes,
compressed bytes, sha256) for the Stage 3 row-count check and for verifying the
drive copy.

Memory: streaming throughout, bounded by one source file's dedup keys
(~250k at worst). Compare `pandas-pg-memory-pitfalls`.

### Stage 2 — load on Debian

Carry the Stage 1 output on an external drive. Keep it **on the drive** — do not
copy into Debian's internal disk, which is reserved for PG.

Apply `scripts/futures_minute/schema.sql` first, then per year:

```bash
# in tmux, so an ssh drop does not kill the run; window 15:00–21:00 (post-close)
for y in /mnt/usb/futures_minute/*.zst; do
  zstd -dc "$y" \
    | PGPASSWORD=... psql -h 127.0.0.1 -U admin -d market_monitor \
        -c "COPY public.futures_minute
              (bar_time, symbol, exchange, open, high, low, close,
               volume, amount, open_interest) FROM STDIN"
  # then compress that year's chunks before moving on, so the uncompressed
  # footprint never exceeds one year
done
```

`psql -h 127.0.0.1 -U admin` is required: the unix socket rejects `admin` under
peer auth (`对用户"admin"的对等认证失败`). Verified 2026-08-12.

Failure of any year is isolated —
`DELETE FROM public.futures_minute WHERE bar_time >= 'YYYY-01-01' AND bar_time < 'YYYY+1-01-01'`
and redo that year.

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

## Bar density is not uniform (measured 2026-08-12 on the Stage 1 output)

The vendor changed how it pads non-trading minutes partway through the archive:

| Year | rows with `volume = 0` |
|---|---|
| 2022 | 59.9% |
| 2023 | 56.1% |
| 2024 | 60.0% |
| 2025 | 36.0% |
| 2026 | 0.0% |

Consequences for anyone querying the table:

- The rationale for decision 3 — keeping `volume = 0` so "no trade" stays
  distinguishable from "no data" — holds only through 2024. From 2026 a missing
  minute is simply missing, with no signal either way.
- Bars-per-day is not a usable completeness check across the 2024/2025/2026
  boundaries, and forward-filling over them changes meaning.
- Row counts per year are not comparable: 2025 has 44.0M rows against 2024's
  63.0M despite similar market activity, because of the padding change alone.

This is recorded in the table's `COMMENT` so it reaches consumers who never read
this document.

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
