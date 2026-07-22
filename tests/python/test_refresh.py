from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kelly_lab.providers import NormalizedPriceSeries, ProviderUnavailable
from kelly_lab.refresh import (
    _cross_check,
    _preserved_failure_document,
    _reason_code,
    _return_difference,
    _trim_to_identity_floor,
    _unavailable_document,
    merge_incremental,
    refresh,
)


def series(dates: tuple[str, ...], prices: tuple[float, ...]) -> NormalizedPriceSeries:
    return NormalizedPriceSeries(
        symbol="005930.KS",
        dates=dates,
        prices=prices,
        currency="KRW",
        exchange="KRX",
        timezone="Asia/Seoul",
        return_basis="price_return",
        provider="Korea Exchange",
        source_url="https://openapi.krx.co.kr/",
        attribution="Source: Korea Exchange",
    )


def total_return_series(dates: tuple[str, ...], prices: tuple[float, ...]) -> NormalizedPriceSeries:
    return NormalizedPriceSeries(
        symbol="AAPL",
        dates=dates,
        prices=prices,
        currency="USD",
        exchange="NASDAQ",
        timezone="America/New_York",
        return_basis="total_return_approximation",
        provider="Yahoo Finance",
        source_url="https://finance.yahoo.com/",
        attribution="Yahoo Finance adjusted close",
    )


def test_incremental_merge_preserves_frozen_history_and_appends() -> None:
    existing = {
        "state": "published",
        "dates": ["2026-07-17", "2026-07-20"],
        "prices": [100.0, 102.0],
    }
    fetched = series(
        ("2026-07-17", "2026-07-20", "2026-07-21"),
        (100.0, 102.0, 103.0),
    )
    merged = merge_incremental(existing, fetched, backfill=False)
    assert merged.dates == ("2026-07-17", "2026-07-20", "2026-07-21")
    assert merged.prices == (100.0, 102.0, 103.0)


def test_incremental_merge_requires_backfill_on_historical_drift() -> None:
    existing = {
        "state": "published",
        "dates": ["2026-07-17", "2026-07-20"],
        "prices": [100.0, 102.0],
    }
    fetched = series(("2026-07-17", "2026-07-20"), (100.0, 102.5))
    try:
        merge_incremental(existing, fetched, backfill=False)
    except ValueError as error:
        assert str(error) == "HISTORICAL_DRIFT_BACKFILL_REQUIRED"
    else:
        raise AssertionError("historical drift must fail closed")


def test_incremental_merge_requires_backfill_when_overlap_observation_disappears() -> None:
    existing = {
        "state": "published",
        "dates": ["2026-07-16", "2026-07-17", "2026-07-20"],
        "prices": [99.0, 100.0, 102.0],
    }
    fetched = series(("2026-07-16", "2026-07-20"), (99.0, 102.0))
    with pytest.raises(ValueError, match="OBSERVATION_REMOVED_BACKFILL_REQUIRED"):
        merge_incremental(existing, fetched, backfill=False)


def test_adjusted_close_rebase_preserves_returns_and_scales_appended_level() -> None:
    existing = {
        "state": "published",
        "dates": ["2026-07-17", "2026-07-20"],
        "prices": [100.0, 102.0],
    }
    fetched = total_return_series(
        ("2026-07-17", "2026-07-20", "2026-07-21"),
        (50.0, 51.0, 52.0),
    )

    merged = merge_incremental(existing, fetched, backfill=False)

    assert merged.dates == ("2026-07-17", "2026-07-20", "2026-07-21")
    assert merged.prices == pytest.approx((100.0, 102.0, 104.0))


def test_adjusted_close_merge_blocks_changed_historical_return() -> None:
    existing = {
        "state": "published",
        "dates": ["2026-07-17", "2026-07-20"],
        "prices": [100.0, 102.0],
    }
    fetched = total_return_series(("2026-07-17", "2026-07-20"), (50.0, 51.5))

    with pytest.raises(ValueError, match="HISTORICAL_DRIFT_BACKFILL_REQUIRED"):
        merge_incremental(existing, fetched, backfill=False)


def test_identity_floor_removes_reused_ticker_history() -> None:
    fetched = total_return_series(
        ("2026-06-11", "2026-06-12", "2026-06-15"),
        (10.0, 100.0, 101.0),
    )

    trimmed = _trim_to_identity_floor(fetched, "2026-06-12")

    assert trimmed.dates == ("2026-06-12", "2026-06-15")
    assert trimmed.prices == (100.0, 101.0)


