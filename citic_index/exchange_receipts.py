"""The exchange's own warehouse receipts, as a product-day series.

`exchange_data.warehouse_source_*` is an archive of what the four commodity
exchanges published: one row per line of the source table, numbers kept as the
strings the exchange wrote, nothing normalised.  The office's disposition for
that load says so in as many words -- it "不能当已入库/可加总库存".  This module
is the layer that makes a stock series out of it, and the Wind series already
in `commodity_research` is the independent judge that says whether it worked.

The reason to bother: **only CZCE publishes 有效预报**, the tonnage that has
been declared for warranting but not yet warranted, and 023 ranks products on
how their receipts moved.  Whether that belongs in the number is an empirical
question, and it cannot even be asked off the Wind series, which carries the
warrants alone.

Four exchanges, four shapes, and each rule below is a reading of a shape seen
in the archive:

    CZCE   per-product sections, each closed by a `TOTAL` row.  Receipts sit
           in 仓单数量, or split across 仓单数量(完税)/(保税), or -- for the
           confirmation-certificate products -- 确认书数量.  The forecast is a
           column of the same table (有效预报 / 有效入库预报) and, for the
           products whose 厂库 declare separately, a section of its own whose
           total is 预报数量.  `COMBINED_TOTAL` restates 完税+保税 and is a
           derived duplicate: counting it doubles the product.
    SHFE   one table per product, sometimes split 仓库/厂库, summed from the
           warehouse rows rather than the 总计 row.  Not for elegance: 黄金
           occupies a single warehouse and the exchange prints no total for
           it, so a total-only rule drops the product without saying so.  The
           totals are pulled anyway and reconciled against the sum.
    DCE    a `SUMMARY` row per product, already the total.  No forecast.
    GFEX   the same, plus a grand-total row carrying no product code.

What arrives here is long and already summed per column, because SHFE alone
has 1.9M detail rows and the link to this database is a relay.  The policy --
which row class counts, which column means what, which product a label is --
is all on this side, where it can be tested.
"""

from __future__ import annotations

import pandas as pd

COLUMNS = ["product", "trade_date", "receipts", "forecast"]

# Which class of source row carries the product total for each capture.
TOTAL_ROW_KIND = {
    "CZCE_TEXT": "TOTAL",
    "CZCE_LEGACY": "TOTAL",
    "DCE_WARRANTS": "SUMMARY",
    "GFEX_WARRANTS": "SUMMARY",
    "SHFE_DAILY": "SOURCE_ROW",
}

# SHFE_WEEKLY restates the same warrants once a week and DCE_AGIO /
# SHFE_PREMIUM are premium quotations, not stock.  Named rather than inferred
# so that a new capture kind fails loudly instead of being read as receipts.
IGNORED_SOURCES = {"SHFE_WEEKLY", "DCE_AGIO", "SHFE_PREMIUM"}

RECEIPT_METRICS = {
    "仓单数量",
    "仓单数量(完税)",
    "仓单数量(保税)",
    "确认书数量",
    "current_quantity",  # DCE / GFEX
    "期货",  # SHFE
}

FORECAST_METRICS = {"有效预报", "有效入库预报", "预报数量"}

# CZCE renamed six products when it moved to the text report in 2015; the
# price side only ever uses the current spelling.
CZCE_ALIASES = {
    "ME": "MA",  # 甲醇
    "RO": "OI",  # 菜籽油
    "ER": "RI",  # 早籼稻
    "TC": "ZC",  # 动力煤
    "WS": "WH",  # 强麦
    "WT": "PM",  # 普麦
    "PTA": "TA",  # written TA through 2022-11-25 and PTA from 2022-11-28
}

# SHFE writes a Chinese name where the other three write a code.  Every label
# seen in the archive is listed; an unlisted one raises rather than vanishing.
SHFE_PRODUCTS = {
    "铜": "CU",
    "铜(BC)": "BC",  # the INE contract, not a warehouse suffix
    "铝": "AL",
    "锌": "ZN",
    "铅": "PB",
    "镍": "NI",
    "锡": "SN",
    "黄金": "AU",
    "白银": "AG",
    "螺纹钢": "RB",
    "线材": "WR",
    "热轧卷板": "HC",
    "不锈钢": "SS",
    "石油沥青": "BU",
    "沥青": "BU",  # the pre-2015 spelling
    "天然橡胶": "RU",
    "20号胶": "NR",
    "丁二烯橡胶": "BR",
    "纸浆": "SP",
    "燃料油": "FU",
    "低硫燃料油": "LU",
    "氧化铝": "AO",
    "铸造铝合金": "AD",
    "中质含硫原油": "SC",
}

_WAREHOUSE_SUFFIXES = ("(仓库)", "(厂库)", "（仓库）", "（厂库）", "仓库", "厂库")


class UnknownProduct(ValueError):
    """A label the archive carries and this module cannot place."""


def _shfe_product(label: str) -> str:
    """Strip the warehouse/factory split, then look the product up.

    The split is a presentation of one product across two tables (石油沥青
    仓库 / 厂库), so both halves belong to the same series.  `铜(BC)` is
    checked before stripping because its bracket is a product, not a split.
    """
    text = (label or "").strip()
    if text in SHFE_PRODUCTS:
        return SHFE_PRODUCTS[text]
    for suffix in _WAREHOUSE_SUFFIXES:
        if text.endswith(suffix):
            stem = text[: -len(suffix)].strip()
            if stem in SHFE_PRODUCTS:
                return SHFE_PRODUCTS[stem]
    raise UnknownProduct(
        f"citic_exchange_receipts: no product for SHFE label {label!r}."
        " Add it to SHFE_PRODUCTS -- a label that is quietly dropped takes a"
        " whole product out of the cross-section and looks like a result."
    )


