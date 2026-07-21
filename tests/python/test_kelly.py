from __future__ import annotations

from math import isclose, log

import pytest

from kelly_lab.errors import KellyLabError
from kelly_lab.kelly import (
    binomial_kelly_fraction,
    exact_historical_kelly,
    historical_log_growth,
    single_asset_gbm_kelly,
)
from kelly_lab.metrics import annual_rate_to_periodic


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


@pytest.mark.parametrize("volatility", [0.0, 1e-13])
def test_zero_or_tiny_volatility_is_unavailable(volatility: float) -> None:
    result = single_asset_gbm_kelly(0.06, volatility)

    assert result.status == "unavailable"
    assert result.reason == "zero_volatility"


def test_negative_volatility_is_invalid_return() -> None:
    with pytest.raises(KellyLabError) as captured:
        single_asset_gbm_kelly(0.06, -0.20)

    assert captured.value.code.value == "invalid_return"


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


def test_exact_kelly_borrowing_spread_changes_the_optimum_above_one_x() -> None:
    returns = [0.01, -0.009] * 100

    without_spread = exact_historical_kelly(returns, risk_free_rate=0.02)
    with_spread = exact_historical_kelly(
        returns,
        risk_free_rate=0.02,
        borrowing_spread=0.10,
    )

    assert without_spread.theoretical_fraction > 4.6
    assert isclose(with_spread.theoretical_fraction, 1.0, abs_tol=1e-6)
    assert (
        with_spread.theoretical_growth.annual_log_growth
        < without_spread.theoretical_growth.annual_log_growth
    )


def test_historical_financing_uses_total_borrow_rate_periodic_difference() -> None:
    result = historical_log_growth(
        [0.01],
        2.0,
        risk_free_rate=0.05,
        borrowing_spread=0.10,
    )
    cash_daily = annual_rate_to_periodic(0.05, 252)
    borrow_daily = annual_rate_to_periodic(0.15, 252)
    expected_multiplier = 1 + cash_daily + 2 * (0.01 - cash_daily) - (borrow_daily - cash_daily)
    assert isclose(result.annual_log_growth, log(expected_multiplier) * 252, rel_tol=1e-12)


def test_exact_kelly_marks_a_theoretical_search_bound_as_degraded() -> None:
    result = exact_historical_kelly([0.01] * 60)

    assert result.status == "degraded"
    assert result.reason == "search_bound_reached"
    assert result.theoretical_fraction == 100.0
    assert result.applied_fraction == 3.0


def test_applied_cap_cannot_exceed_v1_three_x_boundary() -> None:
    with pytest.raises(KellyLabError, match="v1 range"):
        single_asset_gbm_kelly(0.06, 0.20, leverage_cap=3.01)
