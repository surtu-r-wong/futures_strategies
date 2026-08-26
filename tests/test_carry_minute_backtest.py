from collections.abc import Mapping
from dataclasses import FrozenInstanceError, asdict, replace
from datetime import date, datetime, timedelta
import json
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pandas as pd
import pytest

import cta_carry.minute_backtest as minute_backtest_module
from cta_carry.backtest import (
    CarryBacktester,
    CarryBacktestResult,
    WarmupInsufficientError,
)
from cta_carry.data import CarryDataSet
from cta_carry.minute_backtest import (
    IntradayStopMachine,
    StopDecision,
    merge_close_plan,
)
from cta_carry.minute_bars import FifteenMinuteBar, MultiplierResolution
from cta_carry.minute_pg_source import MinuteCandidate, MinutePlanSummary
from cta_carry.report import write_carry_outputs
from cta_carry.minute_sessions import (
    DAY_SEGMENTS,
    SESSION_RULES_VERSION,
    SessionClockError,
    SessionRule,
    SessionSegment,
)
from cta_carry.risk import PositionState
from tests.carry_fixtures import make_carry_panel, small_config


TZ = ZoneInfo("Asia/Shanghai")
TRADE_DATE = date(2024, 1, 8)
_DEFAULT_PLAN_AUDIT = object()


class FakeMinuteSource:
    """Named deterministic small-side source used by the orchestrator tests."""

    def __init__(
        self,
        *,
        transform=None,
        chunks: int = 1,
        duplicate_rows: bool = False,
        omit_role: str | None = None,
        omit_keys: set[tuple[date, str]] | None = None,
        initial_plan_audit: tuple[object, ...] = (),
        plan_entry_factory=None,
        plan_audit_override=_DEFAULT_PLAN_AUDIT,
        plan_audit_snapshot_factory=None,
        audit_query_months_delta: int = 0,
    ) -> None:
        self.calls: list[tuple[tuple[MinuteCandidate, ...], datetime, datetime]] = []
        self.transform = transform
        self.chunks = chunks
        self.duplicate_rows = duplicate_rows
        self.omit_role = omit_role
        self.omit_keys = omit_keys or set()
        self.plan_entry_factory = plan_entry_factory
        self.plan_audit_override = plan_audit_override
        self.plan_audit_snapshot_factory = plan_audit_snapshot_factory
        self._plan_audit_reads = 0
        self.audit_query_months_delta = audit_query_months_delta
        self._plan_summaries = list(initial_plan_audit)
        self._minute_rows = 0
        self._candidate_days: set[tuple[date, str]] = set()
        self._minute_table_min: datetime | None = None
        self._minute_table_max: datetime | None = None

    @property
    def audit(self):
        return {
            "minute_table_min": self._minute_table_min,
            "minute_table_max": self._minute_table_max,
            "minute_query_months": len(self.calls) + self.audit_query_months_delta,
            "minute_rows": self._minute_rows,
            "minute_candidate_contract_days": len(self._candidate_days),
        }

    @property
    def plan_audit(self):
        if self.plan_audit_override is not _DEFAULT_PLAN_AUDIT:
            return self.plan_audit_override
        snapshot = tuple(self._plan_summaries)
        self._plan_audit_reads += 1
        if self.plan_audit_snapshot_factory is not None:
            snapshot = self.plan_audit_snapshot_factory(
                snapshot,
                self._plan_audit_reads,
            )
        return snapshot

    def iter_month(self, candidates, lower, upper):
        materialized = tuple(candidates)
        self.calls.append((materialized, lower, upper))
        summary = MinutePlanSummary(
            query_kind="iter_month",
            lower_bound=lower.isoformat(),
            upper_bound=upper.isoformat(),
            candidate_contract_days=len(materialized),
            referenced_chunks=("_hyper_1_0_chunk", "_hyper_1_1_chunk"),
            maximum_plan_rows=len(materialized) * 1000,
            node_types=("Index Scan", "Nested Loop"),
        )
        entries = (
            (summary,)
            if self.plan_entry_factory is None
            else tuple(self.plan_entry_factory(summary))
        )
        self._plan_summaries.extend(entries)
        self._candidate_days.update(
            (candidate.trade_date, candidate.daily_contract)
            for candidate in materialized
        )
        rows: list[dict[str, object]] = []
        for candidate in materialized:
            current = candidate.window_start
            product_offset = ord(candidate.product[0]) - ord("A")
            while current < candidate.window_end:
                local = current.astimezone(TZ)
                minute = local.hour * 60 + local.minute
                is_night = local.date() < candidate.trade_date and 21 * 60 <= minute
                is_day = any(start <= minute < end for start, end in DAY_SEGMENTS)
                if is_night or is_day:
                    day_offset = (candidate.trade_date - date(2024, 1, 2)).days
                    price = 100.0 + 10.0 * product_offset + 0.02 * day_offset
                    price += (minute % 17) * 0.001
                    volume = 2.0
                    rows.append(
                        {
                            "trade_date": candidate.trade_date,
                            "product": candidate.product,
                            "daily_contract": candidate.daily_contract,
                            "bar_time": current,
                            "symbol": candidate.minute_symbol,
                            "exchange": candidate.exchange,
                            "open": price,
                            "high": price + 0.1,
                            "low": price - 0.1,
                            "close": price,
                            "volume": volume,
                            "amount": price * volume * 10,
                            "open_interest": 1000.0,
                        }
                    )
                current += timedelta(minutes=1)
        if rows:
            frame = (
                pd.DataFrame(rows)
                .sort_values(["bar_time", "symbol"], kind="mergesort")
                .reset_index(drop=True)
            )
            if self.omit_role is not None:
                omitted = {
                    (candidate.trade_date, candidate.daily_contract)
                    for candidate in materialized
                    if candidate.candidate_role == self.omit_role
                }
                frame = frame.loc[
                    ~pd.MultiIndex.from_frame(
                        frame[["trade_date", "daily_contract"]]
                    ).isin(omitted)
                ].reset_index(drop=True)
            if self.omit_keys:
                frame = frame.loc[
                    ~pd.MultiIndex.from_frame(
                        frame[["trade_date", "daily_contract"]]
                    ).isin(self.omit_keys)
                ].reset_index(drop=True)
            if self.transform is not None:
                frame = self.transform(frame.copy())
            if self.duplicate_rows:
                frame = pd.concat([frame, frame], ignore_index=True)
            self._minute_rows += len(frame)
            observed_min = frame["bar_time"].min()
            observed_max = frame["bar_time"].max()
            self._minute_table_min = (
                observed_min
                if self._minute_table_min is None
                else min(self._minute_table_min, observed_min)
            )
            self._minute_table_max = (
                observed_max
                if self._minute_table_max is None
                else max(self._minute_table_max, observed_max)
            )
            for chunk in (
                frame.iloc[index :: self.chunks].sort_values(
                    ["bar_time", "symbol"], kind="mergesort"
                )
                for index in range(self.chunks)
            ):
                if not chunk.empty:
                    yield chunk.reset_index(drop=True)

    def resolve_metadata_multiplier(
        self,
        *,
        daily_contract,
        trade_date,
        frame=None,
        inference_frame=None,
        pricing_basis="amount_vwap",
    ):
        assert frame is not None
        return MultiplierResolution(
            multiplier=10,
            source="fake_metadata",
            sample_rows=len(frame),
            pass_rate=1.0,
            sample_dates=1,
            sample_start=frame["bar_time"].min(),
            sample_end=frame["bar_time"].max(),
        )


