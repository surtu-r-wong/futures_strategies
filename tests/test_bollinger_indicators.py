from dataclasses import FrozenInstanceError

import numpy as np
import pytest

from cta_bollinger.indicators import (
    BandPath,
    OpenInterestPath,
    bands,
    oi_multiplier,
    rolling_oi,
)


def test_bands_use_one_window_for_mean_and_population_std():
    result = bands([1.0, 2.0, 3.0, 9.0], length=3, beta=1.5, ddof=0)

    assert result.middle[-1] == pytest.approx(np.mean([2.0, 3.0, 9.0]))
    assert result.std[-1] == pytest.approx(np.std([2.0, 3.0, 9.0], ddof=0))
    assert result.upper[-1] == pytest.approx(
        np.mean([2.0, 3.0, 9.0]) + 1.5 * np.std([2.0, 3.0, 9.0], ddof=0)
    )
    assert result.lower[-1] == pytest.approx(
        np.mean([2.0, 3.0, 9.0]) - 1.5 * np.std([2.0, 3.0, 9.0], ddof=0)
    )


def test_ddof_one_is_an_explicit_sample_std_sensitivity():
    population = bands([1.0, 2.0, 3.0], length=3, ddof=0)
    sample = bands([1.0, 2.0, 3.0], length=3, ddof=1)

    assert population.std[-1] == pytest.approx(np.sqrt(2.0 / 3.0))
    assert sample.std[-1] == pytest.approx(1.0)
    assert sample.std[-1] > population.std[-1]


def test_bands_default_to_the_paper_parameters_and_exact_warmup():
    closes = np.arange(1.0, 302.0)
    result = bands(closes)

    assert np.isnan(result.middle[:299]).all()
    assert result.middle[299] == pytest.approx(np.mean(closes[:300]))
    assert result.std[299] == pytest.approx(np.std(closes[:300], ddof=0))
    assert result.upper[299] == pytest.approx(
        result.middle[299] + 1.5 * result.std[299]
    )


def test_indicator_history_is_missing_until_the_full_window():
    result = bands([1.0, 2.0], length=3, beta=1.5, ddof=0)

    assert np.isnan(result.middle).all()
    assert np.isnan(result.std).all()
    assert np.isnan(result.upper).all()
    assert np.isnan(result.lower).all()


def test_band_path_is_frozen_slotted_and_preserves_float64_length():
    result = bands(np.array([1, 2, 3], dtype="int32"), length=2)

    assert isinstance(result, BandPath)
    assert not hasattr(result, "__dict__")
    for values in (result.middle, result.std, result.upper, result.lower):
        assert values.dtype == np.dtype("float64")
        assert values.shape == (3,)
    with pytest.raises(FrozenInstanceError):
        result.middle = np.zeros(3)


def test_every_returned_indicator_array_is_read_only():
    band_path = bands([1.0, 2.0, 3.0], length=2)
    oi_path = rolling_oi([10.0, 20.0, 30.0], short=1, long=2)

    for values in (
        band_path.middle,
        band_path.std,
        band_path.upper,
        band_path.lower,
        oi_path.short,
        oi_path.long,
    ):
        with pytest.raises(ValueError, match="read-only"):
            values[0] = 999.0


def test_public_path_constructors_defensively_copy_and_freeze_arrays():
    float_source = np.array([1.0, 2.0, 3.0], dtype="float64")
    int_source = np.array([4, 5, 6], dtype="int32")
    band_path = BandPath(float_source, int_source, float_source, int_source)
    oi_path = OpenInterestPath(float_source, int_source)

    float_source[0] = 999.0
    int_source[0] = 999
    for values, source, expected in (
        (band_path.middle, float_source, 1.0),
        (band_path.std, int_source, 4.0),
        (band_path.upper, float_source, 1.0),
        (band_path.lower, int_source, 4.0),
        (oi_path.short, float_source, 1.0),
        (oi_path.long, int_source, 4.0),
    ):
        assert values.dtype == np.dtype("float64")
        assert values.shape == (3,)
        assert values[0] == expected
        assert not np.shares_memory(values, source)
        with pytest.raises(ValueError, match="read-only"):
            values[0] = 999.0


@pytest.mark.parametrize(
    ("constructor", "arguments"),
    [
        (BandPath, (np.ones((1, 2)), np.ones(2), np.ones(2), np.ones(2))),
        (BandPath, (np.ones(2), np.ones((1, 2)), np.ones(2), np.ones(2))),
        (BandPath, (np.ones(2), np.ones(2), np.ones((1, 2)), np.ones(2))),
        (BandPath, (np.ones(2), np.ones(2), np.ones(2), np.ones((1, 2)))),
        (OpenInterestPath, (np.ones((1, 2)), np.ones(2))),
        (OpenInterestPath, (np.ones(2), np.ones((1, 2)))),
    ],
)
def test_public_path_constructors_reject_non_one_dimensional_arrays(
    constructor, arguments
):
    with pytest.raises(ValueError, match="expected one dimension"):
        constructor(*arguments)


@pytest.mark.parametrize("closes", [1.0, [[1.0, 2.0]], np.ones((2, 2))])
def test_bands_reject_non_one_dimensional_input(closes):
    with pytest.raises(ValueError, match="bollinger_closes: expected one dimension"):
        bands(closes, length=2)


