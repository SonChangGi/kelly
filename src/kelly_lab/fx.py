"""No-lookahead FX alignment and base-currency conversion helpers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime
from math import isfinite

from .errors import KellyLabError, ReasonCode


@dataclass(frozen=True)
class FXAlignmentResult:
    asset_dates: list[str]
    rates: list[float | None]
    source_dates: list[str | None]
    lag_days: list[int | None]
    reasons: list[str | None]
    max_lag_days: int
    status: str = "published"
    reason: str | None = None

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def _date(value: date | datetime | str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError) as error:
        raise KellyLabError(ReasonCode.INVALID_DATES, "dates must be ISO-8601 dates") from error


def _strictly_increasing(values: Sequence[date]) -> bool:
    return all(right > left for left, right in zip(values, values[1:], strict=False))


def align_fx_prior(
    asset_dates: Sequence[date | datetime | str],
    fx_dates: Sequence[date | datetime | str],
    fx_rates: Sequence[float],
    *,
    max_lag_days: int = 5,
) -> FXAlignmentResult:
    """Align each asset date to the latest prior FX fix within a calendar-day cap."""

    if len(fx_dates) != len(fx_rates):
        raise KellyLabError(ReasonCode.INVALID_DATES, "FX dates and rates must match")
    if max_lag_days < 0:
        raise KellyLabError(ReasonCode.INVALID_DATES, "maximum FX lag cannot be negative")
    assets = [_date(value) for value in asset_dates]
    fixes = [_date(value) for value in fx_dates]
    rates = [float(value) for value in fx_rates]
    if assets and not _strictly_increasing(assets):
        raise KellyLabError(ReasonCode.INVALID_DATES, "asset dates must be strictly increasing")
    if fixes and not _strictly_increasing(fixes):
        raise KellyLabError(ReasonCode.INVALID_DATES, "FX dates must be strictly increasing")
    if any(not isfinite(value) or value <= 0 for value in rates):
        raise KellyLabError(ReasonCode.INVALID_RETURN, "FX rates must be positive and finite")

    aligned_rates: list[float | None] = []
    aligned_dates: list[str | None] = []
    lags: list[int | None] = []
    reasons: list[str | None] = []
    pointer = -1
    for asset_date in assets:
        while pointer + 1 < len(fixes) and fixes[pointer + 1] <= asset_date:
            pointer += 1
        if pointer < 0:
            aligned_rates.append(None)
            aligned_dates.append(None)
            lags.append(None)
            reasons.append(ReasonCode.FX_MISSING.value)
            continue
        lag = (asset_date - fixes[pointer]).days
        if lag > max_lag_days:
            aligned_rates.append(None)
            aligned_dates.append(fixes[pointer].isoformat())
            lags.append(lag)
            reasons.append(ReasonCode.FX_TOO_STALE.value)
            continue
        aligned_rates.append(rates[pointer])
        aligned_dates.append(fixes[pointer].isoformat())
        lags.append(lag)
        reasons.append(None)

    missing_reasons = [reason for reason in reasons if reason is not None]
    overall_reason = missing_reasons[0] if missing_reasons else None
    return FXAlignmentResult(
        asset_dates=[value.isoformat() for value in assets],
        rates=aligned_rates,
        source_dates=aligned_dates,
        lag_days=lags,
        reasons=reasons,
        max_lag_days=max_lag_days,
        status="degraded" if missing_reasons else "published",
        reason=overall_reason,
    )


def convert_prices_to_base(
    prices: Sequence[float], aligned_fx_rates: Sequence[float | None]
) -> list[float]:
    """Multiply local-currency prices by quote-per-local FX rates."""

    if len(prices) != len(aligned_fx_rates):
        raise KellyLabError(ReasonCode.FX_MISSING, "prices and aligned FX must match")
    converted: list[float] = []
    for price, fx_rate in zip(prices, aligned_fx_rates, strict=True):
        value = float(price)
        if not isfinite(value) or value <= 0:
            raise KellyLabError(ReasonCode.INVALID_RETURN, "prices must be positive and finite")
        if fx_rate is None:
            raise KellyLabError(ReasonCode.FX_MISSING, "every price requires a valid prior FX fix")
        rate = float(fx_rate)
        if not isfinite(rate) or rate <= 0:
            raise KellyLabError(ReasonCode.INVALID_RETURN, "FX rates must be positive and finite")
        converted.append(value * rate)
    return converted


def simple_returns_from_prices(prices: Sequence[float]) -> list[float]:
    values = [float(value) for value in prices]
    if any(not isfinite(value) or value <= 0 for value in values):
        raise KellyLabError(ReasonCode.INVALID_RETURN, "prices must be positive and finite")
    return [current / previous - 1.0 for previous, current in zip(values, values[1:], strict=False)]
