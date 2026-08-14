"""Compare paired daily and minute Carry result workbooks."""

from __future__ import annotations

import argparse
from dataclasses import fields
from datetime import date, datetime
import math
from pathlib import Path
import sys
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

from common.metrics import summarize
from cta_carry.config import CarryConfig


_PERIODS_PER_YEAR = 252
_REQUIRED_DATE_KEYS = (
    "requested_start",
    "requested_end",
    "report_start_date",
)
_RESEARCH_KEYS = (
    *_REQUIRED_DATE_KEYS,
    "products",
    *(item.name for item in fields(CarryConfig)),
)
_ALLOWED_PAIR_DIFFERENCES = frozenset(
    {
        "accounting_clock",
        "execution_mode",
        "multiplier_resolution_version",
        "session_rules_version",
        "vol_ready_date",
    }
)
_PERFORMANCE_METRICS = (
    "gross_ann_return",
    "net_ann_return",
    "gross_sharpe",
    "net_sharpe",
    "gross_ann_vol",
    "net_ann_vol",
    "gross_max_drawdown",
    "net_max_drawdown",
    "gross_calmar",
    "net_calmar",
    "annual_turnover",
    "total_cost",
    "annualized_cost",
    "avg_gross_leverage",
    "max_gross_leverage",
)
_DAILY_RETURN_COLUMNS = (
    "trade_date",
    "gross_return",
    "turnover",
    "cost",
    "net_return",
    "gross_leverage",
)


class ComparisonInputError(RuntimeError):
    """Structured validation failure for one paired-result workbook."""

    def __init__(
        self,
        *,
        path: str | Path,
        check: str,
        reason: str,
        sheet: str | None = None,
        key: str | None = None,
    ) -> None:
        self.path = Path(path)
        self.check = check
        self.reason = reason
        self.sheet = sheet
        self.key = key
        details = [f"path={self.path}", f"check={check}"]
        if sheet is not None:
            details.append(f"sheet={sheet}")
        if key is not None:
            details.append(f"key={key}")
        super().__init__(f"{reason} [{', '.join(details)}]")


def _required_sheets(path: Path, names: Sequence[str]) -> dict[str, pd.DataFrame]:
    if not path.is_file():
        raise ComparisonInputError(
            path=path,
            check="workbook_missing",
            reason="comparison workbook does not exist",
        )
    try:
        with pd.ExcelFile(path, engine="openpyxl") as workbook:
            missing = [name for name in names if name not in workbook.sheet_names]
            if missing:
                raise ComparisonInputError(
                    path=path,
                    check="sheet_missing",
                    sheet=missing[0],
                    reason=f"required sheet is missing: {missing[0]}",
                )
            frames = {name: pd.read_excel(workbook, sheet_name=name) for name in names}
    except ComparisonInputError:
        raise
    except Exception as exc:
        raise ComparisonInputError(
            path=path,
            check="workbook_read",
            reason=f"cannot read comparison workbook: {type(exc).__name__}: {exc}",
        ) from exc

    for name, frame in frames.items():
        if frame.empty:
            raise ComparisonInputError(
                path=path,
                check="sheet_empty",
                sheet=name,
                reason=f"required sheet is empty: {name}",
            )
    return frames


def _require_columns(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    path: Path,
    sheet: str,
) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ComparisonInputError(
            path=path,
            check="column_missing",
            sheet=sheet,
            reason="required columns are missing: " + ", ".join(missing),
        )


def _finite_numeric(
    frame: pd.DataFrame,
    columns: Sequence[str],
    *,
    path: Path,
    sheet: str,
) -> pd.DataFrame:
    result = frame.copy()
    for column in columns:
        try:
            values = pd.to_numeric(result[column], errors="raise").astype(float)
        except (TypeError, ValueError) as exc:
            raise ComparisonInputError(
                path=path,
                check="numeric_value",
                sheet=sheet,
                key=column,
                reason=f"column must contain numeric values: {column}",
            ) from exc
        if not np.isfinite(values.to_numpy()).all():
            raise ComparisonInputError(
                path=path,
                check="numeric_value",
                sheet=sheet,
                key=column,
                reason=f"column must contain finite values: {column}",
            )
        result[column] = values
    return result


