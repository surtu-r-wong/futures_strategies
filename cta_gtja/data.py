"""Data loading contract for CTA factor-combo strategies.

The current project does not yet have commodity-futures tables in PostgreSQL.
This module keeps the strategy layer independent from storage by accepting a
small long-table contract that can be backed by CSV, Parquet, Wind, or PG later.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import pandas as pd


@dataclass(frozen=True)
class CTADataSet:
    """In-memory commodity futures data.

    Required ``prices`` columns:
        ``trade_date, symbol, open, close``.

    Optional ``prices`` columns:
        ``volume, amount, open_interest, contract``.

    Optional ``fundamentals`` columns:
        ``trade_date, symbol, spot, basis_rate, inventory,
        warehouse_receipt, profit``.

    ``symbol`` is the continuous commodity variety key, not a specific
    contract month.  The upstream data layer should already have mapped each
    symbol to its tradable dominant/secondary contract series.
    """

    prices: pd.DataFrame
    fundamentals: pd.DataFrame
    data_quality: pd.DataFrame = field(default_factory=pd.DataFrame)

    @classmethod
    def from_dir(cls, data_dir: str | Path) -> "CTADataSet":
        root = Path(data_dir)
        prices = _read_table(root / "prices")
        fundamentals = _read_optional_table(root / "fundamentals")
        return cls(prices=normalize_prices(prices), fundamentals=normalize_fundamentals(fundamentals))

    @property
    def symbols(self) -> list[str]:
        return sorted(self.prices["symbol"].dropna().astype(str).unique().tolist())

    @property
    def dates(self) -> list[date]:
        return sorted(pd.to_datetime(self.prices["trade_date"]).dt.date.unique().tolist())

    def slice(
        self,
        *,
        symbols: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> "CTADataSet":
        prices = _slice_frame(self.prices, symbols=symbols, start=start, end=end)
        fundamentals = _slice_frame(self.fundamentals, symbols=symbols, start=start, end=end)
        quality = self.data_quality.copy()
        if symbols is not None and not quality.empty and "base_symbol" in quality.columns:
            quality = quality[quality["base_symbol"].isin(symbols)].copy()
        return CTADataSet(prices=prices, fundamentals=fundamentals, data_quality=quality)

    def price_matrix(self, column: str, *, symbols: list[str] | None = None) -> pd.DataFrame:
        return _matrix(self.prices, column, symbols=symbols)

    def fundamental_matrix(self, column: str, *, symbols: list[str] | None = None) -> pd.DataFrame:
        return _matrix(self.fundamentals, column, symbols=symbols)


def normalize_prices(df: pd.DataFrame) -> pd.DataFrame:
    required = {"trade_date", "symbol", "open", "close"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CTA prices missing required columns: {sorted(missing)}")
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out["symbol"] = out["symbol"].astype(str)
    for col in ["open", "close", "volume", "amount", "open_interest"]:
        if col in out.columns:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def normalize_fundamentals(df: pd.DataFrame | None) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame(columns=["trade_date", "symbol"])
    required = {"trade_date", "symbol"}
    missing = required - set(df.columns)
    if missing:
        raise ValueError(f"CTA fundamentals missing required columns: {sorted(missing)}")
    out = df.copy()
    out["trade_date"] = pd.to_datetime(out["trade_date"]).dt.date
    out["symbol"] = out["symbol"].astype(str)
    for col in out.columns:
        if col not in {"trade_date", "symbol", "contract"}:
            out[col] = pd.to_numeric(out[col], errors="coerce")
    return out.sort_values(["symbol", "trade_date"]).reset_index(drop=True)


def _read_table(stem: Path) -> pd.DataFrame:
    csv_path = stem.with_suffix(".csv")
    parquet_path = stem.with_suffix(".parquet")
    if csv_path.exists():
        return pd.read_csv(csv_path)
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    raise FileNotFoundError(f"expected {csv_path} or {parquet_path}")


def _read_optional_table(stem: Path) -> pd.DataFrame:
    csv_path = stem.with_suffix(".csv")
    parquet_path = stem.with_suffix(".parquet")
    if csv_path.exists():
        return pd.read_csv(csv_path)
    if parquet_path.exists():
        return pd.read_parquet(parquet_path)
    return pd.DataFrame(columns=["trade_date", "symbol"])


def _slice_frame(
    df: pd.DataFrame,
    *,
    symbols: list[str] | None,
    start: date | None,
    end: date | None,
) -> pd.DataFrame:
    if df.empty:
        return df.copy()
    mask = pd.Series(True, index=df.index)
    if symbols is not None:
        mask &= df["symbol"].isin(symbols)
    if start is not None:
        mask &= df["trade_date"] >= start
    if end is not None:
        mask &= df["trade_date"] <= end
    return df.loc[mask].copy()


def _matrix(df: pd.DataFrame, column: str, *, symbols: list[str] | None = None) -> pd.DataFrame:
    if df.empty or column not in df.columns:
        return pd.DataFrame()
    use = df[["trade_date", "symbol", column]].copy()
    if symbols is not None:
        use = use[use["symbol"].isin(symbols)]
    mat = (
        use.drop_duplicates(["trade_date", "symbol"], keep="last")
        .pivot(index="trade_date", columns="symbol", values=column)
        .sort_index()
    )
    if symbols is not None:
        mat = mat.reindex(columns=symbols)
    return mat.astype(float)

