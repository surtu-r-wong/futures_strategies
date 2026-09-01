"""国信开盘动量的杠杆层。

附录二的通用部分（海龟资金管理法、15% 目标波动反馈、4 倍截断、按月重算节拍）
已上提到 `common.leverage` —— 那套东西是国信 CTA 系列研报共用的附录，商品那条线
（《基于连续信号的商品期货交易策略》）是第二个消费者。本模块只留股指侧的口径：
研报钉死的三品种，以及把它绑成 `equal_capital_weights` 默认值这件事。
"""

from __future__ import annotations

from collections.abc import Sequence

from common.leverage import (  # noqa: F401  （原地再导出，调用方 import 路径不变）
    ATR_CAPITAL_FRACTION,
    MAX_LEVERAGE,
    TARGET_ANNUAL_VOL,
    TRADING_DAYS_PER_YEAR,
    atr_leverage,
    final_leverage,
    monthly_realized_volatility,
    realized_volatility,
)
from common.leverage import equal_capital_weights as _equal_capital_weights

#: 忠实口径的品种集合。IM 2022-07-22 才挂牌，晚于研报（2021-05-13），不在内。
PAPER_FAMILIES = ("IF", "IC", "IH")


def equal_capital_weights(
    active_families: Sequence[str], *, universe: Sequence[str] = PAPER_FAMILIES
) -> dict[str, float]:
    """当日有信号的品种之间等分资金；股指侧默认锁在忠实口径的三品种上。

    `universe` 在 `common.leverage` 里是**必填**的 —— 商品那条线的宇宙逐月变化，
    默认值在那边一定是错的。这里补上股指侧的默认值，调用方的姿势不变。
    """
    return _equal_capital_weights(active_families, universe=universe)
