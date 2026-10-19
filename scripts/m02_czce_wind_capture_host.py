"""One-off WindPy capture of CZCE 09:30 minute bars (host-side worker).

Runs on the licensed workstation's Windows Python, pushed there by
scripts/m02_czce_wind_capture.py. No repo dependencies: stdlib + WindPy +
wind_batch_mutex (imported from the collector staging dir via PYTHONPATH,
exactly like the task17 probes). Shares the Global\\MarketMonitorWindBatch
mutex with the production EOD batch, with a bounded wait so a stuck batch
fails this capture closed instead of hanging the ssh session.

    python.exe m02_czce_wind_capture_host.py --date 2026-10-16 \
        --out C:\\ProgramData\\MarketMonitor\\futures-minute\\m02-czce-raw-2026-10-16.json
    python.exe m02_czce_wind_capture_host.py --date 2026-10-16 --smoke   # 2 codes, stdout

The 10-minute window around 09:30 (09:25-09:35) deliberately spans both
minute-bar labeling conventions (bar-start vs bar-end); the local receipt
builder picks the 09:30 bar after cross-validating the labeling against
public.futures_minute. Prices are raw (unadjusted) Wind minute bars.
"""

import argparse
import datetime as dt
import json
import math
import os
import sys
import threading
import time

MUTEX_WAIT_MS = 10 * 60 * 1000
START_RETRIES = 3
START_RETRY_SLEEP_S = 20
# w.start() can block indefinitely while a login box waits for an operator
# (observed 2026-10-18: >24 min, no internal timeout). Hard-exit instead so
# the orchestrator's poll always sees a terminal exit line.
WATCHDOG_S = 15 * 60


def _watchdog(seconds, done):
    if not done.wait(seconds):
        os._exit(3)


FIELDS = "open,high,low,close,volume"
WINDOW_BEGIN = "09:25:00"
WINDOW_END = "09:35:00"

CZCE_CONTRACTS = [
    "AP2701.CZC", "CF2701.CZC", "CJ2701.CZC", "FG2701.CZC", "MA2701.CZC",
    "OI2701.CZC", "PF2612.CZC", "PK2612.CZC", "PL2612.CZC", "PR2612.CZC",
    "PX2612.CZC", "RM2701.CZC", "SA2701.CZC", "SF2701.CZC", "SH2701.CZC",
    "SM2701.CZC", "SR2701.CZC", "TA2701.CZC", "UR2701.CZC",
]
SMOKE_CODES = ["AG2612.SHF", "TA2701.CZC"]


def _clean(value):
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if math.isnan(number):
        return None
    return number


def _bars(result):
    """Flatten a single-code wsi result into [{time, open, ...}]."""
    fields = [str(f).lower() for f in (getattr(result, "Fields", None) or [])]
    times = [str(t) for t in (getattr(result, "Times", None) or [])]
    data = getattr(result, "Data", None) or []
    rows = []
    for i, stamp in enumerate(times):
        row = {"time": stamp}
        for j, field in enumerate(fields):
            if j < len(data) and i < len(data[j]):
                row[field] = _clean(data[j][i])
        rows.append(row)
    return fields, rows


def _czce_wind_code(contract):
    """Wind's CZCE spelling cuts the delivery year to one digit (TA2701.CZC
    -> TA701.CZC); the four-digit form the database stores is refused by
    Wind's data functions with -40520007 "Server not Find Data". Same rule
    as cta_carry.targets.wind_code (user ruling 2026-09-16). Non-CZCE codes
    pass through untouched (AG2612.SHF must NOT lose its year digit)."""
    code, _, suffix = contract.rpartition(".")
    if suffix != "CZC":
        return contract
    letters = code.rstrip("0123456789")
    digits = code[len(letters):]
    if len(digits) == 4:
        digits = digits[1:]
    return f"{letters}{digits}.{suffix}"


def capture(codes, trade_date):
    from WindPy import w
    from wind_batch_mutex import wind_batch_mutex

    done = threading.Event()
    threading.Thread(
        target=_watchdog, args=(WATCHDOG_S, done), daemon=True
    ).start()
    payload = {
        "status": "ok",
        "tool": "WindPy wsi",
        "trade_date": trade_date,
        "window": [WINDOW_BEGIN, WINDOW_END],
        "bar_size": "1",
        "fields_requested": FIELDS,
        "captured_at": dt.datetime.now().isoformat(timespec="seconds"),
        "czce_code_mapping": (
            "wsi queried in Wind's one-digit-year CZCE spelling (TA701.CZC); "
            "keys below stay database-style (TA2701.CZC)"
        ),
        "codes": {},
    }
    with wind_batch_mutex(timeout_ms=MUTEX_WAIT_MS):
        # w.start() may pop the Wind API login box (WBox) on the workstation
        # desktop; the retries give an operator time to complete it. A stale
        # machine-wide login expires over idle weekends (observed 2026-10-18:
        # -40520008 'Timeout Error!' for every fresh client, gateway included).
        errorcode = None
        for attempt in range(START_RETRIES):
            started = w.start()
            errorcode = getattr(started, "ErrorCode", None)
            if errorcode == 0:
                break
            payload.setdefault("wind_start_attempts", []).append(
                {
                    "attempt": attempt + 1,
                    "errorcode": errorcode,
                    "data": str(getattr(started, "Data", None))[:200],
                }
            )
            time.sleep(START_RETRY_SLEEP_S)
        if errorcode != 0:
            payload["status"] = "wind-start-failed"
            payload["wind_start_errorcode"] = errorcode
            done.set()
            return payload
        for code in codes:
            result = w.wsi(
                _czce_wind_code(code),
                FIELDS,
                f"{trade_date} {WINDOW_BEGIN}",
                f"{trade_date} {WINDOW_END}",
                "BarSize=1",
            )
            errorcode = getattr(result, "ErrorCode", None)
            if errorcode != 0:
                payload["codes"][code] = {
                    "errorcode": errorcode,
                    "error": str(getattr(result, "Data", ""))[:200],
                }
                continue
            fields, rows = _bars(result)
            payload["codes"][code] = {"errorcode": 0, "fields": fields, "bars": rows}
        w.stop()
    done.set()
    return payload


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", required=True)
    parser.add_argument("--out")
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args(argv)

    codes = SMOKE_CODES if args.smoke else CZCE_CONTRACTS
    payload = capture(codes, args.date)
    text = json.dumps(payload, ensure_ascii=False, indent=1)
    if args.out and payload["status"] == "ok":
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text + "\n")
        print(f"written: {args.out}")
    print(text if payload["status"] != "ok" or not args.out else text[:400])
    return 0 if payload["status"] == "ok" else 2


if __name__ == "__main__":
    sys.exit(main())
