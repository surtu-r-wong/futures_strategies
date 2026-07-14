# Carry 商品期货日线研究版 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 新增独立的 `cta_carry` 日线研究包，以分合约行情实现可复现、无未来函数、可逐日对账的 Carry 横截面策略。

**Architecture:** 先把配置、合约解析、数据契约、曲线、信号和风险规则实现为纯函数，再由单一日事件状态机组合这些边界；影子收益只读取同一状态机的未缩放权重。PostgreSQL、文件、报告和 CLI 位于外层，现有 `cta_gtja` 行为保持不变，也不依赖 sibling `spread_analyzer`。

**Tech Stack:** Python 3.13, pandas 3, NumPy 2, psycopg2, PyYAML, matplotlib, openpyxl, pyarrow, pytest, PostgreSQL `public.futures_daily`

---

## Implementation constraints

- 以已批准设计 `docs/superpowers/specs/2026-07-14-carry-daily-strategy-design.md` 为唯一规则来源。
- 所有信号只读交易日 `t` 收盘及更早数据，订单在下一策略交易日开盘执行。
- `cta_carry/curve.py` 保持纯 pandas 边界，不导入数据库、CLI、报告或回测状态。
- 正式组合和影子收益共享一份 `PositionState`；波动率缩放绝不回写方向、合约、档位或极值。
- 每个任务遵循 red → green → regression → commit；不要把多个任务压成一个提交。
- PostgreSQL 冒烟测试是显式检查，不成为离线 pytest 的前置条件。

## File map

- Create: `cta_carry/__init__.py` — 稳定公开接口。
- Create: `cta_carry/config.py` — 不可变配置与查询前校验。
- Create: `cta_carry/data.py` — 分合约数据集、规范化、质量审计和 CSV/Parquet 文件源。
- Create: `cta_carry/pg_source.py` — 只读 `public.futures_daily` 查询。
- Create: `cta_carry/curve.py` — 日期感知合约解析、流动性池、主次选择和 Carry。
- Create: `cta_carry/signals.py` — 横截面排序、动量与缩量缩仓过滤。
- Create: `cta_carry/risk.py` — 合约 ATR、影子波动率、杠杆裁剪和吊灯状态。
- Create: `cta_carry/backtest.py` — 开盘到开盘事件循环、订单、账本、异常和结果对象。
- Create: `cta_carry/report.py` — 指标、Excel、PNG 和控制台摘要。
- Create: `cta_carry/__main__.py` — CLI 参数和编排。
- Create: `tests/carry_fixtures.py` — 多品种、多合约确定性合成数据。
- Create: `tests/test_carry_config.py`
- Create: `tests/test_carry_data.py`
- Create: `tests/test_carry_pg_source.py`
- Create: `tests/test_carry_curve.py`
- Create: `tests/test_carry_risk.py`
- Create: `tests/test_carry_signals.py`
- Create: `tests/test_carry_backtest.py`
- Create: `tests/test_carry_report_cli.py`
- Modify: `requirements.txt` — 增加 Parquet 引擎。
- Modify: `README.md` — 运行方式、数据需求、产物和日线近似。

### Task 1: Configuration and date-aware contract parsing

**Files:**
- Create: `cta_carry/__init__.py`
- Create: `cta_carry/config.py`
- Create: `cta_carry/curve.py`
- Create: `tests/test_carry_config.py`
- Create: `tests/test_carry_curve.py`

- [ ] **Step 1: Write failing configuration tests**

Create `tests/test_carry_config.py`:

```python
from dataclasses import asdict

import pytest

from cta_carry.config import CarryConfig


def test_carry_config_defaults_are_the_approved_research_defaults():
    cfg = CarryConfig()

    assert asdict(cfg) == {
        "liquidity_window": 120,
        "liquidity_threshold": 5_000_000_000.0,
        "carry_window": 10,
        "selection_fraction": 0.20,
        "momentum_window": 10,
        "atr_window": 20,
        "atr_risk_budget": 0.005,
        "vol_window": 252,
        "min_shadow_active_days": 126,
        "target_vol": 0.15,
        "max_gross_leverage": 4.0,
        "chandelier_atr_multiple": 2.5,
        "stop_tranches": 3,
        "cost_bps": 13.0,
        "prewarm_calendar_days": 730,
    }


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"liquidity_window": 0}, "liquidity_window"),
        ({"selection_fraction": 0.0}, "selection_fraction"),
        ({"selection_fraction": 0.51}, "selection_fraction"),
        ({"vol_window": 10, "min_shadow_active_days": 11}, "min_shadow_active_days"),
        ({"target_vol": 0.0}, "target_vol"),
        ({"cost_bps": -1.0}, "cost_bps"),
    ],
)
def test_carry_config_rejects_invalid_values(overrides, message):
    with pytest.raises(ValueError, match=message):
        CarryConfig(**overrides)
```

- [ ] **Step 2: Run the configuration tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_config.py -q
```

Expected: collection fails with `ModuleNotFoundError: No module named 'cta_carry'`.

- [ ] **Step 3: Implement the immutable configuration**

Create `cta_carry/config.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
import math


@dataclass(frozen=True)
class CarryConfig:
    liquidity_window: int = 120
    liquidity_threshold: float = 5_000_000_000.0
    carry_window: int = 10
    selection_fraction: float = 0.20
    momentum_window: int = 10
    atr_window: int = 20
    atr_risk_budget: float = 0.005
    vol_window: int = 252
    min_shadow_active_days: int = 126
    target_vol: float = 0.15
    max_gross_leverage: float = 4.0
    chandelier_atr_multiple: float = 2.5
    stop_tranches: int = 3
    cost_bps: float = 13.0
    prewarm_calendar_days: int = 730

    def __post_init__(self) -> None:
        for name in (
            "liquidity_window",
            "carry_window",
            "momentum_window",
            "atr_window",
            "vol_window",
            "stop_tranches",
            "prewarm_calendar_days",
        ):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError(f"{name} must be a positive integer")
        if not 0.0 < self.selection_fraction <= 0.5:
            raise ValueError("selection_fraction must be in (0, 0.5]")
        if not 1 <= self.min_shadow_active_days <= self.vol_window:
            raise ValueError("min_shadow_active_days must be in [1, vol_window]")
        for name in (
            "atr_risk_budget",
            "target_vol",
            "max_gross_leverage",
            "chandelier_atr_multiple",
        ):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value <= 0.0:
                raise ValueError(f"{name} must be finite and positive")
        for name in ("liquidity_threshold", "cost_bps"):
            value = float(getattr(self, name))
            if not math.isfinite(value) or value < 0.0:
                raise ValueError(f"{name} must be finite and non-negative")
```

Create `cta_carry/__init__.py` initially as:

```python
"""Daily contract-level Carry futures research."""

from cta_carry.config import CarryConfig

__all__ = ["CarryConfig"]
```

- [ ] **Step 4: Run configuration tests and verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_config.py -q
```

Expected: all tests in the file pass.

- [ ] **Step 5: Write failing parser tests**

Create `tests/test_carry_curve.py`:

```python
from datetime import date

import pytest

from cta_carry.curve import ContractParseError, parse_contract


@pytest.mark.parametrize(
    ("contract", "trade_date", "product", "exchange", "delivery"),
    [
        ("rb2410.shf", date(2024, 1, 2), "RB", "SHF", 202410),
        ("TA409.CZC", date(2024, 1, 2), "TA", "CZC", 202409),
        ("CF001.CZC", date(2019, 12, 2), "CF", "CZC", 202001),
        ("CU9912.SHF", date(1999, 1, 4), "CU", "SHF", 199912),
        ("m2501", date(2024, 5, 6), "M", "", 202501),
    ],
)
def test_parse_contract_is_date_aware(
    contract, trade_date, product, exchange, delivery
):
    parsed = parse_contract(contract, trade_date)

    assert parsed.product == product
    assert parsed.exchange_suffix == exchange
    assert parsed.delivery_yyyymm == delivery


@pytest.mark.parametrize("contract", ["RB2413.SHF", "2410.SHF", "RB", "RB1901.SHF"])
def test_parse_contract_rejects_invalid_or_unreasonable_codes(contract):
    with pytest.raises(ContractParseError):
        parse_contract(contract, date(2024, 1, 2))
```

- [ ] **Step 6: Run parser tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_curve.py -q
```

Expected: collection fails because `cta_carry.curve` does not exist.

- [ ] **Step 7: Implement the pure parser**

Create `cta_carry/curve.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import re


_CONTRACT_RE = re.compile(
    r"^(?P<product>[A-Za-z]+)(?P<digits>\d{3,4})(?:\.(?P<exchange>[A-Za-z]+))?$"
)


class ContractParseError(ValueError):
    pass


@dataclass(frozen=True)
class ContractId:
    product: str
    exchange_suffix: str
    delivery_year: int
    delivery_month: int
    normalized: str

    @property
    def delivery_yyyymm(self) -> int:
        return self.delivery_year * 100 + self.delivery_month


def parse_contract(contract: str, trade_date: date) -> ContractId:
    text = str(contract).strip().upper()
    match = _CONTRACT_RE.fullmatch(text)
    if match is None:
        raise ContractParseError(f"unparseable contract: {contract!r}")

    digits = match.group("digits")
    month = int(digits[-2:])
    if not 1 <= month <= 12:
        raise ContractParseError(f"invalid delivery month: {contract!r}")

    start_month = trade_date.year * 12 + trade_date.month
    candidates: list[tuple[int, int]] = []
    if len(digits) == 4:
        year_suffix = int(digits[:2])
        century = trade_date.year // 100
        years = [(century + offset) * 100 + year_suffix for offset in (-1, 0, 1)]
    else:
        year_digit = int(digits[0])
        years = [
            year
            for year in range(trade_date.year - 10, trade_date.year + 11)
            if year % 10 == year_digit
        ]

    for year in years:
        delta = year * 12 + month - start_month
        if 0 <= delta <= 120:
            candidates.append((delta, year))
    candidates.sort()
    if not candidates or (
        len(candidates) > 1 and candidates[0][0] == candidates[1][0]
    ):
        raise ContractParseError(
            f"no unique delivery month for {contract!r} at {trade_date}"
        )

    year = candidates[0][1]
    product = match.group("product").upper()
    exchange = (match.group("exchange") or "").upper()
    normalized = f"{product}{digits}" + (f".{exchange}" if exchange else "")
    return ContractId(product, exchange, year, month, normalized)
```

- [ ] **Step 8: Run Task 1 tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_config.py tests/test_carry_curve.py -q
```

Expected: all Task 1 tests pass.

```bash
git add cta_carry/__init__.py cta_carry/config.py cta_carry/curve.py tests/test_carry_config.py tests/test_carry_curve.py
git commit -m "feat: add Carry configuration and contract parser"
```

### Task 2: Contract-level data contract, file source, and PostgreSQL source

**Files:**
- Create: `cta_carry/data.py`
- Create: `cta_carry/pg_source.py`
- Create: `tests/test_carry_data.py`
- Create: `tests/test_carry_pg_source.py`
- Modify: `requirements.txt`

- [ ] **Step 1: Add the Parquet dependency**

Append this exact requirement:

```text
pyarrow>=15.0
```

Install the locked project requirements:

```bash
.venv/bin/python -m pip install -r requirements.txt
```

Expected: `pyarrow` installs successfully and the command exits 0.

- [ ] **Step 2: Write failing normalization and file-source tests**

Create `tests/test_carry_data.py`:

```python
from datetime import date

import pandas as pd
import pytest

from cta_carry.data import CarryDataSet, DataConflictError, normalize_contract_daily


def _valid_row(**overrides):
    row = {
        "trade_date": "2024-01-02",
        "contract": "rb2405.shf",
        "open": 100.0,
        "high": 103.0,
        "low": 99.0,
        "close": 102.0,
        "volume": 1_000.0,
        "oi": 2_000.0,
        "turnover": 6_000_000_000.0,
        "settle": 101.0,
    }
    row.update(overrides)
    return row


def test_normalize_derives_contract_fields_and_drops_exact_duplicates():
    row = _valid_row()
    data = normalize_contract_daily(pd.DataFrame([row, row]))

    assert len(data.prices) == 1
    assert data.prices.loc[0, "trade_date"] == date(2024, 1, 2)
    assert data.prices.loc[0, "contract"] == "RB2405.SHF"
    assert data.prices.loc[0, "product"] == "RB"
    assert data.prices.loc[0, "exchange_suffix"] == "SHF"
    assert data.prices.loc[0, "delivery_yyyymm"] == 202405


def test_normalize_rejects_conflicting_duplicate_keys():
    with pytest.raises(DataConflictError, match="2024-01-02.*RB2405"):
        normalize_contract_daily(
            pd.DataFrame([_valid_row(), _valid_row(close=101.0)])
        )


def test_invalid_candidate_is_excluded_and_audited():
    data = normalize_contract_daily(
        pd.DataFrame(
            [
                _valid_row(),
                _valid_row(contract="TA405.CZC", high=98.0),
                _valid_row(contract="BAD", turnover=-1.0),
            ]
        )
    )

    assert data.prices["contract"].tolist() == ["RB2405.SHF"]
    assert set(data.data_quality["check"]) == {
        "contract_parse",
        "ohlc_integrity",
    }
    assert set(data.data_quality["action"]) == {"exclude_candidate"}


@pytest.mark.parametrize("suffix", ["csv", "parquet"])
def test_from_dir_reads_the_same_contract_for_csv_and_parquet(tmp_path, suffix):
    frame = pd.DataFrame([_valid_row()])
    path = tmp_path / f"prices.{suffix}"
    if suffix == "csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)

    data = CarryDataSet.from_dir(tmp_path)

    assert data.prices["contract"].tolist() == ["RB2405.SHF"]
```

