"""Excel, chart, and console reporting for Carry research runs."""

from __future__ import annotations

import os
from pathlib import Path
import shutil
import tempfile

import pandas as pd

from .backtest import CarryBacktestResult


_CURVE_EXCEL_COLUMNS = (
    "trade_date",
    "product",
    "in_pool",
    "candidate_contracts",
    "main_contract",
    "secondary_contract",
    "exclusion_reasons",
    "liquidity_mean",
)
_SHEET_NAMES = (
    "metrics",
    "daily_returns",
    "positions",
    "trades",
    "signals",
    "curve_selection",
    "data_quality",
    "run_config",
)
_EXCEL_MAX_DATA_ROWS = 1_048_575
_EXCEL_MAX_COLUMNS = 16_384


class ReportWriteError(RuntimeError):
    """Structured failure raised while preparing or publishing reports."""

    def __init__(
        self,
        *,
        stage: str,
        reason: str,
        sheet: str | None = None,
        rows: int | None = None,
        columns: int | None = None,
        path: str | Path | None = None,
        recovery_paths: tuple[str | Path, ...] = (),
        secondary_errors: tuple[str, ...] = (),
    ) -> None:
        self.stage = stage
        self.reason = reason
        self.sheet = sheet
        self.rows = rows
        self.columns = columns
        self.path = Path(path) if path is not None else None
        self.recovery_paths = tuple(Path(item) for item in recovery_paths)
        self.secondary_errors = tuple(secondary_errors)
        details = []
        if sheet is not None:
            details.append(f"sheet={sheet}")
        if rows is not None:
            details.append(f"rows={rows}")
        if columns is not None:
            details.append(f"columns={columns}")
        if path is not None:
            details.append(f"path={path}")
        if self.recovery_paths:
            details.append(
                "recovery_paths=" + "|".join(str(item) for item in self.recovery_paths)
            )
        if self.secondary_errors:
            details.append("secondary_errors=" + " | ".join(self.secondary_errors))
        suffix = f" [{', '.join(details)}]" if details else ""
        super().__init__(
            f"Carry report publication failed at {stage}: {reason}{suffix}"
        )

    def with_secondary_errors(
        self,
        errors: tuple[str, ...],
    ) -> ReportWriteError:
        return ReportWriteError(
            stage=self.stage,
            reason=self.reason,
            sheet=self.sheet,
            rows=self.rows,
            columns=self.columns,
            path=self.path,
            recovery_paths=self.recovery_paths,
            secondary_errors=self.secondary_errors + errors,
        )


def _first_nonempty(values: pd.Series) -> str:
    return next((value for value in values if value), "")


def _join_reasons(values: pd.Series) -> str:
    return ",".join(sorted({value for value in values if value}))


def curve_selection_excel_view(frame: pd.DataFrame) -> pd.DataFrame:
    """Bound contract-level curve audit to one row per product and day."""
    if frame.empty:
        return pd.DataFrame(columns=_CURVE_EXCEL_COLUMNS)

    ordered = (
        frame.loc[
            :,
            [
                "trade_date",
                "product",
                "contract",
                "role",
                "reason",
                "in_pool",
                "liquidity_mean",
            ],
        ]
        .sort_values(
            ["trade_date", "product", "contract"],
            kind="mergesort",
        )
        .copy()
    )
    ordered["_contract_text"] = ordered["contract"].astype(str)
    ordered["_main_contract"] = ordered["_contract_text"].where(
        ordered["role"].eq("main"),
        "",
    )
    ordered["_secondary_contract"] = ordered["_contract_text"].where(
        ordered["role"].eq("secondary"),
        "",
    )
    included_reasons = ~ordered["reason"].isin({"highest_oi", "later_highest_oi"})
    ordered["_exclusion_reason"] = (
        ordered["reason"]
        .astype(str)
        .where(
            included_reasons,
            "",
        )
    )
    view = (
        ordered.groupby(
            ["trade_date", "product"],
            sort=False,
            as_index=False,
        )
        .agg(
            in_pool=("in_pool", "any"),
            candidate_contracts=(
                "_contract_text",
                lambda values: ",".join(values),
            ),
            main_contract=("_main_contract", _first_nonempty),
            secondary_contract=(
                "_secondary_contract",
                _first_nonempty,
            ),
            exclusion_reasons=(
                "_exclusion_reason",
                _join_reasons,
            ),
            liquidity_mean=("liquidity_mean", "first"),
        )
        .loc[:, list(_CURVE_EXCEL_COLUMNS)]
    )
    view["in_pool"] = view["in_pool"].astype(bool)
    return view


