"""Turning the raw exchange capture into a product-day receipt series.

`exchange_data.warehouse_source_record` is an archive of what each exchange
published, one row per line of the source table, with the numbers left as the
strings the exchange wrote.  The office's own disposition says it is not a
stock series and must not be added up as one.  This is the layer that makes it
one, and every rule below is a reading of a shape seen in the archive rather
than a general convention.

The pull sums each named column server-side -- SHFE alone has 1.9M detail rows
-- so what arrives here is long: one row per product-day, source row class and
column name.  The policy is all on this side.
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from citic_index.exchange_receipts import UnknownProduct, normalise_records

COLUMNS = [
    "source_kind",
    "trade_date",
    "product_key",
    "row_kind",
    "metric",
    "value",
    "n_rows",
    "record_ordinal",
]
DAY = date(2026, 9, 18)


def _records(rows) -> pd.DataFrame:
    return pd.DataFrame(rows, columns=COLUMNS)


def test_czce_total_carries_both_receipts_and_the_forecast():
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "CF", "TOTAL", "仓单数量", 7780.0, 1, None),
                ("CZCE_TEXT", DAY, "CF", "TOTAL", "有效预报", 146.0, 1, None),
            ]
        )
    )
    row = frame.iloc[0]
    assert row["product"] == "CF"
    assert row["receipts"] == 7780.0
    assert row["forecast"] == 146.0


def test_czce_bonded_and_duty_paid_receipts_are_one_number():
    """MA writes 仓单数量(完税) and 仓单数量(保税) as separate columns."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "MA", "TOTAL", "仓单数量(完税)", 5865.0, 1, None),
                ("CZCE_TEXT", DAY, "MA", "TOTAL", "仓单数量(保税)", 120.0, 1, None),
            ]
        )
    )
    assert frame.iloc[0]["receipts"] == 5985.0


def test_czce_combined_total_is_a_derived_duplicate_and_is_dropped():
    """MA's COMBINED_TOTAL restates 完税+保税; adding it double counts."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "MA", "TOTAL", "仓单数量(完税)", 5865.0, 1, None),
                ("CZCE_TEXT", DAY, "MA", "COMBINED_TOTAL", "仓单数量(完税+保税)", 5865.0, 1, None),
            ]
        )
    )
    assert len(frame) == 1
    assert frame.iloc[0]["receipts"] == 5865.0


def test_czce_forecast_only_section_adds_to_the_same_product_day():
    """CJ carries a 厂库 forecast section beside its warehouse section, and
    the two are different institutions rather than the same number twice."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "CJ", "TOTAL", "预报数量", 40.0, 1, None),
                ("CZCE_TEXT", DAY, "CJ", "TOTAL", "仓单数量", 4733.0, 1, None),
                ("CZCE_TEXT", DAY, "CJ", "TOTAL", "有效预报", 11.0, 1, None),
            ]
        )
    )
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["receipts"] == 4733.0
    assert row["forecast"] == 51.0


def test_czce_detail_and_subtotal_rows_never_reach_the_series():
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "SR", "SOURCE_DETAIL", "仓单数量", 900.0, 30, None),
                ("CZCE_TEXT", DAY, "SR", "SUBTOTAL", "仓单数量", 900.0, 4, None),
                ("CZCE_TEXT", DAY, "SR", "TOTAL", "仓单数量", 28912.0, 1, None),
            ]
        )
    )
    assert len(frame) == 1
    assert frame.iloc[0]["receipts"] == 28912.0


