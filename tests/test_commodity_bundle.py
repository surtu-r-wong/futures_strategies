"""Versioned, content-addressed commodity panel bundles."""

from __future__ import annotations

import dataclasses
import hashlib
from datetime import date
import json
import math
import multiprocessing
import os
from pathlib import Path
import threading
from types import SimpleNamespace

import pandas as pd
import pytest

pytest_plugins = ("tests.commodity_fixtures",)

from common.minute.bars import MinuteDataError
from common.commodity.bundle import (  # noqa: E402
    BUNDLE_VERSION,
    TABLE_FILES,
    TABLE_SCHEMAS,
    PanelBundle,
    read_bundle,
    write_bundle,
)
from common.commodity.panel import (  # noqa: E402
    PanelMonthChunk,
    build_contexts,
    build_panel as build_commodity_panel,
    normalise_panel,
)
from common.dominant import DominantChoice  # noqa: E402
from common.minute.sessions import SessionRule  # noqa: E402
from common.minute.bars import MultiplierResolution  # noqa: E402
from scripts.commodity.build_panel import (  # noqa: E402
    _metadata_multiplier_resolution,
    DigestingMinuteSource,
    DigestingMultiplierResolver,
    _build_panel_checkpointed,
    _bundle_bars,
    _effective_config_sha256,
    _roll_candidate,
    _source_revision,
    adjust_signal_bars,
    build_parser,
    build_roll_fills,
)


ROLL_DATES = (pd.Timestamp("2024-03-05").date(), pd.Timestamp("2024-03-06").date())


def _manifest(path: Path) -> dict[str, object]:
    return json.loads((path / "manifest.json").read_text(encoding="utf-8"))


def test_bundle_round_trip_preserves_all_tables(tmp_path, bundle_frames):
    write_bundle(tmp_path, **bundle_frames)
    loaded = read_bundle(tmp_path)

    assert isinstance(loaded, PanelBundle)
    for table in TABLE_FILES:
        assert getattr(loaded, table).equals(bundle_frames[table])
    assert loaded.manifest == _manifest(tmp_path)
    assert dataclasses.fields(PanelBundle)
    with pytest.raises((dataclasses.FrozenInstanceError, AttributeError)):
        loaded.bars = pd.DataFrame()


def test_bundle_refuses_a_changed_table(tmp_path, bundle_frames):
    write_bundle(tmp_path, **bundle_frames)
    (tmp_path / "bars.parquet").write_bytes(b"tampered")

    with pytest.raises(ValueError, match="bundle_digest_mismatch"):
        read_bundle(tmp_path)


def test_bundle_checks_every_digest_before_reading_any_parquet(
    tmp_path, bundle_frames, monkeypatch
):
    write_bundle(tmp_path, **bundle_frames)
    (tmp_path / "roll_fills.parquet").write_bytes(b"tampered")

    def forbidden(*args, **kwargs):
        raise AssertionError("parquet read happened before all digest checks")

    monkeypatch.setattr(pd, "read_parquet", forbidden)
    with pytest.raises(ValueError, match="bundle_digest_mismatch"):
        read_bundle(tmp_path)


def test_manifest_has_exact_versioned_table_inventory_and_provenance(
    tmp_path, bundle_frames
):
    write_bundle(
        tmp_path,
        **bundle_frames,
        inputs={"daily_relation": "public.futures_daily", "start": "2024-03-05"},
        provenance={"session_rules_sha256": "a" * 64, "pricing_rules_sha256": "b" * 64},
    )
    manifest = _manifest(tmp_path)

    assert BUNDLE_VERSION == 1
    assert TABLE_FILES == {
        "bars": "bars.parquet",
        "universes": "universes.parquet",
        "dominants": "dominants.parquet",
        "roll_fills": "roll_fills.parquet",
    }
    assert set(manifest) == {"bundle_version", "inputs", "provenance", "tables"}
    assert manifest["bundle_version"] == 1
    assert set(manifest["tables"]) == set(TABLE_FILES)
    for table, filename in TABLE_FILES.items():
        entry = manifest["tables"][table]
        assert entry == {
            "filename": filename,
            "sha256": hashlib.sha256((tmp_path / filename).read_bytes()).hexdigest(),
        }


def test_bundle_manifest_is_deterministic(tmp_path, bundle_frames):
    first = tmp_path / "first"
    second = tmp_path / "second"
    write_bundle(first, **bundle_frames, inputs={"end": "2024-03-06"})
    write_bundle(second, **bundle_frames, inputs={"end": "2024-03-06"})

    assert (first / "manifest.json").read_bytes() == (
        second / "manifest.json"
    ).read_bytes()


def test_manifest_is_replaced_from_a_temporary_file_in_the_bundle_directory(
    tmp_path, bundle_frames, monkeypatch
):
    observed = {}
    original = Path.replace

    def recording_replace(source, target):
        observed["source"] = source
        observed["target"] = Path(target)
        observed["manifest_existed"] = (tmp_path / "manifest.json").exists()
        return original(source, target)

    monkeypatch.setattr(Path, "replace", recording_replace)
    write_bundle(tmp_path, **bundle_frames)

    assert observed["source"].parent == tmp_path
    assert observed["source"].name != "manifest.json"
    assert observed["target"] == tmp_path / "manifest.json"
    assert observed["manifest_existed"] is False


def test_read_refuses_a_missing_table_before_parquet_read(
    tmp_path, bundle_frames, monkeypatch
):
    write_bundle(tmp_path, **bundle_frames)
    (tmp_path / "dominants.parquet").unlink()
    monkeypatch.setattr(
        pd,
        "read_parquet",
        lambda *args, **kwargs: pytest.fail(
            "missing table must fail before parquet read"
        ),
    )

    with pytest.raises(ValueError, match="bundle_table_missing"):
        read_bundle(tmp_path)


@pytest.mark.parametrize("version", [0, 2, "1", None])
def test_read_refuses_an_unsupported_bundle_version(tmp_path, bundle_frames, version):
    write_bundle(tmp_path, **bundle_frames)
    manifest = _manifest(tmp_path)
    manifest["bundle_version"] = version
    (tmp_path / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="bundle_version_unsupported"):
        read_bundle(tmp_path)


@pytest.mark.parametrize("table", ["bars", "universes", "dominants", "roll_fills"])
def test_write_refuses_missing_or_extra_schema_columns(tmp_path, bundle_frames, table):
    missing = {name: frame.copy() for name, frame in bundle_frames.items()}
    missing[table] = missing[table].drop(columns=[missing[table].columns[-1]])
    with pytest.raises(ValueError, match="bundle_schema_columns"):
        write_bundle(tmp_path / "missing", **missing)

    extra = {name: frame.copy() for name, frame in bundle_frames.items()}
    extra[table]["unexpected"] = 1
    with pytest.raises(ValueError, match="bundle_schema_columns"):
        write_bundle(tmp_path / "extra", **extra)


def test_write_refuses_invalid_production_dtype(tmp_path, bundle_frames):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["dominants"]["oi"] = "not-an-integer"

    with pytest.raises(ValueError, match="bundle_schema_dtype"):
        write_bundle(tmp_path, **frames)


def test_read_revalidates_schema_after_verified_parquet_read(tmp_path, bundle_frames):
    write_bundle(tmp_path, **bundle_frames)
    bars_path = tmp_path / "bars.parquet"
    bars = pd.read_parquet(bars_path)
    bars["unexpected"] = 1
    bars.to_parquet(bars_path, index=False)
    manifest = _manifest(tmp_path)
    manifest["tables"]["bars"]["sha256"] = hashlib.sha256(
        bars_path.read_bytes()
    ).hexdigest()
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bundle_schema_columns"):
        read_bundle(tmp_path)


def test_schema_constants_declare_all_production_columns():
    assert tuple(TABLE_SCHEMAS["bars"]) == (
        "product",
        "contract",
        "trade_date",
        "slot_end",
        "open",
        "high",
        "low",
        "close",
        "volume",
        "open_interest",
        "no_trade",
        "adj_factor",
        # 市场断代分段：价格型状态不得跨段延续，所以它必须随每根 bar 落盘。
        "continuity_segment",
        "fill_time",
        "fill_price",
        "fill_pending",
        "fill_unpriceable",
        "pricing_basis",
        "multiplier",
    )
    assert tuple(TABLE_SCHEMAS["universes"]) == ("month_start", "product")
    assert tuple(TABLE_SCHEMAS["dominants"]) == (
        "trade_date",
        "product",
        "contract",
        "oi",
        "volume",
        "selected_from",
        "adj_factor",
    )
    assert tuple(TABLE_SCHEMAS["roll_fills"]) == (
        "trade_date",
        "product",
        "old_contract",
        "new_contract",
        "fill_time",
        "old_price",
        "new_price",
        "old_pricing_basis",
        "new_pricing_basis",
    )


