from __future__ import annotations

import pytest

from kelly_lab.errors import KellyLabError
from kelly_lab.fx import align_fx_prior, convert_prices_to_base


def test_fx_alignment_uses_prior_fix_never_future_fix() -> None:
    result = align_fx_prior(
        ["2024-01-05", "2024-01-08", "2024-01-09"],
        ["2024-01-05", "2024-01-09"],
        [1300.0, 1310.0],
    )

    assert result.rates == [1300.0, 1300.0, 1310.0]
    assert result.source_dates == ["2024-01-05", "2024-01-05", "2024-01-09"]
    assert result.lag_days == [0, 3, 0]


def test_fx_fix_older_than_five_calendar_days_is_rejected() -> None:
    result = align_fx_prior(
        ["2024-01-11"],
        ["2024-01-05"],
        [1300.0],
        max_lag_days=5,
    )

    assert result.status == "degraded"
    assert result.rates == [None]
    assert result.reasons == ["fx_too_stale"]


def test_missing_fx_cannot_be_silently_converted() -> None:
    with pytest.raises(KellyLabError) as captured:
        convert_prices_to_base([100.0], [None])

    assert captured.value.code.value == "fx_missing"
