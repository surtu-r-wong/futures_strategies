from __future__ import annotations

from datetime import date
from decimal import Decimal
import sys

import pandas as pd
import pytest

import cta_gtja.data_quality as data_quality
from cta_gtja.data_quality import (
    build_adjustment_audit,
    format_health_summary,
    strict_failure_reasons,
    summarize_adjustment_quality,
    summarize_continuous_contract_quality,
    summarize_continuous_health,
)


def _row(symbol, raw, ba, fa):
    return {
        "base_symbol": symbol,
        "open_raw": raw, "open_ba": ba, "open_fa": fa,
        "close_raw": raw, "close_ba": ba, "close_fa": fa,
    }


def _health_row(
    symbol: str,
    trade_date: str,
    *,
    raw=10,
    ba=11,
    fa=12,
    daily_return=0.01,
    return_index=100,
):
    row = {
        "base_symbol": symbol,
        "trade_date": trade_date,
        "daily_return": daily_return,
        "return_index": return_index,
    }
    for lineage, value in (("raw", raw), ("ba", ba), ("fa", fa)):
        for field in ("open", "high", "low", "close"):
            row[f"{field}_{lineage}"] = Decimal(str(value))
    return row


def _strict_report(**overrides) -> pd.DataFrame:
    row = {
        "base_symbol": "BR",
        "n_rows": 1,
        "raw_nonpos": 0,
        "ba_nonpos": 0,
        "fa_nonpos": 0,
        "recommended_adj": "fa",
        "status": "ok",
        "raw_ohlc_nonpos": 0,
        "raw_ohlc_incomplete": 0,
        "raw_ohlc_infinite": 0,
        "ba_ohlc_nonpos": 0,
        "ba_ohlc_incomplete": 0,
        "ba_ohlc_infinite": 0,
        "fa_ohlc_nonpos": 0,
        "fa_ohlc_incomplete": 0,
        "fa_ohlc_infinite": 0,
        "suspicious_bar_count": 0,
        "daily_return_invalid": 0,
        "return_index_invalid": 0,
        "last_trade_date": date(2026, 7, 10),
        "lag_to_rule_max_days": 0,
        "missing_trade_dates": 0,
    }
    row.update(overrides)
    return pd.DataFrame([row])