- [ ] **Step 3: Run data tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_data.py -q
```

Expected: collection fails because `cta_carry.data` does not exist.

- [ ] **Step 4: Implement normalization, audit, slicing, and file loading**

Create `cta_carry/data.py` with these public interfaces:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd

from cta_carry.curve import ContractParseError, parse_contract


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


class DataConflictError(ValueError):
    pass


@dataclass(frozen=True)
class CarryDataSet:
    prices: pd.DataFrame
    data_quality: pd.DataFrame = field(
        default_factory=lambda: pd.DataFrame(columns=AUDIT_COLUMNS)
    )

    @classmethod
    def from_dir(cls, data_dir: str | Path) -> "CarryDataSet":
        root = Path(data_dir)
        csv_path = root / "prices.csv"
        parquet_path = root / "prices.parquet"
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
        return sorted(self.prices["trade_date"].unique().tolist())

    def slice(
        self,
        *,
        products: list[str] | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> "CarryDataSet":
        mask = pd.Series(True, index=self.prices.index)
        if products:
            wanted = {value.upper() for value in products}
            mask &= self.prices["product"].isin(wanted)
        if start is not None:
            mask &= self.prices["trade_date"] >= start
        if end is not None:
            mask &= self.prices["trade_date"] <= end
        return CarryDataSet(
            prices=self.prices.loc[mask].reset_index(drop=True),
            data_quality=self.data_quality.copy(),
        )


def normalize_contract_daily(frame: pd.DataFrame) -> CarryDataSet:
    missing = set(REQUIRED_COLUMNS) - set(frame.columns)
    if missing:
        raise ValueError(
            f"Carry prices missing required columns: {sorted(missing)}"
        )
    out = frame.copy().drop_duplicates().reset_index(drop=True)
    out["trade_date"] = pd.to_datetime(
        out["trade_date"], errors="coerce"
    ).dt.date
    out["contract"] = out["contract"].astype(str).str.strip().str.upper()
    for column in REQUIRED_COLUMNS[2:] + (("settle",) if "settle" in out else ()):
        out[column] = pd.to_numeric(out[column], errors="coerce")

    duplicate = out.duplicated(["trade_date", "contract"], keep=False)
    if duplicate.any():
        row = out.loc[duplicate, ["trade_date", "contract"]].iloc[0]
        raise DataConflictError(
            f"conflicting duplicate at {row.trade_date} {row.contract}"
        )

    audit: list[dict[str, object]] = []
    parsed_values: list[object | None] = []
    for row in out.itertuples(index=False):
        try:
            parsed_values.append(parse_contract(row.contract, row.trade_date))
        except (ContractParseError, TypeError):
            parsed_values.append(None)
            audit.append(
                _audit_row(
                    row.contract,
                    row.trade_date,
                    "contract_parse",
                    "unparseable_contract",
                )
            )
    out["_parsed"] = parsed_values
    parse_ok = out["_parsed"].notna()

    numeric = out[list(REQUIRED_COLUMNS[2:])].astype(float)
    finite = np.isfinite(numeric)
    ohlc = out[["open", "high", "low", "close"]].astype(float)
    ohlc_ok = (
        finite[["open", "high", "low", "close"]].all(axis=1)
        & (ohlc > 0.0).all(axis=1)
        & (ohlc["high"] >= ohlc[["open", "close", "low"]].max(axis=1))
        & (ohlc["low"] <= ohlc[["open", "close", "high"]].min(axis=1))
    )
    activity_ok = (
        finite[["volume", "oi", "turnover"]].all(axis=1)
        & (numeric[["volume", "oi", "turnover"]] >= 0.0).all(axis=1)
    )
    for index in out.index[parse_ok & ~ohlc_ok]:
        audit.append(
            _audit_row(
                out.at[index, "contract"],
                out.at[index, "trade_date"],
                "ohlc_integrity",
                "non_finite_non_positive_or_inconsistent_ohlc",
            )
        )
    for index in out.index[parse_ok & ohlc_ok & ~activity_ok]:
        audit.append(
            _audit_row(
                out.at[index, "contract"],
                out.at[index, "trade_date"],
                "activity_fields",
                "non_finite_or_negative_volume_oi_turnover",
            )
        )

    out = out.loc[parse_ok & ohlc_ok & activity_ok].copy()
    out["product"] = out["_parsed"].map(lambda value: value.product)
    out["exchange_suffix"] = out["_parsed"].map(
        lambda value: value.exchange_suffix
    )
    out["delivery_yyyymm"] = out["_parsed"].map(
        lambda value: value.delivery_yyyymm
    )
    out["contract"] = out["_parsed"].map(lambda value: value.normalized)
    out = out.drop(columns="_parsed").sort_values(
        ["trade_date", "product", "contract"]
    )
    quality = pd.DataFrame.from_records(audit, columns=AUDIT_COLUMNS)
    return CarryDataSet(
        prices=out.reset_index(drop=True),
        data_quality=quality,
    )


def _audit_row(
    contract: str,
    trade_date: date,
    check: str,
    reason: str,
) -> dict[str, object]:
    return {
        "object_type": "contract_bar",
        "object_id": contract,
        "trade_date": trade_date,
        "check": check,
        "status": "excluded",
        "action": "exclude_candidate",
        "reason": reason,
    }
```

- [ ] **Step 5: Run data tests and verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_data.py -q
```

Expected: all normalization, audit, CSV, and Parquet tests pass.

- [ ] **Step 6: Write failing PostgreSQL query tests**

Create `tests/test_carry_pg_source.py`:

```python
from contextlib import contextmanager
from datetime import date

import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.pg_source import (
    FINANCIAL_FUTURES,
    _contract_query,
    load_public_carry_data,
)


def test_contract_query_uses_prewarm_products_and_financial_exclusion():
    sql, params = _contract_query(
        query_start=date(2022, 1, 1),
        end=date(2024, 1, 1),
        products=["rb", "TA"],
    )

    assert "FROM public.futures_daily" in sql
    assert "symbol AS contract" in sql
    assert "trade_date >= %(query_start)s" in sql
    assert params["query_start"] == date(2022, 1, 1)
    assert params["products"] == ["RB", "TA"]
    assert set(params["excluded_products"]) == set(FINANCIAL_FUTURES)


def test_public_loader_expands_start_by_730_days(monkeypatch):
    captured = {}

    @contextmanager
    def fake_connection(_):
        yield object()

    def fake_read_sql(sql, conn, *, params):
        captured.update(params)
        return pd.DataFrame(
            [
                {
                    "trade_date": "2022-01-01",
                    "contract": "RB2205.SHF",
                    "open": 100.0,
                    "high": 102.0,
                    "low": 99.0,
                    "close": 101.0,
                    "volume": 100.0,
                    "oi": 200.0,
                    "turnover": 6_000_000_000.0,
                    "settle": 100.5,
                }
            ]
        )

    monkeypatch.setattr(
        "cta_carry.pg_source.resolve_settings_path", lambda: "settings.yaml"
    )
    monkeypatch.setattr(
        "cta_carry.pg_source.load_config",
        lambda _: {"database": {"name": "unused"}},
    )
    monkeypatch.setattr(
        "cta_carry.pg_source.pg_config_from",
        lambda cfg, use_test=False: cfg["database"],
    )
    monkeypatch.setattr("cta_carry.pg_source.get_connection", fake_connection)
    monkeypatch.setattr("cta_carry.pg_source._read_sql", fake_read_sql)

    load_public_carry_data(
        start=date(2024, 1, 1),
        end=date(2024, 2, 1),
        config=CarryConfig(),
    )

    assert captured["query_start"] == date(2022, 1, 1)
    assert captured["end"] == date(2024, 2, 1)
```

- [ ] **Step 7: Run PostgreSQL tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_pg_source.py -q
```

Expected: collection fails because `cta_carry.pg_source` does not exist.

- [ ] **Step 8: Implement the read-only PostgreSQL source**

Create `cta_carry/pg_source.py`:

```python
from __future__ import annotations

from datetime import date, timedelta
import warnings

import pandas as pd

from common.config import load_config, resolve_settings_path
from common.db import get_connection, pg_config_from
from cta_carry.config import CarryConfig
from cta_carry.data import CarryDataSet, normalize_contract_daily


FINANCIAL_FUTURES = frozenset(
    {"IF", "IC", "IH", "IM", "T", "TF", "TL", "TS"}
)


def load_public_carry_data(
    *,
    start: date,
    end: date,
    config: CarryConfig,
    products: list[str] | None = None,
    config_path=None,
    use_test: bool = False,
) -> CarryDataSet:
    query_start = start - timedelta(days=config.prewarm_calendar_days)
    cfg = load_config(config_path or resolve_settings_path())
    pg = pg_config_from(cfg, use_test=use_test).copy()
    pg["schema"] = "public"
    sql, params = _contract_query(
        query_start=query_start,
        end=end,
        products=products,
    )
    with get_connection(pg) as conn:
        raw = _read_sql(sql, conn, params=params)
    return normalize_contract_daily(raw)


def _contract_query(
    *,
    query_start: date,
    end: date,
    products: list[str] | None,
) -> tuple[str, dict[str, object]]:
    product_expr = "UPPER(substring(symbol from '^[A-Za-z]+'))"
    clauses = [
        "trade_date >= %(query_start)s",
        "trade_date <= %(end)s",
        f"NOT ({product_expr} = ANY(%(excluded_products)s))",
    ]
    params: dict[str, object] = {
        "query_start": query_start,
        "end": end,
        "excluded_products": sorted(FINANCIAL_FUTURES),
    }
    if products:
        clauses.append(f"{product_expr} = ANY(%(products)s)")
        params["products"] = sorted(
            {value.strip().upper() for value in products if value.strip()}
        )
    where = " AND ".join(clauses)
    sql = f"""
        SELECT
            trade_date,
            symbol AS contract,
            open::float AS open,
            high::float AS high,
            low::float AS low,
            close::float AS close,
            volume::float AS volume,
            oi::float AS oi,
            turnover::float AS turnover,
            settle::float AS settle
        FROM public.futures_daily
        WHERE {where}
        ORDER BY trade_date, symbol
    """
    return sql, params


def _read_sql(sql: str, conn, *, params: dict[str, object]) -> pd.DataFrame:
    with warnings.catch_warnings():
        warnings.filterwarnings(
            "ignore",
            message="pandas only supports SQLAlchemy connectable.*",
            category=UserWarning,
        )
        return pd.read_sql_query(sql, conn, params=params)
```

- [ ] **Step 9: Run Task 2 regression and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_data.py tests/test_carry_pg_source.py tests/test_cta_pg_source.py -q
```

Expected: all selected tests pass and existing CTA PG behavior is unchanged.

```bash
git add requirements.txt cta_carry/data.py cta_carry/pg_source.py tests/test_carry_data.py tests/test_carry_pg_source.py
git commit -m "feat: add Carry contract data sources"
```

### Task 3: Liquidity universe, dominant/secondary selection, and Carry curve

**Files:**
- Modify: `cta_carry/curve.py`
- Extend: `tests/test_carry_curve.py`

- [ ] **Step 1: Add failing curve tests**

Append to `tests/test_carry_curve.py`:

```python
import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.curve import build_curve
from cta_carry.data import normalize_contract_daily


def _curve_row(
    trade_date,
    contract,
    *,
    close,
    oi,
    volume,
    turnover=6_000_000_000.0,
):
    return {
        "trade_date": trade_date,
        "contract": contract,
        "open": close,
        "high": close + 1.0,
        "low": close - 1.0,
        "close": close,
        "volume": volume,
        "oi": oi,
        "turnover": turnover,
    }


def test_liquidity_window_is_complete_and_shifted_one_product_day():
    dates = pd.bdate_range("2024-01-02", periods=4)
    rows = []
    for index, day in enumerate(dates):
        rows.extend(
            [
                _curve_row(day, "RB2405.SHF", close=100, oi=300, volume=100),
                _curve_row(day, "RB2410.SHF", close=110, oi=200, volume=90),
            ]
        )
    cfg = CarryConfig(
        liquidity_window=2,
        liquidity_threshold=10_000_000_000.0,
        carry_window=1,
    )

    result = build_curve(normalize_contract_daily(pd.DataFrame(rows)).prices, cfg)

    assert result.curve["trade_date"].tolist() == list(dates[2:].date)
    first = result.curve.iloc[0]
    assert first["liquidity_mean"] == pytest.approx(12_000_000_000.0)


