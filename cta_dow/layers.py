"""The paper's three single-product layers, read off one shadow run.

The Dow paper reports iron ore alone at three depths -- preliminary MACD trend
(table 1), turning-point correction (table 2), and Dow resonance (table 3) --
before any selection, allocation, or volatility targeting touches the numbers.
Those tables are the only place the paper can be met layer by layer, so this
module rebuilds each layer's position from the columns the shadow already
records, and evaluates them the way the paper's tables do: close-to-close on
the back-adjusted price, one unit of exposure, a cost per unit of turnover.

Two of the layers admit more than one reading of the paper's words, and the
registered defaults (D4 stand-down, D6 latched breakout entry) are one side of
each. The other readings are computed here as diagnostics only: nothing in
this module feeds the registered results.
"""

from __future__ import annotations

from datetime import date
import math

import numpy as np
import pandas as pd

__all__ = [
    "LAYER_COLUMNS",
    "bar_returns",
    "layer_daily_returns",
    "layer_positions",
    "layer_report",
    "paper_annual_table",
]

LAYER_COLUMNS = (
    "l1_trend",
    "l2_stand_down",
    "l2_reversal",
    "l3_registered",
    "l3_resonance_hold",
    "l3_opposite_flat",
    "l3_breakout_latched",
    "l3_prior_high_breakout",
)

_TREND_DIRECTION = {"up": 1, "down": -1, "neutral": 0}
_STATE_DIRECTION = {"flat": 0, "long": 1, "short": -1}
TRADING_DAYS_PER_YEAR = 252

_REQUIRED = (
    "trade_date",
    "no_trade",
    "continuity_segment",
    "signal_close",
    "trend",
    "turning_valid",
    "close_breakout",
    "last_up_high_1",
    "last_up_high_2",
    "last_down_low_1",
    "last_down_low_2",
    "state_position",
)


def _check(signals: pd.DataFrame) -> None:
    if not isinstance(signals, pd.DataFrame):
        raise ValueError("dow_layers_signals: expected a DataFrame")
    missing = [column for column in _REQUIRED if column not in signals.columns]
    if missing:
        raise ValueError(f"dow_layers_signals: missing columns {missing}")


def _finite(value: object) -> bool:
    return isinstance(value, (int, float, np.integer, np.floating)) and math.isfinite(
        float(value)
    )


def layer_positions(signals: pd.DataFrame) -> pd.DataFrame:
    """Per-bar unit positions for every layer and reading, aligned to ``signals``."""
    _check(signals)
    rows: list[dict[str, int]] = []
    previous = {column: 0 for column in LAYER_COLUMNS}
    latched = 0
    prior_latched = 0
    for row in signals.itertuples(index=False):
        if bool(row.no_trade):
            rows.append(dict(previous))
            continue
        trend = _TREND_DIRECTION[str(row.trend)]
        valid = bool(row.turning_valid)
        if trend == 1:
            has_prior = _finite(row.last_down_low_1)
        elif trend == -1:
            has_prior = _finite(row.last_up_high_1)
        else:
            has_prior = False
        stand_down = trend if valid else 0
        reversal = trend if (valid or not has_prior) else -trend

        dow_up = (
            _finite(row.last_down_low_1)
            and _finite(row.last_down_low_2)
            and float(row.last_down_low_1) > float(row.last_down_low_2)
        )
        dow_down = (
            _finite(row.last_up_high_1)
            and _finite(row.last_up_high_2)
            and float(row.last_up_high_1) < float(row.last_up_high_2)
        )
        opposite = (reversal == 1 and dow_down) or (reversal == -1 and dow_up)
        resonance = (reversal == 1 and dow_up) or (reversal == -1 and dow_down)
        opposite_flat = 0 if opposite else reversal

        if latched != reversal or opposite:
            latched = 0
        if latched == 0 and reversal != 0 and resonance and bool(row.close_breakout):
            latched = reversal

        # 研报公式块定义了上一同向段的极值（lastmax_1 / lastmin_1）却没在条件里用到；
        # 图 13 标的也是「第一高点」与「临时高点」。这条读法把入场参照换成它。
        close = float(row.signal_close)
        if stand_down == 1:
            stand_resonance = dow_up
            prior_cleared = _finite(row.last_up_high_1) and close >= float(
                row.last_up_high_1
            )
        elif stand_down == -1:
            stand_resonance = dow_down
            prior_cleared = _finite(row.last_down_low_1) and close <= float(
                row.last_down_low_1
            )
        else:
            stand_resonance = False
            prior_cleared = False
        if prior_latched != stand_down:
            prior_latched = 0
        if prior_latched == 0 and stand_down != 0 and stand_resonance and prior_cleared:
            prior_latched = stand_down

        current = {
            "l1_trend": trend,
            "l2_stand_down": stand_down,
            "l2_reversal": reversal,
            "l3_registered": _STATE_DIRECTION[str(row.state_position)],
            "l3_resonance_hold": reversal if resonance else 0,
            "l3_opposite_flat": opposite_flat,
            "l3_breakout_latched": latched,
            "l3_prior_high_breakout": prior_latched,
        }
        rows.append(current)
        previous = current
    return pd.DataFrame(rows, columns=list(LAYER_COLUMNS), index=signals.index)


