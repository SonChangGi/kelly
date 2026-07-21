from __future__ import annotations

from datetime import date

import pytest

from kelly_lab.data_quality import (
    check_historical_drift,
    returns_digest,
    validate_price_series,
)
from kelly_lab.providers import NormalizedPriceSeries


def series(dates: tuple[str, ...], prices: tuple[float, ...]) -> NormalizedPriceSeries:
    return NormalizedPriceSeries(
        symbol="TEST",
        dates=dates,
        prices=prices,
        currency="USD",
        exchange="TEST",
        timezone="UTC",
        return_basis="price_return",
        provider="fixture",
        source_url="https://example.com/source",
        attribution="Fixture",
    )


def test_valid_series_is_published() -> None:
    result = validate_price_series(
        series(("2026-07-17", "2026-07-20"), (100.0, 101.0)),
        as_of=date(2026, 7, 21),
    )
    assert result.accepted
    assert result.status == "published"


def test_unsorted_or_nonpositive_prices_fail_closed() -> None:
    result = validate_price_series(
        series(("2026-07-20", "2026-07-20"), (100.0, 0.0)),
    )
    assert not result.accepted
    assert result.status == "unavailable"
    assert {issue.code for issue in result.issues} >= {
        "DATES_NOT_STRICTLY_ASCENDING",
        "PRICE_NOT_POSITIVE",
    }


def test_single_observation_cannot_be_published() -> None:
    result = validate_price_series(series(("2026-07-20",), (100.0,)))
    assert not result.accepted
    assert result.status == "unavailable"
    assert "INSUFFICIENT_OBSERVATIONS" in {issue.code for issue in result.issues}


def test_historical_drift_requires_explicit_backfill() -> None:
    dates = ("2026-07-17", "2026-07-20", "2026-07-21")
    prices = (100.0, 101.0, 102.0)
    digest = returns_digest(dates, prices, through="2026-07-20")
    check_historical_drift(
        dates,
        prices,
        frozen_through="2026-07-20",
        expected_digest=digest,
    )
    with pytest.raises(ValueError, match="HISTORICAL_DRIFT_BACKFILL_REQUIRED"):
        check_historical_drift(
            dates,
            (100.0, 99.0, 102.0),
            frozen_through="2026-07-20",
            expected_digest=digest,
        )
