import pandas as pd
import pytest

from cta_carry.targets import infer_product_multipliers, lots_for_targets


def test_infer_product_multipliers_takes_the_median_ratio_of_recent_traded_rows() -> None:
    prices = pd.DataFrame(
        [
            # turnover / (volume * close): 10, 10, 30 -> median 10; a zero-volume row is ignored
            {"trade_date": pd.Timestamp("2024-01-02"), "product": "RB", "contract": "RB2405", "close": 100.0, "volume": 5.0, "turnover": 5_000.0},
            {"trade_date": pd.Timestamp("2024-01-03"), "product": "RB", "contract": "RB2405", "close": 100.0, "volume": 2.0, "turnover": 2_000.0},
            {"trade_date": pd.Timestamp("2024-01-04"), "product": "RB", "contract": "RB2405", "close": 100.0, "volume": 1.0, "turnover": 3_000.0},
            {"trade_date": pd.Timestamp("2024-01-05"), "product": "RB", "contract": "RB2405", "close": 100.0, "volume": 0.0, "turnover": 0.0},
            {"trade_date": pd.Timestamp("2024-01-02"), "product": "CU", "contract": "CU2402", "close": 70_000.0, "volume": 4.0, "turnover": 1_400_000.0},
        ]
    )

    multipliers = infer_product_multipliers(prices, window=60)

    assert multipliers == {"RB": 10.0, "CU": 5.0}


def test_lots_for_targets_rounds_notional_over_contract_value() -> None:
    targets = pd.DataFrame(
        [
            {"product": "RB", "contract": "RB2405", "close": 100.0, "target_weight": 0.1},
            {"product": "CU", "contract": "CU2402", "close": 70_000.0, "target_weight": -0.0349},
            {"product": "ZZ", "contract": "ZZ2405", "close": 50.0, "target_weight": 0.02},
        ]
    )

    sized = lots_for_targets(targets, capital=1_000_000.0, multipliers={"RB": 10.0, "CU": 5.0})

    # 0.1 * 1e6 / (100 * 10) = 100 lots; -0.0349 * 1e6 / (70000 * 5) = -0.0997 -> 0 lots
    assert sized["multiplier"].tolist()[:2] == [10.0, 5.0]
    assert sized["notional"].tolist()[:2] == pytest.approx([100_000.0, -34_900.0])
    assert sized["lots"].tolist()[:2] == [100, 0]
    assert pd.isna(sized["multiplier"].iloc[2]) and pd.isna(sized["lots"].iloc[2])


@pytest.mark.parametrize(
    "contract",
    [
        "PL2611.CZC",  # Zhengzhou keeps its four-digit year: no PL611 rewrite
        "M2701.DCE",   # no lower-casing either: the Wind code goes out as stored
        "RU2701.SHF",
        "SC2610.INE",
        "LC2701.GFE",
        "A2410",       # no exchange suffix (synthetic panels): unchanged
    ],
)
def test_order_code_is_the_wind_contract_code(contract) -> None:
    # User ruling 2026-09-16: the desk keys orders by the Wind code, suffix
    # included; the bare exchange instrument id could not be used directly.
    from cta_carry.targets import order_code

    assert order_code(contract) == contract