def test_strict_policy_accepts_healthy_report_at_ten_day_boundary():
    reasons = strict_failure_reasons(
        _strict_report(),
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == []


def test_strict_policy_ignores_incomplete_gaps_and_symbol_lag():
    report = _strict_report(
        raw_ohlc_incomplete=783,
        ba_ohlc_incomplete=783,
        fa_ohlc_incomplete=783,
        lag_to_rule_max_days=9724,
        missing_trade_dates=100,
    )

    reasons = strict_failure_reasons(
        report,
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == []


def test_strict_policy_rejects_corruption_impossible_values_and_staleness():
    report = _strict_report(
        status="fa_corrupt",
        raw_ohlc_nonpos=1,
        fa_ohlc_infinite=1,
        suspicious_bar_count=1,
        daily_return_invalid=1,
        return_index_invalid=1,
    )

    reasons = strict_failure_reasons(
        report,
        as_of=date(2026, 7, 22),
        max_lag_days=10,
    )

    assert reasons == [
        "adjustment corruption: BR",
        "raw OHLC invalid: BR",
        "fa OHLC invalid: BR",
        "daily_return invalid (<= -1 or infinite): BR",
        "return_index invalid (<= 0 or infinite): BR",
        "table stale: last_trade_date=2026-07-10 lag_days=12 max_lag_days=10",
    ]


def test_strict_policy_rejects_empty_report():
    reasons = strict_failure_reasons(
        pd.DataFrame(),
        as_of=date(2026, 7, 20),
        max_lag_days=10,
    )

    assert reasons == ["no symbols returned"]


def test_summarize_flags_corrupt_adjustment_column_per_symbol():
    """Each symbol's adjustment lineages are classified independently; the
    report names which column is corrupt and which clean column to use.
    """
    df = pd.DataFrame([
        _row("A", 10, 12, -3),   # forward-adjusted corrupt -> use back-adjusted
        _row("A", 11, 13, 15),
        _row("B", 20, -1, 22),   # back-adjusted corrupt -> use forward-adjusted
        _row("C", 30, 31, 32),   # all clean
    ])

    rep = summarize_adjustment_quality(df).set_index("base_symbol")

    assert rep.loc["A", "fa_nonpos"] == 1
    assert rep.loc["A", "status"] == "fa_corrupt"
    assert rep.loc["A", "recommended_adj"] == "ba"

    assert rep.loc["B", "ba_nonpos"] == 1
    assert rep.loc["B", "status"] == "ba_corrupt"
    assert rep.loc["B", "recommended_adj"] == "fa"

    assert rep.loc["C", "status"] == "ok"
    assert rep.loc["C", "recommended_adj"] == "fa"


def test_summarize_falls_back_to_raw_when_both_adjustments_corrupt():
    df = pd.DataFrame([
        _row("D", 40, -2, -5),  # both adjusted lineages corrupt -> only raw usable
        _row("D", 41, 43, 45),
    ])

    rep = summarize_adjustment_quality(df).set_index("base_symbol")

    assert rep.loc["D", "status"] == "both_corrupt"
    assert rep.loc["D", "recommended_adj"] == "raw"


def test_adjustment_audit_excludes_raw_fallback_by_default():
    report = pd.DataFrame([
        {
            "base_symbol": "M",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 0,
            "fa_nonpos": 3,
            "status": "fa_corrupt",
            "recommended_adj": "ba",
        },
        {
            "base_symbol": "RU",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 2,
            "fa_nonpos": 4,
            "status": "both_corrupt",
            "recommended_adj": "raw",
        },
    ])

    audit = build_adjustment_audit(report, allow_raw_fallback=False).set_index("base_symbol")

    assert bool(audit.loc["M", "included"])
    assert audit.loc["M", "selected_adj"] == "ba"
    assert not bool(audit.loc["M", "raw_fallback"])
    assert not bool(audit.loc["RU", "included"])
    assert audit.loc["RU", "selected_adj"] == ""
    assert audit.loc["RU", "exclusion_reason"] == "both_adjusted_lineages_corrupt"


def test_adjustment_audit_allows_explicit_raw_fallback():
    report = pd.DataFrame([
        {
            "base_symbol": "RU",
            "n_rows": 10,
            "raw_nonpos": 0,
            "ba_nonpos": 2,
            "fa_nonpos": 4,
            "status": "both_corrupt",
            "recommended_adj": "raw",
        },
    ])

    audit = build_adjustment_audit(report, allow_raw_fallback=True).set_index("base_symbol")

    assert bool(audit.loc["RU", "included"])
    assert audit.loc["RU", "selected_adj"] == "raw"
    assert bool(audit.loc["RU", "raw_fallback"])
    assert audit.loc["RU", "exclusion_reason"] == ""


def test_full_ohlc_health_separates_incomplete_infinite_and_suspicious():
    zero_high = _health_row(
        "BR", "2026-01-07", daily_return=Decimal("-1.012815"),
        return_index=Decimal("-13.9470"),
    )
    zero_high["high_raw"] = Decimal("0")

    incomplete_and_infinite = _health_row(
        "BR", "2026-01-08", daily_return=None, return_index=Decimal("100"),
    )
    incomplete_and_infinite["low_raw"] = None
    incomplete_and_infinite["high_ba"] = Decimal("Infinity")

    health = summarize_continuous_health(
        pd.DataFrame([zero_high, incomplete_and_infinite])
    ).set_index("base_symbol")

    assert health.loc["BR", "raw_ohlc_nonpos"] == 1
    assert health.loc["BR", "raw_ohlc_incomplete"] == 1
    assert health.loc["BR", "raw_ohlc_infinite"] == 0
    assert health.loc["BR", "ba_ohlc_infinite"] == 1
    assert health.loc["BR", "suspicious_bar_count"] == 2
    assert health.loc["BR", "daily_return_invalid"] == 1
    assert health.loc["BR", "return_index_invalid"] == 1


def test_continuous_health_reports_lifetime_gaps_and_symbol_lag():
    rows = [
        _health_row("A", "2026-01-02"),
        _health_row("A", "2026-01-04"),
        _health_row("B", "2026-01-01"),
        _health_row("B", "2026-01-02"),
        _health_row("B", "2026-01-03"),
    ]

    health = summarize_continuous_health(pd.DataFrame(rows)).set_index("base_symbol")

    assert health.loc["A", "first_trade_date"] == date(2026, 1, 2)
    assert health.loc["A", "last_trade_date"] == date(2026, 1, 4)
    assert health.loc["A", "missing_trade_dates"] == 1
    assert health.loc["A", "lag_to_rule_max_days"] == 0
    assert health.loc["B", "missing_trade_dates"] == 0
    assert health.loc["B", "lag_to_rule_max_days"] == 1


def test_combined_quality_keeps_legacy_status_when_only_low_is_zero():
    row = _health_row("BR", "2026-01-07")
    row["low_raw"] = Decimal("0")

    report = summarize_continuous_contract_quality(
        pd.DataFrame([row])
    ).set_index("base_symbol")

    assert report.loc["BR", "status"] == "ok"
    assert report.loc["BR", "raw_nonpos"] == 0
    assert report.loc["BR", "raw_ohlc_nonpos"] == 1
    assert report.loc["BR", "suspicious_bar_count"] == 1


def test_main_default_reports_suspicious_bars_without_exiting(
    monkeypatch, capsys
):
    report = _strict_report(
        raw_ohlc_nonpos=1,
        raw_ohlc_incomplete=5,
        suspicious_bar_count=1,
        missing_trade_dates=2,
    )
    monkeypatch.setattr(
        data_quality,
        "scan_continuous_contract_quality",
        lambda **kwargs: report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_quality", "--rule-type", "standard"],
    )

    data_quality.main()

    output = capsys.readouterr().out
    assert "suspicious source bars" in output
    assert "any lineage flags the whole trade date" in output
    assert "incomplete OHLC lineage-rows: 5" in output


def test_main_strict_prints_reasons_and_exits_one(monkeypatch, capsys):
    report = _strict_report(
        raw_ohlc_nonpos=1,
        suspicious_bar_count=1,
        last_trade_date=date.today(),
    )
    monkeypatch.setattr(
        data_quality,
        "scan_continuous_contract_quality",
        lambda **kwargs: report,
    )
    monkeypatch.setattr(
        sys,
        "argv",
        ["data_quality", "--rule-type", "standard", "--strict"],
    )

    with pytest.raises(SystemExit) as exc:
        data_quality.main()

    assert exc.value.code == 1
    output = capsys.readouterr().out
    assert "strict failures:" in output
    assert "raw OHLC invalid: BR" in output
