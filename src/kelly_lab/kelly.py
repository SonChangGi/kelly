"""Single-asset Kelly calculations for assumptions and historical returns."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import asdict, dataclass, field
from math import expm1, inf, isfinite, log

from .errors import KellyLabError, ReasonCode
from .metrics import TRADING_DAYS_PER_YEAR, annual_rate_to_periodic


@dataclass(frozen=True)
class GrowthEvaluation:
    fraction: float
    annual_log_growth: float | None
    expected_geometric_return: float | None
    expected_arithmetic_return: float | None = None
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class SingleAssetKellyResult:
    theoretical_fraction: float | None
    spread_adjusted_fraction: float | None
    applied_fraction: float | None
    leverage_cap: float
    theoretical_growth: GrowthEvaluation | None
    full_kelly_growth: GrowthEvaluation | None
    applied_growth: GrowthEvaluation | None
    two_x_growth: GrowthEvaluation | None
    presets: dict[str, dict[str, float | None]] = field(default_factory=dict)
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class ExactKellyResult:
    theoretical_fraction: float | None
    applied_fraction: float | None
    theoretical_growth: GrowthEvaluation | None
    applied_growth: GrowthEvaluation | None
    observations: int
    leverage_cap: float
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def gbm_growth_rate(
    fraction: float,
    expected_excess_return: float,
    volatility: float,
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
) -> float:
    """Expected annual log-growth under the single-asset GBM model."""

    values = (
        float(fraction),
        float(expected_excess_return),
        float(volatility),
        float(risk_free_rate),
        float(borrowing_spread),
    )
    if any(not isfinite(value) for value in values):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "GBM inputs must be finite")
    fraction, expected_excess_return, volatility, risk_free_rate, borrowing_spread = values
    if volatility < 0:
        raise KellyLabError(ReasonCode.ZERO_VOLATILITY, "volatility cannot be negative")
    if borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing spread cannot be negative")
    return (
        risk_free_rate
        + fraction * expected_excess_return
        - 0.5 * fraction * fraction * volatility * volatility
        - max(fraction - 1.0, 0.0) * borrowing_spread
    )


def _growth_evaluation(
    fraction: float,
    expected_excess_return: float,
    volatility: float,
    risk_free_rate: float,
    borrowing_spread: float,
) -> GrowthEvaluation:
    growth = gbm_growth_rate(
        fraction,
        expected_excess_return,
        volatility,
        risk_free_rate=risk_free_rate,
        borrowing_spread=borrowing_spread,
    )
    expected_arithmetic_return = expm1(
        risk_free_rate
        + fraction * expected_excess_return
        - max(fraction - 1.0, 0.0) * borrowing_spread
    )
    return GrowthEvaluation(
        fraction,
        growth,
        expm1(growth),
        expected_arithmetic_return=expected_arithmetic_return,
    )


def _spread_adjusted_optimum(
    expected_excess_return: float, variance: float, borrowing_spread: float
) -> float:
    # The objective is concave with one kink at 1x.  Comparing its stationary
    # points and the kink avoids hiding borrowing-cost effects in an optimizer.
    candidates = [0.0, 1.0]
    candidates.append(expected_excess_return / variance)
    candidates.append((expected_excess_return - borrowing_spread) / variance)
    feasible = [max(0.0, value) for value in candidates]
    return max(
        feasible,
        key=lambda value: (
            value * expected_excess_return
            - 0.5 * value * value * variance
            - max(value - 1.0, 0.0) * borrowing_spread
        ),
    )


def single_asset_gbm_kelly(
    expected_excess_return: float,
    volatility: float,
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    leverage_cap: float = 3.0,
) -> SingleAssetKellyResult:
    """Calculate theoretical, cost-adjusted, capped, and 2x GBM outcomes."""

    expected_excess_return = float(expected_excess_return)
    volatility = float(volatility)
    risk_free_rate = float(risk_free_rate)
    borrowing_spread = float(borrowing_spread)
    leverage_cap = float(leverage_cap)
    if any(
        not isfinite(value)
        for value in (
            expected_excess_return,
            volatility,
            risk_free_rate,
            borrowing_spread,
            leverage_cap,
        )
    ):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "Kelly inputs must be finite")
    if leverage_cap <= 0 or leverage_cap > 3.0:
        raise KellyLabError(
            ReasonCode.INVALID_LEVERAGE_CAP,
            "applied leverage cap must be in the v1 range (0, 3]",
        )
    if borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing spread cannot be negative")
    if volatility <= 0:
        return SingleAssetKellyResult(
            theoretical_fraction=None,
            spread_adjusted_fraction=None,
            applied_fraction=None,
            leverage_cap=leverage_cap,
            theoretical_growth=None,
            full_kelly_growth=None,
            applied_growth=None,
            two_x_growth=None,
            status="unavailable",
            reason=ReasonCode.ZERO_VOLATILITY.value,
        )

    variance = volatility * volatility
    theoretical = expected_excess_return / variance
    adjusted = _spread_adjusted_optimum(expected_excess_return, variance, borrowing_spread)
    # v1 does not implement shorting.  A negative theoretical Kelly is still
    # shown, while path calculations apply a zero lower bound and the 3x cap.
    applied = min(max(adjusted, 0.0), leverage_cap)
    theoretical_growth = _growth_evaluation(
        theoretical,
        expected_excess_return,
        volatility,
        risk_free_rate,
        borrowing_spread,
    )
    full_growth = _growth_evaluation(
        adjusted,
        expected_excess_return,
        volatility,
        risk_free_rate,
        borrowing_spread,
    )
    applied_growth = _growth_evaluation(
        applied,
        expected_excess_return,
        volatility,
        risk_free_rate,
        borrowing_spread,
    )
    two_x_growth = _growth_evaluation(
        2.0,
        expected_excess_return,
        volatility,
        risk_free_rate,
        borrowing_spread,
    )

    max_excess_growth = full_growth.annual_log_growth - risk_free_rate
    presets: dict[str, dict[str, float | None]] = {}
    for name, scale in (("quarter", 0.25), ("half", 0.5), ("full", 1.0)):
        raw_fraction = adjusted * scale
        path_fraction = min(max(raw_fraction, 0.0), leverage_cap)
        evaluation = _growth_evaluation(
            path_fraction,
            expected_excess_return,
            volatility,
            risk_free_rate,
            borrowing_spread,
        )
        excess_growth = evaluation.annual_log_growth - risk_free_rate
        fraction_of_max = excess_growth / max_excess_growth if max_excess_growth > 0 else None
        presets[name] = {
            "raw_fraction": raw_fraction,
            "applied_fraction": path_fraction,
            "annual_log_growth": evaluation.annual_log_growth,
            "expected_geometric_return": evaluation.expected_geometric_return,
            "fraction_of_max_excess_growth": fraction_of_max,
        }

    return SingleAssetKellyResult(
        theoretical_fraction=theoretical,
        spread_adjusted_fraction=adjusted,
        applied_fraction=applied,
        leverage_cap=leverage_cap,
        theoretical_growth=theoretical_growth,
        full_kelly_growth=full_growth,
        applied_growth=applied_growth,
        two_x_growth=two_x_growth,
        presets=presets,
    )


def binomial_kelly_fraction(
    win_probability: float,
    *,
    win_return: float = 1.0,
    loss_return: float = -1.0,
) -> float:
    """Closed-form Kelly fraction for a two-outcome wager."""

    p = float(win_probability)
    win_return = float(win_return)
    loss_return = float(loss_return)
    if any(not isfinite(value) for value in (p, win_return, loss_return)):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "binomial inputs must be finite")
    if not 0 <= p <= 1 or win_return <= 0 or loss_return >= 0:
        raise KellyLabError(
            ReasonCode.INVALID_RETURN,
            "probability must be in [0, 1], with a positive win and negative loss",
        )
    return -(p * win_return + (1.0 - p) * loss_return) / (win_return * loss_return)


def historical_log_growth(
    returns: Iterable[float],
    fraction: float,
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    annualization: int = TRADING_DAYS_PER_YEAR,
) -> GrowthEvaluation:
    """Evaluate a daily-rebalanced historical path without clipping ruin."""

    values = [float(value) for value in returns]
    if not values:
        return GrowthEvaluation(
            float(fraction),
            None,
            None,
            status="unavailable",
            reason=ReasonCode.INSUFFICIENT_OBSERVATIONS.value,
        )
    inputs = values + [float(fraction), float(risk_free_rate), float(borrowing_spread)]
    if any(not isfinite(value) for value in inputs):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "historical Kelly inputs must be finite")
    if borrowing_spread < 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "borrowing spread cannot be negative")
    risk_free_periodic = annual_rate_to_periodic(risk_free_rate, annualization)
    spread_periodic = annual_rate_to_periodic(borrowing_spread, annualization)
    logs: list[float] = []
    for asset_return in values:
        multiplier = (
            1.0
            + risk_free_periodic
            + fraction * (asset_return - risk_free_periodic)
            - max(fraction - 1.0, 0.0) * spread_periodic
        )
        if multiplier <= 0:
            return GrowthEvaluation(
                float(fraction),
                None,
                None,
                status="ruin",
                reason=ReasonCode.RUIN.value,
            )
        logs.append(log(multiplier))
    annual_log_growth = sum(logs) / len(logs) * annualization
    return GrowthEvaluation(float(fraction), annual_log_growth, expm1(annual_log_growth))


def _golden_section_maximum(
    function: Callable[[float], float], lower: float, upper: float, *, iterations: int = 180
) -> tuple[float, float]:
    if upper <= lower:
        value = function(lower)
        return lower, value
    ratio = (5.0**0.5 - 1.0) / 2.0
    left = upper - ratio * (upper - lower)
    right = lower + ratio * (upper - lower)
    left_value = function(left)
    right_value = function(right)
    for _ in range(iterations):
        if left_value < right_value:
            lower = left
            left = right
            left_value = right_value
            right = lower + ratio * (upper - lower)
            right_value = function(right)
        else:
            upper = right
            right = left
            right_value = left_value
            left = upper - ratio * (upper - lower)
            left_value = function(left)
    point = (lower + upper) / 2.0
    return point, function(point)


def exact_historical_kelly(
    returns: Iterable[float],
    *,
    risk_free_rate: float = 0.0,
    borrowing_spread: float = 0.0,
    annualization: int = TRADING_DAYS_PER_YEAR,
    leverage_cap: float = 3.0,
    theoretical_search_cap: float = 100.0,
) -> ExactKellyResult:
    """Maximize the in-sample daily-rebalanced historical log objective.

    The theoretical optimum is searched over its strictly positive wealth
    domain.  The separately reported applied path is capped at ``leverage_cap``.
    """

    values = [float(value) for value in returns]
    if any(not isfinite(value) for value in values):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "returns must be finite")
    if leverage_cap <= 0 or leverage_cap > 3.0 or theoretical_search_cap <= 0:
        raise KellyLabError(
            ReasonCode.INVALID_LEVERAGE_CAP,
            "applied leverage cap must be in (0, 3] and search cap must be positive",
        )
    if len(values) < 2:
        return ExactKellyResult(
            theoretical_fraction=None,
            applied_fraction=None,
            theoretical_growth=None,
            applied_growth=None,
            observations=len(values),
            leverage_cap=float(leverage_cap),
            status="unavailable",
            reason=ReasonCode.INSUFFICIENT_OBSERVATIONS.value,
        )

    risk_free_periodic = annual_rate_to_periodic(risk_free_rate, annualization)
    spread_periodic = annual_rate_to_periodic(borrowing_spread, annualization)
    upper = float(theoretical_search_cap)
    # For f > 1 the multiplier is affine in f.  Find the first ruin boundary.
    for asset_return in values:
        slope = asset_return - risk_free_periodic - spread_periodic
        intercept = 1.0 + risk_free_periodic + spread_periodic
        if slope < 0:
            upper = min(upper, (-intercept / slope) * (1.0 - 1e-12))
    upper = max(0.0, upper)

    def objective(fraction: float) -> float:
        evaluation = historical_log_growth(
            values,
            fraction,
            risk_free_rate=risk_free_rate,
            borrowing_spread=borrowing_spread,
            annualization=annualization,
        )
        return evaluation.annual_log_growth if evaluation.annual_log_growth is not None else -inf

    candidates: list[tuple[float, float]] = [(0.0, objective(0.0))]
    first_upper = min(1.0, upper)
    candidates.append(_golden_section_maximum(objective, 0.0, first_upper))
    candidates.append((first_upper, objective(first_upper)))
    if upper > 1.0:
        candidates.append(_golden_section_maximum(objective, 1.0, upper))
        candidates.append((upper, objective(upper)))
    optimum, _ = max(candidates, key=lambda pair: pair[1])
    optimum = max(0.0, optimum)
    theoretical_growth = historical_log_growth(
        values,
        optimum,
        risk_free_rate=risk_free_rate,
        borrowing_spread=borrowing_spread,
        annualization=annualization,
    )
    applied = min(optimum, float(leverage_cap))
    applied_growth = historical_log_growth(
        values,
        applied,
        risk_free_rate=risk_free_rate,
        borrowing_spread=borrowing_spread,
        annualization=annualization,
    )
    hit_search_cap = (
        abs(optimum - theoretical_search_cap) <= max(1e-8, theoretical_search_cap * 1e-7)
        and upper == theoretical_search_cap
    )
    return ExactKellyResult(
        theoretical_fraction=optimum,
        applied_fraction=applied,
        theoretical_growth=theoretical_growth,
        applied_growth=applied_growth,
        observations=len(values),
        leverage_cap=float(leverage_cap),
        status="degraded" if hit_search_cap else "published",
        reason=ReasonCode.SEARCH_BOUND_REACHED.value if hit_search_cap else None,
    )


# Concise aliases for external callers.
gbm_kelly = single_asset_gbm_kelly
exact_kelly = exact_historical_kelly