def _report_sheets(
    result: CarryBacktestResult,
) -> tuple[tuple[str, pd.DataFrame], ...]:
    return (
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
    )


def _preflight_sheet_bounds(
    sheets: tuple[tuple[str, pd.DataFrame], ...],
) -> None:
    for name, frame in sheets:
        rows, columns = frame.shape
        if rows > _EXCEL_MAX_DATA_ROWS:
            raise ReportWriteError(
                stage="preflight",
                reason=(
                    f"sheet data rows exceed Excel's limit of {_EXCEL_MAX_DATA_ROWS}"
                ),
                sheet=name,
                rows=rows,
                columns=columns,
            )
        if columns > _EXCEL_MAX_COLUMNS:
            raise ReportWriteError(
                stage="preflight",
                reason=(f"sheet columns exceed Excel's limit of {_EXCEL_MAX_COLUMNS}"),
                sheet=name,
                rows=rows,
                columns=columns,
            )


def _temporary_path(final_path: Path, suffix: str) -> Path:
    descriptor, raw_path = tempfile.mkstemp(
        dir=final_path.parent,
        prefix=f".{final_path.name}.",
        suffix=suffix,
    )
    os.close(descriptor)
    return Path(raw_path)


def _cleanup_paths(
    paths: tuple[Path | None, ...],
) -> tuple[str, ...]:
    errors: list[str] = []
    for path in paths:
        if path is None:
            continue
        try:
            path.unlink(missing_ok=True)
        except Exception as exc:
            errors.append(f"cleanup path={path}: {type(exc).__name__}: {exc}")
    return tuple(errors)


def _write_workbook(
    sheets: tuple[tuple[str, pd.DataFrame], ...],
    path: Path,
) -> None:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        for name, frame in sheets:
            frame.to_excel(writer, sheet_name=name, index=False)


def _validate_workbook(path: Path) -> None:
    if path.stat().st_size <= 0:
        raise ValueError("workbook is empty")
    with pd.ExcelFile(path, engine="openpyxl") as workbook:
        sheet_names = workbook.sheet_names
    if sheet_names != list(_SHEET_NAMES):
        raise ValueError(
            f"workbook sheets are {sheet_names}, expected {list(_SHEET_NAMES)}"
        )


def _validate_png(path: Path) -> None:
    if path.stat().st_size <= 0:
        raise ValueError("overview PNG is empty")


