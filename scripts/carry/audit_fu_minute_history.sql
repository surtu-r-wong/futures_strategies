-- Reproducible, read-only evidence for the Carry minute-session eligibility audit.
--
-- Run against the same PostgreSQL database used by the real capture:
--   psql -X -v ON_ERROR_STOP=1 -f scripts/carry/audit_fu_minute_history.sql
--
-- Important: positive-volume minute rows are the execution/traded-minute
-- convention.  The archive retains zero-volume placeholders with carried prices;
-- including those rows in OHLC would invent extrema that never traded.

\set ON_ERROR_STOP on

BEGIN TRANSACTION READ ONLY;

\echo '=== Overall physical minute-table bounds (index probes, not a full count) ==='
(
    SELECT 'first' AS boundary, bar_time, symbol, exchange
    FROM public.futures_minute
    ORDER BY bar_time ASC, symbol ASC, exchange ASC
    LIMIT 1
)
UNION ALL
(
    SELECT 'last' AS boundary, bar_time, symbol, exchange
    FROM public.futures_minute
    ORDER BY bar_time DESC, symbol DESC, exchange DESC
    LIMIT 1
);

\echo '=== FU daily coverage by year through the capture cutoff ==='
SELECT
    EXTRACT(YEAR FROM trade_date)::integer AS year,
    MIN(trade_date) AS first_trade_date,
    MAX(trade_date) AS last_trade_date,
    COUNT(*) AS contract_days
FROM public.futures_daily
WHERE UPPER(symbol) ~ '^FU[0-9]{3,4}\.SHF$'
  AND trade_date <= DATE '2026-04-29'
GROUP BY 1
ORDER BY 1;

\echo '=== FU physical minute coverage by year (bounded time predicate) ==='
SELECT
    EXTRACT(YEAR FROM bar_time AT TIME ZONE 'Asia/Shanghai')::integer AS year,
    MIN(bar_time) AS first_bar,
    MAX(bar_time) AS last_bar,
    COUNT(DISTINCT symbol) AS contracts
FROM public.futures_minute
WHERE exchange = 'SHFE'
  AND symbol ~ '^FU[0-9]{4}$'
  AND bar_time >= TIMESTAMPTZ '2005-01-01 00:00:00+08'
  AND bar_time <  TIMESTAMPTZ '2026-05-01 00:00:00+08'
GROUP BY 1
ORDER BY 1;

\echo '=== FU1604 daily row on 2016-01-04 ==='
SELECT
    trade_date,
    symbol,
    open,
    high,
    low,
    close,
    volume,
    turnover
FROM public.futures_daily
WHERE symbol = 'FU1604.SHF'
  AND trade_date = DATE '2016-01-04';

\echo '=== FU1604 traded-minute roll-up for target trade date 2016-01-04 ==='
SELECT
    COUNT(*) AS traded_rows,
    MIN(bar_time) AS first_traded_bar,
    MAX(bar_time) AS last_traded_bar,
    (ARRAY_AGG(open ORDER BY bar_time))[1] AS open,
    MAX(high) AS high,
    MIN(low) AS low,
    (ARRAY_AGG(close ORDER BY bar_time DESC))[1] AS close,
    SUM(volume) AS volume,
    SUM(amount) AS amount,
    SUM(amount) / NULLIF(SUM(volume), 0) / 50.0 AS vwap_multiplier_50
FROM public.futures_minute
WHERE exchange = 'SHFE'
  AND symbol = 'FU1604'
  AND bar_time >= TIMESTAMPTZ '2015-12-31 21:00:00+08'
  AND bar_time <  TIMESTAMPTZ '2016-01-04 15:01:00+08'
  AND volume > 0;

\echo '=== FU liquidity input and shifted 120-product-day mean, 2011-2015 targets ==='
WITH product_days AS (
    SELECT
        trade_date,
        SUM(turnover::numeric) AS product_turnover
    FROM public.futures_daily
    WHERE UPPER(symbol) ~ '^FU[0-9]{3,4}\.SHF$'
      AND trade_date BETWEEN DATE '2010-01-01' AND DATE '2015-12-31'
    GROUP BY trade_date
), shifted AS (
    SELECT
        trade_date,
        product_turnover,
        COUNT(*) OVER (
            ORDER BY trade_date
            ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
        ) AS prior_observations,
        AVG(product_turnover) OVER (
            ORDER BY trade_date
            ROWS BETWEEN 120 PRECEDING AND 1 PRECEDING
        ) AS liquidity_mean
    FROM product_days
), target AS (
    SELECT *
    FROM shifted
    WHERE trade_date BETWEEN DATE '2011-01-01' AND DATE '2015-12-31'
)
SELECT
    (SELECT MIN(trade_date) FROM product_days) AS input_start,
    (SELECT MAX(trade_date) FROM product_days) AS input_end,
    (SELECT COUNT(*) FROM product_days) AS input_product_days,
    MIN(trade_date) FILTER (WHERE prior_observations = 120) AS first_complete_target,
    MAX(liquidity_mean) FILTER (WHERE prior_observations = 120) AS max_liquidity_mean,
    (ARRAY_AGG(trade_date ORDER BY liquidity_mean DESC)
        FILTER (WHERE prior_observations = 120))[1] AS max_liquidity_trade_date,
    COUNT(*) FILTER (
        WHERE prior_observations = 120
          AND liquidity_mean >= 5000000000.0
    ) AS eligible_target_days
FROM target;

ROLLBACK;
