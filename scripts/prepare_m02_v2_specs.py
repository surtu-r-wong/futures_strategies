"""Build config/m02_v2/product-specs.json and prove it reproduces v1.

    .venv/bin/python scripts/prepare_m02_v2_specs.py [--as-of 2026-10-28]

Read-only: one REPEATABLE READ snapshot of exchange_data.futures_settle_param
and public.futures_contract_info for `--as-of` (product listing) and for
2026-10-15 (the v1 build day). Writes the sealed table plus a build receipt
(query log, the v1 reproduction result, the rulings applied, the pending and
excluded product lists). Refuses to write when any of the 67 v1 contracts does
not re-derive identically.
"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from forward_inputs import m02_v2_specs as specs  # noqa: E402
from forward_inputs.m01 import ReadOnlySource  # noqa: E402
from forward_account.contracts import now_iso  # noqa: E402

REPO = Path(__file__).resolve().parents[1]
V1_CONFIG = REPO / "docs/requests/2026-10-17-m02-initialization/build/account.config.json"
V1_DAY = "2026-10-15"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as-of", required=True)
    parser.add_argument("--output", default=str(specs.TABLE_PATH))
    args = parser.parse_args(argv)
    v1 = json.loads(V1_CONFIG.read_text())
    with ReadOnlySource() as source:
        settle = specs.fetch(source.query, specs.SETTLE_SQL, args.as_of)
        info = specs.fetch(source.query, specs.INFO_SQL, args.as_of)
        settle_v1 = specs.fetch(source.query, specs.SETTLE_SQL, V1_DAY)
        info_v1 = specs.fetch(source.query, specs.INFO_SQL, V1_DAY)
    if not settle or not info:
        raise SystemExit(f"no source rows for {args.as_of}")
    table = specs.build_table(settle, info, as_of=args.as_of, v1_config=v1)
    diffs = specs.reproduce_v1(v1, table, settle_v1, info_v1)
    if diffs:
        print(json.dumps(diffs[:20], ensure_ascii=False, indent=1))
        raise SystemExit(f"v1 reproduction failed: {len(diffs)} field diffs")
    table = specs.seal_table(table)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(table, ensure_ascii=False, indent=1) + "\n")
    pending = {k: v["pending"] for k, v in table["products"].items() if v["pending"]}
    receipt = {
        "built_at": now_iso(),
        "as_of": args.as_of,
        "table_sha256": table["table_sha256"],
        "products": len(table["products"]),
        "closed": sum(v["status"] == "closed" for v in table["products"].values()),
        "pending": pending,
        "excluded": {
            k: v["excluded"] for k, v in table["products"].items() if v["status"] == "excluded"
        },
        "unit_rulings": [
            {**r, "products": sorted(r["products"])} for r in specs.UNIT_RULINGS
        ],
        "v1_reproduction": {
            "day": V1_DAY,
            "contracts": len(v1["instruments"]),
            "field_diffs": 0,
            "fields": "multiplier, margin_rate, exchange/broker fee values+units, slippage",
        },
        "queries": source.queries,
    }
    out.with_name("build-receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1, default=str) + "\n"
    )
    summary = {k: receipt[k] for k in ("products", "closed", "table_sha256")}
    summary.update(pending=len(pending), excluded=len(receipt["excluded"]))
    print(json.dumps(summary))
    for key, items in pending.items():
        print(f"PENDING {key}: {'; '.join(items)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
