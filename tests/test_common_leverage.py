"""上提到 `common.leverage` 之后新增的公开面（计划 Task 0）。

附录二的算术本身已由 `tests/test_index_open_momentum_leverage.py` 钉住，这里**不重复**，
只钉搬迁本身改变的两件事：`universe` 从"有默认值"变成"必填"，以及股指侧的默认值仍在。
"""

import pytest

import common.leverage as shared
import index_open_momentum.leverage as index_side


def test_shared_equal_weights_refuses_to_guess_the_universe():
    """商品那条线的宇宙逐月变化，任何默认值在那边都是错的 —— 所以必须显式传。"""
    with pytest.raises(TypeError):
        shared.equal_capital_weights(["RB", "CU"])


def test_shared_equal_weights_splits_only_among_the_active_products():
    weights = shared.equal_capital_weights(["RB", "CU"], universe=["RB", "CU", "M"])
    assert weights == {"RB": 0.5, "CU": 0.5}


def test_shared_equal_weights_rejects_products_outside_the_universe():
    with pytest.raises(ValueError, match="不在忠实口径的品种集合"):
        shared.equal_capital_weights(["RB"], universe=["CU"])


def test_index_side_keeps_its_paper_universe_as_the_default():
    """股指侧的调用姿势不能因为搬迁而改变。"""
    assert index_side.PAPER_FAMILIES == ("IF", "IC", "IH")
    assert index_side.equal_capital_weights(["IF", "IC"]) == {"IF": 0.5, "IC": 0.5}
    with pytest.raises(ValueError):
        index_side.equal_capital_weights(["RB"])


def test_index_side_re_exports_the_shared_implementation():
    """再导出必须是同一个函数对象，不是各留一份副本 —— 副本迟早分叉。"""
    for name in (
        "atr_leverage",
        "final_leverage",
        "realized_volatility",
        "monthly_realized_volatility",
    ):
        assert getattr(index_side, name) is getattr(shared, name)


def test_zero_atr_takes_no_position_rather_than_the_cap():
    """除零处研报没写。按字面截到 4 等于在数据坏掉那天上满杠杆，是最差的一种解读。"""
    assert shared.atr_leverage(close=4000.0, atr=0.0) == 0.0