def minute_backtest_fixture():
    base = make_carry_panel(periods=14)
    prices = base.prices.copy()
    shifted_dates = pd.bdate_range("2024-01-23", periods=14).date.tolist()
    date_map = dict(zip(base.dates, shifted_dates, strict=True))
    prices["trade_date"] = prices["trade_date"].map(date_map)
    prices["contract"] = prices["contract"] + ".SHF"
    prices["exchange_suffix"] = "SHF"
    data = CarryDataSet(prices=prices, data_quality=base.data_quality)
    day_segments = tuple(SessionSegment(*segment) for segment in DAY_SEGMENTS)
    rules = tuple(
        SessionRule(
            exchange="SHFE",
            product=product,
            effective_start=data.dates[0],
            effective_end=data.dates[-1],
            segments=(SessionSegment(-180, -60), *day_segments)
            if product == "A"
            else day_segments,
            version=SESSION_RULES_VERSION,
        )
        for product in ("A", "B", "C", "D", "E")
    )
    return data, FakeMinuteSource(), rules, data.dates[8], data.dates[-1]


def test_minute_backtester_produces_auditable_tables_and_feedback() -> None:
    data, source, rules, start, end = minute_backtest_fixture()

    result = minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=small_config(vol_window=3, min_shadow_active_days=2),
        start=start,
        end=end,
    ).run()

    assert not result.daily_returns.empty
    assert set(result.executions["reason"]) >= {"entry", "rebalance"}
    assert {"execution_kind", "daily_open"} <= set(result.executions)
    assert {
        "bar_start",
        "bar_end",
        "threshold",
        "tranches_before",
        "tranches_after",
        "execution_id",
    } <= set(result.intraday_stops)
    assert {
        "session_rules_version",
        "minute_query_rules_version",
        "accounting_clock",
    } <= set(result.run_config["key"])
    assert (
        result.run_config.set_index("key").loc["accounting_clock", "value"]
        == "piecewise_close_marked"
    )
    queried_roles = {
        candidate.candidate_role
        for candidates, _, _ in source.calls
        for candidate in candidates
    }
    assert queried_roles <= {
        "signal_main",
        "carried",
        "roll_old",
        "roll_new",
        "exit",
        "close_mark",
    }
    assert "session_representative" not in queried_roles
    run_config = result.run_config.set_index("key")["value"]
    assert run_config["minute_query_months"] == source.audit["minute_query_months"]
    assert run_config["minute_rows"] == source.audit["minute_rows"]
    assert (
        run_config["minute_candidate_contract_days"]
        == source.audit["minute_candidate_contract_days"]
    )
    assert (
        run_config["minute_table_min"] == source.audit["minute_table_min"].isoformat()
    )
    assert (
        run_config["minute_table_max"] == source.audit["minute_table_max"].isoformat()
    )


def test_minute_backtester_defers_a_registered_absent_product_day() -> None:
    from cta_carry.session_authority import AbsentProductDay

    data, source, rules, start, end = minute_backtest_fixture()
    absent_date = data.dates[10]

    result = minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=small_config(vol_window=3, min_shadow_active_days=2),
        start=start,
        end=end,
        absent_product_days=(
            AbsentProductDay(
                version=SESSION_RULES_VERSION,
                exchange="SHFE",
                product="A",
                trade_date=absent_date,
                absent_segment="day",
                reason="archive holds no day session",
                source_url="docs/research/x.md",
            ),
        ),
    ).run()

    queried = {
        (candidate.trade_date, candidate.product)
        for candidates, _, _ in source.calls
        for candidate in candidates
    }
    assert (absent_date, "A") not in queried

    executed = result.executions
    same_day = executed[executed["trade_date"] == absent_date]
    assert "A" not in set(same_day["product"])

    positions = result.positions
    previous_date = data.dates[9]
    held = positions[(positions["product"] == "A")]
    before = held[held["trade_date"] == previous_date]
    during = held[held["trade_date"] == absent_date]
    if not before.empty and not during.empty:
        for column in ("contract", "direction", "weight"):
            assert during.iloc[0][column] == before.iloc[0][column]


def test_minute_backtester_rejects_two_exchanges_sharing_a_product_day() -> None:
    from cta_carry.session_authority import AbsentProductDay

    data, source, rules, start, end = minute_backtest_fixture()
    day = data.dates[10]

    def _row(exchange: str) -> AbsentProductDay:
        return AbsentProductDay(
            version=SESSION_RULES_VERSION,
            exchange=exchange,
            product="A",
            trade_date=day,
            absent_segment="day",
            reason="archive holds no day session",
            source_url="docs/research/x.md",
        )

    with pytest.raises(ValueError, match="absent_product_day_ambiguous"):
        minute_backtest_module.CarryMinuteBacktester(
            data=data,
            minute_source=source,
            session_rules=rules,
            config=small_config(vol_window=3, min_shadow_active_days=2),
            start=start,
            end=end,
            absent_product_days=(_row("SHFE"), _row("DCE")),
        )


def _explain_only_summary() -> MinutePlanSummary:
    return MinutePlanSummary(
        query_kind="explain_only",
        lower_bound=datetime(2023, 12, 1, tzinfo=TZ).isoformat(),
        upper_bound=datetime(2024, 1, 1, tzinfo=TZ).isoformat(),
        candidate_contract_days=5,
        referenced_chunks=("_hyper_0_0_chunk",),
        maximum_plan_rows=500,
        node_types=("Index Scan",),
    )


def _minute_query_plan_rows(result: CarryBacktestResult) -> pd.DataFrame:
    return result.minute_data_quality.loc[
        result.minute_data_quality["check"].eq("minute_query_plan")
    ].reset_index(drop=True)


def test_minute_backtester_records_one_query_plan_row_per_actual_month() -> None:
    explain_summary = _explain_only_summary()
    source = FakeMinuteSource(initial_plan_audit=(explain_summary,))

    result = _run_fixture(source)

    rows = _minute_query_plan_rows(result)
    assert len(rows) == len(source.calls) == source.audit["minute_query_months"]
    assert len(source.plan_audit) == len(source.calls) + 1
    assert source.plan_audit[0] is explain_summary
    for index, row in rows.iterrows():
        candidates, lower, upper = source.calls[index]
        detail = json.loads(row["detail"])
        assert row["query_month"] == f"{candidates[-1].trade_date:%Y-%m}"
        assert detail == {
            "candidate_contract_days": len(candidates),
            "lower_bound": lower.isoformat(),
            "maximum_plan_rows": len(candidates) * 1000,
            "node_types": ["Index Scan", "Nested Loop"],
            "query_kind": "iter_month",
            "referenced_chunks": ["_hyper_1_0_chunk", "_hyper_1_1_chunk"],
            "upper_bound": upper.isoformat(),
        }
        assert row["observed_rows"] == len(candidates)


def test_minute_query_plan_rows_survive_report_serialization(tmp_path) -> None:
    result = _run_fixture()
    expected = [
        json.loads(value) for value in _minute_query_plan_rows(result)["detail"]
    ]
    report_result = replace(
        result,
        executions=result.executions.iloc[0:0].copy(),
        intraday_stops=result.intraday_stops.iloc[0:0].copy(),
    )

    xlsx, _ = write_carry_outputs(report_result, tmp_path / "minute_plan_audit")

    written = pd.read_excel(xlsx, sheet_name="minute_data_quality")
    written = written.loc[written["check"].eq("minute_query_plan")].reset_index(
        drop=True
    )
    assert [json.loads(value) for value in written["detail"]] == expected
    assert set(expected[0]) == {
        "query_kind",
        "lower_bound",
        "upper_bound",
        "candidate_contract_days",
        "referenced_chunks",
        "maximum_plan_rows",
        "node_types",
    }


