from __future__ import annotations

import json
from math import isclose
from pathlib import Path

from kelly_lab.fx import (
    align_fx_prior,
    convert_prices_to_base,
    simple_returns_from_prices,
)
from kelly_lab.kelly import (
    binomial_kelly_fraction,
    exact_historical_kelly,
    historical_log_growth,
    single_asset_gbm_kelly,
)
from kelly_lab.metrics import calculate_metrics
from kelly_lab.portfolio import covariance_from_correlation, multi_asset_gbm_kelly
from kelly_lab.rebalance import simulate_rebalancing

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "golden.json").read_text(encoding="utf-8")
)


def _assert_close_list(
    actual: list[float], expected: list[float], tolerance: float = 1e-12
) -> None:
    assert len(actual) == len(expected)
    for actual_value, expected_value in zip(actual, expected, strict=True):
        assert isclose(actual_value, expected_value, rel_tol=tolerance, abs_tol=tolerance)


def test_python_engine_matches_shared_gbm_fixture() -> None:
    case = FIXTURE["gbm"]
    result = single_asset_gbm_kelly(
        case["inputs"]["expectedExcessReturn"],
        case["inputs"]["volatility"],
        risk_free_rate=case["inputs"]["riskFreeRate"],
        borrowing_spread=case["inputs"]["borrowingSpread"],
        leverage_cap=case["inputs"]["cap"],
    )
    expected = case["expected"]
    assert isclose(result.theoretical_fraction, expected["theoreticalFraction"], rel_tol=1e-12)
    assert isclose(result.full_kelly_growth.annual_log_growth, expected["fullLogGrowth"])
    assert isclose(
        result.full_kelly_growth.expected_geometric_return,
        expected["fullGeometricReturn"],
    )
    assert isclose(result.two_x_growth.annual_log_growth, expected["twoXLogGrowth"])
    assert isclose(
        result.two_x_growth.expected_arithmetic_return,
        expected["twoXArithmeticReturn"],
    )
    assert isclose(
        result.presets["half"]["fraction_of_max_excess_growth"],
        expected["halfFractionOfMaximumExcessGrowth"],
    )


def test_python_engine_matches_shared_metric_and_ruin_fixtures() -> None:
    metric_case = FIXTURE["metrics"]
    returns = simple_returns_from_prices(metric_case["prices"])
    metrics = calculate_metrics(returns, dates=metric_case["dates"])
    assert isclose(metrics.cumulative_return, metric_case["expected"]["cumulativeReturn"])
    assert isclose(metrics.max_drawdown, metric_case["expected"]["maximumDrawdown"])

    binomial = FIXTURE["binomial"]
    assert isclose(
        binomial_kelly_fraction(
            binomial["winProbability"],
            win_return=binomial["winReturn"],
            loss_return=binomial["lossReturn"],
        ),
        binomial["expectedFraction"],
    )
    ruin = FIXTURE["ruin"]
    result = historical_log_growth(ruin["returns"], ruin["leverage"])
    assert result.status == ruin["expectedStatus"]


def test_python_engine_matches_shared_exact_historical_fixture() -> None:
    case = FIXTURE["exactHistorical"]
    inputs = case["inputs"]
    returns = inputs["returnPattern"] * inputs["repetitions"]
    result = exact_historical_kelly(
        returns,
        risk_free_rate=inputs["riskFreeRate"],
        borrowing_spread=inputs["borrowingSpread"],
        annualization=inputs["annualizationDays"],
        leverage_cap=inputs["cap"],
        theoretical_search_cap=inputs["searchCap"],
    )
    expected = case["expected"]

    assert result.status == expected["status"]
    assert isclose(
        result.theoretical_fraction,
        expected["theoreticalFraction"],
        rel_tol=1e-6,
        abs_tol=1e-6,
    )
    assert isclose(result.applied_fraction, expected["appliedFraction"])
    assert isclose(
        result.theoretical_growth.annual_log_growth,
        expected["annualLogGrowth"],
        rel_tol=1e-12,
    )
    assert isclose(
        result.applied_growth.annual_log_growth,
        expected["appliedAnnualLogGrowth"],
        rel_tol=1e-12,
    )
    assert isclose(
        result.applied_growth.expected_geometric_return,
        expected["appliedAnnualGrowth"],
        rel_tol=1e-12,
    )


