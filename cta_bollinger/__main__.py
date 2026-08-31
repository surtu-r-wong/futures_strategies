"""Run the Guosen Bollinger replication over a cached commodity panel bundle.

Only two things are adjustable here, and neither is a parameter of the strategy:
the standard-deviation convention the paper never disclosed (``--ddof``, with
its sensitivity twin) and the cost assumption. Length, beta, the take-profit
coefficient, the open-interest windows, the target volatility, and the sample
cutoff are constants -- exposing them as flags would turn an undisclosed
parameter into a dial, and the only surface left to fit is the gap against the
paper's own headline.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import pandas as pd

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
from cta_bollinger.backtest import run_backtest
from cta_bollinger.report import IN_SAMPLE_END, write_outputs
from cta_bollinger.shadow import run_shadow_product

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
BAND_LENGTH = 300
BAND_BETA = 1.5
ATR_WINDOW = 20
OI_SHORT = 150
OI_LONG = 300
TARGET_ANNUAL_VOL = 0.10
DEFAULT_COST_BPS = 1.3


@dataclass(frozen=True, slots=True)
class Options:
    panel_dir: Path
    start: date
    end: date
    output_prefix: str
    ddof: int
    cost_bps: float
    require_paper_faithful: bool
    run_ddof_sensitivity: bool
    length: int = BAND_LENGTH
    beta: float = BAND_BETA
    atr_window: int = ATR_WINDOW
    oi_short: int = OI_SHORT
    oi_long: int = OI_LONG
    target_vol: float = TARGET_ANNUAL_VOL
    in_sample_end: date = IN_SAMPLE_END

    @property
    def sensitivity_prefix(self) -> str:
        prefix = Path(self.output_prefix)
        return str(prefix.with_name(f"{prefix.name}_ddof1"))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m cta_bollinger",
        description="国信《基于 Bollinger 通道的商品期货交易策略》复刻",
    )
    parser.add_argument("--panel-dir", required=True, type=Path)
    parser.add_argument("--start", required=True, type=date.fromisoformat)
    parser.add_argument("--end", required=True, type=date.fromisoformat)
    parser.add_argument("--output-prefix", required=True)
    parser.add_argument(
        "--ddof",
        type=int,
        choices=(0, 1),
        default=0,
        help="布林标准差自由度；研报未披露，忠实默认 0",
    )
    parser.add_argument("--cost-bps", type=float, default=DEFAULT_COST_BPS)
    parser.add_argument(
        "--require-paper-faithful",
        action="store_true",
        help="区间内存在无法定价的应成交窗口时直接拒绝运行",
    )
    parser.add_argument(
        "--run-ddof-sensitivity",
        action="store_true",
        help="另出一份 ddof=1 的敏感性产物，仅用于解释差异",
    )
    return parser


_PROG = "cta_bollinger"


def _fail(message: str) -> None:
    _shared_fail(_PROG, message)


def resolve_options(namespace: argparse.Namespace) -> Options:
    start: date = namespace.start
    end: date = namespace.end
    check_window(_PROG, start, end)
    if namespace.ddof == 1 and namespace.run_ddof_sensitivity:
        _fail("--ddof 1 与 --run-ddof-sensitivity 会产出同一份结果，请二选一")
    if not (namespace.cost_bps >= 0.0):
        _fail(f"--cost-bps {namespace.cost_bps} 必须非负")

    return Options(
        panel_dir=Path(namespace.panel_dir),
        start=start,
        end=end,
        output_prefix=str(namespace.output_prefix),
        ddof=int(namespace.ddof),
        cost_bps=float(namespace.cost_bps),
        require_paper_faithful=bool(namespace.require_paper_faithful),
        run_ddof_sensitivity=bool(namespace.run_ddof_sensitivity),
    )


def _run_one(
    bundle: PanelBundle,
    options: Options,
    *,
    ddof: int,
    output_prefix: str,
    sensitivity_only: bool,
    unpriceable_fill_windows: int,
) -> None:
    sliced = slice_bundle(bundle, options.end)
    products = sorted({str(value) for value in sliced.bars["product"]})
    shadows = {
        product: run_shadow_product(
            sliced.bars,
            product=product,
            roll_fills=sliced.roll_fills,
            band_length=options.length,
            atr_window=options.atr_window,
            oi_short=options.oi_short,
            oi_long=options.oi_long,
            beta=options.beta,
            ddof=ddof,
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
        "ddof": ddof,
        "cost_bps": options.cost_bps,
        "band_length": options.length,
        "band_beta": options.beta,
        "atr_window": options.atr_window,
        "oi_short": options.oi_short,
        "oi_long": options.oi_long,
        "target_annual_vol": options.target_vol,
        "in_sample_end": options.in_sample_end.isoformat(),
        "products": products,
        "require_paper_faithful": options.require_paper_faithful,
        # 区间内不可定价的成交窗口数：这条只入账，不拦跑；策略真正需要的那一笔
        # 若定不出价，回测会在那一根上硬失败。
        "unpriceable_fill_windows": unpriceable_fill_windows,
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
    unpriceable_fill_windows = check_coverage(
        _PROG,
        bundle,
        start=options.start,
        end=options.end,
        require_paper_faithful=options.require_paper_faithful,
    )
    _run_one(
        bundle,
        options,
        unpriceable_fill_windows=unpriceable_fill_windows,
        ddof=options.ddof,
        output_prefix=options.output_prefix,
        sensitivity_only=False,
    )
    if options.run_ddof_sensitivity:
        _run_one(
            bundle,
            options,
            ddof=1,
            output_prefix=options.sensitivity_prefix,
            sensitivity_only=True,
            unpriceable_fill_windows=unpriceable_fill_windows,
        )
    return 0


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