def test_return_crosscheck_passes_level_rebase_but_rejects_return_mismatch() -> None:
    dates = tuple(f"2026-01-{day:02d}" for day in range(1, 23))
    primary = total_return_series(dates, tuple(100.0 + day for day in range(22)))
    rebased = series(dates, tuple((100.0 + day) * 2 for day in range(22)))
    mismatched_prices = list(rebased.prices)
    mismatched_prices[11] *= 1.5
    mismatched = series(dates, tuple(mismatched_prices))

    assert _return_difference(primary, rebased)["state"] == "passed"
    assert _return_difference(primary, mismatched)["state"] == "mismatch"


def test_fx_crosscheck_allows_different_daily_fix_times_but_blocks_unit_errors() -> None:
    dates = tuple(f"2026-01-{day:02d}" for day in range(1, 23))
    reference_prices = tuple(1500.0 + day for day in range(22))
    different_fix_prices = tuple(
        price * (1.003 if index % 2 else 0.997) for index, price in enumerate(reference_prices)
    )
    reference = series(dates, reference_prices)
    different_fix = series(dates, different_fix_prices)
    wrong_units = series(dates, tuple(price * 100 for price in reference_prices))

    assert _return_difference(reference, different_fix)["state"] == "mismatch"
    assert (
        _return_difference(
            reference,
            different_fix,
            median_tolerance=0.012,
            p99_tolerance=0.06,
            max_median_level_difference=0.03,
        )["state"]
        == "passed"
    )
    assert (
        _return_difference(
            reference,
            wrong_units,
            median_tolerance=0.012,
            p99_tolerance=0.06,
            max_median_level_difference=0.03,
        )["state"]
        == "mismatch"
    )


class UnavailableCrosscheckProvider:
    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        raise ProviderUnavailable("FINVIZ_ACCESS_UNAVAILABLE")


def test_finviz_crosscheck_access_failure_is_explicit_and_circuit_breaks() -> None:
    entry = {
        "symbol": "AAPL",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "currency": "USD",
    }
    dates = tuple(f"2026-01-{day:02d}" for day in range(1, 23))
    primary = total_return_series(dates, tuple(100.0 + day for day in range(22)))
    disabled: set[str] = set()

    result = _cross_check(
        entry,
        primary,
        date(2026, 1, 1),
        date(2026, 1, 22),
        yahoo_provider=UnavailableCrosscheckProvider(),
        stooq_provider=UnavailableCrosscheckProvider(),
        finviz_provider=UnavailableCrosscheckProvider(),
        fred_provider=UnavailableCrosscheckProvider(),
        disabled_providers=disabled,
    )

    assert result == {
        "provider": "finviz",
        "state": "unavailable",
        "commonObservations": 0,
        "medianAbsReturnDifference": None,
        "p99AbsReturnDifference": None,
    }
    assert disabled == {"finviz"}


def test_public_reason_codes_are_stable_and_never_serialize_exception_urls() -> None:
    error = RuntimeError(
        "401 Client Error for url: https://api.example.test/path?apikey=secret-value"
    )
    assert _reason_code(error) == "refresh_failed"
    assert "secret" not in _reason_code(error)
    assert (
        _reason_code(ValueError("HISTORICAL_DRIFT_BACKFILL_REQUIRED"))
        == "historical_drift_backfill_required"
    )
    assert (
        _reason_code(ProviderUnavailable("INDEPENDENT_SOURCE_MISMATCH"))
        == "independent_source_mismatch"
    )


class FakeKrxProvider:
    available = True
    configured = True
    rights_approved = True

    def history_many(
        self, symbols: list[str], _start: date, _end: date
    ) -> dict[str, NormalizedPriceSeries]:
        return {
            symbol: series(
                ("2026-07-17", "2026-07-20"),
                (100.0, 102.0 if symbol == "005930" else 99.0),
            )
            for symbol in symbols
        }


class DisabledTwelveProvider:
    available = False
    configured = False
    rights_approved = False


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def unavailable_asset(asset_id: str, symbol: str) -> dict[str, object]:
    return {
        "schemaVersion": 1,
        "contract": "kelly-asset-history",
        "state": "unavailable",
        "assetId": asset_id,
        "generatedAt": "2026-07-01T00:00:00+00:00",
        "dataAsOf": None,
        "metadata": {
            "symbol": symbol,
            "assetType": "equity",
            "exchange": "KRX",
            "timezone": "Asia/Seoul",
            "returnBasis": "price_return",
            "baseCurrency": "KRW",
        },
        "dates": [],
        "prices": [],
        "returns": [],
        "source": {
            "provider": "none",
            "normalized": True,
            "rawRedistribution": False,
            "license": "none",
            "attribution": "none",
            "cachedAt": None,
        },
        "limitations": ["unavailable"],
    }


