"""Capital-allocation policies shared by commodity strategy replicas."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import math
from numbers import Real
from typing import TypeAlias


Directions: TypeAlias = Mapping[str, Real]


def _product(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{label} product must be a nonempty string")
    return value


def _directions(directions: Directions) -> dict[str, float]:
    if not isinstance(directions, Mapping):
        raise ValueError("directions must be a mapping")

    validated: dict[str, float] = {}
    for raw_product, raw_side in directions.items():
        product = _product(raw_product, label="direction")
        if isinstance(raw_side, bool) or not isinstance(raw_side, Real):
            raise ValueError(
                f"direction for {product} must be finite and one of -1, 0, or 1"
            )
        side = float(raw_side)
        if not math.isfinite(side) or side not in (-1.0, 0.0, 1.0):
            raise ValueError(
                f"direction for {product} must be finite and one of -1, 0, or 1"
            )
        validated[product] = side
    return validated


def _universe(universe: Iterable[str]) -> tuple[str, ...]:
    if isinstance(universe, (str, bytes)):
        raise ValueError("universe must be an iterable of product names")
    try:
        products = tuple(universe)
    except TypeError as exc:
        raise ValueError("universe must be an iterable of product names") from exc

    validated = tuple(_product(value, label="universe") for value in products)
    if len(set(validated)) != len(validated):
        raise ValueError("universe contains a duplicate product")
    return validated


def fixed_universe_weights(
    directions: Directions,
    *,
    universe: Iterable[str],
) -> dict[str, float]:
    """Equal-weight a fixed universe, leaving inactive sleeves in cash."""
    validated_directions = _directions(directions)
    products = _universe(universe)
    outsiders = set(validated_directions) - set(products)
    if outsiders:
        raise ValueError(f"allocation_outside_universe: {sorted(outsiders)}")

    unit = 0.0 if not products else 1.0 / len(products)
    return {
        product: validated_directions.get(product, 0.0) * unit for product in products
    }


def active_weights(directions: Directions) -> dict[str, float]:
    """Equal-weight only active positions, preserving mapping order."""
    validated = _directions(directions)
    active_count = sum(side != 0.0 for side in validated.values())
    unit = 0.0 if active_count == 0 else 1.0 / active_count
    return {
        product: side * unit if side != 0.0 else 0.0
        for product, side in validated.items()
    }


__all__ = ["active_weights", "fixed_universe_weights"]
