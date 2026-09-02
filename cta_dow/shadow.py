"""Causal one-product Dow shadow strategy.

Segments, extremes, and the MACD trend all live on the back-adjusted
continuous price. That is not a convenience: the entry gates compare a price
against extremes recorded segments earlier, and on the raw contract series a
roll would move every level at once, inventing breakouts out of the roll gap.
Position sizing and every fill use the specific contract's raw price, and the
ATR leverage is factor-invariant, so the two bases meet without a seam.

Execution, rolls, the daily boundary, and trade accounting come from
``common.commodity.execution``, shared with the Bollinger replication.
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
from cta_dow.indicators import Trend, macd_path, preliminary_trend
from cta_dow.signals import Position, SignalMode, TradeState, decide
from cta_dow.state import SegmentState, inspect_bar

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
    "target_magnitude",
    "entry_trend",
    "entry_signal_close",
    "exit_signal_close",
    "exit_reason",
    "roll_count",
    "gross_return",
    "cost",
    "net_return",
)


@dataclass(frozen=True, slots=True)
class ShadowResult:
    """One product's copied signal, logical-trade, and daily result frames."""

    product: str
    signal_mode: str
    signals: pd.DataFrame
    trades: pd.DataFrame
    daily: pd.DataFrame

    def __post_init__(self) -> None:
        if not isinstance(self.product, str) or not self.product:
            raise ValueError("dow_shadow_product: expected nonempty string")
        if self.signal_mode not in tuple(mode.value for mode in SignalMode):
            raise ValueError("dow_shadow_signal_mode: unknown mode")
        for name in ("signals", "trades", "daily"):
            frame = getattr(self, name)
            if not isinstance(frame, pd.DataFrame):
                raise ValueError(f"dow_shadow_{name}: expected DataFrame")
            object.__setattr__(self, name, frame.copy(deep=True))


def _history(values: tuple[float, ...], index: int) -> float:
    return values[index] if len(values) > index else np.nan


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
        "macd": np.nan,
        "signal_line": np.nan,
        "macd_diff": np.nan,
        "cumulative": np.nan,
        "atr_adjusted": np.nan,
        "atr_raw": np.nan,
        "trend": Trend.NEUTRAL.value,
        "trend_changed": False,
        "turning_valid": False,
        "dow_resonance": False,
        "close_breakout": False,
        "enough_history": False,
        "prior_segment_high": np.nan,
        "prior_segment_low": np.nan,
        "segment_high": np.nan,
        "segment_low": np.nan,
        "last_up_high_1": np.nan,
        "last_up_high_2": np.nan,
        "last_down_low_1": np.nan,
        "last_down_low_2": np.nan,
        "state_position": Position.FLAT.value,
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
        "roll_event": False,
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


def _resolve_mode(value: object) -> SignalMode:
    if isinstance(value, SignalMode):
        return value
    try:
        return SignalMode(value)
    except (ValueError, KeyError, TypeError) as exc:
        raise ValueError(
            f"dow_shadow_signal_mode: expected latched or literal; got {value!r}"
        ) from exc


def _trend_path(cumulative: np.ndarray, atr: np.ndarray) -> tuple[Trend, ...]:
    """Determine the trend only where the ATR threshold is usable.

    A bar without a full-window ATR, or with an ATR of zero, has no threshold
    to compare against -- a zero threshold would make every accumulation a
    trigger. Such a bar carries the previous trend instead, which is the same
    thing the between-thresholds rule already does, and is registered as
    fidelity rule D8.
    """
    usable = np.isfinite(atr) & (atr > 0.0)
    determined = preliminary_trend(
        cumulative=cumulative[usable].tolist(), atr=atr[usable].tolist()
    )
    out: list[Trend] = []
    carried = Trend.NEUTRAL
    cursor = 0
    for index in range(len(cumulative)):
        if usable[index]:
            carried = determined[cursor]
            cursor += 1
        out.append(carried)
    return tuple(out)


