"""M02 v2 specification table, morning precheck, and producer/consumer format."""

import json
from copy import deepcopy
from pathlib import Path

import pandas as pd
import pytest

from forward_inputs import m02_daily
from forward_inputs import m02_v2_specs as specs

FIXTURES = Path(__file__).parent / "fixtures"
REPO = Path(__file__).resolve().parents[1]
V1_CONFIG = REPO / "docs/requests/2026-10-17-m02-initialization/build/account.config.json"


@pytest.fixture(scope="module")
def v1():
    return json.loads(V1_CONFIG.read_text())


@pytest.fixture(scope="module")
def rows():
    return json.loads((FIXTURES / "m02_v2_v1_rows_2026-10-15.json").read_text())


@pytest.fixture(scope="module")
def table(v1, rows):
    return specs.seal_table(
        specs.build_table(rows["settle"], rows["info"], as_of="2026-10-15", v1_config=v1)
    )


def test_codes_map_to_exchange_native_ids():
    assert specs.split_code("CF2701.CZC") == ("CZCE", "CF", "CF2701")
    assert specs.source_id("CF2701.CZC") == "CF701"
    assert specs.source_id("I2701.DCE") == "I2701"
    assert specs.source_id("SC2612.INE") == "SC2612"
    with pytest.raises(specs.SpecMissing):
        specs.split_code("IF2612.CFE")
    with pytest.raises(specs.SpecMissing):
        specs.split_code("CF701.CZC")


def test_last_trade_date_parses_both_source_shapes():
    assert specs.last_trade_date({"last_date": 20261113.0}) == "2026-11-13"
    assert specs.last_trade_date({"last_date": "20261228.0"}) == "2026-12-28"


def test_v1_contracts_rederive_field_for_field(v1, table, rows):
    """All 67 closed v1 contracts: fee values/units, broker, slippage, margin,
    multiplier identical when re-derived from their 2026-10-15 rows."""
    assert specs.reproduce_v1(v1, table, rows["settle"], rows["info"]) == []
    assert all(p["status"] == "closed" for p in table["products"].values())
    assert len(table["products"]) == 67


def test_derivation_uses_declared_scale_then_ruling(table, rows):
    settle = {(r["exchange"], r["contract_id"]): r for r in rows["settle"]}
    info = {(r["exchange"], r["contract_id"]): r for r in rows["info"]}
    spec, rules = specs.derive(
        "I2701.DCE", "2026-11-16", table, settle[("DCE", "I2701")], info[("DCE", "I2701")]
    )
    assert spec["valid_from"] == "2026-11-16" and spec["valid_to"] == "2027-01-15"
    assert spec["margin_rate"] == pytest.approx(0.08 + 0.02)
    assert rules[0]["exchange"]["open"] == {"value": 1.0, "unit": "PER_TEN_THOUSAND"}
    assert rules[0]["broker"]["open"] == {"value": 0.1, "unit": "PER_TEN_THOUSAND"}
    assert "比例的单位均为万分" in rules[0]["exchange_source"]
    _, rules = specs.derive(
        "AG2612.SHF", "2026-11-16", table, settle[("SHFE", "AG2612")], info[("SHFE", "AG2612")]
    )
    assert rules[0]["exchange"]["close_today"] == {"value": 0.025, "unit": "PERMILLE"}


def test_pending_product_and_bad_rows_raise(table, rows):
    settle = {(r["exchange"], r["contract_id"]): r for r in rows["settle"]}
    info = {(r["exchange"], r["contract_id"]): r for r in rows["info"]}
    key = ("CZCE", "CF701")
    pending = deepcopy(table)
    pending["products"]["CZCE:CF"].update(status="pending", pending=["unit not ruled"])
    with pytest.raises(specs.SpecMissing, match="pending"):
        specs.derive("CF2701.CZC", "2026-11-16", pending, settle[key], info[key])
    with pytest.raises(specs.SpecMissing, match="no settlement"):
        specs.derive("CF2701.CZC", "2026-11-16", table, None, info[key])
    doubled = {**info[key], "multiplier": 10}
    with pytest.raises(specs.SpecMissing, match="multiplier"):
        specs.derive("CF2701.CZC", "2026-11-16", table, settle[key], doubled)
    undeclared = {**settle[key], "fee_style": "比例值"}
    with pytest.raises(specs.SpecMissing, match="fee unit"):
        specs.derive("CF2701.CZC", "2026-11-16", table, undeclared, info[key])
    with pytest.raises(specs.SpecMissing, match="last trading day"):
        specs.derive("CF2701.CZC", "2027-02-01", table, settle[key], info[key])
    with pytest.raises(specs.SpecMissing, match="not in the M02 v2 table"):
        specs.derive("ZZ2701.CZC", "2026-11-16", table, settle[key], info[key])


def _with_product(rows, exchange, contract_id, product_id, fee_style, multiplier):
    settle = deepcopy(rows["settle"]) + [
        {**rows["settle"][0], "exchange": exchange, "contract_id": contract_id,
         "product_id": product_id, "fee_style": fee_style, "fee_scale": None}
    ]
    info = deepcopy(rows["info"]) + [
        {**rows["info"][0], "exchange": exchange, "contract_id": contract_id,
         "multiplier": multiplier}
    ]
    return settle, info


