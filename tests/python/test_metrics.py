from __future__ import annotations

from math import isclose

from kelly_lab.metrics import calculate_metrics, maximum_drawdown


def test_maximum_drawdown_is_positive_25_percent() -> None:
    assert isclose(maximum_drawdown([1.0, 1.2, 0.9, 1.1]), 0.25)


def test_metrics_keep_arithmetic_return_and_cagr_separate() -> None:
    returns = [0.10, -0.10]
    result = calculate_metrics(
        returns,
        dates=["2024-01-01", "2024-07-01", "2025-01-01"],
    )

    assert isclose(result.annual_arithmetic_return, 0.0, abs_tol=1e-15)
    expected_cagr = 0.99 ** (365.2425 / 366.0) - 1.0
    assert isclose(result.cagr, expected_cagr, rel_tol=1e-12)
    assert isclose(result.cumulative_return, -0.01, abs_tol=1e-15)


def test_n_plus_one_price_dates_use_full_elapsed_span() -> None:
    result = calculate_metrics(
        [0.10],
        dates=["2023-01-01", "2024-01-01"],
    )

    expected = 1.10 ** (365.2425 / 365.0) - 1.0
    assert isclose(result.cagr, expected, rel_tol=1e-12)


def test_undefined_ratios_have_reason_codes_not_zeroes() -> None:
    result = calculate_metrics(
        [0.01, 0.01],
        dates=["2024-01-01", "2024-01-02", "2024-01-03"],
    )

    assert result.sharpe is None
    assert result.reasons["sharpe"] == "zero_volatility"
    assert result.sortino is None
    assert result.reasons["sortino"] == "zero_downside_deviation"


def test_non_positive_multiplier_is_ruin() -> None:
    result = calculate_metrics(
        [0.10, -1.0],
        dates=["2024-01-01", "2024-01-02", "2024-01-03"],
    )

    assert result.status == "ruin"
    assert result.cagr is None
    assert result.reasons["cagr"] == "ruin"
