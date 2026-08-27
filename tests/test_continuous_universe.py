"""Compatibility checks for the legacy liquidity-universe import path."""

from common.commodity import universe as shared
from cta_continuous import universe as legacy


def test_legacy_universe_exports_are_shared_objects():
    assert legacy.universe_for_month is shared.universe_for_month
    assert legacy.product_daily_turnover is shared.product_daily_turnover
    assert legacy.canonical_contract is shared.canonical_contract
    assert legacy.TURNOVER_THRESHOLD == shared.TURNOVER_THRESHOLD == 5e9