def published_asset(asset_id: str = "kr-005930", symbol: str = "005930.KS") -> dict[str, object]:
    document = unavailable_asset(asset_id, symbol)
    document.update(
        {
            "state": "published",
            "dataAsOf": "2026-07-20",
            "dates": ["2026-07-17", "2026-07-20"],
            "prices": [100.0, 102.0],
            "returns": [None, 0.02],
            "source": {
                "provider": "krx",
                "normalized": True,
                "rawRedistribution": False,
                "license": "approved",
                "attribution": "한국거래소 통계정보",
                "cachedAt": "2026-07-20T12:00:00+00:00",
            },
            "limitations": ["price return"],
        }
    )
    return document


def test_rights_revocation_removes_previously_published_observations() -> None:
    result = _unavailable_document(
        published_asset(),
        generated_at="2026-07-21T00:00:00+00:00",
        reason="krx_public_display_rights_unconfirmed",
    )

    assert result["state"] == "unavailable"
    assert result["dates"] == []
    assert result["prices"] == []
    assert result["returns"] == []
    assert result["dataAsOf"] is None
    assert result["source"]["provider"] == "none"  # type: ignore[index]


def test_rights_approved_failed_refresh_preserves_data_with_honest_state() -> None:
    recent = _preserved_failure_document(
        published_asset(),
        generated_at="2026-07-21T00:00:00+00:00",
        as_of=date(2026, 7, 21),
        reason="provider_network_failure",
    )
    stale = _preserved_failure_document(
        published_asset(),
        generated_at="2026-08-02T00:00:00+00:00",
        as_of=date(2026, 8, 2),
        reason="provider_network_failure",
    )

    assert recent["state"] == "degraded"
    assert stale["state"] == "stale"
    assert recent["prices"] == [100.0, 102.0]
    assert recent["source"]["cachedAt"] == "2026-07-20T12:00:00+00:00"  # type: ignore[index]
    assert "provider_network_failure" in recent["limitations"]  # type: ignore[operator]


def test_refresh_joins_config_to_public_catalog_and_publishes_ranges(tmp_path: Path) -> None:
    entries = []
    config_entries = []
    for asset_id, provider_symbol, symbol in (
        ("kr-005930", "005930", "005930.KS"),
        ("kr-000660", "000660", "000660.KS"),
    ):
        entries.append(
            {
                "id": asset_id,
                "symbol": symbol,
                "name": symbol,
                "nameKo": symbol,
                "assetType": "equity",
                "exchange": "KRX",
                "currency": "KRW",
                "timezone": "Asia/Seoul",
                "status": "unavailable",
                "provider": {
                    "provider": "krx",
                    "symbol": provider_symbol,
                    "exchange": "KRX",
                },
                "searchTerms": [symbol],
                "dataPath": f"assets/{asset_id}.json",
                "returnBasis": "price_return",
                "availableFrom": None,
                "availableTo": None,
            }
        )
        config_entries.append(
            {
                "id": asset_id,
                "provider": "krx",
                "providerSymbol": provider_symbol,
                "providerExchange": "KRX",
                "symbol": symbol,
                "returnBasis": "price_return",
            }
        )
        write_json(
            tmp_path / "data" / "assets" / f"{asset_id}.json",
            unavailable_asset(asset_id, symbol),
        )

    write_json(
        tmp_path / "data/catalog.json",
        {"assets": entries, "state": "unavailable", "generatedAt": "old"},
    )
    write_json(
        tmp_path / "config/catalog.json",
        {"assets": config_entries},
    )
    write_json(
        tmp_path / "data/summary.json",
        {
            "state": "unavailable",
            "status": {},
            "coverage": {"availableAssetCount": 0},
            "primaryEntities": [{"id": "kelly-allocation-lab", "state": "unavailable"}],
        },
    )
    write_json(
        tmp_path / "data/automation-status.json",
        {
            "state": "unavailable",
            "lastSuccessAt": None,
            "provider": {"normalizedOnly": True},
            "publication": {"assetCount": 0, "latestPublishedAt": None},
        },
    )

    count = refresh(
        tmp_path,
        tmp_path / "config/catalog.json",
        backfill=True,
        start=date(2026, 7, 17),
        end=date(2026, 7, 20),
        krx_provider=FakeKrxProvider(),
        twelve_provider=DisabledTwelveProvider(),
    )

    assert count == 2
    catalog = json.loads((tmp_path / "data/catalog.json").read_text(encoding="utf-8"))
    assert catalog["state"] == "published"
    assert catalog["assets"][0]["availableFrom"] == "2026-07-17"
    assert catalog["assets"][0]["availableTo"] == "2026-07-20"
    published = json.loads((tmp_path / "data/assets/kr-005930.json").read_text(encoding="utf-8"))
    assert published["source"]["provider"] == "krx"
    assert published["returns"][1] == pytest.approx(0.02)
    automation = json.loads((tmp_path / "data/automation-status.json").read_text(encoding="utf-8"))
    assert automation["lastSuccessAt"] is not None
