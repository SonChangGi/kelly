from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kelly_lab.providers import NormalizedPriceSeries, ProviderUnavailable
from kelly_lab.refresh import (
    _cross_check,
    _fetch_free_series,
    _free_provider_chain,
    _preserved_failure_document,
    _reason_code,
    _refresh_exit_code,
    _refresh_succeeded,
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

    result = _return_difference(primary, rebased)
    assert result["state"] == "passed"
    assert result["commonObservations"] == 21
    assert result["windowStart"] == "2026-01-01"
    assert result["windowEnd"] == "2026-01-22"
    assert _return_difference(primary, mismatched)["state"] == "mismatch"


def test_return_crosscheck_labels_recent_incremental_window() -> None:
    primary_dates = tuple(f"2026-01-{day:02d}" for day in range(1, 31))
    secondary_dates = primary_dates[-24:]
    primary = total_return_series(
        primary_dates,
        tuple(100.0 + day for day in range(len(primary_dates))),
    )
    secondary = series(
        secondary_dates,
        tuple((100.0 + day) * 2 for day in range(6, len(primary_dates))),
    )

    result = _return_difference(primary, secondary)

    assert result["state"] == "passed"
    assert result["commonObservations"] == 23
    assert result["windowStart"] == "2026-01-07"
    assert result["windowEnd"] == "2026-01-30"


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
        "windowStart": None,
        "windowEnd": None,
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
    assert (
        _reason_code(RuntimeError("DATA_QUALITY_REJECTED:stock-aapl"))
        == "data_quality_rejected_stock_aapl"
    )


def test_expected_unavailable_reasons_are_successful_skips() -> None:
    assert _refresh_exit_code(0, ["yahoo_public_display_rights_unconfirmed"]) == 0
    assert (
        _refresh_exit_code(
            0,
            ["krx_api_key_unavailable", "krx_public_display_rights_unconfirmed"],
        )
        == 0
    )
    assert _refresh_exit_code(1, []) == 0
    assert _refresh_exit_code(0, []) == 1
    assert _refresh_exit_code(1, ["fred_access_unavailable"]) == 1
    assert _refresh_succeeded(1, ["yahoo_public_display_rights_unconfirmed"]) is True
    assert _refresh_succeeded(1, ["fred_access_unavailable"]) is False
    assert _refresh_succeeded(0, ["yahoo_public_display_rights_unconfirmed"]) is False


def test_unapproved_yahoo_keeps_same_basis_stooq_index_fallback() -> None:
    yahoo = NeverCalledProvider()
    fdr = NeverCalledProvider()
    stooq = NeverCalledProvider()
    fred = NeverCalledProvider()
    index_entry = {
        "assetType": "index",
        "returnBasis": "price_return",
    }
    equity_entry = {
        "assetType": "equity",
        "returnBasis": "total_return_approximation",
    }

    index_chain = _free_provider_chain(
        index_entry,
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
        fred_provider=fred,
        yahoo_allowed=False,
    )
    equity_chain = _free_provider_chain(
        equity_entry,
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
        fred_provider=fred,
        yahoo_allowed=False,
    )

    assert [name for name, _provider in index_chain] == ["stooq"]
    assert equity_chain == []

    stooq_series, adapter_failures = _fetch_free_series(
        {
            "symbol": "^GSPC",
            "assetType": "index",
            "exchange": "INDEX",
            "currency": "USD",
            "returnBasis": "price_return",
        },
        date(2026, 7, 17),
        date(2026, 7, 20),
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=FakeStooqIndexProvider(),
        fred_provider=fred,
        yahoo_allowed=False,
    )
    assert stooq_series.provider == "Stooq"
    assert adapter_failures == ["yahoo_public_display_rights_unconfirmed"]


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


class NeverCalledProvider:
    def __init__(self) -> None:
        self.calls = 0

    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        self.calls += 1
        raise AssertionError("provider must not be called")


class FakeFredProvider:
    def __init__(self) -> None:
        self.calls = 0

    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        self.calls += 1
        return NormalizedPriceSeries(
            symbol="USD/KRW",
            dates=("2026-07-17", "2026-07-20"),
            prices=(1380.0, 1385.0),
            currency="KRW",
            exchange="FX",
            timezone="UTC",
            return_basis="fx_rate",
            provider="FRED DEXKOUS",
            source_url="https://fred.stlouisfed.org/series/DEXKOUS",
            attribution="Federal Reserve Bank of St. Louis",
        )


class FakeYahooProvider:
    def __init__(self) -> None:
        self.calls = 0

    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        self.calls += 1
        return total_return_series(
            ("2026-07-17", "2026-07-20"),
            (200.0, 204.0),
        )


class FakeStooqIndexProvider:
    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        return NormalizedPriceSeries(
            symbol="^GSPC",
            dates=("2026-07-17", "2026-07-20"),
            prices=(6200.0, 6250.0),
            currency="USD",
            exchange="INDEX",
            timezone="America/New_York",
            return_basis="price_return",
            provider="Stooq",
            source_url="https://stooq.com/",
            attribution="Stooq",
        )


class FakeFinvizProvider:
    def history(self, *_args: object, **_kwargs: object) -> NormalizedPriceSeries:
        return NormalizedPriceSeries(
            symbol="AAPL",
            dates=("2026-07-17", "2026-07-20"),
            prices=(200.0, 204.0),
            currency="USD",
            exchange="NASDAQ",
            timezone="America/New_York",
            return_basis="price_return",
            provider="Finviz",
            source_url="https://finviz.com/quote.ashx?t=AAPL",
            attribution="Finviz cross-check",
        )


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


def test_unapproved_yahoo_is_skipped_without_blocking_persisted_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {
        "id": "stock-aapl",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "nameKo": "애플",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "timezone": "America/New_York",
        "status": "published",
        "provider": {
            "provider": "yahoo_finance",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
        },
        "searchTerms": ["AAPL"],
        "dataPath": "assets/stock-aapl.json",
        "returnBasis": "total_return_approximation",
        "availableFrom": "2026-07-17",
        "availableTo": "2026-07-20",
    }
    config_path = tmp_path / "config/catalog.json"
    existing = published_asset("stock-aapl", "AAPL")
    existing["metadata"] = {
        "symbol": "AAPL",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "timezone": "America/New_York",
        "returnBasis": "total_return_approximation",
        "baseCurrency": "USD",
    }
    existing["source"] = {
        "provider": "yahoo_finance",
        "adapter": "native",
        "normalized": True,
        "rawRedistribution": False,
        "sourceUrl": "https://finance.yahoo.com/",
        "license": "Yahoo Finance research data; no vendor license asserted",
        "attribution": "Yahoo Finance adjusted close",
        "cachedAt": "2026-07-20T12:00:00+00:00",
        "contentDigest": "0" * 64,
    }
    write_json(
        tmp_path / "data/catalog.json",
        {"assets": [entry], "state": "published", "generatedAt": "2026-07-20T12:00:00+00:00"},
    )
    write_json(config_path, {"assets": [{"id": "stock-aapl"}]})
    write_json(tmp_path / "data/assets/stock-aapl.json", existing)
    write_json(
        tmp_path / "data/summary.json",
        {
            "state": "published",
            "status": {},
            "coverage": {"availableAssetCount": 1},
            "primaryEntities": [],
        },
    )
    previous_success = "2026-07-20T12:00:00+00:00"
    write_json(
        tmp_path / "data/automation-status.json",
        {
            "state": "published",
            "lastSuccessAt": previous_success,
            "provider": {"normalizedOnly": True},
            "publication": {"assetCount": 1, "latestPublishedAt": previous_success},
        },
    )
    yahoo = NeverCalledProvider()
    fdr = NeverCalledProvider()
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    count = refresh(
        tmp_path,
        config_path,
        end=date(2026, 7, 21),
        asset_ids={"stock-aapl"},
        krx_provider=FakeKrxProvider(),
        twelve_provider=DisabledTwelveProvider(),
        yahoo_provider=yahoo,  # type: ignore[arg-type]
        fdr_provider=fdr,  # type: ignore[arg-type]
        stooq_provider=NeverCalledProvider(),  # type: ignore[arg-type]
        fred_provider=NeverCalledProvider(),  # type: ignore[arg-type]
        finviz_provider=NeverCalledProvider(),  # type: ignore[arg-type]
    )

    document = json.loads((tmp_path / "data/assets/stock-aapl.json").read_text(encoding="utf-8"))
    automation = json.loads((tmp_path / "data/automation-status.json").read_text(encoding="utf-8"))
    providers = {item["name"]: item for item in automation["provider"]["providers"]}
    assert count == 0
    assert yahoo.calls == 0
    assert fdr.calls == 0
    assert document["state"] == "degraded"
    assert document["prices"] == [100.0, 102.0]
    assert "yahoo_public_display_rights_unconfirmed" in document["limitations"]
    assert automation["lastAttemptAt"] != previous_success
    assert automation["lastSuccessAt"] == previous_success
    assert automation["reasonCodes"] == ["yahoo_public_display_rights_unconfirmed"]
    assert providers["yahoo_finance"]["rightsApproved"] is False
    assert providers["finance_data_reader"]["rightsApproved"] is False


def test_unapproved_yahoo_does_not_block_fred_in_a_mixed_refresh(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    yahoo_entry = {
        "id": "stock-aapl",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "nameKo": "애플",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "timezone": "America/New_York",
        "status": "published",
        "provider": {
            "provider": "yahoo_finance",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
        },
        "searchTerms": ["AAPL"],
        "dataPath": "assets/stock-aapl.json",
        "returnBasis": "total_return_approximation",
        "availableFrom": "2026-07-17",
        "availableTo": "2026-07-20",
    }
    fred_entry = {
        "id": "fx-usd-krw",
        "symbol": "USD/KRW",
        "name": "US Dollar / Korean Won",
        "nameKo": "미국 달러/원 환율",
        "assetType": "fx",
        "exchange": "FX",
        "currency": "KRW",
        "timezone": "UTC",
        "status": "unavailable",
        "provider": {"provider": "fred", "symbol": "DEXKOUS", "exchange": "FX"},
        "searchTerms": ["USD/KRW"],
        "dataPath": "assets/fx-usd-krw.json",
        "returnBasis": "fx_rate",
        "availableFrom": None,
        "availableTo": None,
    }
    yahoo_document = published_asset("stock-aapl", "AAPL")
    yahoo_document["metadata"] = {
        "symbol": "AAPL",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "timezone": "America/New_York",
        "returnBasis": "total_return_approximation",
        "baseCurrency": "USD",
    }
    yahoo_document["source"] = {
        "provider": "yahoo_finance",
        "adapter": "native",
        "normalized": True,
        "rawRedistribution": False,
        "sourceUrl": "https://finance.yahoo.com/",
        "license": "Yahoo Finance research data; no vendor license asserted",
        "attribution": "Yahoo Finance adjusted close",
        "cachedAt": "2026-07-20T12:00:00+00:00",
        "contentDigest": "0" * 64,
    }
    fx_document = unavailable_asset("fx-usd-krw", "USD/KRW")
    fx_document["metadata"] = {
        "symbol": "USD/KRW",
        "assetType": "fx",
        "exchange": "FX",
        "timezone": "UTC",
        "returnBasis": "fx_rate",
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
    }
    write_json(
        tmp_path / "data/catalog.json",
        {
            "assets": [yahoo_entry, fred_entry],
            "state": "degraded",
            "generatedAt": "2026-07-20T12:00:00+00:00",
        },
    )
    write_json(
        tmp_path / "config/catalog.json",
        {"assets": [{"id": "stock-aapl"}, {"id": "fx-usd-krw"}]},
    )
    write_json(tmp_path / "data/assets/stock-aapl.json", yahoo_document)
    write_json(tmp_path / "data/assets/fx-usd-krw.json", fx_document)
    write_json(
        tmp_path / "data/summary.json",
        {
            "state": "degraded",
            "status": {},
            "coverage": {"availableAssetCount": 1},
            "primaryEntities": [],
        },
    )
    write_json(
        tmp_path / "data/automation-status.json",
        {
            "state": "degraded",
            "lastSuccessAt": "2026-07-20T12:00:00+00:00",
            "provider": {"normalizedOnly": True},
            "publication": {
                "assetCount": 1,
                "latestPublishedAt": "2026-07-20T12:00:00+00:00",
            },
        },
    )
    yahoo = NeverCalledProvider()
    fdr = NeverCalledProvider()
    fred = FakeFredProvider()
    stooq = NeverCalledProvider()
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    count = refresh(
        tmp_path,
        tmp_path / "config/catalog.json",
        backfill=True,
        start=date(2026, 7, 17),
        end=date(2026, 7, 20),
        krx_provider=FakeKrxProvider(),
        twelve_provider=DisabledTwelveProvider(),
        yahoo_provider=yahoo,  # type: ignore[arg-type]
        fdr_provider=fdr,  # type: ignore[arg-type]
        stooq_provider=stooq,  # type: ignore[arg-type]
        fred_provider=fred,  # type: ignore[arg-type]
        finviz_provider=NeverCalledProvider(),  # type: ignore[arg-type]
    )

    yahoo_result = json.loads(
        (tmp_path / "data/assets/stock-aapl.json").read_text(encoding="utf-8")
    )
    fred_result = json.loads((tmp_path / "data/assets/fx-usd-krw.json").read_text(encoding="utf-8"))
    automation = json.loads((tmp_path / "data/automation-status.json").read_text(encoding="utf-8"))
    assert count == 1
    assert yahoo.calls == 0
    assert fdr.calls == 0
    assert fred.calls == 1
    assert stooq.calls == 0
    assert yahoo_result["state"] == "degraded"
    assert yahoo_result["prices"] == [100.0, 102.0]
    assert fred_result["state"] == "published"
    assert fred_result["source"]["provider"] == "fred"
    assert automation["reasonCodes"] == ["yahoo_public_display_rights_unconfirmed"]


def test_explicit_yahoo_approval_allows_core_refresh_and_records_rights(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {
        "id": "stock-aapl",
        "symbol": "AAPL",
        "name": "Apple Inc.",
        "nameKo": "애플",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "currency": "USD",
        "timezone": "America/New_York",
        "status": "unavailable",
        "provider": {
            "provider": "yahoo_finance",
            "symbol": "AAPL",
            "exchange": "NASDAQ",
        },
        "searchTerms": ["AAPL"],
        "dataPath": "assets/stock-aapl.json",
        "returnBasis": "total_return_approximation",
        "availableFrom": None,
        "availableTo": None,
    }
    existing = unavailable_asset("stock-aapl", "AAPL")
    existing["metadata"] = {
        "symbol": "AAPL",
        "assetType": "equity",
        "exchange": "NASDAQ",
        "timezone": "America/New_York",
        "returnBasis": "total_return_approximation",
        "baseCurrency": "USD",
    }
    write_json(tmp_path / "data/catalog.json", {"assets": [entry], "state": "unavailable"})
    write_json(tmp_path / "config/catalog.json", {"assets": [{"id": "stock-aapl"}]})
    write_json(tmp_path / "data/assets/stock-aapl.json", existing)
    write_json(
        tmp_path / "data/summary.json",
        {
            "state": "unavailable",
            "status": {},
            "coverage": {"availableAssetCount": 0},
            "primaryEntities": [],
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
    yahoo = FakeYahooProvider()
    fdr = NeverCalledProvider()
    monkeypatch.setenv("YAHOO_PUBLIC_DISPLAY_APPROVED", "true")

    count = refresh(
        tmp_path,
        tmp_path / "config/catalog.json",
        backfill=True,
        start=date(2026, 7, 17),
        end=date(2026, 7, 20),
        asset_ids={"stock-aapl"},
        krx_provider=FakeKrxProvider(),
        twelve_provider=DisabledTwelveProvider(),
        yahoo_provider=yahoo,  # type: ignore[arg-type]
        fdr_provider=fdr,  # type: ignore[arg-type]
        stooq_provider=NeverCalledProvider(),  # type: ignore[arg-type]
        fred_provider=NeverCalledProvider(),  # type: ignore[arg-type]
        finviz_provider=FakeFinvizProvider(),  # type: ignore[arg-type]
    )

    document = json.loads((tmp_path / "data/assets/stock-aapl.json").read_text(encoding="utf-8"))
    automation = json.loads((tmp_path / "data/automation-status.json").read_text(encoding="utf-8"))
    providers = {item["name"]: item for item in automation["provider"]["providers"]}
    assert count == 1
    assert yahoo.calls == 1
    assert fdr.calls == 0
    assert document["state"] == "published"
    assert document["source"]["provider"] == "yahoo_finance"
    assert providers["yahoo_finance"]["rightsApproved"] is True
    assert providers["finance_data_reader"]["rightsApproved"] is True


def test_targeted_fred_refresh_does_not_require_or_call_yahoo(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    entry = {
        "id": "fx-usd-krw",
        "symbol": "USD/KRW",
        "name": "US Dollar / Korean Won",
        "nameKo": "미국 달러/원 환율",
        "assetType": "fx",
        "exchange": "FX",
        "currency": "KRW",
        "timezone": "UTC",
        "status": "unavailable",
        "provider": {"provider": "fred", "symbol": "DEXKOUS", "exchange": "FX"},
        "searchTerms": ["USD/KRW"],
        "dataPath": "assets/fx-usd-krw.json",
        "returnBasis": "fx_rate",
        "availableFrom": None,
        "availableTo": None,
    }
    existing = unavailable_asset("fx-usd-krw", "USD/KRW")
    existing["metadata"] = {
        "symbol": "USD/KRW",
        "assetType": "fx",
        "exchange": "FX",
        "timezone": "UTC",
        "returnBasis": "fx_rate",
        "baseCurrency": "USD",
        "quoteCurrency": "KRW",
    }
    write_json(tmp_path / "data/catalog.json", {"assets": [entry], "state": "unavailable"})
    write_json(tmp_path / "config/catalog.json", {"assets": [{"id": "fx-usd-krw"}]})
    write_json(tmp_path / "data/assets/fx-usd-krw.json", existing)
    write_json(
        tmp_path / "data/summary.json",
        {
            "state": "unavailable",
            "status": {},
            "coverage": {"availableAssetCount": 0},
            "primaryEntities": [],
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
    fred = FakeFredProvider()
    yahoo = NeverCalledProvider()
    fdr = NeverCalledProvider()
    stooq = NeverCalledProvider()
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    count = refresh(
        tmp_path,
        tmp_path / "config/catalog.json",
        backfill=True,
        start=date(2026, 7, 17),
        end=date(2026, 7, 20),
        asset_ids={"fx-usd-krw"},
        krx_provider=FakeKrxProvider(),
        twelve_provider=DisabledTwelveProvider(),
        yahoo_provider=yahoo,  # type: ignore[arg-type]
        fdr_provider=fdr,  # type: ignore[arg-type]
        stooq_provider=stooq,  # type: ignore[arg-type]
        fred_provider=fred,  # type: ignore[arg-type]
        finviz_provider=NeverCalledProvider(),  # type: ignore[arg-type]
    )

    document = json.loads((tmp_path / "data/assets/fx-usd-krw.json").read_text(encoding="utf-8"))
    assert count == 1
    assert document["state"] == "published"
    assert document["source"]["provider"] == "fred"
    assert fred.calls == 1
    assert yahoo.calls == 0
    assert fdr.calls == 0
    assert stooq.calls == 0


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
    legacy = published_asset()
    legacy["quality"] = {
        "observationCount": 2,
        "eligibleForKelly": False,
        "minimumKellyObservations": 60,
        "crossCheck": {
            "provider": "finviz",
            "state": "passed",
            "commonObservations": 21,
            "medianAbsReturnDifference": 0.001,
            "p99AbsReturnDifference": 0.01,
        },
    }
    recent = _preserved_failure_document(
        legacy,
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
    cross_check = recent["quality"]["crossCheck"]  # type: ignore[index]
    assert cross_check["state"] == "unavailable"  # type: ignore[index]
    assert cross_check["windowStart"] is None  # type: ignore[index]
    assert cross_check["windowEnd"] is None  # type: ignore[index]
    assert "independent_crosscheck_unavailable" in recent["limitations"]  # type: ignore[operator]


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
