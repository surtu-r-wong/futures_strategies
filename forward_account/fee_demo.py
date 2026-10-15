"""Replayable SYNTHETIC fee acceptance; temporary SQLite, never a real account.

The two independent versions use the same invented prices and integer lot rule.
Writes auditable inputs/states/ReturnFeed under a new test evidence directory.
"""

import argparse
from copy import deepcopy
import json
import math
from pathlib import Path
import tempfile

from .demo import make_inputs
from . import store


def _write(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )


def synthetic_model(contracts):
    def rate(value):
        return {"value": value, "unit": "YUAN_PER_LOT"}

    rule = {
        "rule_id": "SYNTHETIC-rule-1",
        "valid_from": "2026-10-12",
        "valid_to": "2026-10-14",
        "exchange_source": "SYNTHETIC invented rates, not exchange data",
        "broker_source": "SYNTHETIC invented additional commission",
        "slippage_source": "SYNTHETIC explicit zero assumption",
        "exchange": {
            "open": rate(2),
            "close": rate(3),
            "close_today": rate(7),
            "intraday_open": rate(1),
        },
        "broker": {
            k: rate(0.5) for k in ("open", "close", "close_today", "intraday_open")
        },
        "slippage": rate(0),
    }
    return {
        "version": "contract-fees-v1",
        "position_accounting": "dated-net-lots-v1",
        "close_order": "fifo",
        "broker_fee_basis": "additional_to_exchange",
        "intraday_open_policy": "ordinary_open_then_same_day_adjustment",
        "policy_source": "SYNTHETIC explicit same-day opening adjustment policy",
        "excluded_costs": ["SYNTHETIC: delivery and taxes not modeled"],
        "rules": {k: [deepcopy(rule)] for k in contracts},
    }


EXPECTED = {
    "uniform-bps-v1": {
        "fees": [20, 33.72],
        "nav": [11730, 12826.28],
        "arithmetic": [
            "10000 + 1750 - 20000*0.001 = 11730",
            "11730 + 350 - (16800+18*940)*0.001 + 13*5*12 = 12826.28",
        ],
    },
    "contract-fees-v1": {
        "fees": [50, 102.5],
        "nav": [11700, 12727.5],
        "arithmetic": [
            "10000 + 1750 - (15+5)*(2+0.5) = 11700",
            "11700 + 350 - 20*(3+0.5) - 13*(2+0.5) + 13*5*12 = 12727.5",
        ],
    },
}


def replay(root):
    root = Path(root)
    if root.exists():
        raise ValueError("synthetic evidence requires a new directory")
    root.mkdir(parents=True)
    audit = {}
    # This store is destroyed after the test; the JSON states are the portable ledger.
    with tempfile.TemporaryDirectory(prefix="m02-synthetic-") as tmp:
        db = Path(tmp) / "synthetic.sqlite"
        for version, expected in EXPECTED.items():
            folder = root / version
            config, calendar, packets = make_inputs(folder)
            config["account_id"] = "SYNTHETIC-" + version
            config["account_version"] = "SYNTHETIC-" + version
            config["lot_rule"] = "nearest_integer_ties_even"
            config["label"] = "M02 invented fee arithmetic / " + version
            if version == "contract-fees-v1":
                del config["cost_bps"]
                config["fee_model"] = synthetic_model(config["instruments"])
                config["fee_description"] = (
                    "SYNTHETIC separate exchange/broker/slippage"
                )
            _write(folder / "config.json", config)
            _write(folder / "packets.json", packets)
            initial = store.initialize(db, config, calendar)
            states = [store.advance(db, config["account_id"], p) for p in packets]
            for i, state in enumerate(states):
                for key in ("fees", "nav"):
                    if not math.isclose(state[key], expected[key][i], abs_tol=1e-8):
                        raise AssertionError(f"synthetic {version} {key} mismatch")
            if store.advance(db, config["account_id"], packets[-1]) != states[-1]:
                raise AssertionError("synthetic repeated settlement changed state")
            audit[version] = {"initial_state": initial, "states": states}
        feed = store.export_feed(db, root / "synthetic-returns.json", synthetic=True)
        if (
            len(feed["series"]) != 2
            or len({s["strategy"]["version"] for s in feed["series"]}) != 2
        ):
            raise AssertionError("fee versions must export separate series identities")
    _write(root / "synthetic-ledger.json", audit)
    _write(root / "hand-calculated-expected.json", EXPECTED)
    _write(
        root / "manifest.json",
        {
            "synthetic": True,
            "real_account_initialized": False,
            "sqlite": "temporary test store destroyed after replay",
            "engine_sha256": store.engine_fingerprint(),
            "ledger": "synthetic-ledger.json",
            "return_feed": "synthetic-returns.json",
            "expected": "hand-calculated-expected.json",
            "separate_series": 2,
            "economic_results_reproducible": True,
            "publication_timestamps": "actual replay time; timestamps differ on another replay",
        },
    )
    return feed


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args(argv)
    replay(args.output_dir)
    print(f"SYNTHETIC fee acceptance: {args.output_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
