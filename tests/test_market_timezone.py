"""Business dates must agree on Shanghai even when the host is in New York."""

import os
from pathlib import Path
import shutil
import subprocess
import sys
from datetime import date, datetime, timedelta
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest
import pandas as pd

from common import market_time
from cta_carry import coverage
from cta_gtja import data_quality
from scripts import citic_index_fetch, citic_receipts_exchange_fetch, m02_seal
from scripts.research import audit_fundamentals


SHANGHAI = ZoneInfo("Asia/Shanghai")
NEW_YORK = ZoneInfo("America/New_York")


@pytest.fixture(params=[
    "2026-11-04T01:30:00+00:00",  # winter: Shanghai morning, NY previous day
    "2026-07-01T01:30:00+00:00",  # summer, month boundary
    "2026-11-02T00:30:00+00:00",  # Monday in Shanghai, Sunday in NY
    "2026-12-31T15:59:59+00:00",  # Shanghai year-end midnight boundary
    "2026-12-31T16:00:00+00:00",
    "2026-03-08T06:59:59+00:00",  # NY spring DST jump
    "2026-03-08T07:00:00+00:00",
    "2026-11-01T05:30:00+00:00",  # both occurrences of NY's repeated hour
    "2026-11-01T06:30:00+00:00",
])
def market_clock(request, monkeypatch):
    instant = datetime.fromisoformat(request.param)

    class FrozenDate(date):
        @classmethod
        def today(cls):
            return instant.astimezone(NEW_YORK).date()

    class FrozenDatetime(datetime):
        @classmethod
        def now(cls, tz=None):
            if tz is None:
                return instant.astimezone(NEW_YORK).replace(tzinfo=None)
            return instant.astimezone(tz)

    for module in (coverage, data_quality, citic_index_fetch, citic_receipts_exchange_fetch):
        monkeypatch.setattr(module, "date", FrozenDate)
    monkeypatch.setattr(audit_fundamentals, "datetime", FrozenDatetime)
    monkeypatch.setattr(market_time, "datetime", FrozenDatetime)
    return instant.astimezone(SHANGHAI).date()


@pytest.mark.parametrize("explicit", [None, "2024-02-29"])
def test_quality_strict_lag_uses_market_date(market_clock, explicit, monkeypatch, capsys):
    expected = date.fromisoformat(explicit) if explicit else market_clock
    row = {
        "base_symbol": "CU", "trade_date": expected - timedelta(days=1),
        "daily_return": 0.01, "return_index": 100,
        **{f"{field}_{lineage}": 10 for lineage in ("raw", "ba", "fa")
           for field in ("open", "high", "low", "close")},
    }
    report = data_quality.summarize_continuous_contract_quality(pd.DataFrame([row]))
    monkeypatch.setattr(data_quality, "scan_continuous_contract_quality", lambda **kwargs: report)
    monkeypatch.setattr(sys, "argv", ["quality", "--strict", "--max-lag-days", "0"]
                        + (["--as-of", explicit] if explicit else []))
    with pytest.raises(SystemExit) as exc:
        data_quality.main()
    assert exc.value.code == 1
    assert "table stale" in capsys.readouterr().out


@pytest.mark.parametrize("explicit", [None, "2024-02-29"])
def test_coverage_uses_market_date_and_preserves_override(market_clock, explicit, capsys):
    expected = date.fromisoformat(explicit) if explicit else market_clock

    def loader(*, config_path, since):
        assert since == expected - timedelta(days=30)
        return {ex: expected - timedelta(days=1) for ex in coverage.COMMODITY_EXCHANGES}

    assert coverage.main(["--as-of", explicit] if explicit else [], loader=loader) == 0
    out, err = capsys.readouterr()
    assert out.strip() == (expected - timedelta(days=1)).isoformat()
    assert f"coverage as of {expected}:" in err


@pytest.mark.parametrize("module", [citic_index_fetch, citic_receipts_exchange_fetch])
@pytest.mark.parametrize("explicit", [None, "2024-02-29"])
def test_fetch_window_ends_on_market_date(market_clock, explicit, module, monkeypatch, tmp_path):
    expected = date.fromisoformat(explicit) if explicit else market_clock
    monkeypatch.setattr(module, "_dsn", lambda _: {})

    class WindowCaptured(Exception):
        pass

    def chunks(start, end, *, chunk_days):
        assert end == expected
        raise WindowCaptured

    # Stop before SQL or file export; test the actual CLI-to-window wiring.
    monkeypatch.setattr(module, "date_chunks", chunks)
    with pytest.raises(WindowCaptured):
        module.main(["--out", str(tmp_path)] + (["--end", explicit] if explicit else []))


def test_closeout_verdict_directory_uses_market_date(market_clock, monkeypatch):
    seen = {}
    monkeypatch.setattr(m02_seal.subprocess, "run", lambda argv, env, check: seen.update(env))
    m02_seal.v1_catch_up("/fake/nightly.sh", "/fake/account.sqlite", "2026-11-13")()
    assert seen["M02_FUTURES_EXTRA_ARGS"] == (
        f"--today 2026-11-13 --verdict-dir /fake/verdicts-closeout/{market_clock}"
    )


