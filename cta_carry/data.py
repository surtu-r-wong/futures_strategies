"""Contract-level daily data contract for the Carry strategy."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math
from pathlib import Path

import pandas as pd

from .curve import ContractParseError, parse_contract


REQUIRED_COLUMNS = (
    "trade_date",
    "contract",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "oi",
    "turnover",
)
AUDIT_COLUMNS = (
    "object_type",
    "object_id",
    "trade_date",
    "check",
    "status",
    "action",
    "reason",
)
_NUMERIC_COLUMNS = ("open", "high", "low", "close", "volume", "oi", "turnover")


class DataConflictError(ValueError):
    """Raised when one contract has conflicting bars for a trade date."""


@dataclass(frozen=True)
class CarryDataSet:
    """Normalized contract bars and their row-level exclusion audit."""

    prices: pd.DataFrame
    data_quality: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=AUDIT_COLUMNS)
    )

    @classmethod
    def from_dir(cls, root: str | Path) -> "CarryDataSet":
        data_root = Path(root)
        csv_path = data_root / "prices.csv"
        parquet_path = data_root / "prices.parquet"
        if csv_path.exists():
            frame = pd.read_csv(csv_path)
        elif parquet_path.exists():
            frame = pd.read_parquet(parquet_path)
        else:
            raise FileNotFoundError(
                f"expected {csv_path} or {parquet_path}"
            )
        return normalize_contract_daily(frame)

    @property
    def dates(self) -> list[date]:
        return sorted(self.prices["trade_date"].dropna().unique().tolist())

    def slice(
        self,
        *,
        products: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> "CarryDataSet":
        mask = pd.Series(True, index=self.prices.index)
        if products:
            selected = {
                str(product).strip().upper()
                for product in products
                if str(product).strip()
            }
            if selected:
                mask &= self.prices["product"].isin(selected)
        if start is not None:
            mask &= self.prices["trade_date"] >= start
        if end is not None:
            mask &= self.prices["trade_date"] <= end
        return CarryDataSet(
            prices=self.prices.loc[mask].copy().reset_index(drop=True),
            data_quality=self.data_quality.copy(),
        )


def normalize_contract_daily(frame: pd.DataFrame) -> CarryDataSet:
    """Normalize daily contract bars and audit candidates that are unusable."""
    missing = sorted(set(REQUIRED_COLUMNS) - set(frame.columns))
    if missing:
        raise ValueError(f"Carry prices missing required columns: {missing}")

    normalized = frame.drop_duplicates().copy()
    normalized["trade_date"] = pd.to_datetime(
        normalized["trade_date"].astype("string").str.strip(),
        format="mixed",
        errors="coerce",
    ).dt.date
    normalized["contract"] = (
        normalized["contract"].astype(str).str.strip().str.upper()
    )
    numeric_columns = list(_NUMERIC_COLUMNS)
    if "settle" in normalized.columns:
        numeric_columns.append("settle")
    for column in numeric_columns:
        normalized[column] = pd.to_numeric(
            normalized[column], errors="coerce"
        ).astype("float64")

    audit: list[dict[str, object]] = []
    invalid_trade_dates = normalized["trade_date"].isna()
    for _, row in normalized.loc[invalid_trade_dates].iterrows():
        audit.append(
            _exclusion(
                object_id=row["contract"],
                trade_date=row["trade_date"],
                check="trade_date",
                reason="unparseable_trade_date",
            )
        )
    normalized = normalized.loc[~invalid_trade_dates].copy()

    duplicates = normalized.duplicated(
        subset=["trade_date", "contract"], keep=False
    )
    if duplicates.any():
        conflict = normalized.loc[duplicates].iloc[0]
        trade_date = conflict["trade_date"]
        date_text = (
            trade_date.isoformat() if hasattr(trade_date, "isoformat") else str(trade_date)
        )
        raise DataConflictError(
            f"conflicting contract bars for {date_text} {conflict['contract']}"
        )

    accepted: list[dict[str, object]] = []
    for _, row in normalized.iterrows():
        contract = row["contract"]
        trade_date = row["trade_date"]
        try:
            parsed = parse_contract(contract, trade_date)
        except (ContractParseError, TypeError, ValueError) as exc:
            audit.append(
                _exclusion(
                    object_id=contract,
                    trade_date=trade_date,
                    check="contract_parse",
                    reason=str(exc),
                )
            )
            continue

        if not _valid_ohlc(row):
            audit.append(
                _exclusion(
                    object_id=parsed.normalized,
                    trade_date=trade_date,
                    check="ohlc_integrity",
                    reason="OHLC values must be finite, positive, and internally consistent",
                )
            )
            continue

        if not _valid_activity(row):
            audit.append(
                _exclusion(
                    object_id=parsed.normalized,
                    trade_date=trade_date,
                    check="activity_fields",
                    reason="volume, oi, and turnover must be finite and nonnegative",
                )
            )
            continue

        record = row.to_dict()
        record.update(
            contract=parsed.normalized,
            product=parsed.product,
            exchange_suffix=parsed.exchange_suffix,
            delivery_yyyymm=parsed.delivery_yyyymm,
        )
        accepted.append(record)

    derived_columns = ["product", "exchange_suffix", "delivery_yyyymm"]
    price_columns = [
        column for column in normalized.columns if column not in derived_columns
    ]
    price_columns.extend(derived_columns)
    prices = pd.DataFrame(accepted, columns=price_columns)
    prices = prices.sort_values(
        ["trade_date", "product", "contract"]
    ).reset_index(drop=True)
    data_quality = pd.DataFrame(audit, columns=AUDIT_COLUMNS)
    return CarryDataSet(prices=prices, data_quality=data_quality)


def _valid_ohlc(row: pd.Series) -> bool:
    open_price, high, low, close = (
        row["open"],
        row["high"],
        row["low"],
        row["close"],
    )
    values = (open_price, high, low, close)
    if not all(math.isfinite(value) and value > 0 for value in values):
        return False
    return high >= max(open_price, close, low) and low <= min(
        open_price, close, high
    )


def _valid_activity(row: pd.Series) -> bool:
    values = (row["volume"], row["oi"], row["turnover"])
    return all(math.isfinite(value) and value >= 0 for value in values)


def _exclusion(
    *, object_id: str, trade_date: date, check: str, reason: str
) -> dict[str, object]:
    return {
        "object_type": "contract_bar",
        "object_id": object_id,
        "trade_date": trade_date,
        "check": check,
        "status": "excluded",
        "action": "exclude_candidate",
        "reason": reason,
    }
