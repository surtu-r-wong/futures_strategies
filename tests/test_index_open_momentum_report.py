"""报告与保真度披露 —— 计划 Task 8。

本文件最要紧的一条是计划的**硬性验收**：**2021-05 之后必须单独成段呈现**。
理由不是谨慎，是本仓已经撞过一次 —— 国信 Carry 那篇研报的样本恰好止于策略失效前
（记忆 `guosen-carry-paper-direction-is-wrong` / `carry-decay-four-ruled-out`）。
同一家、同一类研报，先验上应当假设这里也一样。

所以「只给全样本合并数」不是风格问题，是**验收失败**，这里用测试把它变成不可能。
"""

from datetime import date

import pandas as pd
import pytest

from index_open_momentum.report import (
    OUT_OF_SAMPLE_START,
    REPORT_SHEETS,
    build_sheets,
    segment_metrics,
    write_outputs,
)
from index_open_momentum.run import ProductDay, RunResult


def _daily(dates, values):
    return pd.DataFrame(
        {"IF": values, "portfolio": values},
        index=pd.Index(dates, name="trade_date"),
    )


def _span(start, end, step=0.001):
    days = pd.bdate_range(start, end).date.tolist()
    return _daily(days, [step * (1 if i % 2 else -1) + step for i in range(len(days))])


def _result(daily, product_days=()):
    return RunResult(
        product_days=tuple(product_days),
        daily=daily,
        plan_summaries=(),
        multipliers={},
    )


def _day(trade_date, **kw):
    base = dict(
        trade_date=trade_date,
        product="IF",
        contract="IF1606.CFE",
        direction=None,
        gross_return=0.0,
        cost=0.0,
        net_return=0.0,
        leverage=0.0,
        realized_vol=None,
        atr_at_entry=None,
        entry_price=None,
        scale_downs=0,
        carried_overnight=0.0,
        bars=16,
        no_trade_bars=0,
        missing_slots=0,
        max_relative_excursion=0.0,
        known_gap_minutes=0,
    )
    base.update(kw)
    return ProductDay(**base)


# --------------------------------------------------------------------------
# 1. 样本外必须单独成段 —— 计划的硬性验收
# --------------------------------------------------------------------------


def test_the_out_of_sample_cut_is_the_report_date():
    """研报日期 2021-05-13 ⇒ 其样本止于此。切点就钉在这里。"""
    assert OUT_OF_SAMPLE_START == date(2021, 5, 13)


def test_a_window_spanning_the_cut_yields_three_segments():
    """全样本 + 复刻区间 + 样本外，三段并列 —— 不允许只给合并数。"""
    segments = segment_metrics(_span("2019-01-01", "2023-12-29"))

    assert [s.name for s in segments] == ["full", "in_sample", "out_of_sample"]


def test_the_in_sample_segment_stops_before_the_cut():
    segments = {s.name: s for s in segment_metrics(_span("2019-01-01", "2023-12-29"))}

    assert segments["in_sample"].end < OUT_OF_SAMPLE_START
    assert segments["out_of_sample"].start >= OUT_OF_SAMPLE_START


def test_a_window_entirely_before_the_cut_says_so_rather_than_faking_a_segment():
    """整段落在切点之前 ⇒ 样本外那段**不存在**，而不是给一段空的假数。"""
    segments = {s.name: s for s in segment_metrics(_span("2015-01-01", "2016-12-30"))}

    assert "out_of_sample" not in segments
    assert segments["full"].observations == segments["in_sample"].observations


def test_a_window_entirely_after_the_cut_has_no_in_sample_segment():
    segments = {s.name: s for s in segment_metrics(_span("2022-01-03", "2023-12-29"))}

    assert "in_sample" not in segments
    assert "out_of_sample" in segments


def test_each_segment_carries_its_own_metrics_not_a_shared_dict():
    segments = segment_metrics(_span("2019-01-01", "2023-12-29"))
    values = [s.metrics["ann_return"] for s in segments]

    assert len({id(s.metrics) for s in segments}) == len(segments)
    assert len(set(values)) > 1  # 三段不可能恰好同值


def test_metrics_are_annualised_on_trading_days_not_months():
    """日频收益 ⇒ 年化用 252，不是 `common.metrics` 的月频默认 12。"""
    segments = {s.name: s for s in segment_metrics(_span("2019-01-01", "2023-12-29"))}

    assert segments["full"].periods_per_year == 252


def test_an_empty_frame_produces_no_segments():
    assert segment_metrics(pd.DataFrame()) == ()


# --------------------------------------------------------------------------
# 2. 工作表
# --------------------------------------------------------------------------


def test_every_required_sheet_is_present():
    """计划点名的七类：指标 / 日收益 / 年度收益 / 成交 / 信号 / 杠杆 / 数据质量。"""
    result = _result(_span("2019-01-01", "2023-12-29"), [_day(date(2019, 1, 1))])

    sheets = build_sheets(result)

    assert set(REPORT_SHEETS) <= set(sheets)
    assert set(REPORT_SHEETS) == {
        "metrics",
        "daily_returns",
        "annual_returns",
        "trades",
        "signals",
        "leverage",
        "data_quality",
    }


def test_the_metrics_sheet_has_one_row_per_segment():
    result = _result(_span("2019-01-01", "2023-12-29"))

    metrics = build_sheets(result)["metrics"]

    assert list(metrics["segment"]) == ["full", "in_sample", "out_of_sample"]


def test_annual_returns_are_one_row_per_calendar_year():
    result = _result(_span("2019-01-01", "2021-12-31"))

    annual = build_sheets(result)["annual_returns"]

    assert list(annual["year"]) == [2019, 2020, 2021]


