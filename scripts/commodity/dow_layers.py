"""Meet the Dow paper's single-product tables layer by layer.

Runs the registered Dow shadow on one product and evaluates the paper's three
layers -- preliminary trend, turning-point correction, Dow resonance -- under
every reading ``cta_dow.layers`` knows, in the paper's own table conventions.
Nothing here touches the registered results; it is a diagnostic.

    PYTHONPATH=. python scripts/commodity/dow_layers.py \
        --panel-dir output/commodity-panel-v1 --product I \
        --start 2014-01-01 --end 2022-07-29 --output-prefix output/diag_dow_layers_I

``--bars`` / ``--rolls`` accept a product slice written out as parquet instead
of a full bundle directory.
"""

from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import pandas as pd

from cta_dow.layers import layer_report
from cta_dow.shadow import run_shadow_product


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--panel-dir", type=Path)
    source.add_argument("--bars", type=Path, help="one product's bars as parquet")
    parser.add_argument("--rolls", type=Path, help="roll fills parquet (with --bars)")
    parser.add_argument("--product", required=True)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--atr-frequency", choices=("bar", "daily"), default="bar")
    parser.add_argument(
        "--signal-mode", choices=("latched", "literal"), default="latched"
    )
    parser.add_argument("--cost-bps", type=float, default=1.3)
    parser.add_argument(
        "--sizing",
        choices=("unit", "atr"),
        default="unit",
        help="每层按 1 倍名义（unit）或研报附录二的 ATR 杠杆（atr）持仓",
    )
    parser.add_argument("--output-prefix", required=True)
    return parser


def _load(args: argparse.Namespace) -> tuple[pd.DataFrame, pd.DataFrame | None]:
    if args.panel_dir is not None:
        from common.commodity.bundle import read_bundle

        bundle = read_bundle(args.panel_dir)
        bars = bundle.bars.loc[bundle.bars["product"] == args.product]
        return bars, bundle.roll_fills
    bars = pd.read_parquet(args.bars)
    rolls = pd.read_parquet(args.rolls) if args.rolls is not None else None
    return bars, rolls


def _with_segments(signals: pd.DataFrame, bars: pd.DataFrame) -> pd.DataFrame:
    """The shadow does not echo the continuity segment; join it back by slot."""
    key = pd.to_datetime(bars["slot_end"], utc=True)
    segments = pd.Series(bars["continuity_segment"].to_numpy(), index=key)
    if segments.index.has_duplicates:
        raise ValueError("dow_layers_bars: duplicate slot_end")
    out = signals.copy()
    out["continuity_segment"] = (
        pd.to_datetime(out["slot_end"], utc=True).map(segments).to_numpy()
    )
    if pd.isna(out["continuity_segment"]).any():
        raise ValueError("dow_layers_bars: a signal row has no bar")
    return out


def _markdown(name: str, table: pd.DataFrame) -> str:
    lines = [
        f"### {name}",
        "",
        "| 年 | 收益 | 最大回撤 | 夏普 | 波动 | Calmar | 月胜率 | 交易日 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for label, row in table.iterrows():
        lines.append(
            f"| {label} | {row['return'] * 100:.2f}% | {row['max_drawdown'] * 100:.2f}% "
            f"| {row['sharpe']:.2f} | {row['volatility'] * 100:.2f}% | {row['calmar']:.2f} "
            f"| {row['monthly_win_rate'] * 100:.2f}% | {int(row['trading_days'])} |"
        )
    return "\n".join(lines) + "\n"


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    bars, rolls = _load(args)
    result = run_shadow_product(
        bars,
        product=args.product,
        roll_fills=rolls,
        signal_mode=args.signal_mode,
        atr_frequency=args.atr_frequency,
        cost_bps=args.cost_bps,
    )
    signals = _with_segments(result.signals, bars)
    report = layer_report(
        signals,
        shadow_daily=result.daily,
        start=args.start,
        end=args.end,
        cost_bps=args.cost_bps,
        sizing=args.sizing,
    )
    prefix = Path(args.output_prefix)
    prefix.parent.mkdir(parents=True, exist_ok=True)
    frames = []
    for name, table in report.items():
        frame = table.reset_index(names="period")
        frame.insert(0, "layer", name)
        frames.append(frame)
    combined = pd.concat(frames, ignore_index=True)
    combined.to_csv(f"{prefix}.csv", index=False)
    header = (
        f"# {args.product} layers {args.start}..{args.end} "
        f"(atr_frequency={args.atr_frequency}, signal_mode={args.signal_mode}, "
        f"cost_bps={args.cost_bps}, sizing={args.sizing})\n\n"
    )
    Path(f"{prefix}.md").write_text(
        header + "\n".join(_markdown(name, table) for name, table in report.items()),
        encoding="utf-8",
    )
    counts = result.signals["action"].value_counts()
    Path(f"{prefix}.actions.csv").write_text(counts.to_csv(), encoding="utf-8")
    print(f"wrote {prefix}.csv / .md / .actions.csv")
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
