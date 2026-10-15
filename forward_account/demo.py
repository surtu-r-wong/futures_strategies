"""SYNTHETIC fixtures only: invented prices, capital, margin, fees and contracts."""

import argparse
import csv
import hashlib
import json
from pathlib import Path

from .adapters import read_targets
from .contracts import SOURCE_RULES, digest


def _write(path, value):
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def make_inputs(root, adapter="citic027_r150_t1"):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    calendar = {
        "calendar_id": "synthetic-two-intervals-v1",
        "timezone": "Asia/Shanghai",
        "source": "SYNTHETIC explicit test sessions, not an exchange calendar",
        "complete": True,
        "coverage_start": "2026-10-13",
        "coverage_end": "2026-10-14",
        "intervals": [
            {
                "trade_date": f"2026-10-{day}",
                "signal_date": f"2026-10-{day - 1}",
                "start_at": f"2026-10-{day - 1}T15:00:00+08:00",
                "execution_at": f"2026-10-{day}T09:00:00+08:00",
                "end_at": f"2026-10-{day}T15:00:00+08:00",
            }
            for day in (13, 14)
        ],
    }
    config = {
        "account_id": f"synthetic-{adapter}",
        "account_version": f"synthetic-{adapter}-v1",
        "adapter": adapter,
        "source_version": "SYNTHETIC-source-v1",
        "source_parameters": dict(SOURCE_RULES[adapter][1]),
        "label": adapter,
        "mode": "paper",
        "currency": "CNY",
        "synthetic": True,
        "initial_at": "2026-10-12T15:00:00+08:00",
        "initial_capital": 10000.0,
        "initial_unit_nav": 1.0,
        "cost_bps": 10.0,
        "fee_description": "SYNTHETIC all-in commission/slippage; no other fees modeled",
        "lot_rule": "fractional",
        "cash_rule": "no_interest_no_external_flows",
        "financing_rule": "no_borrowing",
        "execution_rule": "calendar_day_open",
        "valuation_rule": "daily_settlement",
        "calendar_id": calendar["calendar_id"],
        "calendar_sha256": digest(calendar),
        "instruments": {
            name: {
                "asset_type": "linear_futures",
                "currency": "CNY",
                "multiplier": mult,
                "margin_rate": 0.1,
                "valid_from": "2026-10-12",
                "valid_to": "2026-10-14",
                "source": "SYNTHETIC test specification",
            }
            for name, mult in (("A.TEST", 10), ("B.TEST", 5))
        },
    }
    _write(root / "config.json", config)
    _write(root / "calendar.json", calendar)
    metadata = root / "source-parameters.json"
    _write(metadata, config["source_parameters"])
    packets = []
    for day, weights, execution, settlement in (
        (
            13,
            {"A.TEST": 1.5, "B.TEST": -0.5},
            {"A.TEST": 100, "B.TEST": 200},
            {"A.TEST": 110, "B.TEST": 190},
        ),
        (
            14,
            {"A.TEST": 0, "B.TEST": 1},
            {"A.TEST": 112, "B.TEST": 188},
            {"B.TEST": 200},
        ),
    ):
        target_path = root / f"targets-{day}.csv"
        with target_path.open("w", newline="") as f:
            writer = csv.DictWriter(
                f,
                fieldnames=[
                    "signal_date",
                    "contract",
                    "target_weight",
                    "vol_scale",
                    "lots",
                    "notional",
                ],
            )
            writer.writeheader()
            for k, w in weights.items():
                writer.writerow(
                    {
                        "signal_date": f"2026-10-{day - 1}",
                        "contract": k,
                        "target_weight": w,
                        "vol_scale": 99,
                        "lots": 999,
                        "notional": 999999,
                    }
                )
        receipt = {
            "source_version": config["source_version"],
            "synthetic": True,
            "available_at": f"2026-10-{day - 1}T16:00:00+08:00",
            "row_count": len(weights),
            "signal_date": f"2026-10-{day - 1}",
            "csv_sha256": hashlib.sha256(target_path.read_bytes()).hexdigest(),
            "metadata_sha256": hashlib.sha256(metadata.read_bytes()).hexdigest(),
        }
        _write(root / f"receipt-{day}.json", receipt)
        packet = {
            "trade_date": f"2026-10-{day}",
            "target": read_targets(config, target_path, metadata, receipt),
        }
        for kind, prices, hour in (
            ("execution", execution, "09"),
            ("settlement", settlement, "15"),
        ):
            packet[kind] = {
                "kind": kind,
                "at": f"2026-10-{day}T{hour}:00:00+08:00",
                "available_at": f"2026-10-{day}T{hour}:10:00+08:00",
                "source": "SYNTHETIC hand-calculated prices",
                "synthetic": True,
                "prices": prices,
            }
            if kind == "execution":
                packet[kind]["tradable"] = dict.fromkeys(prices, True)
        packets.append(packet)
        # The CLI ingests native artifacts, not a fabricated research return.
        raw = {k: v for k, v in packet.items() if k != "target"}
        raw["target_csv"] = target_path.name
        raw["source_metadata"] = metadata.name
        raw["source_receipt"] = f"receipt-{day}.json"
        _write(root / f"interval-{day}.json", raw)
    return config, calendar, packets


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    root = Path(args.output_dir).resolve()
    if root.exists():
        raise ValueError(
            "demo requires a new directory; do not overwrite an earlier acceptance run"
        )
    from .store import advance, export_feed, initialize

    db = root / "synthetic.sqlite"
    summary = {}
    for adapter in SOURCE_RULES:
        config, calendar, packets = make_inputs(root / adapter, adapter)
        initialize(db, config, calendar)
        states = [advance(db, config["account_id"], p) for p in packets]
        assert advance(db, config["account_id"], packets[-1]) == states[-1]
        summary[adapter] = states
    export_feed(db, root / "synthetic-returns.json", synthetic=True)
    _write(root / "synthetic-account-audit.json", summary)
    print(f"SYNTHETIC acceptance artifacts: {root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
