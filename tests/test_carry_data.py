from datetime import date

import pandas as pd
import pytest

from cta_carry.data import CarryDataSet, DataConflictError, normalize_contract_daily


AUDIT_COLUMNS = [
    "object_type",
    "object_id",
    "trade_date",
    "check",
    "status",
    "action",
    "reason",
]


def _valid_row(**overrides):
    row = {
        "trade_date": "2024-01-02",
        "contract": "rb2405.shf",
        "open": 100.0,
        "high": 104.0,
        "low": 99.0,
        "close": 102.0,
        "volume": 10.0,
        "oi": 20.0,
        "turnover": 1_000.0,
    }
    row.update(overrides)
    return row


def test_carry_dataset_default_data_quality_uses_audit_columns():
    dataset = CarryDataSet(pd.DataFrame())

    assert dataset.data_quality.columns.tolist() == AUDIT_COLUMNS


def test_carry_dataset_slice_empty_products_preserves_all():
    dataset = normalize_contract_daily(
        pd.DataFrame(
            [
                _valid_row(),
                _valid_row(
                    trade_date="2024-01-03",
                    contract="TA405.CZC",
                    open=200.0,
                    high=204.0,
                    low=199.0,
                    close=202.0,
                ),
            ]
        )
    )

    sliced = dataset.slice(products=[])

    assert sliced.prices.equals(dataset.prices)


def test_carry_dataset_slice_blank_products_preserves_all():
    dataset = normalize_contract_daily(pd.DataFrame([_valid_row()]))

    sliced = dataset.slice(products=[" "])

    assert sliced.prices.equals(dataset.prices)


def test_carry_dataset_slice_resets_filtered_price_index():
    dataset = normalize_contract_daily(
        pd.DataFrame(
            [
                _valid_row(),
                _valid_row(
                    trade_date="2024-01-03",
                    contract="TA405.CZC",
                    open=200.0,
                    high=204.0,
                    low=199.0,
                    close=202.0,
                ),
            ]
        )
    )

    sliced = dataset.slice(products=["TA"])

    assert sliced.prices["contract"].tolist() == ["TA405.CZC"]
    assert sliced.prices.index.tolist() == [0]


def test_normalize_contract_daily_parses_mixed_trade_date_formats():
    date_values = [20240102, "2024-01-03", "20240104"]
    frame = pd.DataFrame(
        [_valid_row(trade_date=value) for value in date_values]
    )

    dataset = normalize_contract_daily(frame)

    assert dataset.prices["trade_date"].tolist() == [
        date(2024, 1, 2),
        date(2024, 1, 3),
        date(2024, 1, 4),
    ]


def test_normalize_contract_daily_audits_each_unparseable_trade_date():
    frame = pd.DataFrame(
        [
            _valid_row(trade_date="not-a-date"),
            _valid_row(trade_date="still-not-a-date"),
        ]
    )

    dataset = normalize_contract_daily(frame)

    assert dataset.prices.empty
    assert dataset.data_quality.columns.tolist() == AUDIT_COLUMNS
    assert len(dataset.data_quality) == 2
    assert dataset.data_quality["check"].tolist() == ["trade_date"] * 2
    assert dataset.data_quality["reason"].tolist() == [
        "unparseable_trade_date"
    ] * 2


def test_normalize_contract_daily_audits_nullable_ohlc():
    frame = pd.DataFrame([_valid_row()])
    frame["open"] = pd.Series([pd.NA], dtype="Float64")

    dataset = normalize_contract_daily(frame)

    assert dataset.prices.empty
    assert dataset.data_quality["check"].tolist() == ["ohlc_integrity"]


def test_normalize_contract_daily_audits_nullable_activity():
    frame = pd.DataFrame([_valid_row()])
    frame["volume"] = pd.Series([pd.NA], dtype="Float64")

    dataset = normalize_contract_daily(frame)

    assert dataset.prices.empty
    assert dataset.data_quality["check"].tolist() == ["activity_fields"]


def test_normalize_contract_daily_is_idempotent():
    once = normalize_contract_daily(pd.DataFrame([_valid_row()]))

    twice = normalize_contract_daily(once.prices)

    assert twice.prices.columns.is_unique
    pd.testing.assert_frame_equal(twice.prices, once.prices)


