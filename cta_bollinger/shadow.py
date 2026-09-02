"""Causal one-product Bollinger shadow strategy.

The signal path runs on back-adjusted continuous prices; every fill, position
size, and roll uses the specific contract's raw price. Execution, rolls, the
daily boundary, and logical-trade accounting live in
``common.commodity.execution`` -- the Dow replication trades the same way and a
defect in that half must not need fixing twice.
"""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
import pandas as pd

from common.commodity.execution import (
    DAILY_COLUMNS,
    ShadowLedger,
    finite,
    nonempty_string,
    prepare_bars,
    prepare_rolls,
    unexecutable_transitions,
    segment_slices,
)
from common.commodity.indicators import atr_series
from common.commodity.panel import SessionCalendar
from common.leverage import atr_leverage
from cta_bollinger.indicators import bands, oi_multiplier, rolling_oi
from cta_bollinger.signals import Position, State, step

__all__ = ["ShadowResult", "run_shadow_product"]


_TRADE_COLUMNS = (
    "product",
    "trade_id",
    "entry_date",
    "exit_date",
    "entry_time",
    "exit_time",
    "entry_contract",
    "exit_contract",
    "entry_price",
    "exit_price",
    "direction",
    "oi_scale",
    "target_magnitude",
    "entry_signal_close",
    "exit_signal_close",
    "exit_reason",
    "roll_count",
    "gross_return",
    "cost",
    "net_return",
)

_DAILY_COLUMNS = DAILY_COLUMNS


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """One product's copied signal, logical-trade, and daily result frames."""

    product: str
    signals: pd.DataFrame
    trades: pd.DataFrame
    daily: pd.DataFrame

    def __post_init__(self) -> None:
        if not isinstance(self.product, str) or not self.product:
            raise ValueError("bollinger_shadow_product: expected nonempty string")
        for name in ("signals", "trades", "daily"):
            frame = getattr(self, name)
            if not isinstance(frame, pd.DataFrame):
                raise ValueError(f"bollinger_shadow_{name}: expected DataFrame")
            object.__setattr__(self, name, frame.copy(deep=True))


def _blank_signal(row) -> dict[str, object]:
    return {
        "product": str(row.product),
        "trade_date": row.trade_date,
        "slot_end": row.slot_end,
        "contract": str(row.contract),
        "no_trade": bool(row.no_trade),
        "raw_open": row.open,
        "raw_high": row.high,
        "raw_low": row.low,
        "raw_close": row.close,
        "adj_factor": row.adj_factor,
        "signal_open": np.nan,
        "signal_high": np.nan,
        "signal_low": np.nan,
        "signal_close": np.nan,
        "middle": np.nan,
        "std": np.nan,
        "upper": np.nan,
        "lower": np.nan,
        "atr_adjusted": np.nan,
        "atr_raw": np.nan,
        "oi_short": np.nan,
        "oi_long": np.nan,
        "oi_scale_input": np.nan,
        "state_position": Position.FLAT.value,
        "state_take_profit": np.nan,
        "state_oi_scale": 0.0,
        "action": "no_trade" if row.no_trade else "warmup",
        "action_changed": False,
        "target_direction": 0,
        "target_magnitude": 0.0,
        "target_weight": 0.0,
        "fill_time": row.fill_time,
        "fill_price": row.fill_price,
        "fill_pending": bool(row.fill_pending),
        "fill_unpriceable": bool(row.fill_unpriceable),
        "pricing_basis": row.pricing_basis,
        "multiplier": row.multiplier,
        "roll_fill_time": pd.NaT,
        "roll_old_contract": None,
        "roll_new_contract": None,
        "roll_old_price": np.nan,
        "roll_new_price": np.nan,
        "roll_old_pricing_basis": None,
        "roll_new_pricing_basis": None,
        "roll_execution_count": 0,
        "roll_turnover": 0.0,
        "roll_cost": 0.0,
    }


