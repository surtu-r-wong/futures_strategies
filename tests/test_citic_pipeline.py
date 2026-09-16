from datetime import date

import pandas as pd
import pytest

from citic_index.pipeline import ReplicaConfig, build_replica


DAYS = [date(2010, 1, 4), date(2010, 1, 5), date(2010, 1, 6), date(2010, 1, 7)]

# Each product carries three contracts.  Open interest never moves, so the
# dominant never changes and nothing rolls -- the wiring is what is under test,
# not the roll.  X2 is the dominant, X1 the only earlier delivery and X3 the
# only later one, four months apart.
_OI = {"X1": 500.0, "X2": 1000.0, "X3": 300.0}
_DELIVERY = {"X1": 202401, "X2": 202403, "X3": 202405}

# T1 compounds, T2 is flat, and the dominant only moves on the last day.
_CLOSES = {
    # product: {contract: [d1, d2, d3, d4]}
    "M": {"X1": [100.0, 110.0, 121.0, 121.0], "X2": [100.0] * 3 + [102.0], "X3": [100.0] * 4},
    "Y": {"X1": [100.0] * 4, "X2": [100.0] * 3 + [105.0], "X3": [100.0] * 4},
    "C": {"X1": [100.0, 90.0, 81.0, 81.0], "X2": [100.0] * 3 + [96.0], "X3": [100.0] * 4},
}


def _panel():
    rows = []
    for product, contracts in _CLOSES.items():
        for leg, closes in contracts.items():
            for day, close in zip(DAYS, closes):
                rows.append(
                    {
                        "trade_date": day,
                        "product": product,
                        "contract": f"{product}{str(_DELIVERY[leg])[2:]}.DCE",
                        "delivery_yyyymm": _DELIVERY[leg],
                        "close": close,
                        # Settlement one percent under the close on every bar, so
                        # the two return conventions are distinguishable.
                        "settle": close * 0.99,
                        "volume": 100.0,
                        "oi": _OI[leg],
                        "turnover": close * 100.0 * 10.0,
                    }
                )
    return pd.DataFrame(rows)


def _config(**overrides):
    base = dict(
        window=2,
        min_observations=2,
        liquidity_window=1,
        liquidity_threshold=0.0,
        min_listing_calendar_days=0,
        base_date=DAYS[0],
        # The hand-computed figures below are close-to-close; the settlement
        # convention gets its own test rather than being folded into all of them.
        return_basis="close_to_close",
        # They also strike the book at the signal day's price, so the weights
        # of d3 earn d4 and the four-day fixture reaches a return.  The T+1
        # default is asserted separately below.
        execution_lag=0,
    )
    base.update(overrides)
    return ReplicaConfig(**base)


def test_the_whole_pipeline_produces_the_hand_computed_index():
    result = build_replica(_panel(), _config())

    # T1 over d2..d3: M compounds 1.1*1.1 = 1.21 against a flat T2, so the raw
    # difference is 0.21 and the factor is 0.21/4 = 0.0525.  C mirrors it at
    # 0.9*0.9 = 0.81, giving -0.19/4 = -0.0475.  Y is flat at 0.
    factor = result.factor.set_index(["trade_date", "product"])["basis_momentum"]
    assert factor[(DAYS[2], "M")] == pytest.approx(0.0525)
    assert factor[(DAYS[2], "Y")] == pytest.approx(0.0)
    assert factor[(DAYS[2], "C")] == pytest.approx(-0.0475)

    # Ranked ascending over N=3: C, Y, M take ranks 1..3 and weights (r-2)/6.
    weights = result.weights.set_index(["trade_date", "product"])["weight"]
    assert weights[(DAYS[2], "C")] == pytest.approx(-1 / 6)
    assert weights[(DAYS[2], "Y")] == pytest.approx(0.0)
    assert weights[(DAYS[2], "M")] == pytest.approx(1 / 6)

    # Those weights earn d4's dominant returns: M +2%, Y +5%, C -4%.
    #   (1/6)(0.02) + 0(0.05) + (-1/6)(-0.04) = 0.01
    index = result.index.set_index("trade_date")
    assert index.loc[DAYS[0], "index_value"] == pytest.approx(1000.0)
    assert index.loc[DAYS[2], "index_value"] == pytest.approx(1000.0)
    assert index.loc[DAYS[3], "daily_return"] == pytest.approx(0.01)
    assert index.loc[DAYS[3], "index_value"] == pytest.approx(1010.0)


def test_the_default_strikes_the_book_a_day_after_the_signal():
    # Signal on d3, traded at d4's price, first return d4 -> d5.  The fixture
    # ends on d4, so under the default nothing has been earned yet; striking at
    # d3's own price -- the price the signal was computed on -- is lag 0.
    assert ReplicaConfig().execution_lag == 1
    result = build_replica(_panel(), _config(execution_lag=1))
    index = result.index.set_index("trade_date")
    assert index.loc[DAYS[3], "daily_return"] == pytest.approx(0.0)
    assert index.loc[DAYS[3], "index_value"] == pytest.approx(1000.0)
    # The weights themselves are the same book, only struck later.
    weights = result.weights.set_index(["trade_date", "product"])["weight"]
    assert weights[(DAYS[2], "M")] == pytest.approx(1 / 6)