def test_manifest_rejects_secret_bearing_provenance(tmp_path, bundle_frames):
    with pytest.raises(ValueError, match="bundle_manifest_sensitive"):
        write_bundle(
            tmp_path,
            **bundle_frames,
            provenance={"database_password": "must-not-leak"},
        )
    assert not (tmp_path / "manifest.json").exists()


def _roll_choices():
    return (
        DominantChoice(
            trade_date=ROLL_DATES[0],
            product="RB",
            contract="RB2405.SHF",
            oi=100,
            volume=90,
            selected_from=pd.Timestamp("2024-03-04").date(),
        ),
        DominantChoice(
            trade_date=ROLL_DATES[1],
            product="RB",
            contract="RB2410.SHF",
            oi=110,
            volume=100,
            selected_from=ROLL_DATES[0],
        ),
    )


def _roll_minute_frame(candidate, slots, price):
    rows = []
    for index, slot in enumerate(slots[:5]):
        level = price + index
        rows.append(
            {
                "trade_date": candidate.trade_date,
                "product": candidate.product,
                "daily_contract": candidate.daily_contract,
                "bar_time": slot,
                "symbol": candidate.minute_symbol,
                "exchange": candidate.exchange,
                "open": level,
                "high": level + 0.5,
                "low": level - 0.5,
                "close": level,
                "volume": 1.0,
                "amount": level * 10.0,
                "open_interest": 1000.0 + index,
            }
        )
    return pd.DataFrame(rows)


class _RollSource:
    def __init__(self, slots, prices):
        self.slots = slots
        self.prices = prices
        self.requests = []

    def iter_month(self, candidates, lower, upper):
        self.requests.append(tuple(candidates))
        frames = [
            _roll_minute_frame(
                candidate, self.slots, self.prices[candidate.daily_contract]
            )
            for candidate in candidates
            if candidate.daily_contract in self.prices
        ]
        if frames:
            yield pd.concat(frames, ignore_index=True)


def test_roll_fill_requests_both_raw_contracts_and_records_exact_window():
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(
        context.slots,
        {"RB2405.SHF": 100.0, "RB2410.SHF": 200.0},
    )

    fills, skipped = build_roll_fills(
        choices=choices,
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
        multiplier_resolver=lambda candidate, frame, **_: 10,
    )

    assert skipped == ()
    assert len(source.requests) == 1
    request = source.requests[0]
    assert {candidate.daily_contract for candidate in request} == {
        "RB2405.SHF",
        "RB2410.SHF",
    }
    assert {candidate.candidate_role for candidate in request} == {
        "roll_old",
        "roll_new",
    }
    assert all(candidate.window_start == context.slots[0] for candidate in request)
    # 请求整段：乘数在元数据缺档时靠推断，而推断要十根有成交的分钟，五分钟给不够。
    assert all(
        candidate.window_end == context.slots[-1] + pd.Timedelta(minutes=1)
        for candidate in request
    )
    assert fills.loc[0, "old_price"] == pytest.approx(102.0)
    assert fills.loc[0, "new_price"] == pytest.approx(202.0)
    assert fills.loc[0, "fill_time"] == context.slots[4]
    assert fills.loc[0, "old_pricing_basis"] == "amount_vwap"
    assert fills.loc[0, "new_pricing_basis"] == "amount_vwap"


def test_a_roll_with_no_fill_window_books_no_fill_and_is_reported():
    """成交窗口零成交（旧腿已退市，或那五分钟没人交易）—— 没有可执行的转移。

    全历史 3,406 次换月里 96 次如此：链断（AU1912 到期十天后主力才换到 AU2006）与
    薄成交（WR/B/SF/SM，以及 PP1405 2014-03-05 那种当天有成交但不在开盘五分钟里的）。
    连续价仍由日线收盘算出的复权因子缝合；跳过的清单一次报全，不是一次炸一条。
    """
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(context.slots, {"RB2410.SHF": 200.0})

    fills, skipped = build_roll_fills(
        choices=choices,
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
        multiplier_resolver=lambda candidate, frame, **_: 10,
    )

    assert fills.empty
    assert [
        (
            row["trade_date"],
            row["product"],
            row["old_contract"],
            row["new_contract"],
            row["unpriceable_leg"],
        )
        for row in skipped
    ] == [(ROLL_DATES[1], "RB", "RB2405.SHF", "RB2410.SHF", "RB2405.SHF")]


def _multiplier_that_fails(check: str):
    def resolver(candidate, frame, **_):
        if candidate.daily_contract == "RB2405.SHF":
            raise MinuteDataError(
                trade_date=candidate.trade_date,
                contract=candidate.daily_contract,
                check=check,
                reason="synthetic",
            )
        return 10

    return resolver


def test_a_leg_whose_multiplier_cannot_be_resolved_books_no_fill():
    """乘数定不出来 ⇒ 这条腿定不出价，与窗口零成交同一个结果。

    元数据缺档时乘数只能从分钟推断，推断要跨多个交易日取样，而换月只请求换月当天
    ——聚丙烯上市第二周的换月（PP1405 2014-03-05）就卡在这里。真正的把关在 bars
    阶段：同一张合约在它当主力的那些天必须解析出乘数。
    """
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(context.slots, {"RB2405.SHF": 100.0, "RB2410.SHF": 200.0})

    fills, skipped = build_roll_fills(
        choices=choices,
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
        multiplier_resolver=_multiplier_that_fails("contract_multiplier_sample"),
    )

    assert fills.empty
    assert [row["unpriceable_leg"] for row in skipped] == ["RB2405.SHF"]


def test_a_multiplier_failure_of_another_kind_is_still_fatal():
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(context.slots, {"RB2405.SHF": 100.0, "RB2410.SHF": 200.0})

    with pytest.raises(ValueError, match="roll_fill_unpriceable"):
        build_roll_fills(
            choices=choices,
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={"SHFE": "amount_vwap"},
            multiplier_resolver=_multiplier_that_fails("minute_schema"),
        )


def test_roll_fill_hard_fails_when_a_traded_leg_still_cannot_be_priced():
    """两条腿都有成交却定不出价 —— 那是缺数据或口径错误，不是「没有市场」。"""
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(context.slots, {"RB2405.SHF": 100.0, "RB2410.SHF": 200.0})

    with pytest.raises(ValueError, match="roll_fill_unpriceable"):
        build_roll_fills(
            choices=choices,
            contexts=contexts,
            source=source,
            pricing_basis_by_exchange={"SHFE": "amount_vwap"},
            multiplier_resolver=lambda candidate, frame, **_: 0,
            )


def test_builder_caches_raw_ohlc_and_only_carries_the_adjustment_factor(
    bundle_frames, monkeypatch
):
    raw = bundle_frames["bars"].iloc[[2]].copy()
    raw.loc[:, ["open", "high", "low", "close"]] = [100.0, 110.0, 90.0, 105.0]
    raw.loc[:, "adj_factor"] = 0.5
    monkeypatch.setattr(
        "scripts.commodity.build_panel.adjust_signal_bars",
        lambda frame: pytest.fail("builder applied signal adjustment before caching"),
    )

    row = raw.iloc[0]
    contexts = {
        (row["trade_date"].date(), row["product"]): SimpleNamespace(
            candidate=SimpleNamespace(
                trade_date=row["trade_date"].date(),
                product=row["product"],
                minute_symbol="RB2410",
                daily_contract=row["contract"],
            )
        )
    }
    cached = _bundle_bars(
        raw.assign(contract="RB2410"),
        contexts=contexts,
        dominants=bundle_frames["dominants"].iloc[[2]],
    )

    assert cached.loc[0, ["open", "high", "low", "close"]].tolist() == [
        100.0,
        110.0,
        90.0,
        105.0,
    ]
    assert cached.loc[0, "adj_factor"] == 0.5


