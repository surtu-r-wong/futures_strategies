-- Stage 3 of the futures_minute ingestion: verify the loaded table.
-- Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
--
--   PGPASSWORD=... psql -h 127.0.0.1 -U admin -d market_monitor -f stage3_verify.sql
--
-- Read-only.
--
-- ⚠️ RESOURCE DISCIPLINE — READ BEFORE EDITING ⚠️
--
-- The first version of this script OOM-killed the whole PostgreSQL cluster on
-- 2026-08-12 (Mem peak 14.7G + 12.2G swap on a 15 GB box, market_monitor down
-- until restarted by hand). It did two things wrong, both of which this version
-- must never reintroduce:
--
--   1. It ran `SELECT ... count(*) FROM public.futures_minute GROUP BY year` —
--      a full scan of 661M rows across 264 compressed chunks. TimescaleDB
--      decompresses each chunk as it scans, so peak memory is far above
--      work_mem and grows with the table, not with the result.
--   2. It raised work_mem to 256MB and allowed 4 parallel workers, multiplying
--      that footprint on a box that also runs the market-monitor collectors.
--
-- The rules that follow from that:
--   * Never aggregate the whole hypertable. Bound every query by symbol or by a
--     short time range so it uses the PK index.
--   * Never raise work_mem here. Lower parallelism instead.
--   * Prefer sampling: a few hundred well-chosen contract-days will expose a
--     wrong column mapping, a timezone offset, or a bad join just as surely as
--     661M rows will, at a millionth of the cost.
--   * Per-year row counts are already verified by stage2_load.sh against the
--     manifest after every COPY. Do not re-count them here.
--
-- ⚠️ AND THE RULE THE SECOND VERSION HAD TO LEARN (2026-08-13) ⚠️
--
-- "Bound it by symbol" is not about what you write in the WHERE clause, it is
-- about the plan you get. The second version joined on
--
--     CASE WHEN m.exchange = 'CZCE' THEN regexp_replace(m.symbol, ...) END = s.contract
--
-- and the planner cannot invert an expression over m.symbol back into an index
-- qual. It produced a Hash Join over an Append of all 264 chunks
-- (rows=661619348) with the bar_time bounds demoted to a post-join Join Filter:
-- a full decompressing scan wearing a WHERE clause. Only statement_timeout
-- stopped it. Normalising on the *small* side instead — computing the minute
-- symbol from futures_daily and joining on the bare m.symbol column — turns the
-- same query into a Nested Loop of index probes: 20 contract-days went from
-- >300 s (unfinished) to 284 ms.
--
-- So: keep every join key on futures_minute a BARE COLUMN, drive the loop from a
-- small temp table, and use literal time bounds where you can so chunk exclusion
-- happens at plan time. EXPLAIN anything new before you run it — a correlated
-- NOT EXISTS against this table still plans as an anti-join over all 264 chunks
-- and will sit there until the timeout fires.

\timing on
\set ON_ERROR_STOP on

-- Deliberately restrictive: no parallel workers, default work_mem, and a hard
-- ceiling so a mistake times out instead of taking the cluster with it.
SET max_parallel_workers_per_gather = 0;
SET statement_timeout = '300s';
SET work_mem = '32MB';

\echo ''
\echo '=== 1. 规模与区间（走 chunk 元数据，不扫表）==='
-- Exact row counts for compressed chunks come from the compression catalog, so
-- this stays metadata-only. Any uncompressed chunk is excluded from
-- `rows_exact` and shows up in `uncompressed` — see §1b.
SELECT (SELECT approximate_row_count('public.futures_minute'))                AS approx_rows,
       (SELECT count(*) FROM timescaledb_information.chunks
         WHERE hypertable_name = 'futures_minute')                            AS chunks,
       (SELECT count(*) FROM timescaledb_information.chunks
         WHERE hypertable_name = 'futures_minute' AND is_compressed)          AS compressed,
       (SELECT sum(ccs.numrows_pre_compression)
          FROM _timescaledb_catalog.chunk c
          JOIN _timescaledb_catalog.hypertable h ON h.id = c.hypertable_id
          JOIN _timescaledb_catalog.compression_chunk_size ccs ON ccs.chunk_id = c.id
         WHERE h.schema_name = 'public' AND h.table_name = 'futures_minute')  AS rows_exact_compressed,
       pg_size_pretty(hypertable_size('public.futures_minute'))               AS size;

