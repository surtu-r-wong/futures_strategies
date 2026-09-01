"""What the Bollinger workbook, chart, and audit file are required to say."""

from __future__ import annotations

from datetime import date, datetime, time
import json
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd
import pytest

from cta_bollinger.backtest import BacktestResult
from cta_bollinger.report import (
    FIDELITY_RULE_IDS,
    IN_SAMPLE_END,
    PAPER_METRICS,
    REPORT_SHEETS,
    write_outputs,
)


TZ = ZoneInfo("Asia/Shanghai")


def _daily() -> pd.DataFrame:
    days = [stamp.date() for stamp in pd.bdate_range("2021-08-02", "2021-11-30")]
    returns = [0.004 if index % 3 else -0.003 for index in range(len(days))]
    return pd.DataFrame(
        {
            "trade_date": days,
            "month_start": [day.replace(day=1) for day in days],
            "gross_return": returns,
            "turnover": [0.1] * len(days),
            "cost": [1.3e-5] * len(days),
            "direct_cost": [1.3e-5] * len(days),
            "net_return": returns,
            "gross_equity": np.cumprod([1.0 + value for value in returns]),
            "equity": np.cumprod([1.0 + value for value in returns]),
            "gross_leverage": [0.8] * len(days),
            "prevol_net_return": returns,
            "prevol_equity": np.cumprod([1.0 + value for value in returns]),
            "realized_vol": [0.12] * len(days),
            "vol_multiplier": [0.10 / 0.12] * len(days),
            "selected_count": [2] * len(days),
        }
    )


def _result() -> BacktestResult:
    daily = _daily()
    day = daily["trade_date"].iloc[0]
    positions = pd.DataFrame(
        [
            {
                "trade_date": day,
                "month_start": day.replace(day=1),
                "product": "RB",
                "contract": "RB2110.SHF",
                "selected": True,
                "universe_weight": 0.5,
                "direction": 1,
                "oi_scale": 1.0,
                "atr_leverage": 2.0,
                "realized_vol": 0.12,
                "target_annual_vol": 0.10,
                "vol_multiplier": 0.10 / 0.12,
                "leverage": 1.6667,
                "target_weight": 0.8333,
                "actual_weight": 0.8333,
                "prevol_weight": 1.0,
            }
        ]
    )
    trades = pd.DataFrame(
        [
            {
                "timestamp": pd.Timestamp(
                    datetime.combine(day, time(14, 50), tzinfo=TZ)
                ),
                "trade_date": day,
                "product": "RB",
                "contract": "RB2110.SHF",
                "reason": "upper_cross",
                "price": 5000.0,
                "old_weight": 0.0,
                "new_weight": 0.8333,
                "weight_change": 0.8333,
                "turnover": 0.8333,
                "cost": 0.8333 * 1.3 / 10_000.0,
            }
        ]
    )
    signals = pd.DataFrame(
        [
            {
                "product": "RB",
                "trade_date": day,
                "month_start": day.replace(day=1),
                "slot_end": pd.Timestamp(
                    datetime.combine(day, time(14, 45), tzinfo=TZ)
                ),
                "contract": "RB2110.SHF",
                "action": "upper_cross",
                "selected": True,
            }
        ]
    )
    selection = pd.DataFrame(
        [
            {
                "month_start": day.replace(day=1),
                "product": "RB",
                "in_liquidity_universe": True,
                "has_score": True,
                "observations": 252,
                "trade_count": 9,
                "cumulative_return": 0.21,
                "sharpe": 1.1,
                "calmar": 1.4,
                "eligible": True,
                "selected": True,
                "reason": "selected",
            }
        ]
    )
    quality = pd.DataFrame(
        [{"metric": "bars", "product": "RB", "value": 1234.0}],
        columns=["metric", "product", "value"],
    )
    return BacktestResult(
        daily=daily,
        positions=positions,
        trades=trades,
        signals=signals,
        selection=selection,
        data_quality=quality,
    )


@pytest.fixture
def result() -> BacktestResult:
    return _result()


