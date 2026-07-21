"""Performance metrics with explicit undefined and ruin semantics."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from math import isfinite, sqrt

from .errors import KellyLabError, ReasonCode

TRADING_DAYS_PER_YEAR = 252
CALENDAR_DAYS_PER_YEAR = 365.2425


@dataclass(frozen=True)
class PerformanceMetrics:
    observations: int
    cumulative_return: float | None
    annual_arithmetic_return: float | None
    cagr: float | None
    annual_volatility: float | None
    max_drawdown: float | None
    sharpe: float | None
    sortino: float | None
    calmar_style: float | None
    status: str = "published"
    reasons: dict[str, str] = field(default_factory=dict)

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _values(values: Iterable[float]) -> list[float]:
    result = [float(value) for value in values]
    if any(not isfinite(value) for value in result):
        raise KellyLabError(ReasonCode.NON_FINITE_INPUT, "returns must be finite")
    return result


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise KellyLabError(
            ReasonCode.INVALID_DATES, "dates must be ISO-8601 calendar dates"
        ) from error


def annual_rate_to_periodic(rate: float, periods: int = TRADING_DAYS_PER_YEAR) -> float:
    """Convert an effective annual rate to an effective periodic rate."""

    rate = float(rate)
    if not isfinite(rate) or rate <= -1:
        raise KellyLabError(ReasonCode.INVALID_RATE, "annual rate must be finite and > -1")
    if periods <= 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "periods must be positive")
    return (1.0 + rate) ** (1.0 / periods) - 1.0


def annual_borrowing_spread_to_periodic(
    risk_free_rate: float,
    borrowing_spread: float,
    periods: int = TRADING_DAYS_PER_YEAR,
) -> float:
    """Convert an additive annual borrowing spread into periodic financing drag."""

    risk_free_rate = float(risk_free_rate)
    borrowing_spread = float(borrowing_spread)
    if borrowing_spread < 0 or risk_free_rate + borrowing_spread <= -1:
        raise KellyLabError(
            ReasonCode.INVALID_RATE,
            "borrowing spread must be non-negative and total borrowing rate greater than -1",
        )
    return annual_rate_to_periodic(
        risk_free_rate + borrowing_spread, periods
    ) - annual_rate_to_periodic(risk_free_rate, periods)


def wealth_index(returns: Iterable[float], *, initial: float = 1.0) -> list[float]:
    """Return wealth including the initial observation.

    A multiplier at or below zero is ruin, not a value to clip or silently
    continue through.
    """

    values = _values(returns)
    if not isfinite(initial) or initial <= 0:
        raise KellyLabError(ReasonCode.INVALID_RETURN, "initial wealth must be positive")
    wealth = [float(initial)]
    for value in values:
        multiplier = 1.0 + value
        if multiplier <= 0:
            raise KellyLabError(
                ReasonCode.RUIN, "a period return produced a non-positive asset multiplier"
            )
        wealth.append(wealth[-1] * multiplier)
    return wealth


def maximum_drawdown(wealth: Sequence[float]) -> float:
    """Return maximum drawdown as a positive fraction (for example, 0.25)."""

    values = _values(wealth)
    if not values or values[0] <= 0 or any(value <= 0 for value in values):
        raise KellyLabError(ReasonCode.INVALID_RETURN, "wealth must be positive")
    peak = values[0]
    result = 0.0
    for value in values:
        peak = max(peak, value)
        result = max(result, (peak - value) / peak)
    return result


def _sample_standard_deviation(values: Sequence[float]) -> float:
    if len(values) < 2:
        raise KellyLabError(
            ReasonCode.INSUFFICIENT_OBSERVATIONS,
            "at least two observations are required for sample volatility",
        )
    mean = sum(values) / len(values)
    return sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))


def calculate_metrics(
    returns: Iterable[float],
    *,
    dates: Sequence[date | datetime | str] | None = None,
    risk_free_rate: float = 0.0,
    mar: float | None = None,
    annualization: int = TRADING_DAYS_PER_YEAR,
    calendar_days_per_year: float = CALENDAR_DAYS_PER_YEAR,
) -> PerformanceMetrics:
    """Calculate the fixed v1 historical-performance contract.

    ``annual_arithmetic_return`` is the annualized arithmetic mean and is kept
    separate from ``cagr``.  Sharpe uses daily effective excess returns.
    Sortino's downside denominator averages squared shortfalls across *all*
    observations, including zero contributions for periods above the target.
    """

    values = _values(returns)
    n = len(values)
    reasons: dict[str, str] = {}
    if n == 0:
        reason = ReasonCode.INSUFFICIENT_OBSERVATIONS.value
        return PerformanceMetrics(
            observations=0,
            cumulative_return=None,
            annual_arithmetic_return=None,
            cagr=None,
            annual_volatility=None,
            max_drawdown=None,
            sharpe=None,
            sortino=None,
            calmar_style=None,
            status="unavailable",
            reasons={
                name: reason
                for name in (
                    "cumulative_return",
                    "annual_arithmetic_return",
                    "cagr",
                    "annual_volatility",
                    "max_drawdown",
                    "sharpe",
                    "sortino",
                    "calmar_style",
                )
            },
        )

    if annualization <= 0 or not isfinite(calendar_days_per_year) or calendar_days_per_year <= 0:
        raise KellyLabError(ReasonCode.INVALID_RATE, "annualization constants must be positive")

    try:
        wealth = wealth_index(values)
    except KellyLabError as error:
        if error.code is not ReasonCode.RUIN:
            raise
        reason = ReasonCode.RUIN.value
        return PerformanceMetrics(
            observations=n,
            cumulative_return=None,
            annual_arithmetic_return=(sum(values) / n) * annualization,
            cagr=None,
            annual_volatility=None,
            max_drawdown=1.0,
            sharpe=None,
            sortino=None,
            calmar_style=None,
            status="ruin",
            reasons={
                "cumulative_return": reason,
                "cagr": reason,
                "annual_volatility": reason,
                "sharpe": reason,
                "sortino": reason,
                "calmar_style": reason,
            },
        )

    cumulative_return = wealth[-1] - 1.0
    arithmetic_return = (sum(values) / n) * annualization
    mdd = maximum_drawdown(wealth)

    parsed_dates: list[date] | None = None
    dates_include_initial_observation = False
    if dates is not None:
        if len(dates) not in (n, n + 1):
            raise KellyLabError(
                ReasonCode.INVALID_DATES,
                "dates must contain one date per return or the preferred N+1 price dates",
            )
        parsed_dates = [_date(value) for value in dates]
        dates_include_initial_observation = len(parsed_dates) == n + 1
        if any(right <= left for left, right in zip(parsed_dates, parsed_dates[1:], strict=False)):
            raise KellyLabError(ReasonCode.INVALID_DATES, "dates must be strictly increasing")

    cagr: float | None = None
    if parsed_dates is None:
        reasons["cagr"] = ReasonCode.INSUFFICIENT_OBSERVATIONS.value
    elif not dates_include_initial_observation:
        years = n / annualization
        cagr = wealth[-1] ** (1.0 / years) - 1.0
    else:
        elapsed_days = (parsed_dates[-1] - parsed_dates[0]).days
        if elapsed_days <= 0:
            reasons["cagr"] = ReasonCode.INSUFFICIENT_OBSERVATIONS.value
        else:
            years = elapsed_days / calendar_days_per_year
            cagr = wealth[-1] ** (1.0 / years) - 1.0

    annual_volatility: float | None = None
    sharpe: float | None = None
    if n < 2:
        reasons["annual_volatility"] = ReasonCode.INSUFFICIENT_OBSERVATIONS.value
        reasons["sharpe"] = ReasonCode.INSUFFICIENT_OBSERVATIONS.value
    else:
        periodic_volatility = _sample_standard_deviation(values)
        annual_volatility = periodic_volatility * sqrt(annualization)
        if periodic_volatility <= 0:
            reasons["sharpe"] = ReasonCode.ZERO_VOLATILITY.value
        else:
            risk_free_periodic = annual_rate_to_periodic(risk_free_rate, annualization)
            sharpe = (
                (sum(values) / n - risk_free_periodic) / periodic_volatility * sqrt(annualization)
            )

    target_annual = risk_free_rate if mar is None else float(mar)
    target_periodic = annual_rate_to_periodic(target_annual, annualization)
    excess = [value - target_periodic for value in values]
    downside_periodic = sqrt(sum(min(value, 0.0) ** 2 for value in excess) / n)
    sortino: float | None = None
    if downside_periodic <= 0:
        reasons["sortino"] = ReasonCode.ZERO_DOWNSIDE_DEVIATION.value
    else:
        sortino = (sum(excess) / n * annualization) / (downside_periodic * sqrt(annualization))

    calmar: float | None = None
    if cagr is None:
        reasons["calmar_style"] = reasons.get("cagr", ReasonCode.INSUFFICIENT_OBSERVATIONS.value)
    elif mdd <= 0:
        reasons["calmar_style"] = ReasonCode.ZERO_MAX_DRAWDOWN.value
    else:
        calmar = cagr / mdd

    return PerformanceMetrics(
        observations=n,
        cumulative_return=cumulative_return,
        annual_arithmetic_return=arithmetic_return,
        cagr=cagr,
        annual_volatility=annual_volatility,
        max_drawdown=mdd,
        sharpe=sharpe,
        sortino=sortino,
        calmar_style=calmar,
        status="published",
        reasons=reasons,
    )


# A descriptive alias is convenient for consumers and keeps the public API clear.
performance_metrics = calculate_metrics