def test_python_engine_matches_shared_multi_asset_fixture() -> None:
    case = FIXTURE["multiAssetGbm"]
    inputs = case["inputs"]
    covariance = covariance_from_correlation(inputs["volatilities"], inputs["correlation"])
    result = multi_asset_gbm_kelly(
        inputs["expectedExcessReturns"],
        covariance,
        risk_free_rate=inputs["riskFreeRate"],
        borrowing_spread=inputs["borrowingSpread"],
        leverage_cap=inputs["cap"],
        common_observations=inputs["commonObservations"],
    )
    expected = case["expected"]

    assert result.status == expected["status"]
    _assert_close_list(result.theoretical_weights, expected["theoreticalWeights"])
    _assert_close_list(result.applied_weights, expected["appliedWeights"], tolerance=1e-9)
    assert isclose(result.theoretical_total_exposure, expected["theoreticalTotalExposure"])
    assert isclose(result.applied_total_exposure, expected["appliedTotalExposure"])
    assert isclose(
        result.theoretical_annual_log_growth,
        expected["theoreticalAnnualLogGrowth"],
    )
    assert isclose(result.applied_annual_log_growth, expected["appliedAnnualLogGrowth"])
    assert isclose(
        result.applied_expected_geometric_return,
        expected["appliedAnnualGrowth"],
    )


def test_python_engine_matches_shared_singular_multi_asset_fixture() -> None:
    case = FIXTURE["singularMultiAssetGbm"]
    inputs = case["inputs"]
    covariance = covariance_from_correlation(inputs["volatilities"], inputs["correlation"])
    result = multi_asset_gbm_kelly(
        inputs["expectedExcessReturns"],
        covariance,
        risk_free_rate=inputs["riskFreeRate"],
        borrowing_spread=inputs["borrowingSpread"],
        leverage_cap=inputs["cap"],
        common_observations=inputs["commonObservations"],
    )
    expected = case["expected"]

    assert result.status == expected["status"]
    assert result.reason == expected["reason"]
    assert result.theoretical_weights is expected["theoreticalWeights"]
    assert result.theoretical_total_exposure is expected["theoreticalTotalExposure"]
    assert result.theoretical_annual_log_growth is expected["theoreticalAnnualLogGrowth"]
    _assert_close_list(result.applied_weights, expected["appliedWeights"], tolerance=1e-6)
    assert isclose(
        result.applied_total_exposure,
        expected["appliedTotalExposure"],
        rel_tol=1e-6,
    )
    assert isclose(result.applied_annual_log_growth, expected["appliedAnnualLogGrowth"])
    assert isclose(
        result.applied_expected_geometric_return,
        expected["appliedAnnualGrowth"],
    )


def test_python_engine_matches_shared_prior_fx_fixture() -> None:
    case = FIXTURE["fxPrior"]
    inputs = case["inputs"]
    result = align_fx_prior(
        inputs["assetDates"],
        inputs["fxDates"],
        inputs["fxRates"],
        max_lag_days=inputs["maxLagDays"],
    )
    expected = case["expected"]

    assert result.status == expected["status"]
    assert result.rates == expected["alignedRates"]
    assert result.source_dates == expected["sourceDates"]
    assert result.lag_days == expected["lagDays"]
    _assert_close_list(
        convert_prices_to_base(inputs["assetPrices"], result.rates),
        expected["convertedPrices"],
    )


def test_python_engine_matches_shared_rebalancing_fixture() -> None:
    case = FIXTURE["rebalancing"]
    inputs = case["inputs"]
    result = simulate_rebalancing(
        inputs["returnsMatrix"],
        inputs["targetWeights"],
        dates=inputs["dates"],
        frequency=inputs["frequency"],
        one_way_cost_bps=inputs["oneWayCostBps"],
        risk_free_rate=inputs["riskFreeRate"],
        borrowing_spread=inputs["borrowingSpread"],
        annualization=inputs["annualizationDays"],
    )
    expected = case["expected"]

    assert result.status == expected["status"]
    _assert_close_list(result.buy_and_hold_wealth, expected["buyAndHoldWealth"])
    _assert_close_list(result.gross_rebalanced_wealth, expected["grossWealth"])
    _assert_close_list(result.net_rebalanced_wealth, expected["netWealth"])
    assert isclose(result.gross_rebalancing_effect, expected["grossRebalancingEffect"])
    assert isclose(result.trading_cost_drag, expected["tradingCostDrag"])
    assert isclose(result.net_rebalancing_effect, expected["netRebalancingEffect"])
    assert isclose(result.total_turnover, expected["turnover"])
    assert isclose(result.trading_cost_paid, expected["tradingCostPaid"])
    assert result.rebalance_count == expected["rebalanceCount"]


def test_python_engine_matches_shared_rebalancing_ruin_fixture() -> None:
    case = FIXTURE["rebalancingRuin"]
    inputs = case["inputs"]
    result = simulate_rebalancing(
        inputs["returnsMatrix"],
        inputs["targetWeights"],
        dates=inputs["dates"],
        frequency=inputs["frequency"],
        one_way_cost_bps=inputs["oneWayCostBps"],
        risk_free_rate=inputs["riskFreeRate"],
        borrowing_spread=inputs["borrowingSpread"],
        annualization=inputs["annualizationDays"],
    )

    assert result.status == case["expectedStatus"]
    assert result.reason == case["expectedStatus"]