def test_fidelity_contains_every_registered_rule() -> None:
    # F9/F10/F11 are shared with the Dow replication: a flat volatility window is
    # warmup, a market break opens a new continuity segment, and a signal whose fill
    # window never traded is cancelled. The ledger sorts case-insensitively, so
    # "F10" and "F11" land between "F1" and "F2".
    assert FIDELITY_RULE_IDS == (
        "B1", "B2", "B3", "B4",
        "F1", "F10", "F11", "F2", "F3", "F4", "F5", "F6", "F7", "F8", "F9",
    )


def test_workbook_has_the_required_sheets(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "bollinger")

    book = pd.ExcelFile(paths.xlsx)
    assert book.sheet_names == list(REPORT_SHEETS)
    assert REPORT_SHEETS == (
        "metrics",
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "universe",
        "selection",
        "dominant_rolls",
        "data_quality",
        "fidelity",
        "run_config",
    )
    assert paths.png.exists()
    assert paths.audit.exists()


def test_metrics_split_at_the_paper_sample_end(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "bollinger")

    metrics = pd.read_excel(paths.xlsx, sheet_name="metrics").set_index("period")
    assert IN_SAMPLE_END == date(2021, 9, 30)
    assert pd.Timestamp(metrics.loc["in_sample", "end"]).date() <= IN_SAMPLE_END
    assert pd.Timestamp(metrics.loc["out_of_sample", "start"]).date() > IN_SAMPLE_END


def test_paper_metrics_sit_beside_the_replication(tmp_path, result) -> None:
    paths = write_outputs(result, output_prefix=tmp_path / "bollinger")

    metrics = pd.read_excel(paths.xlsx, sheet_name="metrics").set_index("period")
    assert PAPER_METRICS == {
        "annual_return": 0.1752,
        "sharpe": 1.72,
        "max_drawdown": 0.0827,
        "calmar": 2.12,
        "annual_volatility": 0.1016,
    }
    for name, value in PAPER_METRICS.items():
        assert metrics.loc["in_sample", f"paper_{name}"] == pytest.approx(value)
        # The paper only reports its own sample; nothing else may claim its numbers.
        assert pd.isna(metrics.loc["out_of_sample", f"paper_{name}"])


def test_audit_records_a_digest_and_row_count_for_every_sheet(tmp_path, result) -> None:
    paths = write_outputs(
        result,
        output_prefix=tmp_path / "bollinger",
        manifest={"bundle_version": 1},
        query_plans=[{"query_kind": "bars", "node_types": ["Index Scan"]}],
    )

    audit = json.loads(paths.audit.read_text(encoding="utf-8"))
    assert set(audit["sheets"]) == set(REPORT_SHEETS)
    for name in REPORT_SHEETS:
        entry = audit["sheets"][name]
        assert len(entry["sha256"]) == 64
        assert entry["rows"] >= 0
    assert audit["manifest"] == {"bundle_version": 1}
    assert audit["query_plans"][0]["query_kind"] == "bars"
    assert audit["paper_metrics"] == PAPER_METRICS
    assert audit["sensitivity_only"] is False


def test_a_formula_like_cell_cannot_execute_when_the_workbook_opens(
    tmp_path, result
) -> None:
    universe = pd.DataFrame(
        [{"month_start": date(2021, 8, 1), "product": "=1+1", "turnover": 6.0e9}]
    )
    paths = write_outputs(
        result, output_prefix=tmp_path / "bollinger", universe=universe
    )

    written = pd.read_excel(paths.xlsx, sheet_name="universe")
    assert written.loc[0, "product"] == "'=1+1"


def test_a_sensitivity_run_says_so_in_both_the_ledger_and_the_audit(
    tmp_path, result
) -> None:
    paths = write_outputs(
        result, output_prefix=tmp_path / "bollinger_ddof1", sensitivity_only=True
    )

    fidelity = pd.read_excel(paths.xlsx, sheet_name="fidelity").set_index("rule_id")
    assert "sensitivity_only" in fidelity.loc["B1", "status"]
    audit = json.loads(paths.audit.read_text(encoding="utf-8"))
    assert audit["sensitivity_only"] is True
