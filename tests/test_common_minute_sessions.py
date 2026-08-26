"""The market-neutral seam: a SessionRuleset carries what a market decides.

The Carry suite in ``test_carry_minute_sessions.py`` covers the commodity
consumer. These cases cover the part commodity cannot exercise on its own --
a market whose own hours changed mid-history, and a market with no night
session -- because a single-entry, night-bearing ruleset makes both paths
invisible.
"""

from datetime import date

import pytest

from common.minute.sessions import (
    CLOCK_MAX_MINUTE,
    COMMODITY_V1,
    DAY_SEGMENTS,
    SESSION_RULESETS,
    SessionRule,
    SessionRuleset,
    SessionSegment,
    load_session_rules,
    ruleset_for_version,
)


# CFFEX shortened the stock-index day session on 2016-01-01: 09:15-11:30 and
# 13:00-15:15 became 09:30-11:30 and 13:00-15:00. Written out as trade-date
# minute offsets so the schedule has two entries that actually differ.
_EARLY = (SessionSegment(555, 690), SessionSegment(780, 915))
_LATE = (SessionSegment(570, 690), SessionSegment(780, 900))

_TWO_ERA = SessionRuleset(
    version="test-two-era",
    capture_start=date(2010, 1, 1),
    day_segment_schedule=((date(2000, 1, 1), _EARLY), (date(2016, 1, 1), _LATE)),
    clock_start_minute=540,
    clock_end_minute=915,
    allows_night=False,
)


@pytest.fixture
def registered_two_era(monkeypatch):
    monkeypatch.setitem(SESSION_RULESETS, _TWO_ERA.version, _TWO_ERA)
    return _TWO_ERA


def _write_session_csv(tmp_path, *rows):
    path = tmp_path / "sessions.csv"
    body = "".join(f"{row}\n" for row in rows)
    path.write_text(
        "exchange,product,effective_start,effective_end,"
        f"night_start,night_end,version\n{body}",
        encoding="utf-8",
    )
    return path


def test_the_commodity_ruleset_still_states_the_hours_carry_was_built_on():
    assert COMMODITY_V1.version == "commodity-v1"
    assert COMMODITY_V1.capture_start == date(2011, 1, 1)
    assert DAY_SEGMENTS == ((540, 615), (630, 690), (810, 900))
    assert COMMODITY_V1.allows_night is True


def test_day_segments_come_from_the_entry_in_effect_on_that_date():
    assert _TWO_ERA.day_segments_for(date(2015, 12, 31)) == _EARLY
    assert _TWO_ERA.day_segments_for(date(2016, 1, 1)) == _LATE
    assert _TWO_ERA.day_segments_for(date(2020, 6, 30)) == _LATE


def test_a_date_before_the_first_entry_fails_instead_of_borrowing_later_hours():
    with pytest.raises(ValueError, match="session_ruleset_lookup"):
        _TWO_ERA.day_segments_for(date(1999, 12, 31))


@pytest.mark.parametrize(
    "first_date,second_date",
    [
        # out of order
        (date(2016, 1, 1), date(2000, 1, 1)),
        # same date twice: two answers to "which hours were in effect", and the
        # lookup would silently take the later one
        (date(2016, 1, 1), date(2016, 1, 1)),
    ],
)
def test_schedule_entries_must_strictly_increase(first_date, second_date):
    with pytest.raises(ValueError, match="session_ruleset_schedule"):
        SessionRuleset(
            version="test-unordered",
            capture_start=date(2010, 1, 1),
            day_segment_schedule=(
                (first_date, _LATE),
                (second_date, _EARLY),
            ),
            clock_start_minute=540,
            clock_end_minute=915,
            allows_night=False,
        )


def test_a_segment_outside_the_markets_own_clock_is_rejected():
    # 915 is a legal offset structurally -- CFFEX closed at 15:15 until 2016 --
    # but not for a market that declares it stops at 15:00.
    assert SessionSegment(780, 915).end_minute <= CLOCK_MAX_MINUTE
    with pytest.raises(ValueError, match="session_ruleset_schedule"):
        SessionRuleset(
            version="test-too-wide",
            capture_start=date(2010, 1, 1),
            day_segment_schedule=((date(2000, 1, 1), (SessionSegment(780, 915),)),),
            clock_start_minute=540,
            clock_end_minute=900,
            allows_night=False,
        )


def test_loading_stamps_each_row_with_the_hours_of_its_own_era(
    tmp_path, registered_two_era
):
    path = _write_session_csv(
        tmp_path,
        "CFFEX,IF,2010-04-16,2015-12-31,none,none,test-two-era",
        "CFFEX,IF,2016-01-01,,none,none,test-two-era",
    )

    rules = load_session_rules(path, ruleset=registered_two_era)

    assert tuple(rule.segments for rule in rules) == (_EARLY, _LATE)


def test_a_night_interval_is_refused_by_a_market_that_has_no_night(
    tmp_path, registered_two_era
):
    path = _write_session_csv(
        tmp_path,
        "CFFEX,IF,2016-01-01,,21:00,23:00,test-two-era",
    )

    with pytest.raises(ValueError, match="session_rules_csv_night"):
        load_session_rules(path, ruleset=registered_two_era)


def test_loading_refuses_rows_stamped_with_another_markets_version(
    tmp_path, registered_two_era
):
    path = _write_session_csv(
        tmp_path,
        "SHFE,AU,2016-01-01,,21:00,23:00,commodity-v1",
    )

    with pytest.raises(ValueError, match="session_rules_csv_version"):
        load_session_rules(path, ruleset=registered_two_era)


def test_day_only_takes_its_segments_from_the_named_ruleset(registered_two_era):
    rule = SessionRule.day_only("CFFEX", "IF", version="test-two-era")

    assert rule.segments == _EARLY


def test_an_unregistered_version_fails_and_names_what_is_registered():
    # Deliberately a name no market will ever claim. "cffex-v1" would read more
    # naturally today and stop holding anything down the day Task 1 registers it.
    with pytest.raises(ValueError, match="session_rule_version"):
        ruleset_for_version("no-such-market-v0")
