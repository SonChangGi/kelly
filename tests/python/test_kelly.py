from __future__ import annotations

from math import isclose

import pytest

from kelly_lab.errors import KellyLabError
from kelly_lab.kelly import (
    binomial_kelly_fraction,
    exact_historical_kelly,
    historical_log_growth,
    single_asset_gbm_kelly,
)


def test_binomial_kelly_is_20_percent() -> None:
    assert isclose(binomial_kelly_fraction(0.60), 0.20, abs_tol=1e-12)


def test_requested_gbm_golden_values() -> None:
    result = single_asset_gbm_kelly(
        expected_excess_return=0.06,
        volatility=0.20,
        risk_free_rate=0.02,
    )

    assert isclose(result.theoretical_fraction, 1.5, rel_tol=1e-12)
    assert isclose(result.full_kelly_growth.annual_log_growth, 0.065, rel_tol=1e-12)
    assert isclose(result.two_x_growth.annual_log_growth, 0.06, rel_tol=1e-12)
    assert isclose(
        result.two_x_growth.expected_arithmetic_return,
        2.718281828459045**0.14 - 1.0,
        rel_tol=1e-12,
    )
    assert result.two_x_growth.expected_arithmetic_return > (
        result.two_x_growth.expected_geometric_return
    )
    assert isclose(
        result.presets["half"]["fraction_of_max_excess_growth"],
        0.75,
        rel_tol=1e-12,
    )


def test_theoretical_fraction_and_capped_path_are_separate() -> None:
    result = single_asset_gbm_kelly(0.20, 0.10, leverage_cap=3.0)

    assert isclose(result.theoretical_fraction, 20.0)
    assert isclose(result.applied_fraction, 3.0)
    assert isclose(result.theoretical_growth.fraction, 20.0)
    assert result.applied_growth.fraction == 3.0


def test_zero_volatility_returns_explicit_unavailable_reason() -> None:
    result = single_asset_gbm_kelly(0.06, 0.0)

    assert result.status == "unavailable"
    assert result.reason == "zero_volatility"
    assert result.applied_fraction is None


def test_two_x_daily_loss_of_60_percent_is_ruin() -> None:
    result = historical_log_growth([-0.60], 2.0)

    assert result.status == "ruin"
    assert result.reason == "ruin"
    assert result.annual_log_growth is None


def test_exact_kelly_labels_theoretical_and_applied_cap() -> None:
    # A strongly positive but non-deterministic sample has an in-sample optimum
    # above the deployable 3x path.
    returns = [0.04, 0.03, -0.005, 0.02] * 20
    result = exact_historical_kelly(returns, leverage_cap=3.0)

    assert result.theoretical_fraction > 3.0
    assert result.applied_fraction == 3.0
    assert result.theoretical_growth.status == "published"


def test_applied_cap_cannot_exceed_v1_three_x_boundary() -> None:
    with pytest.raises(KellyLabError, match="v1 range"):
        single_asset_gbm_kelly(0.06, 0.20, leverage_cap=3.01)