def test_main_ties_use_volume_then_code_and_secondary_must_be_later():
    rows = [
        _curve_row("2024-01-02", "RB2401.SHF", close=98, oi=200, volume=999),
        _curve_row("2024-01-02", "RB2405.SHF", close=100, oi=300, volume=100),
        _curve_row("2024-01-02", "RB2410.SHF", close=110, oi=300, volume=200),
        _curve_row("2024-01-02", "RB2501.SHF", close=120, oi=250, volume=300),
        _curve_row("2024-01-03", "RB2401.SHF", close=98, oi=200, volume=999),
        _curve_row("2024-01-03", "RB2405.SHF", close=100, oi=300, volume=100),
        _curve_row("2024-01-03", "RB2410.SHF", close=110, oi=300, volume=200),
        _curve_row("2024-01-03", "RB2501.SHF", close=120, oi=250, volume=300),
    ]
    cfg = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=1,
    )

    result = build_curve(normalize_contract_daily(pd.DataFrame(rows)).prices, cfg)
    row = result.curve.iloc[0]

    assert row["main_contract"] == "RB2410.SHF"
    assert row["secondary_contract"] == "RB2501.SHF"
    assert row["month_gap"] == 3
    assert row["carry_raw"] == pytest.approx((110 / 120 - 1) * 12 / 3)


def test_carry_uses_a_full_product_window_and_emits_selection_audit():
    dates = pd.bdate_range("2024-01-02", periods=4)
    rows = []
    for day in dates:
        rows.extend(
            [
                _curve_row(day, "M2405.DCE", close=100, oi=300, volume=100),
                _curve_row(day, "M2409.DCE", close=110, oi=200, volume=90),
            ]
        )
    cfg = CarryConfig(
        liquidity_window=1,
        liquidity_threshold=0.0,
        carry_window=2,
    )

    result = build_curve(normalize_contract_daily(pd.DataFrame(rows)).prices, cfg)

    assert result.curve["carry_ma"].isna().tolist() == [True, False, False]
    assert {"main", "secondary"}.issubset(set(result.audit["role"]))
```

- [ ] **Step 2: Run the new curve tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_curve.py -q
```

Expected: import fails because `build_curve` does not exist.

- [ ] **Step 3: Add the curve result and liquidity aggregation**

Append these imports and definitions to `cta_carry/curve.py`:

```python
from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from cta_carry.config import CarryConfig


CURVE_AUDIT_COLUMNS = (
    "trade_date",
    "product",
    "contract",
    "in_pool",
    "role",
    "selected",
    "reason",
    "liquidity_mean",
)


@dataclass(frozen=True)
class CurveResult:
    curve: pd.DataFrame
    audit: pd.DataFrame


def aggregate_product_liquidity(
    prices: pd.DataFrame,
    config: CarryConfig,
) -> pd.DataFrame:
    daily = (
        prices.groupby(["product", "trade_date"], as_index=False, sort=True)
        .agg(product_turnover=("turnover", "sum"))
        .sort_values(["product", "trade_date"])
    )
    daily["liquidity_mean"] = daily.groupby(
        "product", sort=False
    )["product_turnover"].transform(
        lambda values: values.rolling(
            config.liquidity_window,
            min_periods=config.liquidity_window,
        ).mean().shift(1)
    )
    daily["in_pool"] = (
        daily["liquidity_mean"].notna()
        & (daily["liquidity_mean"] >= config.liquidity_threshold)
    )
    return daily
```

- [ ] **Step 4: Implement deterministic daily curve selection**

Append:

```python
def build_curve(prices: pd.DataFrame, config: CarryConfig) -> CurveResult:
    liquidity = aggregate_product_liquidity(prices, config)
    lookup = liquidity.set_index(["product", "trade_date"])
    curve_records: list[dict[str, object]] = []
    audit_records: list[dict[str, object]] = []

    for (product, trade_date), group in prices.groupby(
        ["product", "trade_date"], sort=True
    ):
        liq = lookup.loc[(product, trade_date)]
        liquidity_mean = liq["liquidity_mean"]
        if not bool(liq["in_pool"]):
            reason = (
                "liquidity_history_incomplete"
                if pd.isna(liquidity_mean)
                else "below_liquidity_threshold"
            )
            audit_records.extend(
                _audit_candidates(
                    group, False, "", reason, liquidity_mean
                )
            )
            continue

        ranked = group.sort_values(
            ["oi", "volume", "contract"],
            ascending=[False, False, True],
            kind="mergesort",
        )
        main = ranked.iloc[0]
        later = ranked[
            ranked["delivery_yyyymm"] > int(main["delivery_yyyymm"])
        ]
        if later.empty:
            audit_records.extend(
                _audit_candidates(
                    ranked, True, "", "no_strictly_later_contract", liquidity_mean
                )
            )
            continue
        secondary = later.iloc[0]
        month_gap = _month_gap(
            int(main["delivery_yyyymm"]),
            int(secondary["delivery_yyyymm"]),
        )
        carry_raw = (
            float(main["close"]) / float(secondary["close"]) - 1.0
        ) * 12.0 / month_gap
        curve_records.append(
            {
                "trade_date": trade_date,
                "product": product,
                "main_contract": main["contract"],
                "secondary_contract": secondary["contract"],
                "main_delivery_yyyymm": int(main["delivery_yyyymm"]),
                "secondary_delivery_yyyymm": int(
                    secondary["delivery_yyyymm"]
                ),
                "month_gap": month_gap,
                "main_close": float(main["close"]),
                "secondary_close": float(secondary["close"]),
                "main_volume": float(main["volume"]),
                "main_oi": float(main["oi"]),
                "product_turnover": float(liq["product_turnover"]),
                "liquidity_mean": float(liquidity_mean),
                "carry_raw": carry_raw,
            }
        )
        for row in ranked.itertuples(index=False):
            if row.contract == main["contract"]:
                role, selected, reason = "main", True, "highest_oi"
            elif row.contract == secondary["contract"]:
                role, selected, reason = "secondary", True, "later_highest_oi"
            else:
                role, selected, reason = "candidate", False, "not_selected"
            audit_records.append(
                {
                    "trade_date": trade_date,
                    "product": product,
                    "contract": row.contract,
                    "in_pool": True,
                    "role": role,
                    "selected": selected,
                    "reason": reason,
                    "liquidity_mean": float(liquidity_mean),
                }
            )

    curve = pd.DataFrame.from_records(curve_records)
    if not curve.empty:
        curve = curve.sort_values(["product", "trade_date"])
        curve["carry_ma"] = curve.groupby(
            "product", sort=False
        )["carry_raw"].transform(
            lambda values: values.rolling(
                config.carry_window,
                min_periods=config.carry_window,
            ).mean()
        )
        curve = curve.sort_values(
            ["trade_date", "product"]
        ).reset_index(drop=True)
    audit = pd.DataFrame.from_records(
        audit_records, columns=CURVE_AUDIT_COLUMNS
    ).sort_values(["trade_date", "product", "contract"]).reset_index(drop=True)
    return CurveResult(curve=curve, audit=audit)


def _month_gap(near_yyyymm: int, far_yyyymm: int) -> int:
    near_year, near_month = divmod(near_yyyymm, 100)
    far_year, far_month = divmod(far_yyyymm, 100)
    gap = (far_year - near_year) * 12 + far_month - near_month
    if gap <= 0:
        raise ValueError("secondary delivery must be strictly later")
    return gap


def _audit_candidates(
    group: pd.DataFrame,
    in_pool: bool,
    role: str,
    reason: str,
    liquidity_mean: float,
) -> list[dict[str, object]]:
    return [
        {
            "trade_date": row.trade_date,
            "product": row.product,
            "contract": row.contract,
            "in_pool": in_pool,
            "role": role,
            "selected": False,
            "reason": reason,
            "liquidity_mean": liquidity_mean,
        }
        for row in group.itertuples(index=False)
    ]
```

- [ ] **Step 5: Run curve tests, check purity, and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_curve.py tests/test_carry_data.py -q
rg -n "common\.db|psycopg2|argparse|matplotlib|backtest" cta_carry/curve.py
```

Expected: pytest passes; `rg` prints no matches.

```bash
git add cta_carry/curve.py tests/test_carry_curve.py
git commit -m "feat: build deterministic Carry curves"
```

### Task 4: Contract ATR, shadow volatility, leverage, and chandelier state

**Files:**
- Create: `cta_carry/risk.py`
- Create: `tests/test_carry_risk.py`

- [ ] **Step 1: Write failing ATR, volatility, and state tests**

Create `tests/test_carry_risk.py`:

```python
from datetime import date

import numpy as np
import pandas as pd
import pytest

from cta_carry.config import CarryConfig
from cta_carry.risk import (
    PositionState,
    ShadowVolWindow,
    apply_chandelier,
    compute_contract_atr,
    raw_target_weight,
    scale_weights,
    transition_signal,
)


def test_atr_uses_previous_close_of_the_same_contract():
    prices = pd.DataFrame(
        [
            {"trade_date": date(2024, 1, 2), "contract": "A2405", "high": 11, "low": 9, "close": 10},
            {"trade_date": date(2024, 1, 3), "contract": "A2405", "high": 13, "low": 10, "close": 12},
            {"trade_date": date(2024, 1, 2), "contract": "A2409", "high": 101, "low": 99, "close": 100},
            {"trade_date": date(2024, 1, 3), "contract": "A2409", "high": 103, "low": 100, "close": 102},
        ]
    )

    result = compute_contract_atr(prices, CarryConfig(atr_window=2))

    latest = result.set_index(["trade_date", "contract"])
    assert latest.loc[(date(2024, 1, 3), "A2405"), "atr"] == pytest.approx(2.5)
    assert latest.loc[(date(2024, 1, 3), "A2409"), "atr"] == pytest.approx(2.5)


def test_raw_risk_budget_and_gross_cap_are_exact():
    cfg = CarryConfig(atr_risk_budget=0.005, max_gross_leverage=4.0)

    weight = raw_target_weight(
        direction=1,
        strength=0.5,
        close=100.0,
        atr=2.0,
        tranches_remaining=2,
        config=cfg,
    )
    scaled = scale_weights(
        {"A2405": weight, "B2405": -weight},
        vol_scale=100.0,
        config=cfg,
    )

    assert weight == pytest.approx(0.5 * 0.005 * 100 / 2 * 2 / 3)
    assert sum(abs(value) for value in scaled.values()) == pytest.approx(4.0)
    assert np.sign(scaled["A2405"]) == 1
    assert np.sign(scaled["B2405"]) == -1


def test_shadow_window_counts_true_flat_days_but_requires_active_days():
    cfg = CarryConfig(vol_window=4, min_shadow_active_days=2)
    window = ShadowVolWindow(cfg)
    for value, active in [
        (0.0, False),
        (0.0, False),
        (0.01, True),
        (-0.01, True),
    ]:
        window.append(value, active=active)

    estimate = window.estimate()

    assert estimate.observations == 4
    assert estimate.active_days == 2
    assert estimate.ready
    assert estimate.realized_vol == pytest.approx(
        np.std([0.0, 0.0, 0.01, -0.01], ddof=0) * np.sqrt(252)
    )
    assert estimate.vol_scale == pytest.approx(
        cfg.target_vol / estimate.realized_vol
    )


def test_shadow_window_rejects_zero_vol_even_with_enough_active_flags():
    cfg = CarryConfig(vol_window=3, min_shadow_active_days=2)
    window = ShadowVolWindow(cfg)
    for active in [True, True, False]:
        window.append(0.0, active=active)

    assert not window.estimate().ready


def test_chandelier_removes_one_tranche_per_day_and_locks_after_third():
    cfg = CarryConfig(atr_window=2)
    state = PositionState(
        direction=1,
        contract="A2405",
        tranches_remaining=3,
        highest_high=110.0,
    )

    for expected in [2, 1, 0]:
        state, triggered = apply_chandelier(
            state,
            high=111.0,
            low=99.0,
            close=100.0,
            atr=4.0,
            config=cfg,
        )
        assert triggered
        assert state.tranches_remaining == expected

    assert state.direction == 0
    assert state.locked_direction == 1
    locked = transition_signal(state, 1, "A2405", cfg)
    assert locked.direction == 0
    unlocked = transition_signal(state, -1, "A2405", cfg)
    assert unlocked.direction == -1
    assert unlocked.tranches_remaining == 3


def test_same_direction_roll_preserves_tranches_and_resets_extreme():
    cfg = CarryConfig()
    state = PositionState(
        direction=-1,
        contract="A2405",
        tranches_remaining=2,
        lowest_low=90.0,
    )

    rolled = transition_signal(state, -1, "A2409", cfg)

    assert rolled.contract == "A2409"
    assert rolled.tranches_remaining == 2
    assert rolled.lowest_low is None
```

- [ ] **Step 2: Run risk tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_risk.py -q
```

Expected: collection fails because `cta_carry.risk` does not exist.

- [ ] **Step 3: Implement ATR and portfolio scaling**

Create `cta_carry/risk.py` with:

