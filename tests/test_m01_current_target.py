"""M01 exports current native weights without inventing capital or execution facts."""

from datetime import date
from types import SimpleNamespace

import pandas as pd
import pytest

from citic_index.book import lever, next_targets, rolling_vol_scale
from citic_index.daily import PRODUCTION, LOOKBACK_DAYS
from forward_inputs.m01 import current_weights, preview_evidence


def result_fixture():
    dates = [d.date() for d in pd.bdate_range(end="2026-10-15", periods=260)]
    end = dates[-1]
    weights = pd.DataFrame(
        {"trade_date": [end, end], "product": ["M", "Y"], "weight": [-0.25, 0.25]}
    )
    legs = pd.DataFrame(
        {
            "trade_date": [end, end],
            "product": ["M", "Y"],
            "main_contract": ["M2701.DCE", "Y2701.DCE"],
            "main_close": [3000.0, 6000.0],
        }
    )
    pool = pd.DataFrame(
        {"trade_date": [end, end], "product": ["M", "Y"], "multiplier": [10, 10]}
    )
    index = pd.DataFrame(
        {
            "trade_date": dates,
            "daily_return": [0.001, -0.001] * 130,
            "n_products": [2] * 260,
        }
    )
    return SimpleNamespace(weights=weights, legs=legs, pool=pool, index=index), end


def test_export_equals_native_target_weights_without_capital_or_estimated_specs():
    result, end = result_fixture()
    actual = current_weights(result, end, PRODUCTION)
    scale = rolling_vol_scale(
        result.index.set_index("trade_date")["daily_return"],
        vol_window=252,
        target_vol=0.15,
        min_observations=252,
    )
    native = next_targets(
        lever(result.weights, scale, config=PRODUCTION),
        result.legs,
        result.pool,
        signal_date=end,
        capital=123456.0,
    )
    assert actual["contract"].tolist() == native["contract"].tolist()
    assert actual["target_weight"].tolist() == pytest.approx(
        native["target_weight"].tolist()
    )
    assert actual["target_weight"].abs().sum() == pytest.approx(4.0)
    assert not {"capital", "lots", "notional", "multiplier", "margin_rate"} & set(
        actual
    )
    assert LOOKBACK_DAYS == 900
    assert PRODUCTION.execution_lag == 1 and PRODUCTION.window == 150


def test_short_vol_window_refuses_target_without_shortening_algorithm():
    result, end = result_fixture()
    result.index = result.index.tail(251)
    with pytest.raises(ValueError, match="volatility"):
        current_weights(result, end, PRODUCTION)


def test_missing_or_duplicate_dominant_contract_refuses_incomplete_target():
    result, end = result_fixture()
    result.legs = result.legs.iloc[:1]
    with pytest.raises(ValueError):
        current_weights(result, end, PRODUCTION)
    result, end = result_fixture()
    result.legs.loc[1, "main_contract"] = result.legs.loc[0, "main_contract"]
    with pytest.raises(ValueError):
        current_weights(result, end, PRODUCTION)


def test_eod_price_volume_and_raw_margin_do_not_manufacture_execution_evidence():
    targets = pd.DataFrame(
        {
            "contract": ["M2701.DCE"],
            "target_weight": [1.5],
            "signal_date": [date(2026, 10, 15)],
        }
    )
    prices = pd.DataFrame(
        {
            "trade_date": [date(2026, 10, 15)],
            "contract": ["M2701.DCE"],
            "close": [3000.0],
            "open": [2990.0],
            "volume": [999],
            "settle": [3001.0],
        }
    )
    meta = pd.DataFrame(
        [
            {
                "合约代码": "M2701",
                "交易日期": date(2026, 10, 15),
                "合约乘数": 10,
                "多头保证金率": 12,
                "空头保证金率": 12,
                "手续费收取方式": "按手",
                "开仓手续费": 1.5,
                "最后日期": "2027-01-15",
            }
        ]
    )
    evidence = preview_evidence(
        targets, prices, meta, observed_at="2026-10-15T19:00:00+08:00"
    )
    row = evidence["contracts"][0]
    assert row["reference_price"]["value"] == 3000.0
    assert row["reference_price"]["kind"] == "eod_close_reference"
    assert row["reference_price"]["source_available_at"] is None
    assert row["tradable_buy"] is None and row["tradable_sell"] is None
    assert row["specification"]["multiplier"] == 10
    assert row["specification"]["margin_long_raw"] == 12
    assert row["specification"]["margin_rate"] is None
    assert row["specification"]["minimum_order_lots"] is None
    assert evidence["execution_status"] == "blocked"
    assert evidence["daily_settlement_status"] == "not_adapted_not_an_m01_target_gate"


