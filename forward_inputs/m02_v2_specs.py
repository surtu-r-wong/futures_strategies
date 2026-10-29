"""M02 v2 contract specifications: a per-product table, contracts derived on demand.

The 027 universe is open, so the account cannot freeze a contract list the way
v1 froze 67 contracts. Instead each product carries one reviewed row (exchange,
multiplier, fee-unit authority, status) and a contract's instrument spec and fee
rule are derived the first time the account needs it, from that contract's
exchange-published settlement parameters of the prior trading day:

    exchange fee   exchange_data.futures_settle_param four fee columns
                   open -> open_fee, close -> offset_fee,
                   intraday_open -> short_open_fee, close_today -> short_offset_fee
                   (the projection that reproduces all 67 v1 rules, see
                   `reproduce_v1`)
    fee unit       the row's declared fee_scale; where the source leaves it
                   NULL, the 2026-10-16 user unit rulings by (exchange,
                   fee_style), limited to the products those rulings covered
    margin         max(spec_buy_rate, spec_sell_rate) + 2pp (ruling 4-3)
    multiplier     public.futures_contract_info 合约乘数, must equal the product row
    validity       first needed trade date .. 最后日期 (last trading day)
    broker/slip    0.1 x exchange per action (x1.1 total, ruling 4-1), 4bp CNY cost

A derived contract is then frozen for its whole life: position lots and DCE
fee matching bind each rule's digest, so an exchange fee change applies to
contracts the account adds after the change, not to rules already registered.

Products whose fee unit is not declared by the source and not covered by a
ruling are `pending`; products the user ruled out are `excluded`. Deriving a
contract of either raises, and the morning precheck names it. Nothing here
writes to any database.
"""

import json
import re
from datetime import date
from pathlib import Path

from forward_account.contracts import digest
from forward_account.fees import ACTIONS

ROOT = Path(__file__).resolve().parents[1]
TABLE_PATH = ROOT / "config/m02_v2/product-specs.json"
TABLE_SCHEMA = "m02-v2-product-specs.v1"

SUFFIX_EXCHANGE = {"SHF": "SHFE", "INE": "INE", "DCE": "DCE", "CZC": "CZCE", "GFE": "GFEX"}
EXCHANGE_SUFFIX = {v: k for k, v in SUFFIX_EXCHANGE.items()}
FEE_COLUMNS = {
    "open": "open_fee",
    "close": "offset_fee",
    "intraday_open": "short_open_fee",
    "close_today": "short_offset_fee",
}
MARGIN_ADD = 0.02
BROKER_RATIO = 0.1
SLIPPAGE = {"value": 4, "unit": "PER_TEN_THOUSAND"}

# User unit rulings, exactly as far as each reached. Generalizing a ruling to
# another product of the same fee style is a user decision, recorded as its own
# entry with its own authority and source.
RULING_SOURCE = (
    "docs/requests/2026-10-16-m02-futures-rule-clarification/unit-decisions.json; "
    "docs/requests/2026-10-17-m02-initialization (round 2 ruling 1)"
)
RULING_SOURCE_1029 = (
    "docs/requests/2026-10-28-m02-futures-daily-follow/ruling-01.md "
    "(sha256 7fdac6f9bdafd53a55c121105c183cd14a25d160b020f00514dc08091d40be12)"
)
UNIT_RULINGS = (
    {
        "exchange": "DCE",
        "fee_style": "比例值",
        "unit": "PER_TEN_THOUSAND",
        "products": {"I", "J", "JD", "JM", "LH"},
        "authority": "user ruling 2026-10-16: 比例的单位均为万分，1就代表成交额的万分之一",
        "source": RULING_SOURCE,
    },
    {
        "exchange": "CZCE",
        "fee_style": "比例值",
        "unit": "PER_TEN_THOUSAND",
        "products": {"MA", "PR", "PX", "SA", "SH", "UR"},
        "authority": "user ruling 2026-10-16: 比例的单位均为万分，1就代表成交额的万分之一",
        "source": RULING_SOURCE,
    },
    {
        "exchange": "CZCE",
        "fee_style": "绝对值",
        "unit": "YUAN_PER_LOT",
        "products": {"AP", "CF", "CJ", "FG", "OI", "PF", "PK", "PL", "RM", "SF", "SM", "SR", "TA"},
        "authority": "user ruling 2026-10-16 round 2 ruling 1: CZCE absolute fee values are "
        "CNY yuan/lot, value-matched to the official CZCE fee schedule",
        "source": RULING_SOURCE,
    },
    {
        "exchange": "DCE",
        "fee_style": "比例值",
        "unit": "PER_TEN_THOUSAND",
        "products": {"BB", "FB", "LG"},
        "authority": "user_decision_2026-10-29: DCE ratio-style fees of BB/FB/LG are "
        "PER_TEN_THOUSAND (1 = 1/10000 of notional), same as the 2026-10-16 ruling",
        "source": RULING_SOURCE_1029,
    },
    {
        "exchange": "CZCE",
        "fee_style": "绝对值",
        "unit": "YUAN_PER_LOT",
        "products": {"CY", "JR", "PM", "RI", "RS", "WH", "ZC"},
        "authority": "user_decision_2026-10-29: CZCE absolute fees of CY/JR/PM/RI/RS/WH/ZC "
        "are CNY yuan/lot, same as the 2026-10-16 round 2 ruling",
        "source": RULING_SOURCE_1029,
    },
)
# Products the user ruled out of the account. Deriving their contracts raises
# exactly like a pending product (morning precheck names it, spec_gate fails).
EXCLUSIONS = {
    product_code: {
        "reason": "user_decision_2026-10-29: monthly-average contract, not a standard "
        "product code; not admitted",
        "source": RULING_SOURCE_1029,
    }
    for product_code in ("DCE:L-F", "DCE:PP-F", "DCE:V-F")
}
BROKER_SOURCE = (
    "user ruling 2026-10-16 4-1: exchange fee x1.1, no minimum; broker leg = 10% "
    "of the exchange rate registered for this contract, per action"
)
SLIPPAGE_SOURCE = (
    "user ruling 2026-10-16 4-2: single-side 4bp on raw notional charged as a CNY "
    "cost; no tick rounding (mechanism per user ruling 2026-10-18)"
)
MARGIN_SOURCE = "user ruling 2026-10-16 4-3: exchange margin +2pp"