```python
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, replace
import math

import numpy as np
import pandas as pd

from cta_carry.config import CarryConfig


def compute_contract_atr(
    prices: pd.DataFrame,
    config: CarryConfig,
) -> pd.DataFrame:
    ordered = prices.sort_values(["contract", "trade_date"]).copy()
    previous_close = ordered.groupby(
        "contract", sort=False
    )["close"].shift(1)
    ordered["tr"] = pd.concat(
        [
            ordered["high"] - ordered["low"],
            (ordered["high"] - previous_close).abs(),
            (ordered["low"] - previous_close).abs(),
        ],
        axis=1,
    ).max(axis=1)
    ordered["atr"] = ordered.groupby(
        "contract", sort=False
    )["tr"].transform(
        lambda values: values.rolling(
            config.atr_window,
            min_periods=config.atr_window,
        ).mean()
    )
    return ordered[
        ["trade_date", "contract", "tr", "atr"]
    ].sort_values(["trade_date", "contract"]).reset_index(drop=True)


def raw_target_weight(
    *,
    direction: int,
    strength: float,
    close: float,
    atr: float,
    tranches_remaining: int,
    config: CarryConfig,
) -> float:
    values = (strength, close, atr)
    if direction not in (-1, 0, 1) or not all(map(math.isfinite, values)):
        raise ValueError("raw weight inputs must be finite and directional")
    if close <= 0.0 or atr <= 0.0:
        raise ValueError("close and atr must be positive")
    if not 0 <= tranches_remaining <= config.stop_tranches:
        raise ValueError("invalid tranches_remaining")
    return (
        direction
        * strength
        * config.atr_risk_budget
        * close
        / atr
        * tranches_remaining
        / config.stop_tranches
    )


def scale_weights(
    raw_weights: dict[str, float],
    *,
    vol_scale: float,
    config: CarryConfig,
) -> dict[str, float]:
    if not math.isfinite(vol_scale) or vol_scale <= 0.0:
        raise ValueError("vol_scale must be finite and positive")
    scaled = {
        contract: float(weight) * vol_scale
        for contract, weight in raw_weights.items()
        if weight != 0.0
    }
    gross = sum(abs(value) for value in scaled.values())
    if gross > config.max_gross_leverage:
        multiplier = config.max_gross_leverage / gross
        scaled = {
            contract: value * multiplier
            for contract, value in scaled.items()
        }
    if not all(math.isfinite(value) for value in scaled.values()):
        raise ValueError("scaled weights must be finite")
    return scaled
```

- [ ] **Step 4: Implement the shadow window**

Append:

```python
@dataclass(frozen=True)
class VolEstimate:
    observations: int
    active_days: int
    realized_vol: float
    vol_scale: float
    ready: bool


class ShadowVolWindow:
    def __init__(self, config: CarryConfig):
        self.config = config
        self._returns: deque[float] = deque(maxlen=config.vol_window)
        self._active: deque[bool] = deque(maxlen=config.vol_window)

    def append(self, net_return: float, *, active: bool) -> None:
        value = float(net_return)
        if not math.isfinite(value):
            raise ValueError("shadow return must be finite")
        self._returns.append(value)
        self._active.append(bool(active))

    def estimate(self) -> VolEstimate:
        observations = len(self._returns)
        active_days = sum(self._active)
        realized_vol = (
            float(np.std(self._returns, ddof=0)) * math.sqrt(252)
            if observations
            else float("nan")
        )
        ready = (
            observations == self.config.vol_window
            and active_days >= self.config.min_shadow_active_days
            and math.isfinite(realized_vol)
            and realized_vol > 0.0
        )
        scale = (
            self.config.target_vol / realized_vol
            if ready
            else float("nan")
        )
        return VolEstimate(
            observations=observations,
            active_days=active_days,
            realized_vol=realized_vol,
            vol_scale=scale,
            ready=ready,
        )
```

- [ ] **Step 5: Implement the single discrete position state**

Append:

```python
@dataclass(frozen=True)
class PositionState:
    direction: int = 0
    contract: str | None = None
    tranches_remaining: int = 0
    highest_high: float | None = None
    lowest_low: float | None = None
    locked_direction: int = 0


def transition_signal(
    state: PositionState,
    direction: int,
    contract: str | None,
    config: CarryConfig,
) -> PositionState:
    if direction not in (-1, 0, 1):
        raise ValueError("direction must be -1, 0, or 1")
    if direction == 0:
        return PositionState()
    if state.locked_direction == direction and state.direction == 0:
        return state
    if contract is None:
        raise ValueError("non-zero direction needs a contract")
    if state.direction == direction and state.contract == contract:
        return state
    if state.direction == direction:
        return replace(
            state,
            contract=contract,
            highest_high=None,
            lowest_low=None,
            locked_direction=0,
        )
    return PositionState(
        direction=direction,
        contract=contract,
        tranches_remaining=config.stop_tranches,
    )


def apply_chandelier(
    state: PositionState,
    *,
    high: float,
    low: float,
    close: float,
    atr: float,
    config: CarryConfig,
) -> tuple[PositionState, bool]:
    if state.direction == 0:
        return state, False
    if not all(math.isfinite(value) for value in (high, low, close, atr)):
        raise ValueError("chandelier inputs must be finite")
    if atr <= 0.0:
        raise ValueError("chandelier ATR must be positive")

    if state.direction > 0:
        extreme = max(
            high,
            state.highest_high if state.highest_high is not None else high,
        )
        updated = replace(state, highest_high=extreme)
        triggered = close < extreme - config.chandelier_atr_multiple * atr
    else:
        extreme = min(
            low,
            state.lowest_low if state.lowest_low is not None else low,
        )
        updated = replace(state, lowest_low=extreme)
        triggered = close > extreme + config.chandelier_atr_multiple * atr

    if not triggered:
        return updated, False
    remaining = updated.tranches_remaining - 1
    if remaining > 0:
        return replace(updated, tranches_remaining=remaining), True
    return (
        PositionState(
            direction=0,
            contract=None,
            tranches_remaining=0,
            locked_direction=state.direction,
        ),
        True,
    )
```

- [ ] **Step 6: Run risk tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_risk.py -q
```

Expected: all risk tests pass.

```bash
git add cta_carry/risk.py tests/test_carry_risk.py
git commit -m "feat: add Carry risk and stop state"
```

### Task 5: Cross-sectional Carry signals and momentum/volume/OI filter

**Files:**
- Create: `cta_carry/signals.py`
- Create: `tests/test_carry_signals.py`

- [ ] **Step 1: Write failing signal tests**

Create `tests/test_carry_signals.py`:

```python
from datetime import date, timedelta

import pandas as pd
import pytest

from cta_carry.config import CarryConfig
from cta_carry.signals import build_signals


def _signal_curve(
    products=("A", "B", "C", "D", "E"),
    *,
    periods=3,
):
    records = []
    start = date(2024, 1, 2)
    carries = {"A": -0.5, "B": -0.2, "C": 0.0, "D": 0.2, "E": 0.5}
    for offset in range(periods):
        day = start + timedelta(days=offset)
        for index, product in enumerate(products):
            slope = offset if product in {"A", "B"} else -offset
            records.append(
                {
                    "trade_date": day,
                    "product": product,
                    "main_contract": f"{product}2405",
                    "main_close": 100.0 + index + slope,
                    "main_volume": 100.0,
                    "main_oi": 200.0,
                    "carry_ma": carries[product],
                    "atr": 2.0,
                }
            )
    return pd.DataFrame(records)


def test_signal_selects_signed_extremes_with_stable_k():
    result = build_signals(
        _signal_curve(),
        CarryConfig(momentum_window=2, selection_fraction=0.2),
    )
    latest = result.signals[
        result.signals["trade_date"] == result.signals["trade_date"].max()
    ].set_index("product")

    assert result.signal_ready_date == date(2024, 1, 3)
    assert latest.loc["A", "rank_direction"] == 1
    assert latest.loc["E", "rank_direction"] == -1
    assert latest.loc[["B", "C", "D"], "rank_direction"].eq(0).all()
    assert latest.loc["A", "effective_direction"] == 1
    assert latest.loc["E", "effective_direction"] == -1


def test_opposite_momentum_gets_half_only_when_volume_and_oi_are_both_lower():
    frame = _signal_curve()
    last_day = frame["trade_date"].max()
    frame.loc[
        (frame["trade_date"] == last_day) & (frame["product"] == "A"),
        ["main_close", "main_volume", "main_oi"],
    ] = [90.0, 50.0, 100.0]

    half = build_signals(
        frame,
        CarryConfig(momentum_window=2),
    ).signals
    row = half[
        (half["trade_date"] == last_day) & (half["product"] == "A")
    ].iloc[0]
    assert row["signal_strength"] == pytest.approx(0.5)

    frame.loc[
        (frame["trade_date"] == last_day) & (frame["product"] == "A"),
        "main_oi",
    ] = 200.0
    zero = build_signals(
        frame,
        CarryConfig(momentum_window=2),
    ).signals
    row = zero[
        (zero["trade_date"] == last_day) & (zero["product"] == "A")
    ].iloc[0]
    assert row["signal_strength"] == 0.0
    assert row["effective_direction"] == 0


def test_equal_to_moving_average_is_neither_trend_nor_strict_contraction():
    frame = _signal_curve(periods=2)
    last_day = frame["trade_date"].max()
    first_a = frame[
        (frame["trade_date"] != last_day) & (frame["product"] == "A")
    ].iloc[0]
    frame.loc[
        (frame["trade_date"] == last_day) & (frame["product"] == "A"),
        ["main_close", "main_volume", "main_oi"],
    ] = [first_a.main_close, first_a.main_volume, first_a.main_oi]

    result = build_signals(
        frame,
        CarryConfig(momentum_window=2),
    ).signals
    row = result[
        (result["trade_date"] == last_day) & (result["product"] == "A")
    ].iloc[0]

    assert row["signal_strength"] == 0.0


def test_fewer_than_five_ready_products_produces_no_signal():
    result = build_signals(
        _signal_curve(products=("A", "B", "C", "D")),
        CarryConfig(momentum_window=2),
    )

    assert result.signal_ready_date is None
    assert result.signals["effective_direction"].eq(0).all()
```

- [ ] **Step 2: Run signal tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_signals.py -q
```

Expected: collection fails because `cta_carry.signals` does not exist.

- [ ] **Step 3: Implement complete-window momentum features**

Create `cta_carry/signals.py`:

```python
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
import math

import numpy as np
import pandas as pd

from cta_carry.config import CarryConfig


@dataclass(frozen=True)
class SignalResult:
    signals: pd.DataFrame
    signal_ready_date: date | None


def build_signals(
    curve_with_atr: pd.DataFrame,
    config: CarryConfig,
) -> SignalResult:
    work = curve_with_atr.sort_values(
        ["product", "trade_date"]
    ).copy()
    for source, target in (
        ("main_close", "price_ma"),
        ("main_volume", "volume_ma"),
        ("main_oi", "oi_ma"),
    ):
        work[target] = work.groupby(
            "product", sort=False
        )[source].transform(
            lambda values: values.rolling(
                config.momentum_window,
                min_periods=config.momentum_window,
            ).mean()
        )
    required = [
        "carry_ma",
        "price_ma",
        "volume_ma",
        "oi_ma",
        "atr",
    ]
    work["input_ready"] = (
        work[required].notna().all(axis=1)
        & np.isfinite(work[required]).all(axis=1)
        & (work["atr"] > 0.0)
    )

    records: list[dict[str, object]] = []
    signal_ready_date: date | None = None
    for trade_date, day in work.groupby("trade_date", sort=True):
        ready = day[day["input_ready"]].sort_values(
            ["carry_ma", "product"], kind="mergesort"
        )
        enough = len(ready) >= 5
        if enough and signal_ready_date is None:
            signal_ready_date = trade_date
        directions = {product: 0 for product in day["product"]}
        if enough:
            k = max(1, math.floor(len(ready) * config.selection_fraction))
            for row in ready.head(k).itertuples(index=False):
                if row.carry_ma < 0.0:
                    directions[row.product] = 1
            for row in ready.tail(k).itertuples(index=False):
                if row.carry_ma > 0.0:
                    directions[row.product] = -1

        for row in day.sort_values("product").itertuples(index=False):
            rank_direction = directions[row.product]
            strength = _signal_strength(row, rank_direction)
            effective = rank_direction if strength > 0.0 else 0
            record = row._asdict()
            record.update(
                {
                    "rank_direction": rank_direction,
                    "signal_strength": strength,
                    "effective_direction": effective,
                    "reason": (
                        "insufficient_cross_section"
                        if not enough
                        else "rank_and_filter"
                    ),
                }
            )
            records.append(record)
    signals = pd.DataFrame.from_records(records).sort_values(
        ["trade_date", "product"]
    ).reset_index(drop=True)
    return SignalResult(signals=signals, signal_ready_date=signal_ready_date)


def _signal_strength(row, direction: int) -> float:
    if direction == 0 or not row.input_ready:
        return 0.0
    trend_aligned = (
        direction > 0 and row.main_close > row.price_ma
    ) or (
        direction < 0 and row.main_close < row.price_ma
    )
    if trend_aligned:
        return 1.0
    contracted = (
        row.main_volume < row.volume_ma
        and row.main_oi < row.oi_ma
    )
    return 0.5 if contracted else 0.0
```

- [ ] **Step 4: Add the unadjusted roll-momentum test**

Append:

```python
def test_momentum_naturally_splices_unadjusted_main_contract_prices():
    frame = _signal_curve(periods=3)
    middle = sorted(frame["trade_date"].unique())[1]
    last = sorted(frame["trade_date"].unique())[2]
    frame.loc[
        (frame["product"] == "A") & (frame["trade_date"] >= middle),
        "main_contract",
    ] = "A2409"
    frame.loc[
        (frame["product"] == "A") & (frame["trade_date"] == middle),
        "main_close",
    ] = 200.0
    frame.loc[
        (frame["product"] == "A") & (frame["trade_date"] == last),
        "main_close",
    ] = 202.0

    result = build_signals(
        frame,
        CarryConfig(momentum_window=2),
    ).signals
    row = result[
        (result["trade_date"] == last) & (result["product"] == "A")
    ].iloc[0]

    assert row["price_ma"] == pytest.approx(201.0)
```