def test_ruling_reaches_only_the_products_it_named(v1, rows):
    """A DCE ratio-style product no ruling named stays pending: generalizing a
    ruling beyond the products it named is a user decision."""
    settle, info = _with_product(rows, "DCE", "QQ2701", "qq", "比例值", 10)
    table = specs.build_table(settle, info, as_of="2026-10-15", v1_config=v1)
    qq = table["products"]["DCE:QQ"]
    assert qq["status"] == "pending" and qq["excluded"] is None
    assert "rulings covered" in qq["pending"][0] and "LG" in qq["pending"][0]


@pytest.mark.parametrize(
    "exchange, contract_id, product, fee_style, unit",
    [("DCE", "LG2701", "LG", "比例值", "PER_TEN_THOUSAND"),
     ("DCE", "BB2701", "BB", "比例值", "PER_TEN_THOUSAND"),
     ("CZCE", "CY701", "CY", "绝对值", "YUAN_PER_LOT"),
     ("CZCE", "ZC701", "ZC", "绝对值", "YUAN_PER_LOT")],
)
def test_2026_10_29_ruling_closes_its_products(v1, rows, exchange, contract_id, product, fee_style, unit):
    settle, info = _with_product(rows, exchange, contract_id, product.lower(), fee_style, 90)
    table = specs.build_table(settle, info, as_of="2026-10-15", v1_config=v1)
    row = table["products"][f"{exchange}:{product}"]
    assert row["status"] == "closed" and row["pending"] == []
    assert row["fee_unit_ruling"]["unit"] == unit
    assert row["fee_unit_ruling"]["authority"].startswith("user_decision_2026-10-29")
    assert "ruling-01.md" in row["fee_unit_ruling"]["source"]


def test_monthly_average_products_are_excluded_not_pending(v1, rows):
    settle, info = _with_product(rows, "DCE", "L-F2701", "l-f", "绝对值", 5)
    settle[-1]["fee_scale"] = "YUAN_PER_LOT"
    table = specs.build_table(settle, info, as_of="2026-10-15", v1_config=v1)
    row = table["products"]["DCE:L-F"]
    assert row["status"] == "excluded" and row["pending"] == []
    assert row["excluded"]["reason"].startswith("user_decision_2026-10-29")
    # an excluded product never derives, whatever its contract code looks like
    fake = {"products": {"DCE:I": {**table["products"]["DCE:I"], "status": "excluded",
                                   "excluded": row["excluded"]}}}
    s_row = next(r for r in rows["settle"] if r["contract_id"] == "I2701")
    i_row = next(r for r in rows["info"] if r["contract_id"] == "I2701")
    with pytest.raises(specs.SpecMissing, match="product excluded .*monthly-average"):
        specs.derive("I2701.DCE", "2026-11-16", fake, s_row, i_row)


def test_precheck_reports_missing_specs_and_expiry(table, rows):
    targets = pd.DataFrame(
        {"contract": ["I2701.DCE", "B2611.DCE", "ZZ2701.CZC", "CS2611.DCE"],
         "target_weight": [0.1, 0.05, 0.02, 0.0]}
    )
    check = m02_daily.precheck(
        targets, "2026-11-13", "2026-11-12", table, rows["settle"], rows["info"],
        held={"B2611.DCE": 3, "CS2611.DCE": -2},
    )
    assert set(check["spec_missing"]) == {"ZZ2701.CZC"}
    alerts = {a["contract"]: a for a in check["expiry_alerts"]}
    # B2611 last trades 2026-11-13 and the target still holds it
    assert alerts["B2611.DCE"]["last_trade_date"] == "2026-11-13"
    # CS2611 also expires 11-13 but the target has rolled it out (weight 0)
    assert "CS2611.DCE" not in alerts
    assert check["ok"] is False


def test_real_package_is_readable_by_the_nightly(table):
    """The 2026-10-28 morning run's package (copied verbatim) passes the
    nightly's classification for the interval it was produced for."""
    import sys

    sys.path.insert(0, str(REPO / "scripts"))
    import m02_v2_nightly as nightly
    from forward_account.contracts import SOURCE_RULES

    package = FIXTURES / "m02_v2_package_2026-10-29"
    manifest = json.loads((package / "manifest.json").read_text())
    root = package.parent / "pkg-root"
    interval = {
        "trade_date": "2026-10-29",
        "signal_date": "2026-10-28",
        "start_at": "2026-10-28T15:00:00+08:00",
        "execution_at": "2026-10-29T09:30:00+08:00",
        "end_at": "2026-10-29T15:00:00+08:00",
    }
    config = {
        "source_version": manifest["source_version"],
        "synthetic": False,
        "source_parameters": dict(SOURCE_RULES["citic027_r150_t1"][1]),
    }
    link = root / "2026-10-29"
    root.mkdir(exist_ok=True)
    try:
        if not link.exists():
            link.symlink_to(package.resolve())
        target, missing = nightly.classify_target(root, interval, config)
    finally:
        if link.is_symlink():
            link.unlink()
        root.rmdir()
    assert missing is None
    assert target["row_count"] == 67 and target["signal_date"] == "2026-10-28"
    assert sum(1 for w in target["weights"].values() if w) == 66
