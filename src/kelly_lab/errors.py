"""Structured errors and reason codes used by the calculation engine.

Public results use stable machine-readable codes.  UI copy is deliberately kept
out of the numerical package so a missing metric can never be confused with a
numeric zero.
"""

from __future__ import annotations

from enum import StrEnum


class ReasonCode(StrEnum):
    INSUFFICIENT_OBSERVATIONS = "insufficient_observations"
    INSUFFICIENT_COMMON_OBSERVATIONS = "insufficient_common_observations"
    NON_FINITE_INPUT = "non_finite_input"
    INVALID_RETURN = "invalid_return"
    INVALID_RATE = "invalid_rate"
    ZERO_VOLATILITY = "zero_volatility"
    ZERO_DOWNSIDE_DEVIATION = "zero_downside_deviation"
    ZERO_MAX_DRAWDOWN = "zero_max_drawdown"
    RUIN = "ruin"
    SINGULAR_COVARIANCE = "singular_covariance"
    COVARIANCE_NOT_SQUARE = "covariance_not_square"
    COVARIANCE_NOT_SYMMETRIC = "covariance_not_symmetric"
    COVARIANCE_NOT_PSD = "covariance_not_psd"
    CORRELATION_NOT_SQUARE = "correlation_not_square"
    CORRELATION_NOT_SYMMETRIC = "correlation_not_symmetric"
    CORRELATION_OUT_OF_RANGE = "correlation_out_of_range"
    CORRELATION_DIAGONAL_INVALID = "correlation_diagonal_invalid"
    CORRELATION_NOT_PSD = "correlation_not_psd"
    INVALID_LEVERAGE_CAP = "invalid_leverage_cap"
    OPTIMIZATION_FAILED = "optimization_failed"
    SEARCH_BOUND_REACHED = "search_bound_reached"
    INVALID_FREQUENCY = "invalid_frequency"
    INVALID_TARGET_WEIGHTS = "invalid_target_weights"
    INVALID_COST = "invalid_cost"
    INVALID_DATES = "invalid_dates"
    FX_MISSING = "fx_missing"
    FX_TOO_STALE = "fx_too_stale"


class KellyLabError(ValueError):
    """A validation error with a stable public reason code."""

    def __init__(self, code: ReasonCode | str, message: str):
        self.code = ReasonCode(code)
        super().__init__(message)

    def as_dict(self) -> dict[str, str]:
        return {"code": self.code.value, "message": str(self)}
