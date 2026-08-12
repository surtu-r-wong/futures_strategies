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

\timing on
\set ON_ERROR_STOP on

-- Deliberately restrictive: no parallel workers, default work_mem, and a hard
-- ceiling so a mistake times out instead of taking the cluster with it.
SET max_parallel_workers_per_gather = 0;
SET statement_timeout = '300s';
SET work_mem = '32MB';

\echo '=== 1. 规模与区间（走 chunk 元数据，不扫表）==='
SELECT (SELECT approximate_row_count('public.futures_minute'))                AS approx_rows,
       (SELECT count(*) FROM timescaledb_information.chunks
         WHERE hypertable_name = 'futures_minute')                            AS chunks,
       (SELECT count(*) FROM timescaledb_information.chunks
         WHERE hypertable_name = 'futures_minute' AND is_compressed)          AS compressed,
       pg_size_pretty(hypertable_size('public.futures_minute'))               AS size;

\echo '=== 2. 首尾 K 线（索引扫描）==='
SELECT (SELECT min(bar_time) FROM public.futures_minute) AS first_bar,
       (SELECT max(bar_time) FROM public.futures_minute) AS last_bar;

\echo '=== 3. 抽样比对：分钟聚合成日线 vs futures_daily ==='
-- 300 contract-days drawn from across the overlap. Each is fetched by
-- (symbol, bar_time) so the PK index bounds the work; nothing scans the table.
--
-- Trading days do not align with calendar days: night session 21:00-02:59 plus
-- day session 09:00-15:00, so the night session of trade date T opens on the
-- previous trading day's calendar date. Bars at hour >= 20 belong to the next
-- trading day; everything else to its own date.
--
-- futures_daily keeps CZCE months at 3 digits (AP605.CZC) while this table keeps
-- 4 (AP2605), so the minute side drops the decade digit to join.
CREATE TEMP TABLE _sample AS
SELECT f.trade_date,
       upper(split_part(f.symbol, '.', 1)) AS contract,
       f.open, f.high, f.low, f.close, f.volume
FROM public.futures_daily f
WHERE f.trade_date BETWEEN '2006-01-01' AND '2026-04-29'
  AND f.volume > 0
  AND f.close IS NOT NULL
ORDER BY md5(f.symbol || f.trade_date::text)   -- deterministic pseudo-random
LIMIT 300;
ANALYZE _sample;

CREATE TEMP TABLE _rolled AS
SELECT s.trade_date,
       s.contract,
       (array_agg(m.open  ORDER BY m.bar_time))[1]      AS m_open,
       max(m.high)                                      AS m_high,
       min(m.low)                                       AS m_low,
       (array_agg(m.close ORDER BY m.bar_time DESC))[1] AS m_close,
       sum(m.volume)                                    AS m_volume,
       count(*)                                         AS bars
FROM _sample s
JOIN public.futures_minute m
  ON (CASE WHEN m.exchange = 'CZCE' AND m.symbol ~ '^[A-Z]+[0-9]{4}[A-Z]*$'
           THEN regexp_replace(m.symbol, '^([A-Z]+)[0-9]([0-9]{3})([A-Z]*)$', '\1\2\3')
           ELSE m.symbol END) = s.contract
 AND m.bar_time >= (s.trade_date - 1) + time '20:00'
 AND m.bar_time <  s.trade_date       + time '15:30'
GROUP BY 1, 2;

SELECT count(*) AS sampled, (SELECT count(*) FROM _rolled) AS matched_in_minute
FROM _sample;

\echo '=== 4. 抽样上的数值一致性 ==='
SELECT 'close'  AS field,
       count(*) FILTER (WHERE r.m_close = s.close)  AS exact,
       count(*)                                     AS comparable
FROM _rolled r JOIN _sample s USING (trade_date, contract)
UNION ALL
SELECT 'high',  count(*) FILTER (WHERE r.m_high = s.high),   count(*)
FROM _rolled r JOIN _sample s USING (trade_date, contract)
UNION ALL
SELECT 'low',   count(*) FILTER (WHERE r.m_low = s.low),     count(*)
FROM _rolled r JOIN _sample s USING (trade_date, contract)
UNION ALL
SELECT 'volume',count(*) FILTER (WHERE r.m_volume = s.volume), count(*)
FROM _rolled r JOIN _sample s USING (trade_date, contract);

\echo '=== 5. 抽样中的分歧明细（最多 15 条）==='
SELECT s.trade_date, s.contract, r.bars,
       r.m_open AS m_o, s.open AS d_o, r.m_high AS m_h, s.high AS d_h,
       r.m_low  AS m_l, s.low  AS d_l, r.m_close AS m_c, s.close AS d_c,
       r.m_volume AS m_v, s.volume AS d_v
FROM _rolled r JOIN _sample s USING (trade_date, contract)
WHERE r.m_close IS DISTINCT FROM s.close
   OR r.m_high  IS DISTINCT FROM s.high
   OR r.m_low   IS DISTINCT FROM s.low
LIMIT 15;

\echo '=== 6. 盲区提醒：futures_daily 只到 2026-04-29 ==='
SELECT (SELECT max(trade_date) FROM public.futures_daily) AS daily_ends,
       (SELECT max(bar_time)   FROM public.futures_minute) AS minute_ends;

\echo '=== 7. 与 market_data_minute 交叉验证（覆盖 2026-04-30 之后的唯一独立信源）==='
-- market_data_minute is not minute bars: it is an irregular realtime polling
-- snapshot across mixed assets (759 stock/index symbols against 66 futures),
-- stamped off minute boundaries with only last_price populated. So this
-- brackets rather than compares: each snapshot must fall inside the [low, high]
-- of the bar containing it. Bounded to one week to stay index-friendly.
WITH snap AS (
    SELECT upper(split_part(w.symbol, '.', 1))                         AS contract,
           date_trunc('minute', w."time") AT TIME ZONE 'Asia/Shanghai' AS bar_time,
           w.last_price
    FROM public.market_data_minute w
    WHERE w.symbol ~ '\.(SHF|DCE|CFE|INE|GFE)$'
      AND w.last_price > 0
      AND w."time" >= '2026-07-27' AND w."time" < '2026-08-03'
)
SELECT count(*)                                                      AS matched_snapshots,
       count(*) FILTER (WHERE s.last_price BETWEEN m.low AND m.high) AS inside_bar_range
FROM snap s
JOIN public.futures_minute m
  ON m.symbol = s.contract AND m.bar_time = s.bar_time;
