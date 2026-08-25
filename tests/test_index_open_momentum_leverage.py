"""ATR 杠杆、已实现波动率反馈与品种等权（计划 Task 6）。

研报口径：

- ATR 资金管理令 1 个 ATR 的波动等于总资金的 0.5% ⇒ `Lev_ATR = 0.005 * close / ATR`，
  上限 4；
- 已实现波动率乘数按**月**用过去一年的策略收益重算，目标年化波动 15%；
- 最终杠杆 `clip(Lev_ATR * 0.15 / realized_vol, 0, 4)`；
- IF / IC / IH 等资金组合；IM 上市晚于研报，不在忠实口径内。
"""

import math
from datetime import date, timedelta

import pytest

from index_open_momentum.leverage import (
    PAPER_FAMILIES,
    atr_leverage,
    equal_capital_weights,
    final_leverage,
    monthly_realized_volatility,
    realized_volatility,
)


def test_atr_leverage_makes_one_atr_move_half_a_percent_of_capital():
    # 0.005 * 4000 / 20 = 1.0
    assert atr_leverage(close=4000.0, atr=20.0) == pytest.approx(1.0)


def test_atr_leverage_is_capped_at_four():
    # 0.005 * 4000 / 2 = 10 → 截到 4。这里写死 4.0 而不是引 MAX_LEVERAGE：
    # 拿被测常量当期望值，常量改了测试跟着改，等于没有断言。
    assert atr_leverage(close=4000.0, atr=2.0) == pytest.approx(4.0)


def test_a_zero_atr_means_no_position_not_maximum_leverage():
    """ATR 为 0 时公式没有定义（除零），不是"波动无穷小所以满仓"。

    研报没写这种情形。按字面把 +inf 截到 4，等于在**跌停封死或数据坏掉**的那一天
    上满杠杆——这是所有可能解读里最差的一种。故取 0（当日不建仓）。
    """
    assert atr_leverage(close=4000.0, atr=0.0) == 0.0


def test_atr_leverage_without_an_atr_is_a_hard_failure():
    with pytest.raises(ValueError, match="ATR"):
        atr_leverage(close=4000.0, atr=None)


def test_realized_volatility_is_the_annualised_sample_deviation():
    returns = [0.02, -0.02] * 126  # 252 个观测
    # 均值 0；样本方差 = 252*0.0004/251；年化 = sqrt(方差)*sqrt(252)
    expected = math.sqrt(252 * 0.0004 / 251) * math.sqrt(252)
    assert realized_volatility(returns) == pytest.approx(expected)


def test_realized_volatility_needs_a_full_year_of_history():
    assert realized_volatility([0.01] * 251) is None
    assert realized_volatility([0.02, -0.02] * 126) is not None


def test_final_leverage_scales_towards_the_fifteen_percent_target():
    # Lev_ATR = 1.0；已实现波动 30% ⇒ 1.0 * 0.15 / 0.30 = 0.5
    assert final_leverage(close=4000.0, atr=20.0, realized_vol=0.30) == pytest.approx(0.5)


def test_the_atr_cap_is_applied_before_the_volatility_multiplier():
    """两道截断都在，且顺序有实质差别。

    close=4000, atr=2 ⇒ 未截断的 Lev_ATR = 10，截断后 = 4。
    已实现波动 60% ⇒ 先截断：4 * 0.15 / 0.6 = 1.0；不先截断：10 * 0.15 / 0.6 = 2.5。
    """
    assert final_leverage(close=4000.0, atr=2.0, realized_vol=0.60) == pytest.approx(1.0)


def test_final_leverage_is_capped_again_after_the_multiplier():
    # Lev_ATR 截到 4；已实现波动 5% ⇒ 4 * 0.15 / 0.05 = 12 → 再截到 4
    assert final_leverage(close=4000.0, atr=2.0, realized_vol=0.05) == pytest.approx(4.0)


def test_no_volatility_history_means_no_position():
    """过去一年的策略收益不够就不建仓，而非默默把乘数当 1。

    这条同时给研报的样本起点一个可检验的解释：IF 2010-04-16 挂牌，而研报样本
    自 2011 年 6 月起 —— 相隔约 14 个月，与"先攒够一年策略收益再开跑"一致。
    ⚠️ 这是**假设**不是研报原文，须在保真度报告里作为待检验项列出。
    """
    assert final_leverage(close=4000.0, atr=20.0, realized_vol=None) == 0.0