def test_conflicting_or_missing_metadata_never_falls_back_to_price_inference():
    targets = pd.DataFrame(
        {
            "contract": ["M2701.DCE", "Y2701.DCE"],
            "target_weight": [1, -1],
            "signal_date": [date(2026, 10, 15)] * 2,
        }
    )
    prices = pd.DataFrame(
        {
            "trade_date": [date(2026, 10, 15)] * 2,
            "contract": ["M2701.DCE", "Y2701.DCE"],
            "close": [3000, 6000],
        }
    )
    meta = pd.DataFrame(
        [
            {"合约代码": "M2701", "交易日期": date(2026, 10, 15), "合约乘数": 10},
            {"合约代码": "M2701", "交易日期": date(2026, 10, 15), "合约乘数": 20},
        ]
    )
    evidence = preview_evidence(
        targets, prices, meta, observed_at="2026-10-15T19:00:00+08:00"
    )
    assert all(
        row["specification"]["multiplier"] is None for row in evidence["contracts"]
    )
    assert "conflicting_contract_metadata" in evidence["contracts"][0]["blockers"]
    assert "missing_contract_metadata" in evidence["contracts"][1]["blockers"]


def test_readonly_snapshot_rejects_mutation_and_has_no_query_retry(monkeypatch):
    from forward_inputs import m01

    calls = []

    class Cursor:
        description = [("value",)]

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, sql, params=None):
            calls.append(sql)
            if sql == "SELECT failing":
                raise RuntimeError("synthetic source unavailable")

        def fetchone(self):
            return ("on",)

        def fetchall(self):
            return [(1,)]

    class Connection:
        def set_session(self, **kwargs):
            assert kwargs == {"readonly": True, "isolation_level": "REPEATABLE READ"}

        def cursor(self):
            return Cursor()

        def rollback(self):
            calls.append("rollback")

        def close(self):
            calls.append("close")

    monkeypatch.setattr(m01, "_dsn", lambda: {})
    monkeypatch.setattr(m01.psycopg2, "connect", lambda **kwargs: Connection())
    with m01.ReadOnlySource() as source:
        with pytest.raises(ValueError, match="SELECT"):
            source.query("UPDATE forbidden", {})
        assert source.query("SELECT 1", {}).iloc[0, 0] == 1
        with pytest.raises(RuntimeError):
            source.query("SELECT failing", {})
    assert calls.count("SELECT failing") == 1
    assert "UPDATE forbidden" not in calls
    assert calls[-2:] == ["rollback", "close"]


def test_failed_calculation_is_attempted_once_and_destination_cannot_be_reused(
    tmp_path, monkeypatch
):
    import json
    from forward_inputs import m01

    fake_production = {name: "a" * 64 for name in m01.NATIVE_FILES}
    manifest_files = {**fake_production, "forward_inputs/m01.py": "b" * 64}
    production = tmp_path / "production.json"
    production.write_text(
        json.dumps(
            {
                "files": fake_production,
                "tracked_changes": [],
                "local_mismatches": [],
                "targets": ["output/targets/old.csv"],
                "observed_at": "2026-10-15T19:00:00+08:00",
                "checkout": "/synthetic",
                "git_head": "a" * 40,
            }
        )
    )
    monkeypatch.setattr(m01, "source_files", lambda: manifest_files)
    monkeypatch.setattr(m01.resource, "getrlimit", lambda key: (2000000 * 1024,) * 2)
    count = []

    class Source:
        queries = []

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def frontier(self, as_of):
            return pd.DataFrame(
                {"exchange": list(m01.COMMODITY_EXCHANGES), "max_date": [as_of] * 5}
            )

        def window(self, start, end, root):
            assert (end - start).days == 900
            return pd.DataFrame({"trade_date": [end]})

    def build(prices, config):
        count.append(1)
        assert config.window == 150 and config.execution_lag == 1
        raise ValueError("synthetic incomplete input")

    monkeypatch.setattr(m01, "ReadOnlySource", Source)
    monkeypatch.setattr(m01, "build_panel", build)
    out = tmp_path / "capture"
    report = m01.run(date(2026, 10, 15), production, out)
    assert report["status"] == "blocked"
    assert report["actual_target_calculations"] == len(count) == 1
    assert report["failure_phase"] == "single_native_target_calculation"
    assert (out / "calculation-attempt.json").is_file()
    assert report["available_at"] is None
    with pytest.raises(FileExistsError):
        m01.run(date(2026, 10, 15), production, out)
    assert len(count) == 1


def test_failure_trace_records_only_code_positions():
    import json
    from forward_inputs.m01 import safe_failure_trace

    try:
        raise ValueError("SYNTHETIC_SECRET_DO_NOT_RECORD")
    except ValueError as exc:
        trace = safe_failure_trace(exc)
    assert trace[-1]["file"] == "tests/test_m01_current_target.py"
    assert trace[-1]["function"] == "test_failure_trace_records_only_code_positions"
    assert isinstance(trace[-1]["line"], int)
    assert set(trace[-1]) == {"file", "line", "function"}
    assert "SYNTHETIC_SECRET_DO_NOT_RECORD" not in json.dumps(trace)