class SpecMissing(ValueError):
    """A contract the account needs has no derivable specification."""


def split_code(contract):
    """'CF2701.CZC' -> ('CZCE', 'CF', 'CF2701'); refuses unknown suffixes."""
    code, _, suffix = contract.rpartition(".")
    if suffix not in SUFFIX_EXCHANGE:
        raise SpecMissing(f"{contract}: unsupported exchange suffix")
    match = re.fullmatch(r"([A-Z]+)(\d{4})", code)
    if not match:
        raise SpecMissing(f"{contract}: expected PRODUCT + four-digit month")
    return SUFFIX_EXCHANGE[suffix], match[1], code


def source_id(contract):
    """Exchange-native contract id as both source tables key it (upper case).

    Zhengzhou drops the decade digit (CF2701 -> CF701); the others keep four.
    """
    exchange, product, code = split_code(contract)
    if exchange == "CZCE":
        return product + code[len(product) + 1 :]
    return code


def product_key(exchange, product):
    return f"{exchange}:{product}"


def fee_unit(row, product_row):
    """(unit, authority) for one settlement-parameter row, or raise."""
    if row["fee_scale"] is not None:
        return row["fee_scale"], f"exchange_data declared fee_scale={row['fee_scale']}"
    ruling = product_row.get("fee_unit_ruling")
    if not ruling or ruling["fee_style"] != row["fee_style"]:
        raise SpecMissing(
            f"{product_row['exchange']}:{product_row['product']}: fee unit undeclared "
            f"for fee_style={row['fee_style']!r} and not covered by a user ruling"
        )
    return ruling["unit"], ruling["authority"]


def last_trade_date(info_row):
    raw = info_row["last_date"]
    text = str(int(float(raw))) if not isinstance(raw, str) else raw.split(".")[0]
    return date(int(text[:4]), int(text[4:6]), int(text[6:8])).isoformat()