def _legacy_bundle_inputs(bundle_frames):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["bars"] = frames["bars"].iloc[:2].copy().reset_index(drop=True)
    frames["dominants"] = frames["dominants"].iloc[:2].copy().reset_index(drop=True)
    trade_date = pd.Timestamp("2023-03-06")
    frames["bars"].loc[:, "trade_date"] = trade_date
    frames["bars"].loc[:, "slot_end"] = pd.DatetimeIndex(
        ["2023-03-06 09:15"] * 2, tz="Asia/Shanghai"
    )
    frames["bars"].loc[:, "fill_time"] = pd.DatetimeIndex(
        ["2023-03-06 09:20"] * 2, tz="Asia/Shanghai"
    )
    frames["bars"].loc[:, "contract"] = ["RB2305", "TA2305"]
    frames["dominants"].loc[:, "trade_date"] = trade_date
    frames["dominants"].loc[:, "selected_from"] = pd.Timestamp("2023-03-03")
    frames["dominants"].loc[:, "contract"] = ["RB2305.SHF", "TA305.CZC"]
    frames["universes"].loc[:, "month_start"] = pd.Timestamp("2023-03-01")
    frames["roll_fills"] = frames["roll_fills"].iloc[:0].copy()
    contexts = {}
    for row in frames["dominants"].itertuples(index=False):
        minute_symbol = "TA2305" if row.product == "TA" else "RB2305"
        key = (row.trade_date.date(), row.product)
        contexts[key] = SimpleNamespace(
            candidate=SimpleNamespace(
                trade_date=key[0],
                product=row.product,
                minute_symbol=minute_symbol,
                daily_contract=row.contract,
            )
        )
    return frames, contexts


def test_bundle_builder_maps_legacy_minute_ids_to_exact_daily_dominants(
    tmp_path, bundle_frames
):
    frames, contexts = _legacy_bundle_inputs(bundle_frames)

    frames["bars"] = _bundle_bars(
        frames["bars"], contexts=contexts, dominants=frames["dominants"]
    )
    write_bundle(tmp_path, **frames)

    assert frames["bars"]["contract"].tolist() == [
        "RB2305.SHF",
        "TA305.CZC",
    ]
    assert read_bundle(tmp_path).bars.equals(frames["bars"])


def test_bundle_builder_rejects_a_missing_contract_identity_mapping(bundle_frames):
    frames, contexts = _legacy_bundle_inputs(bundle_frames)
    contexts.pop((date(2023, 3, 6), "TA"))

    with pytest.raises(ValueError, match="panel_bundle_contract_mapping.*missing"):
        _bundle_bars(frames["bars"], contexts=contexts, dominants=frames["dominants"])


def test_bundle_builder_rejects_an_ambiguous_daily_dominant_mapping(bundle_frames):
    frames, contexts = _legacy_bundle_inputs(bundle_frames)
    duplicate = frames["dominants"].iloc[[0]].copy()
    duplicate.loc[:, "contract"] = "RB2410.SHF"
    ambiguous = pd.concat([frames["dominants"], duplicate], ignore_index=True)

    with pytest.raises(ValueError, match="panel_bundle_contract_mapping.*ambiguous"):
        _bundle_bars(frames["bars"], contexts=contexts, dominants=ambiguous)


def test_bundle_builder_rejects_a_mismatched_minute_contract(bundle_frames):
    frames, contexts = _legacy_bundle_inputs(bundle_frames)
    frames["bars"].loc[frames["bars"]["product"].eq("TA"), "contract"] = "TA305"

    with pytest.raises(
        ValueError, match="panel_bundle_contract_mapping.*minute_contract"
    ):
        _bundle_bars(frames["bars"], contexts=contexts, dominants=frames["dominants"])


def test_bundle_builder_rejects_context_daily_contract_that_disagrees_with_dominant(
    bundle_frames,
):
    frames, contexts = _legacy_bundle_inputs(bundle_frames)
    key = (date(2023, 3, 6), "TA")
    contexts[key].candidate.daily_contract = "TA405.CZC"

    with pytest.raises(
        ValueError, match="panel_bundle_contract_mapping.*daily contract"
    ):
        _bundle_bars(frames["bars"], contexts=contexts, dominants=frames["dominants"])


def test_signal_bars_are_adjusted_without_adjusting_actual_fills(bundle_frames):
    raw = bundle_frames["bars"].iloc[[2]].copy()
    raw.loc[:, ["open", "high", "low", "close"]] = [100.0, 110.0, 90.0, 105.0]
    raw.loc[:, "adj_factor"] = 0.5
    raw.loc[:, "fill_price"] = 103.0

    adjusted = adjust_signal_bars(raw)

    assert adjusted.loc[0, ["open", "high", "low", "close"]].tolist() == [
        50.0,
        55.0,
        45.0,
        52.5,
    ]
    assert adjusted.loc[0, "fill_price"] == 103.0
    assert raw.iloc[0]["close"] == 105.0


def test_shared_builder_cli_accepts_exact_smoke_arguments(tmp_path):
    args = build_parser().parse_args(
        [
            "--start",
            "2023-01-03",
            "--end",
            "2023-01-31",
            "--output-dir",
            str(tmp_path),
            "--settings",
            "/safe/settings.yaml",
        ]
    )

    assert args.start == pd.Timestamp("2023-01-03").date()
    assert args.end == pd.Timestamp("2023-01-31").date()
    assert args.output_dir == tmp_path
    assert args.settings == Path("/safe/settings.yaml")


def test_shared_builder_takes_the_session_asset_as_an_argument(tmp_path):
    """同一个 main 服务两个消费者：连续信号读自己的资产，商品复刻读自己的那份。
    把资产写死在模块常量里，就等于让其中一个消费者悄悄读错时段规则。"""
    required = ["--start", "2023-01-03", "--end", "2023-01-31", "--output-dir", str(tmp_path)]

    default = build_parser().parse_args(required)
    chosen = build_parser().parse_args(
        [*required, "--session-rules", "/safe/commodity_minute_sessions.csv"]
    )

    assert default.session_rules.name == "continuous_minute_sessions.csv"
    assert chosen.session_rules == Path("/safe/commodity_minute_sessions.csv")


def test_legacy_continuous_builder_delegates_to_shared_entry_point():
    import scripts.commodity.build_panel as shared_builder
    import scripts.continuous.build_panel as legacy_builder

    assert legacy_builder.main is shared_builder.main


def test_builder_keeps_shadow_panel_contexts_outside_monthly_universe():
    rb_choices = _roll_choices()
    ta_choices = (
        DominantChoice(
            trade_date=ROLL_DATES[0],
            product="TA",
            contract="TA405.CZC",
            oi=100,
            volume=90,
            selected_from=pd.Timestamp("2024-03-04").date(),
        ),
        DominantChoice(
            trade_date=ROLL_DATES[1],
            product="TA",
            contract="TA405.CZC",
            oi=110,
            volume=100,
            selected_from=ROLL_DATES[0],
        ),
    )

    contexts = __import__(
        "scripts.commodity.build_panel", fromlist=["_target_contexts"]
    )._target_contexts(
        choices=(*rb_choices, *ta_choices),
        rules=[
            SessionRule.day_only("SHFE", "RB", version="commodity-v1"),
            SessionRule.day_only("CZCE", "TA", version="commodity-v1"),
        ],
        start=ROLL_DATES[1],
        end=ROLL_DATES[1],
    )

    assert set(contexts) == {
        (ROLL_DATES[1], "RB"),
        (ROLL_DATES[1], "TA"),
    }


