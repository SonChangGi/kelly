from __future__ import annotations

from math import isclose

import numpy as np
import pytest

from kelly_lab.errors import KellyLabError
from kelly_lab.portfolio import (
    covariance_from_correlation,
    estimate_covariance,
    multi_asset_exact_kelly,
    multi_asset_gbm_kelly,
    validate_correlation_matrix,
)


def test_multi_asset_theory_and_long_only_cap() -> None:
    result = multi_asset_gbm_kelly(
        [0.20, 0.16],
        [[0.04, 0.0], [0.0, 0.04]],
        common_observations=100,
    )

    assert np.allclose(result.theoretical_weights, [5.0, 4.0])
    assert sum(result.applied_weights) <= 3.0 + 1e-10
    assert all(weight >= 0 for weight in result.applied_weights)


def test_singular_covariance_keeps_constrained_result_but_flags_theory() -> None:
    result = multi_asset_gbm_kelly(
        [0.06, 0.06],
        [[0.04, 0.04], [0.04, 0.04]],
        common_observations=100,
    )

    assert result.status == "degraded"
    assert result.reason == "singular_covariance"
    assert result.theoretical_weights is None
    assert result.applied_weights is not None


@pytest.mark.parametrize("volatility", [0.0, 1e-13])
def test_direct_multi_asset_rejects_zero_or_tiny_input_volatility(volatility: float) -> None:
    with pytest.raises(KellyLabError) as captured:
        covariance_from_correlation([0.2, volatility], [[1.0, 0.0], [0.0, 1.0]])

    assert captured.value.code.value == "zero_volatility"


def test_direct_multi_asset_rejects_zero_variance_covariance() -> None:
    with pytest.raises(KellyLabError) as captured:
        multi_asset_gbm_kelly([0.06, 0.03], [[0.04, 0.0], [0.0, 0.0]])

    assert captured.value.code.value == "zero_volatility"


def test_fewer_than_60_common_returns_is_unavailable() -> None:
    result = multi_asset_gbm_kelly(
        [0.06, 0.04],
        [[0.04, 0.0], [0.0, 0.04]],
        common_observations=59,
    )

    assert result.status == "unavailable"
    assert result.reason == "insufficient_common_observations"


def test_covariance_estimate_drops_non_common_rows() -> None:
    rows = [[0.01 + index / 100_000, 0.02 + ((-1) ** index) / 10_000] for index in range(60)] + [
        [0.01, None]
    ]
    result = estimate_covariance(rows)

    assert result.status == "published"
    assert result.common_observations == 60


@pytest.mark.parametrize(
    "rows",
    [
        [[0.01, 0.02] for _ in range(60)],
        [[0.01 + ((-1) ** index) * 1e-15, 0.02 + index / 100_000] for index in range(60)],
    ],
)
def test_covariance_estimate_rejects_zero_or_tiny_volatility(
    rows: list[list[float]],
) -> None:
    result = estimate_covariance(rows)

    assert result.status == "unavailable"
    assert result.reason == "zero_volatility"
    assert result.covariance is None
    assert result.correlation is None


def test_covariance_estimate_rejects_non_finite_numeric_returns() -> None:
    rows = [[0.01 + index / 100_000, 0.02 + index / 90_000] for index in range(60)]
    rows[10][0] = float("inf")

    with pytest.raises(KellyLabError) as captured:
        estimate_covariance(rows)

    assert captured.value.code.value == "non_finite_input"


def test_multi_asset_exact_rejects_non_finite_numeric_returns() -> None:
    rows = [[0.01, -0.005], [-0.005, 0.01]] * 30
    rows[10][0] = float("nan")

    with pytest.raises(KellyLabError) as captured:
        multi_asset_exact_kelly(rows)

    assert captured.value.code.value == "non_finite_input"


def test_covariance_estimate_rejects_invalid_annualization() -> None:
    rows = [[0.01 + index / 100_000, 0.02 + index / 90_000] for index in range(60)]

    with pytest.raises(KellyLabError) as captured:
        estimate_covariance(rows, annualization=0)

    assert captured.value.code.value == "invalid_rate"


@pytest.mark.parametrize(
    ("matrix", "reason"),
    [
        ([[1.0, 0.2], [0.1, 1.0]], "correlation_not_symmetric"),
        ([[1.0, 1.2], [1.2, 1.0]], "correlation_out_of_range"),
        (
            [[1.0, 0.9, 0.9], [0.9, 1.0, -0.9], [0.9, -0.9, 1.0]],
            "correlation_not_psd",
        ),
    ],
)
def test_invalid_correlation_is_rejected(matrix: list[list[float]], reason: str) -> None:
    with pytest.raises(KellyLabError) as captured:
        validate_correlation_matrix(matrix)

    assert captured.value.code.value == reason


def test_multi_asset_exact_kelly_uses_common_sample_and_cap() -> None:
    rows = [[0.015, -0.005], [-0.005, 0.015]] * 35
    result = multi_asset_exact_kelly(rows)

    assert result.status == "published"
    assert result.observations == 70
    assert sum(result.weights) <= 3.0 + 1e-9
    assert isclose(result.total_exposure, sum(result.weights), rel_tol=1e-12)