def derive(contract, trade_date, table, settle_row, info_row):
    """(instrument spec, one-rule fee schedule) for `contract` from `trade_date`.

    settle_row / info_row are the prior trading day's rows (available before
    the 09:30 execution). Raises SpecMissing when the product is not closed,
    a source row is absent, or the multiplier disagrees with the product row.
    """
    exchange, product, _ = split_code(contract)
    product_row = table["products"].get(product_key(exchange, product))
    if product_row is None:
        raise SpecMissing(f"{contract}: product not in the M02 v2 table")
    if product_row["status"] != "closed":
        reasons = product_row["pending"] or [product_row["excluded"]["reason"]]
        raise SpecMissing(f"{contract}: product {product_row['status']} ({'; '.join(reasons)})")
    if settle_row is None or info_row is None:
        raise SpecMissing(f"{contract}: no settlement-parameter/contract-info row")
    multiplier = float(info_row["multiplier"])
    if multiplier != product_row["multiplier"]:
        raise SpecMissing(
            f"{contract}: multiplier {multiplier} != product table "
            f"{product_row['multiplier']}; dimension change needs review"
        )
    unit, unit_authority = fee_unit(settle_row, product_row)
    exchange_rates = {}
    for action in ACTIONS:
        value = settle_row[FEE_COLUMNS[action]]
        if value is None or float(value) < 0:
            raise SpecMissing(f"{contract}: {FEE_COLUMNS[action]} missing/negative")
        exchange_rates[action] = {"value": float(value), "unit": unit}
    rates = [settle_row["spec_buy_rate"], settle_row["spec_sell_rate"]]
    if any(r is None for r in rates):
        raise SpecMissing(f"{contract}: speculative margin rate missing")
    base_margin = max(float(r) for r in rates)
    valid_to = last_trade_date(info_row)
    if valid_to < trade_date:
        raise SpecMissing(f"{contract}: past its last trading day {valid_to}")
    param_date = settle_row["trade_date"]
    source = (
        f"exchange_data.futures_settle_param {exchange}/{settle_row['contract_id']} "
        f"{param_date}; {settle_row['source_url']}; sha256={settle_row['source_sha256']}"
    )
    instrument = {
        "asset_type": "linear_futures",
        "currency": "CNY",
        "multiplier": multiplier,
        "margin_rate": round(base_margin + MARGIN_ADD, 10),
        "valid_from": trade_date,
        "valid_to": valid_to,
        "source": (
            f"public.futures_contract_info {exchange}/{info_row['contract_id']} "
            f"{info_row['trade_date']} (multiplier, last trading day); margin {source}; "
            f"{MARGIN_SOURCE}; base {base_margin}; product table "
            f"{product_key(exchange, product)} rev {product_row['revision']}"
        ),
    }
    rule = {
        "rule_id": f"exchange-{param_date.replace('-', '')}-{exchange}-{source_id(contract)}",
        "valid_from": trade_date,
        "valid_to": valid_to,
        "exchange_source": f"{source}; fetched_at={settle_row['fetched_at']}; unit: {unit_authority}",
        "broker_source": BROKER_SOURCE,
        "slippage_source": SLIPPAGE_SOURCE,
        "exchange": exchange_rates,
        "broker": {
            a: {"value": round(r["value"] * BROKER_RATIO, 10), "unit": r["unit"]}
            for a, r in exchange_rates.items()
        },
        "slippage": dict(SLIPPAGE),
    }
    return instrument, [rule]


def load_table(path=TABLE_PATH):
    table = json.loads(Path(path).read_text())
    if table.get("schema") != TABLE_SCHEMA:
        raise ValueError("unexpected product table schema")
    return table


def build_table(settle_rows, info_rows, *, as_of, v1_config):
    """Product table from one day's source rows plus the v1 closed contracts.

    settle_rows / info_rows: dict rows for every commodity contract on `as_of`.
    A product is closed when its fee unit is declared by the source or covered
    by a ruling, and its multiplier is a single value across listed contracts;
    a product in EXCLUSIONS is `excluded` whatever its source rows say.
    """
    v1_products = {}
    for contract in v1_config["instruments"]:
        exchange, product, _ = split_code(contract)
        v1_products[product_key(exchange, product)] = contract
    info = {(r["exchange"], r["contract_id"].upper()): r for r in info_rows}
    grouped = {}
    for row in settle_rows:
        product = (row["product_id"] or "").upper().removesuffix("_F")
        grouped.setdefault((row["exchange"], product), []).append(row)
    products = {}
    for (exchange, product), rows in sorted(grouped.items()):
        key = product_key(exchange, product)
        pending = []
        if not re.fullmatch(r"[A-Z]+", product):
            pending.append("non-standard product code (monthly-average contract)")
        styles = sorted({r["fee_style"] or "-" for r in rows})
        scales = sorted({r["fee_scale"] or "NULL" for r in rows})
        multipliers = sorted(
            {
                float(info[(exchange, r["contract_id"].upper())]["multiplier"])
                for r in rows
                if (exchange, r["contract_id"].upper()) in info
            }
        )
        if len(multipliers) != 1:
            pending.append(f"multiplier not single-valued: {multipliers}")
        ruling = None
        if "NULL" in scales:
            style = styles[0] if len(styles) == 1 else None
            rulings = [
                r for r in UNIT_RULINGS if (r["exchange"], r["fee_style"]) == (exchange, style)
            ]
            covered = [r for r in rulings if product in r["products"]]
            if covered:
                (match,) = covered
                ruling = {k: match[k] for k in ("fee_style", "unit", "authority", "source")}
            elif rulings:
                named = sorted(set().union(*(r["products"] for r in rulings)))
                pending.append(
                    f"fee unit undeclared (fee_style={style}); the {rulings[0]['unit']} "
                    f"rulings covered {named}, not {product}"
                )
            else:
                pending.append(f"fee unit undeclared (fee_style={styles})")
        excluded = EXCLUSIONS.get(key)
        if excluded:
            status, pending = "excluded", []
        else:
            status = "pending" if pending else "closed"
        products[key] = {
            "exchange": exchange,
            "product": product,
            "name": max((r["product_name"] or "" for r in rows), key=len) or None,
            "revision": 1,
            "effective_from": as_of,
            "multiplier": multipliers[0] if len(multipliers) == 1 else None,
            "fee_styles": styles,
            "fee_scales": scales,
            "fee_unit_ruling": ruling,
            "price_limit": "per-day exchange limit prices, public.futures_contract_info "
            "涨停价/跌停价 of the trade date",
            "v1_closed_contract": v1_products.get(key),
            "status": status,
            "pending": pending,
            "excluded": excluded,
        }
    missing_v1 = sorted(set(v1_products) - set(products))
    if missing_v1:
        raise ValueError(f"v1 products absent from source rows: {missing_v1}")
    return {
        "schema": TABLE_SCHEMA,
        "as_of": as_of,
        "derivation": __doc__.split("\n\n")[1].strip(),
        "products": products,
        "table_sha256": None,
    }


