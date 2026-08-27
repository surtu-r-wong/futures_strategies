"""Legacy commodity continuous-price compatibility exports."""

from common.commodity.continuous import (
    adjustment_factors as shared_adjustment_factors,
    continuous_close as shared_continuous_close,
)
from common.commodity.dominant import (
    choose_dominant_commodity as shared_choose_dominant_commodity,
    delivery_month as shared_delivery_month,
)
from cta_continuous import continuous as legacy_continuous


def test_legacy_continuous_exports_are_shared_function_identities():
    assert legacy_continuous.adjustment_factors is shared_adjustment_factors
    assert legacy_continuous.continuous_close is shared_continuous_close
    assert (
        legacy_continuous.choose_dominant_commodity
        is shared_choose_dominant_commodity
    )
    assert legacy_continuous.delivery_month is shared_delivery_month