def _run_config(
    frame: pd.DataFrame,
    *,
    path: Path,
) -> dict[str, Any]:
    _require_columns(frame, ("key", "value"), path=path, sheet="run_config")
    keys = frame["key"].astype(str)
    duplicated = keys.duplicated(keep=False)
    if duplicated.any():
        key = sorted(keys.loc[duplicated].unique())[0]
        raise ComparisonInputError(
            path=path,
            check="run_config_duplicate_key",
            sheet="run_config",
            key=key,
            reason=f"run_config key is duplicated: {key}",
        )
    return dict(zip(keys, frame["value"], strict=True))


def _normalized_config_value(value: Any) -> Any:
    if value is None or bool(pd.isna(value)):
        return None
    if isinstance(value, pd.Timestamp):
        value = value.to_pydatetime()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, np.generic):
        return value.item()
    return value


def _is_execution_metadata(key: str) -> bool:
    return key in _ALLOWED_PAIR_DIFFERENCES or key.startswith("minute_")


def _validate_pair_config(
    daily: dict[str, Any],
    minute: dict[str, Any],
    *,
    daily_path: Path,
    minute_path: Path,
) -> None:
    for path, config in ((daily_path, daily), (minute_path, minute)):
        for key in _RESEARCH_KEYS:
            if key not in config:
                raise ComparisonInputError(
                    path=path,
                    check="run_config_key_missing",
                    sheet="run_config",
                    key=key,
                    reason=f"required comparison key is missing: {key}",
                )

    comparison_keys = sorted(
        key for key in set(daily) | set(minute) if not _is_execution_metadata(key)
    )
    for key in comparison_keys:
        if key not in daily or key not in minute:
            missing_path = daily_path if key not in daily else minute_path
            raise ComparisonInputError(
                path=missing_path,
                check="paired_config_mismatch",
                sheet="run_config",
                key=key,
                reason=f"paired workbooks do not share run_config key: {key}",
            )
        daily_value = _normalized_config_value(daily[key])
        minute_value = _normalized_config_value(minute[key])
        if daily_value != minute_value:
            raise ComparisonInputError(
                path=minute_path,
                check="paired_config_mismatch",
                sheet="run_config",
                key=key,
                reason=(
                    f"paired run_config values differ for {key}: "
                    f"daily={daily_value!r}, minute={minute_value!r}"
                ),
            )


def _validated_daily_returns(frame: pd.DataFrame, *, path: Path) -> pd.DataFrame:
    _require_columns(
        frame,
        _DAILY_RETURN_COLUMNS,
        path=path,
        sheet="daily_returns",
    )
    result = _finite_numeric(
        frame,
        _DAILY_RETURN_COLUMNS[1:],
        path=path,
        sheet="daily_returns",
    )
    try:
        dates = pd.to_datetime(result["trade_date"], errors="raise")
    except (TypeError, ValueError) as exc:
        raise ComparisonInputError(
            path=path,
            check="trade_date",
            sheet="daily_returns",
            key="trade_date",
            reason="trade_date values must be valid dates",
        ) from exc
    if dates.isna().any() or dates.duplicated().any():
        raise ComparisonInputError(
            path=path,
            check="trade_date",
            sheet="daily_returns",
            key="trade_date",
            reason="trade_date values must be non-null and unique",
        )
    if (result[["turnover", "cost", "gross_leverage"]] < 0.0).any().any():
        raise ComparisonInputError(
            path=path,
            check="numeric_value",
            sheet="daily_returns",
            reason="turnover, cost, and gross_leverage must be nonnegative",
        )
    result["trade_date"] = dates
    return result.sort_values("trade_date", kind="mergesort").reset_index(drop=True)


def _calmar(summary: dict[str, float]) -> float:
    drawdown = summary["max_drawdown"]
    if math.isfinite(drawdown) and drawdown > 0.0:
        return float(summary["ann_return"] / drawdown)
    return float("nan")


