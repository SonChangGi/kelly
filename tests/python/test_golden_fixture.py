from __future__ import annotations

import json
from math import isclose
from pathlib import Path

from kelly_lab.fx import simple_returns_from_prices
from kelly_lab.kelly import (
    binomial_kelly_fraction,
    historical_log_growth,
    single_asset_gbm_kelly,
)
from kelly_lab.metrics import calculate_metrics

FIXTURE = json.loads(
    (Path(__file__).parents[1] / "fixtures" / "golden.json").read_text(encoding="utf-8")
)


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
