"""Reproducible Alpha #013 pilot on cached commodity contract daily bars.

Run from the repository root with PYTHONPATH=. and bounded process memory.
This is a daily, fractional-notional research simulation, not an order engine.
"""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd

from cta_gtja.pg_source import FINANCIAL_FUTURES


def alpha013(close: pd.DataFrame, volume: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Cross-sectional percentile ranks, five-session sample covariance, -rank.

    Explicit centering preserves exact zero for constant price ranks. Ordinary
    rolling covariance subtraction can create tiny residuals which rank then
    amplifies into spurious signals. Never forward-fill absent sessions.
    """
    valid = np.isfinite(close) & np.isfinite(volume) & (close > 0) & (volume > 0)
    x = close.where(valid).rank(axis=1, method="average", pct=True)
    y = volume.where(valid).rank(axis=1, method="average", pct=True)
    xs = np.stack([x.shift(k).to_numpy() for k in range(5)])
    ys = np.stack([y.shift(k).to_numpy() for k in range(5)])
    cov = ((xs - xs.mean(axis=0)) * (ys - ys.mean(axis=0))).sum(axis=0) / 4
    cov[np.abs(cov) < 1e-14] = 0.0
    covariance = pd.DataFrame(cov, index=close.index, columns=close.columns)
    return -covariance.rank(axis=1, method="average", pct=True), covariance


def quantile_weights(scores: pd.DataFrame, bins: int = 5) -> list[pd.DataFrame]:
    """Split tied blocks fractionally across bins; never rank ties by symbol.

    Bin 1 is lowest score; bin 5 is highest. Each nonempty bin has gross 1.
    A constant cross-section gives identical bins and zero long-short exposure.
    """
    lo = scores.rank(axis=1, method="min") - 1
    hi = scores.rank(axis=1, method="max")
    n = scores.notna().sum(axis=1)
    result = []
    for q in range(bins):
        left = lo.clip(lower=n * q / bins, axis=0)
        right = hi.clip(upper=n * (q + 1) / bins, axis=0)
        fraction = (right - left).clip(lower=0) / (hi - lo)
        result.append(fraction.div(n / bins, axis=0).fillna(0.0))
    return result


def hac_t(values: pd.Series, lag: int) -> float:
    """Bartlett/Newey-West t for a mean; retain the session grid through gaps."""
    a = values.to_numpy(dtype=float)
    good = np.isfinite(a)
    n = int(good.sum())
    if n < 3:
        return float("nan")
    e = np.where(good, a - np.nanmean(a), 0.0)
    var_sum = float(e @ e)
    for k in range(1, min(lag + 1, len(e))):
        var_sum += 2 * (1 - k / (lag + 1)) * float(e[k:] @ e[:-k])
    return float(np.nanmean(a) * n / np.sqrt(var_sum)) if var_sum > 0 else float("nan")


def load_panel(path: Path) -> tuple[pd.DataFrame, pd.DataFrame, dict]:
    bars = pd.read_csv(path, parse_dates=["trade_date"])
    bars["product"] = bars.contract.str.extract(r"^([A-Za-z]+)")[0].str.upper()
    bars = bars.loc[~bars["product"].isin(FINANCIAL_FUTURES)].copy()
    # Keep ordinary month contracts, consistent with the production parser.
    tas = ~bars.contract.str.match(r"^[A-Za-z]+\d{4}\.[A-Za-z]+$")
    special_rows = int(tas.sum())
    input_rows = len(bars)
    bars = bars.loc[~tas].copy()
    if bars.duplicated(["trade_date", "contract"]).any():
        raise ValueError("Duplicate contract-day bars")
    cols = ["open", "high", "low", "close", "volume", "oi", "turnover"]
    numeric = np.isfinite(bars[cols]).all(axis=1)
    valid = (numeric & bars[["open", "high", "low", "close"]].gt(0).all(axis=1)
             & bars[["volume", "oi", "turnover"]].ge(0).all(axis=1)
             & (bars.low <= bars[["open", "close"]].min(axis=1))
             & (bars.high >= bars[["open", "close"]].max(axis=1)))
    audit = {"input_rows": input_rows, "excluded_nonstandard_contract_rows": special_rows,
             "invalid_bars": int((~valid).sum())}
    clean = bars.loc[valid].copy()
    calendar = pd.DatetimeIndex(sorted(bars.trade_date.unique()), name="trade_date")
    # Selection uses only information known at signal-day close. Retain native
    # quotes for ranking: per-product backward-adjustment constants change ranks.
    candidates = clean.loc[(clean.volume > 0) & (clean.oi > 0)].copy()
    # Exclude delivery-month contracts using metadata known on the signal date.
    # This cache uses four-digit YYMM symbols (validated, never guessed).
    parts = candidates.contract.str.extract(r"^[A-Za-z]+(\d{2})(\d{2})\.[A-Za-z]+$")
    if parts.isna().any().any():
        raise ValueError("Pilot requires unambiguous four-digit YYMM contract codes")
    delivery = 200000 + parts[0].astype(int) * 100 + parts[1].astype(int)
    candidates = candidates.loc[delivery > candidates.trade_date.dt.year * 100
                                + candidates.trade_date.dt.month]
    dominant = (candidates.sort_values(["trade_date", "product", "oi", "volume", "contract"],
                                      ascending=[True, True, False, False, True])
                .drop_duplicates(["trade_date", "product"]))
    # Lagged product aggregate turnover; at least 60 complete global sessions.
    turnover_bars = bars.loc[np.isfinite(bars.turnover) & (bars.turnover >= 0)]
    turnover = (turnover_bars.groupby(["trade_date", "product"]).turnover.sum()
                .unstack().reindex(calendar))
    eligible = turnover.rolling(60, min_periods=60).mean().shift(1) >= 1e8
    panel = dominant.set_index(["trade_date", "product"]).sort_index()
    panel["eligible"] = eligible.stack().reindex(panel.index).fillna(False)
    audit.update(products=sorted(clean["product"].unique()),
                 source_start=str(calendar.min().date()), source_end=str(calendar.max().date()),
                 eligible_product_days=int(panel.eligible.sum()))
    return panel, clean, audit


def forward_returns(panel: pd.DataFrame, bars: pd.DataFrame, horizon: int) -> pd.DataFrame:
    """At close(t), choose contract; buy its open(t+1), sell SAME open(t+1+h).

    Date offsets use the global session grid, never each contract's next row.
    Absent prices remain missing, so missing trading days cannot be bridged.
    """
    calendar = pd.DatetimeIndex(sorted(bars.trade_date.unique()))
    dates = pd.Series(calendar, index=calendar)
    signal_dates = panel.index.get_level_values("trade_date")
    entry_dates = signal_dates.map(dates.shift(-1))
    exit_dates = signal_dates.map(dates.shift(-1 - horizon))
    quotes = bars.loc[bars.volume > 0].set_index(["trade_date", "contract"])["open"]
    entry = quotes.reindex(pd.MultiIndex.from_arrays([entry_dates, panel.contract])).to_numpy()
    exit_px = quotes.reindex(pd.MultiIndex.from_arrays([exit_dates, panel.contract])).to_numpy()
    return pd.Series(exit_px / entry - 1, index=panel.index).unstack().reindex(calendar)


def performance(returns: pd.Series) -> dict:
    r = returns.dropna()
    if r.empty:
        return {"days": 0}
    equity = (1 + r).cumprod()
    peak = equity.cummax().clip(lower=1.0)
    vol = float(r.std(ddof=1) * np.sqrt(252))
    return {"days": len(r), "annual_return": float(equity.iloc[-1] ** (252 / len(r)) - 1),
            "annual_vol": vol, "sharpe": float(r.mean() * 252 / vol) if vol else None,
            "max_drawdown": float((equity / peak - 1).min())}



def simulate_opens(weights: pd.DataFrame, contracts: pd.DataFrame,
                   market: dict, calendar: pd.DatetimeIndex, cost_bps: float,
                   rebalance_every: int = 1, phase: int = 0) -> pd.DataFrame:
    """Close(t) targets execute at the next global open, with blocked fills.

    Unquoted holdings retain their last mark; the full gap is booked on the
    next quote. A blocked roll cannot open another contract of that product.
    New unquoted entries are skipped. Quoted opens are assumed fillable even
    on limit days, an explicit daily-data research approximation.
    """
    signal_for_open = dict(zip(calendar[1:], calendar[:-1]))
    weight_rows = weights.to_dict("index")
    contract_rows = contracts.to_dict("index")
    first = calendar.get_loc(weights.index[0]) + 1
    last = calendar.get_loc(weights.index[-1]) + 2
    positions, marks = {}, {}
    capital = 1.0
    rows = []
    for step, day in enumerate(calendar[first:last + 1]):
        quote = market.get(day, {})
        previous_capital = capital
        pnl = sum(q * (quote.get(c, marks[c]) - marks[c]) for c, q in positions.items())
        capital += pnl
        if capital <= 0:
            raise ValueError("Research account exhausted")
        blocked = {c for c in positions if c not in quote}
        blocked_products = {c.split(".")[0].rstrip("0123456789") for c in blocked}
        terminal = day == calendar[last]
        signal_day = signal_for_open[day]
        rebalance = step >= phase and (step - phase) % rebalance_every == 0
        target = {} if rebalance or terminal else dict(positions)
        skipped = 0
        if not terminal and rebalance and signal_day in weight_rows:
            for product, w in weight_rows[signal_day].items():
                if abs(w) <= 1e-14:
                    continue
                contract = contract_rows[signal_day][product]
                if contract not in quote or product in blocked_products:
                    skipped += 1
                    continue
                target[contract] = float(w * capital / quote[contract])
        if not terminal and not rebalance and signal_day in contract_rows:
            # Keep the alpha allocation until its next scheduled decision, but
            # maintain the currently selected month contract each session.
            for old, quantity in list(positions.items()):
                product = old.split(".")[0].rstrip("0123456789")
                new = contract_rows[signal_day][product]
                if (pd.notna(new) and new != old and old in quote and new in quote
                        and product not in blocked_products):
                    target.pop(old, None)
                    target[new] = target.get(new, 0) + quantity * quote[old] / quote[new]
        for c in blocked:
            target[c] = positions[c]
        turnover_cash = sum(abs(target.get(c, 0) - positions.get(c, 0)) * quote[c]
                            for c in target.keys() | positions.keys() if c in quote)
        cost_cash = turnover_cash * cost_bps / 10000
        capital -= cost_cash
        if capital <= 0:
            raise ValueError("Research account exhausted by fees")
        positions = {c: q for c, q in target.items() if abs(q) > 1e-16}
        marks = {c: quote[c] if c in quote else marks[c] for c in positions}
        if terminal and positions:
            raise ValueError(f"Terminal holdings cannot be liquidated: {sorted(positions)}")
        rows.append({"trade_date": day, "gross": pnl / previous_capital,
                     "turnover": turnover_cash / previous_capital,
                     "cost": cost_cash / previous_capital,
                     "net": capital / previous_capital - 1,
                     "gross_exposure": sum(abs(q * marks[c]) for c, q in positions.items()) / capital,
                     "unquoted_holdings": len(blocked), "skipped_targets": skipped})
    return pd.DataFrame(rows).set_index("trade_date")

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prices", type=Path, default=Path("data/citic_index/prices.csv"))
    parser.add_argument("--output", type=Path, default=Path("output/alpha013"))
    parser.add_argument("--start", default="2012-01-01")
    parser.add_argument("--reconciliation", type=Path, required=True)
    args = parser.parse_args()
    reconciliation = json.loads(args.reconciliation.read_text())
    if reconciliation["status"] != "matched":
        raise ValueError("Input cache must pass database row-count reconciliation")
    with args.prices.open("rb") as stream:
        input_hash = hashlib.file_digest(stream, "sha256").hexdigest()
    if reconciliation.get("input_sha256") != input_hash:
        raise ValueError("Reconciliation fingerprint does not match the price file")
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "reconciliation.json").write_text(json.dumps(reconciliation, indent=2))
    print("Loading contract daily bars", flush=True)
    panel, bars, audit = load_panel(args.prices)
    calendar = pd.DatetimeIndex(sorted(bars.trade_date.unique()), name="trade_date")
    close = panel.close.unstack().reindex(calendar)
    volume = panel.volume.unstack().reindex(calendar)
    eligible = panel.eligible.unstack().reindex(calendar).fillna(False)
    contracts = panel.contract.unstack().reindex(calendar)
    scores, covariance = alpha013(close.where(eligible), volume.where(eligible))
    # Diagnostic excludes the four transitions within the own-product window.
    changed = contracts.ne(contracts.shift())
    same_contract = changed.rolling(4, min_periods=4).sum().eq(0)
    variants = {"literal": scores, "no_recent_roll": scores.where(same_contract)}
    labels = {h: forward_returns(panel, bars, h).reindex_like(scores) for h in (1, 5, 20)}
    market = {day: dict(zip(part.contract, part.open))
              for day, part in bars.loc[bars.volume > 0].groupby("trade_date")}
    mask = (calendar >= pd.Timestamp(args.start)) & (calendar <= calendar[-3])
    daily_rows, ic_summary, metrics, series = [], [], [], {}
    for name, signal in variants.items():
        # No selection by future return availability; at least ten scores known
        # at decision time, otherwise the daily basket is flat.
        signal = signal.where(signal.notna().sum(axis=1) >= 10).loc[mask]
        bins = quantile_weights(signal)
        weights = (bins[-1] - bins[0]) * 0.5
        for h, label in labels.items():
            future = label.reindex_like(signal)
            paired = signal.notna() & np.isfinite(future)
            x = signal.where(paired).rank(axis=1)
            y = future.where(paired).rank(axis=1)
            ic = x.corrwith(y, axis=1).where(paired.sum(axis=1) >= 10)
            for period, pmask in (("full", np.ones(len(ic), dtype=bool)),
                                  ("2012_2020", ic.index < "2021-01-01"),
                                  ("2021_plus", ic.index >= "2021-01-01")):
                use = ic.loc[pmask]
                ic_summary.append({"variant": name, "horizon": h, "period": period,
                                   "days": int(use.notna().sum()), "mean_rank_ic": float(use.mean()),
                                   "hac_t": hac_t(use, max(5, h)),
                                   "missing_label_fraction": float((signal.notna() & ~paired).loc[pmask].sum().sum()
                                        / max(1, signal.notna().loc[pmask].sum().sum()))})
            daily_rows.append(pd.DataFrame({"variant": name, "horizon": h, "rank_ic": ic,
                                            "pairs": paired.sum(axis=1)}).rename_axis("trade_date").reset_index())
        for bps in (0, 1, 4, 8):
            daily = simulate_opens(weights, contracts, market, calendar, bps)
            series[f"{name}_{bps}bps"] = daily.net
            daily.to_csv(args.output / f"{name}_{bps}bps_daily.csv")
            for period, pmask in (("full", np.ones(len(daily), dtype=bool)),
                                  ("2012_2020", daily.index < "2021-01-01"),
                                  ("2021_plus", daily.index >= "2021-01-01")):
                use = daily.loc[pmask]
                metrics.append({"variant": name, "cost_bps": bps, "period": period,
                                **performance(use.net), "mean_turnover": float(use.turnover.mean()),
                                "mean_gross_exposure": float(use.gross_exposure.mean()),
                                "unquoted_holding_days": int(use.unquoted_holdings.sum()),
                                "skipped_targets": int(use.skipped_targets.sum())})
        # Gross quintile averages are diagnostics; mark an entire group/day
        # missing if any positive-weight member lacks a return.
        groups = {}
        for q, w in enumerate(bins, 1):
            ret = labels[1].reindex_like(w)
            groups[f"Q{q}"] = (w * ret).sum(axis=1).where(~((w > 0) & ret.isna()).any(axis=1))
        pd.DataFrame(groups).to_csv(args.output / f"{name}_groups_gross.csv")
        signal.to_csv(args.output / f"{name}_scores.csv")
        weights.to_csv(args.output / f"{name}_weights.csv")
    # The five schedules are all reported; no best-offset selection.
    weekly_metrics = []
    for phase in range(5):
        for bps in (0, 4):
            signal = scores.where(scores.notna().sum(axis=1) >= 10).loc[mask]
            bins = quantile_weights(signal)
            weights = (bins[-1] - bins[0]) * 0.5
            try:
                daily = simulate_opens(weights, contracts, market, calendar, bps,
                                       rebalance_every=5, phase=phase)
            except ValueError as exc:
                weekly_metrics.append({"phase": phase, "cost_bps": bps,
                                       "period": "full", "status": "failed", "reason": str(exc)})
                continue
            daily.to_csv(args.output / f"five_session_phase{phase}_{bps}bps_daily.csv")
            for period, pmask in (("full", np.ones(len(daily), dtype=bool)),
                                  ("2012_2020", daily.index < "2021-01-01"),
                                  ("2021_plus", daily.index >= "2021-01-01")):
                use = daily.loc[pmask]
                weekly_metrics.append({"phase": phase, "cost_bps": bps, "period": period, "status": "ok",
                                       **performance(use.net),
                                       "mean_turnover": float(use.turnover.mean())})
    pd.DataFrame(weekly_metrics).to_csv(args.output / "five_session_performance.csv", index=False)
    pd.DataFrame(metrics).to_csv(args.output / "performance.csv", index=False)
    pd.DataFrame(ic_summary).to_csv(args.output / "ic_summary.csv", index=False)
    pd.concat(daily_rows, ignore_index=True).to_csv(args.output / "ic_daily.csv", index=False)
    selected_cov = covariance.loc[mask]
    audit.update(zero_covariance_fraction=float(selected_cov.eq(0).sum().sum() / selected_cov.notna().sum().sum()),
                 median_unique_scores=float(scores.loc[mask].nunique(axis=1).median()),
                 median_eligible_products=float(eligible.loc[mask].sum(axis=1).median()),
                 signal_start=str(calendar[mask][0].date()), signal_end=str(calendar[mask][-1].date()),
                 last_exit=str(calendar[-1].date()), reconciliation=reconciliation,
                 input_sha256=input_hash,
                 formula="-rank(covariance(rank(close), rank(volume), 5))",
                 ranks="daily cross-sectional percentile, average ties",
                 price="raw close of highest-OI eligible contract, ties volume then code",
                 pool="cached 37 products; lagged 60-session mean aggregate turnover >= CNY 100 million",
                 execution="close(t) signal; same contract open(t+1) to open(t+2); daily rebalance",
                 unquoted_execution="hold last mark and defer exit; full gap booked on next quote; skip unquoted new entries",
                 limitations=["37-product ex-post selected cache, not full commodity universe",
                              "Raw cross-product prices depend on quotation units",
                              "Daily opens, fractional notional; no limits, margins, capacity or integer contracts",
                              "2021 split is retrospective, not a pristine out-of-sample test",
                              "No-recent-roll excludes own-product switches only; other ranks may still be affected"])
    (args.output / "audit.json").write_text(json.dumps(audit, indent=2, ensure_ascii=False))
    panel.reset_index().to_parquet(args.output / "dominants.parquet", index=False)
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    fig, axes = plt.subplots(2, 1, figsize=(12, 8))
    for name, r in series.items():
        if name.endswith(("_0bps", "_4bps")):
            axes[0].plot(r.index, (1 + r).cumprod(), label=name)
    axes[0].set_title("Alpha #013 commodity pilot | target gross exposure <= 1")
    axes[0].legend()
    ic_daily = pd.concat(daily_rows).query("horizon == 1")
    for name, part in ic_daily.groupby("variant"):
        axes[1].plot(part.trade_date, part.rank_ic.cumsum(), label=name)
    axes[1].set_title("Cumulative daily 1-session Rank IC")
    axes[1].legend()
    fig.tight_layout()
    fig.savefig(args.output / "overview.png", dpi=140)
    plt.close(fig)
    print(pd.DataFrame(metrics).query("cost_bps == 4").to_string(index=False))
    print(pd.DataFrame(ic_summary).to_string(index=False))
    print(json.dumps(audit, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