\echo ''
\echo '=== 1b. 未压缩的 chunk（应为 0；非 0 说明 stage 2 的压缩有洞）==='
-- chunk_time_interval is INTERVAL '1 month', which TimescaleDB stores as a fixed
-- 30-day window on a timestamptz column — chunks do NOT align to calendar years.
-- stage2_load.sh compresses with show_chunks(newer_than => Y-01-01,
-- older_than => Y+1-01-01), which only returns chunks wholly inside that year, so
-- the chunk straddling each New Year is skipped by both neighbouring years.
-- Left unfixed that is 22 chunks and ~5.6 GB of uncompressed heap.
SELECT count(*) AS uncompressed,
       pg_size_pretty(coalesce(sum(pg_relation_size(cl.oid)), 0)) AS heap
FROM timescaledb_information.chunks ch
JOIN pg_class cl     ON cl.relname = ch.chunk_name
JOIN pg_namespace ns ON ns.oid = cl.relnamespace AND ns.nspname = ch.chunk_schema
WHERE ch.hypertable_name = 'futures_minute' AND NOT ch.is_compressed;

\echo ''
\echo '=== 2. 首尾 K 线（索引扫描）==='
SELECT (SELECT min(bar_time) FROM public.futures_minute) AS first_bar,
       (SELECT max(bar_time) FROM public.futures_minute) AS last_bar;

\echo ''
\echo '=== 3. 交易日历：夜盘属于下一个交易日，而不是下一个自然日 ==='
-- The night session of trade date T runs 21:00-02:30 on the evening of the
-- PREVIOUS TRADING DAY, so Monday's night session happens on Friday. Only 79% of
-- trading-day gaps are one calendar day (2015+: 2161 of 2743), so `trade_date - 1`
-- would silently drop the night session for every Monday and every post-holiday
-- session, producing a wall of false mismatches in §5.
CREATE TEMP TABLE _cal AS
SELECT trade_date,
       lag(trade_date) OVER (ORDER BY trade_date) AS prev_trade_date
FROM (SELECT DISTINCT trade_date FROM public.futures_daily) d;
CREATE INDEX ON _cal (trade_date);
ANALYZE _cal;

SELECT count(*) AS trading_days,
       count(*) FILTER (WHERE trade_date - prev_trade_date > 1) AS days_after_a_break
FROM _cal WHERE prev_trade_date IS NOT NULL;