#: 附录二海龟法：1 单位 ATR 对应总资金 0.5%，截到 4 倍。
ATR_CAPITAL_FRACTION = 0.005
MAX_LEVERAGE = 4.0


def atr_magnitudes(signals: pd.DataFrame) -> pd.Series:
    """The paper's ATR leverage per bar, ``0.005 * close / ATR`` capped at four."""
    for column in ("raw_close", "atr_raw"):
        if column not in signals.columns:
            raise ValueError(f"dow_layers_signals: missing columns ['{column}']")
    close = pd.to_numeric(signals["raw_close"], errors="coerce").to_numpy(
        dtype="float64"
    )
    atr = pd.to_numeric(signals["atr_raw"], errors="coerce").to_numpy(dtype="float64")
    usable = np.isfinite(atr) & (atr > 0.0) & np.isfinite(close)
    out = np.zeros(len(signals))
    out[usable] = np.minimum(
        ATR_CAPITAL_FRACTION * close[usable] / atr[usable], MAX_LEVERAGE
    )
    return pd.Series(out, index=signals.index, name="atr_magnitude")


def sized_positions(direction: pd.Series, magnitude: pd.Series) -> pd.Series:
    """Signed exposure: the magnitude is fixed on the bar the direction changes.

    That is how the shadow ledger sizes -- a target is set when the signal
    changes and held until the next change -- so a layer sized this way is
    comparable to the ledger rather than rebalanced on every bar.
    """
    if len(direction) != len(magnitude):
        raise ValueError("dow_layers_sizing: direction and magnitude must align")
    signs = direction.to_numpy(dtype="float64")
    sizes = magnitude.to_numpy(dtype="float64")
    out = np.zeros(len(signs))
    held = 0.0
    previous = 0.0
    for index in range(len(signs)):
        if signs[index] != previous:
            held = sizes[index] if signs[index] else 0.0
            previous = signs[index]
        out[index] = signs[index] * held
    return pd.Series(out, index=direction.index, name=direction.name)


def bar_returns(signals: pd.DataFrame) -> pd.Series:
    """Close-to-close return of each traded bar on the back-adjusted price.

    Untraded bars have no return, and the first traded bar of every
    continuity segment has none either -- the two sides of a break are
    different contracts that never traded together.
    """
    _check(signals)
    close = pd.to_numeric(signals["signal_close"], errors="coerce").to_numpy(
        dtype="float64"
    )
    traded = ~signals["no_trade"].to_numpy(dtype=bool)
    segment = signals["continuity_segment"].to_numpy()
    out = np.full(len(signals), np.nan)
    last_close = math.nan
    last_segment: object = None
    for index in range(len(signals)):
        if not traded[index]:
            continue
        if last_segment is not None and segment[index] == last_segment:
            out[index] = close[index] / last_close - 1.0
        last_close = close[index]
        last_segment = segment[index]
    return pd.Series(out, index=signals.index, name="bar_return")


def layer_daily_returns(
    signals: pd.DataFrame,
    positions: pd.Series,
    *,
    returns: pd.Series,
    cost_bps: float,
) -> pd.Series:
    """Daily return of holding ``positions`` bar to bar, charged per unit turnover.

    A bar earns the position held at the end of the previous bar times its
    own close-to-close return; the turnover into the bar's new position pays
    ``cost_bps`` of the traded notional. Bars compound within a trade date.
    """
    _check(signals)
    if len(positions) != len(signals) or len(returns) != len(signals):
        raise ValueError(
            "dow_layers_alignment: positions and returns must match signals"
        )
    if not (cost_bps >= 0.0):
        raise ValueError("dow_layers_cost: expected non-negative cost")
    rate = float(cost_bps) / 10_000.0
    held = positions.to_numpy(dtype="float64")
    previous = np.concatenate(([0.0], held[:-1]))
    bar = returns.to_numpy(dtype="float64")
    bar = np.where(np.isfinite(bar), bar, 0.0)
    factor = (1.0 + previous * bar) * (1.0 - np.abs(held - previous) * rate)
    frame = pd.DataFrame(
        {"trade_date": signals["trade_date"].to_numpy(), "factor": factor}
    )
    daily = frame.groupby("trade_date", sort=True)["factor"].prod() - 1.0
    daily.name = "net_return"
    return daily