def _performance_metrics(daily: pd.DataFrame) -> dict[str, float]:
    indexed = daily.set_index("trade_date")
    gross = summarize(
        indexed["gross_return"],
        periods_per_year=_PERIODS_PER_YEAR,
        turnover=indexed["turnover"],
    )
    net = summarize(
        indexed["net_return"],
        periods_per_year=_PERIODS_PER_YEAR,
        turnover=indexed["turnover"],
    )
    return {
        "gross_ann_return": gross["ann_return"],
        "net_ann_return": net["ann_return"],
        "gross_sharpe": gross["sharpe"],
        "net_sharpe": net["sharpe"],
        "gross_ann_vol": gross["ann_vol"],
        "net_ann_vol": net["ann_vol"],
        "gross_max_drawdown": gross["max_drawdown"],
        "net_max_drawdown": net["max_drawdown"],
        "gross_calmar": _calmar(gross),
        "net_calmar": _calmar(net),
        "annual_turnover": gross["avg_turnover"] * _PERIODS_PER_YEAR,
        "total_cost": float(daily["cost"].sum()),
        "annualized_cost": float(daily["cost"].mean() * _PERIODS_PER_YEAR),
        "avg_gross_leverage": float(daily["gross_leverage"].mean()),
        "max_gross_leverage": float(daily["gross_leverage"].max()),
    }


def _validate_report_dates(
    daily: pd.DataFrame,
    minute: pd.DataFrame,
    *,
    minute_path: Path,
) -> None:
    daily_dates = tuple(daily["trade_date"])
    minute_dates = tuple(minute["trade_date"])
    if daily_dates != minute_dates:
        raise ComparisonInputError(
            path=minute_path,
            check="paired_report_dates",
            sheet="daily_returns",
            key="trade_date",
            reason="paired workbooks have different report trading dates",
        )


def _trigger_mask(stops: pd.DataFrame, *, path: Path) -> pd.Series:
    _require_columns(
        stops,
        ("triggered",),
        path=path,
        sheet="intraday_stops",
    )
    values = stops["triggered"]
    if pd.api.types.is_bool_dtype(values):
        return values.astype(bool)
    numeric = pd.to_numeric(values, errors="coerce")
    if numeric.isna().any() or not numeric.isin((0, 1)).all():
        raise ComparisonInputError(
            path=path,
            check="boolean_value",
            sheet="intraday_stops",
            key="triggered",
            reason="triggered must contain only boolean or 0/1 values",
        )
    return numeric.astype(bool)


def _stop_metrics(stops: pd.DataFrame, *, path: Path) -> dict[str, int]:
    _require_columns(
        stops,
        ("trade_date", "product"),
        path=path,
        sheet="intraday_stops",
    )
    triggered = _trigger_mask(stops, path=path)
    triggered_rows = stops.loc[triggered, ["trade_date", "product"]].copy()
    try:
        triggered_rows["trade_date"] = pd.to_datetime(
            triggered_rows["trade_date"], errors="raise"
        )
    except (TypeError, ValueError) as exc:
        raise ComparisonInputError(
            path=path,
            check="trade_date",
            sheet="intraday_stops",
            key="trade_date",
            reason="stop trade_date values must be valid dates",
        ) from exc
    multi_stop_days = (
        triggered_rows.groupby(["trade_date", "product"], dropna=False).size() > 1
    ).sum()
    return {
        "stop_row_count": int(len(stops)),
        "triggered_stop_count": int(triggered.sum()),
        "same_day_multi_stop_count": int(multi_stop_days),
    }


def _daily_target_mask(executions: pd.DataFrame, *, path: Path) -> pd.Series:
    _require_columns(
        executions,
        ("execution_kind",),
        path=path,
        sheet="executions",
    )
    kinds = executions["execution_kind"].astype(str)
    unknown = ~kinds.isin(("daily_target", "intraday_stop"))
    if unknown.any():
        value = sorted(kinds.loc[unknown].unique())[0]
        raise ComparisonInputError(
            path=path,
            check="execution_kind",
            sheet="executions",
            key="execution_kind",
            reason=f"unknown execution_kind: {value}",
        )
    return kinds.eq("daily_target")