def test_czce_retired_codes_become_the_code_the_prices_use():
    """ME/RO/ER/TC/WS/WT are the pre-2015 spellings of MA/OI/RI/ZC/WH/PM."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_LEGACY", date(2013, 6, 18), "ME", "TOTAL", "仓单数量", 100.0, 1, None),
                ("CZCE_LEGACY", date(2013, 6, 18), "RO", "TOTAL", "仓单数量", 200.0, 1, None),
            ]
        )
    )
    assert sorted(frame["product"]) == ["MA", "OI"]


def test_shfe_sums_the_warehouse_detail_rows():
    frame, _ = normalise_records(
        _records([("SHFE_DAILY", DAY, "铜", "SOURCE_ROW", "期货", 26655.0, 14, None)])
    )
    row = frame.iloc[0]
    assert row["product"] == "CU"
    assert row["receipts"] == 26655.0
    assert row["forecast"] == 0.0


def test_shfe_warehouse_and_factory_sections_are_one_product():
    """石油沥青(仓库) and 石油沥青(厂库) are two tables for BU, and the older
    captures spell the same split without brackets."""
    frame, _ = normalise_records(
        _records(
            [
                ("SHFE_DAILY", DAY, "石油沥青(仓库)", "SOURCE_ROW", "期货", 1000.0, 5, None),
                ("SHFE_DAILY", DAY, "石油沥青(厂库)", "SOURCE_ROW", "期货", 200.0, 2, None),
                ("SHFE_DAILY", date(2015, 6, 16), "沥青仓库", "SOURCE_ROW", "期货", 700.0, 3, None),
                ("SHFE_DAILY", date(2015, 6, 16), "沥青厂库", "SOURCE_ROW", "期货", 300.0, 1, None),
            ]
        )
    )
    assert list(frame["product"]) == ["BU", "BU"]
    # sorted by product then date: the 2015 pair first, then the 2026 pair
    assert list(frame["receipts"]) == [1000.0, 1200.0]


def test_shfe_international_copper_is_not_shanghai_copper():
    """铜(BC) is the INE contract; the bracket is not a warehouse suffix."""
    frame, _ = normalise_records(
        _records(
            [
                ("SHFE_DAILY", DAY, "铜", "SOURCE_ROW", "期货", 26655.0, 14, None),
                ("SHFE_DAILY", DAY, "铜(BC)", "SOURCE_ROW", "期货", 5000.0, 3, None),
            ]
        )
    )
    assert sorted(frame["product"]) == ["BC", "CU"]


def test_gold_has_no_total_row_and_still_arrives():
    """黄金 sits in a single warehouse row, so the exchange prints no 总计 for
    it.  A rule that reads only totals drops the product silently."""
    frame, _ = normalise_records(
        _records([("SHFE_DAILY", DAY, "黄金", "SOURCE_ROW", "期货", 1500.0, 1, None)])
    )
    assert frame.iloc[0]["product"] == "AU"
    assert frame.iloc[0]["receipts"] == 1500.0


def test_an_unmapped_shfe_label_is_raised_not_dropped():
    """A product silently missing from the cross-section is the failure mode
    that looks like a result."""
    with pytest.raises(UnknownProduct, match="新品种"):
        normalise_records(
            _records([("SHFE_DAILY", DAY, "新品种", "SOURCE_ROW", "期货", 10.0, 1, None)])
        )


def test_dce_summary_is_the_product_total():
    frame, _ = normalise_records(
        _records(
            [
                ("DCE_WARRANTS", DAY, "a", "SUMMARY", "current_quantity", 74629.0, 1, None),
                ("DCE_WARRANTS", DAY, "a", "SOURCE_DETAIL", "current_quantity", 74629.0, 9, None),
            ]
        )
    )
    assert len(frame) == 1
    row = frame.iloc[0]
    assert row["product"] == "A"
    assert row["receipts"] == 74629.0
    assert row["forecast"] == 0.0


def test_gfex_grand_total_row_has_no_product_and_is_dropped():
    frame, _ = normalise_records(
        _records(
            [
                ("GFEX_WARRANTS", DAY, "si", "SUMMARY", "current_quantity", 109784.0, 1, None),
                ("GFEX_WARRANTS", DAY, "", "SUMMARY", "current_quantity", 200000.0, 1, None),
            ]
        )
    )
    assert list(frame["product"]) == ["SI"]


def test_a_blank_number_is_not_a_zero():
    """An empty cell means the exchange printed nothing and the day has no
    total; a zero means it printed a zero.  `receipts.fill_absent_zeros`
    decides which absences become zeros, and it cannot do that if this layer
    has already invented one."""
    frame, report = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "PF", "TOTAL", "仓单数量", None, 0, None),
                ("CZCE_TEXT", DAY, "SA", "TOTAL", "仓单数量", 0.0, 1, None),
                ("CZCE_TEXT", DAY, "SA", "TOTAL", "有效预报", 1823.0, 1, None),
            ]
        )
    )
    assert list(frame["product"]) == ["SA"]
    assert frame.iloc[0]["receipts"] == 0.0
    assert report["product_days_without_a_number"] == 1


def test_a_forecast_without_receipts_is_still_absent_receipts():
    """AP on a quiet day prints only its forecast section.  The product has no
    receipt total that day, and a zero must not be invented for it."""
    frame, _ = normalise_records(
        _records([("CZCE_TEXT", DAY, "AP", "TOTAL", "预报数量", 0.0, 1, None)])
    )
    assert frame.empty


def test_the_premium_captures_are_not_receipts():
    frame, _ = normalise_records(
        _records(
            [
                ("DCE_AGIO", DAY, "a", "SOURCE_QUOTATION", "premium", 30.0, 1, None),
                ("SHFE_PREMIUM", DAY, "铜", "SOURCE_QUOTATION", "premium", 30.0, 1, None),
            ]
        )
    )
    assert frame.empty


def test_weekly_shfe_is_not_mixed_into_the_daily_series():
    """SHFE_WEEKLY restates the same warrants once a week; taking both would
    double every Friday."""
    frame, _ = normalise_records(
        _records(
            [
                ("SHFE_DAILY", DAY, "铜", "SOURCE_ROW", "期货", 26655.0, 14, None),
                ("SHFE_WEEKLY", DAY, "铜", "SOURCE_ROW", "期货", 26655.0, 14, None),
            ]
        )
    )
    assert len(frame) == 1
    assert frame.iloc[0]["receipts"] == 26655.0


def test_a_grand_total_beside_its_sections_is_not_added_to_them():
    """PF spent 2021-04..09 printing one 总计 per brand table and then a final
    总计 over them, all parsed as TOTAL.  Summing the lot doubles the product:
    Wind reads 2100 where the three rows are 200, 1900 and 2100.

    The rule is conditional on the arithmetic, because the rows carry no
    structural difference -- same 总计 cell, same section position, same
    label.  Across the whole CZCE archive every product-day with several
    same-metric totals is this shape, and the last row is the grand total in
    every one of them."""
    frame, report = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "PF", "TOTAL", "仓单数量", 200.0, 1, 839),
                ("CZCE_TEXT", DAY, "PF", "TOTAL", "仓单数量", 1900.0, 1, 846),
                ("CZCE_TEXT", DAY, "PF", "TOTAL", "仓单数量", 2100.0, 1, 860),
            ]
        )
    )
    assert frame.iloc[0]["receipts"] == 2100.0
    assert report["grand_totals_collapsed"] == 1


def test_sections_that_do_not_add_up_to_the_last_are_all_kept():
    """Independent sections are the ordinary case and must still be summed."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "SR", "TOTAL", "仓单数量", 100.0, 1, 10),
                ("CZCE_TEXT", DAY, "SR", "TOTAL", "仓单数量", 250.0, 1, 20),
            ]
        )
    )
    assert frame.iloc[0]["receipts"] == 350.0


