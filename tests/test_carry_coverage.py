from datetime import date

import pytest

from cta_carry.coverage import COMMODITY_EXCHANGES, coverage_cutoff, main


def test_coverage_cutoff_is_the_earliest_commodity_exchange_max_date() -> None:
    # 2026-09-10 morning: four exchanges landed 09-09 at 03:06, DCE still 09-08.
    max_dates = {
        "CFE": date(
            2026, 9, 4
        ),  # financial futures: stale but outside the commodity universe
        "CZC": date(2026, 9, 9),
        "DCE": date(2026, 9, 8),
        "GFE": date(2026, 9, 9),
        "INE": date(2026, 9, 9),
        "SHF": date(2026, 9, 9),
    }

    report = coverage_cutoff(max_dates)

    assert COMMODITY_EXCHANGES == ("CZC", "DCE", "GFE", "INE", "SHF")
    assert report.cutoff == date(2026, 9, 8)
    assert report.latest == date(2026, 9, 9)
    assert report.lagging == {"DCE": date(2026, 9, 8)}


def test_coverage_cutoff_has_no_lagging_exchange_when_all_are_level() -> None:
    report = coverage_cutoff(
        {ex: date(2026, 9, 9) for ex in ("CZC", "DCE", "GFE", "INE", "SHF", "CFE")}
    )

    assert report.cutoff == report.latest == date(2026, 9, 9)
    assert report.lagging == {}


def test_coverage_cutoff_refuses_a_missing_commodity_exchange() -> None:
    with pytest.raises(ValueError, match="GFE"):
        coverage_cutoff(
            {
                "CZC": date(2026, 9, 9),
                "DCE": date(2026, 9, 9),
                "INE": date(2026, 9, 9),
                "SHF": date(2026, 9, 9),
            }
        )


def test_main_prints_the_cutoff_alone_on_stdout_and_the_lag_on_stderr(capsys) -> None:
    def loader(*, config_path=None, since):
        assert since == date(2026, 8, 11)
        return {
            "CZC": date(2026, 9, 9),
            "DCE": date(2026, 9, 8),
            "GFE": date(2026, 9, 9),
            "INE": date(2026, 9, 9),
            "SHF": date(2026, 9, 9),
        }

    rc = main(["--as-of", "2026-09-10"], loader=loader)

    out, err = capsys.readouterr()
    assert rc == 0
    assert out == "2026-09-08\n"
    assert "DCE" in err and "2026-09-08" in err and "2026-09-09" in err
