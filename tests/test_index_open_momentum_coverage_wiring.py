"""覆盖度闸的接线 —— 验收标准："闸全部通过后才允许标 paper-faithful"。

⚠️ 这个文件是自查时补的：Task 1 把 `coverage_gate` 写好并变异验证过，但直到
Task 8 收尾它**从没被任何地方调用**。一个写好、测好、却没接线的闸等于不存在，
而且比不存在更糟 —— 它会让人以为这道检查已经在跑。

所以这里测的不是闸的算法（那在 `test_index_open_momentum_sessions.py` 里），
而是**它确实被调用了，并且它的判定真的能否决 paper-faithful 标注**。
"""

from datetime import date

import pytest

from index_open_momentum.report import fidelity_verdict
from index_open_momentum.run import ProductDay, RunResult


def _day(trade_date, product="IF"):
    return ProductDay(
        trade_date=trade_date,
        product=product,
        contract=f"{product}1606.CFE",
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


def _result(days):
    import pandas as pd

    return RunResult(
        product_days=tuple(days),
        daily=pd.DataFrame(),
        plan_summaries=(),
        multipliers={},
    )


CALENDAR = (date(2016, 6, 1), date(2016, 6, 2), date(2016, 6, 3))


def test_full_coverage_allows_the_paper_faithful_label():
    result = _result([_day(d) for d in CALENDAR])

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=True,
    )

    assert verdict.paper_faithful is True
    assert verdict.coverage.shortfalls == ()


def test_a_coverage_shortfall_vetoes_the_label():
    """少了一个交易日就不许标 —— 这正是那道闸存在的理由。"""
    result = _result([_day(CALENDAR[0]), _day(CALENDAR[2])])

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=True,
    )

    assert verdict.paper_faithful is False
    assert verdict.coverage.shortfalls[0].missing_dates == (date(2016, 6, 2),)
    assert any("coverage" in reason for reason in verdict.reasons)


def test_the_window_veto_still_applies_even_with_perfect_coverage():
    """覆盖满分也救不了一个结构上标不了的区间（2016 前那 15 分钟缺口）。"""
    result = _result([_day(d) for d in CALENDAR])

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=False,
    )

    assert verdict.paper_faithful is False
    assert any("window" in reason for reason in verdict.reasons)


def test_an_unvalidated_multiplier_also_vetoes_the_label():
    """凡是走了元数据兜底、没过价域校验的合约，结果就不是 paper-faithful。"""
    import pandas as pd

    from common.minute.bars import MultiplierResolution

    result = RunResult(
        product_days=tuple(_day(d) for d in CALENDAR),
        daily=pd.DataFrame(),
        plan_summaries=(),
        multipliers={
            "IF1606": MultiplierResolution(
                multiplier=300,
                source="metadata_unvalidated",
                sample_rows=10,
                pass_rate=float("nan"),
                sample_dates=2,
            )
        },
    )

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=True,
    )

    assert verdict.paper_faithful is False
    assert any("multiplier" in reason for reason in verdict.reasons)


def test_every_veto_reason_is_reported_not_just_the_first():
    """三条都不满足时要三条都报 —— 只报第一条会让人修一次跑一次。"""
    import pandas as pd

    from common.minute.bars import MultiplierResolution

    result = RunResult(
        product_days=(_day(CALENDAR[0]),),
        daily=pd.DataFrame(),
        plan_summaries=(),
        multipliers={
            "IF1606": MultiplierResolution(
                multiplier=300,
                source="metadata_unvalidated",
                sample_rows=1,
                pass_rate=float("nan"),
                sample_dates=1,
            )
        },
    )

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=False,
    )

    assert len(verdict.reasons) == 3


def test_the_verdict_lands_in_the_data_quality_sheet():
    from index_open_momentum.report import build_sheets

    result = _result([_day(CALENDAR[0])])
    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=True,
    )

    audit = build_sheets(result, fidelity=verdict)["data_quality"]
    text = " ".join(str(v) for v in audit.to_numpy().ravel())

    assert "paper_faithful" in text and "False" in text


@pytest.mark.parametrize("threshold", [0, 1])
def test_the_threshold_travels_through_to_the_gate(threshold):
    result = _result([_day(CALENDAR[0]), _day(CALENDAR[2])])

    verdict = fidelity_verdict(
        result,
        calendar_dates=CALENDAR,
        window_start=date(2016, 6, 1),
        window_end=date(2016, 6, 3),
        window_allows_paper_faithful=True,
        max_missing_days=threshold,
    )

    assert verdict.coverage.paper_faithful is (threshold == 1)