def test_the_rule_is_per_column_not_per_product_day():
    """MA writes 完税 and 保税 in one row; they add, and neither is a total
    of the other even when one happens to equal the other."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", DAY, "MA", "TOTAL", "仓单数量(完税)", 500.0, 1, 900),
                ("CZCE_TEXT", DAY, "MA", "TOTAL", "仓单数量(保税)", 500.0, 1, 900),
            ]
        )
    )
    assert frame.iloc[0]["receipts"] == 1000.0


def test_pta_is_the_same_product_as_ta():
    """CZCE wrote TA until 2022-11-25 and PTA from 2022-11-28.  Left apart,
    TA's series stops dead in 2022 and a phantom product appears beside it --
    and the factor divides today's receipts by a 200-day-old baseline, so a
    break inside one product's own series is the one thing that does move a
    rank."""
    frame, _ = normalise_records(
        _records(
            [
                ("CZCE_TEXT", date(2022, 11, 25), "TA", "TOTAL", "仓单数量", 100.0, 1, None),
                ("CZCE_TEXT", date(2022, 11, 28), "PTA", "TOTAL", "仓单数量", 110.0, 1, None),
            ]
        )
    )
    assert list(frame["product"]) == ["TA", "TA"]


# --- the switch that lets one bundle carry two measurements ------------------

def _bundle(tmp_path, name, rows):
    path = tmp_path / name
    pd.DataFrame(rows, columns=["product", "trade_date", "receipts", "forecast"]).to_csv(
        path, index=False
    )
    return path


def test_load_receipts_can_read_the_exchange_file_beside_the_wind_one(tmp_path):
    """The two files are two measurements of the same quantity, so they live
    side by side in one bundle and the run says which it read."""
    from citic_index.__main__ import load_receipts

    _bundle(tmp_path, "receipts_exchange.csv", [("SR", date(2024, 1, 2), 100.0, 7.0)])
    frame = load_receipts(
        tmp_path,
        calendar=[date(2024, 1, 2)],
        through=date(2024, 1, 2),
        filename="receipts_exchange.csv",
    )
    assert float(frame.iloc[0]["receipts"]) == 100.0


def test_the_forecast_is_added_only_when_asked_for(tmp_path):
    from citic_index.__main__ import load_receipts

    _bundle(tmp_path, "receipts_exchange.csv", [("SR", date(2024, 1, 2), 100.0, 7.0)])
    kw = dict(calendar=[date(2024, 1, 2)], through=date(2024, 1, 2),
              filename="receipts_exchange.csv")
    off = load_receipts(tmp_path, **kw)
    on = load_receipts(tmp_path, include_forecast=True, **kw)
    assert float(off.iloc[0]["receipts"]) == 100.0
    assert float(on.iloc[0]["receipts"]) == 107.0


def test_asking_for_the_forecast_of_a_file_that_has_none_is_refused(tmp_path):
    """The Wind file carries warrants only.  Silently reading its receipts as
    though the forecast were in them would make the two arms identical and
    look like a finding of no effect."""
    from citic_index.__main__ import load_receipts

    path = tmp_path / "receipts.csv"
    pd.DataFrame(
        [("SR", date(2024, 1, 2), 100.0)], columns=["product", "trade_date", "receipts"]
    ).to_csv(path, index=False)
    with pytest.raises(SystemExit, match="forecast"):
        load_receipts(
            tmp_path,
            calendar=[date(2024, 1, 2)],
            through=date(2024, 1, 2),
            include_forecast=True,
        )