def run_shadow_product(
    bars: object,
    *,
    product: str,
    roll_fills: pd.DataFrame | None = None,
    signal_mode: str | SignalMode = SignalMode.LATCHED,
    atr_window: int = 20,
    cost_bps: float = 1.3,
) -> ShadowResult:
    """Run one product without the portfolio-level volatility multiplier."""
    nonempty_string(product, "product")
    mode = _resolve_mode(signal_mode)
    if type(atr_window) is not int or atr_window < 1:
        raise ValueError("dow_shadow_atr_window: expected positive integer")
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
                    **{column: 0.0 for column in DAILY_COLUMNS[2:]},
                    "gross_equity": 1.0,
                    "equity": 1.0,
                }
                for day in sorted(frame["trade_date"].unique())
            ),
            columns=DAILY_COLUMNS,
        )
        return ShadowResult(
            product,
            mode.value,
            pd.DataFrame(signal_rows),
            pd.DataFrame(columns=_TRADE_COLUMNS),
            daily,
        )

    factors = traded["adj_factor"].to_numpy(dtype="float64")
    adjusted_open = traded["open"].to_numpy(dtype="float64") * factors
    adjusted_high = traded["high"].to_numpy(dtype="float64") * factors
    adjusted_low = traded["low"].to_numpy(dtype="float64") * factors
    adjusted_close = traded["close"].to_numpy(dtype="float64") * factors
    # 每个连续段各自算指标、各自预热、各自判趋势。断代两侧价格不可比，跨段的
    # EMA 与累计距离只是把两段不相干的价格拼在一起（保真度 F10）。
    segments = traded["continuity_segment"].to_numpy()
    slices = segment_slices(segments)
    blank = lambda: np.full(len(adjusted_close), np.nan)
    macd, signal_line, macd_diff, cumulative = blank(), blank(), blank(), blank()
    adjusted_atr = blank()
    trends: list[Trend] = [Trend.NEUTRAL] * len(adjusted_close)
    for span in slices:
        path = macd_path(adjusted_close[span])
        macd[span] = path.macd
        signal_line[span] = path.signal_line
        macd_diff[span] = path.diff
        cumulative[span] = path.cumulative
        atr = atr_series(
            adjusted_high[span], adjusted_low[span], adjusted_close[span],
            window=atr_window,
        )
        atr[: min(atr_window - 1, len(atr))] = np.nan
        adjusted_atr[span] = atr
        trends[span] = list(_trend_path(path.cumulative, atr))
    trends = tuple(trends)

    traded_positions = list(traded.index)
    for position, frame_index in enumerate(traded_positions):
        output = signal_rows[frame_index]
        factor = factors[position]
        output.update(
            signal_open=adjusted_open[position],
            signal_high=adjusted_high[position],
            signal_low=adjusted_low[position],
            signal_close=adjusted_close[position],
            macd=macd[position],
            signal_line=signal_line[position],
            macd_diff=macd_diff[position],
            cumulative=cumulative[position],
            atr_adjusted=adjusted_atr[position],
            atr_raw=adjusted_atr[position] / factor,
        )

    ledger = ShadowLedger(
        product=product,
        calendar=SessionCalendar.from_bars(frame),
        timezone=frame["slot_end"].dt.tz,
        trade_columns=_TRADE_COLUMNS,
        cost_bps=cost_bps,
        error_prefix="dow_shadow",
    )
    first = traded.iloc[0]
    ledger.initialize(str(first["contract"]), float(first["close"]))

    segment = SegmentState.empty()
    trade_state = TradeState(Position.FLAT)
    previous_contract: str | None = None
    previous_pricing_basis: str | None = None
    used_rolls: set[int] = set()
    number_by_index = {
        frame_index: number for number, frame_index in enumerate(traded_positions)
    }
    segment_by_index = {
        frame_index: int(segments[number])
        for number, frame_index in enumerate(traded_positions)
    }
    # 只在**还有下一段**的段末强制平仓；面板最后一根不是断代。
    # 换了合约却没有换月成交单 —— 没人能执行的转移，与断代同样处理（判据与理由见
    # `unexecutable_transitions`）：切换前那一根强制平仓，切换那一根清空状态机。
    unexecutable_breaks, unexecutable_switches = unexecutable_transitions(traded, rolls)
    segment_last_bars = {
        traded_positions[span.stop - 1] for span in slices[:-1]
    } | unexecutable_breaks
    segment_first_bars = {traded_positions[span.start] for span in slices}
    previous_segment: int | None = None

    def carry(output: dict[str, object]) -> None:
        output.update(
            state_position=trade_state.position.value,
            target_direction=int(math.copysign(1, ledger.current_target))
            if ledger.current_target
            else 0,
            target_magnitude=abs(ledger.current_target),
            target_weight=ledger.current_target,
        )

    def record_segment(output: dict[str, object], prior: SegmentState, decision) -> None:
        state = decision.next_state
        output.update(
            trend=state.trend.value if not decision.trend_changed else state.trend.value,
            trend_changed=decision.trend_changed,
            turning_valid=decision.turning_valid,
            dow_resonance=decision.dow_resonance,
            close_breakout=decision.close_breakout,
            enough_history=decision.enough_history,
            prior_segment_high=prior.segment_high
            if prior.segment_high is not None
            else np.nan,
            prior_segment_low=prior.segment_low
            if prior.segment_low is not None
            else np.nan,
            segment_high=state.segment_high if state.segment_high is not None else np.nan,
            segment_low=state.segment_low if state.segment_low is not None else np.nan,
            last_up_high_1=_history(state.last_up_highs, 0),
            last_up_high_2=_history(state.last_up_highs, 1),
            last_down_low_1=_history(state.last_down_lows, 0),
            last_down_low_2=_history(state.last_down_lows, 1),
        )

    for trade_date, day_frame in frame.groupby("trade_date", sort=True):
        ledger.close_trailing_days(trade_date)
        for frame_index, row in day_frame.iterrows():
            output = signal_rows[frame_index]
            if bool(row["no_trade"]):
                ledger.drain_until(row["slot_end"])
                carry(output)
                continue

            contract = str(row["contract"])
            current_segment = segment_by_index[frame_index]
            if (
                previous_segment is not None and current_segment != previous_segment
            ) or frame_index in unexecutable_switches:
                # 新的一段（或一次没人能执行的换月）：趋势段、极值历史与持仓状态全部
                # 重来，且不向换月要成交 —— 断代处两张合约从没同日交易过，不可执行的
                # 换月则是那五分钟根本没有成交。
                segment = SegmentState.empty()
                trade_state = TradeState(Position.FLAT)
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
                        "dow_shadow_roll_missing: exact contract transition fill required"
                    )
                roll = matches.iloc[0]
                if roll["fill_time"] >= row["slot_end"]:
                    raise ValueError(
                        "dow_shadow_roll_time: roll must precede bar signal"
                    )
                if (
                    roll["old_pricing_basis"] != previous_pricing_basis
                    or roll["new_pricing_basis"] != row["pricing_basis"]
                ):
                    raise ValueError(
                        "dow_shadow_roll_pricing_basis: both legs must match panel provenance"
                    )
                used_rolls.add(int(matches.index[0]))
                event = ledger.roll(
                    fill_time=roll["fill_time"],
                    old_contract=previous_contract,
                    new_contract=contract,
                    old_price=float(roll["old_price"]),
                    new_price=float(roll["new_price"]),
                )
                output.update(
                    roll_event=True,
                    roll_fill_time=roll["fill_time"],
                    roll_old_contract=previous_contract,
                    roll_new_contract=contract,
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
            elif previous_contract is None:
                # 这是这个品种在面板里的第一根（或断代后重来的第一根）。首日那笔换月
                # 成交单指向窗口之外的主力，没人能消费它 —— bundle 自己就允许这种
                # 形态（`first_keys`），所以记成"已用"，而不是当作漏用把整跑打断。
                # 实测 PF 2024-01-05（PF402→PF403）就落在 PF 进面板的第一天。
                opening = rolls.loc[
                    (rolls["trade_date"] == trade_date)
                    & (rolls["new_contract"] == contract)
                ]
                used_rolls.update(int(index) for index in opening.index)
                ledger.current_contract = contract
            previous_contract = contract
            previous_pricing_basis = str(row["pricing_basis"])
            ledger.drain_until(row["slot_end"])
            ledger.note_price(contract, row["slot_end"], float(row["close"]))

            position = number_by_index[frame_index]
            if frame_index in segment_last_bars:
                output["action"] = "continuity_break"
                if ledger.current_target != 0.0:
                    if bool(row["fill_unpriceable"]):
                        # 强制平仓没有下一根：下一根已经是新合约，两张合约的原始价
                        # 不可比，信号路径那条「没有对手盘就作废、下一根重新判」在
                        # 这里用不了 —— 仓位真的困住了。按该 bar 的收盘价平掉（那是
                        # 当天真实成交过的价，口径 C 保证 bar 不合成），换掉的计价
                        # 基准由 `continuity_break_close` 单列申报（用户 2026-09-02
                        # 裁决）。价既然是 slot_end 那一刻的，成交时刻就用 slot_end。
                        output["action"] = "continuity_break_close"
                        fill_price = finite(
                            row["close"], "continuity break close", positive=True
                        )
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
                            "exit_signal_close": float(adjusted_close[position])
                        },
                        on_execute=lambda output=output: output.__setitem__(
                            "action_changed", True
                        ),
                    )
                    trade_state = TradeState(Position.FLAT)
                carry(output)
                continue

            atr_adjusted = adjusted_atr[position]
            if not math.isfinite(atr_adjusted) or atr_adjusted <= 0.0:
                output["action"] = (
                    "warmup" if frame_index in segment_first_bars else "atr_unavailable"
                )
                carry(output)
                continue

            prior_segment = segment
            decision = inspect_bar(
                segment,
                trend=trends[position],
                high=float(adjusted_high[position]),
                low=float(adjusted_low[position]),
                close=float(adjusted_close[position]),
            )
            segment = decision.next_state
            record_segment(output, prior_segment, decision)

            signal = decide(
                trade_state,
                trend=trends[position],
                trend_changed=decision.trend_changed,
                turning_valid=decision.turning_valid,
                dow_resonance=decision.dow_resonance,
                close_breakout=decision.close_breakout,
                enough_history=decision.enough_history,
                mode=mode,
            )
            output["action"] = signal.reason
            if not signal.changed:
                carry(output)
                continue

            if bool(row["fill_pending"]) and frame_index == traded_positions[-1]:
                output["action"] = "fill_pending"
                carry(output)
                continue
            if bool(row["fill_unpriceable"]):
                # 那五分钟根本没人成交 ⇒ 这一笔没有对手盘，信号作废（用户 2026-09-01
                # 裁决）。仓位与状态都不动，下一根重新判；这一根标成 `fill_unavailable`
                # 交给报告层计数。菜籽油 2012-12-26 那种日子全天仍成交 196 手，只是
                # 开盘那一窗无人 —— 剔品种、剔品种日都盖不住它。
                output["action"] = "fill_unavailable"
                carry(output)
                continue
            if bool(row["fill_pending"]):
                raise ValueError("dow_shadow_required fill is unavailable")
            if pd.isna(row["fill_time"]) or pd.isna(row["fill_price"]):
                raise ValueError("dow_shadow_required fill is missing")

            fill_price = finite(row["fill_price"], "required fill", positive=True)
            next_target = 0.0
            if signal.target_direction:
                next_target = signal.target_direction * atr_leverage(
                    close=float(row["close"]), atr=float(output["atr_raw"])
                )
            signal_close = float(adjusted_close[position])
            assert ledger.current_contract is not None
            ledger.request_target(
                trade_date=trade_date,
                fill_time=row["fill_time"],
                contract=ledger.current_contract,
                fill_price=fill_price,
                next_target=next_target,
                direction=signal.target_direction,
                reason=signal.reason,
                entry_fields={
                    "entry_trend": trends[position].value,
                    "entry_signal_close": signal_close,
                },
                exit_fields={"exit_signal_close": signal_close},
                on_execute=lambda output=output: output.__setitem__(
                    "action_changed", True
                ),
            )
            trade_state = signal.state
            if output["action_changed"]:
                carry(output)
            else:
                output.update(
                    state_position=trade_state.position.value,
                    target_direction=signal.target_direction,
                    target_magnitude=abs(next_target),
                    target_weight=next_target,
                )

        ledger.close_day(trade_date, day_frame["slot_end"].max())

    ledger.close_trailing_days(None)

    if len(used_rolls) != len(rolls):
        raise ValueError("dow_shadow_roll_mismatch: unused or mismatched roll fills")

    trades_output, daily_output = ledger.finish()
    return ShadowResult(
        product, mode.value, pd.DataFrame(signal_rows), trades_output, daily_output
    )