def _vwap_open_bps(executions: pd.DataFrame, *, path: Path) -> float:
    _require_columns(
        executions,
        ("vwap", "daily_open", "volume"),
        path=path,
        sheet="executions",
    )
    targets = executions.loc[_daily_target_mask(executions, path=path)].copy()
    if targets.empty:
        raise ComparisonInputError(
            path=path,
            check="daily_target_fill_missing",
            sheet="executions",
            reason="executions has no daily target fills",
        )
    targets = _finite_numeric(
        targets,
        ("vwap", "daily_open", "volume"),
        path=path,
        sheet="executions",
    )
    if (targets[["vwap", "daily_open", "volume"]] <= 0.0).any().any():
        raise ComparisonInputError(
            path=path,
            check="numeric_value",
            sheet="executions",
            reason="daily target VWAP, open, and volume must be positive",
        )
    basis_points = (targets["vwap"] / targets["daily_open"] - 1.0) * 10_000.0
    return float(np.average(basis_points, weights=targets["volume"]))


def compare_workbooks(
    daily_path: str | Path,
    minute_path: str | Path,
    *,
    label: str,
) -> pd.DataFrame:
    """Return one audited daily/minute comparison row."""
    if not isinstance(label, str) or not label.strip():
        raise ValueError("label must be a nonempty string")
    daily_path = Path(daily_path)
    minute_path = Path(minute_path)
    daily_sheets = _required_sheets(
        daily_path,
        ("metrics", "daily_returns", "run_config"),
    )
    minute_sheets = _required_sheets(
        minute_path,
        (
            "metrics",
            "daily_returns",
            "executions",
            "intraday_stops",
            "run_config",
        ),
    )
    _validate_pair_config(
        _run_config(daily_sheets["run_config"], path=daily_path),
        _run_config(minute_sheets["run_config"], path=minute_path),
        daily_path=daily_path,
        minute_path=minute_path,
    )

    daily_returns = _validated_daily_returns(
        daily_sheets["daily_returns"], path=daily_path
    )
    minute_returns = _validated_daily_returns(
        minute_sheets["daily_returns"], path=minute_path
    )
    _validate_report_dates(
        daily_returns,
        minute_returns,
        minute_path=minute_path,
    )
    daily_metrics = _performance_metrics(daily_returns)
    minute_metrics = _performance_metrics(minute_returns)

    row: dict[str, Any] = {"label": label.strip()}
    for metric in _PERFORMANCE_METRICS:
        row[f"{metric}_daily"] = daily_metrics[metric]
        row[f"{metric}_minute"] = minute_metrics[metric]
        row[f"{metric}_delta"] = minute_metrics[metric] - daily_metrics[metric]
    row.update(_stop_metrics(minute_sheets["intraday_stops"], path=minute_path))
    row["daily_target_vwap_open_bps"] = _vwap_open_bps(
        minute_sheets["executions"], path=minute_path
    )
    row["gross_gap_explained_fraction"] = row["gross_ann_return_delta"] / 0.10
    return pd.DataFrame([row])


def compare_pairs(
    pairs: Iterable[tuple[str, str | Path, str | Path]],
) -> pd.DataFrame:
    """Compare repeated labeled workbook pairs in argument order."""
    rows: list[pd.DataFrame] = []
    seen: set[str] = set()
    for label, daily_path, minute_path in pairs:
        if label in seen:
            raise ValueError(f"pair label must be unique: {label}")
        seen.add(label)
        rows.append(compare_workbooks(daily_path, minute_path, label=label))
    if not rows:
        raise ValueError("at least one workbook pair is required")
    return pd.concat(rows, ignore_index=True)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare paired daily and minute Carry workbooks.",
    )
    parser.add_argument(
        "--pair",
        action="append",
        nargs=3,
        required=True,
        metavar=("LABEL", "DAILY_XLSX", "MINUTE_XLSX"),
    )
    parser.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        result = compare_pairs(args.pair)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        result.to_csv(args.output, index=False)
    except (ComparisonInputError, OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
