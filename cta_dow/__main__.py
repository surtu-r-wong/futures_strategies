"""Run the Guosen Dow replication over a cached commodity panel bundle.

Two things are adjustable, and neither is a parameter of the strategy: which
reading of the entry gates to run (``--signal-mode``, with its sensitivity
twin) and the cost assumption. The EMA spans, the ATR window, the extreme
history depth, the target volatility, the selection thresholds, and the sample
cutoff are constants -- with the gap against the paper's headline printed in
the metrics sheet, any of them exposed as a flag becomes the dial that closes
it, and closing it that way is fitting rather than replicating.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from common.commodity.bundle import PanelBundle, read_bundle
from common.commodity.cli import (
    SESSION_RULES_LAST,
    SESSION_RULES_PATH,
    SESSION_RULES_START,
    check_coverage,
    check_window,
    fail as _shared_fail,
    restrict_result,
    slice_bundle,
)
from cta_dow.backtest import run_backtest
from cta_dow.report import IN_SAMPLE_END, write_outputs
from cta_dow.shadow import run_shadow_product

__all__ = [
    "SESSION_RULES_LAST",
    "SESSION_RULES_PATH",
    "SESSION_RULES_START",
    "Options",
    "build_parser",
    "main",
    "resolve_options",
]


#: 研报口径固定值 —— 不做命令行开关。
EMA_SPANS = (12, 26, 9)
ATR_WINDOW = 20
TARGET_ANNUAL_VOL = 0.15
DEFAULT_COST_BPS = 1.3

_PROG = "cta_dow"


@dataclass(frozen=True, slots=True)
class Options:
    panel_dir: Path
    start: date
    end: date
    output_prefix: str
    signal_mode: str
    cost_bps: float
    require_paper_faithful: bool
    run_literal_sensitivity: bool
    ema_spans: tuple[int, int, int] = EMA_SPANS
    atr_window: int = ATR_WINDOW
    target_vol: float = TARGET_ANNUAL_VOL
    in_sample_end: date = IN_SAMPLE_END

    @property
    def sensitivity_prefix(self) -> str:
        prefix = Path(self.output_prefix)
        return str(prefix.with_name(f"{prefix.name}_literal"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cta_dow",
        description="国信《基于道氏理论的商品期货交易策略》复刻",
    )
    parser.add_argument("--panel-dir", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument(
        "--signal-mode",
        choices=("latched", "literal"),
        default="latched",
        help="入场后锁存（忠实默认）或逐 bar 重验全部三道闸",
    )
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument(
        "--require-paper-faithful",
        action="store_true",
        help="区间内存在无法定价的应成交窗口时直接拒绝运行",
    )
    parser.add_argument(
        "--run-literal-sensitivity",
        action="store_true",
        help="另出一份逐 bar 重验的敏感性产物，仅用于解释差异",
    )
    return parser


def _fail(message: str) -> None:
    _shared_fail(_PROG, message)


def resolve_options(namespace: argparse.Namespace) -> Options:
    start: date = namespace.start
    end: date = namespace.end
    check_window(_PROG, start, end)
    if namespace.signal_mode == "literal" and namespace.run_literal_sensitivity:
        _fail("--signal-mode literal 与 --run-literal-sensitivity 会产出同一份结果，请二选一")
    if not (namespace.cost_bps >= 0.0):
        _fail(f"--cost-bps {namespace.cost_bps} 必须非负")

    return Options(
        panel_dir=Path(namespace.panel_dir),
        start=start,
        end=end,
        output_prefix=str(namespace.output_prefix),
        signal_mode=str(namespace.signal_mode),
        cost_bps=float(namespace.cost_bps),
        require_paper_faithful=bool(namespace.require_paper_faithful),
        run_literal_sensitivity=bool(namespace.run_literal_sensitivity),
    )


def _run_one(
    bundle: PanelBundle,
    options: Options,
    *,
    signal_mode: str,
    output_prefix: str,
    sensitivity_only: bool,
) -> None:
    sliced = slice_bundle(bundle, options.end)
    products = sorted({str(value) for value in sliced.bars["product"]})
    shadows = {
        product: run_shadow_product(
            sliced.bars,
            product=product,
            roll_fills=sliced.roll_fills,
            signal_mode=signal_mode,
            atr_window=options.atr_window,
            cost_bps=options.cost_bps,
        )
        for product in products
    }
    result = run_backtest(
        bundle=sliced,
        shadows=shadows,
        target_vol=options.target_vol,
        cost_bps=options.cost_bps,
    )
    run_config = {
        "start": options.start.isoformat(),
        "end": options.end.isoformat(),
        "panel_dir": str(options.panel_dir),
        "signal_mode": signal_mode,
        "cost_bps": options.cost_bps,
        "ema_fast": options.ema_spans[0],
        "ema_slow": options.ema_spans[1],
        "ema_signal": options.ema_spans[2],
        "atr_window": options.atr_window,
        "target_annual_vol": options.target_vol,
        "in_sample_end": options.in_sample_end.isoformat(),
        "products": products,
        "require_paper_faithful": options.require_paper_faithful,
    }
    write_outputs(
        restrict_result(result, options.start),
        output_prefix=output_prefix,
        universe=sliced.universes,
        dominant_rolls=sliced.dominants,
        run_config=run_config,
        manifest=dict(bundle.manifest),
        sensitivity_only=sensitivity_only,
    )


def main(argv: list[str] | None = None) -> int:
    options = resolve_options(build_parser().parse_args(argv))
    bundle = read_bundle(options.panel_dir)
    check_coverage(
        _PROG,
        bundle,
        start=options.start,
        end=options.end,
        require_paper_faithful=options.require_paper_faithful,
    )
    _run_one(
        bundle,
        options,
        signal_mode=options.signal_mode,
        output_prefix=options.output_prefix,
        sensitivity_only=options.signal_mode == "literal",
    )
    if options.run_literal_sensitivity:
        _run_one(
            bundle,
            options,
            signal_mode="literal",
            output_prefix=options.sensitivity_prefix,
            sensitivity_only=True,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
