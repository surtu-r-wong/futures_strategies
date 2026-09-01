"""报告与保真度台账（计划 Task 7）。

研报样本止于 2023-05-31。本仓已经两次撞上「研报样本恰好止于策略失效前」
（国信 Carry、国信开盘动量），所以**样本外分段是强制输出**：窗口没走到切点时也要
留一行说明为什么没有，而不是让那一段悄悄消失。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from cta_continuous.backtest import BacktestParams, TurnoverCause
from cta_continuous.report import (
    DECISIONS,
    OUT_OF_SAMPLE_START,
    PAPER_AVERAGE_LEVERAGE,
    PAPER_HEADLINE,
    annual_table,
    build_sheets,
    cost_sensitivity,
    fidelity_ledger,
    leverage_profile,
    segment_metrics,
    turnover_breakdown,
    write_outputs,
)


def _daily(dates, returns, *, turnover=None, leverage=None):
    return pd.DataFrame(
        {
            "trade_date": list(dates),
            "gross_return": list(returns),
            "net_return": list(returns),
            "turnover": list(turnover or [0.1] * len(returns)),
            "cost": [0.0] * len(returns),
            "direct_cost": [0.0] * len(returns),
            "equity": (1.0 + pd.Series(returns)).cumprod().tolist(),
            "gross_equity": (1.0 + pd.Series(returns)).cumprod().tolist(),
            "gross_leverage": list(leverage or [2.4] * len(returns)),
        }
    )


def _span(start, count):
    return [stamp.date() for stamp in pd.bdate_range(start, periods=count)]


# --- 分段 -------------------------------------------------------------------


def test_report_refuses_to_omit_the_out_of_sample_segment():
    """窗口整段落在切点之前，样本外那一行**照样要在**，并说明为什么是空的。

    本仓已经两次发现研报样本恰好止于策略失效前。一段悄悄消失的样本外，读者是
    看不出来的 —— 那正是最该被看见的一段。
    """
    dates = _span(date(2022, 1, 3), 40)
    segments = segment_metrics(_daily(dates, [0.001] * 40))
    by_name = {segment.name: segment for segment in segments}

    assert set(by_name) == {"full", "in_sample", "out_of_sample"}
    absent = by_name["out_of_sample"]
    assert absent.observations == 0
    assert absent.note
    # 不许拿假数填：缺席的那段指标全是 NaN，不是 0。
    assert all(pd.isna(value) for value in absent.metrics.values() if value is not None)


def test_segments_split_at_the_papers_cut():
    dates = _span(date(2023, 5, 25), 10)
    segments = {s.name: s for s in segment_metrics(_daily(dates, [0.001] * 10))}

    assert segments["in_sample"].end < OUT_OF_SAMPLE_START
    assert segments["out_of_sample"].start >= OUT_OF_SAMPLE_START
    assert (
        segments["in_sample"].observations + segments["out_of_sample"].observations
        == segments["full"].observations
    )


def test_an_empty_run_still_reports_three_segments():
    segments = {s.name: s for s in segment_metrics(_daily([], []))}

    assert set(segments) == {"full", "in_sample", "out_of_sample"}
    assert all(segment.observations == 0 for segment in segments.values())


# --- 保真度台账 -------------------------------------------------------------


def test_fidelity_ledger_lists_every_decision():
    """台账是一本**出处账**：每条裁决都要有歧义、裁定、依据三栏。

    ⚠️ 条数被这条用例钉死。新增裁决必须同时改这里，并在提交里说明它的来由 ——
    否则「研报没写、我们自己定的」那部分会悄悄长大。
    """
    ledger = fidelity_ledger()

    assert list(ledger.columns) == ["id", "question", "ruling", "basis"]
    ids = list(ledger["id"])
    assert ids == [f"D{n}" for n in range(1, len(DECISIONS) + 1)]
    assert len(ids) == 22
    assert ledger["question"].str.len().min() > 0
    assert ledger["ruling"].str.len().min() > 0
    assert ledger["basis"].str.len().min() > 0


def test_the_ledger_names_the_decisions_the_paper_contradicts_itself_on():
    ledger = fidelity_ledger().set_index("id")

    assert "短均线" in ledger.loc["D2", "ruling"]
    assert "ΔTNR" in ledger.loc["D3", "ruling"] or "> 0" in ledger.loc["D3", "ruling"]
    assert "影子" in ledger.loc["D17", "ruling"]


# --- 分年度表 ---------------------------------------------------------------


def test_annual_table_matches_the_papers_columns():
    """列对齐研报表 7：收益 / 最大回撤 / 夏普 / 波动 / Calmar / 月度胜率。"""
    dates = _span(date(2022, 1, 3), 300)
    returns = [0.001 if index % 3 else -0.002 for index in range(300)]

    table = annual_table(_daily(dates, returns))

    assert list(table.columns) == [
        "year",
        "return",
        "max_drawdown",
        "sharpe",
        "ann_vol",
        "calmar",
        "monthly_win_rate",
        "observations",
    ]
    assert set(table["year"]) == {2022, 2023}
    assert table["observations"].sum() == 300
    assert (table["monthly_win_rate"].between(0.0, 1.0)).all()


# --- 换手四分解 -------------------------------------------------------------


class _Result:
    def __init__(self, executions, daily=None, leverage=None):
        self.executions = executions
        self.daily = daily if daily is not None else _daily(_span(date(2024, 1, 2), 3), [0.0] * 3)
        self.leverage = leverage if leverage is not None else pd.DataFrame()


def test_turnover_breakdown_keeps_a_row_for_every_cause():
    """四类都要有行，哪怕某一类是 0 —— 隐掉一行读者就以为它不存在。"""
    executions = pd.DataFrame(
        {
            "cause": ["signal", "signal", "roll"],
            "turnover": [1.0, 2.0, 0.5],
            "cost": [0.00013, 0.00026, 0.000065],
        }
    )

    table = turnover_breakdown(_Result(executions))

    assert set(table["cause"]) == {c.value for c in TurnoverCause}
    assert list(table.columns) == ["cause", "turnover", "cost", "turnover_share"]
    assert table["turnover"].sum() == pytest.approx(3.5)
    assert table["cost"].sum() == pytest.approx(0.000455)
    assert table["turnover_share"].sum() == pytest.approx(1.0)
    universe = table.loc[table["cause"] == "universe"].iloc[0]
    assert universe["turnover"] == 0.0


def test_turnover_breakdown_of_an_empty_run_is_all_zeros():
    table = turnover_breakdown(_Result(pd.DataFrame(columns=["cause", "turnover", "cost"])))

    assert len(table) == len(TurnoverCause)
    assert table["turnover"].sum() == 0.0
    assert table["turnover_share"].sum() == 0.0


# --- 杠杆 -------------------------------------------------------------------


def test_leverage_profile_reports_the_mean_against_the_paper():
    """研报图 15：平均杠杆 2.5 倍。报告要把实测均值摆在它旁边。"""
    dates = _span(date(2024, 1, 2), 40)
    # ⚠️ 均值与中位数必须分得开：`[2.0]*20 + [3.0]*20` 两者都是 2.5，把 mean 换成
    # median 这条变异就抓不住。30 个 2.0 + 10 个 4.0 ⇒ 均值 2.5、中位数 2.0。
    daily = _daily(dates, [0.0] * 40, leverage=[2.0] * 30 + [4.0] * 10)

    profile = leverage_profile(daily)

    assert profile["mean_gross_leverage"] == pytest.approx(2.5)
    assert profile["paper_average_leverage"] == PAPER_AVERAGE_LEVERAGE
    assert len(profile["monthly"]) == len({(d.year, d.month) for d in dates})


# --- 成本敏感性 -------------------------------------------------------------


def test_cost_sensitivity_reports_all_three_levels_the_paper_tabulates():
    """研报表 8：1.3 / 1.8 / 2.5 个基点三档。"""
    calls = []

    def _runner(*, params):
        calls.append(params.cost_bps)
        dates = _span(date(2024, 1, 2), 20)
        drag = params.cost_bps / 10_000.0
        return _Result(
            pd.DataFrame({"cause": ["signal"], "turnover": [1.0], "cost": [drag]}),
            daily=_daily(dates, [0.001 - drag] * 20),
        )

    table = cost_sensitivity(runner=_runner, params=BacktestParams())

    assert calls == [1.3, 1.8, 2.5]
    assert list(table["cost_bps"]) == [1.3, 1.8, 2.5]
    assert table["ann_return"].is_monotonic_decreasing
    assert list(table.columns) == [
        "cost_bps", "ann_return", "sharpe", "max_drawdown", "ann_vol", "turnover"
    ]


# --- 汇总 -------------------------------------------------------------------


def test_build_sheets_carries_every_required_table():
    dates = _span(date(2023, 1, 3), 200)
    result = _Result(
        pd.DataFrame({"cause": ["signal"], "turnover": [1.0], "cost": [0.00013]}),
        daily=_daily(dates, [0.0005] * 200),
    )

    sheets = build_sheets(result)

    assert set(sheets) == {
        "segments", "annual", "fidelity", "turnover", "leverage", "daily"
    }
    assert "out_of_sample" in set(sheets["segments"]["segment"])
    assert len(sheets["fidelity"]) == len(DECISIONS)


def test_write_outputs_produces_a_workbook_and_an_audit(tmp_path):
    dates = _span(date(2023, 1, 3), 200)
    result = _Result(
        pd.DataFrame({"cause": ["signal"], "turnover": [1.0], "cost": [0.00013]}),
        daily=_daily(dates, [0.0005] * 200),
    )

    written = write_outputs(build_sheets(result), prefix=tmp_path / "continuous")

    assert (tmp_path / "continuous.xlsx").exists()
    assert (tmp_path / "continuous-audit.json").exists()
    assert set(written) == {"workbook", "audit"}
    audit = pd.read_json(tmp_path / "continuous-audit.json", typ="series")
    assert audit["paper_headline"] == PAPER_HEADLINE
    assert audit["out_of_sample_start"] == str(OUT_OF_SAMPLE_START)