@pytest.mark.parametrize(
    ("source", "field"),
    [
        (SimpleNamespace(), "plan_audit"),
        (FakeMinuteSource(plan_audit_override=None), "plan_audit"),
        (FakeMinuteSource(plan_audit_override=lambda: ()), "plan_audit"),
        (FakeMinuteSource(plan_audit_override=[]), "plan_audit"),
    ],
    ids=("absent", "none", "callable", "mutable_sequence"),
)
def test_minute_backtester_requires_an_immutable_plan_audit_snapshot(
    source,
    field: str,
) -> None:
    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["field"] == field


def test_minute_backtester_rejects_mutation_of_a_prior_query_plan() -> None:
    def mutate_prior_entry(snapshot, read_count):
        if read_count >= 3 and len(snapshot) >= 2:
            return (None, *snapshot[1:])
        return snapshot

    source = FakeMinuteSource(plan_audit_snapshot_factory=mutate_prior_entry)

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 2
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_rejects_reordered_preexisting_plan_prefix() -> None:
    first = _explain_only_summary()
    second = replace(first, maximum_plan_rows=501)

    def reorder_prefix(snapshot, read_count):
        if read_count >= 2:
            return (snapshot[1], snapshot[0], *snapshot[2:])
        return snapshot

    source = FakeMinuteSource(
        initial_plan_audit=(first, second),
        plan_audit_snapshot_factory=reorder_prefix,
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 1
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_rejects_final_plan_snapshot_mutation() -> None:
    def mutate_final_entry(snapshot, read_count):
        if read_count >= 4:
            return (*snapshot[:-1], None)
        return snapshot

    source = FakeMinuteSource(plan_audit_snapshot_factory=mutate_final_entry)

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 2
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_rejects_in_place_relevant_mapping_mutation() -> None:
    def mutate_final_mapping(snapshot, read_count):
        if read_count >= 4:
            snapshot[-1]["query_kind"] = "explain_only"
        return snapshot

    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (asdict(summary),),
        plan_audit_snapshot_factory=mutate_final_mapping,
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 2
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_rejects_in_place_preexisting_mapping_mutation() -> None:
    prefix = asdict(_explain_only_summary())

    def mutate_prefix_mapping(snapshot, read_count):
        if read_count >= 2:
            snapshot[0]["query_kind"] = "corrupted"
        return snapshot

    source = FakeMinuteSource(
        initial_plan_audit=(prefix,),
        plan_audit_snapshot_factory=mutate_prefix_mapping,
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 1
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_detaches_nested_preexisting_mapping_lists() -> None:
    prefix = asdict(_explain_only_summary())
    prefix["referenced_chunks"] = list(prefix["referenced_chunks"])

    def mutate_nested_prefix_list(snapshot, read_count):
        if read_count >= 2:
            snapshot[0]["referenced_chunks"].append("_hyper_0_1_chunk")
        return snapshot

    source = FakeMinuteSource(
        initial_plan_audit=(prefix,),
        plan_audit_snapshot_factory=mutate_nested_prefix_list,
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert len(source.calls) == 1
    assert exc_info.value.check == "minute_query_plan"


def test_minute_backtester_wraps_plan_audit_getter_failure() -> None:
    class RaisingPlanAuditSource(FakeMinuteSource):
        @property
        def plan_audit(self):
            raise RuntimeError("hostile plan_audit getter")

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(RaisingPlanAuditSource())

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["field"] == "plan_audit"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_minute_backtester_preserves_structured_plan_audit_getter_failure() -> None:
    expected = minute_backtest_module.MinuteDataError(
        check="minute_query_plan",
        reason="already structured getter failure",
    )

    class RaisingPlanAuditSource(FakeMinuteSource):
        @property
        def plan_audit(self):
            raise expected

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(RaisingPlanAuditSource())

    assert exc_info.value is expected


def test_minute_backtester_does_not_execute_plan_entry_equality_protocol() -> None:
    class RaisingEquality:
        def __init__(self, summary):
            self._summary = summary

        def __eq__(self, other):
            raise RuntimeError("unsafe equality protocol executed")

        def __getattr__(self, name):
            return getattr(self._summary, name)

    prefix = _explain_only_summary()

    def replace_prefix_identity(snapshot, read_count):
        if read_count >= 2:
            return (RaisingEquality(prefix), *snapshot[1:])
        return snapshot

    source = FakeMinuteSource(
        initial_plan_audit=(RaisingEquality(prefix),),
        plan_audit_snapshot_factory=replace_prefix_identity,
    )

    result = _run_fixture(source)

    assert len(_minute_query_plan_rows(result)) == len(source.calls)


def test_minute_backtester_wraps_object_plan_field_failure() -> None:
    class RaisingFieldSummary:
        def __init__(self, summary):
            self._summary = summary

        @property
        def query_kind(self):
            raise RuntimeError("hostile object field")

        def __getattr__(self, name):
            return getattr(self._summary, name)

    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (RaisingFieldSummary(summary),),
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["field"] == "query_kind"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_minute_backtester_wraps_mapping_plan_field_failure() -> None:
    class RaisingFieldMapping(Mapping):
        def __init__(self, summary):
            self._values = asdict(summary)

        def __getitem__(self, key):
            if key == "query_kind":
                raise RuntimeError("hostile mapping lookup")
            return self._values[key]

        def __iter__(self):
            return iter(self._values)

        def __len__(self):
            return len(self._values)

    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (RaisingFieldMapping(summary),),
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["field"] == "query_kind"
    assert isinstance(exc_info.value.__cause__, RuntimeError)


def test_minute_backtester_observes_object_plan_fields_once_per_snapshot() -> None:
    entries = []

    class StatefulObjectSummary:
        def __init__(self, summary):
            self._summary = summary
            self._observation_reads = {}
            self.maximum_reads = {}

        def begin_observation(self):
            self._observation_reads = {}

        def __getattr__(self, name):
            reads = self._observation_reads.get(name, 0) + 1
            self._observation_reads[name] = reads
            self.maximum_reads[name] = max(self.maximum_reads.get(name, 0), reads)
            if name == "query_kind" and reads > 1:
                return "explain_only"
            return getattr(self._summary, name)

    def wrap_summary(summary):
        entry = StatefulObjectSummary(summary)
        entries.append(entry)
        return (entry,)

    def begin_snapshot(snapshot, read_count):
        for entry in snapshot:
            entry.begin_observation()
        return snapshot

    source = FakeMinuteSource(
        plan_entry_factory=wrap_summary,
        plan_audit_snapshot_factory=begin_snapshot,
    )

    result = _run_fixture(source)

    plan_rows = _minute_query_plan_rows(result)
    assert len(plan_rows) == len(source.calls)
    assert {json.loads(detail)["query_kind"] for detail in plan_rows["detail"]} == {
        "iter_month"
    }
    assert entries
    assert all(max(entry.maximum_reads.values()) == 1 for entry in entries)


def test_minute_backtester_observes_mapping_plan_fields_once_per_snapshot() -> None:
    entries = []

    class StatefulMappingSummary(Mapping):
        def __init__(self, summary):
            self._values = asdict(summary)
            self._observation_reads = {}
            self.maximum_reads = {}

        def begin_observation(self):
            self._observation_reads = {}

        def __getitem__(self, key):
            reads = self._observation_reads.get(key, 0) + 1
            self._observation_reads[key] = reads
            self.maximum_reads[key] = max(self.maximum_reads.get(key, 0), reads)
            if key == "query_kind" and reads > 1:
                return "explain_only"
            return self._values[key]

        def __iter__(self):
            return iter(self._values)

        def __len__(self):
            return len(self._values)

    def wrap_summary(summary):
        entry = StatefulMappingSummary(summary)
        entries.append(entry)
        return (entry,)

    def begin_snapshot(snapshot, read_count):
        for entry in snapshot:
            entry.begin_observation()
        return snapshot

    source = FakeMinuteSource(
        plan_entry_factory=wrap_summary,
        plan_audit_snapshot_factory=begin_snapshot,
    )

    result = _run_fixture(source)

    plan_rows = _minute_query_plan_rows(result)
    assert len(plan_rows) == len(source.calls)
    assert {json.loads(detail)["query_kind"] for detail in plan_rows["detail"]} == {
        "iter_month"
    }
    assert entries
    assert all(max(entry.maximum_reads.values()) == 1 for entry in entries)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda summary: None,
        lambda summary: replace(summary, query_kind="explain_only"),
        lambda summary: replace(summary, lower_bound=17),
        lambda summary: replace(summary, upper_bound="2024-02-01T00:00:00"),
        lambda summary: replace(summary, candidate_contract_days=True),
        lambda summary: replace(summary, candidate_contract_days=0),
        lambda summary: replace(summary, referenced_chunks=["_hyper_1_0_chunk"]),
        lambda summary: replace(summary, referenced_chunks=(17,)),
        lambda summary: replace(summary, referenced_chunks=("not_a_chunk",)),
        lambda summary: replace(
            summary,
            referenced_chunks=tuple(f"_hyper_1_{index}_chunk" for index in range(4)),
        ),
        lambda summary: replace(summary, maximum_plan_rows=True),
        lambda summary: replace(summary, maximum_plan_rows=-1),
        lambda summary: replace(summary, maximum_plan_rows=float("nan")),
        lambda summary: replace(summary, maximum_plan_rows=10_000_000),
        lambda summary: replace(summary, maximum_plan_rows=10**1000),
        lambda summary: replace(summary, node_types=["Index Scan"]),
        lambda summary: replace(summary, node_types=(17,)),
        lambda summary: replace(summary, node_types=()),
    ],
    ids=(
        "none_summary",
        "wrong_query_kind",
        "lower_bound_type",
        "naive_upper_bound",
        "candidate_count_bool",
        "candidate_count_mismatch",
        "chunks_container",
        "chunk_member",
        "chunk_name",
        "too_many_chunks",
        "maximum_rows_bool",
        "maximum_rows_negative",
        "maximum_rows_nan",
        "maximum_rows_unsafe",
        "maximum_rows_overflow",
        "node_types_container",
        "node_type_member",
        "node_types_empty",
    ),
)
def test_minute_backtester_rejects_malformed_relevant_plan_summary(mutate) -> None:
    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (mutate(summary),),
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"


@pytest.mark.parametrize(
    "plan_entry_factory",
    [
        lambda summary: (),
        lambda summary: (summary, summary),
    ],
    ids=("missing_summary", "extra_summary"),
)
def test_minute_backtester_rejects_plan_summary_count_mismatch(
    plan_entry_factory,
) -> None:
    source = FakeMinuteSource(plan_entry_factory=plan_entry_factory)

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["actual_monthly_queries"] == 1


def test_minute_backtester_rejects_plan_bounds_that_do_not_match_query() -> None:
    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (
            replace(summary, lower_bound=summary.upper_bound),
        ),
    )

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context["field"] == "lower_bound"


