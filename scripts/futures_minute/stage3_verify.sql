-- Stage 3 of the futures_minute ingestion: verify the loaded table.
-- Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
--
--   PGPASSWORD=... psql -h 127.0.0.1 -U admin -d market_monitor -f stage3_verify.sql
--
-- Read-only. Every check prints a row; nothing is asserted, so read the output.
--
-- Trading days do not align with calendar days. Measured session structure:
-- night 21:00-02:59, day 09:00-15:00 (15:00-15:15 for bond futures). The night
-- session of trade date T opens at 21:00 on the previous trading day's calendar
-- date and runs past midnight into T's own calendar date. Hence:
--
--   hour >= 20  ->  trade_date = the next trading day after the bar's date
--   hour <  20  ->  trade_date = the bar's own date
--
-- Trading days are taken from futures_daily itself rather than trading_calendar,
-- so the mapping cannot disagree with the table being compared against.

\timing on
\set ON_ERROR_STOP on

\echo '=== 1. 总量与区间 ==='
SELECT count(*)                          AS rows,
       count(DISTINCT symbol)            AS contracts,
       min(bar_time)                     AS first_bar,
       max(bar_time)                     AS last_bar,
       pg_size_pretty(hypertable_size('public.futures_minute')) AS size
FROM public.futures_minute;

\echo '=== 2. 逐年行数（与 stage1 manifest 对照）==='
SELECT extract(year FROM bar_time)::int AS year, count(*) AS rows
FROM public.futures_minute
GROUP BY 1 ORDER BY 1;

\echo '=== 3. 压缩情况 ==='
SELECT count(*) FILTER (WHERE is_compressed)     AS compressed_chunks,
       count(*) FILTER (WHERE NOT is_compressed) AS uncompressed_chunks
FROM timescaledb_information.chunks
WHERE hypertable_name = 'futures_minute';

-- Trading-day mapping, materialised once and reused by the checks below.
CREATE TEMP TABLE _td AS
SELECT trade_date AS d,
       lead(trade_date) OVER (ORDER BY trade_date) AS next_d
FROM (SELECT DISTINCT trade_date FROM public.futures_daily) x;
CREATE INDEX ON _td (d);
ANALYZE _td;

CREATE TEMP TABLE _daily AS
SELECT CASE WHEN extract(hour FROM m.bar_time) >= 20 THEN t.next_d
            ELSE m.bar_time::date END          AS trade_date,
       m.symbol,
       m.exchange,
       -- futures_daily keeps CZCE months at 3 digits (AP605.CZC) while this
       -- table keeps 4 (AP2605). Drop the decade digit on the minute side to
       -- join; keyed on (trade_date, contract) the decade is unambiguous,
       -- since a contract never lists more than ~2 years before delivery.
       CASE WHEN m.exchange = 'CZCE' AND m.symbol ~ '^[A-Z]+[0-9]{4}[A-Z]*$'
            THEN regexp_replace(m.symbol, '^([A-Z]+)[0-9]([0-9]{3})([A-Z]*)$', '\1\2\3')
            ELSE m.symbol END                  AS join_key,
       sum(m.volume)                           AS volume,
       sum(m.amount)                           AS amount,
       max(m.high)                             AS high,
       min(m.low)                              AS low,
       (array_agg(m.close ORDER BY m.bar_time DESC))[1] AS close,
       (array_agg(m.open  ORDER BY m.bar_time))[1]      AS open,
       count(*)                                AS bars
FROM public.futures_minute m
LEFT JOIN _td t ON t.d = m.bar_time::date
GROUP BY 1, 2, 3, 4;
CREATE INDEX ON _daily (trade_date, join_key);
ANALYZE _daily;

\echo '=== 4. 分钟聚合成日线 vs futures_daily：覆盖度 ==='
CREATE TEMP TABLE _fd AS
SELECT trade_date,
       upper(split_part(symbol, '.', 1)) AS contract,
       open, high, low, close, volume, turnover, oi
FROM public.futures_daily
WHERE trade_date >= '2005-01-01';
CREATE INDEX ON _fd (trade_date, contract);
ANALYZE _fd;

SELECT (SELECT count(*) FROM _daily)                    AS minute_contract_days,
       (SELECT count(*) FROM _fd)                       AS daily_contract_days,
       (SELECT count(*) FROM _daily d JOIN _fd f
          ON f.trade_date = d.trade_date AND f.contract = d.join_key) AS matched;

\echo '=== 5. 数值一致性（重叠合约-日）==='
WITH j AS (
    SELECT d.close AS m_close, f.close AS d_close,
           d.high  AS m_high,  f.high  AS d_high,
           d.low   AS m_low,   f.low   AS d_low,
           d.volume AS m_vol,  f.volume AS d_vol
    FROM _daily d JOIN _fd f
      ON f.trade_date = d.trade_date AND f.contract = d.join_key
)
SELECT 'close' AS field,
       count(*) FILTER (WHERE m_close = d_close)                                    AS exact,
       count(*) FILTER (WHERE abs(m_close - d_close) <= 1e-6 * greatest(abs(m_close), abs(d_close))) AS within_1e6,
       count(*)                                                                     AS comparable
