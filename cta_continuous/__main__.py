"""国信连续信号的 CLI 与端到端 —— 计划 Task 8。

回测吃的是**缓存好的面板**，不是数据库：面板每行已经带着 15 分钟 K 线与它那根的
5 分钟成交价，12 次网格于是一次网都不用过（本机到库是 DERP 中继，抖动会半开断连）。

## ⚠️ 越界要报错，不许悄悄截断

`config/continuous_minute_sessions.csv` 的规则止于 2026-01-30。要一个更晚的窗口，
答案是「这段还没采」，不是「给你截到 01-30」—— 悄悄截断会让人拿着一段比以为的短
的样本外去下结论。延长那份资产是交易所公告维护工作，不在本计划内。

## 预注册网格（D9）

EMA `(short, long)` × TNR `N` 共 9 个点，外加基线点上 D2/D3 的 3 个反向对照，合计
12 次。**网格不得事后扩张** —— 扩了就改计划并说明原因。样本内选点，样本外只报不选。
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import replace
from datetime import date
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.minute.sessions import load_session_rules  # noqa: E402
from cta_continuous.backtest import (  # noqa: E402
    BacktestParams,
    product_signals,
    run_backtest,
)
from cta_continuous.report import (  # noqa: E402
    COST_LEVELS,
    build_sheets,
    cost_sensitivity,
    segment_metrics,
    write_outputs,
)

__all__ = ["GRID", "REVERSALS", "load_panel", "main", "session_horizon"]

DEFAULT_SESSIONS = Path("config/continuous_minute_sessions.csv")

#: D9 的预注册网格：9 个点。ATR 的 n 固定 20、k 固定 3（研报给定）。
GRID: tuple[tuple[int, int, int], ...] = tuple(
    (short, long, window)
    for short, long in ((12, 26), (10, 60), (20, 120))
    for window in (10, 20, 40)
)

#: 基线点上的三个反向对照（D2 / D3 的两处研报自相矛盾）。
REVERSALS: tuple[tuple[str, str], ...] = (
    ("reversed", "positive"),
    ("paper", "negative"),
    ("reversed", "negative"),
)

#: 基线参数点。反向对照都在它上面跑。
BASELINE = (12, 26, 20)


def _month(value: str) -> date:
    year, month = value.split("-")
    return date(int(year), int(month), 1)


def _next_month(anchor: date) -> date:
    return date(anchor.year + anchor.month // 12, anchor.month % 12 + 1, 1)


def session_horizon(path: Path) -> date:
    """时段规则资产覆盖到哪一天。

    ``effective_end`` 为空表示该规则仍然开着 —— 那种资产没有地平线可言，调用方
    不该据此放行任意窗口，所以这里硬失败而不是返回一个猜的日期。
    """
    rules = load_session_rules(path)
    if not rules:
        raise ValueError(f"session_rules_empty: {path} 里一条规则都没有")
    ends = [rule.effective_end for rule in rules]
    if any(end is None for end in ends):
        raise ValueError(
            f"session_rules_open_ended: {path} 有未封口的规则，地平线无从判定"
        )
    return max(ends)


def load_panel(directory: Path, *, start: date, end: date) -> pd.DataFrame:
    """读 `[start, end]` 覆盖到的月度分片。

    缺月不补也不猜：分片一个都没有就硬失败，说清看的是哪个目录、要的是哪几个月。
    """
    months: list[date] = []
    cursor = start
    while cursor <= end:
        months.append(cursor)
        cursor = _next_month(cursor)
    shards = [directory / f"panel-{month:%Y-%m}.parquet" for month in months]
    present = [shard for shard in shards if shard.exists()]
    if not present:
        raise ValueError(
            f"panel_shards_missing: {directory} 里没有 {start:%Y-%m}..{end:%Y-%m} 的任何分片"
        )
    frames = [pd.read_parquet(shard) for shard in present]
    panel = pd.concat(frames, ignore_index=True)
    return panel.sort_values(["slot_end", "product"]).reset_index(drop=True)


def _params(args, *, ema_short, ema_long, tnr_window, ma_orientation, tnr_sign):
    return BacktestParams(
        ema_short=ema_short,
        ema_long=ema_long,
        tnr_window=tnr_window,
        atr_window=args.atr_window,
        dtnr_k=args.dtnr_k,
        cost_bps=args.cost_bps,
        ma_orientation=ma_orientation,
        tnr_sign=tnr_sign,
        dtnr_mode=args.dtnr_mode,
        min_observations=args.min_observations,
    )


def _points(args) -> tuple[tuple[str, BacktestParams], ...]:
    if not args.grid:
        return (
            (
                "single",
                _params(
                    args,
                    ema_short=args.ema_short,
                    ema_long=args.ema_long,
                    tnr_window=args.tnr_window,
                    ma_orientation=args.ma_orientation,
                    tnr_sign=args.tnr_sign,
                ),
            ),
        )
    points = [
        (
            f"grid-{short}-{long}-N{window}",
            _params(
                args,
                ema_short=short,
                ema_long=long,
                tnr_window=window,
                ma_orientation="paper",
                tnr_sign="positive",
            ),
        )
        for short, long, window in GRID
    ]
    short, long, window = BASELINE
    points += [
        (
            f"reversal-ma{orientation}-tnr{sign}",
            _params(
                args,
                ema_short=short,
                ema_long=long,
                tnr_window=window,
                ma_orientation=orientation,
                tnr_sign=sign,
            ),
        )
        for orientation, sign in REVERSALS
    ]
    return tuple(points)


def _summary_row(label: str, params: BacktestParams, result) -> dict:
    segments = {segment.name: segment for segment in segment_metrics(result.daily)}
    row = {
        "label": label,
        "ema_short": params.ema_short,
        "ema_long": params.ema_long,
        "tnr_window": params.tnr_window,
        "ma_orientation": params.ma_orientation,
        "tnr_sign": params.tnr_sign,
        "dtnr_mode": params.dtnr_mode,
        "cost_bps": params.cost_bps,
    }
    for name in ("full", "in_sample", "out_of_sample"):
        segment = segments[name]
        row[f"{name}_observations"] = segment.observations
        for metric in ("ann_return", "sharpe", "max_drawdown", "ann_vol", "calmar"):
            row[f"{name}_{metric}"] = segment.metrics.get(metric)
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cta_continuous")
    parser.add_argument("--panel", required=True, type=Path, help="面板分片目录")
    parser.add_argument("--start", required=True, type=_month)
    parser.add_argument("--end", required=True, type=_month)
    parser.add_argument("--sessions", type=Path, default=DEFAULT_SESSIONS)
    parser.add_argument("--ema-short", type=int, default=12)
    parser.add_argument("--ema-long", type=int, default=26)
    parser.add_argument("--tnr-window", type=int, default=20)
    parser.add_argument("--atr-window", type=int, default=20)
    parser.add_argument("--dtnr-k", type=int, default=3)
    parser.add_argument("--cost-bps", type=float, default=1.3)
    parser.add_argument("--min-observations", type=int, default=252)
    parser.add_argument(
        "--ma-orientation",
        choices=("paper", "reversed"),
        default="paper",
        help="D2：短均线在上为多头（paper，§2.1 正文）还是反过来（reversed，§5.1 汇总框）",
    )
    parser.add_argument(
        "--tnr-sign",
        choices=("positive", "negative"),
        default="positive",
        help="D3：噪音闸取 ΔTNR>0（positive，正文与表 4）还是 <0（negative，§5.1 汇总框）",
    )
    parser.add_argument(
        "--dtnr-mode",
        choices=("mean", "lag"),
        default="mean",
        help="D7：ΔTNR 与近 k 期均值比（mean，公式图）还是与 k 期前比（lag，正文）",
    )
    parser.add_argument("--grid", action="store_true", help="跑 D9 的 9 个网格点 + 3 个反向对照")
    parser.add_argument("--cost-sensitivity", action="store_true", help="追加研报表 8 的三档成本")
    parser.add_argument("--output-prefix", required=True, type=Path)
    args = parser.parse_args(argv)

    # ⚠️ 闸在读面板之前。越界报错、不截断（§4 第 1 条）。
    #
    # 按**月**比，不按日：资产止于 2026-01-30，而 01-31 是周六、本来就没有交易日。
    # 拿自然月末去比会把 `--end 2026-01` 也拒掉，白丢一个月的样本外 —— 那不是
    # 「拒绝越界」，是把闸调过了头。真正要挡的是「要了 2026-06 却拿到 01 月的数据
    # 还不知道」，所以判据是**请求的末月是否整个落在地平线所在月之后**。
    horizon = session_horizon(args.sessions)
    horizon_month = date(horizon.year, horizon.month, 1)
    if args.end > horizon_month:
        print(
            f"session_horizon_exceeded: 时段规则止于 {horizon}，"
            f"请求的末月是 {args.end:%Y-%m}。延长该资产是交易所公告维护工作，"
            "不在本计划内；这里不做静默截断。",
            file=sys.stderr,
        )
        return 2
    month_end = _next_month(args.end) - pd.Timedelta(days=1).to_pytimedelta()
    window_end = min(month_end, horizon)

    panel = load_panel(args.panel, start=args.start, end=args.end)
    print(f"面板 {len(panel):,} 行，{panel['product'].nunique()} 个品种", flush=True)

    summary: list[dict] = []
    for label, params in _points(args):
        signals = product_signals(panel, params=params)
        result = run_backtest(panel, params=params, signals=signals)
        sheets = build_sheets(result)
        if args.cost_sensitivity:
            sheets["cost_sensitivity"] = cost_sensitivity(
                runner=lambda *, params: run_backtest(
                    panel, params=params, signals=signals
                ),
                params=params,
                levels=COST_LEVELS,
            )
        prefix = (
            args.output_prefix
            if label == "single"
            else args.output_prefix.parent / f"{args.output_prefix.name}-{label}"
        )
        write_outputs(sheets, prefix=prefix)
        row = _summary_row(label, params, result)
        summary.append(row)
        print(
            f"{label}: 样本内 {row['in_sample_observations']} 天 "
            f"夏普 {row['in_sample_sharpe']}, "
            f"样本外 {row['out_of_sample_observations']} 天 "
            f"夏普 {row['out_of_sample_sharpe']}",
            flush=True,
        )

    frame = pd.DataFrame(summary)
    summary_path = args.output_prefix.parent / f"{args.output_prefix.name}-summary.csv"
    summary_path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(summary_path, index=False)
    (args.output_prefix.parent / f"{args.output_prefix.name}-window.json").write_text(
        json.dumps(
            {
                "start": str(args.start),
                "end": str(window_end),
                "session_horizon": str(horizon),
                "points": [row["label"] for row in summary],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
