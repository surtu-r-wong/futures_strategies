from datetime import date

import pandas as pd

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