def test_minute_backtester_rejects_source_audit_query_count_mismatch() -> None:
    source = FakeMinuteSource(audit_query_months_delta=1)

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    assert exc_info.value.check == "minute_query_plan"
    assert exc_info.value.context == {
        "actual_monthly_queries": len(source.calls),
        "plan_summary_count": len(source.calls),
        "source_audit_minute_query_months": len(source.calls) + 1,
    }


def test_minute_backtester_accepts_mapping_style_plan_summary() -> None:
    source = FakeMinuteSource(
        plan_entry_factory=lambda summary: (asdict(summary),),
    )

    result = _run_fixture(source)

    assert len(_minute_query_plan_rows(result)) == len(source.calls)


def _valid_source_audit() -> dict[str, object]:
    return {
        "minute_table_min": datetime(2024, 1, 1, tzinfo=TZ),
        "minute_table_max": datetime(2024, 2, 1, tzinfo=TZ),
        "minute_query_months": 2,
        "minute_rows": 100,
        "minute_candidate_contract_days": 10,
    }


def test_minute_source_provenance_requires_an_audit_contract() -> None:
    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        minute_backtest_module._validated_source_audit(SimpleNamespace())

    assert exc_info.value.check == "minute_source_audit"
    assert exc_info.value.context == {"field": "audit"}


def test_minute_source_provenance_rejects_a_missing_field() -> None:
    audit = _valid_source_audit()
    del audit["minute_rows"]

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        minute_backtest_module._validated_source_audit(SimpleNamespace(audit=audit))

    assert exc_info.value.check == "minute_source_audit"
    assert exc_info.value.context == {"field": "minute_rows"}


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("minute_table_min", datetime(2024, 1, 1)),
        ("minute_query_months", 2.0),
        ("minute_rows", True),
        ("minute_candidate_contract_days", -1),
    ],
)
def test_minute_source_provenance_rejects_field_type_or_range(
    field: str,
    value: object,
) -> None:
    audit = _valid_source_audit()
    audit[field] = value

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        minute_backtest_module._validated_source_audit(SimpleNamespace(audit=audit))

    assert exc_info.value.check == "minute_source_audit"
    assert exc_info.value.context["field"] == field


def test_minute_source_provenance_accepts_task10_object_contract() -> None:
    audit = SimpleNamespace(**_valid_source_audit())

    assert (
        minute_backtest_module._validated_source_audit(SimpleNamespace(audit=audit))
        == _valid_source_audit()
    )


def test_shadow_warmup_starts_at_signal_ready_and_matches_daily_gate() -> None:
    data, source, rules, _, end = minute_backtest_fixture()
    start = data.dates[9]
    config = small_config(vol_window=5, min_shadow_active_days=1)

    with pytest.raises(WarmupInsufficientError) as daily_exc:
        CarryBacktester(
            data,
            config,
            start=start,
            end=end,
        ).run()
    with pytest.raises(WarmupInsufficientError) as minute_exc:
        minute_backtest_module.CarryMinuteBacktester(
            data=data,
            minute_source=source,
            session_rules=rules,
            config=config,
            start=start,
            end=end,
        ).run()

    assert minute_exc.value.signal_ready_date == daily_exc.value.signal_ready_date
    assert minute_exc.value.shadow_observations == daily_exc.value.shadow_observations
    assert minute_exc.value.active_days == daily_exc.value.active_days
    assert minute_exc.value.required_observations == 5
    assert minute_exc.value.required_active_days == 1


def test_minute_backtester_rejects_a_session_asset_that_misses_prewarm(
    monkeypatch,
) -> None:
    data, source, rules, start, end = minute_backtest_fixture()
    monkeypatch.setattr(
        minute_backtest_module,
        "SESSION_RULES_CAPTURE_START",
        start,
    )

    with pytest.raises(SessionClockError) as exc_info:
        minute_backtest_module.CarryMinuteBacktester(
            data=data,
            minute_source=source,
            session_rules=rules,
            config=small_config(),
            start=start,
            end=end,
        ).run()

    assert getattr(exc_info.value, "check", None) == "session_asset_prewarm_coverage"
    assert source.calls == []