def test_write_refuses_to_replace_bundle_with_different_inputs(
    tmp_path, bundle_frames, monkeypatch
):
    write_bundle(tmp_path, **bundle_frames, inputs={"start": "2024-03-05"})
    original = {
        path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()
    }

    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda *args, **kwargs: pytest.fail("incompatible republish touched files"),
    )
    with pytest.raises(ValueError, match="bundle_input_mismatch"):
        write_bundle(tmp_path, **bundle_frames, inputs={"start": "2024-03-06"})

    assert {
        path.name: path.read_bytes() for path in tmp_path.iterdir() if path.is_file()
    } == original


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("open", float("inf")),
        ("open_interest", float("-inf")),
        ("fill_price", float("inf")),
        ("volume", -1.0),
    ],
)
def test_bundle_refuses_semantically_invalid_bar_values(
    tmp_path, bundle_frames, column, value
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["bars"].loc[0, column] = value

    with pytest.raises(ValueError, match="bundle_schema_value"):
        write_bundle(tmp_path, **frames)


def test_legacy_month_end_clamps_only_to_same_month_rule_authority():
    builder = __import__("scripts.commodity.build_panel", fromlist=["_resolve_end"])
    reliable = pd.Timestamp("2026-01-30").date()

    assert (
        builder._resolve_end(
            pd.Timestamp("2026-01-31").date(),
            reliable_end=reliable,
            legacy_month=True,
        )
        == reliable
    )
    with pytest.raises(ValueError, match="panel_session_authority"):
        builder._resolve_end(
            pd.Timestamp("2026-01-31").date(),
            reliable_end=reliable,
            legacy_month=False,
        )


def test_input_frame_digest_is_order_independent_and_content_sensitive():
    builder = __import__("scripts.commodity.build_panel", fromlist=["_frame_sha256"])
    left = pd.DataFrame(
        {
            "trade_date": pd.to_datetime(["2024-03-05", "2024-03-06"]).date,
            "symbol": ["RB2405.SHF", "RB2405.SHF"],
            "close": [100.0, 101.0],
        }
    )
    reordered = left.iloc[::-1].reset_index(drop=True)
    changed = left.copy()
    changed.loc[1, "close"] = 102.0

    assert builder._frame_sha256(left) == builder._frame_sha256(reordered)
    assert builder._frame_sha256(left) != builder._frame_sha256(changed)


def test_copy_daily_returns_validated_production_frame():
    builder = __import__("scripts.commodity.build_panel", fromlist=["_copy_daily"])

    class Cursor:
        sql = None

        def copy_expert(self, sql, buffer):
            self.sql = sql
            buffer.write(
                "symbol,trade_date,oi,volume,turnover,close\n"
                "RB2405.SHF,2024-03-05,100,90,900000,101.5\n"
            )

    cursor = Cursor()
    frame = builder._copy_daily(cursor, end=pd.Timestamp("2024-03-06").date())

    assert list(frame.columns) == [
        "symbol",
        "trade_date",
        "oi",
        "volume",
        "turnover",
        "close",
    ]
    assert frame.loc[0, "trade_date"] == pd.Timestamp("2024-03-05").date()
    assert "trade_date < DATE '2024-03-07'" in cursor.sql


def _bundle_bytes(directory):
    return {
        path.name: path.read_bytes() for path in directory.iterdir() if path.is_file()
    }


def _crash_after_first_bundle_table(directory, frames):
    original = Path.replace

    def replace_then_exit(source, target):
        result = original(source, target)
        if Path(target).name == TABLE_FILES["bars"]:
            os._exit(73)
        return result

    Path.replace = replace_then_exit
    write_bundle(directory, **frames)


def test_child_crash_after_first_table_rename_recovers_on_retry(
    tmp_path, bundle_frames
):
    output = tmp_path / "crashed-generation"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_after_first_bundle_table,
        args=(output, bundle_frames),
    )
    process.start()
    process.join(timeout=15)

    assert not process.is_alive()
    assert process.exitcode == 73
    assert (output / ".incomplete-generation.json").is_file()
    assert (output / TABLE_FILES["bars"]).is_file()
    assert not (output / "manifest.json").exists()

    loaded = write_bundle(output, **bundle_frames)

    assert loaded.bars.equals(bundle_frames["bars"])
    assert read_bundle(output).bars.equals(bundle_frames["bars"])
    assert not (output / ".incomplete-generation.json").exists()


