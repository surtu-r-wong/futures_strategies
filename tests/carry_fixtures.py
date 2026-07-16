"""Deterministic synthetic fixtures for Carry integration tests."""

from __future__ import annotations

import math

import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.data import CarryDataSet, normalize_contract_daily


def small_config(**overrides) -> CarryConfig:
    values = {
        "liquidity_window": 2,
        "liquidity_threshold": 0.0,
        "carry_window": 2,
        "selection_fraction": 0.20,
        "momentum_window": 2,
        "atr_window": 2,
        "vol_window": 4,
        "min_shadow_active_days": 2,
        "prewarm_calendar_days": 15,
    }
    values.update(overrides)
    return CarryConfig(**values)


def make_carry_panel(periods: int = 24) -> CarryDataSet:
    dates = pd.bdate_range("2024-01-02", periods=periods).date.tolist()
    products = ("A", "B", "C", "D", "E")
    contracts = (
        ("2410", 500.0, 300.0, 3_000_000_000.0),
        ("2501", 300.0, 200.0, 2_000_000_000.0),
        ("2505", 100.0, 100.0, 1_000_000_000.0),
    )
    far_multipliers = {
        "A": (1.04, 1.08),
        "B": (1.03, 1.06),
        "C": (1.00, 1.00),
        "D": (0.97, 0.94),
        "E": (0.96, 0.92),
    }
    rows: list[dict[str, object]] = []
    for day_index, trade_date in enumerate(dates):
        for product_index, product in enumerate(products):
            perturbation = 0.002 * math.sin(day_index + product_index * 0.7)
            if product in ("A", "B"):
                trend = 1.0 + 0.003 * day_index
            elif product in ("D", "E"):
                trend = 1.0 - 0.003 * day_index
            else:
                trend = 1.0
            near_close = (100.0 + 10.0 * product_index) * (trend + perturbation)
            multipliers = (1.0, *far_multipliers[product])
            for (digits, oi, volume, turnover), multiplier in zip(
                contracts,
                multipliers,
            ):
                close = near_close * multiplier
                open_price = close * (
                    1.0 + 0.002 * math.sin(day_index * 1.3 + product_index)
                )
                high = max(open_price, close) * 1.01
                low = min(open_price, close) * 0.99
                rows.append(
                    {
                        "trade_date": trade_date,
                        "contract": f"{product}{digits}",
                        "open": open_price,
                        "high": high,
                        "low": low,
                        "close": close,
                        "volume": volume,
                        "oi": oi,
                        "turnover": turnover,
                    }
                )
    return normalize_contract_daily(pd.DataFrame(rows))