\echo ''
\echo '=== 4. 抽样：按 交易所 × 五年档 分层，各取 12 个 contract-day ==='
-- Stratified rather than uniform: a plain random draw over futures_daily is 68%
-- SHFE+DCE and heavily weighted to recent years, which would leave GFEX (~1% of
-- rows) and the pre-2010 era with almost no samples. Errors here are systematic
-- (a column swapped, a timezone off, a code mapped wrong), so coverage of the
-- corners matters more than sample size.
--
-- futures_daily writes CZCE months with 3 digits (AP605.CZC) while this table
-- uses 4 (AP2605) — and futures_daily ALSO carries a 4-digit CZCE form from 2004
-- to 2017-01-16 (106,792 rows alongside 368,119 3-digit ones). So the mapping is
-- 3-digit -> 4-digit only, and the decade digit is recovered from the trade date:
-- k = (ydigit - year mod 10) mod 10 is how many years ahead the contract expires,
-- which is 0..2 in practice. Contracts implying k > 3 are dropped as unmappable.
--
-- Non-standard codes (L2602F.DCE, SC2003TAS.INE — TAS/spread instruments, 5,709
-- rows) are excluded: they have no counterpart in the minute archive and would
-- only show up as noise in the unmatched count.
CREATE TEMP TABLE _sample AS
WITH src AS (
    SELECT f.trade_date,
           upper(split_part(f.symbol, '.', 1)) AS contract,
           split_part(f.symbol, '.', 2)        AS venue,
           f.open, f.high, f.low, f.close, f.volume
    FROM public.futures_daily f
    WHERE f.trade_date BETWEEN '2006-01-01' AND '2026-04-29'
      AND f.volume > 0
      AND f.close IS NOT NULL
      AND upper(split_part(f.symbol, '.', 1)) ~ '^[A-Z]+[0-9]{3,4}$'
), ranked AS (
    SELECT src.*,
           (extract(year FROM trade_date)::int / 5) * 5 AS era,
           row_number() OVER (PARTITION BY venue, (extract(year FROM trade_date)::int / 5) * 5
                              ORDER BY md5(contract || trade_date::text)) AS rn
    FROM src
), mapped AS (
    SELECT r.*,
           extract(year FROM r.trade_date)::int AS yr,
           CASE WHEN r.venue = 'CZC' AND r.contract ~ '^[A-Z]+[0-9]{3}$'
                THEN (left(right(r.contract, 3), 1)::int
                      - extract(year FROM r.trade_date)::int % 10 + 10) % 10
           END AS k
    FROM ranked r WHERE r.rn <= 12
)
SELECT m.trade_date, c.prev_trade_date, m.contract, m.venue, m.era,
       CASE WHEN m.k IS NULL THEN m.contract
            ELSE regexp_replace(m.contract, '[0-9]{3}$', '')
                 || lpad(((m.yr + m.k) % 100)::text, 2, '0')
                 || right(m.contract, 2)
       END AS m_symbol,
       m.open, m.high, m.low, m.close, m.volume
FROM mapped m
JOIN _cal c USING (trade_date)
WHERE m.k IS NULL OR m.k <= 3;
ANALYZE _sample;

SELECT venue, count(*) AS n, min(era) AS era_lo, max(era) AS era_hi
FROM _sample GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 5. 把分钟聚合成日线（Nested Loop + 索引探针，不扫表）==='
-- volume = 0 bars MUST be excluded. The vendor emits a bar for every minute of
-- the session through 2024 whether or not anything traded, and an empty bar
-- carries a carried-forward price, not a trade. Rolling them into OHLC invents
-- highs and lows that never happened: on 2007-11-07 TA0809 traded twice, both at
-- 8888, and futures_daily reports open=high=low=close=8888 — but the raw minute
-- roll-up shows a low of 8788 purely from empty bars, while the volume (2) still
-- matches exactly. Across the sample this one filter moved open from 180/285 to
-- 276/285, high 246 -> 277, low 241 -> 270.
--
-- This is a property of the table, not of this script: anything computing daily
-- OHLC, ranges, or true-range style indicators off futures_minute has to filter
-- the same way. It is recorded in the table COMMENT in schema.sql.
--
-- Hash/merge joins against this table mean a full decompressing scan, and the
-- planner reaches for one whenever its row estimate is off — which it always is
-- here, because it cannot see that the per-row time window excludes 262 of 264
-- chunks. Forcing a nested loop makes the access path structural rather than a
-- lucky plan.
--
-- Turn them back on immediately afterwards. They are a guard for hypertable
-- access only: leaving them off across the temp-to-temp joins in §10 turned a
-- 32k x 122k join into a 4-billion-comparison nested loop that took 132 s.
SET enable_hashjoin  = off;
SET enable_mergejoin = off;

CREATE TEMP TABLE _rolled AS
SELECT s.trade_date,
       s.contract,
       (array_agg(m.open  ORDER BY m.bar_time))[1]      AS m_open,
       max(m.high)                                      AS m_high,
       min(m.low)                                       AS m_low,
       (array_agg(m.close ORDER BY m.bar_time DESC))[1] AS m_close,
       sum(m.volume)                                    AS m_volume,
       count(*)                                         AS bars,
       max(m.bar_time)                                  AS last_bar
FROM _sample s
JOIN public.futures_minute m
  ON m.symbol = s.m_symbol                                                  -- bare column: index qual
 AND m.bar_time >= (s.prev_trade_date + time '20:00') AT TIME ZONE 'Asia/Shanghai'
 AND m.bar_time <  (s.trade_date      + time '15:30') AT TIME ZONE 'Asia/Shanghai'
 AND m.volume > 0