def _run_fixture(source: FakeMinuteSource | None = None, **config_overrides):
    data, default_source, rules, start, end = minute_backtest_fixture()
    return minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source or default_source,
        session_rules=rules,
        config=small_config(
            vol_window=3,
            min_shadow_active_days=2,
            **config_overrides,
        ),
        start=start,
        end=end,
    ).run()


def test_public_api_exports_minute_engine_and_structured_error() -> None:
    import cta_carry

    assert (
        cta_carry.CarryMinuteBacktester is minute_backtest_module.CarryMinuteBacktester
    )
    assert cta_carry.MinuteDataError is minute_backtest_module.MinuteDataError


def test_daily_result_defaults_keep_minute_frames_empty() -> None:
    empty = pd.DataFrame()
    result = CarryBacktestResult(
        daily_returns=empty,
        positions=empty,
        trades=empty,
        signals=empty,
        curve_selection=empty,
        data_quality=empty,
        run_config=empty,
    )

    assert result.execution_mode == "daily"
    assert result.executions.empty
    assert result.intraday_stops.empty
    assert result.minute_data_quality.empty


def test_dynamic_candidate_union_assigns_roll_exit_carried_and_signal_roles() -> None:
    days = [date(2024, 1, day) for day in (2, 3, 4)]

    def signal(product, contract, direction=1):
        return SimpleNamespace(
            product=product,
            main_contract=contract,
            effective_direction=direction,
        )

    active = {
        days[0]: {
            "A": signal("A", "A2401.SHF"),
            "B": signal("B", "B2401.SHF"),
            "D": signal("D", "D2401.SHF"),
        },
        days[1]: {
            "A": signal("A", "A2405.SHF"),
            "C": signal("C", "C2401.SHF"),
            "D": signal("D", "D2401.SHF"),
        },
        days[2]: {"E": signal("E", "E2401.SHF")},
    }

    roles = minute_backtest_module._candidate_roles_for_date(
        index=2,
        dates=days,
        active=active,
    )

    assert roles == {
        "A2401.SHF": "roll_old",
        "A2405.SHF": "roll_new",
        "B2401.SHF": "exit",
        "C2401.SHF": "signal_main",
        "D2401.SHF": "carried",
    }


def test_cross_contract_direction_reversal_uses_exit_and_signal_main_roles() -> None:
    before = PositionState(
        direction=1,
        contract="A2401.SHF",
        tranches_remaining=3,
    )
    after = PositionState(
        direction=-1,
        contract="A2405.SHF",
        tranches_remaining=3,
    )

    roles = minute_backtest_module._execution_roles(
        before,
        after,
        {"A2401.SHF", "A2405.SHF"},
    )

    assert roles == {
        "A2401.SHF": "exit",
        "A2405.SHF": "signal_main",
    }

    locked_roles = minute_backtest_module._execution_roles(
        PositionState(locked_direction=1),
        after,
        {"A2401.SHF", "A2405.SHF"},
        old_contract_override="A2401.SHF",
    )

    assert locked_roles == {
        "A2401.SHF": "exit",
        "A2405.SHF": "signal_main",
    }


def test_current_close_signal_is_not_required_as_same_day_minute_candidate() -> None:
    data, _, rules, start, end = minute_backtest_fixture()
    config = small_config(vol_window=3, min_shadow_active_days=2)
    research = minute_backtest_module.build_daily_research(data.prices, config)
    first_signal = (
        research.signal_result.signals.loc[
            research.signal_result.signals["effective_direction"].ne(0)
        ]
        .sort_values(["trade_date", "product"], kind="mergesort")
        .iloc[0]
    )
    omitted_key = (first_signal["trade_date"], first_signal["main_contract"])
    source = FakeMinuteSource(omit_keys={omitted_key})

    minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=config,
        start=start,
        end=end,
    ).run()

    queried = {
        (candidate.trade_date, candidate.daily_contract)
        for candidates, _, _ in source.calls
        for candidate in candidates
    }
    assert omitted_key not in queried


def test_missing_dynamic_leg_reports_actual_candidate_role() -> None:
    source = FakeMinuteSource(omit_role="carried")

    with pytest.raises(minute_backtest_module.MinuteDataError) as exc_info:
        _run_fixture(source)

    error = exc_info.value
    assert error.check == "dynamic_execution_leg_missing_minutes"
    assert error.context == {"candidate_role": "carried"}
    assert error.context["candidate_role"] != "session_representative"


def test_dynamic_session_audit_fails_before_first_month_query() -> None:
    data, source, rules, start, end = minute_backtest_fixture()

    with pytest.raises(SessionClockError) as exc_info:
        minute_backtest_module.CarryMinuteBacktester(
            data=data,
            minute_source=source,
            session_rules=tuple(rule for rule in rules if rule.product != "E"),
            config=small_config(vol_window=3, min_shadow_active_days=2),
            start=start,
            end=end,
        ).run()

    assert getattr(exc_info.value, "check", None) == "dynamic_audit_coverage"
    assert source.calls == []


def test_month_chunks_and_exact_overlap_duplicates_do_not_change_results() -> None:
    expected = _run_fixture(FakeMinuteSource())
    actual = _run_fixture(FakeMinuteSource(chunks=7, duplicate_rows=True))

    for name in (
        "daily_returns",
        "positions",
        "trades",
        "executions",
        "intraday_stops",
    ):
        pd.testing.assert_frame_equal(getattr(actual, name), getattr(expected, name))
    assert actual.executions["execution_id"].is_unique


def test_intraday_stop_receives_only_the_atr_labeled_previous_trade_date(
    monkeypatch,
) -> None:
    captured: list[tuple[date, str, float]] = []
    original = minute_backtest_module.IntradayStopMachine.on_bar

    def recording(self, trade_date, product, state, bar, atr, next_fill_end):
        captured.append((trade_date, state.contract, atr))
        return original(
            self,
            trade_date,
            product,
            state,
            bar,
            atr,
            next_fill_end,
        )

    monkeypatch.setattr(minute_backtest_module.IntradayStopMachine, "on_bar", recording)
    data, source, rules, start, end = minute_backtest_fixture()
    config = small_config(vol_window=3, min_shadow_active_days=2)
    minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=config,
        start=start,
        end=end,
    ).run()
    research = minute_backtest_module.build_daily_research(data.prices, config)
    atr = research.contract_atr.set_index(["trade_date", "contract"])["atr"]
    previous = {
        day: data.dates[index - 1] for index, day in enumerate(data.dates[1:], 1)
    }

    assert captured
    for trade_date, contract, value in captured:
        assert value == pytest.approx(atr.loc[(previous[trade_date], contract)])


def _shift_first_fill_on(trade_date: date, amount: float):
    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        for (_, contract), group in frame.loc[
            frame["trade_date"].eq(trade_date)
        ].groupby(["trade_date", "daily_contract"], sort=True):
            slots = group.nsmallest(5, "bar_time").index
            if contract.startswith("A"):
                for column in ("open", "high", "low", "close"):
                    frame.loc[slots, column] += amount
                frame.loc[slots, "amount"] = (
                    frame.loc[slots, "close"] * frame.loc[slots, "volume"] * 10
                )
        return frame

    return transform