def _period_row(returns: pd.Series, *, annualize_return: bool) -> dict[str, float]:
    equity = (1.0 + returns).cumprod()
    total = float(equity.iloc[-1]) - 1.0
    if annualize_return:
        total = float(equity.iloc[-1]) ** (TRADING_DAYS_PER_YEAR / len(returns)) - 1.0
    volatility = float(returns.std(ddof=1)) * math.sqrt(TRADING_DAYS_PER_YEAR)
    drawdown = float((1.0 - equity / equity.cummax()).max())
    month_keys = np.array([f"{d.year}-{d.month:02d}" for d in returns.index])
    months = returns.groupby(month_keys).apply(lambda r: (1.0 + r).prod() - 1.0)
    return {
        "return": total,
        "max_drawdown": drawdown,
        "sharpe": total / volatility if volatility > 0.0 else math.nan,
        "volatility": volatility,
        "calmar": total / drawdown if drawdown > 0.0 else math.nan,
        "monthly_win_rate": float((months > 0.0).mean()),
        "trading_days": int(len(returns)),
    }


def paper_annual_table(daily: pd.Series) -> pd.DataFrame:
    """Per-year rows plus a full-sample row, in the paper's conventions.

    Yearly return is the simple compounded return of the calendar year (a
    partial year is not annualized, as in the paper's 2022 row); Sharpe is
    that return over annualized volatility; Calmar is that return over the
    year's own maximum drawdown. The full-sample row annualizes as a CAGR.
    """
    if not isinstance(daily, pd.Series) or daily.empty:
        raise ValueError("dow_layers_daily: expected a non-empty Series")
    rows: dict[str, dict[str, float]] = {}
    years = pd.Index([d.year for d in daily.index])
    for year in sorted(set(years)):
        rows[str(year)] = _period_row(daily[years == year], annualize_return=False)
    rows["full_sample"] = _period_row(daily, annualize_return=True)
    return pd.DataFrame.from_dict(rows, orient="index")


def layer_report(
    signals: pd.DataFrame,
    *,
    shadow_daily: pd.DataFrame,
    start: date,
    end: date,
    cost_bps: float,
    sizing: str = "unit",
) -> dict[str, pd.DataFrame]:
    """Annual tables for every layer, plus the shadow's own ledger, in one window.

    Positions and returns are built on the whole frame so the window starts
    warm; only the daily series is cut to ``[start, end]``. The shadow row is
    the registered product ledger (ATR leverage, real five-minute fills) --
    the same product-level series the portfolio consumes -- so the gap between
    it and ``l3_registered`` is the price of execution, not of reading.
    """
    if sizing not in ("unit", "atr"):
        raise ValueError(f"dow_layers_sizing: expected 'unit' or 'atr'; got {sizing!r}")
    positions = layer_positions(signals)
    returns = bar_returns(signals)
    magnitude = atr_magnitudes(signals) if sizing == "atr" else None
    tables: dict[str, pd.DataFrame] = {}
    for column in LAYER_COLUMNS:
        exposure = positions[column]
        if magnitude is not None:
            exposure = sized_positions(exposure, magnitude)
        daily = layer_daily_returns(
            signals, exposure, returns=returns, cost_bps=cost_bps
        )
        tables[column] = paper_annual_table(_window(daily, start, end))
    ledger = shadow_daily.set_index("trade_date")["net_return"].astype("float64")
    ledger.index = [_as_date(item) for item in ledger.index]
    tables["shadow_registered"] = paper_annual_table(_window(ledger, start, end))
    return tables


def _as_date(value: object) -> date:
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _window(daily: pd.Series, start: date, end: date) -> pd.Series:
    index = pd.Index([_as_date(item) for item in daily.index])
    keep = (index >= start) & (index <= end)
    out = daily[keep.tolist()]
    out.index = index[keep]
    if out.empty:
        raise ValueError("dow_layers_window: no trading days inside the window")
    return out