def test_trades_only_lists_days_that_actually_took_a_position():
    from index_open_momentum.risk import Direction

    days = [
        _day(date(2019, 1, 1)),
        _day(date(2019, 1, 2), direction=Direction.LONG, entry_price=4000.0),
    ]
    result = _result(_span("2019-01-01", "2019-12-31"), days)

    trades = build_sheets(result)["trades"]

    assert list(trades["trade_date"]) == [date(2019, 1, 2)]


def test_signals_lists_every_session_including_the_flat_ones():
    """信号表要**全量**：哪天没信号本身就是结论的一部分。"""
    days = [_day(date(2019, 1, 1)), _day(date(2019, 1, 2))]
    result = _result(_span("2019-01-01", "2019-12-31"), days)

    signals = build_sheets(result)["signals"]

    assert len(signals) == 2


def test_the_data_quality_sheet_discloses_the_archive_gap():
    """2016 前的 15 分钟缺口必须在报告里显式披露，不能只活在代码注释里。"""
    days = [_day(date(2011, 6, 1), known_gap_minutes=15)]
    result = _result(_span("2011-06-01", "2011-12-30"), days)

    audit = build_sheets(result)["data_quality"]
    text = " ".join(str(v) for v in audit.to_numpy().ravel())

    assert "14:59" in text and "15:15" in text


def test_the_data_quality_sheet_names_unvalidated_multipliers():
    """样本太薄而走了元数据兜底的合约，必须点名 —— 那是没经过价域校验的数。"""
    from common.minute.bars import MultiplierResolution

    result = RunResult(
        product_days=(_day(date(2019, 1, 1)),),
        daily=_span("2019-01-01", "2019-12-31"),
        plan_summaries=(),
        multipliers={
            "IF1901": MultiplierResolution(
                multiplier=300,
                source="metadata_unvalidated",
                sample_rows=10,
                pass_rate=float("nan"),
                sample_dates=2,
            )
        },
    )

    audit = build_sheets(result)["data_quality"]
    text = " ".join(str(v) for v in audit.to_numpy().ravel())

    assert "IF1901" in text and "metadata_unvalidated" in text


# --------------------------------------------------------------------------
# 3. 落盘
# --------------------------------------------------------------------------


def test_write_outputs_produces_a_workbook_a_chart_and_the_explain_archive(tmp_path):
    result = _result(_span("2019-01-01", "2021-12-31"), [_day(date(2019, 1, 1))])

    paths = write_outputs(result, output_prefix=str(tmp_path / "run"))

    suffixes = sorted(p.suffix for p in paths)
    assert suffixes == [".json", ".png", ".xlsx"]
    assert all(p.exists() and p.stat().st_size > 0 for p in paths)


def test_the_explain_archive_holds_the_plan_summaries(tmp_path):
    """Task 7 第 5 步：`EXPLAIN` 产物必须**落盘**，不能只在内存里。"""
    import json

    result = RunResult(
        product_days=(_day(date(2019, 1, 1)),),
        daily=_span("2019-01-01", "2019-12-31"),
        plan_summaries=({"query_kind": "iter_month", "referenced_chunks": ["c1"]},),
        multipliers={},
    )

    paths = write_outputs(result, output_prefix=str(tmp_path / "run"))
    archive = [p for p in paths if p.suffix == ".json"][0]

    assert (
        json.loads(archive.read_text(encoding="utf-8"))["plans"][0]["query_kind"]
        == "iter_month"
    )


def test_an_empty_result_still_writes_the_audit_rather_than_nothing(tmp_path):
    """一笔都没跑出来时更需要看审计 —— 空结果不能静默不出文件。"""
    paths = write_outputs(_result(pd.DataFrame()), output_prefix=str(tmp_path / "run"))

    assert any(p.suffix == ".json" for p in paths)


def test_writing_twice_replaces_rather_than_appends(tmp_path):
    result = _result(_span("2019-01-01", "2019-12-31"))

    first = write_outputs(result, output_prefix=str(tmp_path / "run"))
    second = write_outputs(result, output_prefix=str(tmp_path / "run"))

    assert sorted(first) == sorted(second)


def test_the_output_directory_is_created_when_missing(tmp_path):
    result = _result(_span("2019-01-01", "2019-12-31"))

    paths = write_outputs(result, output_prefix=str(tmp_path / "deep" / "run"))

    assert all(p.exists() for p in paths)


# --------------------------------------------------------------------------
# 4. 保真度声明
# --------------------------------------------------------------------------


def test_the_runbook_exists_and_names_the_four_things_the_plan_requires():
    from pathlib import Path

    runbook = Path("docs/operations/guosen-open-momentum-runbook.md")
    text = runbook.read_text(encoding="utf-8")

    assert "public.futures_minute" in text
    assert "2026-08-11" in text  # 分钟表末端，实盘前需补尾
    assert "cffex-v1" in text  # 时段版本
    assert "15:15" in text  # 2016 前缺口的披露口径
    assert "EXPLAIN" in text  # 存档位置


def test_the_runbook_states_the_out_of_sample_requirement():
    from pathlib import Path

    text = Path("docs/operations/guosen-open-momentum-runbook.md").read_text(
        encoding="utf-8"
    )

    assert "2021-05" in text


@pytest.mark.parametrize(
    "phrase",
    ["25.79", "1.77", "7.66", "3.37"],
)
def test_the_runbook_carries_the_paper_headline_to_reconcile_against(phrase):
    """研报口径：费后年化 25.79% / Sharpe 1.77 / 最大回撤 7.66% / Calmar 3.37。"""
    from pathlib import Path

    text = Path("docs/operations/guosen-open-momentum-runbook.md").read_text(
        encoding="utf-8"
    )

    assert phrase in text