def test_minute_change_affects_only_that_fill_and_the_next_close_scale() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    changed_date = data.dates[10]
    next_date = data.dates[11]
    baseline = _run_fixture(
        FakeMinuteSource(),
        target_vol=0.01,
    )
    changed = _run_fixture(
        FakeMinuteSource(transform=_shift_first_fill_on(changed_date, 0.25)),
        target_vol=0.01,
    )

    before = baseline.executions["trade_date"] < changed_date
    pd.testing.assert_frame_equal(
        baseline.executions.loc[before].reset_index(drop=True),
        changed.executions.loc[before].reset_index(drop=True),
    )
    baseline_same = baseline.executions.loc[
        baseline.executions["trade_date"].eq(changed_date)
        & baseline.executions["contract"].str.startswith("A")
    ].reset_index(drop=True)
    changed_same = changed.executions.loc[
        changed.executions["trade_date"].eq(changed_date)
        & changed.executions["contract"].str.startswith("A")
    ].reset_index(drop=True)
    pd.testing.assert_series_equal(
        baseline_same["new_weight"], changed_same["new_weight"]
    )
    assert not baseline_same["vwap"].equals(changed_same["vwap"])
    baseline_next = baseline.executions.loc[
        baseline.executions["trade_date"].eq(next_date), "new_weight"
    ].reset_index(drop=True)
    changed_next = changed.executions.loc[
        changed.executions["trade_date"].eq(next_date), "new_weight"
    ].reset_index(drop=True)
    assert not baseline_next.equals(changed_next)


def test_formal_and_shadow_positions_share_state_but_use_scaled_weights() -> None:
    result = _run_fixture()
    active = result.positions.loc[
        result.positions["raw_weight"].ne(0.0) & result.positions["weight"].ne(0.0)
    ]

    assert not active.empty
    assert (active["raw_weight"] * active["weight"] > 0.0).all()
    assert active["raw_weight"].ne(active["weight"]).any()
    assert active["tranches_remaining"].between(1, 3).all()


def _full_short_stop_on(trade_date: date):
    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        mask = frame["trade_date"].eq(trade_date) & frame[
            "daily_contract"
        ].str.startswith("A")
        frame.loc[mask, "open"] = 106.0
        frame.loc[mask, "high"] = 106.0
        frame.loc[mask, "low"] = 100.0
        frame.loc[mask, "close"] = 106.0
        frame.loc[mask, "amount"] = frame.loc[mask, "volume"] * 106.0 * 10
        return frame

    return transform


def _short_stop_bar_on(trade_date: date, start_hour: int, start_minute: int):
    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        local_times = frame["bar_time"].map(lambda value: value.astimezone(TZ))
        start = start_hour * 60 + start_minute
        minutes = local_times.map(lambda value: value.hour * 60 + value.minute)
        mask = (
            frame["trade_date"].eq(trade_date)
            & frame["daily_contract"].str.startswith("A")
            & minutes.ge(start)
            & minutes.lt(start + 15)
        )
        frame.loc[mask, ["open", "high", "low", "close"]] = 106.0
        frame.loc[mask, "amount"] = frame.loc[mask, "volume"] * 106.0 * 10
        return frame

    return transform


def test_final_bar_stop_is_merged_into_the_next_daily_target() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    stop_date = data.dates[9]
    next_date = data.dates[10]

    result = _run_fixture(
        FakeMinuteSource(transform=_short_stop_bar_on(stop_date, 14, 45)),
        cost_bps=0.0,
    )

    final_stop = result.intraday_stops.loc[
        result.intraday_stops["trade_date"].eq(stop_date)
        & result.intraday_stops["product"].eq("A")
        & result.intraday_stops["bar_end"]
        .map(lambda value: value.astimezone(TZ).time())
        .eq(datetime.strptime("15:00", "%H:%M").time())
    ]
    assert len(final_stop) == 1
    next_execution = result.executions.loc[
        result.executions["trade_date"].eq(next_date)
        & result.executions["product"].eq("A")
    ]
    assert len(next_execution) == 1
    assert next_execution.iloc[0]["reason"] == "stop_1"
    assert next_execution.iloc[0]["execution_kind"] == "daily_target"
    assert final_stop.iloc[0]["execution_id"] == next_execution.iloc[0]["execution_id"]


def test_penultimate_bar_stop_is_not_reused_as_a_final_stop() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    stop_date = data.dates[9]
    next_date = data.dates[10]

    result = _run_fixture(
        FakeMinuteSource(transform=_short_stop_bar_on(stop_date, 14, 30)),
        cost_bps=0.0,
    )

    same_day = result.executions.loc[
        result.executions["trade_date"].eq(stop_date)
        & result.executions["product"].eq("A")
        & result.executions["execution_kind"].eq("intraday_stop")
    ]
    assert len(same_day) == 1
    next_execution = result.executions.loc[
        result.executions["trade_date"].eq(next_date)
        & result.executions["product"].eq("A")
    ]
    assert next_execution["reason"].eq("rebalance").all()


def _three_short_stop_bars_ending_at_close(trade_date: date):
    trigger_starts = {(13, 30), (14, 0), (14, 45)}

    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        local_times = frame["bar_time"].map(lambda value: value.astimezone(TZ))
        selected = pd.Series(False, index=frame.index)
        for hour, minute in trigger_starts:
            start = hour * 60 + minute
            clock_minutes = local_times.map(
                lambda value: value.hour * 60 + value.minute
            )
            selected |= clock_minutes.ge(start) & clock_minutes.lt(start + 15)
        mask = (
            frame["trade_date"].eq(trade_date)
            & frame["daily_contract"].str.startswith("A")
            & selected
        )
        frame.loc[mask, ["open", "high", "low", "close"]] = 106.0
        frame.loc[mask, "amount"] = frame.loc[mask, "volume"] * 106.0 * 10
        return frame

    return transform


def test_final_full_stop_closes_the_concrete_leg_in_next_daily_target() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    stop_date = data.dates[9]
    next_date = data.dates[10]

    result = _run_fixture(
        FakeMinuteSource(transform=_three_short_stop_bars_ending_at_close(stop_date)),
        cost_bps=0.0,
    )

    same_day = result.executions.loc[
        result.executions["trade_date"].eq(stop_date)
        & result.executions["product"].eq("A")
        & result.executions["execution_kind"].eq("intraday_stop")
    ]
    assert len(same_day) == 2
    close = result.executions.loc[
        result.executions["trade_date"].eq(next_date)
        & result.executions["product"].eq("A")
    ]
    assert len(close) == 1
    assert close.iloc[0]["reason"] == "stop_3"
    assert close.iloc[0]["execution_kind"] == "daily_target"
    assert close.iloc[0]["new_weight"] == 0.0


def test_full_stop_locks_direction_without_same_window_intermediate_rows() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    stop_date = data.dates[9]
    result = _run_fixture(
        FakeMinuteSource(transform=_full_short_stop_on(stop_date)),
        cost_bps=0.0,
    )
    stops = result.intraday_stops.loc[
        result.intraday_stops["trade_date"].eq(stop_date)
        & result.intraday_stops["product"].eq("A")
    ]

    assert len(stops) == 3
    assert stops["triggered"].all()
    assert stops[["product", "bar_start", "bar_end"]].drop_duplicates().shape[0] == 3
    stop_fills = result.executions.loc[
        result.executions["execution_kind"].eq("intraday_stop")
        & result.executions["trade_date"].eq(stop_date)
        & result.executions["product"].eq("A")
    ]
    assert len(stop_fills) == 3
    assert not stop_fills.duplicated(["product", "window_start", "window_end"]).any()
    locked = result.positions.loc[
        result.positions["trade_date"].ge(stop_date)
        & result.positions["product"].eq("A")
    ]
    assert not locked.empty
    assert locked["direction"].eq(0).all()
    assert locked["locked_direction"].eq(-1).all()
    assert result.executions.loc[
        result.executions["trade_date"].gt(stop_date)
        & result.executions["product"].eq("A")
        & result.executions["new_weight"].ne(0.0)
    ].empty