- [ ] **Step 5: Run signal/risk regression and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_signals.py tests/test_carry_risk.py -q
```

Expected: all selected tests pass.

```bash
git add cta_carry/signals.py tests/test_carry_signals.py
git commit -m "feat: add Carry cross-sectional signals"
```

### Task 6: Contract-weight accounting and explicit report-start boundary

**Files:**
- Create: `cta_carry/backtest.py`
- Create: `tests/test_carry_backtest.py`

- [ ] **Step 1: Write failing ledger tests**

Create `tests/test_carry_backtest.py`:

```python
from datetime import date

import pandas as pd
import pytest

from cta_carry.backtest import (
    ExecutionPriceError,
    WarmupInsufficientError,
    contract_gross_return,
    initial_report_row,
    ordinary_ledger_row,
    weight_turnover,
)


def test_contract_return_and_turnover_use_specific_contract_weights():
    gross = contract_gross_return(
        {"A2405": 0.5, "B2405": -0.25},
        {"A2405": 100.0, "B2405": 200.0},
        {"A2405": 110.0, "B2405": 180.0},
        trade_date=date(2024, 1, 3),
    )
    turnover = weight_turnover(
        {"A2405": 0.5},
        {"A2405": 0.2, "B2405": -0.3},
    )

    assert gross == pytest.approx(0.075)
    assert turnover == pytest.approx(0.6)


def test_missing_held_contract_open_is_fatal():
    with pytest.raises(
        ExecutionPriceError,
        match="2024-01-03.*A2405.*open_price",
    ):
        contract_gross_return(
            {"A2405": 0.5},
            {"A2405": 100.0},
            {},
            trade_date=date(2024, 1, 3),
        )


def test_ordinary_ledger_row_satisfies_the_daily_identity():
    row = ordinary_ledger_row(
        trade_date=date(2024, 1, 3),
        previous_equity=1.2,
        gross_return=0.02,
        turnover=0.6,
        cost_bps=13.0,
    )

    expected_cost = 0.6 * 13 / 10_000
    assert row["cost"] == pytest.approx(expected_cost)
    assert row["net_return"] == pytest.approx(0.02 - expected_cost)
    assert row["equity"] == pytest.approx(
        1.2 * (1 + 0.02 - expected_cost)
    )


def test_report_start_row_keeps_open_trade_cost_but_discards_prewarm_gross():
    row = initial_report_row(
        trade_date=date(2024, 1, 3),
        carried_weights={"A2405": 0.5},
        target_weights={"A2405": 0.2, "B2405": -0.3},
        cost_bps=13.0,
    )

    expected_turnover = 0.6
    expected_cost = expected_turnover * 13 / 10_000
    assert row["gross_return"] == 0.0
    assert row["turnover"] == pytest.approx(expected_turnover)
    assert row["net_return"] == pytest.approx(-expected_cost)
    assert row["equity"] == pytest.approx(1.0 - expected_cost)
    assert row["boundary_type"] == "report_start_initialization"


def test_warmup_error_exposes_both_window_gaps():
    error = WarmupInsufficientError(
        query_start=date(2022, 1, 1),
        report_start_date=date(2024, 1, 2),
        signal_ready_date=date(2023, 1, 2),
        shadow_observations=250,
        active_days=120,
        required_observations=252,
        required_active_days=126,
    )

    assert error.shadow_gap == 2
    assert error.active_gap == 6
    assert "shadow=250/252" in str(error)
    assert "active=120/126" in str(error)
```

- [ ] **Step 2: Run ledger tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_backtest.py -q
```

Expected: collection fails because `cta_carry.backtest` does not exist.

- [ ] **Step 3: Implement typed fatal errors and accounting primitives**

Create `cta_carry/backtest.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
import math

import pandas as pd


class ExecutionPriceError(RuntimeError):
    def __init__(
        self,
        trade_date: date,
        contract: str,
        check: str,
        reason: str,
    ):
        self.trade_date = trade_date
        self.contract = contract
        self.check = check
        self.reason = reason
        super().__init__(
            f"{trade_date} {contract} {check}: {reason}"
        )


class WarmupInsufficientError(RuntimeError):
    def __init__(
        self,
        *,
        query_start: date,
        report_start_date: date,
        signal_ready_date: date | None,
        shadow_observations: int,
        active_days: int,
        required_observations: int,
        required_active_days: int,
    ):
        self.query_start = query_start
        self.report_start_date = report_start_date
        self.signal_ready_date = signal_ready_date
        self.shadow_observations = shadow_observations
        self.active_days = active_days
        self.required_observations = required_observations
        self.required_active_days = required_active_days
        self.shadow_gap = max(0, required_observations - shadow_observations)
        self.active_gap = max(0, required_active_days - active_days)
        super().__init__(
            "risk scaling not ready at "
            f"{report_start_date}; query_start={query_start}; "
            f"signal_ready_date={signal_ready_date}; "
            f"shadow={shadow_observations}/{required_observations}; "
            f"active={active_days}/{required_active_days}"
        )


def contract_gross_return(
    weights: dict[str, float],
    previous_open: dict[str, float],
    current_open: dict[str, float],
    *,
    trade_date: date,
) -> float:
    gross = 0.0
    for contract, weight in weights.items():
        if weight == 0.0:
            continue
        before = previous_open.get(contract)
        after = current_open.get(contract)
        if (
            before is None
            or after is None
            or not math.isfinite(before)
            or not math.isfinite(after)
            or before <= 0.0
            or after <= 0.0
        ):
            raise ExecutionPriceError(
                trade_date,
                contract,
                "open_price",
                "missing_non_finite_or_non_positive_held_price",
            )
        gross += weight * (after / before - 1.0)
    if not math.isfinite(gross):
        raise RuntimeError(f"{trade_date} non-finite gross return")
    return gross


def weight_turnover(
    old_weights: dict[str, float],
    new_weights: dict[str, float],
) -> float:
    contracts = set(old_weights) | set(new_weights)
    turnover = sum(
        abs(new_weights.get(contract, 0.0) - old_weights.get(contract, 0.0))
        for contract in contracts
    )
    if not math.isfinite(turnover):
        raise RuntimeError("non-finite turnover")
    return turnover


def ordinary_ledger_row(
    *,
    trade_date: date,
    previous_equity: float,
    gross_return: float,
    turnover: float,
    cost_bps: float,
) -> dict[str, object]:
    cost = turnover * cost_bps / 10_000.0
    net_return = gross_return - cost
    equity = previous_equity * (1.0 + net_return)
    if not all(math.isfinite(value) for value in (cost, net_return, equity)):
        raise RuntimeError(f"{trade_date} non-finite ledger value")
    return {
        "trade_date": trade_date,
        "gross_return": gross_return,
        "turnover": turnover,
        "cost": cost,
        "net_return": net_return,
        "equity": equity,
        "boundary_type": "ordinary",
    }


def initial_report_row(
    *,
    trade_date: date,
    carried_weights: dict[str, float],
    target_weights: dict[str, float],
    cost_bps: float,
) -> dict[str, object]:
    turnover = weight_turnover(carried_weights, target_weights)
    cost = turnover * cost_bps / 10_000.0
    return {
        "trade_date": trade_date,
        "gross_return": 0.0,
        "turnover": turnover,
        "cost": cost,
        "net_return": -cost,
        "equity": 1.0 - cost,
        "boundary_type": "report_start_initialization",
    }
```

- [ ] **Step 4: Add the result object before the event loop**

Append:

```python
@dataclass(frozen=True)
class CarryBacktestResult:
    daily_returns: pd.DataFrame
    positions: pd.DataFrame
    trades: pd.DataFrame
    signals: pd.DataFrame
    curve_selection: pd.DataFrame
    data_quality: pd.DataFrame
    run_config: pd.DataFrame
    metrics: dict[str, float] = field(default_factory=dict)
```

- [ ] **Step 5: Run ledger tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_backtest.py -q
```

Expected: all ledger and warmup-diagnostic tests pass.

```bash
git add cta_carry/backtest.py tests/test_carry_backtest.py
git commit -m "feat: add Carry contract accounting"
```

### Task 7: Single-state-machine daily event engine

**Files:**
- Create: `tests/carry_fixtures.py`
- Modify: `cta_carry/backtest.py`
- Extend: `tests/test_carry_backtest.py`
- Modify: `cta_carry/__init__.py`

- [ ] **Step 1: Create a deterministic multi-contract fixture**

Create `tests/carry_fixtures.py`:

```python
from datetime import date

import numpy as np
import pandas as pd

from cta_carry.config import CarryConfig
from cta_carry.data import CarryDataSet, normalize_contract_daily


def small_config() -> CarryConfig:
    return CarryConfig(
        liquidity_window=2,
        liquidity_threshold=0.0,
        carry_window=2,
        selection_fraction=0.2,
        momentum_window=2,
        atr_window=2,
        vol_window=4,
        min_shadow_active_days=2,
        prewarm_calendar_days=15,
    )


def make_carry_panel(periods: int = 24) -> CarryDataSet:
    dates = pd.bdate_range("2024-01-02", periods=periods)
    rows = []
    products = ("A", "B", "C", "D", "E")
    for product_index, product in enumerate(products):
        long_side = product in {"A", "B"}
        short_side = product in {"D", "E"}
        for day_index, timestamp in enumerate(dates):
            trend = (
                0.25 * day_index
                if long_side
                else (-0.25 * day_index if short_side else 0.0)
            )
            near_close = (
                100.0
                + product_index * 10.0
                + trend
                + 0.4 * np.sin(day_index + product_index)
            )
            if long_side:
                far_close = near_close * 1.05
            elif short_side:
                far_close = near_close * 0.95
            else:
                far_close = near_close
            for contract, close, oi, volume in (
                (f"{product}2410", near_close, 500.0, 300.0),
                (f"{product}2501", far_close, 300.0, 200.0),
            ):
                open_price = close * (
                    1.0 + 0.002 * np.sin(day_index * 0.7 + product_index)
                )
                rows.append(
                    {
                        "trade_date": timestamp.date(),
                        "contract": contract,
                        "open": open_price,
                        "high": max(open_price, close) + 1.0,
                        "low": min(open_price, close) - 1.0,
                        "close": close,
                        "volume": volume,
                        "oi": oi,
                        "turnover": 3_000_000_000.0,
                    }
                )
    return normalize_contract_daily(pd.DataFrame(rows))
```

- [ ] **Step 2: Add failing end-to-end event tests**

Append to `tests/test_carry_backtest.py`:

```python
import pandas.testing as pdt

from cta_carry.backtest import CarryBacktester
from tests.carry_fixtures import make_carry_panel, small_config


def test_event_engine_warms_state_and_reports_from_requested_date():
    data = make_carry_panel()
    dates = data.dates
    result = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()

    first = result.daily_returns.iloc[0]
    assert first["trade_date"] == dates[12]
    assert first["boundary_type"] == "report_start_initialization"
    assert first["gross_return"] == 0.0
    assert result.run_config.set_index("key").loc[
        "signal_ready_date", "value"
    ]
    assert result.run_config.set_index("key").loc[
        "vol_ready_date", "value"
    ]
    assert result.positions["carried_in"].any()
    assert result.positions["gross_weight"].abs().max() <= 4.0 + 1e-12


def test_every_ordinary_row_and_initial_row_reconcile():
    data = make_carry_panel()
    dates = data.dates
    result = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()
    rows = result.daily_returns.reset_index(drop=True)

    assert rows.loc[0, "equity"] == pytest.approx(
        1.0 - rows.loc[0, "cost"]
    )
    for index in range(1, len(rows)):
        expected = rows.loc[index - 1, "equity"] * (
            1.0 + rows.loc[index, "gross_return"] - rows.loc[index, "cost"]
        )
        assert rows.loc[index, "equity"] == pytest.approx(expected)


