from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from kelly_lab.providers import NormalizedPriceSeries
from kelly_lab.refresh import (
    _preserved_failure_document,
    _reason_code,
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
