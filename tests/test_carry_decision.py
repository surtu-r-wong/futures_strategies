from datetime import date

import pandas as pd
import pytest

from cta_carry.decision import build_daily_research, plan_signal_targets
from cta_carry.risk import PositionState
from tests.carry_fixtures import make_carry_panel, small_config


def _signal(direction=1, contract="A2405.SHF", strength=1.0):
    return pd.DataFrame(
        [
            {
                "trade_date": date(2024, 1, 2),
                "product": "A",
                "effective_direction": direction,
                "main_contract": contract,
                "strength": strength,
                "main_close": 100.0,
                "atr": 2.0,
            }
        ]
    )


def test_build_daily_research_returns_aligned_curve_atr_and_signals():
    data = make_carry_panel(periods=24)
    research = build_daily_research(data.prices, small_config())

    assert not research.curve_result.curve.empty
    assert not research.contract_atr.empty
    assert research.signal_result.signal_ready_date is not None
    assert set(research.contract_atr["trade_date"]) <= set(data.prices["trade_date"])


def test_signal_target_planning_preserves_a_post_stop_tranche_count():
    config = small_config()
    states = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=2,
            highest_high=110.0,
        )
    }

    plan = plan_signal_targets(
        states,
        _signal(),
        config,
        previous_states=states,
        reason_hints={"A": "stop_1"},
    )

    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "stop_1"}
    assert plan.raw_weights["A2405.SHF"] > 0.0


def test_direction_reversal_takes_precedence_over_stop_reason():
    config = small_config()
    previous = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=2,
        )
    }

    plan = plan_signal_targets(
        previous,
        _signal(direction=-1),
        config,
        previous_states=previous,
        reason_hints={"A": "stop_1"},
    )

    assert plan.states["A"].direction == -1
    assert plan.states["A"].tranches_remaining == config.stop_tranches
    assert plan.reasons == {"A": "direction_reversal"}


def test_first_missing_signal_creates_a_signal_exit_target():
    config = small_config()
    states = {
        "A": PositionState(
            direction=1,
            contract="A2405.SHF",
            tranches_remaining=config.stop_tranches,
        )
    }

    plan = plan_signal_targets(
        states,
        pd.DataFrame(columns=_signal().columns),
        config,
    )

    assert plan.states["A"].direction == 0
    assert plan.states["A"].contract is None
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}


def _rank_rows(carries, strengths=None):
    strengths = strengths or {}
    rows = []
    for product, carry_ma in carries.items():
        rows.append(
            {
                "trade_date": date(2024, 1, 2),
                "product": product,
                "main_contract": f"{product}2405",
                "carry_ma": carry_ma,
                "input_ready": True,
                "strength": strengths.get(product, 1.0),
                "main_close": 100.0,
                "atr": 2.0,
            }
        )
    frame = pd.DataFrame(rows)
    # what build_signals would set under rank_linear: sign of the centred rank
    order = frame.sort_values(["carry_ma", "product"])["product"].tolist()
    centre = (len(order) + 1) / 2
    direction = {p: int((i + 1 > centre) - (i + 1 < centre)) for i, p in enumerate(order)}
    frame["effective_direction"] = [
        direction[p] if strengths.get(p, 1.0) > 0 else 0 for p in frame["product"]
    ]
    return frame


def test_rank_linear_sizing_uses_rank_weight_times_strength_not_the_atr_budget():
    config = small_config(weighting="rank_linear", trend_filter_enabled=False)
    rows = _rank_rows({"A": -0.3, "B": 0.4, "C": -0.1, "D": 0.2, "E": 0.05}, {"D": 0.5})

    plan = plan_signal_targets({}, rows, config)

    assert plan.raw_weights == pytest.approx(
        {"A2405": -2 / 15, "B2405": 2 / 15, "C2405": -1 / 15, "D2405": 0.5 / 15}
    )
    assert plan.states["E"].direction == 0
    assert plan.reasons["A"] == "entry"


def test_rank_linear_sizing_is_independent_of_close_and_atr():
    config = small_config(weighting="rank_linear", trend_filter_enabled=False)
    rows = _rank_rows({"A": -0.3, "B": 0.4, "C": -0.1, "D": 0.2, "E": 0.05})
    rows["atr"] = 7.0
    rows["main_close"] = 1_000.0

    plan = plan_signal_targets({}, rows, config)

    assert plan.raw_weights["B2405"] == pytest.approx(2 / 15)


def test_blended_sizing_reads_the_weight_signals_struck_not_a_fresh_carry_rank():
    # signals decided the side from the blend, so decision has to size from the
    # same number: re-ranking carry_ma here would size D against the side its
    # position was opened on.
    config = small_config(
        weighting="rank_linear",
        trend_filter_enabled=False,
        basis_momentum_weight=0.5,
    )
    rows = _rank_rows({"A": -0.3, "B": 0.4, "C": -0.1, "D": 0.2, "E": 0.05})
    blend = {"A": 0.3, "B": -0.2, "C": 0.1, "D": -0.15, "E": -0.05}
    rows["blend_weight"] = [blend[product] for product in rows["product"]]
    rows["effective_direction"] = [
        int(blend[product] > 0) - int(blend[product] < 0) for product in rows["product"]
    ]

    plan = plan_signal_targets({}, rows, config)

    assert plan.raw_weights == pytest.approx(
        {
            "A2405": 0.3,
            "B2405": -0.2,
            "C2405": 0.1,
            "D2405": -0.15,
            "E2405": -0.05,
        }
    )