def test_fundamental_audit_query_uses_market_date(market_clock):
    class QueryCaptured(Exception):
        pass

    class Cursor:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

        def execute(self, query, params=None):
            if params is not None:
                assert params["as_of"] == market_clock
                raise QueryCaptured

        def fetchone(self):
            return dict(build_version="test", catalog_version="test", source_recorded_cutoff=None)

    connection = SimpleNamespace(
        set_session=lambda **kwargs: None,
        cursor=lambda **kwargs: Cursor(),
        rollback=lambda: None,
    )
    with pytest.raises(QueryCaptured):
        audit_fundamentals.collect(connection)


REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def shell_runner(tmp_path):
    """Execute copied wrappers with fake Python/date/sleep; never run a producer."""
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    bindir = tmp_path / "bin"
    bindir.mkdir()
    py = tmp_path / ".venv/bin/python"
    py.parent.mkdir(parents=True)
    py.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$CALLS"\n'
                  'if [[ "$*" == *"cta_carry.coverage"* ]]; then\n'
                  '  echo 2026-10-30\n  exit 0\nfi\nexit "${FAKE_EXIT:-0}"\n')
    py.chmod(0o755)
    date_cmd = bindir / "date"
    date_cmd.write_text('#!/bin/bash\nif [ "$1" = "-d" ]; then exec /bin/date "$@"; fi\n'
                        'exec /bin/date --date="$FIXED_INSTANT" "$@"\n')
    date_cmd.chmod(0o755)
    sleep_cmd = bindir / "sleep"
    sleep_cmd.write_text('#!/bin/bash\nprintf "%s\\n" "$*" >> "$SLEEPS"\n')
    sleep_cmd.chmod(0o755)
    state = tmp_path / "state"
    state.mkdir()
    (state / "account.sqlite").touch()
    calls, sleeps = tmp_path / "calls", tmp_path / "sleeps"

    def run(script, instant, *, args=(), overrides=None):
        shutil.copyfile(REPO / "scripts" / script, scripts / script)
        env = {
            **os.environ, "TZ": "America/New_York", "PATH": f"{bindir}:/usr/bin:/bin",
            "FIXED_INSTANT": instant, "CALLS": str(calls), "SLEEPS": str(sleeps),
            "M02_FUTURES_STATE_DIR": str(state), "M02_V2_STATE_DIR": str(state),
            "M02_V2_ROOT": str(tmp_path / "morning"), "M02_V2_CALENDAR": "/fake/calendar.json",
            "M02_V2_TARGETS": "/fake/targets", "M02_V2_ACCOUNT": "test",
            "M02_V2_STORE": "", "M02_V2_TODAY": "", "M02_V2_RETRY_AT": "0000",
            "M02_FUTURES_RETRY_AT": "0000", "M02_FUTURES_EXTRA_ARGS": "",
            "M02_V2_EXTRA_ARGS": "", **(overrides or {}),
        }
        result = subprocess.run(["/bin/bash", str(scripts / script), *args],
                                env=env, capture_output=True, text=True, timeout=10)
        return result, calls.read_text().splitlines(), sleeps.read_text().splitlines() if sleeps.exists() else []

    return run


@pytest.mark.parametrize("instant,expected", [
    ("2026-11-02T00:30:00Z", "2026-11-02"),
    ("2026-07-01T00:30:00Z", "2026-07-01"),
    ("2026-12-31T16:00:00Z", "2027-01-01"),
])
@pytest.mark.parametrize("script", ["carry_tsindex_daily.sh", "citic027_daily.sh", "m02_v2_morning.sh"])
@pytest.mark.parametrize("explicit", [False, True])
def test_shell_default_business_day(shell_runner, script, instant, expected, explicit):
    if explicit:
        expected = "2024-02-29"
    if script == "m02_v2_morning.sh":
        args = []
        overrides = {"M02_V2_TODAY": expected} if explicit else {}
        flag = "--trade-date"
    else:
        args = ["1000000"] + ([expected] if explicit else [])
        overrides = {}
        flag = "--as-of"
    result, calls, sleeps = shell_runner(script, instant, args=args, overrides=overrides)
    assert result.returncode == 0, result.stderr
    assert f"{flag} {expected}" in calls[0]
    assert sleeps == []


@pytest.mark.parametrize("script,hour,prefix", [
    ("m02_v2_morning.sh", 9, "M02_V2"),
    ("m02_v2_nightly.sh", 21, "M02_V2"),
    ("m02_futures_nightly.sh", 21, "M02_FUTURES"),
])
@pytest.mark.parametrize("month", [7, 11])
@pytest.mark.parametrize("minute,expected_sleep", [(34, ["60"]), (36, [])])
def test_shell_retry_uses_market_hour(shell_runner, script, hour, prefix, month, minute, expected_sleep):
    result, calls, sleeps = shell_runner(
        script, f"2026-{month:02d}-04T{hour:02d}:{minute}:00+08:00",
        overrides={"FAKE_EXIT": "2", f"{prefix}_RETRY_AT": f"{hour:02d}35"},
    )
    assert result.returncode == 2
    assert sleeps == expected_sleep
    assert len(calls) == 1 + len(expected_sleep)
