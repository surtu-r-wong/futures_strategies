from __future__ import annotations

import numpy as np
import pandas as pd

from common.metrics import summarize


def test_summarize_returns_nan_when_cumulative_equity_goes_nonpositive():
    """A period return <= -100% drives cumulative equity <= 0; annualizing that
    with a fractional exponent must yield NaN, not raise (a negative base to a
    fractional power returns a Python complex, which ``float()`` rejects)."""
    rets = pd.Series([-1.5, 0.02, 0.01, -0.03, 0.04])

    result = summarize(rets, periods_per_year=252)

    assert np.isnan(result["ann_return"])
    assert np.isnan(result["sharpe"])
    assert result["n_periods"] == 5
