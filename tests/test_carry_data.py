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