def test_normalize_contract_daily_deduplicates_and_derives_contract_fields():
    row = _valid_row()

    dataset = normalize_contract_daily(pd.DataFrame([row, row.copy()]))

    assert len(dataset.prices) == 1
    normalized = dataset.prices.iloc[0]
    assert normalized["trade_date"] == date(2024, 1, 2)
    assert normalized["contract"] == "RB2405.SHF"
    assert normalized["product"] == "RB"
    assert normalized["exchange_suffix"] == "SHF"
    assert normalized["delivery_yyyymm"] == 202405
    assert dataset.dates == [date(2024, 1, 2)]
    assert dataset.data_quality.empty
    assert dataset.data_quality.columns.tolist() == AUDIT_COLUMNS


def test_normalize_contract_daily_rejects_conflicting_duplicate_keys():
    rows = [_valid_row(), _valid_row(close=103.0)]

    with pytest.raises(DataConflictError, match=r"2024-01-02.*RB2405"):
        normalize_contract_daily(pd.DataFrame(rows))


def test_normalize_contract_daily_audits_parse_and_ohlc_exclusions_once():
    rows = [
        _valid_row(),
        _valid_row(
            contract="TA405.CZC",
            open=100.0,
            high=98.0,
            low=97.0,
            close=102.0,
        ),
        _valid_row(contract="BAD", turnover=-1.0),
    ]

    dataset = normalize_contract_daily(pd.DataFrame(rows))

    assert dataset.prices["contract"].tolist() == ["RB2405.SHF"]
    assert dataset.data_quality.columns.tolist() == AUDIT_COLUMNS
    assert set(dataset.data_quality["check"]) == {
        "contract_parse",
        "ohlc_integrity",
    }
    assert set(dataset.data_quality["object_id"]) == {"TA405.CZC", "BAD"}
    assert set(dataset.data_quality["status"]) == {"excluded"}
    assert set(dataset.data_quality["action"]) == {"exclude_candidate"}
    assert set(dataset.data_quality["object_type"]) == {"contract_bar"}


def test_normalize_contract_daily_lists_missing_required_columns():
    frame = pd.DataFrame([_valid_row()]).drop(columns=["oi", "turnover"])

    with pytest.raises(ValueError) as exc_info:
        normalize_contract_daily(frame)

    assert "oi" in str(exc_info.value)
    assert "turnover" in str(exc_info.value)


@pytest.mark.parametrize(
    ("field", "invalid_value"),
    [
        ("volume", -1.0),
        ("oi", float("nan")),
        ("turnover", float("inf")),
    ],
)
def test_normalize_contract_daily_audits_invalid_activity_fields(
    field, invalid_value
):
    dataset = normalize_contract_daily(
        pd.DataFrame([_valid_row(**{field: invalid_value})])
    )

    assert dataset.prices.empty
    assert dataset.data_quality["check"].tolist() == ["activity_fields"]
    assert dataset.data_quality["action"].tolist() == ["exclude_candidate"]


def test_carry_dataset_slice_uppercases_products_and_copies_audit():
    dataset = normalize_contract_daily(
        pd.DataFrame(
            [
                _valid_row(),
                _valid_row(
                    trade_date="2024-01-03",
                    contract="TA405.CZC",
                    open=200.0,
                    high=204.0,
                    low=199.0,
                    close=202.0,
                ),
                _valid_row(contract="BAD"),
            ]
        )
    )

    sliced = dataset.slice(
        products=[" ta "],
        start=date(2024, 1, 3),
        end=date(2024, 1, 3),
    )

    assert sliced.prices["contract"].tolist() == ["TA405.CZC"]
    assert sliced.data_quality.equals(dataset.data_quality)
    assert sliced.data_quality is not dataset.data_quality


@pytest.mark.parametrize("file_type", ["csv", "parquet"])
def test_carry_dataset_from_dir_reads_csv_or_parquet(tmp_path, file_type):
    frame = pd.DataFrame([_valid_row()])
    path = tmp_path / f"prices.{file_type}"
    if file_type == "csv":
        frame.to_csv(path, index=False)
    else:
        frame.to_parquet(path, index=False)

    dataset = CarryDataSet.from_dir(tmp_path)

    assert dataset.prices["contract"].tolist() == ["RB2405.SHF"]


def test_carry_dataset_from_dir_prefers_csv_and_requires_a_prices_file(tmp_path):
    with pytest.raises(FileNotFoundError):
        CarryDataSet.from_dir(tmp_path)

    pd.DataFrame([_valid_row(contract="TA405.CZC")]).to_parquet(
        tmp_path / "prices.parquet", index=False
    )
    pd.DataFrame([_valid_row()]).to_csv(tmp_path / "prices.csv", index=False)

    dataset = CarryDataSet.from_dir(tmp_path)

    assert dataset.prices["contract"].tolist() == ["RB2405.SHF"]
