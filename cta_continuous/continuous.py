"""Compatibility exports for commodity dominant selection and continuous prices."""

from common.commodity.continuous import adjustment_factors, continuous_close
from common.commodity.dominant import (
    DOMINANT_SELECTION_LAG,
    choose_dominant_commodity,
    delivery_month,
)

__all__ = [
    "DOMINANT_SELECTION_LAG",
    "adjustment_factors",
    "choose_dominant_commodity",
    "continuous_close",
    "delivery_month",
]
