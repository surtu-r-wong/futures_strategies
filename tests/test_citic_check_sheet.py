import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from check_citic027_sheet import check  # noqa: E402


CAPITAL = 1e8


def _sheet(tmp_path, *, rows=None, meta=None) -> Path:
    base = pd.DataFrame(
        {
            "signal_date": "2026-09-10",
            "product": ["M", "Y"],
            "contract": ["M2701.DCE", "Y2701.DCE"],
            "order_code": ["m2701", "y2701"],
            "direction": [1, -1],
            "close": [3000.0, 9000.0],
            "multiplier": [10.0, 10.0],
            "raw_weight": [0.05, -0.05],
            "vol_scale": 5.0,
            "target_weight": [0.25, -0.25],
        }
    )
    base["notional"] = base["target_weight"] * CAPITAL
    base["lots"] = (base["notional"] / (base["close"] * base["multiplier"])).round().astype(int)
    if rows is not None:
        base = rows(base)

    defaults = {
        "vol_window": 252, "vol_window_days": 252, "vol_window_min_products": 40,
        "vol_window_median_products": 60.0, "vol_window_realised_vol": 0.03,
        "min_products": 5, "max_gross_leverage": 4.0, "capital": CAPITAL,
        "signal_date": "2026-09-10", "vol_scale": 5.0,
        "gross_exposure": float(base["target_weight"].abs().sum()),
        "products": len(base),
    }
    if meta:
        defaults.update(meta)

    prefix = tmp_path / "citic027_20260910"
    base.to_csv(f"{prefix}_next_targets.csv", index=False)
    Path(f"{prefix}_config.json").write_text(json.dumps(defaults))
    return prefix


def test_a_healthy_sheet_is_accepted(tmp_path):
    assert check(_sheet(tmp_path)) == []


def test_a_short_volatility_window_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, meta={"vol_window_days": 187}))
    assert any("not converged" in m for m in bad)


def test_a_window_holding_an_empty_cross_section_is_rejected(tmp_path):
    # The defect this file exists for: a vol window containing days on which
    # almost nothing was held reads the volatility low, levers against it, and
    # leaves the last day looking perfectly normal.
    bad = check(_sheet(tmp_path, meta={"vol_window_min_products": 2}))
    assert any("mixes a real book with an empty one" in m for m in bad)


def test_gross_over_the_cap_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, rows=lambda d: d.assign(target_weight=[3.0, -3.0])))
    assert any("over the" in m and "cap" in m for m in bad)


def test_a_book_that_does_not_net_to_zero_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, rows=lambda d: d.assign(target_weight=[0.25, -0.10])))
    assert any("net exposure" in m for m in bad)


def test_notional_that_does_not_match_capital_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, rows=lambda d: d.assign(notional=d["notional"] * 1.1)))
    assert any("notional" in m for m in bad)


def test_lots_that_do_not_match_the_contract_value_are_rejected(tmp_path):
    # Recomputed rather than trusted: this is the one number that gets typed
    # into an order ticket.
    bad = check(_sheet(tmp_path, rows=lambda d: d.assign(lots=d["lots"] + 1)))
    assert any("lots do not equal" in m for m in bad)


def test_a_duplicated_product_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, rows=lambda d: pd.concat([d, d.iloc[[0]]], ignore_index=True)))
    assert any("duplicated" in m for m in bad)


def test_a_sheet_dated_differently_from_the_run_is_rejected(tmp_path):
    bad = check(_sheet(tmp_path, meta={"signal_date": "2026-09-09"}))
    assert any("but the run says" in m for m in bad)


def test_every_failure_is_reported_not_just_the_first(tmp_path):
    bad = check(
        _sheet(
            tmp_path,
            rows=lambda d: d.assign(target_weight=[3.0, -2.0]),
            meta={"vol_window_days": 100},
        )
    )
    # Short window, over the cap, and not netting to zero: three separate
    # problems, and a checker that stopped at the first would hide two.
    assert len(bad) >= 3


def test_zhengzhou_codes_must_be_three_digits(tmp_path):
    # The exchange quotes a one-digit year, so an order ticket carrying the
    # four-digit form is rejected at entry.  The carry sheet has always done
    # this; the check exists so it cannot quietly stop.
    def four_digit(d):
        return d.assign(
            contract=["MA2701.CZC", "Y2701.DCE"],
            order_code=["MA2701", "y2701"],
        )

    bad = check(_sheet(tmp_path, rows=four_digit))
    assert any("MA2701" in m and "three digits" in m for m in bad)


def test_a_correct_zhengzhou_code_passes(tmp_path):
    def czce(d):
        return d.assign(
            contract=["MA2701.CZC", "Y2701.DCE"],
            order_code=["MA701", "y2701"],
        )

    assert check(_sheet(tmp_path, rows=czce)) == []


def test_the_other_exchanges_must_be_four_digits_lower_case(tmp_path):
    def upper(d):
        return d.assign(
            contract=["M2701.DCE", "Y2701.DCE"],
            order_code=["M2701", "y2701"],
        )

    bad = check(_sheet(tmp_path, rows=upper))
    assert any("M2701" in m for m in bad)