def run_shadow_product(
    bars: object,
    *,
    product: str,
    roll_fills: pd.DataFrame | None = None,
    band_length: int = 300,
    atr_window: int = 20,
    oi_short: int = 150,
    oi_long: int = 300,
    beta: float = 1.5,
    ddof: int = 0,
    cost_bps: float = 1.3,
) -> ShadowResult:
    """Run one product without the portfolio-level volatility multiplier."""
    nonempty_string(product, "product")
    if type(atr_window) is not int or atr_window < 1:
        raise ValueError("bollinger_shadow_atr_window: expected positive integer")
    frame, embedded_rolls = prepare_bars(bars, product)
    rolls = prepare_rolls(
        roll_fills if roll_fills is not None else embedded_rolls, product
    )

    traded = frame.loc[~frame["no_trade"]].copy()
    signal_rows = [_blank_signal(row) for row in frame.itertuples(index=False)]
    if traded.empty:
        daily = pd.DataFrame(
            (
                {
                    "product": product,
                    "trade_date": day,
                    **{column: 0.0 for column in _DAILY_COLUMNS[2:]},
                    "gross_equity": 1.0,
                    "equity": 1.0,
                }
                for day in sorted(frame["trade_date"].unique())
            ),
            columns=_DAILY_COLUMNS,
        )
        return ShadowResult(
            product,
            pd.DataFrame(signal_rows),
            pd.DataFrame(columns=_TRADE_COLUMNS),
            daily,
        )

    factors = traded["adj_factor"].to_numpy(dtype="float64")
    adjusted_open = traded["open"].to_numpy(dtype="float64") * factors
    adjusted_high = traded["high"].to_numpy(dtype="float64") * factors
    adjusted_low = traded["low"].to_numpy(dtype="float64") * factors
    adjusted_close = traded["close"].to_numpy(dtype="float64") * factors
    # 每个连续段各自算指标并各自预热。断代两侧的价格不可比，跨段的均线只是把
    # 两段不相干的价格拼在一起（保真度 F10）。
    open_interest = traded["open_interest"].to_numpy(dtype="float64")
    segments = traded["continuity_segment"].to_numpy()
    slices = segment_slices(segments)
    blank = lambda: np.full(len(adjusted_close), np.nan)
    middle, std_path, upper, lower = blank(), blank(), blank(), blank()
    oi_short_path, oi_long_path, adjusted_atr = blank(), blank(), blank()
    for span in slices:
        band = bands(
            adjusted_close[span], length=band_length, beta=beta, ddof=ddof
        )
        middle[span] = band.middle
        std_path[span] = band.std
        upper[span] = band.upper
        lower[span] = band.lower
        oi = rolling_oi(open_interest[span], short=oi_short, long=oi_long)
        oi_short_path[span] = oi.short
        oi_long_path[span] = oi.long
        atr = atr_series(
            adjusted_high[span], adjusted_low[span], adjusted_close[span],
            window=atr_window,
        )
        atr[: min(atr_window - 1, len(atr))] = np.nan
        adjusted_atr[span] = atr

    traded_positions = list(traded.index)
    for position, frame_index in enumerate(traded_positions):
        output = signal_rows[frame_index]
        factor = float(traded.loc[frame_index, "adj_factor"])
        output.update(
            signal_open=adjusted_open[position],
            signal_high=adjusted_high[position],
            signal_low=adjusted_low[position],
            signal_close=adjusted_close[position],
            middle=middle[position],
            std=std_path[position],
            upper=upper[position],
            lower=lower[position],
            atr_adjusted=adjusted_atr[position],
            atr_raw=adjusted_atr[position] / factor,
            oi_short=oi_short_path[position],
            oi_long=oi_long_path[position],
        )

    ledger = ShadowLedger(
        product=product,
        calendar=SessionCalendar.from_bars(frame),
        timezone=frame["slot_end"].dt.tz,
        trade_columns=_TRADE_COLUMNS,
        cost_bps=cost_bps,
        error_prefix="bollinger_shadow",
    )
    first = traded.iloc[0]
    ledger.initialize(str(first["contract"]), float(first["close"]))

    state = State(Position.FLAT, take_profit=None, oi_scale=0.0)
    previous_contract: str | None = None
    previous_pricing_basis: str | None = None
    used_rolls: set[int] = set()
    traded_number_by_index = {
        frame_index: number for number, frame_index in enumerate(traded_positions)
    }
    segment_by_index = {
        frame_index: int(segments[number])
        for number, frame_index in enumerate(traded_positions)
    }
    # 只在**还有下一段**的段末强制平仓；面板最后一根不是断代，仓位照常留着。
    # 换了合约却没有换月成交单 —— 没人能执行的转移，与断代同样处理（判据与理由见
    # `unexecutable_transitions`）：切换前那一根强制平仓，切换那一根清空状态机。
    unexecutable_breaks, unexecutable_switches = unexecutable_transitions(traded, rolls)
    segment_last_bars = {
        traded_positions[span.stop - 1] for span in slices[:-1]
    } | unexecutable_breaks
    previous_segment: int | None = None

    def carry_state(output: dict[str, object]) -> None:
        output.update(
            state_position=state.position.value,
            state_take_profit=state.take_profit
            if state.take_profit is not None
            else np.nan,
            state_oi_scale=state.oi_scale,
            target_direction=int(math.copysign(1, ledger.current_target))
            if ledger.current_target
            else 0,
            target_magnitude=abs(ledger.current_target),
            target_weight=ledger.current_target,
        )

    def record_roll(output: dict[str, object], roll, old_contract: str, event) -> None:
        output.update(
            roll_fill_time=roll["fill_time"],
            roll_old_contract=old_contract,
            roll_new_contract=str(roll["new_contract"]),
            roll_old_price=float(roll["old_price"]),
            roll_new_price=float(roll["new_price"]),
            roll_old_pricing_basis=roll["old_pricing_basis"],
            roll_new_pricing_basis=roll["new_pricing_basis"],
        )
        if event is not None:
            output.update(
                roll_execution_count=len(event.executions),
                roll_turnover=event.turnover,
                roll_cost=event.cost,
            )

    for trade_date, day_frame in frame.groupby("trade_date", sort=True):
        ledger.close_trailing_days(trade_date)
        for frame_index, row in day_frame.iterrows():
            output = signal_rows[frame_index]
            if bool(row["no_trade"]):
                ledger.drain_until(row["slot_end"])
                carry_state(output)
                continue

            contract = str(row["contract"])
            current_segment = segment_by_index[frame_index]
            if (
                previous_segment is not None and current_segment != previous_segment
            ) or frame_index in unexecutable_switches:
                # 新的一段（或一次没人能执行的换月）：状态在上一根已经平掉，这里把
                # 状态机也清干净，并且不再向换月要成交 —— 断代处两张合约从没同日
                # 交易过，不可执行的换月则是那五分钟根本没有成交。
                state = State(Position.FLAT, take_profit=None, oi_scale=0.0)
                previous_contract = None
                previous_pricing_basis = None
            previous_segment = current_segment
            if previous_contract is not None and contract != previous_contract:
                matches = rolls.loc[
                    (rolls["trade_date"] == trade_date)
                    & (rolls["old_contract"] == previous_contract)
                    & (rolls["new_contract"] == contract)
                ]
                if len(matches) != 1:
                    raise ValueError(
                        "bollinger_shadow_roll_missing: exact contract transition fill required"
                    )
                roll = matches.iloc[0]
                if roll["fill_time"] >= row["slot_end"]:
                    raise ValueError(
                        "bollinger_shadow_roll_time: roll must precede bar signal"
                    )
                if (
                    roll["old_pricing_basis"] != previous_pricing_basis
                    or roll["new_pricing_basis"] != row["pricing_basis"]
                ):
                    raise ValueError(
                        "bollinger_shadow_roll_pricing_basis: both legs must match panel provenance"
                    )
                used_rolls.add(int(matches.index[0]))
                event = ledger.roll(
                    fill_time=roll["fill_time"],
                    old_contract=previous_contract,
                    new_contract=contract,
                    old_price=float(roll["old_price"]),
                    new_price=float(roll["new_price"]),
                )
                record_roll(output, roll, previous_contract, event)
            elif previous_contract is None:
                boundary = rolls.loc[
                    (rolls["trade_date"] == trade_date)
                    & (rolls["new_contract"] == contract)
                    & ~rolls.index.isin(used_rolls)
                ]
                if len(boundary) > 1:
                    raise ValueError(
                        "bollinger_shadow_roll_boundary: duplicate boundary fills"
                    )
                if len(boundary) == 1:
                    roll = boundary.iloc[0]
                    if roll["fill_time"] >= row["slot_end"]:
                        raise ValueError(
                            "bollinger_shadow_roll_time: boundary roll must precede bar signal"
                        )
                    if roll["new_pricing_basis"] != row["pricing_basis"]:
                        raise ValueError(
                            "bollinger_shadow_roll_pricing_basis: boundary new leg must match panel provenance"
                        )
                    old_contract = str(roll["old_contract"])
                    if ledger.current_target != 0.0 and (
                        ledger.current_contract != old_contract
                    ):
                        raise ValueError(
                            "bollinger_shadow_roll_boundary: held old leg mismatches boundary"
                        )
                    event = ledger.roll(
                        fill_time=roll["fill_time"],
                        old_contract=old_contract,
                        new_contract=contract,
                        old_price=float(roll["old_price"]),
                        new_price=float(roll["new_price"]),
                    )
                    record_roll(output, roll, old_contract, event)
                    used_rolls.add(int(boundary.index[0]))
                ledger.current_contract = contract
            previous_contract = contract
            previous_pricing_basis = str(row["pricing_basis"])
            ledger.drain_until(row["slot_end"])
            ledger.note_price(contract, row["slot_end"], float(row["close"]))

            if frame_index in segment_last_bars:
                output["action"] = "continuity_break"
                if ledger.current_target != 0.0:
                    # 我们持的这条腿在这个成交窗口有没有对手盘 —— 两种形态都算没有：
                    # ① 那五分钟根本无人成交（`fill_unpriceable`）；
                    # ② 成交窗口落到下一交易日，那时旧腿已经不在面板里，面板给的价
                    #    是**后继合约**的（实测 CS 2021-11-02 / SM 2016-09-01 /
                    #    ZC 2022-05-05，与旧腿收盘差 0.8%~5.4%）。
                    fillable = not bool(row["fill_unpriceable"]) and (
                        ledger.calendar.execution_trade_date(
                            row["fill_time"], trade_date
                        )
                        == trade_date
                    )
                    if not fillable:
                        # 强制平仓没有下一根：下一根已经是新合约，两张合约的原始价
                        # 不可比，信号路径那条「没有对手盘就作废、下一根重新判」在
                        # 这里用不了 —— 仓位真的困住了。按该 bar 的收盘价平掉（那是
                        # 当天真实成交过的价，口径 C 保证 bar 不合成），换掉的计价
                        # 基准由 `continuity_break_close` 单列申报（用户 2026-09-02
                        # 裁决 B 及其延用）。
                        output["action"] = "continuity_break_close"
                        fill_price = finite(
                            row["close"], "continuity break close", positive=True
                        )
                        # 这一行必须报出真正用掉的价：组合层的事件构造见
                        # `action_changed` 就要一个价，不给它就在这一根上硬失败
                        # （实测 FU 2025-08-29 与 AU 2019-12-16）。`fill_time` 不动
                        # —— 组合层拿它与面板逐点对齐，改了会被对齐检查正确拦下。
                        output["fill_price"] = fill_price
                        # 价是 slot_end 那一刻的，账本就在那一刻成交：用面板的成交
                        # 时刻会把这一笔挂到下一交易日，而那时旧腿已经没了。
                        exit_fill_time = row["slot_end"]
                    else:
                        fill_price = finite(
                            row["fill_price"], "continuity break fill", positive=True
                        )
                        exit_fill_time = row["fill_time"]
                    assert ledger.current_contract is not None
                    ledger.request_target(
                        trade_date=trade_date,
                        fill_time=exit_fill_time,
                        contract=ledger.current_contract,
                        fill_price=fill_price,
                        next_target=0.0,
                        direction=0,
                        reason="continuity_break",
                        exit_fields={
                            "exit_signal_close": float(output["signal_close"])
                        },
                        on_execute=lambda output=output: output.__setitem__(
                            "action_changed", True
                        ),
                    )
                    state = State(Position.FLAT, take_profit=None, oi_scale=0.0)
                carry_state(output)
                continue

            number = traded_number_by_index[frame_index]
            ready_values = (
                output["middle"],
                output["std"],
                output["upper"],
                output["lower"],
                output["atr_adjusted"],
                output["oi_short"],
                output["oi_long"],
            )
            previous_ready = number > 0 and all(
                math.isfinite(float(signal_rows[traded_positions[number - 1]][column]))
                for column in ("signal_close", "middle", "upper", "lower")
            )
            if previous_ready and all(
                math.isfinite(float(value)) for value in ready_values
            ):
                previous = signal_rows[traded_positions[number - 1]]
                supplied_scale = oi_multiplier(
                    short_oi=float(output["oi_short"]),
                    long_oi=float(output["oi_long"]),
                )
                output["oi_scale_input"] = supplied_scale
                desired = step(
                    state,
                    previous_close=float(previous["signal_close"]),
                    previous_middle=float(previous["middle"]),
                    previous_upper=float(previous["upper"]),
                    previous_lower=float(previous["lower"]),
                    close=float(output["signal_close"]),
                    middle=float(output["middle"]),
                    upper=float(output["upper"]),
                    lower=float(output["lower"]),
                    std=float(output["std"]),
                    oi_scale=supplied_scale,
                )
                output["action"] = desired.reason
                if desired.changed:
                    if bool(row["fill_pending"]) and frame_index == traded_positions[-1]:
                        output["action"] = "fill_pending"
                    elif bool(row["fill_unpriceable"]):
                        # 那五分钟根本没人成交 ⇒ 这一笔没有对手盘，信号作废（用户
                        # 2026-09-01 裁决）。仓位与状态都不动，下一根重新判；这一根
                        # 标成 `fill_unavailable` 供报告层计数。菜籽油 2012-12-26 那种
                        # 日子全天仍成交 196 手，只是开盘那一窗无人 —— 剔品种、剔
                        # 品种日都盖不住它。
                        output["action"] = "fill_unavailable"
                    else:
                        if bool(row["fill_pending"]):
                            raise ValueError(
                                "bollinger_shadow_required fill is unavailable"
                            )
                        if pd.isna(row["fill_time"]) or pd.isna(row["fill_price"]):
                            raise ValueError("bollinger_shadow_required fill is missing")
                        fill_price = finite(
                            row["fill_price"], "required fill", positive=True
                        )
                        next_direction = desired.target_direction
                        next_target = 0.0
                        if next_direction:
                            next_target = (
                                next_direction
                                * desired.state.oi_scale
                                * atr_leverage(
                                    close=float(row["close"]),
                                    atr=float(output["atr_raw"]),
                                )
                            )
                        signal_close = float(output["signal_close"])
                        assert ledger.current_contract is not None
                        ledger.request_target(
                            trade_date=trade_date,
                            fill_time=row["fill_time"],
                            contract=ledger.current_contract,
                            fill_price=fill_price,
                            next_target=next_target,
                            direction=desired.target_direction,
                            reason=desired.reason,
                            entry_fields={
                                "oi_scale": desired.state.oi_scale,
                                "entry_signal_close": signal_close,
                            },
                            exit_fields={"exit_signal_close": signal_close},
                            on_execute=lambda output=output: output.__setitem__(
                                "action_changed", True
                            ),
                        )
                        state = desired.state
                        if not output["action_changed"]:
                            output.update(
                                state_position=state.position.value,
                                state_take_profit=state.take_profit
                                if state.take_profit is not None
                                else np.nan,
                                state_oi_scale=state.oi_scale,
                                target_direction=desired.target_direction,
                                target_magnitude=abs(next_target),
                                target_weight=next_target,
                            )
                            continue

            carry_state(output)

        ledger.close_day(trade_date, day_frame["slot_end"].max())

    ledger.close_trailing_days(None)

    if len(used_rolls) != len(rolls):
        raise ValueError(
            "bollinger_shadow_roll_mismatch: unused or mismatched roll fills"
        )

    trades_output, daily_output = ledger.finish()
    return ShadowResult(
        product, pd.DataFrame(signal_rows), trades_output, daily_output
    )
