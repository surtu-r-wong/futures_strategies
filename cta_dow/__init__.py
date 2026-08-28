"""Guosen Dow-theory commodity-futures strategy."""

from cta_dow.indicators import (
    MacdPath,
    Trend,
    cumulative_macd,
    macd_path,
    preliminary_trend,
)
from cta_dow.signals import (
    Position,
    SignalDecision,
    SignalMode,
    TradeState,
    decide,
)
from cta_dow.state import SegmentDecision, SegmentState, inspect_bar

__all__ = [
    "MacdPath",
    "Position",
    "SegmentDecision",
    "SegmentState",
    "SignalDecision",
    "SignalMode",
    "TradeState",
    "Trend",
    "cumulative_macd",
    "decide",
    "inspect_bar",
    "macd_path",
    "preliminary_trend",
]
