"""Capture the 19 CZCE contracts' 09:30 minute bar from the licensed workstation.

    .venv/bin/python scripts/m02_czce_wind_capture.py --trade-date 2026-10-20
    .venv/bin/python scripts/m02_czce_wind_capture.py --smoke         # 2 codes
    .venv/bin/python scripts/m02_czce_wind_capture.py --from-file raw.json  # no host run

public.futures_minute has no CZCE coverage (collector v1 keeps it frozen), so
the M02 build day takes the 19 CZCE execution prices from a one-off WindPy wsi
process on the licensed workstation. The deployed Wind gateway has no minute
endpoint and its long-lived session goes stale after idle weekends (observed
2026-10-18: wsd -103), so the capture runs its own client:

  push host worker -> schtasks /Create /IT (session-1, one-shot) -> /Run
  -> poll -> fetch raw JSON -> schtasks /Delete -> validate -> receipt

wsi minute bars are labeled by minute START and are value-identical to
public.futures_minute (cross-validated on AG2612 2026-10-16 09:30:00:
open/high/low/close/volume all equal), so the receipt is the same quote
semantic as the 48 non-CZCE contracts. Fail-closed: any missing code, missing
09:30 bar, non-positive open, or price outside the futures_daily [low, high]
band aborts without writing the receipt (band check is advisory if the EOD
row has not landed yet; the advance script re-checks settlement anyway).

Writes build/czce-<date>.json (m02-czce-wind-minute-receipt.v1, consumed by
m02_futures_advance.py) plus the raw capture. Machine-wide Wind API login can
expire over idle weekends; the host worker retries w.start() while an
operator completes the login box if one pops.
"""

import argparse
import hashlib
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

REPO = Path(__file__).resolve().parents[1]
OUT_DIR = REPO / "docs/requests/2026-10-17-m02-initialization/build"
HOST_SCRIPT = REPO / "scripts/m02_czce_wind_capture_host.py"
HOST_TASK_CMD = REPO / "scripts/m02_czce_wind_capture_task.cmd"

SSH = ["ssh", "-p", "2223", "ghls@100.120.152.1"]
SCP = ["scp", "-P", "2223"]
HOST_DIR_U = "/mnt/c/ProgramData/MarketMonitor/futures-minute/m02"
HOST_DIR_W = r"C:\ProgramData\MarketMonitor\futures-minute\m02"
TASK_NAME = "m02_czce_capture"
POLL_TIMEOUT_S = 25 * 60  # covers mutex wait + w.start() login retries
POLL_STEP_S = 15

CZCE_CONTRACTS = [
    "AP2701.CZC", "CF2701.CZC", "CJ2701.CZC", "FG2701.CZC", "MA2701.CZC",
    "OI2701.CZC", "PF2612.CZC", "PK2612.CZC", "PL2612.CZC", "PR2612.CZC",
    "PX2612.CZC", "RM2701.CZC", "SA2701.CZC", "SF2701.CZC", "SH2701.CZC",
    "SM2701.CZC", "SR2701.CZC", "TA2701.CZC", "UR2701.CZC",
]


def _assert_frozen_czce():
    """The hardcoded list must equal the frozen batch's CZCE column."""
    import csv

    frozen = (
        REPO
        / "docs/requests/2026-10-14-multi-asset-operational-returns/"
        "m01-offline-diagnosis-evidence/verification-02/targets.csv"
    )
    with frozen.open() as handle:
        codes = sorted(
            row["contract"]
            for row in csv.DictReader(handle)
            if row["contract"].endswith(".CZC")
        )
    if codes != sorted(CZCE_CONTRACTS):
        raise ValueError(
            f"CZCE list drifted from the frozen batch: {set(codes) ^ set(CZCE_CONTRACTS)}"
        )


_assert_frozen_czce()


def _ssh(command, *, check=True, capture=True):
    result = subprocess.run(
        SSH + [command],
        capture_output=capture,
        text=True,
        errors="replace",  # schtasks emits localized GBK banners
        timeout=180,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            f"ssh failed ({result.returncode}): {command[:120]}\n"
            f"{(result.stderr or result.stdout or '')[:300]}"
        )
    return result


