"""Excel, chart, and console reporting for Carry research runs."""

from __future__ import annotations

from pathlib import Path

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


def curve_selection_excel_view(frame: pd.DataFrame) -> pd.DataFrame:
    """Bound contract-level curve audit to one row per product and day."""
    if frame.empty:
        return pd.DataFrame(columns=_CURVE_EXCEL_COLUMNS)

    rows: list[dict[str, object]] = []
    grouped = frame.groupby(["trade_date", "product"], sort=True)
    for (trade_date, product), group in grouped:
        main = group.loc[group["role"] == "main", "contract"].tolist()
        secondary = group.loc[group["role"] == "secondary", "contract"].tolist()
        reasons = sorted(
            {
                value
                for value in group["reason"].astype(str)
                if value not in {"highest_oi", "later_highest_oi"}
            }
        )
        rows.append(
            {
                "trade_date": trade_date,
                "product": product,
                "in_pool": bool(group["in_pool"].any()),
                "candidate_contracts": ",".join(sorted(group["contract"].astype(str))),
                "main_contract": main[0] if main else "",
                "secondary_contract": secondary[0] if secondary else "",
                "exclusion_reasons": ",".join(reasons),
                "liquidity_mean": group["liquidity_mean"].iloc[0],
            }
        )
    return pd.DataFrame(rows, columns=_CURVE_EXCEL_COLUMNS)


def write_carry_outputs(
    result: CarryBacktestResult,
    output_prefix: str | Path,
) -> tuple[Path, Path]:
    """Write the eight-sheet research workbook and overview chart."""
    prefix = Path(output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    xlsx_path = prefix.with_suffix(".xlsx")
    png_path = prefix.with_name(f"{prefix.name}_overview.png")
    sheets = (
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
    plt.close(fig)


def console_summary(result: CarryBacktestResult) -> str:
    """Return a compact audit and performance summary for terminal runs."""
    config = result.run_config.set_index("key")["value"]
    selection = curve_selection_excel_view(result.curve_selection)
    if selection.empty:
        included = 0
        excluded = 0
    else:
        in_pool = selection["in_pool"].astype(bool)
        included = int(in_pool.sum())
        excluded = int((~in_pool).sum())
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
