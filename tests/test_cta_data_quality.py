from __future__ import annotations

import pandas as pd

from cta_gtja.data_quality import build_adjustment_audit, summarize_adjustment_quality


def _row(symbol, raw, ba, fa):
    return {
        "base_symbol": symbol,
        "open_raw": raw, "open_ba": ba, "open_fa": fa,
        "close_raw": raw, "close_ba": ba, "close_fa": fa,
    }


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