FROM j WHERE m_close IS NOT NULL AND d_close IS NOT NULL
UNION ALL
SELECT 'high',
       count(*) FILTER (WHERE m_high = d_high),
       count(*) FILTER (WHERE abs(m_high - d_high) <= 1e-6 * greatest(abs(m_high), abs(d_high))),
       count(*)
FROM j WHERE m_high IS NOT NULL AND d_high IS NOT NULL
UNION ALL
SELECT 'low',
       count(*) FILTER (WHERE m_low = d_low),
       count(*) FILTER (WHERE abs(m_low - d_low) <= 1e-6 * greatest(abs(m_low), abs(d_low))),
       count(*)
FROM j WHERE m_low IS NOT NULL AND d_low IS NOT NULL
UNION ALL
SELECT 'volume',
       count(*) FILTER (WHERE m_vol = d_vol),
       count(*) FILTER (WHERE abs(m_vol - d_vol) <= 1e-6 * greatest(abs(m_vol), abs(d_vol))),
       count(*)
FROM j WHERE m_vol IS NOT NULL AND d_vol IS NOT NULL;

\echo '=== 6. 最大分歧样例（close 相对差 > 1%）==='
SELECT d.trade_date, d.symbol, d.close AS minute_close, f.close AS daily_close,
       d.bars, d.volume AS minute_vol, f.volume AS daily_vol
FROM _daily d JOIN _fd f ON f.trade_date = d.trade_date AND f.contract = d.join_key
WHERE d.close IS NOT NULL AND f.close IS NOT NULL AND f.close <> 0
  AND abs(d.close - f.close) / greatest(abs(d.close), abs(f.close)) > 0.01
ORDER BY abs(d.close - f.close) / greatest(abs(d.close), abs(f.close)) DESC
LIMIT 15;

\echo '=== 6b. 提醒：futures_daily 只到 2026-04-29，以上检查覆盖不到之后的数据 ==='
SELECT (SELECT max(trade_date) FROM public.futures_daily)     AS daily_ends,
       (SELECT max(bar_time)   FROM public.futures_minute)    AS minute_ends,
       (SELECT count(*) FROM public.futures_minute
         WHERE bar_time > (SELECT max(trade_date) + 1 FROM public.futures_daily))
                                                              AS rows_beyond_daily;

\echo '=== 7. 与 market_data_minute 交叉验证（唯一能覆盖 2026-04-30 之后的独立信源）==='
-- market_data_minute is NOT minute bars: it is an irregular realtime polling
-- snapshot across mixed asset classes (759 stock/index symbols, only 66 futures
-- contracts), stamped at times like 09:01:27.078 with volume/open/high/low all
-- zero and only last_price populated. So this cannot be an equality test on
-- close. What it can do is bracket: each snapshot must fall inside the [low,
-- high] of the minute bar containing it.
--
-- Its futures symbols carry the exchange suffix (T2603.CFE) while futures_minute
-- keeps the bare code. CZCE is excluded: lifting its 3-digit month back to 4 is
-- ambiguous in this direction.
WITH snap AS (
    SELECT upper(split_part(w.symbol, '.', 1))                            AS contract,
           date_trunc('minute', w."time") AT TIME ZONE 'Asia/Shanghai'    AS bar_time,
           w.last_price
    FROM public.market_data_minute w
    WHERE w.symbol ~ '\.(SHF|DCE|CFE|INE|GFE)$'
      AND w.last_price > 0
)
SELECT CASE WHEN s.bar_time >= '2026-04-30'::timestamptz
            THEN '2026-04-30 之后（futures_daily 无覆盖）'
            ELSE '2026-04-29 及之前' END                                  AS period,
       count(*)                                                           AS matched_snapshots,
       count(*) FILTER (WHERE s.last_price BETWEEN m.low AND m.high)      AS inside_bar_range,
       round((count(*) FILTER (WHERE s.last_price BETWEEN m.low AND m.high))::numeric
             / nullif(count(*), 0), 4)                                    AS frac_inside
FROM snap s
JOIN public.futures_minute m
  ON m.symbol = s.contract AND m.bar_time = s.bar_time
GROUP BY 1 ORDER BY 1;

\echo '=== 8. 每日 K 线根数分布（提醒：2025 起厂商不再补齐非交易分钟）==='
SELECT extract(year FROM trade_date)::int AS year,
       round(avg(bars)::numeric, 1) AS avg_bars_per_contract_day,
       max(bars)                    AS max_bars
FROM _daily
WHERE trade_date IS NOT NULL
GROUP BY 1 ORDER BY 1;
