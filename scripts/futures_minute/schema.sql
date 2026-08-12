-- Stage 2 schema for the futures_minute ingestion.
-- Design: docs/plans/2026-08-12-futures-minute-ingestion-design.md
--
-- Run on the Debian primary. The unix socket rejects `admin` under peer auth,
-- so connect over TCP even when logged into the box:
--
--   PGPASSWORD=... psql -h 127.0.0.1 -U admin -d market_monitor \
--       -v ON_ERROR_STOP=1 -f schema.sql
--
-- This table is deliberately NOT on the sync chain: it is absent from
-- sync-worker config.yaml, exactly like market_data_minute. No two-end DDL,
-- no sync_state row, no Pi5 work. The check below fails the script if that
-- assumption ever stops holding.

\set ON_ERROR_STOP on

DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM sync_state
               WHERE schema_name = 'public' AND table_name = 'futures_minute') THEN
        RAISE EXCEPTION
            'futures_minute has a sync_state row; this design assumes it is unsynced';
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS public.futures_minute (
    bar_time      timestamptz      NOT NULL,   -- bar open (source column `bob`)
    symbol        text             NOT NULL,   -- uppercase, 4-digit month code
    exchange      text             NOT NULL,   -- SHFE/DCE/CZCE/CFFEX/INE/GFEX
    open          double precision,
    high          double precision,
    low           double precision,
    close         double precision,
    volume        double precision,
    amount        double precision,            -- 成交额
    open_interest double precision,            -- source column `position`
    PRIMARY KEY (symbol, bar_time)
);

COMMENT ON TABLE public.futures_minute IS
    'Commodity futures 1-minute bars from the local vendor archive (期货数据/1m), '
    '2005 -> 2026-08. Bars are stamped with their OPEN time: a row at 09:00 '
    'covers [09:00, 09:01). volume=0 bars are kept on purpose so that "no trade" '
    'stays distinguishable from "no data". Vendor continuous series (9999/8888) '
    'are excluded - they exist only from 2025 in the archive; use '
    'continuous_contract_ohlc instead. CZCE months are 4-digit here (AP2605) '
    'while futures_daily stores 3-digit (AP605.CZC); normalise before joining. '
    'Not on the Pi5 sync chain.';

SELECT create_hypertable('public.futures_minute', 'bar_time',
                         chunk_time_interval => INTERVAL '1 month',
                         if_not_exists => TRUE);

ALTER TABLE public.futures_minute SET (
    timescaledb.compress,
    timescaledb.compress_segmentby = 'symbol',
    timescaledb.compress_orderby   = 'bar_time'
);

-- No compression policy: Stage 2 compresses each year's chunks explicitly as
-- they land, which is what holds peak uncompressed footprint to one year.
-- Add a policy afterwards if this table starts taking daily appends.