def _push_workers():
    _ssh(f"mkdir -p {HOST_DIR_U}", capture=False)
    for local, name in ((HOST_SCRIPT, "m02_czce_wind_capture_host.py"),
                        (HOST_TASK_CMD, "m02_czce_wind_capture_task.cmd")):
        subprocess.run(
            SCP + [str(local), f"ghls@100.120.152.1:{HOST_DIR_U}/{name}"],
            check=True,
            timeout=60,
        )


def _run_on_host(trade_date, smoke):
    """Create the one-shot session-1 task, run it, poll, return raw stdout."""
    tr = f"{HOST_DIR_W}\\m02_czce_wind_capture_task.cmd --date {trade_date}"
    if not smoke:
        tr += f" --out {HOST_DIR_W}\\m02-czce-raw-{trade_date}.json"
    create = (
        "LANG=C /mnt/c/Windows/System32/schtasks.exe "
        f'/Create /F /TN {TASK_NAME} /TR "{tr}" /SC ONCE /ST 23:59 /IT'
    ).replace("\\", "\\\\")
    _ssh(create)
    try:
        # Stale stdout from a previous run would fake the exit marker.
        _ssh(f"rm -f {HOST_DIR_U}/last-run-stdout.txt", check=False)
        _ssh(f"LANG=C /mnt/c/Windows/System32/schtasks.exe /Run /TN {TASK_NAME}")
        deadline = time.monotonic() + POLL_TIMEOUT_S
        stdout = ""
        while time.monotonic() < deadline:
            time.sleep(POLL_STEP_S)
            stdout = _ssh(
                f"cat {HOST_DIR_U}/last-run-stdout.txt 2>/dev/null", check=False
            ).stdout
            if stdout and "exit=" in stdout:
                tail = stdout.strip().splitlines()[-1]
                if tail != "exit=0":
                    raise RuntimeError(f"host capture failed: {stdout[:400]}")
                return stdout
        raise RuntimeError(
            f"host capture did not finish within {POLL_TIMEOUT_S}s "
            "(is the Wind login box waiting for an operator?)"
        )
    finally:
        # /End first: a task whose process died mid-run leaves a zombie
        # "running" state that plain /Delete refuses to reap.
        _ssh(
            "LANG=C /mnt/c/Windows/System32/schtasks.exe "
            f"/End /TN {TASK_NAME}".replace("\\", "\\\\"),
            check=False,
        )
        _ssh(
            "LANG=C /mnt/c/Windows/System32/schtasks.exe "
            f"/Delete /F /TN {TASK_NAME}".replace("\\", "\\\\"),
            check=False,
        )


def _fetch_raw(trade_date):
    raw_name = f"m02-czce-raw-{trade_date}.json"
    text = _ssh(f"cat {HOST_DIR_U}/{raw_name}").stdout
    return raw_name, json.loads(text)


def _bar_at(code_entry, hhmm="09:30:00"):
    for bar in code_entry.get("bars", []):
        stamp = str(bar.get("time", "")).replace("T", " ")
        if stamp.endswith(hhmm):
            return bar
    return None


def _daily_band(trade_date):
    """futures_daily [low, high] for the 19 CZCE contracts (read-only)."""
    from common.config import load_config, resolve_settings_path
    from common.db import get_connection, pg_config_from

    with get_connection(pg_config_from(load_config(resolve_settings_path()))) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT symbol, low, high FROM public.futures_daily "
                "WHERE trade_date = %s AND symbol = ANY(%s)",
                (trade_date, CZCE_CONTRACTS),
            )
            return {r[0]: (float(r[1]), float(r[2])) for r in cur.fetchall()}