def test_active_families_share_capital_equally():
    assert equal_capital_weights(["IF", "IC"]) == {"IF": 0.5, "IC": 0.5}
    assert equal_capital_weights(["IF", "IC", "IH"]) == pytest.approx(
        {"IF": 1 / 3, "IC": 1 / 3, "IH": 1 / 3}
    )


def test_a_day_with_one_active_family_puts_all_capital_there():
    assert equal_capital_weights(["IH"]) == {"IH": 1.0}


def test_a_day_with_no_active_family_holds_nothing():
    assert equal_capital_weights([]) == {}


def test_im_is_outside_the_paper_faithful_universe():
    assert "IM" not in PAPER_FAMILIES
    with pytest.raises(ValueError, match="IM"):
        equal_capital_weights(["IF", "IM"])


# --- 按月重算的节拍（研报："Realized-volatility multiplier is recomputed monthly"）---

_ALTERNATING = [0.02, -0.02] * 200


def _history(n: int, *, start=date(2011, 1, 1), values=None):
    """连续 n 个自然日的 (日期, 收益)。日历有效性无关紧要 —— 函数只按月分桶。"""
    vals = values if values is not None else _ALTERNATING
    return [(start + timedelta(days=i), vals[i]) for i in range(n)]


def test_the_multiplier_is_computed_from_returns_before_the_month_starts():
    history = _history(252)  # 2011-01-01 起 252 天，全部早于 2012-01-01
    expected = math.sqrt(252 * 0.0004 / 251) * math.sqrt(252)
    assert monthly_realized_volatility(
        session_date=date(2012, 1, 15), returns_by_date=history
    ) == pytest.approx(expected)


def test_the_multiplier_does_not_move_within_a_month():
    """月内保持不变 —— 而且当月自己的收益不许参与，哪怕它极端。"""
    history = [*_history(252), (date(2012, 1, 5), 0.5)]  # 当月一个极端值
    expected = math.sqrt(252 * 0.0004 / 251) * math.sqrt(252)
    mid = monthly_realized_volatility(session_date=date(2012, 1, 15), returns_by_date=history)
    end = monthly_realized_volatility(session_date=date(2012, 1, 31), returns_by_date=history)
    assert mid == pytest.approx(expected)
    assert end == pytest.approx(expected)


def test_the_multiplier_is_recomputed_when_the_month_rolls_over():
    history = [*_history(252), (date(2012, 1, 5), 0.5)]
    january = monthly_realized_volatility(session_date=date(2012, 1, 15), returns_by_date=history)
    february = monthly_realized_volatility(session_date=date(2012, 2, 1), returns_by_date=history)
    # 二月起 1 月 5 日那个极端值进入窗口 → 必须变
    assert february != pytest.approx(january)


def test_only_the_trailing_year_of_prior_returns_is_used():
    history = _history(300)  # 早于当月的观测有 300 个，只该用最后 252 个
    oracle = realized_volatility([r for _, r in history[-252:]])
    assert monthly_realized_volatility(
        session_date=date(2012, 1, 15), returns_by_date=history
    ) == pytest.approx(oracle)


def test_no_multiplier_before_a_full_year_of_prior_returns_exists():
    assert monthly_realized_volatility(
        session_date=date(2012, 1, 15), returns_by_date=_history(251)
    ) is None


def test_the_window_counts_observations_not_calendar_days():
    """窗口是"最近 252 个观测"，不是"最近 365 个自然日"。

    上面几个用例的历史是**连续自然日**，252 个观测恰好只跨 252 天 —— 两种口径选出
    的是同一批，差别不可见。这里把观测按 3 天一个铺开（300 个观测跨 897 天），两种
    口径才分得开：按观测数仍取到 252 个；按 365 自然日只剩约 122 个，不足一年会返回
    None。研报只说 "past one year"，本实现取观测数（停市长假下窗口长度稳定），这是
    显式假设 —— 有了这条用例它才真的有人守。
    """
    sparse = [(date(2009, 1, 1) + timedelta(days=3 * i), _ALTERNATING[i]) for i in range(300)]
    assert sparse[-1][0] < date(2012, 1, 1)
    oracle = realized_volatility([r for _, r in sparse[-252:]])
    assert monthly_realized_volatility(
        session_date=date(2012, 1, 15), returns_by_date=sparse
    ) == pytest.approx(oracle)