def test_full_stop_drops_a_missing_next_day_signal_envelope_candidate() -> None:
    data, _, rules, start, end = minute_backtest_fixture()
    config = small_config(vol_window=3, min_shadow_active_days=2, cost_bps=0.0)
    stop_date = data.dates[9]
    next_date = data.dates[10]
    research = minute_backtest_module.build_daily_research(data.prices, config)
    contexts = minute_backtest_module._prepare_candidates(
        dates=data.dates,
        research=research,
        rules=rules,
    )
    omitted = next(
        context.candidate
        for (trade_date, _), context in contexts.items()
        if trade_date == next_date
        and context.candidate.product == "A"
        and context.candidate.candidate_role in {"carried", "roll_new"}
    )
    source = FakeMinuteSource(
        transform=_full_short_stop_on(stop_date),
        omit_keys={(next_date, omitted.daily_contract)},
    )

    result = minute_backtest_module.CarryMinuteBacktester(
        data=data,
        minute_source=source,
        session_rules=rules,
        config=config,
        start=start,
        end=end,
    ).run()

    assert result.executions.loc[
        result.executions["trade_date"].ge(next_date)
        & result.executions["product"].eq("A")
        & result.executions["new_weight"].ne(0.0)
    ].empty


def _inject_partial_and_zero_volume_slots(trade_date: date):
    def transform(frame: pd.DataFrame) -> pd.DataFrame:
        target = frame.loc[
            frame["trade_date"].eq(trade_date)
            & frame["daily_contract"].str.startswith("A")
        ].nsmallest(5, "bar_time")
        if target.empty:
            return frame
        assert len(target) == 5
        missing_index = target.index[1]
        zero_index = target.index[2]
        frame.loc[zero_index, "volume"] = 0.0
        frame.loc[zero_index, "amount"] = 0.0
        return frame.drop(index=missing_index).reset_index(drop=True)

    return transform


def test_partial_zero_volume_and_fill_missing_slots_are_audited() -> None:
    data, _, _, _, _ = minute_backtest_fixture()
    changed_date = data.dates[10]

    result = _run_fixture(
        FakeMinuteSource(transform=_inject_partial_and_zero_volume_slots(changed_date))
    )

    execution = result.executions.loc[
        result.executions["trade_date"].eq(changed_date)
        & result.executions["product"].eq("A")
        & result.executions["execution_kind"].eq("daily_target")
    ]
    assert len(execution) == 1
    assert execution.iloc[0]["missing_slots"] == 1

    quality = result.minute_data_quality.loc[
        result.minute_data_quality["trade_date"].eq(changed_date)
        & result.minute_data_quality["product"].eq("A")
    ]
    partial = quality.loc[quality["check"].eq("partial_fifteen_minute_bar")]
    assert len(partial) == 1
    assert partial.iloc[0]["missing_slots"] == 1
    zero_volume = quality.loc[quality["check"].eq("zero_volume_minute_slots")]
    assert len(zero_volume) == 1
    assert zero_volume.iloc[0]["observed_rows"] == 1
    fill_quality = quality.loc[quality["check"].eq("five_minute_fill_missing_slots")]
    assert len(fill_quality) == 1
    assert fill_quality.iloc[0]["missing_slots"] == 1


def test_minute_result_tables_are_deterministic_across_two_runs() -> None:
    first = _run_fixture()
    second = _run_fixture()

    for name in (
        "daily_returns",
        "positions",
        "trades",
        "signals",
        "curve_selection",
        "run_config",
        "executions",
        "intraday_stops",
        "minute_data_quality",
    ):
        pd.testing.assert_frame_equal(getattr(first, name), getattr(second, name))


def _bar(
    index: int,
    *,
    high: float = 110.0,
    low: float = 100.0,
    close: float = 100.0,
    no_trade: bool = False,
    contract: str = "A2405.SHF",
    day_offset: int = 0,
) -> FifteenMinuteBar:
    start = datetime(2024, 1, 8, 9, 0, tzinfo=TZ) + timedelta(
        days=day_offset, minutes=15 * index
    )
    return FifteenMinuteBar(
        start=start,
        end=start + timedelta(minutes=15),
        contract=contract,
        open=close,
        high=high,
        low=low,
        close=close,
        volume=0.0 if no_trade else 1.0,
        no_trade=no_trade,
    )


def _long_state(
    *,
    contract: str = "A2405.SHF",
    tranches: int = 3,
    highest_high: float | None = 110.0,
) -> PositionState:
    return PositionState(
        direction=1,
        contract=contract,
        tranches_remaining=tranches,
        highest_high=highest_high,
    )


def _signal(
    direction: int = 1,
    contract: str = "A2405.SHF",
) -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "trade_date": TRADE_DATE,
                "product": "A",
                "effective_direction": direction,
                "main_contract": contract,
                "strength": 1.0,
                "main_close": 100.0,
                "atr": 2.0,
            }
        ]
    )


def _final_stop(
    state: PositionState,
    *,
    stage: int = 1,
    triggered: bool = True,
) -> StopDecision:
    bar = _bar(4)
    return StopDecision(
        eligible=True,
        triggered=triggered,
        state=state,
        stage=stage if triggered else None,
        threshold=105.0,
        bar=bar,
        not_before=bar.end + timedelta(minutes=5),
    )


def test_three_separate_bars_can_remove_all_tranches_in_one_day() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = _long_state()

    for stage, index in enumerate((0, 2, 4), start=1):
        bar = _bar(index)
        decision = machine.on_bar(
            trade_date=TRADE_DATE,
            product="A",
            state=state,
            bar=bar,
            atr=5.0,
            next_fill_end=bar.end + timedelta(minutes=5),
        )
        assert decision.triggered
        assert decision.stage == stage
        assert decision.threshold == 105.0
        state = decision.state

    assert state == PositionState(locked_direction=1)


def test_bar_overlapping_the_previous_fill_is_not_eligible() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    first_bar = _bar(0)
    first = machine.on_bar(
        TRADE_DATE,
        "A",
        _long_state(),
        first_bar,
        5.0,
        _bar(1).end,
    )

    skipped = machine.on_bar(
        TRADE_DATE,
        "A",
        first.state,
        _bar(1),
        5.0,
        _bar(2).end,
    )

    assert not skipped.eligible
    assert not skipped.triggered
    assert skipped.state is first.state
    assert skipped.not_before == _bar(1).end
    at_gate = machine.on_bar(
        TRADE_DATE,
        "A",
        first.state,
        _bar(2),
        5.0,
        _bar(2).end + timedelta(minutes=5),
    )
    assert at_gate.eligible
    assert at_gate.triggered