def build_receipt(payload, trade_date, raw_name, *, synthetic=False):
    """Validate the raw wsi capture and return the receipt (fail-closed)."""
    if payload.get("status") != "ok":
        raise ValueError(f"host capture failed: {json.dumps(payload)[:400]}")
    if payload.get("trade_date") != trade_date:
        raise ValueError("raw capture trade_date mismatch")
    codes = payload.get("codes", {})
    missing = [c for c in CZCE_CONTRACTS if c not in codes]
    if missing:
        raise ValueError(f"raw capture missing codes: {missing}")
    prices = {}
    rejects = []
    for contract in CZCE_CONTRACTS:
        entry = codes[contract]
        if entry.get("errorcode") != 0:
            rejects.append(f"{contract}: wsi errorcode {entry.get('errorcode')}")
            continue
        bar = _bar_at(entry)
        if bar is None:
            rejects.append(f"{contract}: no 09:30:00 bar")
            continue
        open_price = bar.get("open")
        if open_price is None or float(open_price) <= 0:
            rejects.append(f"{contract}: non-positive open {open_price}")
            continue
        prices[contract] = float(open_price)
    if rejects:
        raise ValueError("capture rejects: " + "; ".join(rejects))

    band = _daily_band(trade_date)
    band_checked, band_pending = {}, []
    for contract in CZCE_CONTRACTS:
        low_high = band.get(contract)
        if low_high is None or low_high[0] is None or low_high[1] is None:
            band_pending.append(contract)
        elif not low_high[0] - 1e-9 <= prices[contract] <= low_high[1] + 1e-9:
            raise ValueError(
                f"{contract}: 09:30 open {prices[contract]} outside daily band "
                f"[{low_high[0]}, {low_high[1]}]"
            )
        else:
            band_checked[contract] = [low_high[0], low_high[1]]

    captured_at = payload["captured_at"]
    if "+" not in captured_at:  # host clock is China Standard Time
        captured_at += "+08:00"
    raw_bytes = json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()
    return {
        "schema": "m02-czce-wind-minute-receipt.v1",
        "synthetic": synthetic,
        "captured_at": captured_at,
        "trade_date": trade_date,
        "bar_time": "09:30:00",
        "price_field": "open",
        "source": (
            "WindPy wsi minute bars (BarSize=1) from a one-off session-1 "
            "process on the licensed workstation, scheduled via schtasks /IT; "
            "wsi labels bars by minute start, value-identical to "
            "public.futures_minute (cross-validated AG2612 2026-10-16 09:30)"
        ),
        "raw_artifact": raw_name,
        "raw_sha256": hashlib.sha256(raw_bytes).hexdigest(),
        "plausibility": {
            "futures_daily_band_checked": sorted(band_checked),
            "futures_daily_band_pending": sorted(band_pending),
        },
        "prices": {c: prices[c] for c in CZCE_CONTRACTS},
    }


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--trade-date", default="2026-10-20")
    parser.add_argument("--smoke", action="store_true", help="2 codes, no receipt")
    parser.add_argument(
        "--from-file",
        help="build receipt from a local raw JSON (rehearsal; no host run)",
    )
    parser.add_argument("--receipt-synthetic", action="store_true",
                        help="mark the receipt synthetic (rehearsal only)")
    args = parser.parse_args(argv)

    if args.from_file:
        raw_name, payload = Path(args.from_file).name, json.loads(
            Path(args.from_file).read_text()
        )
    else:
        _push_workers()
        stdout = _run_on_host(args.trade_date, args.smoke)
        if args.smoke:
            print(stdout[:3000])
            return 0
        raw_name, payload = _fetch_raw(args.trade_date)
        OUT_DIR.mkdir(parents=True, exist_ok=True)
        (OUT_DIR / raw_name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=1) + "\n"
        )

    receipt = build_receipt(
        payload, args.trade_date, raw_name, synthetic=args.receipt_synthetic
    )
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    receipt_path = OUT_DIR / f"czce-{args.trade_date}.json"
    receipt_path.write_text(json.dumps(receipt, ensure_ascii=False, indent=1) + "\n")
    print(f"receipt: {receipt_path}")
    for contract in CZCE_CONTRACTS:
        print(f"  {contract}: {receipt['prices'][contract]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