@pytest.mark.parametrize("length", [True, 1, 2.0, np.int64(2)])
def test_bands_reject_invalid_lengths(length):
    with pytest.raises(ValueError, match="bollinger_length: expected integer >= 2"):
        bands([1.0, 2.0], length=length)


@pytest.mark.parametrize("ddof", [-1, 2, 0.0, True])
def test_bands_reject_unsupported_ddof(ddof):
    with pytest.raises(ValueError, match="bollinger_ddof: expected 0 or 1"):
        bands([1.0, 2.0], length=2, ddof=ddof)


@pytest.mark.parametrize("beta", [0.0, -1.0, np.nan, np.inf, -np.inf, "wide"])
def test_bands_require_a_finite_positive_beta(beta):
    with pytest.raises(ValueError, match="bollinger_beta: expected finite positive value"):
        bands([1.0, 2.0], length=2, beta=beta)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_bands_reject_nonfinite_closes_instead_of_hiding_them(bad):
    with pytest.raises(ValueError, match="bollinger_closes: values must be finite"):
        bands([1.0, bad, 3.0], length=2)


def test_bands_do_not_mutate_the_input_and_do_not_look_ahead():
    closes = np.array([1.0, 2.0, 3.0, 4.0])
    original = closes.copy()
    baseline = bands(closes, length=3)

    changed_future = closes.copy()
    changed_future[-1] = 400.0
    changed = bands(changed_future, length=3)

    np.testing.assert_array_equal(closes, original)
    np.testing.assert_allclose(
        baseline.middle[:3], changed.middle[:3], equal_nan=True
    )
    np.testing.assert_allclose(baseline.std[:3], changed.std[:3], equal_nan=True)


def test_rolling_oi_uses_full_window_simple_means_with_exact_warmups():
    result = rolling_oi([10.0, 20.0, 30.0, 40.0], short=2, long=3)

    np.testing.assert_allclose(result.short, [np.nan, 15.0, 25.0, 35.0], equal_nan=True)
    np.testing.assert_allclose(
        result.long, [np.nan, np.nan, 20.0, 30.0], equal_nan=True
    )


def test_rolling_oi_path_is_frozen_slotted_float64_and_same_length():
    result = rolling_oi(np.array([10, 20, 30], dtype="int32"), short=1, long=2)

    assert isinstance(result, OpenInterestPath)
    assert not hasattr(result, "__dict__")
    assert result.short.dtype == np.dtype("float64")
    assert result.long.dtype == np.dtype("float64")
    assert result.short.shape == result.long.shape == (3,)
    with pytest.raises(FrozenInstanceError):
        result.long = np.zeros(3)


@pytest.mark.parametrize("open_interest", [1.0, [[1.0, 2.0]], np.ones((2, 2))])
def test_rolling_oi_rejects_non_one_dimensional_input(open_interest):
    with pytest.raises(ValueError, match="bollinger_open_interest: expected one dimension"):
        rolling_oi(open_interest, short=1, long=2)


@pytest.mark.parametrize(
    ("short", "long"),
    [
        (True, 2),
        (0, 2),
        (1.0, 2),
        (np.int64(1), 2),
        (1, True),
        (1, 1),
        (2, 1),
        (1, 2.0),
        (1, np.int64(2)),
    ],
)
def test_rolling_oi_rejects_invalid_windows(short, long):
    with pytest.raises(
        ValueError,
        match="bollinger_oi_windows: expected integers satisfying 1 <= short < long",
    ):
        rolling_oi([10.0, 20.0], short=short, long=long)


@pytest.mark.parametrize("bad", [np.nan, np.inf, -np.inf])
def test_rolling_oi_rejects_nonfinite_values(bad):
    with pytest.raises(ValueError, match="bollinger_open_interest: values must be finite"):
        rolling_oi([10.0, bad, 30.0], short=1, long=2)


def test_rolling_oi_does_not_mutate_the_input_and_does_not_look_ahead():
    open_interest = np.array([10.0, 20.0, 30.0, 40.0])
    original = open_interest.copy()
    baseline = rolling_oi(open_interest, short=2, long=3)

    changed_future = open_interest.copy()
    changed_future[-1] = 400.0
    changed = rolling_oi(changed_future, short=2, long=3)

    np.testing.assert_array_equal(open_interest, original)
    np.testing.assert_allclose(baseline.short[:3], changed.short[:3], equal_nan=True)
    np.testing.assert_allclose(baseline.long[:3], changed.long[:3], equal_nan=True)


def test_oi_multiplier_is_full_only_when_short_is_strictly_above_long():
    assert oi_multiplier(short_oi=101.0, long_oi=100.0) == 1.0
    assert oi_multiplier(short_oi=100.0, long_oi=100.0) == 0.5
    assert oi_multiplier(short_oi=99.0, long_oi=100.0) == 0.5


@pytest.mark.parametrize(
    ("short_oi", "long_oi"),
    [
        (np.nan, 1.0),
        (1.0, np.nan),
        (np.inf, 1.0),
        (1.0, -np.inf),
        ("missing", 1.0),
    ],
)
def test_oi_multiplier_requires_two_finite_values(short_oi, long_oi):
    with pytest.raises(ValueError, match="bollinger_oi: both averages must be finite"):
        oi_multiplier(short_oi=short_oi, long_oi=long_oi)