def test_the_series_starts_at_the_base_date_even_with_no_return_that_day():
    # A published index is 1000 on its base date.  The first day carries no
    # return -- nothing has been struck yet -- so it only appears if the base
    # date is put on the calendar deliberately.
    result = build_replica(_panel(), _config())
    assert result.index["trade_date"].iloc[0] == DAYS[0]
    assert result.index["index_value"].iloc[0] == pytest.approx(1000.0)


def test_dropping_the_month_gap_division_changes_the_factor_but_not_the_ranking():
    # Deviation 2 scales every product by the same gap here, so it moves the
    # factor and leaves the cross-sectional order alone.  Worth pinning: it
    # means the division can only matter when gaps differ across products.
    result = build_replica(_panel(), _config(normalise_by_gap=False))
    factor = result.factor.set_index(["trade_date", "product"])["basis_momentum"]
    assert factor[(DAYS[2], "M")] == pytest.approx(0.21)
    weights = result.weights.set_index(["trade_date", "product"])["weight"]
    assert weights[(DAYS[2], "M")] == pytest.approx(1 / 6)


def test_the_t1_switch_reaches_the_factor():
    # With t1_leg="main" both legs of the difference become the dominant and the
    # later contract, and the dominant is flat until d4 -- so the factor that
    # was 0.0525 collapses to zero and the cross-section has nothing to rank.
    result = build_replica(_panel(), _config(t1_leg="main"))
    factor = result.factor.set_index(["trade_date", "product"])["basis_momentum"]
    assert factor[(DAYS[2], "M")] == pytest.approx(0.0)
    assert factor[(DAYS[2], "C")] == pytest.approx(0.0)


def test_a_product_outside_the_named_universe_is_ranked_out_not_dropped_from_the_factor():
    panel = _panel()
    panel.loc[panel["product"] == "C", "product"] = "SA"   # not one of the 37
    panel.loc[panel["product"] == "SA", "contract"] = panel.loc[
        panel["product"] == "SA", "contract"
    ].str.replace("^C", "SA", regex=True)
    result = build_replica(panel, _config())
    factor = result.factor.set_index(["trade_date", "product"])
    # The factor is still computed for SA -- 3.2 gates ranking, not measurement.
    assert factor.loc[(DAYS[2], "SA"), "basis_momentum"] == pytest.approx(-0.0475)
    assert bool(factor.loc[(DAYS[2], "SA"), "rankable"]) is False
    assert set(result.weights["product"]) == {"M", "Y"}


def test_the_settlement_basis_reaches_the_factor_and_the_index():
    # Settlement sits one percent under every close, so each leg return becomes
    # (1+r)/0.99 and a two-day product picks up 1/0.99**2 = 1/0.9801.
    #   T1: 1.21/0.9801 - 1 = 0.2345679,  T2: 1/0.9801 - 1 = 0.0203041
    #   BM = (0.2345679 - 0.0203041) / 4 = 0.0535660
    result = build_replica(_panel(), _config(return_basis="close_to_prev_settle"))
    factor = result.factor.set_index(["trade_date", "product"])["basis_momentum"]
    assert factor[(DAYS[2], "M")] == pytest.approx(0.05356596, abs=1e-8)

    # The accrual is the same for every product, so it cannot move a rank.
    weights = result.weights.set_index(["trade_date", "product"])["weight"]
    assert weights[(DAYS[2], "M")] == pytest.approx(1 / 6)
    assert weights[(DAYS[2], "C")] == pytest.approx(-1 / 6)

    # It does move the index: M's dominant earns 102/99 - 1 and C's 96/99 - 1,
    # so the day is (1/6)(0.0303030) + (-1/6)(-0.0303030) = 0.0101010.
    index = result.index.set_index("trade_date")
    assert index.loc[DAYS[3], "daily_return"] == pytest.approx(0.01010101, abs=1e-8)
    assert index.loc[DAYS[3], "index_value"] == pytest.approx(1010.10101, abs=1e-4)


def _receipts(series):
    """series: {product: [d1..d4]} laid on the shared DAYS."""
    rows = []
    for product, values in series.items():
        for day, value in zip(DAYS, values):
            rows.append(
                {"trade_date": day, "product": product, "receipts": float(value)}
            )
    return pd.DataFrame(rows)


def test_023_sorts_descending_so_growing_receipts_are_sold():
    """023's §3.5 step 2 sorts 从大到小; the ranking layer sorts ascending.

    The two papers differ here even though their step-3 weight formulas are
    word-for-word identical, which is exactly the trap: reusing the section
    wholesale gets the sign backwards.  Ranked the paper's way, the product
    whose receipts grew takes Rank 1 and the most negative weight -- 第 1 页's
    "选择仓单增加的商品做空" -- and the one whose receipts fell goes long.

    Measured consequence of getting it wrong: the full-history replica
    correlates -0.50 with the published series instead of +0.50.
    """
    receipts = _receipts(
        {
            "M": [100, 100, 100, 200],  # receipts doubled -> should be sold
            "Y": [100, 100, 100, 100],  # flat -> middle
            "C": [100, 100, 100, 50],   # receipts halved -> should be bought
        }
    )
    result = build_replica(
        _panel(),
        _config(
            factor_kind="warehouse_receipt",
            window=1,
            min_observations=1,
            baseline_lag=2,
            baseline_window=1,
        ),
        receipts=receipts,
    )
    last = result.weights[result.weights["trade_date"] == DAYS[-1]]
    weights = last.set_index("product")["weight"]
    assert weights["M"] < 0 < weights["C"], (
        f"growing receipts must be sold: {weights.to_dict()}"
    )