GROUP BY 1, 2;

RESET enable_hashjoin;
RESET enable_mergejoin;
ANALYZE _rolled;

\echo ''
\echo '=== 6. 命中率（未命中按 交易所 × 年代 归类，两张临时表相减）==='
-- LEFT JOIN between the two temp tables, NOT a correlated NOT EXISTS against
-- futures_minute: that plans as an anti-join over every chunk and never returns.
SELECT (SELECT count(*) FROM _sample) AS sampled,
       (SELECT count(*) FROM _rolled) AS matched;

SELECT s.venue, s.era, count(*) AS unmatched
FROM _sample s
LEFT JOIN _rolled r USING (trade_date, contract)
WHERE r.contract IS NULL
GROUP BY 1, 2 ORDER BY 1, 2;

\echo ''
\echo '=== 7. 抽样上的数值一致性（CFFEX 单列，见下方说明）==='
-- CFFEX is reported apart from the commodity venues because it fails for a known
-- structural reason rather than a data-quality one: the archive ends every CFFEX
-- session at 15:00 (last bar 14:59). That is correct for index futures from
-- 2016-01-01, when their close moved to 15:00, but it drops the final 15 minutes
-- of pre-2016 index futures and of ALL treasury futures (T/TF/TS), which close at
-- 15:15. Verified directly: IF2406 and T2406 both carry exactly 240 bars/day
-- ending 14:59, where T should have 255. So CFFEX closes and volumes are
-- systematically wrong here, always short, and no amount of sampling will fix it.
--
-- The commodity venues are what this project trades (CLAUDE.md: 股指期货 belongs
-- to stock_selector), and they are the number to read.
CREATE TEMP TABLE _cmp AS
SELECT s.venue, f.field, f.m, f.d
FROM _rolled r
JOIN _sample s USING (trade_date, contract),
LATERAL (VALUES ('open',   r.m_open::double precision,   s.open::double precision),
                ('high',   r.m_high::double precision,   s.high::double precision),
                ('low',    r.m_low::double precision,    s.low::double precision),
                ('close',  r.m_close::double precision,  s.close::double precision),
                ('volume', r.m_volume::double precision, s.volume::double precision)
        ) AS f(field, m, d);
ANALYZE _cmp;

SELECT field,
       count(*) FILTER (WHERE venue <> 'CFE')             AS commodity_n,
       count(*) FILTER (WHERE venue <> 'CFE' AND m = d)   AS commodity_exact,
       count(*) FILTER (WHERE venue =  'CFE')             AS cffex_n,
       count(*) FILTER (WHERE venue =  'CFE' AND m = d)   AS cffex_exact
FROM _cmp GROUP BY 1 ORDER BY 1;

\echo ''
\echo '=== 8. 分歧明细（商品在前，CFFEX 在后；最多 25 条）==='
-- last_bar is in the output because it is what identifies the CFFEX truncation
-- on sight: a 14:59 last bar on a T/TF/TS contract means 15 missing minutes, not
-- a bad load.
SELECT s.trade_date, s.venue, s.contract, s.m_symbol, r.bars, r.last_bar::time AS t1,
       r.m_open AS m_o, s.open AS d_o, r.m_high AS m_h, s.high AS d_h,
       r.m_low  AS m_l, s.low  AS d_l, r.m_close AS m_c, s.close AS d_c,
       r.m_volume AS m_v, s.volume AS d_v
FROM _rolled r JOIN _sample s USING (trade_date, contract)
WHERE r.m_close IS DISTINCT FROM s.close
   OR r.m_high  IS DISTINCT FROM s.high
   OR r.m_low   IS DISTINCT FROM s.low
ORDER BY (s.venue = 'CFE'), s.trade_date
LIMIT 25;

\echo ''
\echo '=== 9. 盲区提醒：futures_daily 只到 2026-04-29 ==='
SELECT (SELECT max(trade_date) FROM public.futures_daily) AS daily_ends,
       (SELECT max(bar_time)   FROM public.futures_minute) AS minute_ends;