def test_no_trade_bar_does_not_update_extreme_or_trigger() -> None:
    machine = IntradayStopMachine(small_config(chandelier_atr_multiple=1.0))
    state = _long_state(highest_high=105.0)

    decision = machine.on_bar(
        TRADE_DATE,
        "A",
        state,
        _bar(0, high=120.0, low=80.0, close=80.0, no_trade=True),
        5.0,
        _bar(1).end,
    )

    assert isinstance(decision, StopDecision)
    assert not decision.eligible
    assert not decision.triggered
    assert decision.state is state
    assert decision.threshold is None
    with pytest.raises(FrozenInstanceError):
        decision.triggered = True


def test_stop_limit_is_enforced_per_product_and_trade_date() -> None:
    config = small_config(
        chandelier_atr_multiple=1.0,
        stop_tranches=2,
    )
    machine = IntradayStopMachine(config)
    before = _long_state(tranches=2)
    first_bar = _bar(0)
    first = machine.on_bar(
        TRADE_DATE,
        "A",
        before,
        first_bar,
        5.0,
        first_bar.end + timedelta(minutes=5),
    )
    assert first.triggered

    short_state = PositionState(
        direction=-1,
        contract="A2405.SHF",
        tranches_remaining=2,
        lowest_low=90.0,
    )
    second_bar = _bar(2, high=100.0, low=90.0, close=100.0)
    machine.reset_for_transition(
        "A", first.state, short_state, fill_end=second_bar.start
    )
    second = machine.on_bar(
        TRADE_DATE,
        "A",
        short_state,
        second_bar,
        5.0,
        second_bar.end + timedelta(minutes=5),
    )
    assert second.triggered

    state = _long_state(tranches=2)
    third_bar = _bar(4)
    machine.reset_for_transition("A", second.state, state, fill_end=third_bar.start)

    blocked = machine.on_bar(
        TRADE_DATE,
        "A",
        state,
        third_bar,
        5.0,
        third_bar.end + timedelta(minutes=5),
    )
    next_bar = _bar(0, day_offset=1)
    next_day = machine.on_bar(
        date(2024, 1, 9),
        "A",
        state,
        next_bar,
        5.0,
        next_bar.end + timedelta(minutes=5),
    )

    assert not blocked.eligible
    assert not blocked.triggered
    assert next_day.eligible
    assert next_day.triggered


def test_active_position_rejects_a_bar_for_another_contract() -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0, contract="B2405.SHF")

    with pytest.raises(ValueError, match="bar.contract.*state.contract"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end + timedelta(minutes=5),
        )


@pytest.mark.parametrize("field", ["start", "end"])
def test_stop_bar_requires_timezone_aware_boundaries(field: str) -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0)
    bar = replace(bar, **{field: getattr(bar, field).replace(tzinfo=None)})

    with pytest.raises(ValueError, match="bar.*timezone-aware"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            _bar(0).end + timedelta(minutes=5),
        )


def test_stop_fill_end_requires_timezone_and_must_follow_bar_end() -> None:
    machine = IntradayStopMachine(small_config())
    bar = _bar(0)

    with pytest.raises(ValueError, match="next_fill_end.*timezone-aware"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end.replace(tzinfo=None),
        )
    with pytest.raises(ValueError, match="next_fill_end.*after bar.end"):
        machine.on_bar(
            TRADE_DATE,
            "A",
            _long_state(),
            bar,
            5.0,
            bar.end,
        )


def test_signal_exit_reversal_and_roll_reset_the_gate() -> None:
    machine = IntradayStopMachine(small_config())
    before = _long_state()
    fill_end = _bar(2).end

    machine.reset_for_transition("A", before, PositionState(), fill_end=fill_end)
    assert machine.not_before("A") is None

    reversed_state = PositionState(
        direction=-1,
        contract="A2405.SHF",
        tranches_remaining=3,
    )
    machine.reset_for_transition("A", before, reversed_state, fill_end=fill_end)
    assert machine.not_before("A") == fill_end

    rolled_state = _long_state(contract="A2409.SHF", highest_high=None)
    machine.reset_for_transition("A", before, rolled_state, fill_end=fill_end)
    assert machine.not_before("A") == fill_end

    machine.reset_for_transition("A", rolled_state, PositionState(), fill_end=fill_end)
    assert machine.not_before("A") is None


def test_close_merge_keeps_final_stop_reduction_for_same_direction() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "stop_1"}
    assert list(plan.raw_weights) == ["A2405.SHF"]


def test_close_merge_final_stop_and_zero_signal_produces_one_exit() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}
    zero_signal = _signal(direction=0, contract="")

    plan = merge_close_plan(
        post_stop,
        zero_signal,
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states == {"A": PositionState()}
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}


def test_close_merge_full_stop_and_zero_signal_releases_lock_as_exit() -> None:
    config = small_config()
    post_stop = {"A": PositionState(locked_direction=1)}

    plan = merge_close_plan(
        post_stop,
        _signal(direction=0, contract=""),
        config,
        final_stop_decisions={
            "A": _final_stop(post_stop["A"], stage=config.stop_tranches)
        },
    )

    assert plan.states == {"A": PositionState()}
    assert plan.raw_weights == {}
    assert plan.reasons == {"A": "signal_exit"}


def test_close_merge_final_stop_and_reversal_produces_full_reverse() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(-1),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"].direction == -1
    assert plan.states["A"].tranches_remaining == config.stop_tranches
    assert plan.reasons == {"A": "direction_reversal"}
    assert list(plan.raw_weights) == ["A2405.SHF"]
    assert plan.raw_weights["A2405.SHF"] < 0.0


def test_close_merge_roll_preserves_tranches_and_resets_extremes() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(contract="A2409.SHF"),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    assert plan.states["A"] == PositionState(
        direction=1,
        contract="A2409.SHF",
        tranches_remaining=2,
    )
    assert plan.reasons == {"A": "roll"}
    assert set(plan.raw_weights) == {"A2409.SHF"}


def test_early_stop_without_a_final_bar_trigger_is_not_labeled_as_stop() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"], triggered=False)},
    )

    assert plan.states["A"].tranches_remaining == 2
    assert plan.reasons == {"A": "rebalance"}


@pytest.mark.parametrize(
    "decisions",
    [
        {"B": _final_stop(_long_state(tranches=2))},
        {"A": _final_stop(_long_state(tranches=1), stage=2)},
    ],
)
def test_close_merge_rejects_final_stop_product_or_state_mismatch(
    decisions: dict[str, StopDecision],
) -> None:
    with pytest.raises(ValueError, match="final_stop_decisions"):
        merge_close_plan(
            {"A": _long_state(tranches=2)},
            _signal(),
            small_config(),
            final_stop_decisions=decisions,
        )


def test_close_merge_rejects_duplicate_signal_products_before_sorting() -> None:
    duplicate_signals = pd.concat([_signal(1), _signal(-1)], ignore_index=True)

    with pytest.raises(ValueError, match="duplicate.*A"):
        merge_close_plan(
            {"A": _long_state()},
            duplicate_signals,
            small_config(),
        )


def test_close_merge_returns_one_final_net_target_for_a_stop_and_roll() -> None:
    config = small_config()
    post_stop = {"A": _long_state(tranches=2)}

    plan = merge_close_plan(
        post_stop,
        _signal(contract="A2409.SHF"),
        config,
        final_stop_decisions={"A": _final_stop(post_stop["A"])},
    )

    # Concrete product/window execution-row uniqueness belongs to Task 8,
    # where the actual fill window exists. Task 7 emits only this final target.
    assert plan.states == {
        "A": PositionState(
            direction=1,
            contract="A2409.SHF",
            tranches_remaining=2,
        )
    }
    assert plan.reasons == {"A": "roll"}
    assert set(plan.raw_weights) == {"A2409.SHF"}
