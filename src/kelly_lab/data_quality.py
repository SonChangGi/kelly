from __future__ import annotations

import hashlib
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime

import numpy as np

from .providers import NormalizedPriceSeries


@dataclass(frozen=True)
class QualityIssue:
    code: str
    severity: str
    detail: str


@dataclass(frozen=True)
class QualityReport:
    accepted: bool
    status: str
    issues: tuple[QualityIssue, ...]
    observation_count: int
    first_date: str | None
    last_date: str | None


def _parse_iso(value: str) -> date:
    return datetime.strptime(value[:10], "%Y-%m-%d").date()


def validate_price_series(
    series: NormalizedPriceSeries,
    *,
    as_of: date | None = None,
    freshness_days: int = 7,
    anomaly_return: float = 0.80,
) -> QualityReport:
    issues: list[QualityIssue] = []
    dates = list(series.dates)
    prices = np.asarray(series.prices, dtype=float)
    parsed: list[date] = []
    try:
        parsed = [_parse_iso(value) for value in dates]
    except ValueError as error:
        issues.append(QualityIssue("DATE_FORMAT_INVALID", "critical", str(error)))

    if len(dates) != len(prices):
        issues.append(QualityIssue("COLUMN_LENGTH_MISMATCH", "critical", "dates and prices differ"))
    if len(dates) < 2:
        issues.append(
            QualityIssue(
                "INSUFFICIENT_OBSERVATIONS",
                "critical",
                "at least two daily prices are required for publication",
            )
        )
    if parsed and any(
        current <= previous for previous, current in zip(parsed, parsed[1:], strict=False)
    ):
        issues.append(
            QualityIssue(
                "DATES_NOT_STRICTLY_ASCENDING",
                "critical",
                "duplicate or unsorted date",
            )
        )
    if not np.isfinite(prices).all():
        issues.append(QualityIssue("PRICE_NOT_FINITE", "critical", "NaN or infinite price"))
    if np.any(prices <= 0):
        issues.append(QualityIssue("PRICE_NOT_POSITIVE", "critical", "price must be positive"))

    if len(prices) >= 2 and np.all(prices[:-1] > 0):
        returns = prices[1:] / prices[:-1] - 1.0
        positions = np.flatnonzero(np.abs(returns) > anomaly_return)
        for position in positions[:5]:
            issues.append(
                QualityIssue(
                    "RETURN_ANOMALY_REVIEW_REQUIRED",
                    "high",
                    f"{dates[position + 1]} simple return {returns[position]:.6f}",
                )
            )

    if as_of and parsed:
        lag = (as_of - parsed[-1]).days
        if lag > freshness_days:
            issues.append(
                QualityIssue("SERIES_STALE", "high", f"last observation is {lag} days old")
            )

    critical = any(issue.severity == "critical" for issue in issues)
    stale = any(issue.code == "SERIES_STALE" for issue in issues)
    degraded = any(issue.severity == "high" for issue in issues)
    if critical:
        status = "unavailable"
    elif stale:
        status = "stale"
    elif degraded:
        status = "degraded"
    else:
        status = "published"
    return QualityReport(
        accepted=not critical,
        status=status,
        issues=tuple(issues),
        observation_count=len(dates),
        first_date=dates[0] if dates else None,
        last_date=dates[-1] if dates else None,
    )


def returns_digest(
    dates: Sequence[str], prices: Sequence[float], *, through: str | None = None
) -> str:
    if len(dates) != len(prices):
        raise ValueError("dates and prices must have the same length")
    selected = [
        (day, float(price))
        for day, price in zip(dates, prices, strict=True)
        if through is None or day <= through
    ]
    if len(selected) < 2:
        raise ValueError("at least two prices are required")
    returns = [selected[index][1] / selected[index - 1][1] - 1 for index in range(1, len(selected))]
    payload = "\n".join(
        f"{selected[index][0]}:{value:.12g}" for index, value in enumerate(returns, start=1)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def check_historical_drift(
    dates: Sequence[str],
    prices: Sequence[float],
    *,
    frozen_through: str,
    expected_digest: str,
) -> None:
    actual = returns_digest(dates, prices, through=frozen_through)
    if actual != expected_digest:
        raise ValueError("HISTORICAL_DRIFT_BACKFILL_REQUIRED")