\echo ''
\echo '=== 10. 与 market_data_minute 交叉验证（2026-04-30 之后唯一的独立信源）==='
-- market_data_minute is not minute bars: it is a realtime polling snapshot
-- across mixed assets, one row per symbol per minute with only last_price
-- populated. So this brackets rather than compares — every snapshot must fall
-- inside the [low, high] of the bar containing it.
--
-- `matched` is expected to be well below `snapshots_total`, for two reasons that
-- are properties of the collector, not faults in the load: it polls around the
-- clock and repeats the last price through closed hours (2026-08-11: 60 rows and
-- ONE distinct price for each of 01:00-08:00, against 14 distinct prices in the
-- 09:00 hour), and it does not run 17:00-23:59 at all, so the night session
-- opening is never sampled. Minutes with no bar simply drop out of the join.
-- The signal to read is inside_bar_range / matched: polling lag puts a few
-- snapshots a minute behind, but a timezone or symbol-mapping error would drive
-- this to ~0, which is what the check exists to catch.
--
-- market_data_minute.time is `timestamp WITHOUT time zone` holding Shanghai wall
-- clock (established from the session structure above), so it needs an explicit
-- AT TIME ZONE to meet bar_time's timestamptz — never rely on the session TZ.
--
-- Both sides are materialised into temp tables first. Driving 114k snapshot rows
-- straight into the hypertable would re-probe the same segments tens of
-- thousands of times; pulling one week of the relevant symbols out once, with
-- LITERAL time bounds so chunk exclusion happens at plan time, touches one chunk
-- and then the join is temp-to-temp.
--
-- CZCE is reported separately, not silently dropped: its snapshots would carry
-- 3-digit months and need the §4 mapping before they could join.
--
-- The window ends at the LAST OVERLAPPING minute, not at the snapshot table's
-- max: collection is live (2026-08-12) while the archive load stops at
-- 2026-08-05, so anchoring on market_data_minute alone would leave one day of
-- overlap out of seven.
SELECT to_char(w.hi - interval '7 days', 'YYYY-MM-DD') AS w0,
       to_char(w.hi + interval '1 day',  'YYYY-MM-DD') AS w1
FROM (SELECT least((SELECT max(bar_time) FROM public.futures_minute),
                   (SELECT max("time") FROM public.market_data_minute)
                       AT TIME ZONE 'Asia/Shanghai') AS hi) w \gset

\echo 'window:'
\echo :w0 ' .. ' :w1

CREATE TEMP TABLE _snap AS
SELECT upper(split_part(symbol, '.', 1))                          AS contract,
       date_trunc('minute', "time") AT TIME ZONE 'Asia/Shanghai'  AS bar_time,
       min(last_price)                                            AS lo,
       max(last_price)                                            AS hi
FROM public.market_data_minute
WHERE "time" >= :'w0' AND "time" < :'w1'
  AND symbol ~ '\.(SHF|DCE|CFE|INE|GFE)$'
  AND last_price > 0
GROUP BY 1, 2;
ANALYZE _snap;

SELECT count(*) AS czce_snapshots_skipped
FROM public.market_data_minute
WHERE "time" >= :'w0' AND "time" < :'w1' AND symbol ~ '\.CZC$';

-- Nested loop again for the one statement that touches the hypertable, then
-- straight back off: the join below is temp-to-temp and wants a hash.
SET enable_hashjoin  = off;
SET enable_mergejoin = off;

CREATE TEMP TABLE _bars AS
SELECT m.symbol, m.bar_time, m.low, m.high
FROM public.futures_minute m
WHERE m.bar_time >= :'w0' AND m.bar_time < :'w1'          -- literals: static chunk exclusion
  AND m.symbol IN (SELECT DISTINCT contract FROM _snap);

RESET enable_hashjoin;
RESET enable_mergejoin;
ANALYZE _bars;

SELECT count(*)                                                  AS matched_snapshots,
       count(*) FILTER (WHERE s.lo >= b.low AND s.hi <= b.high)   AS inside_bar_range,
       (SELECT count(*) FROM _snap)                              AS snapshots_total
FROM _snap s JOIN _bars b ON b.symbol = s.contract AND b.bar_time = s.bar_time;
