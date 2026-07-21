"""Multi-asset Kelly allocation, covariance, and correlation validation."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass
from math import expm1, isfinite

import numpy as np

from .errors import KellyLabError, ReasonCode
from .metrics import TRADING_DAYS_PER_YEAR, annual_rate_to_periodic

MIN_COMMON_OBSERVATIONS = 60


@dataclass(frozen=True)
class MultiAssetKellyResult:
    theoretical_weights: list[float] | None
    applied_weights: list[float] | None
    theoretical_total_exposure: float | None
    applied_total_exposure: float | None
    theoretical_annual_log_growth: float | None
    applied_annual_log_growth: float | None
    applied_expected_geometric_return: float | None
    leverage_cap: float
    common_observations: int | None = None
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class MultiAssetExactResult:
    weights: list[float] | None
    total_exposure: float | None
    annual_log_growth: float | None
    expected_geometric_return: float | None
    observations: int
    leverage_cap: float
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class CovarianceEstimate:
    covariance: list[list[float]] | None
    correlation: list[list[float]] | None
    common_observations: int
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _array(values: Iterable[float], *, name: str) -> np.ndarray:
    result = np.asarray(list(values), dtype=float)
    if result.ndim != 1 or result.size == 0:
        raise KellyLabError(ReasonCode.INSUFFICIENT_OBSERVATIONS, f"{name} must be a vector")
    if not np.all(np.isfinite(result)):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, f"{name} must be finite")
    return result


def validate_correlation_matrix(
    matrix: Sequence[Sequence[float]], *, tolerance: float = 1e-10
) -> np.ndarray:
    """Validate and return an editable historical correlation matrix."""

    result = np.asarray(matrix, dtype=float)
    if result.ndim != 2 or result.shape[0] != result.shape[1] or result.shape[0] == 0:
        raise KellyLabError(
            ReasonCode.CORRELATION_NOT_SQUARE, "correlation matrix must be non-empty and square"
        )
    if not np.all(np.isfinite(result)):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "correlation matrix must be finite")
    if not np.allclose(result, result.T, atol=tolerance, rtol=0.0):
        raise KellyLabError(
            ReasonCode.CORRELATION_NOT_SYMMETRIC, "correlation matrix must be symmetric"
        )
    if np.any(result < -1.0 - tolerance) or np.any(result > 1.0 + tolerance):
        raise KellyLabError(
            ReasonCode.CORRELATION_OUT_OF_RANGE,
            "correlations must be between -1 and 1",
        )
    if not np.allclose(np.diag(result), 1.0, atol=tolerance, rtol=0.0):
        raise KellyLabError(
            ReasonCode.CORRELATION_DIAGONAL_INVALID,
            "correlation diagonal must equal 1",
        )
    eigenvalues = np.linalg.eigvalsh((result + result.T) / 2.0)
    if float(np.min(eigenvalues)) < -tolerance:
        raise KellyLabError(
            ReasonCode.CORRELATION_NOT_PSD,
            "correlation matrix must be positive semidefinite",
        )
    return result


def covariance_from_correlation(
    volatilities: Iterable[float], correlation: Sequence[Sequence[float]]
) -> np.ndarray:
    volatility = _array(volatilities, name="volatilities")
    if np.any(volatility < 0):
        raise KellyLabError(ReasonCode.INVALID_RETURN, "volatilities cannot be negative")
    correlation_array = validate_correlation_matrix(correlation)
    if correlation_array.shape[0] != volatility.size:
        raise KellyLabError(
            ReasonCode.CORRELATION_NOT_SQUARE,
            "correlation dimensions must match volatilities",
        )
    return np.outer(volatility, volatility) * correlation_array


def _validate_covariance(matrix: Sequence[Sequence[float]], size: int) -> np.ndarray:
    covariance = np.asarray(matrix, dtype=float)
    if covariance.ndim != 2 or covariance.shape[0] != covariance.shape[1]:
        raise KellyLabError(ReasonCode.COVARIANCE_NOT_SQUARE, "covariance must be square")
    if covariance.shape[0] != size:
        raise KellyLabError(
            ReasonCode.COVARIANCE_NOT_SQUARE,
            "covariance dimensions must match expected returns",
        )
    if not np.all(np.isfinite(covariance)):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "covariance must be finite")
    if not np.allclose(covariance, covariance.T, atol=1e-10, rtol=0.0):
        raise KellyLabError(ReasonCode.COVARIANCE_NOT_SYMMETRIC, "covariance must be symmetric")
    covariance = (covariance + covariance.T) / 2.0
    if float(np.min(np.linalg.eigvalsh(covariance))) < -1e-10:
        raise KellyLabError(
            ReasonCode.COVARIANCE_NOT_PSD,
            "covariance must be positive semidefinite",
        )
    return covariance


def estimate_covariance(
    returns_matrix: Sequence[Sequence[float | None]],
    *,
    annualization: int = TRADING_DAYS_PER_YEAR,
    minimum_common_observations: int = MIN_COMMON_OBSERVATIONS,
) -> CovarianceEstimate:
    """Estimate annual covariance using only fully common finite observations."""

    matrix = np.asarray(
        [[np.nan if value is None else float(value) for value in row] for row in returns_matrix],
        dtype=float,
    )
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS,
            "returns matrix must contain at least one asset",
        )
    common = matrix[np.all(np.isfinite(matrix), axis=1)]
    observations = int(common.shape[0])
    if observations < minimum_common_observations:
        return CovarianceEstimate(
            covariance=None,
            correlation=None,
            common_observations=observations,
            status="unavailable",
            reason=ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS.value,
        )
    covariance = np.cov(common, rowvar=False, ddof=1) * annualization
    covariance = np.atleast_2d(covariance)
    standard_deviation = np.sqrt(np.maximum(np.diag(covariance), 0.0))
    denominator = np.outer(standard_deviation, standard_deviation)
    with np.errstate(divide="ignore", invalid="ignore"):
        correlation = np.divide(
            covariance,
            denominator,
            out=np.zeros_like(covariance),
            where=denominator > 0,
        )
    np.fill_diagonal(correlation, 1.0)
    return CovarianceEstimate(
        covariance=covariance.tolist(),
        correlation=correlation.tolist(),
        common_observations=observations,
    )


def _project_nonnegative_l1(values: np.ndarray, cap: float) -> np.ndarray:
    positive = np.maximum(values, 0.0)
    if float(np.sum(positive)) <= cap:
        return positive
    sorted_values = np.sort(positive)[::-1]
    cumulative = np.cumsum(sorted_values)
    indices = np.nonzero(sorted_values * np.arange(1, sorted_values.size + 1) > cumulative - cap)[0]
    rho = int(indices[-1])
    threshold = (cumulative[rho] - cap) / (rho + 1)
    return np.maximum(positive - threshold, 0.0)


def _portfolio_log_growth(
    weights: np.ndarray,
    expected_excess_returns: np.ndarray,
    covariance: np.ndarray,
    risk_free_rate: float,
    borrowing_spread: float,
) -> float:
    exposure = float(np.sum(weights))
    return float(
        risk_free_rate
        + expected_excess_returns @ weights
        - 0.5 * weights @ covariance @ weights
        - max(exposure - 1.0, 0.0) * borrowing_spread
    )


def _constrained_gbm_weights(
    expected_excess_returns: np.ndarray,
    covariance: np.ndarray,
    leverage_cap: float,
    borrowing_spread: float,
) -> tuple[np.ndarray | None, bool]:
    n_assets = expected_excess_returns.size
    diagonal = np.maximum(np.diag(covariance), 1e-10)
    initial = _project_nonnegative_l1(
        np.maximum(expected_excess_returns / diagonal, 0.0), leverage_cap
    )
    try:
        from scipy.optimize import minimize

        result = minimize(
            lambda weights: -(
                expected_excess_returns @ weights
                - 0.5 * weights @ covariance @ weights
                - max(float(np.sum(weights)) - 1.0, 0.0) * borrowing_spread
            ),
            initial,
            method="SLSQP",
            bounds=[(0.0, leverage_cap)] * n_assets,
            constraints=[{"type": "ineq", "fun": lambda weights: leverage_cap - np.sum(weights)}],
            options={"ftol": 1e-12, "maxiter": 2_000},
        )
        if result.success and np.all(np.isfinite(result.x)):
            return _project_nonnegative_l1(np.asarray(result.x), leverage_cap), True
    except ImportError:
        pass

    # Deterministic projected-gradient fallback keeps the engine runnable in
    # minimal environments; CI uses SciPy and cross-checks this result.
    weights = initial
    spectral_norm = max(float(np.linalg.norm(covariance, ord=2)), 1e-8)
    step = 0.8 / spectral_norm
    for iteration in range(50_000):
        spread_gradient = borrowing_spread if float(np.sum(weights)) > 1.0 else 0.0
        gradient = expected_excess_returns - covariance @ weights - spread_gradient
        candidate = _project_nonnegative_l1(weights + step * gradient, leverage_cap)
        if float(np.max(np.abs(candidate - weights))) < 1e-10:
            return candidate, True
        weights = candidate
        if iteration and iteration % 2_000 == 0:
            step *= 0.8
    return weights, False


def multi_asset_gbm_kelly(
    expected_excess_returns: Iterable[float],
    covariance: Sequence[Sequence[float]],
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    leverage_cap: float = 3.0,
    common_observations: int | None = None,
    minimum_common_observations: int = MIN_COMMON_OBSERVATIONS,
) -> MultiAssetKellyResult:
    """Return unconstrained theory and a long-only, capped usable allocation."""

    expected = _array(expected_excess_returns, name="expected excess returns")
    covariance_array = _validate_covariance(covariance, expected.size)
    if any(
        not isfinite(float(value)) for value in (risk_free_rate, borrowing_spread, leverage_cap)
    ):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "portfolio inputs must be finite")
    if leverage_cap <= 0 or leverage_cap > 3.0:
        raise KellyLabError(
            ReasonCode.INVALID_LEVERAGE_CAP,
            "applied leverage cap must be in the v1 range (0, 3]",
        )
    if borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing spread cannot be negative")
    if common_observations is not None and common_observations < minimum_common_observations:
        return MultiAssetKellyResult(
            theoretical_weights=None,
            applied_weights=None,
            theoretical_total_exposure=None,
            applied_total_exposure=None,
            theoretical_annual_log_growth=None,
            applied_annual_log_growth=None,
            applied_expected_geometric_return=None,
            leverage_cap=float(leverage_cap),
            common_observations=common_observations,
            status="unavailable",
            reason=ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS.value,
        )

    singular = np.linalg.matrix_rank(covariance_array, tol=1e-12) < expected.size
    theoretical: np.ndarray | None = None
    theoretical_growth: float | None = None
    if not singular:
        theoretical = np.linalg.solve(covariance_array, expected)
        theoretical_growth = _portfolio_log_growth(
            theoretical,
            expected,
            covariance_array,
            float(risk_free_rate),
            float(borrowing_spread),
        )

    applied, converged = _constrained_gbm_weights(
        expected, covariance_array, float(leverage_cap), float(borrowing_spread)
    )
    if applied is None:
        return MultiAssetKellyResult(
            theoretical_weights=theoretical.tolist() if theoretical is not None else None,
            applied_weights=None,
            theoretical_total_exposure=(
                float(np.sum(theoretical)) if theoretical is not None else None
            ),
            applied_total_exposure=None,
            theoretical_annual_log_growth=theoretical_growth,
            applied_annual_log_growth=None,
            applied_expected_geometric_return=None,
            leverage_cap=float(leverage_cap),
            common_observations=common_observations,
            status="unavailable",
            reason=ReasonCode.OPTIMIZATION_FAILED.value,
        )
    applied_growth = _portfolio_log_growth(
        applied,
        expected,
        covariance_array,
        float(risk_free_rate),
        float(borrowing_spread),
    )
    reason = None
    status = "published"
    if singular:
        status = "degraded"
        reason = ReasonCode.SINGULAR_COVARIANCE.value
    elif not converged:
        status = "degraded"
        reason = ReasonCode.OPTIMIZATION_FAILED.value
    return MultiAssetKellyResult(
        theoretical_weights=theoretical.tolist() if theoretical is not None else None,
        applied_weights=applied.tolist(),
        theoretical_total_exposure=float(np.sum(theoretical)) if theoretical is not None else None,
        applied_total_exposure=float(np.sum(applied)),
        theoretical_annual_log_growth=theoretical_growth,
        applied_annual_log_growth=applied_growth,
        applied_expected_geometric_return=expm1(applied_growth),
        leverage_cap=float(leverage_cap),
        common_observations=common_observations,
        status=status,
        reason=reason,
    )


def multi_asset_exact_kelly(
    returns_matrix: Sequence[Sequence[float | None]],
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    annualization: int = TRADING_DAYS_PER_YEAR,
    leverage_cap: float = 3.0,
    minimum_common_observations: int = MIN_COMMON_OBSERVATIONS,
) -> MultiAssetExactResult:
    """Constrained in-sample exact Kelly for daily common return observations."""

    matrix = np.asarray(
        [[np.nan if value is None else float(value) for value in row] for row in returns_matrix],
        dtype=float,
    )
    if matrix.ndim != 2 or matrix.shape[1] == 0:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS,
            "returns matrix must contain at least one asset",
        )
    matrix = matrix[np.all(np.isfinite(matrix), axis=1)]
    observations, n_assets = matrix.shape
    if observations < minimum_common_observations:
        return MultiAssetExactResult(
            weights=None,
            total_exposure=None,
            annual_log_growth=None,
            expected_geometric_return=None,
            observations=int(observations),
            leverage_cap=float(leverage_cap),
            status="unavailable",
            reason=ReasonCode.INSUFFICIENT_COMMON_OBSERVATIONS.value,
        )
    if any(
        not isfinite(float(value)) for value in (risk_free_rate, borrowing_spread, leverage_cap)
    ):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "portfolio inputs must be finite")
    if leverage_cap <= 0 or leverage_cap > 3.0:
        raise KellyLabError(
            ReasonCode.INVALID_LEVERAGE_CAP,
            "applied leverage cap must be in the v1 range (0, 3]",
        )
    if borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing spread cannot be negative")
    risk_free_periodic = annual_rate_to_periodic(risk_free_rate, annualization)
    spread_periodic = annual_rate_to_periodic(borrowing_spread, annualization)
    excess = matrix - risk_free_periodic

    def multipliers(weights: np.ndarray) -> np.ndarray:
        return (
            1.0
            + risk_free_periodic
            + excess @ weights
            - max(float(np.sum(weights)) - 1.0, 0.0) * spread_periodic
        )

    def objective(weights: np.ndarray) -> float:
        values = multipliers(weights)
        if np.any(values <= 0):
            return 1e100
        return -float(np.mean(np.log(values)) * annualization)

    initial = np.full(n_assets, min(1.0, leverage_cap) / n_assets)
    try:
        from scipy.optimize import minimize

        result = minimize(
            objective,
            initial,
            method="SLSQP",
            bounds=[(0.0, leverage_cap)] * n_assets,
            constraints=[
                {"type": "ineq", "fun": lambda weights: leverage_cap - np.sum(weights)},
                {"type": "ineq", "fun": lambda weights: multipliers(weights) - 1e-12},
            ],
            options={"ftol": 1e-12, "maxiter": 4_000},
        )
        if not result.success or np.any(multipliers(result.x) <= 0):
            return MultiAssetExactResult(
                weights=None,
                total_exposure=None,
                annual_log_growth=None,
                expected_geometric_return=None,
                observations=int(observations),
                leverage_cap=float(leverage_cap),
                status="unavailable",
                reason=ReasonCode.OPTIMIZATION_FAILED.value,
            )
        weights = _project_nonnegative_l1(np.asarray(result.x), float(leverage_cap))
    except ImportError as error:
        raise RuntimeError("SciPy is required for exact multi-asset Kelly") from error

    values = multipliers(weights)
    if np.any(values <= 0):
        return MultiAssetExactResult(
            weights=weights.tolist(),
            total_exposure=float(np.sum(weights)),
            annual_log_growth=None,
            expected_geometric_return=None,
            observations=int(observations),
            leverage_cap=float(leverage_cap),
            status="ruin",
            reason=ReasonCode.RUIN.value,
        )
    annual_log_growth = float(np.mean(np.log(values)) * annualization)
    return MultiAssetExactResult(
        weights=weights.tolist(),
        total_exposure=float(np.sum(weights)),
        annual_log_growth=annual_log_growth,
        expected_geometric_return=expm1(annual_log_growth),
        observations=int(observations),
        leverage_cap=float(leverage_cap),
    )


# External API aliases.
portfolio_kelly = multi_asset_gbm_kelly