def _publish_outputs(
    pairs: tuple[tuple[Path, Path], ...],
) -> None:
    backups: dict[Path, Path | None] = {}
    published: list[Path] = []
    try:
        for _, final_path in pairs:
            if final_path.exists():
                backup = _temporary_path(final_path, ".backup")
                backups[final_path] = backup
                shutil.copy2(final_path, backup)
            else:
                backups[final_path] = None
        for temporary_path, final_path in pairs:
            os.replace(temporary_path, final_path)
            published.append(final_path)
    except Exception as publish_exc:
        rollback_errors: list[str] = []
        recovery_paths: list[Path] = []
        for final_path in reversed(published):
            backup = backups[final_path]
            if backup is None:
                try:
                    final_path.unlink(missing_ok=True)
                except Exception as rollback_exc:
                    rollback_errors.append(
                        "rollback remove "
                        f"path={final_path}: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
            else:
                try:
                    os.replace(backup, final_path)
                except Exception as rollback_exc:
                    rollback_errors.append(
                        "rollback restore "
                        f"path={final_path} from={backup}: "
                        f"{type(rollback_exc).__name__}: {rollback_exc}"
                    )
                    recovery_paths.append(backup)
                else:
                    backups[final_path] = None

        recoverable = set(recovery_paths)
        cleanup_errors = _cleanup_paths(
            tuple(
                backup
                for backup in backups.values()
                if backup is not None and backup not in recoverable
            )
        )
        raise ReportWriteError(
            stage="publish",
            reason=f"{type(publish_exc).__name__}: {publish_exc}",
            recovery_paths=tuple(recovery_paths),
            secondary_errors=tuple(rollback_errors) + cleanup_errors,
        ) from publish_exc

    cleanup_errors = _cleanup_paths(tuple(backups.values()))
    if cleanup_errors:
        leftover_backups = tuple(
            backup
            for backup in backups.values()
            if backup is not None and backup.exists()
        )
        raise ReportWriteError(
            stage="cleanup",
            reason="outputs published but backup cleanup failed",
            recovery_paths=leftover_backups,
            secondary_errors=cleanup_errors,
        )


def write_carry_outputs(
    result: CarryBacktestResult,
    output_prefix: str | Path,
) -> tuple[Path, Path]:
    """Transactionally publish the workbook and overview chart."""
    prefix = Path(output_prefix)
    xlsx_path = Path(f"{prefix}.xlsx")
    png_path = Path(f"{prefix}_overview.png")
    temporary_xlsx: Path | None = None
    temporary_png: Path | None = None
    stage = "prepare"
    active_path: Path | None = None
    error: ReportWriteError | None = None
    error_cause: Exception | None = None
    try:
        sheets = _report_sheets(result)

        stage = "preflight"
        _preflight_sheet_bounds(sheets)

        stage = "prepare"
        active_path = prefix.parent
        prefix.parent.mkdir(parents=True, exist_ok=True)
        active_path = xlsx_path
        temporary_xlsx = _temporary_path(xlsx_path, ".tmp.xlsx")
        active_path = png_path
        temporary_png = _temporary_path(png_path, ".tmp.png")

        stage = "excel_write"
        active_path = temporary_xlsx
        _write_workbook(sheets, temporary_xlsx)

        stage = "png_write"
        active_path = temporary_png
        _write_overview_png(result, temporary_png)

        stage = "excel_validate"
        active_path = temporary_xlsx
        _validate_workbook(temporary_xlsx)

        stage = "png_validate"
        active_path = temporary_png
        _validate_png(temporary_png)

        stage = "publish"
        active_path = None
        _publish_outputs(
            (
                (temporary_xlsx, xlsx_path),
                (temporary_png, png_path),
            )
        )
    except ReportWriteError as exc:
        error = exc
    except Exception as exc:
        error_cause = exc
        error = ReportWriteError(
            stage=stage,
            reason=f"{type(exc).__name__}: {exc}",
            path=active_path,
        )

    cleanup_errors = _cleanup_paths((temporary_xlsx, temporary_png))
    if error is not None:
        if cleanup_errors:
            original_error = error
            error = error.with_secondary_errors(cleanup_errors)
            if error_cause is None:
                error_cause = original_error
        if error_cause is not None:
            raise error from error_cause
        raise error
    if cleanup_errors:
        raise ReportWriteError(
            stage="cleanup",
            reason="output temporary cleanup failed",
            secondary_errors=cleanup_errors,
        )
    return xlsx_path, png_path


def _write_overview_png(
    result: CarryBacktestResult,
    png_path: Path,
) -> None:
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig = None
    try:
        daily = result.daily_returns.set_index("trade_date")
        fig, axes = plt.subplots(3, 1, figsize=(10, 8), sharex=True)
        if not daily.empty:
            equity = daily["equity"]
            drawdown = equity / equity.cummax() - 1.0
            leverage = daily["gross_leverage"]
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
    finally:
        if fig is not None:
            plt.close(fig)


def _pool_counts(frame: pd.DataFrame) -> tuple[int, int]:
    if frame.empty:
        return 0, 0
    in_pool = (
        frame.groupby(
            ["trade_date", "product"],
            sort=False,
        )["in_pool"]
        .any()
        .astype(bool)
    )
    included = int(in_pool.sum())
    return included, int(len(in_pool) - included)


def console_summary(result: CarryBacktestResult) -> str:
    """Return a compact audit and performance summary for terminal runs."""
    config = result.run_config.set_index("key")["value"]
    included, excluded = _pool_counts(result.curve_selection)
    return (
        f"report_start={config.get('report_start_date')} "
        f"signal_ready={config.get('signal_ready_date')} "
        f"vol_ready={config.get('vol_ready_date')} "
        f"in_pool_product_days={included} "
        f"excluded_product_days={excluded} "
        f"trades={len(result.trades)} "
        f"cost={result.metrics['total_cost']:.6f} "
        f"ann_return={result.metrics['ann_return']:.4f} "
        f"ann_vol={result.metrics['ann_vol']:.4f} "
        f"sharpe={result.metrics['sharpe']:.4f} "
        f"calmar={result.metrics['calmar']:.4f} "
        f"max_drawdown={result.metrics['max_drawdown']:.4f} "
        f"max_gross={result.metrics['max_gross_leverage']:.4f}"
    )