def seal_table(table):
    body = {k: v for k, v in table.items() if k != "table_sha256"}
    return {**body, "table_sha256": digest(body)}


def reproduce_v1(v1_config, table, settle_rows, info_rows):
    """Re-derive the 67 v1 contracts from their 2026-10-15 rows; list diffs.

    Compared fields: exchange fee values and units per action, broker values,
    slippage, margin_rate and multiplier. Validity and source strings differ
    by construction (v1 froze to 2026-12-31).
    """
    settle = {(r["exchange"], r["contract_id"].upper()): r for r in settle_rows}
    info = {(r["exchange"], r["contract_id"].upper()): r for r in info_rows}
    diffs = []
    for contract in sorted(v1_config["instruments"]):
        exchange, _, _ = split_code(contract)
        key = (exchange, source_id(contract))
        instrument, rules = derive(
            contract, "2026-10-15", table, settle.get(key), info.get(key)
        )
        old_spec = v1_config["instruments"][contract]
        old = v1_config["fee_model"]["rules"][contract][0]
        new = rules[0]
        for field, a, b in (
            ("multiplier", old_spec["multiplier"], instrument["multiplier"]),
            ("margin_rate", old_spec["margin_rate"], instrument["margin_rate"]),
            ("exchange", old["exchange"], new["exchange"]),
            ("broker", old["broker"], new["broker"]),
            ("slippage", old["slippage"], new["slippage"]),
        ):
            if a != b:
                diffs.append({"contract": contract, "field": field, "v1": a, "v2": b})
    return diffs


SETTLE_SQL = (
    "SELECT exchange, trade_date, upper(contract_id) AS contract_id, product_id, "
    "product_name, open_fee, offset_fee, short_open_fee, short_offset_fee, fee_style, "
    "fee_scale, spec_buy_rate, spec_sell_rate, source_url, source_sha256, fetched_at "
    "FROM exchange_data.futures_settle_param WHERE trade_date = %(day)s "
    "AND exchange = ANY(%(exchanges)s)"
)
INFO_SQL = (
    'SELECT "交易所代码" AS exchange, "交易日期" AS trade_date, '
    'upper("合约代码") AS contract_id, "合约乘数" AS multiplier, '
    '"最后日期" AS last_date, "涨停价" AS limit_up, "跌停价" AS limit_down '
    'FROM public.futures_contract_info WHERE "交易日期" = %(day)s '
    'AND "交易所代码" = ANY(%(exchanges)s)'
)
COMMODITY = sorted(SUFFIX_EXCHANGE.values())


def _plain(value):
    from decimal import Decimal
    import datetime as dt

    if value is None or (isinstance(value, float) and value != value):
        return None  # pandas turns SQL NULL into NaN in object columns
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dt.datetime):
        return value.isoformat()
    if isinstance(value, dt.date):
        return value.isoformat()
    return value


def fetch(query, sql, day):
    """Rows as plain dicts; `query(sql, params)` returns a DataFrame."""
    frame = query(sql, {"day": day, "exchanges": COMMODITY})
    return [
        {k: _plain(v) for k, v in row.items()} for row in frame.to_dict("records")
    ]
