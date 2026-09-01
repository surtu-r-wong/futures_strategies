"""上提到 `common.dominant` 之后新增的公开面（计划 Task 0）。

选主力的行为本身已由 `tests/test_index_open_momentum_pg_source.py` 钉住，这里只钉
搬迁本身：`product_of` 成为公开面，以及类身份没有因为再导出而分裂。
"""

import common.dominant as shared
import index_open_momentum.pg_source as index_side


def test_product_of_folds_a_contract_code_to_its_product():
    assert shared.product_of("RB2410.SHF") == "RB"
    assert shared.product_of("IF2409") == "IF"
    assert shared.product_of("m2501.DCE") == "M"


def test_index_side_re_exports_the_same_class_object():
    """`DominantChoice` 必须是同一个类。两份同名类会让 isinstance 在跨模块时静默失败。"""
    assert index_side.DominantChoice is shared.DominantChoice
    assert index_side.choose_dominant is shared.choose_dominant
    assert index_side.reconcile_dominant is shared.reconcile_dominant
    assert index_side.daily_stats_from_minutes is shared.daily_stats_from_minutes


def test_selection_lag_is_one_and_shared():
    assert shared.DOMINANT_SELECTION_LAG == 1
    assert index_side.DOMINANT_SELECTION_LAG == shared.DOMINANT_SELECTION_LAG


def test_contract_shape_judgement_stayed_with_the_index_consumer():
    """CFFEX 合成码的判据是消费者的事，不该跟着搬进 common。"""
    assert not hasattr(shared, "is_concrete_index_contract")
    assert index_side.is_concrete_index_contract("IF2409")
    assert not index_side.is_concrete_index_contract("IF01")