def test_future_changes_do_not_change_prior_outputs():
    data = make_carry_panel()
    dates = data.dates
    cutoff = dates[16]
    baseline = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()
    changed_prices = data.prices.copy()
    changed_prices.loc[
        changed_prices["trade_date"] > cutoff,
        ["open", "high", "low", "close"],
    ] *= 1.7
    changed = CarryBacktester(
        type(data)(changed_prices, data.data_quality),
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()

    pdt.assert_frame_equal(
        baseline.daily_returns[
            baseline.daily_returns["trade_date"] <= cutoff
        ].reset_index(drop=True),
        changed.daily_returns[
            changed.daily_returns["trade_date"] <= cutoff
        ].reset_index(drop=True),
    )


def test_same_input_produces_identical_structured_results():
    data = make_carry_panel()
    dates = data.dates
    first = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()
    second = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()

    for name in (
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "data_quality",
        "run_config",
    ):
        pdt.assert_frame_equal(getattr(first, name), getattr(second, name))


def test_report_start_fails_instead_of_sliding_when_scale_is_not_ready():
    data = make_carry_panel(periods=12)
    dates = data.dates

    with pytest.raises(WarmupInsufficientError) as raised:
        CarryBacktester(
            data,
            config=small_config(),
            start=dates[6],
            end=dates[-1],
        ).run()

    assert raised.value.report_start_date == dates[6]
    assert raised.value.shadow_observations < small_config().vol_window
```

- [ ] **Step 3: Run only the new engine tests and verify red**

Run:

```bash
.venv/bin/python -m pytest +  tests/test_carry_backtest.py::test_event_engine_warms_state_and_reports_from_requested_date +  tests/test_carry_backtest.py::test_every_ordinary_row_and_initial_row_reconcile +  tests/test_carry_backtest.py::test_future_changes_do_not_change_prior_outputs +  tests/test_carry_backtest.py::test_same_input_produces_identical_structured_results +  tests/test_carry_backtest.py::test_report_start_fails_instead_of_sliding_when_scale_is_not_ready -q
```

Expected: collection fails because `CarryBacktester` does not exist.

- [ ] **Step 4: Add engine imports and close-plan helpers**

Extend the imports in `cta_carry/backtest.py`:

```python
from dataclasses import asdict, dataclass, field

from common.metrics import summarize
from cta_carry.config import CarryConfig
from cta_carry.curve import build_curve
from cta_carry.data import CarryDataSet
from cta_carry.risk import (
    PositionState,
    ShadowVolWindow,
    VolEstimate,
    apply_chandelier,
    compute_contract_atr,
    raw_target_weight,
    scale_weights,
    transition_signal,
)
from cta_carry.signals import SignalResult, build_signals
```

Append these helpers:

```python
@dataclass(frozen=True)
class ClosePlan:
    states: dict[str, PositionState]
    raw_weights: dict[str, float]
    reasons: dict[str, str]


def _curve_with_atr(
    prices: pd.DataFrame,
    config: CarryConfig,
) -> tuple[object, pd.DataFrame, SignalResult]:
    curve_result = build_curve(prices, config)
    atr = compute_contract_atr(prices, config)
    main_atr = atr.rename(columns={"contract": "main_contract"})
    curve = curve_result.curve.merge(
        main_atr[["trade_date", "main_contract", "atr"]],
        on=["trade_date", "main_contract"],
        how="left",
        validate="one_to_one",
    )
    return curve_result, atr, build_signals(curve, config)


def _close_plan(
    *,
    trade_date: date,
    day_signals: pd.DataFrame,
    bars: dict[str, dict[str, float]],
    atr_by_contract: dict[str, float],
    states: dict[str, PositionState],
    config: CarryConfig,
) -> ClosePlan:
    signal_rows = {
        row.product: row
        for row in day_signals.sort_values("product").itertuples(index=False)
    }
    products = sorted(set(states) | set(signal_rows))
    next_states: dict[str, PositionState] = {}
    raw_weights: dict[str, float] = {}
    reasons: dict[str, str] = {}

    for product in products:
        before = states.get(product, PositionState())
        after_stop = before
        stop_triggered = False
        if before.direction != 0 and before.contract in bars:
            atr = atr_by_contract.get(before.contract)
            if atr is not None and math.isfinite(atr) and atr > 0.0:
                bar = bars[before.contract]
                after_stop, stop_triggered = apply_chandelier(
                    before,
                    high=bar["high"],
                    low=bar["low"],
                    close=bar["close"],
                    atr=atr,
                    config=config,
                )

        signal = signal_rows.get(product)
        direction = (
            int(signal.effective_direction) if signal is not None else 0
        )
        contract = signal.main_contract if signal is not None else None
        after_signal = transition_signal(
            after_stop,
            direction,
            contract,
            config,
        )
        next_states[product] = after_signal
        reasons[product] = _transition_reason(
            before,
            after_signal,
            stop_triggered=stop_triggered,
        )
        if after_signal.direction == 0 or signal is None:
            continue
        raw_weights[after_signal.contract] = raw_target_weight(
            direction=after_signal.direction,
            strength=float(signal.signal_strength),
            close=float(signal.main_close),
            atr=float(signal.atr),
            tranches_remaining=after_signal.tranches_remaining,
            config=config,
        )
    return ClosePlan(next_states, raw_weights, reasons)


def _transition_reason(
    before: PositionState,
    after: PositionState,
    *,
    stop_triggered: bool,
) -> str:
    if before.direction != 0 and after.direction == -before.direction:
        return "direction_reversal"
    if before.direction != 0 and after.direction == 0:
        if stop_triggered:
            return f"stop_{before.tranches_remaining}"
        return "signal_exit"
    if stop_triggered:
        return f"stop_{before.tranches_remaining - after.tranches_remaining}"
    if before.direction == 0 and after.direction != 0:
        return "entry"
    if (
        before.direction == after.direction
        and before.contract != after.contract
    ):
        return "roll"
    return "rebalance"
```

- [ ] **Step 5: Implement deterministic open maps, trade rows, and position rows**

Append:

```python
def _bar_maps(
    day_prices: pd.DataFrame,
) -> tuple[dict[str, float], dict[str, dict[str, float]]]:
    opens: dict[str, float] = {}
    bars: dict[str, dict[str, float]] = {}
    for row in day_prices.sort_values("contract").itertuples(index=False):
        opens[row.contract] = float(row.open)
        bars[row.contract] = {
            "open": float(row.open),
            "high": float(row.high),
            "low": float(row.low),
            "close": float(row.close),
        }
    return opens, bars


def _validate_target_opens(
    old_weights: dict[str, float],
    target_weights: dict[str, float],
    current_open: dict[str, float],
    *,
    trade_date: date,
) -> None:
    changed = {
        contract
        for contract in set(old_weights) | set(target_weights)
        if target_weights.get(contract, 0.0)
        != old_weights.get(contract, 0.0)
    }
    for contract in sorted(changed):
        value = current_open.get(contract)
        if value is None or not math.isfinite(value) or value <= 0.0:
            raise ExecutionPriceError(
                trade_date,
                contract,
                "open_price",
                "missing_non_finite_or_non_positive_rebalance_price",
            )


def _contract_products(prices: pd.DataFrame) -> dict[str, str]:
    return (
        prices[["contract", "product"]]
        .drop_duplicates()
        .set_index("contract")["product"]
        .to_dict()
    )


def _trade_rows(
    *,
    trade_date: date,
    old_weights: dict[str, float],
    new_weights: dict[str, float],
    contract_products: dict[str, str],
    reasons: dict[str, str],
) -> list[dict[str, object]]:
    rows = []
    for contract in sorted(set(old_weights) | set(new_weights)):
        old = old_weights.get(contract, 0.0)
        new = new_weights.get(contract, 0.0)
        if old == new:
            continue
        product = contract_products[contract]
        rows.append(
            {
                "trade_date": trade_date,
                "product": product,
                "contract": contract,
                "old_weight": old,
                "new_weight": new,
                "weight_change": new - old,
                "reason": reasons.get(product, "rebalance"),
            }
        )
    return rows


def _position_rows(
    *,
    trade_date: date,
    states: dict[str, PositionState],
    raw_weights: dict[str, float],
    formal_weights: dict[str, float],
    carried_weights: dict[str, float],
    report_start_date: date,
) -> list[dict[str, object]]:
    gross = sum(abs(value) for value in formal_weights.values())
    rows = []
    for product, state in sorted(states.items()):
        if state.direction == 0 and state.locked_direction == 0:
            continue
        contract = state.contract
        rows.append(
            {
                "trade_date": trade_date,
                "product": product,
                "contract": contract,
                "direction": state.direction,
                "raw_weight": raw_weights.get(contract, 0.0),
                "weight": formal_weights.get(contract, 0.0),
                "gross_leverage": gross,
                "tranches_remaining": state.tranches_remaining,
                "highest_high": state.highest_high,
                "lowest_low": state.lowest_low,
                "locked_direction": state.locked_direction,
                "carried_in": (
                    trade_date == report_start_date
                    and contract is not None
                    and carried_weights.get(contract, 0.0) != 0.0
                ),
            }
        )
    return rows
```

- [ ] **Step 6: Implement the daily event loop**

Append:

```python
class CarryBacktester:
    def __init__(
        self,
        data: CarryDataSet,
        *,
        config: CarryConfig,
        start: date,
        end: date,
    ):
        if start > end:
            raise ValueError("start must be on or before end")
        self.data = data
        self.config = config
        self.start = start
        self.end = end

    def run(self) -> CarryBacktestResult:
        prices = self.data.prices[
            self.data.prices["trade_date"] <= self.end
        ].copy()
        dates = sorted(prices["trade_date"].unique().tolist())
        report_dates = [value for value in dates if value >= self.start]
        if not report_dates:
            raise ValueError("no strategy trading day on or after start")
        report_start_date = report_dates[0]
        query_start = min(dates)

        curve_result, atr_frame, signal_result = _curve_with_atr(
            prices, self.config
        )
        prices_by_date = {
            day: group.copy()
            for day, group in prices.groupby("trade_date", sort=True)
        }
        signals_by_date = {
            day: group.copy()
            for day, group in signal_result.signals.groupby(
                "trade_date", sort=True
            )
        }
        atr_lookup = {
            (row.trade_date, row.contract): float(row.atr)
            for row in atr_frame.itertuples(index=False)
            if pd.notna(row.atr)
        }
        contract_products = _contract_products(prices)

        states: dict[str, PositionState] = {}
        raw_weights: dict[str, float] = {}
        formal_weights: dict[str, float] = {}
        pending_raw: dict[str, float] = {}
        pending_formal: dict[str, float] = {}
        pending_reasons: dict[str, str] = {}
        pending_source_date: date | None = None
        pending_scale_ready = False
        pending_estimate = ShadowVolWindow(self.config).estimate()

        shadow = ShadowVolWindow(self.config)
        shadow_interval_enabled = False
        previous_open: dict[str, float] | None = None
        vol_ready_date: date | None = None
        daily_records: list[dict[str, object]] = []
        position_records: list[dict[str, object]] = []
        trade_records: list[dict[str, object]] = []
        equity: float | None = None

        for index, trade_date in enumerate(dates):
            current_open, bars = _bar_maps(prices_by_date[trade_date])
            carried_formal = dict(formal_weights)
            raw_gross = 0.0
            formal_gross = 0.0
            if previous_open is not None:
                raw_gross = contract_gross_return(
                    raw_weights,
                    previous_open,
                    current_open,
                    trade_date=trade_date,
                )
                formal_gross = contract_gross_return(
                    formal_weights,
                    previous_open,
                    current_open,
                    trade_date=trade_date,
                )

            target_raw = dict(pending_raw)
            target_formal = dict(pending_formal)
            _validate_target_opens(
                raw_weights,
                target_raw,
                current_open,
                trade_date=trade_date,
            )
            _validate_target_opens(
                formal_weights,
                target_formal,
                current_open,
                trade_date=trade_date,
            )
            raw_turnover = weight_turnover(raw_weights, target_raw)
            formal_turnover = weight_turnover(
                formal_weights, target_formal
            )

            if previous_open is not None and shadow_interval_enabled:
                shadow.append(
                    raw_gross
                    - raw_turnover * self.config.cost_bps / 10_000.0,
                    active=sum(abs(value) for value in raw_weights.values()) > 0.0,
                )

            if trade_date == report_start_date and not pending_scale_ready:
                raise WarmupInsufficientError(
                    query_start=query_start,
                    report_start_date=report_start_date,
                    signal_ready_date=signal_result.signal_ready_date,
                    shadow_observations=pending_estimate.observations,
                    active_days=pending_estimate.active_days,
                    required_observations=self.config.vol_window,
                    required_active_days=self.config.min_shadow_active_days,
                )

            if trade_date == report_start_date:
                first = initial_report_row(
                    trade_date=trade_date,
                    carried_weights=carried_formal,
                    target_weights=target_formal,
                    cost_bps=self.config.cost_bps,
                )
                equity = float(first["equity"])
                daily_records.append(first)
            elif trade_date > report_start_date:
                row = ordinary_ledger_row(
                    trade_date=trade_date,
                    previous_equity=float(equity),
                    gross_return=formal_gross,
                    turnover=formal_turnover,
                    cost_bps=self.config.cost_bps,
                )
                equity = float(row["equity"])
                daily_records.append(row)

            raw_weights = target_raw
            formal_weights = target_formal
            if trade_date >= report_start_date:
                trade_records.extend(
                    _trade_rows(
                        trade_date=trade_date,
                        old_weights=carried_formal,
                        new_weights=formal_weights,
                        contract_products=contract_products,
                        reasons=pending_reasons,
                    )
                )
                position_records.extend(
                    _position_rows(
                        trade_date=trade_date,
                        states=states,
                        raw_weights=raw_weights,
                        formal_weights=formal_weights,
                        carried_weights=carried_formal,
                        report_start_date=report_start_date,
                    )
                )

            shadow_interval_enabled = (
                pending_source_date is not None
                and signal_result.signal_ready_date is not None
                and pending_source_date >= signal_result.signal_ready_date
            )
            previous_open = current_open
            if index == len(dates) - 1:
                continue

            atr_today = {
                contract: value
                for (day, contract), value in atr_lookup.items()
                if day == trade_date
            }
            plan = _close_plan(
                trade_date=trade_date,
                day_signals=signals_by_date.get(
                    trade_date,
                    pd.DataFrame(columns=signal_result.signals.columns),
                ),
                bars=bars,
                atr_by_contract=atr_today,
                states=states,
                config=self.config,
            )
            states = plan.states
            pending_raw = plan.raw_weights
            pending_reasons = plan.reasons
            pending_source_date = trade_date

            estimate = shadow.estimate()
            pending_estimate = estimate
            if estimate.ready:
                pending_formal = scale_weights(
                    pending_raw,
                    vol_scale=estimate.vol_scale,
                    config=self.config,
                )
                pending_scale_ready = True
                if vol_ready_date is None:
                    vol_ready_date = dates[index + 1]
            else:
                pending_formal = {}
                pending_scale_ready = False

        daily = pd.DataFrame.from_records(daily_records)
        positions = pd.DataFrame.from_records(position_records)
        trades = pd.DataFrame.from_records(trade_records)
        metrics = summarize(
            daily.set_index("trade_date")["net_return"],
            periods_per_year=252,
            turnover=daily.set_index("trade_date")["turnover"],
        )
        metrics.update(
            {
                "calmar": (
                    metrics["ann_return"] / metrics["max_drawdown"]
                    if metrics["max_drawdown"] > 0.0
                    else float("nan")
                ),
                "total_cost": float(daily["cost"].sum()),
                "avg_gross_leverage": (
                    float(
                        positions.drop_duplicates("trade_date")[
                            "gross_leverage"
                        ].mean()
                    )
                    if not positions.empty
                    else 0.0
                ),
                "max_gross_leverage": (
                    float(positions["gross_leverage"].max())
                    if not positions.empty
                    else 0.0
                ),
            }
        )
        config_rows = [
            {"key": "requested_start", "value": self.start},
            {"key": "requested_end", "value": self.end},
            {"key": "query_start", "value": query_start},
            {"key": "report_start_date", "value": report_start_date},
            {
                "key": "signal_ready_date",
                "value": signal_result.signal_ready_date,
            },
            {"key": "vol_ready_date", "value": vol_ready_date},
        ]
        config_rows.extend(
            {"key": key, "value": value}
            for key, value in asdict(self.config).items()
        )
        return CarryBacktestResult(
            daily_returns=daily.reset_index(drop=True),
            positions=positions.reset_index(drop=True),
            trades=trades.reset_index(drop=True),
            signals=signal_result.signals.reset_index(drop=True),
            curve_selection=curve_result.audit.reset_index(drop=True),
            data_quality=self.data.data_quality.reset_index(drop=True),
            run_config=pd.DataFrame(config_rows),
            metrics=metrics,
        )
```

The deliberate readiness convention in this loop is: the shadow estimate computed at close `t` creates a scaled order for open `t+1`; therefore `vol_ready_date` is the execution date `t+1`. This preserves the approved `vol_ready_date <= report_start_date` rule without using the report-start open return to size an order executed at that same open.

- [ ] **Step 7: Export the stable engine interface**

Replace `cta_carry/__init__.py` with:

```python
"""Daily contract-level Carry futures research."""

from cta_carry.backtest import (
    CarryBacktestResult,
    CarryBacktester,
    ExecutionPriceError,
    WarmupInsufficientError,
)
from cta_carry.config import CarryConfig
from cta_carry.data import CarryDataSet

__all__ = [
    "CarryBacktestResult",
    "CarryBacktester",
    "CarryConfig",
    "CarryDataSet",
    "ExecutionPriceError",
    "WarmupInsufficientError",
]
```

- [ ] **Step 8: Run the engine tests and fix only failures demonstrated by them**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_backtest.py -q
```

Expected: all ledger, cold-start, initialization-accounting, determinism, and no-lookahead tests pass.

- [ ] **Step 9: Add state identity, as-of, roll, and gross-cap regression tests**

First extend `make_carry_panel` so every product has a third, strictly later contract. Replace its two-contract tuple with:

```python
            if long_side:
                later_close = far_close * 1.03
            elif short_side:
                later_close = far_close * 0.97
            else:
                later_close = far_close
            for contract, close, oi, volume in (
                (f"{product}2410", near_close, 500.0, 300.0),
                (f"{product}2501", far_close, 300.0, 200.0),
                (f"{product}2505", later_close, 100.0, 100.0),
            ):
```

Change the fixture row's `turnover` from `3_000_000_000.0` to `2_000_000_000.0`, preserving product-level daily turnover of 6 billion yuan.

Append to `tests/test_carry_backtest.py`:

```python
from cta_carry.data import CarryDataSet


def test_formal_weights_only_scale_the_single_raw_state():
    data = make_carry_panel()
    dates = data.dates
    result = CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()
    active = result.positions[
        (result.positions["raw_weight"] != 0.0)
        & (result.positions["weight"] != 0.0)
    ]

    assert (
        active["raw_weight"].apply(lambda value: 1 if value > 0 else -1)
        == active["weight"].apply(lambda value: 1 if value > 0 else -1)
    ).all()
    assert (
        result.positions.drop_duplicates("trade_date")["gross_leverage"]
        <= small_config().max_gross_leverage + 1e-12
    ).all()


def test_main_contract_can_roll_from_t_oi_while_pool_still_uses_t_minus_one():
    data = make_carry_panel()
    switch_date = data.dates[15]
    prices = data.prices.copy()
    prices.loc[
        (prices["trade_date"] == switch_date)
        & (prices["contract"] == "A2410"),
        ["oi", "turnover"],
    ] = [50.0, 0.0]
    prices.loc[
        (prices["trade_date"] == switch_date)
        & (prices["contract"] == "A2501"),
        ["oi", "turnover"],
    ] = [700.0, 0.0]
    changed = CarryDataSet(prices, data.data_quality)
    result = CarryBacktester(
        changed,
        config=small_config(),
        start=data.dates[12],
        end=data.dates[-1],
    ).run()

    signal = result.signals[
        (result.signals["trade_date"] == switch_date)
        & (result.signals["product"] == "A")
    ].iloc[0]
    assert signal["main_contract"] == "A2501"
    assert signal["input_ready"]
    roll_date = data.dates[16]
    roll = result.trades[
        (result.trades["trade_date"] == roll_date)
        & (result.trades["product"] == "A")
    ]
    assert set(roll["contract"]) == {"A2410", "A2501"}
    assert set(roll["reason"]) == {"roll"}
```

The test sets all three product-A contract turnovers to zero on `switch_date` if the third contract exists; use:

```python
    prices.loc[
        (prices["trade_date"] == switch_date)
        & (prices["product"] == "A"),
        "turnover",
    ] = 0.0
```

instead of relying only on the two assignments above. The expected in-pool result proves that the current day's zero turnover did not enter the shifted liquidity mean, while current-day OI did select the new main contract.

- [ ] **Step 10: Correctly label stop stages**

Replace `_transition_reason` with this version and pass `config` from `_close_plan`:

```python
def _transition_reason(
    before: PositionState,
    after: PositionState,
    *,
    stop_triggered: bool,
    config: CarryConfig,
) -> str:
    if before.direction != 0 and after.direction == -before.direction:
        return "direction_reversal"
    if stop_triggered:
        remaining = max(after.tranches_remaining, 0)
        stage = config.stop_tranches - remaining
        return f"stop_{stage}"
    if before.direction != 0 and after.direction == 0:
        return "signal_exit"
    if before.direction == 0 and after.direction != 0:
        return "entry"
    if (
        before.direction == after.direction
        and before.contract != after.contract
    ):
        return "roll"
    return "rebalance"
```

The call site becomes:

```python
        reasons[product] = _transition_reason(
            before,
            after_signal,
            stop_triggered=stop_triggered,
            config=config,
        )
```

- [ ] **Step 11: Run all Carry core tests and existing regression**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_config.py tests/test_carry_data.py tests/test_carry_pg_source.py tests/test_carry_curve.py tests/test_carry_risk.py tests/test_carry_signals.py tests/test_carry_backtest.py -q
```

Expected: all Carry core tests pass.

Run:

```bash
.venv/bin/python -m pytest tests/test_cta_strategy.py tests/test_cta_pg_source.py tests/test_cta_data_quality.py -q
```

Expected: all pre-existing tests pass unchanged.

- [ ] **Step 12: Commit the complete event engine**

```bash
git add cta_carry/__init__.py cta_carry/backtest.py tests/carry_fixtures.py tests/test_carry_backtest.py
git commit -m "feat: add stateful Carry daily engine"
```

### Task 8: Excel/PNG report and command-line workflow

**Files:**
- Create: `cta_carry/report.py`
- Create: `cta_carry/__main__.py`
- Create: `tests/test_carry_report_cli.py`

- [ ] **Step 1: Write failing report tests**

Create `tests/test_carry_report_cli.py`:

```python
import pandas as pd

from cta_carry.backtest import CarryBacktester
from cta_carry.report import write_carry_outputs
from tests.carry_fixtures import make_carry_panel, small_config


def _result():
    data = make_carry_panel()
    dates = data.dates
    return CarryBacktester(
        data,
        config=small_config(),
        start=dates[12],
        end=dates[-1],
    ).run()


def test_report_writes_all_required_sheets_and_chart(tmp_path):
    result = _result()

    xlsx, png = write_carry_outputs(result, tmp_path / "carry_daily")

    assert png.exists()
    assert png.stat().st_size > 0
    assert pd.ExcelFile(xlsx).sheet_names == [
        "metrics",
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "data_quality",
        "run_config",
    ]
    metrics = pd.read_excel(xlsx, sheet_name="metrics").iloc[0]
    assert "calmar" in metrics.index
    first = pd.read_excel(xlsx, sheet_name="daily_returns").iloc[0]
    assert first["boundary_type"] == "report_start_initialization"


def test_curve_selection_excel_view_is_one_row_per_product_day():
    result = _result()

    from cta_carry.report import curve_selection_excel_view

    view = curve_selection_excel_view(result.curve_selection)

    assert not view.duplicated(["trade_date", "product"]).any()
    assert {"candidate_contracts", "main_contract", "secondary_contract"}.issubset(
        view.columns
    )
```

- [ ] **Step 2: Run report tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_report_cli.py -q
```

Expected: collection fails because `cta_carry.report` does not exist.

- [ ] **Step 3: Implement the bounded Excel view and report writer**

Create `cta_carry/report.py`:

```python
from __future__ import annotations

from pathlib import Path

import pandas as pd

from cta_carry.backtest import CarryBacktestResult


def curve_selection_excel_view(frame: pd.DataFrame) -> pd.DataFrame:
    if frame.empty:
        return pd.DataFrame(
            columns=[
                "trade_date",
                "product",
                "in_pool",
                "candidate_contracts",
                "main_contract",
                "secondary_contract",
                "exclusion_reasons",
                "liquidity_mean",
            ]
        )

    def summarize(group: pd.DataFrame) -> pd.Series:
        main = group.loc[group["role"] == "main", "contract"].tolist()
        secondary = group.loc[
            group["role"] == "secondary", "contract"
        ].tolist()
        return pd.Series(
            {
                "in_pool": bool(group["in_pool"].any()),
                "candidate_contracts": ",".join(sorted(group["contract"])),
                "main_contract": main[0] if main else "",
                "secondary_contract": secondary[0] if secondary else "",
                "exclusion_reasons": ",".join(
                    sorted(
                        {
                            value
                            for value in group["reason"].astype(str)
                            if value not in {"highest_oi", "later_highest_oi"}
                        }
                    )
                ),
                "liquidity_mean": group["liquidity_mean"].iloc[0],
            }
        )

    return (
        frame.groupby(["trade_date", "product"], sort=True)
        .apply(summarize, include_groups=False)
        .reset_index()
    )


def write_carry_outputs(
    result: CarryBacktestResult,
    output_prefix: str | Path,
) -> tuple[Path, Path]:
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path = prefix.with_suffix(".xlsx")
    png_path = prefix.with_name(prefix.name + "_overview.png")
    sheets = [
        ("metrics", pd.DataFrame([result.metrics])),
        ("daily_returns", result.daily_returns),
        ("positions", result.positions),
        ("trades", result.trades),
        ("signals", result.signals),
        (
            "curve_selection",
            curve_selection_excel_view(result.curve_selection),
        ),
        ("data_quality", result.data_quality),
        ("run_config", result.run_config),
    ]
    with pd.ExcelWriter(xlsx_path, engine="openpyxl") as writer:
        for name, frame in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)
    _write_overview_png(result, png_path)
    return xlsx_path, png_path


def _write_overview_png(
    result: CarryBacktestResult,
    png_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    daily = result.daily_returns.set_index("trade_date")
    equity = daily["equity"]
    drawdown = equity / equity.cummax() - 1.0
    leverage = (
        result.positions.drop_duplicates("trade_date")
        .set_index("trade_date")["gross_leverage"]
        .reindex(equity.index)
        .fillna(0.0)
    )
    fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
    equity.plot(ax=axes[0], color="#1f77b4", lw=1.8)
    drawdown.plot(ax=axes[1], color="#d62728", lw=1.2)
    leverage.plot(ax=axes[2], color="#2ca02c", lw=1.2)
    axes[0].set_ylabel("Equity")
    axes[1].set_ylabel("Drawdown")
    axes[2].set_ylabel("Gross")
    for axis in axes:
        axis.grid(True, alpha=0.3)
    fig.suptitle("Carry Daily Research")
    fig.tight_layout()
    fig.savefig(png_path, dpi=140)
    plt.close(fig)


def console_summary(result: CarryBacktestResult) -> str:
    config = result.run_config.set_index("key")["value"]
    return (
        f"report_start={config.get('report_start_date')} "
        f"signal_ready={config.get('signal_ready_date')} "
        f"vol_ready={config.get('vol_ready_date')} "
        f"trades={len(result.trades)} "
        f"cost={result.metrics['total_cost']:.6f} "
        f"ann_return={result.metrics['ann_return']:.4f} "
        f"ann_vol={result.metrics['ann_vol']:.4f} "
        f"sharpe={result.metrics['sharpe']:.4f} "
        f"calmar={result.metrics['calmar']:.4f} "
        f"max_drawdown={result.metrics['max_drawdown']:.4f} "
        f"max_gross={result.metrics['max_gross_leverage']:.4f}"
    )
```

- [ ] **Step 4: Run report tests and verify green**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_report_cli.py -q
```

Expected: both report tests pass and the workbook contains every required sheet.

- [ ] **Step 5: Write failing file-source CLI tests**

Append to `tests/test_carry_report_cli.py`:

```python
from cta_carry.__main__ import main


def _small_cli_args(data_dir, output_prefix, start, end):
    return [
        "--source",
        "files",
        "--data-dir",
        str(data_dir),
        "--start",
        start.isoformat(),
        "--end",
        end.isoformat(),
        "--output-prefix",
        str(output_prefix),
        "--liquidity-window",
        "2",
        "--liquidity-threshold",
        "0",
        "--carry-window",
        "2",
        "--momentum-window",
        "2",
        "--atr-window",
        "2",
        "--vol-window",
        "4",
        "--min-shadow-active-days",
        "2",
        "--prewarm-calendar-days",
        "15",
    ]


def test_file_cli_runs_and_writes_outputs(tmp_path):
    data = make_carry_panel()
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "output" / "carry"

    exit_code = main(
        _small_cli_args(
            data_dir,
            prefix,
            data.dates[12],
            data.dates[-1],
        )
    )

    assert exit_code == 0
    assert prefix.with_suffix(".xlsx").exists()
    assert prefix.with_name("carry_overview.png").exists()


def test_cli_returns_nonzero_and_writes_no_success_report_on_warmup_error(
    tmp_path,
    capsys,
):
    data = make_carry_panel(periods=12)
    data_dir = tmp_path / "input"
    data_dir.mkdir()
    data.prices.to_csv(data_dir / "prices.csv", index=False)
    prefix = tmp_path / "carry_failed"

    exit_code = main(
        _small_cli_args(
            data_dir,
            prefix,
            data.dates[6],
            data.dates[-1],
        )
    )

    assert exit_code == 2
    assert "risk scaling not ready" in capsys.readouterr().err
    assert not prefix.with_suffix(".xlsx").exists()
```

- [ ] **Step 6: Run CLI tests and verify red**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_report_cli.py::test_file_cli_runs_and_writes_outputs tests/test_carry_report_cli.py::test_cli_returns_nonzero_and_writes_no_success_report_on_warmup_error -q
```

Expected: collection fails because `cta_carry.__main__` does not exist.

- [ ] **Step 7: Implement the CLI parser and orchestration**

Create `cta_carry/__main__.py`:

```python
from __future__ import annotations

import argparse
from dataclasses import replace
from datetime import date, timedelta
from pathlib import Path
import subprocess
import sys

import pandas as pd

from cta_carry.backtest import CarryBacktester
from cta_carry.config import CarryConfig
from cta_carry.data import CarryDataSet
from cta_carry.pg_source import load_public_carry_data
from cta_carry.report import console_summary, write_carry_outputs


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m cta_carry")
    parser.add_argument(
        "--source", choices=["public-pg", "files"], default="public-pg"
    )
    parser.add_argument("--data-dir")
    parser.add_argument("--settings")
    parser.add_argument("--use-test", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--products", help="comma-separated product codes")
    parser.add_argument(
        "--output-prefix", default="output/carry_daily"
    )
    parser.add_argument("--liquidity-window", type=int, default=120)
    parser.add_argument(
        "--liquidity-threshold", type=float, default=5_000_000_000.0
    )
    parser.add_argument("--carry-window", type=int, default=10)
    parser.add_argument("--selection-fraction", type=float, default=0.20)
    parser.add_argument("--momentum-window", type=int, default=10)
    parser.add_argument("--atr-window", type=int, default=20)
    parser.add_argument("--atr-risk-budget", type=float, default=0.005)
    parser.add_argument("--vol-window", type=int, default=252)
    parser.add_argument("--min-shadow-active-days", type=int, default=126)
    parser.add_argument("--target-vol", type=float, default=0.15)
    parser.add_argument("--max-gross-leverage", type=float, default=4.0)
    parser.add_argument(
        "--chandelier-atr-multiple", type=float, default=2.5
    )
    parser.add_argument("--stop-tranches", type=int, default=3)
    parser.add_argument("--cost-bps", type=float, default=13.0)
    parser.add_argument("--prewarm-calendar-days", type=int, default=730)
    return parser


def _config_from_args(args) -> CarryConfig:
    return CarryConfig(
        liquidity_window=args.liquidity_window,
        liquidity_threshold=args.liquidity_threshold,
        carry_window=args.carry_window,
        selection_fraction=args.selection_fraction,
        momentum_window=args.momentum_window,
        atr_window=args.atr_window,
        atr_risk_budget=args.atr_risk_budget,
        vol_window=args.vol_window,
        min_shadow_active_days=args.min_shadow_active_days,
        target_vol=args.target_vol,
        max_gross_leverage=args.max_gross_leverage,
        chandelier_atr_multiple=args.chandelier_atr_multiple,
        stop_tranches=args.stop_tranches,
        cost_bps=args.cost_bps,
        prewarm_calendar_days=args.prewarm_calendar_days,
    )


def _parse_products(value: str | None) -> list[str] | None:
    if not value:
        return None
    return sorted(
        {part.strip().upper() for part in value.split(",") if part.strip()}
    )


def _git_version() -> str:
    try:
        return subprocess.run(
            ["git", "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return "unknown"


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config = _config_from_args(args)
        products = _parse_products(args.products)
        if args.source == "files":
            if not args.data_dir:
                raise ValueError(
                    "--data-dir is required when --source files"
                )
            query_start = args.start - timedelta(
                days=config.prewarm_calendar_days
            )
            data = CarryDataSet.from_dir(args.data_dir).slice(
                products=products,
                start=query_start,
                end=args.end,
            )
        else:
            data = load_public_carry_data(
                start=args.start,
                end=args.end,
                config=config,
                products=products,
                config_path=args.settings,
                use_test=args.use_test,
            )
        result = CarryBacktester(
            data,
            config=config,
            start=args.start,
            end=args.end,
        ).run()
        runtime = pd.DataFrame(
            [
                {"key": "source", "value": args.source},
                {
                    "key": "products",
                    "value": ",".join(products) if products else "ALL",
                },
                {"key": "code_version", "value": _git_version()},
            ]
        )
        result = replace(
            result,
            run_config=pd.concat(
                [result.run_config, runtime], ignore_index=True
            ),
        )
        xlsx, png = write_carry_outputs(result, Path(args.output_prefix))
        print(console_summary(result))
        print(f"xlsx={xlsx.resolve()}")
        print(f"chart={png.resolve()}")
        return 0
    except (FileNotFoundError, RuntimeError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 8: Include pool/exclusion counts in the console summary**

At the start of `console_summary`, derive unique product-day status:

```python
    selection = curve_selection_excel_view(result.curve_selection)
    included = int(selection["in_pool"].sum()) if not selection.empty else 0
    excluded = int((~selection["in_pool"]).sum()) if not selection.empty else 0
```

Add these fields immediately after the readiness dates:

```python
        f"in_pool_product_days={included} "
        f"excluded_product_days={excluded} "
```

- [ ] **Step 9: Run report/CLI tests and commit**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_report_cli.py -q
```

Expected: all report and CLI tests pass.

```bash
git add cta_carry/report.py cta_carry/__main__.py tests/test_carry_report_cli.py
git commit -m "feat: add Carry reports and CLI"
```

### Task 9: Documentation, full regression, and PostgreSQL smoke check

**Files:**
- Modify: `README.md`

- [ ] **Step 1: Document the new package and exact run commands**

Update the layout section so `cta_carry/` is no longer described as “next strategy”. Add a Carry section containing:

```markdown
## Carry 日线研究版

`cta_carry/` 使用分合约 `public.futures_daily`，按前 120 个品种交易日的日均成交额建立动态交易池，逐日选择主力与严格晚月次主力，并在下一交易日开盘执行 Carry、动量/缩量缩仓过滤和三档日线吊灯止损。

```bash
.venv/bin/python -m cta_carry \
  --source public-pg \
  --start 2012-01-01 \
  --end 2026-04-29 \
  --output-prefix output/carry_daily
```

离线源要求 `DATA_DIR/prices.csv` 或 `DATA_DIR/prices.parquet`，字段为 `trade_date, contract, open, high, low, close, volume, oi, turnover`，可选 `settle`。使用文件源时增加 `--source files --data-dir DATA_DIR`。

默认预热 730 日历日；若正式起始交易日之前未积累 252 个影子收益、至少 126 个实际持仓日和正的有限波动率，命令以非零状态退出，不会静默推迟回测起点。

输出为 `*_overview.png` 与 Excel，工作表包括 `metrics`、`daily_returns`、`positions`、`trades`、`signals`、`curve_selection`、`data_quality`、`run_config`。

本版是日线研究近似：原研报的 15 分钟止损改为日收盘触发，下一 5 分钟 VWAP 改为下一交易日开盘，ATR 默认 20 个合约交易日，成本固定为单边 13 bps，不换算张数、乘数和保证金，也不模拟涨跌停与容量。
```
```

Ensure the nested code fences render correctly by using four tildes for the outer documentation block while editing the actual README.

- [ ] **Step 2: Run focused tests from a clean process**

Run:

```bash
.venv/bin/python -m pytest tests/test_carry_config.py tests/test_carry_data.py tests/test_carry_pg_source.py tests/test_carry_curve.py tests/test_carry_risk.py tests/test_carry_signals.py tests/test_carry_backtest.py tests/test_carry_report_cli.py -q
```

Expected: every Carry test passes with zero failures.

- [ ] **Step 3: Run the entire offline regression suite**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: every existing and new test passes with zero failures.

- [ ] **Step 4: Verify CLI, module isolation, and deterministic structure**

Run each command separately:

```bash
.venv/bin/python -m cta_carry --help
```

Expected: exit 0 and all approved research parameters appear.

```bash
rg -n "spread_analyzer" cta_carry tests/test_carry_config.py tests/test_carry_data.py tests/test_carry_pg_source.py tests/test_carry_curve.py tests/test_carry_risk.py tests/test_carry_signals.py tests/test_carry_backtest.py tests/test_carry_report_cli.py
```

Expected: no matches.

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Run the explicit read-only PostgreSQL smoke check**

With valid local database settings, run:

```bash
.venv/bin/python -m cta_carry --source public-pg --start 2024-01-02 --end 2026-04-29 --output-prefix /tmp/carry_daily_smoke
```

Expected: the query is read-only; the command either produces the workbook/chart with `report_start_date=2024-01-02`, or fails loudly with a fully populated `WarmupInsufficientError`. It must not silently move the reporting start.

Inspect the generated workbook:

```bash
.venv/bin/python -c 'import pandas as pd; p="/tmp/carry_daily_smoke.xlsx"; x=pd.ExcelFile(p); print(x.sheet_names); print(pd.read_excel(p, sheet_name="run_config").to_string(index=False))'
```

Expected after a successful run: all eight required sheets exist; `report_start_date`, `signal_ready_date`, and `vol_ready_date` are populated; `vol_ready_date` is not later than `report_start_date`.

- [ ] **Step 6: Commit documentation**

```bash
git add README.md
git commit -m "docs: document Carry daily research"
```

- [ ] **Step 7: Final verification before declaring completion**

Run:

```bash
.venv/bin/python -m pytest -q
```

Expected: zero failures.

```bash
git status --short --branch
```

Expected: clean worktree; the branch only contains the task commits described above.

## Spec coverage check

| Approved requirement | Plan coverage |
|---|---|
| Immutable defaults and validation before query | Task 1 |
| CSV/Parquet and read-only `futures_daily` sources | Task 2 |
| Three/four-digit parser, 120-day shifted pool, current-day OI, strict later secondary, Carry sign/window | Tasks 1 and 3 |
| Stable quintiles, sign gates, momentum, volume+OI half position, unadjusted roll splice | Task 5 |
| Per-contract ATR, 0.5% risk, 252/126 shadow semantics, 15% target, gross cap 4 | Task 4 |
| One discrete state machine, three stops, lock/unlock/reversal/roll | Tasks 4 and 7 |
| Close-to-next-open timing, contract returns, 13 bps costs, fatal execution prices | Tasks 6 and 7 |
| 730-day warmup, hard failure, inherited state, explicit report-start cost boundary | Tasks 2, 6, and 7 |
| Audit/result frames, metrics, Excel, chart, console, deterministic output | Tasks 3, 7, and 8 |
| No-lookahead, as-of asymmetry, accounting identities, current regression | Tasks 3, 6, 7, and 9 |
| README deviations and no `spread_analyzer` dependency | Task 9 |

## Execution notes

- Implement in an isolated worktree created at execution time with the `using-git-worktrees` skill.
- Use `subagent-driven-development` for one fresh implementer per task, or `executing-plans` for inline batches with review checkpoints.
- Before any completion claim, invoke `verification-before-completion` and cite the final test output.