def test_month_checkpoint_resumes_after_later_failure_without_rebuilding_completed(
    tmp_path, bundle_frames, monkeypatch
):
    import scripts.commodity.build_panel as builder_module

    template = normalise_panel(bundle_frames["bars"])
    january = PanelMonthChunk(
        month_start=pd.Timestamp("2024-01-01").date(),
        bars=template.iloc[:1].copy(),
        pending=template.iloc[1:2].reset_index(drop=True),
    )
    february = PanelMonthChunk(
        month_start=pd.Timestamp("2024-02-01").date(),
        bars=template.iloc[1:2].reset_index(drop=True),
        pending=template.iloc[:1].reset_index(drop=True),
    )
    calls = []

    def fail_later(**kwargs):
        calls.append((kwargs["resume_after"], kwargs["initial_pending"]))
        yield january
        raise RuntimeError("later-month failure")

    monkeypatch.setattr(builder_module, "iter_panel_months", fail_later)
    checkpoint = tmp_path / "checkpoint"
    with pytest.raises(RuntimeError, match="later-month failure"):
        _build_panel_checkpointed(
            contexts={},
            source=object(),
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame, **_: 10,
            adjustment_factor_by_key={},
            continuity_segment_by_key={},
            checkpoint_directory=checkpoint,
            checkpoint_key="a" * 64,
        )

    def resume(**kwargs):
        calls.append((kwargs["resume_after"], kwargs["initial_pending"]))
        yield february

    monkeypatch.setattr(builder_module, "iter_panel_months", resume)
    rebuilt = _build_panel_checkpointed(
        contexts={},
        source=object(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame, **_: 10,
        adjustment_factor_by_key={},
            continuity_segment_by_key={},
        checkpoint_directory=checkpoint,
        checkpoint_key="a" * 64,
    )

    assert calls[0] == (None, None)
    assert calls[1][0] == january.month_start
    assert calls[1][1].equals(january.pending)
    assert len(rebuilt) == len(january.bars) + len(february.bars) + len(february.pending)
    with pytest.raises(ValueError, match="panel_checkpoint_mismatch"):
        _build_panel_checkpointed(
            contexts={},
            source=object(),
            pricing_basis_by_exchange={},
            multiplier_resolver=lambda candidate, frame, **_: 10,
            adjustment_factor_by_key={},
            continuity_segment_by_key={},
            checkpoint_directory=checkpoint,
            checkpoint_key="b" * 64,
        )


def _crash_checkpoint_after_first_month_file(directory, chunk):
    import scripts.commodity.build_panel as builder_module

    builder_module.iter_panel_months = lambda **kwargs: iter((chunk,))
    original = builder_module._checkpoint_frame
    calls = 0

    def crash(frame, path):
        nonlocal calls
        result = original(frame, path)
        calls += 1
        if calls == 1:
            os._exit(74)
        return result

    builder_module._checkpoint_frame = crash
    builder_module._build_panel_checkpointed(
        contexts={},
        source=object(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame, **_: 10,
        adjustment_factor_by_key={},
            continuity_segment_by_key={},
        checkpoint_directory=directory,
        checkpoint_key="c" * 64,
    )


def test_checkpoint_recovers_after_child_exit_during_first_month_file(
    tmp_path, bundle_frames, monkeypatch
):
    import scripts.commodity.build_panel as builder_module

    template = normalise_panel(bundle_frames["bars"])
    chunk = PanelMonthChunk(
        month_start=pd.Timestamp("2024-01-01").date(),
        bars=template.iloc[:1].reset_index(drop=True),
        pending=template.iloc[1:2].reset_index(drop=True),
    )
    checkpoint = tmp_path / "hard-crash-checkpoint"
    process = multiprocessing.get_context("fork").Process(
        target=_crash_checkpoint_after_first_month_file,
        args=(checkpoint, chunk),
    )
    process.start()
    process.join(timeout=15)
    assert process.exitcode == 74

    monkeypatch.setattr(
        builder_module, "iter_panel_months", lambda **kwargs: iter((chunk,))
    )
    recovered = builder_module._build_panel_checkpointed(
        contexts={},
        source=object(),
        pricing_basis_by_exchange={},
        multiplier_resolver=lambda candidate, frame, **_: 10,
        adjustment_factor_by_key={},
            continuity_segment_by_key={},
        checkpoint_directory=checkpoint,
        checkpoint_key="c" * 64,
    )

    assert len(recovered) == len(chunk.bars) + len(chunk.pending)

def test_compatible_republish_is_a_byte_identical_no_op(
    tmp_path, bundle_frames, monkeypatch
):
    metadata = {
        "inputs": {"minute_content_sha256": "a" * 64},
        "provenance": {"effective_config_sha256": "b" * 64},
    }
    write_bundle(tmp_path, **bundle_frames, **metadata)
    original = _bundle_bytes(tmp_path)
    changed = {name: frame.copy() for name, frame in bundle_frames.items()}
    changed["bars"].loc[0, "close"] += 99

    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda *args, **kwargs: pytest.fail("compatible republish rewrote a table"),
    )
    loaded = write_bundle(tmp_path, **changed, **metadata)

    assert loaded.bars.equals(bundle_frames["bars"])
    assert _bundle_bytes(tmp_path) == original


def test_invalid_existing_bundle_is_not_repaired(tmp_path, bundle_frames, monkeypatch):
    write_bundle(tmp_path, **bundle_frames, inputs={"source": "same"})
    (tmp_path / TABLE_FILES["bars"]).write_bytes(b"tampered")

    monkeypatch.setattr(
        pd.DataFrame,
        "to_parquet",
        lambda *args, **kwargs: pytest.fail("invalid existing bundle was rewritten"),
    )
    with pytest.raises(ValueError, match="bundle_digest_mismatch"):
        write_bundle(tmp_path, **bundle_frames, inputs={"source": "same"})


def test_new_bundle_stages_all_tables_before_publishing(
    tmp_path, bundle_frames, monkeypatch
):
    original = pd.DataFrame.to_parquet
    calls = 0

    def fail_third(frame, path, *args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 3:
            raise RuntimeError("injected parquet staging failure")
        return original(frame, path, *args, **kwargs)

    monkeypatch.setattr(pd.DataFrame, "to_parquet", fail_third)
    with pytest.raises(RuntimeError, match="injected parquet staging failure"):
        write_bundle(tmp_path, **bundle_frames)

    assert not (tmp_path / "manifest.json").exists()
    assert not any((tmp_path / filename).exists() for filename in TABLE_FILES.values())
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize(
    ("table", "column"),
    [
        ("bars", "trade_date"),
        ("universes", "month_start"),
        ("dominants", "trade_date"),
        ("dominants", "selected_from"),
        ("roll_fills", "trade_date"),
    ],
)
@pytest.mark.parametrize(
    "invalid",
    [
        pd.Timestamp("2024-03-05 12:00:00"),
        pd.Timestamp("2024-03-05", tz="Asia/Shanghai"),
    ],
)
def test_bundle_date_columns_reject_non_dates(
    tmp_path, bundle_frames, table, column, invalid
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames[table][column] = frames[table][column].astype(object)
    frames[table].loc[0, column] = invalid

    with pytest.raises(ValueError, match="bundle_schema_dtype"):
        write_bundle(tmp_path, **frames)


def test_bundle_date_columns_accept_python_dates_and_naive_midnight(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    date_columns = (
        ("bars", "trade_date"),
        ("universes", "month_start"),
        ("dominants", "trade_date"),
        ("dominants", "selected_from"),
        ("roll_fills", "trade_date"),
    )
    for table, column in date_columns:
        frames[table][column] = [
            pd.Timestamp(value).date()
            if index % 2 == 0
            else pd.Timestamp(value).normalize()
            for index, value in enumerate(frames[table][column])
        ]

    loaded = write_bundle(tmp_path, **frames)
    for table, column in date_columns:
        assert str(getattr(loaded, table)[column].dtype) == "datetime64[ns]"


def _minute_digest(price, *, reverse=False):
    class Source(_RollSource):
        def iter_month(self, candidates, lower, upper):
            for frame in super().iter_month(candidates, lower, upper):
                if reverse:
                    frame = frame.iloc[::-1].reset_index(drop=True)
                yield frame

    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = DigestingMinuteSource(
        Source(context.slots, {"RB2410.SHF": price}),
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    source.set_phase("roll_fills")
    candidate = _roll_candidate(choices[1], context, role="roll_new")
    chunks = list(
        source.iter_month([candidate], candidate.window_start, candidate.window_end)
    )
    assert chunks
    return source.minute_content_sha256


def _minute_open_digest(value):
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]

    class Source(_RollSource):
        def iter_month(self, candidates, lower, upper):
            for frame in super().iter_month(candidates, lower, upper):
                frame.loc[0, "open"] = value
                yield frame

    source = DigestingMinuteSource(
        Source(context.slots, {"RB2410.SHF": 1.0}),
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    source.set_phase("bars")
    candidate = _roll_candidate(choices[1], context, role="panel")
    list(source.iter_month([candidate], candidate.window_start, candidate.window_end))
    return source.minute_content_sha256


def test_minute_digest_preserves_adjacent_ieee_754_values():
    one = 1.0
    adjacent = math.nextafter(one, 2.0)

    assert one != adjacent
    assert _minute_open_digest(one) != _minute_open_digest(adjacent)


def test_minute_digest_is_content_sensitive_with_equal_rows_and_candidates():
    original = _minute_digest(200.0)
    assert original == _minute_digest(200.0, reverse=True)
    assert original != _minute_digest(201.0)


def test_minute_digest_records_labeled_roll_and_bar_requests():
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = DigestingMinuteSource(
        _RollSource(context.slots, {"RB2410.SHF": 200.0}),
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    candidate = _roll_candidate(choices[1], context, role="roll_new")
    for phase in ("roll_fills", "bars"):
        source.set_phase(phase)
        list(
            source.iter_month([candidate], candidate.window_start, candidate.window_end)
        )

    assert [item["phase"] for item in source.minute_request_digests] == [
        "roll_fills",
        "bars",
    ]


def test_minute_row_encoding_is_streamed_instead_of_materialized():
    import scripts.commodity.build_panel as builder_module

    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    candidate = _roll_candidate(choices[1], context, role="panel")
    frame = next(
        _RollSource(context.slots, {"RB2410.SHF": 200.0}).iter_month(
            [candidate], candidate.window_start, candidate.window_end
        )
    )

    payloads = builder_module._minute_row_payloads(frame)

    assert not isinstance(payloads, list)
    assert iter(payloads) is payloads
    assert sum(1 for _ in payloads) == len(frame)


def test_effective_config_digest_redacts_credentials_and_tracks_safe_settings(tmp_path):
    sessions = tmp_path / "sessions.csv"
    pricing = tmp_path / "pricing.csv"
    sessions.write_text("safe session rules\n", encoding="utf-8")
    pricing.write_text("safe pricing rules\n", encoding="utf-8")
    first = {
        "database": {
            "host": "db.internal",
            "port": 5432,
            "user": "alice",
            "password": "top-secret-one",
        },
        "research": {"turnover_window": 20},
    }
    different_secret = {
        **first,
        "database": {
            **first["database"],
            "user": "bob",
            "password": "secret-two",
        },
    }
    different_safe = {**first, "research": {"turnover_window": 21}}
    source_revision = {
        "git_head": "a" * 40,
        "production_python_sha256": "b" * 64,
    }

    digest, safe = _effective_config_sha256(
        first, sessions, pricing, source_revision=source_revision
    )
    secret_digest, secret_safe = _effective_config_sha256(
        different_secret,
        sessions,
        pricing,
        source_revision=source_revision,
    )
    safe_digest, _ = _effective_config_sha256(
        different_safe, sessions, pricing, source_revision=source_revision
    )
    source_digest, _ = _effective_config_sha256(
        first,
        sessions,
        pricing,
        source_revision={**source_revision, "production_python_sha256": "c" * 64},
    )

    assert digest == secret_digest
    assert digest != safe_digest
    assert digest != source_digest
    encoded = json.dumps(safe, sort_keys=True)
    assert safe == secret_safe
    assert "alice" not in encoded and "top-secret-one" not in encoded
    assert "user" not in encoded and "password" not in encoded


def test_effective_config_digest_ignores_secret_bearing_values_under_safe_keys(
    tmp_path,
):
    sessions = tmp_path / "sessions.csv"
    pricing = tmp_path / "pricing.csv"
    sessions.write_text("safe session rules\n", encoding="utf-8")
    pricing.write_text("safe pricing rules\n", encoding="utf-8")
    revision = {
        "git_head": "a" * 40,
        "production_python_sha256": "b" * 64,
    }
    first = {
        "database": {"endpoint": "postgresql://alice:secret-one@db.internal/market"},
        "headers": ["Bearer abcdefghijklmnopqrstuvwxyz"],
        "research": {"window": 20},
    }
    changed = {
        "database": {"endpoint": "postgresql://bob:secret-two@db.internal/market"},
        "headers": ["Bearer zyxwvutsrqponmlkjihgfedcba"],
        "research": {"window": 20},
    }

    digest, safe = _effective_config_sha256(
        first, sessions, pricing, source_revision=revision
    )
    changed_digest, changed_safe = _effective_config_sha256(
        changed, sessions, pricing, source_revision=revision
    )

    assert digest == changed_digest
    assert safe == changed_safe
    emitted = json.dumps(safe, sort_keys=True)
    assert "alice" not in emitted and "secret-one" not in emitted
    assert "Bearer" not in emitted


def test_publish_failure_never_leaves_a_manifest_for_partial_tables(
    tmp_path, bundle_frames, monkeypatch
):
    original = Path.replace

    def fail_dominants(source, target):
        if Path(target).name == TABLE_FILES["dominants"]:
            raise RuntimeError("injected publication failure")
        return original(source, target)

    monkeypatch.setattr(Path, "replace", fail_dominants)
    with pytest.raises(RuntimeError, match="injected publication failure"):
        write_bundle(tmp_path, **bundle_frames)

    assert not (tmp_path / "manifest.json").exists()
    assert not any((tmp_path / filename).exists() for filename in TABLE_FILES.values())
    assert not list(tmp_path.glob(".*.tmp"))
    with pytest.raises(ValueError, match="bundle_manifest_missing"):
        read_bundle(tmp_path)

    monkeypatch.setattr(Path, "replace", original)
    write_bundle(tmp_path, **bundle_frames)
    assert read_bundle(tmp_path).bars.equals(bundle_frames["bars"])


def test_exception_reported_after_manifest_rename_keeps_committed_bundle(
    tmp_path, bundle_frames, monkeypatch
):
    original = Path.replace

    def raise_after_manifest_rename(source, target):
        result = original(source, target)
        if Path(target).name == "manifest.json":
            raise RuntimeError("injected post-commit exception")
        return result

    monkeypatch.setattr(Path, "replace", raise_after_manifest_rename)
    with pytest.raises(RuntimeError, match="injected post-commit exception"):
        write_bundle(tmp_path, **bundle_frames)

    assert read_bundle(tmp_path).bars.equals(bundle_frames["bars"])
    monkeypatch.setattr(Path, "replace", original)
    assert write_bundle(tmp_path, **bundle_frames).bars.equals(bundle_frames["bars"])


def test_exception_reported_after_table_rename_cleans_for_retry(
    tmp_path, bundle_frames, monkeypatch
):
    original = Path.replace

    def raise_after_bars_rename(source, target):
        result = original(source, target)
        if Path(target).name == TABLE_FILES["bars"]:
            raise RuntimeError("injected post-table-rename exception")
        return result

    monkeypatch.setattr(Path, "replace", raise_after_bars_rename)
    with pytest.raises(RuntimeError, match="injected post-table-rename exception"):
        write_bundle(tmp_path, **bundle_frames)

    assert not (tmp_path / "manifest.json").exists()
    assert not any((tmp_path / filename).exists() for filename in TABLE_FILES.values())
    monkeypatch.setattr(Path, "replace", original)
    assert write_bundle(tmp_path, **bundle_frames).bars.equals(bundle_frames["bars"])


def test_same_process_writers_are_serialized_to_one_publication(
    tmp_path, bundle_frames, monkeypatch
):
    import common.commodity.bundle as bundle_module

    output = tmp_path / "new-bundle"
    original = bundle_module._stage_parquet
    entered = threading.Event()
    release = threading.Event()
    staged = []
    errors = []
    results = []

    def slow_first_stage(frame, path):
        staged.append(Path(path).name)
        if not entered.is_set():
            entered.set()
            assert release.wait(timeout=5)
        return original(frame, path)

    def publish():
        try:
            results.append(
                write_bundle(
                    output,
                    **bundle_frames,
                    inputs={"generation": "same"},
                )
            )
        except Exception as exc:  # pragma: no cover - assertion reports details
            errors.append(exc)

    monkeypatch.setattr(bundle_module, "_stage_parquet", slow_first_stage)
    first = threading.Thread(target=publish)
    second = threading.Thread(target=publish)
    first.start()
    assert entered.wait(timeout=5)
    second.start()
    release.set()
    first.join(timeout=10)
    second.join(timeout=10)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(results) == 2
    assert len(staged) == len(TABLE_FILES)
    assert all(
        any(name.startswith(f".{filename}.") for filename in TABLE_FILES.values())
        and name.endswith(".tmp")
        for name in staged
    )
    assert read_bundle(output).bars.equals(bundle_frames["bars"])


def test_source_revision_hashes_all_production_python_without_emitting_contents(
    tmp_path, monkeypatch
):
    import scripts.commodity.build_panel as builder_module

    def fixed_git_metadata(root, *arguments):
        if arguments[0] == "ls-files":
            return (
                b"scripts/commodity/build_panel.py\0"
                b"common/commodity/universe.py\0"
                b"tests/test_universe.py\0"
            )
        return ("a" * 40 + "\n").encode("ascii")

    monkeypatch.setattr(builder_module, "_git_command", fixed_git_metadata)
    builder = tmp_path / "scripts" / "commodity" / "build_panel.py"
    dependency = tmp_path / "common" / "commodity" / "universe.py"
    ignored_test = tmp_path / "tests" / "test_universe.py"
    for path, content in (
        (builder, "BUILDER_SENTINEL = 1\n"),
        (dependency, "DEPENDENCY_SECRET_SENTINEL = 1\n"),
        (ignored_test, "IGNORED_TEST_SENTINEL = 1\n"),
    ):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    first = _source_revision(tmp_path)
    ignored_test.write_text("IGNORED_TEST_SENTINEL = 2\n", encoding="utf-8")
    ignored_change = _source_revision(tmp_path)
    dependency.write_text("DEPENDENCY_SECRET_SENTINEL = 2\n", encoding="utf-8")
    dependency_change = _source_revision(tmp_path)

    assert first == ignored_change
    assert dependency_change["git_head"] == first["git_head"]
    assert (
        first["production_python_sha256"]
        != (dependency_change["production_python_sha256"])
    )
    assert first["git_head"] == "a" * 40
    assert set(first) == {"git_head", "production_python_sha256"}
    emitted = json.dumps(first, sort_keys=True)
    assert "SENTINEL" not in emitted
    assert str(dependency) not in emitted


def _bar_multiplier_digest(multiplier):
    choice = _roll_choices()[1]
    contexts = build_contexts(
        [choice],
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(choice.trade_date, choice.product)]
    source = _RollSource(context.slots, {choice.contract: 200.0})
    resolver = DigestingMultiplierResolver(
        lambda candidate, frame, **_: multiplier,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    resolver.set_phase("bars")
    bars = build_commodity_panel(
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
        multiplier_resolver=resolver,
        adjustment_factor_by_key={(choice.trade_date, choice.product): 1.0},
        continuity_segment_by_key={(choice.trade_date, choice.product): 0},
    )
    bars = _bundle_bars(
        bars,
        contexts=contexts,
        dominants=pd.DataFrame(
            {
                "trade_date": [choice.trade_date],
                "product": [choice.product],
                "contract": [choice.contract],
            }
        ),
    )
    resolver.assert_complete(bars=bars, roll_fills=pd.DataFrame())
    assert not bars.empty
    return resolver.multiplier_resolutions_sha256


def test_bar_multiplier_resolution_changes_provenance_with_same_minutes():
    assert _bar_multiplier_digest(10) != _bar_multiplier_digest(11)


def _roll_multiplier_digest(old_multiplier, new_multiplier):
    choices = _roll_choices()
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(ROLL_DATES[1], "RB")]
    source = _RollSource(
        context.slots,
        {"RB2405.SHF": 100.0, "RB2410.SHF": 200.0},
    )
    multipliers = {
        "RB2405.SHF": old_multiplier,
        "RB2410.SHF": new_multiplier,
    }
    resolver = DigestingMultiplierResolver(
        lambda candidate, frame, **_: multipliers[candidate.daily_contract],
        pricing_basis_by_exchange={"SHFE": "ohlc_typical"},
    )
    resolver.set_phase("roll_fills")
    fills, _skipped = build_roll_fills(
        choices=choices,
        contexts=contexts,
        source=source,
        pricing_basis_by_exchange={"SHFE": "ohlc_typical"},
        multiplier_resolver=resolver,
    )
    resolver.assert_complete(bars=pd.DataFrame(), roll_fills=fills)
    return resolver.multiplier_resolutions_sha256


def test_each_roll_leg_multiplier_changes_provenance_with_same_minutes():
    original = _roll_multiplier_digest(10, 10)

    assert original != _roll_multiplier_digest(11, 10)
    assert original != _roll_multiplier_digest(10, 11)


def _audited_multiplier_digest(*, source_name, price):
    choice = _roll_choices()[1]
    contexts = build_contexts(
        [choice],
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(choice.trade_date, choice.product)]
    candidate = _roll_candidate(choice, context, role="panel")
    frame = next(
        _RollSource(context.slots, {choice.contract: price}).iter_month(
            [candidate], candidate.window_start, candidate.window_end
        )
    )
    resolution = MultiplierResolution(
        multiplier=10,
        source=source_name,
        sample_rows=5,
        pass_rate=1.0,
        sample_dates=1,
        sample_start=context.slots[0],
        sample_end=context.slots[4],
        max_range_error=0.0,
    )
    resolver = DigestingMultiplierResolver(
        lambda candidate, frame, **_: resolution,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    resolver.set_phase("bars")
    assert resolver(candidate, frame) == 10
    return resolver.multiplier_resolutions_sha256


def test_multiplier_provenance_hashes_full_resolution_and_frame_evidence():
    original = _audited_multiplier_digest(source_name="metadata", price=200.0)

    assert original != _audited_multiplier_digest(
        source_name="daily_inference", price=200.0
    )
    assert original != _audited_multiplier_digest(source_name="metadata", price=201.0)


def test_metadata_multiplier_resolution_runs_for_each_consuming_frame():
    import scripts.commodity.build_panel as builder_module

    choice = _roll_choices()[1]
    contexts = build_contexts(
        [choice],
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(choice.trade_date, choice.product)]
    candidate = _roll_candidate(choice, context, role="panel")
    short = (
        next(
            _RollSource(context.slots, {choice.contract: 200.0}).iter_month(
                [candidate], candidate.window_start, context.slots[4]
            )
        )
        .iloc[:3]
        .copy()
    )
    full = next(
        _RollSource(context.slots, {choice.contract: 200.0}).iter_month(
            [candidate], candidate.window_start, candidate.window_end
        )
    )

    class Source:
        def __init__(self):
            self.calls = []

        def resolve_metadata_multiplier(self, **kwargs):
            self.calls.append(kwargs)
            return MultiplierResolution(
                multiplier=10,
                source="metadata",
                sample_rows=len(kwargs["frame"]),
                pass_rate=1.0,
                sample_dates=1,
            )

    source = Source()
    first = builder_module._metadata_multiplier_resolution(
        source,
        {"SHFE": "amount_vwap"},
        candidate,
        short,
    )
    second = builder_module._metadata_multiplier_resolution(
        source,
        {"SHFE": "amount_vwap"},
        candidate,
        full,
    )

    assert len(source.calls) == 2
    assert source.calls[0]["frame"] is short
    assert source.calls[0]["inference_frame"] is short
    assert source.calls[1]["frame"] is full
    assert source.calls[1]["inference_frame"] is full
    assert first.sample_rows != second.sample_rows


def test_metadata_multiplier_remote_resolution_is_bounded_by_concrete_contract():
    import scripts.commodity.build_panel as builder_module

    roll_choices = _roll_choices()
    choices = (
        roll_choices[0],
        dataclasses.replace(
            roll_choices[1],
            contract=roll_choices[0].contract,
        ),
    )
    contexts = build_contexts(
        choices,
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    calls = []

    class Source:
        def resolve_metadata_multiplier(self, **kwargs):
            calls.append(kwargs)
            return MultiplierResolution(
                multiplier=10,
                source="metadata",
                sample_rows=0,
                pass_rate=float("nan"),
                sample_dates=0,
            )

    resolver = builder_module.CachingMetadataMultiplierResolver(
        Source(),
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    for choice in choices:
        context = contexts[(choice.trade_date, choice.product)]
        candidate = _roll_candidate(choice, context, role="panel")
        frame = next(
            _RollSource(context.slots, {choice.contract: 200.0}).iter_month(
                [candidate], candidate.window_start, candidate.window_end
            )
        )
        assert resolver(candidate, frame).multiplier == 10

    assert len(calls) == 1


def test_metadata_multiplier_cache_separates_contracts_and_pricing_basis():
    import scripts.commodity.build_panel as builder_module

    choice = _roll_choices()[1]
    context = build_contexts(
        [choice],
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )[(choice.trade_date, choice.product)]
    base = _roll_candidate(choice, context, role="roll_new")
    old = dataclasses.replace(
        base,
        daily_contract="RB2405.SHF",
        minute_symbol="RB2405",
        candidate_role="roll_old",
    )
    calls = []

    class Source:
        def resolve_metadata_multiplier(self, **kwargs):
            calls.append(kwargs)
            return MultiplierResolution(
                multiplier=10,
                source="metadata",
                sample_rows=0,
                pass_rate=float("nan"),
                sample_dates=0,
            )

    source = Source()
    amount = builder_module.CachingMetadataMultiplierResolver(
        source,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    typical = builder_module.CachingMetadataMultiplierResolver(
        source,
        pricing_basis_by_exchange={"SHFE": "ohlc_typical"},
    )
    frames = {
        candidate.daily_contract: next(
            _RollSource(
                context.slots, {candidate.daily_contract: 200.0}
            ).iter_month([candidate], candidate.window_start, candidate.window_end)
        )
        for candidate in (old, base)
    }
    for candidate in (old, base, old, base):
        amount(candidate, frames[candidate.daily_contract])
    typical(base, frames[base.daily_contract])

    assert len(calls) == 3
    assert [call["daily_contract"] for call in calls[:2]] == [
        "RB2405.SHF",
        "RB2410.SHF",
    ]
    assert calls[2]["pricing_basis"] == "ohlc_typical"


def test_multiplier_cache_separates_roll_from_earlier_bars_and_concrete_czce():
    import scripts.commodity.build_panel as builder_module

    calls = []

    class Source:
        def resolve_metadata_multiplier(self, **kwargs):
            calls.append((kwargs["daily_contract"], kwargs["trade_date"]))
            return MultiplierResolution(
                multiplier=10,
                source="metadata",
                sample_rows=0,
                pass_rate=float("nan"),
                sample_dates=0,
            )

    resolver = builder_module.CachingMetadataMultiplierResolver(
        Source(), pricing_basis_by_exchange={"SHFE": "amount_vwap", "CZCE": "ohlc_typical"}
    )
    choice = _roll_choices()[0]
    context = build_contexts(
        [choice], rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")]
    )[(choice.trade_date, choice.product)]
    candidate = _roll_candidate(choice, context, role="roll_old")
    resolver.set_phase("roll_fills")
    resolver(candidate, pd.DataFrame())
    resolver.set_phase("bars")
    resolver(dataclasses.replace(candidate, candidate_role="dominant"), pd.DataFrame())

    czc_base = dataclasses.replace(
        candidate,
        product="TA",
        daily_contract="TA405.CZC",
        exchange="CZCE",
        minute_symbol="TA1405",
        trade_date=pd.Timestamp("2014-03-05").date(),
    )
    resolver(czc_base, pd.DataFrame())
    resolver(
        dataclasses.replace(
            czc_base,
            minute_symbol="TA2405",
            trade_date=pd.Timestamp("2024-03-05").date(),
        ),
        pd.DataFrame(),
    )

    assert len(calls) == 4


def test_cached_multiplier_rejects_single_day_range_contradiction():
    import scripts.commodity.build_panel as builder_module

    choice = _roll_choices()[0]
    context = build_contexts(
        [choice], rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")]
    )[(choice.trade_date, choice.product)]
    candidate = _roll_candidate(choice, context, role="dominant")
    frame = next(
        _RollSource(context.slots, {choice.contract: 200.0}).iter_month(
            [candidate], candidate.window_start, candidate.window_end
        )
    )

    class Source:
        def resolve_metadata_multiplier(self, **kwargs):
            return MultiplierResolution(
                multiplier=10,
                source="metadata",
                sample_rows=0,
                pass_rate=float("nan"),
                sample_dates=0,
            )

    resolver = builder_module.CachingMetadataMultiplierResolver(
        Source(), pricing_basis_by_exchange={"SHFE": "amount_vwap"}
    )
    resolver(candidate, frame)
    boundary = frame.copy()
    boundary["amount"] = (
        boundary["high"] + boundary["high"].abs() * 5e-7
    ) * boundary["volume"] * 10
    assert resolver(candidate, boundary).multiplier == 10
    contradicted = frame.copy()
    contradicted["amount"] *= 2
    with pytest.raises(ValueError, match="panel_multiplier_cached_conflict"):
        resolver(candidate, contradicted)


def test_multiplier_provenance_fails_when_a_used_contract_was_not_recorded(
    bundle_frames,
):
    resolver = DigestingMultiplierResolver(
        lambda candidate, frame, **_: 10,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    resolver.set_phase("bars")

    with pytest.raises(ValueError, match="panel_multiplier_provenance_missing"):
        resolver.assert_complete(
            bars=bundle_frames["bars"],
            roll_fills=bundle_frames["roll_fills"],
        )


def test_multiplier_completeness_requires_each_used_contract_date():
    choice = _roll_choices()[1]
    contexts = build_contexts(
        [choice],
        rules=[SessionRule.day_only("SHFE", "RB", version="commodity-v1")],
    )
    context = contexts[(choice.trade_date, choice.product)]
    candidate = _roll_candidate(choice, context, role="panel")
    frame = next(
        _RollSource(context.slots, {choice.contract: 200.0}).iter_month(
            [candidate], candidate.window_start, candidate.window_end
        )
    )
    resolver = DigestingMultiplierResolver(
        lambda candidate, frame, **_: 10,
        pricing_basis_by_exchange={"SHFE": "amount_vwap"},
    )
    resolver.set_phase("bars")
    resolver(candidate, frame)
    bars = pd.DataFrame(
        {
            "trade_date": [choice.trade_date + pd.Timedelta(days=1)],
            "contract": [choice.contract],
            "multiplier": [10],
        }
    )

    with pytest.raises(ValueError, match="panel_multiplier_provenance_missing"):
        resolver.assert_complete(bars=bars, roll_fills=pd.DataFrame())


@pytest.mark.parametrize("table", list(TABLE_FILES))
def test_bundle_rejects_duplicate_primary_keys(tmp_path, bundle_frames, table):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames[table] = pd.concat(
        [frames[table], frames[table].iloc[[0]]], ignore_index=True
    )

    with pytest.raises(ValueError, match="bundle_primary_key"):
        write_bundle(tmp_path, **frames)


def test_bar_primary_key_does_not_depend_on_contract_payload(tmp_path, bundle_frames):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    duplicate = frames["bars"].iloc[[0]].copy()
    duplicate["contract"] = "RB9999.SHF"
    frames["bars"] = pd.concat([frames["bars"], duplicate], ignore_index=True)

    with pytest.raises(ValueError, match="bundle_primary_key.*bars"):
        write_bundle(tmp_path, **frames)


def test_bundle_rejects_bar_contract_that_disagrees_with_dominant(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["bars"].loc[0, "contract"] = "RB2410.SHF"

    with pytest.raises(ValueError, match="bundle_relationship.*bars_dominants"):
        write_bundle(tmp_path, **frames)


def test_bundle_rejects_roll_chain_that_disagrees_with_dominants(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["roll_fills"].loc[0, "old_contract"] = "RB2409.SHF"

    with pytest.raises(ValueError, match="bundle_relationship.*roll_dominants"):
        write_bundle(tmp_path, **frames)


def test_bundle_rejects_same_contract_roll_on_first_retained_date(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["dominants"].loc[frames["dominants"]["product"].eq("RB"), "contract"] = (
        "RB2405.SHF"
    )
    frames["bars"].loc[frames["bars"]["product"].eq("RB"), "contract"] = "RB2405.SHF"
    frames["roll_fills"].loc[0, "trade_date"] = pd.Timestamp("2024-03-05")
    frames["roll_fills"].loc[0, "old_contract"] = "RB2405.SHF"
    frames["roll_fills"].loc[0, "new_contract"] = "RB2405.SHF"

    with pytest.raises(ValueError, match="bundle_relationship.*distinct"):
        write_bundle(tmp_path, **frames)


def test_bundle_rejects_noncanonical_contracts_even_when_tables_agree(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["bars"].loc[0, "contract"] = "garbage"
    frames["dominants"].loc[0, "contract"] = "garbage"

    with pytest.raises(ValueError, match="bundle_relationship.*canonical_contract"):
        write_bundle(tmp_path, **frames)


def test_read_revalidates_cross_table_relationships(tmp_path, bundle_frames):
    write_bundle(tmp_path, **bundle_frames)
    bars_path = tmp_path / TABLE_FILES["bars"]
    bars = pd.read_parquet(bars_path)
    bars.loc[0, "contract"] = "RB2410.SHF"
    bars.to_parquet(bars_path, index=False)
    manifest = _manifest(tmp_path)
    manifest["tables"]["bars"]["sha256"] = hashlib.sha256(
        bars_path.read_bytes()
    ).hexdigest()
    (tmp_path / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="bundle_relationship.*bars_dominants"):
        read_bundle(tmp_path)


@pytest.mark.parametrize(
    "secret_value",
    [
        "postgresql://alice:hunter2@db.internal/market",
        "host=db.internal password=hunter2",
        "Bearer abcdefghijklmnopqrstuvwxyz",
        "sk-abcdefghijklmnopqrstuvwxyz123456",
    ],
)
def test_manifest_rejects_nested_secret_bearing_values_without_echoing_them(
    tmp_path, bundle_frames, secret_value
):
    with pytest.raises(ValueError, match="bundle_manifest_sensitive") as captured:
        write_bundle(
            tmp_path,
            **bundle_frames,
            inputs={"nested": [{"reference": secret_value}]},
        )

    assert secret_value not in str(captured.value)
    assert not (tmp_path / "manifest.json").exists()


def test_a_transition_without_a_fill_is_allowed_when_the_bundle_declares_it(
    tmp_path, bundle_frames
):
    """成交窗口零成交的换月不发成交单，所以「每个换月都必须有成交单」不再成立 ——
    但缺多少必须由 bundle 自己申报，否则「悄悄少了一笔」和「按规矩跳过」看起来一样。
    """
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["roll_fills"] = frames["roll_fills"].iloc[0:0].reset_index(drop=True)

    write_bundle(tmp_path, **frames, inputs={"unpriceable_rolls": 1})

    assert read_bundle(tmp_path).roll_fills.empty


def test_a_transition_without_a_fill_is_refused_when_the_bundle_declares_none(
    tmp_path, bundle_frames
):
    frames = {name: frame.copy() for name, frame in bundle_frames.items()}
    frames["roll_fills"] = frames["roll_fills"].iloc[0:0].reset_index(drop=True)

    with pytest.raises(ValueError, match="bundle_relationship.*roll_dominants"):
        write_bundle(tmp_path, **frames)


def _inferable_rows(symbol: str, *, multiplier: int, start_day: int) -> pd.DataFrame:
    """一张合约三天各 20 根、amount 与乘数自洽的分钟行 —— 推断要的就是这个形状。"""
    records = []
    for offset in range(3):
        base = pd.Timestamp(
            f"2012-05-{start_day + offset:02d} 09:00", tz="Asia/Shanghai"
        )
        for index in range(20):
            price = 100.0 + index
            volume = 1.0 + index
            records.append(
                {
                    "bar_time": base + pd.Timedelta(minutes=index),
                    "symbol": symbol,
                    "trade_date": (base + pd.Timedelta(minutes=index)).date(),
                    "low": price,
                    "high": price,
                    "volume": volume,
                    "amount": price * volume * multiplier,
                }
            )
    return pd.DataFrame(records)


def test_a_contract_that_cannot_infer_its_own_multiplier_asks_its_siblings():
    """乘数是品种级常量：本合约取样跨不到足够多的交易日时，同品种两张以上兄弟合约
    推出同一个值就采用（2026-08-31 用户裁决），provenance 记 `sibling_inference`。"""

    class _NoMetadata:
        def resolve_metadata_multiplier(self, **_):
            raise MinuteDataError(
                trade_date=date(2012, 5, 11),
                contract="AG1209.SHF",
                check="contract_multiplier_sample",
                reason="multiplier sample spans too few trade dates",
            )

    candidate = SimpleNamespace(
        exchange="SHFE",
        daily_contract="AG1209.SHF",
        minute_symbol="AG1209",
        trade_date=date(2012, 5, 11),
        product="AG",
    )
    sample = pd.concat(
        [
            _inferable_rows("AG1209", multiplier=15, start_day=11).iloc[:5],
            _inferable_rows("AG1212", multiplier=15, start_day=11),
            _inferable_rows("AG1306", multiplier=15, start_day=11),
        ],
        ignore_index=True,
    )

    resolution = _metadata_multiplier_resolution(
        _NoMetadata(),
        {"SHFE": "amount_vwap"},
        candidate,
        sample.loc[sample["symbol"] == "AG1209"],
        inference_frame=sample,
    )

    assert resolution.multiplier == 15
    assert resolution.source == "sibling_inference"


def test_one_sibling_alone_is_not_enough_to_settle_a_multiplier():
    class _NoMetadata:
        def resolve_metadata_multiplier(self, **_):
            raise MinuteDataError(
                trade_date=date(2012, 5, 11),
                contract="AG1209.SHF",
                check="contract_multiplier_sample",
                reason="multiplier sample spans too few trade dates",
            )

    candidate = SimpleNamespace(
        exchange="SHFE",
        daily_contract="AG1209.SHF",
        minute_symbol="AG1209",
        trade_date=date(2012, 5, 11),
        product="AG",
    )
    sample = _inferable_rows("AG1212", multiplier=15, start_day=11)

    with pytest.raises(MinuteDataError, match="contract_multiplier_sample"):
        _metadata_multiplier_resolution(
            _NoMetadata(),
            {"SHFE": "amount_vwap"},
            candidate,
            sample.iloc[:0],
            inference_frame=sample,
        )


def test_the_wide_sample_is_cut_to_this_contract_before_inference():
    """宽样本是给兄弟兜底用的；推断本身只认这一张合约的行 —— 混着别的合约会直接
    报 `minute_contract`（菜籽油 OI1307 2012-09-19 就是这样被我自己的修改打断的）。"""
    seen: dict[str, object] = {}

    class _RecordingSource:
        def resolve_metadata_multiplier(self, **kwargs):
            seen.update(kwargs)
            return 15

    candidate = SimpleNamespace(
        exchange="CZCE",
        daily_contract="OI1307.CZC",
        minute_symbol="OI1307",
        trade_date=date(2012, 9, 19),
        product="OI",
    )
    sample = pd.concat(
        [
            _inferable_rows("OI1307", multiplier=10, start_day=11),
            _inferable_rows("OI1309", multiplier=10, start_day=11),
        ],
        ignore_index=True,
    )

    _metadata_multiplier_resolution(
        _RecordingSource(),
        {"CZCE": "ohlc_typical"},
        candidate,
        sample.loc[sample["symbol"] == "OI1307"],
        inference_frame=sample,
    )

    assert set(seen["inference_frame"]["symbol"]) == {"OI1307"}