def _product(source_kind: str, key: str) -> str | None:
    if source_kind.startswith("SHFE"):
        return _shfe_product(key)
    code = (key or "").strip().upper()
    if not code:
        return None  # GFEX prints a grand total with no product
    if source_kind.startswith("CZCE"):
        return CZCE_ALIASES.get(code, code)
    return code


def _collapse_grand_totals(frame: pd.DataFrame) -> tuple[pd.DataFrame, int]:
    """Drop a product-day's section totals when the last row totals them.

    CZCE spent 2021-04..09 printing PF as several brand tables, each closed
    by its own 总计, and then a final 总计 over them -- all of it parsed as
    `TOTAL`, with nothing to tell them apart: same cell text, same section
    position, same label.  Summing them doubled the product against Wind on
    exactly the hundred days the shape occurs.

    So the discriminator has to be the arithmetic, and the rule is made
    conditional on it rather than applied on faith.  Across the whole CZCE
    archive every product-day carrying several same-column totals is this
    shape and the last row is the grand total in each one; the only
    non-conforming groups are the 2010-2012 rows the capture could not
    attribute to a product, which never reach this far.

    Per column, not per product-day: MA writes 完税 and 保税 side by side in
    one row and those genuinely add.
    """
    ordered = frame.dropna(subset=["record_ordinal"])
    if ordered.empty:
        return frame, 0

    drop: list = []
    collapsed = 0
    for _, group in ordered.groupby(["product", "trade_date", "metric"], sort=False):
        if len(group) < 2 or group["value"].isna().any():
            continue
        group = group.sort_values("record_ordinal")
        last = group["value"].iloc[-1]
        others = group["value"].iloc[:-1].sum()
        if abs(last - others) <= 1e-6 * max(1.0, abs(float(last))):
            drop.extend(group.index[:-1])
            collapsed += 1
    if not drop:
        return frame, 0
    return frame.drop(index=drop), collapsed


def normalise_records(records: pd.DataFrame) -> tuple[pd.DataFrame, dict]:
    """Fold the archive's summed columns into one receipt series per product-day.

    Returns the series and a report.  A product-day whose receipt columns
    carried no number at all is left out rather than zeroed: the exchange
    printing nothing and the exchange printing a zero are different events,
    and `citic_index.receipts.fill_absent_zeros` is the layer that decides
    which absences become zeros.  It cannot decide that if a zero has already
    been invented here.
    """
    report = {
        "rows_in": int(len(records)),
        "rows_from_ignored_sources": 0,
        "rows_not_a_product_total": 0,
        "rows_unknown_metric": 0,
        "grand_totals_collapsed": 0,
        "product_days_without_a_number": 0,
    }
    if records.empty:
        return pd.DataFrame(columns=COLUMNS), report

    frame = records.copy()
    if "record_ordinal" not in frame.columns:
        frame["record_ordinal"] = float("nan")
    ignored = frame["source_kind"].isin(IGNORED_SOURCES)
    report["rows_from_ignored_sources"] = int(ignored.sum())
    frame = frame.loc[~ignored]

    unknown_source = ~frame["source_kind"].isin(TOTAL_ROW_KIND)
    if unknown_source.any():
        kinds = sorted(frame.loc[unknown_source, "source_kind"].unique())
        raise UnknownProduct(
            f"citic_exchange_receipts: unhandled capture kind(s) {kinds}."
            " Every source has its own shape; none may be read by analogy."
        )

    wanted = frame["source_kind"].map(TOTAL_ROW_KIND)
    is_total = frame["row_kind"] == wanted
    report["rows_not_a_product_total"] = int((~is_total).sum())
    frame = frame.loc[is_total]

    bucket = frame["metric"].map(
        lambda m: "receipts" if m in RECEIPT_METRICS
        else ("forecast" if m in FORECAST_METRICS else None)
    )
    report["rows_unknown_metric"] = int(bucket.isna().sum())
    frame = frame.assign(bucket=bucket).dropna(subset=["bucket"])
    if frame.empty:
        return pd.DataFrame(columns=COLUMNS), report

    product = [
        _product(kind, key)
        for kind, key in zip(frame["source_kind"], frame["product_key"])
    ]
    frame = frame.assign(product=product).dropna(subset=["product"])

    frame, collapsed = _collapse_grand_totals(frame)
    report["grand_totals_collapsed"] = collapsed

    # A column that carried no number contributes nothing; a column that
    # carried a zero contributes a zero, and the difference decides whether
    # the product-day exists at all.
    seen = len(frame.loc[:, ["product", "trade_date"]].drop_duplicates())
    totals = (
        frame.dropna(subset=["value"])
        .groupby(["product", "trade_date", "bucket"], sort=True)["value"]
        .sum()
        .unstack("bucket")
    )
    for column in ("receipts", "forecast"):
        if column not in totals.columns:
            totals[column] = float("nan")

    totals = totals.loc[totals["receipts"].notna()]
    report["product_days_without_a_number"] = seen - len(totals)

    out = (
        totals.assign(forecast=totals["forecast"].fillna(0.0))
        .reset_index()
        .loc[:, COLUMNS]
        .sort_values(["product", "trade_date"], ignore_index=True)
    )
    out["receipts"] = out["receipts"].astype(float)
    out["forecast"] = out["forecast"].astype(float)
    return out, report
