from __future__ import annotations

import json
import shutil
from datetime import date, timedelta
from pathlib import Path

import pytest

from kelly_lab.dynamic_assets import DynamicAssetError
from kelly_lab.dynamic_universe import (
    PROVIDER_CIRCUIT_THRESHOLD,
    CircuitBreakingHistoryProvider,
    FinanceDataReaderUniverseProvider,
    NasdaqScreenerUniverseProvider,
    UniverseCandidate,
    UniverseSelection,
    collect_us_batch,
    load_symbol_file,
    preflight_public_manifest,
    select_universe,
    upsert_public_asset_manifest,
)
from kelly_lab.free_providers import YahooInstrumentMetadata
from kelly_lab.providers import (
    NormalizedPriceSeries,
    ProviderResponseError,
    ProviderUnavailable,
)

ROOT = Path(__file__).resolve().parents[2]


@pytest.fixture(autouse=True)
def approve_public_dynamic_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("YAHOO_PUBLIC_DISPLAY_APPROVED", "true")


def contract_root(tmp_path: Path, *, core_symbols: tuple[str, ...] = ()) -> Path:
    (tmp_path / "schemas").mkdir()
    for filename in ("asset.schema.json", "dynamic-catalog.schema.json"):
        shutil.copy2(ROOT / "schemas" / filename, tmp_path / "schemas" / filename)
    (tmp_path / "data").mkdir()
    assets = [
        {
            "symbol": symbol,
            "assetType": "equity",
            "currency": "USD",
        }
        for symbol in core_symbols
    ]
    (tmp_path / "data/catalog.json").write_text(
        json.dumps({"assets": assets}),
        encoding="utf-8",
    )
    return tmp_path


def metadata(symbol: str, *, name: str | None = None) -> YahooInstrumentMetadata:
    return YahooInstrumentMetadata(
        requested_symbol=symbol,
        provider_symbol=symbol,
        instrument_type="EQUITY",
        currency="USD",
        exchange_code="NMS",
        exchange_name="NasdaqGS",
        timezone="America/New_York",
        short_name=name or f"{symbol} Corporation",
        long_name=name,
        first_trade_date="2000-01-03",
    )


def price_series(
    symbol: str,
    start: date,
    *,
    return_basis: str,
    provider: str,
    count: int = 31,
) -> NormalizedPriceSeries:
    return NormalizedPriceSeries(
        symbol=symbol,
        dates=tuple((start + timedelta(days=index)).isoformat() for index in range(count)),
        prices=tuple(100.0 + index for index in range(count)),
        currency="USD",
        exchange="NasdaqGS",
        timezone="America/New_York",
        return_basis=return_basis,
        provider=provider,
        source_url="https://example.test/source",
        attribution="Fixture provider",
    )


class FakeYahoo:
    def __init__(
        self,
        *,
        unavailable: set[str] | None = None,
        missing: set[str] | None = None,
    ) -> None:
        self.unavailable = unavailable or set()
        self.missing = missing or set()
        self.lookup_calls: list[str] = []

    def lookup(self, symbol: str) -> YahooInstrumentMetadata:
        self.lookup_calls.append(symbol)
        if symbol in self.unavailable:
            raise ProviderUnavailable("YAHOO_RATE_LIMITED")
        if symbol in self.missing:
            raise ProviderResponseError("YAHOO_SYMBOL_NOT_FOUND")
        return metadata(symbol)

    def history(self, symbol: str, start: date, _end: date, **_kwargs: object):
        return price_series(
            symbol,
            start,
            return_basis="total_return_approximation",
            provider="Yahoo Finance",
        )


class MatchingStooq:
    def history(self, symbol: str, start: date, _end: date, **_kwargs: object):
        return price_series(
            symbol,
            start,
            return_basis="price_return",
            provider="Stooq",
        )


class UnusedProvider:
    def history(self, *_args: object, **_kwargs: object):
        raise AssertionError("provider should not be called")


def candidate(symbol: str, cap: float = 1.0) -> UniverseCandidate:
    return UniverseCandidate(symbol, f"{symbol} Inc.", cap, "fixture")


def test_nasdaq_screener_parses_deduplicates_and_sorts_market_cap() -> None:
    class Response:
        status_code = 200

        def json(self) -> object:
            return {
                "data": {
                    "rows": [
                        {"symbol": "SMALL", "name": "Small Corp", "marketCap": "$2.5B"},
                        {"symbol": "BRK/B", "name": "Berkshire", "marketCap": "900B"},
                        {"symbol": "SMALL", "name": "Small Corp", "marketCap": "$2.0B"},
                        {"symbol": "BAD/../../", "name": "Bad", "marketCap": "$1T"},
                        {"symbol": "NOCAP", "name": "No Cap", "marketCap": "N/A"},
                    ]
                }
            }

    class Session:
        def get(self, _url: str, **kwargs: object) -> Response:
            assert kwargs["params"]["limit"] == 5000  # type: ignore[index]
            return Response()

    result = NasdaqScreenerUniverseProvider(session=Session()).candidates()  # type: ignore[arg-type]
    assert [item.symbol for item in result] == ["BRK-B", "SMALL"]
    assert result[0].market_cap == 900_000_000_000.0


def test_auto_universe_falls_back_to_fdr_without_claiming_market_cap_rank() -> None:
    class FailedNasdaq:
        def candidates(self):
            raise ProviderUnavailable("FAILED")

    class Fdr:
        def candidates(self):
            return [candidate("COST"), candidate("KO")]

    result = select_universe(
        "auto",
        2,
        nasdaq_provider=FailedNasdaq(),
        fdr_provider=Fdr(),
    )
    assert result.source == "finance_data_reader_listing"
    assert result.fallback_reason == "nasdaq_screener_unavailable"
    assert [item.symbol for item in result.candidates] == ["COST", "KO"]


def test_fdr_listing_round_robins_exchange_order() -> None:
    class Frame:
        def __init__(self, records: list[dict[str, str]]) -> None:
            self.records = records

        def to_dict(self, orientation: str):
            assert orientation == "records"
            return self.records

    class Reader:
        @staticmethod
        def StockListing(market: str):
            return Frame(
                {
                    "NASDAQ": [
                        {"Symbol": "NVDA", "Name": "Nvidia"},
                        {"Symbol": "MSFT", "Name": "Microsoft"},
                    ],
                    "NYSE": [{"Symbol": "BRK/B", "Name": "Berkshire"}],
                    "AMEX": [{"Symbol": "SPY", "Name": "SPDR"}],
                }[market]
            )

    result = FinanceDataReaderUniverseProvider(reader_module=Reader()).candidates()
    assert [item.symbol for item in result] == ["NVDA", "BRK-B", "SPY", "MSFT"]
    assert all(item.market_cap is None for item in result)


def test_symbol_file_is_bounded_validated_and_failure_isolated(tmp_path: Path) -> None:
    path = tmp_path / "symbols.txt"
    path.write_text("COST, brk.b\n../BAD\nCOST # duplicate\nKO\n", encoding="utf-8")
    result = load_symbol_file(path, 4)
    assert [item.symbol for item in result.candidates] == ["COST", "BRK-B", "KO"]
    assert result.failures == ({"symbol": "entry-3", "reason": "invalid_symbol"},)
    assert result.requested_count == 3


def test_optional_provider_circuit_opens_only_for_system_failures() -> None:
    class Provider:
        def __init__(self, errors: list[Exception]) -> None:
            self.errors = errors
            self.calls = 0

        def history(self, *_args: object, **_kwargs: object):
            error = self.errors[min(self.calls, len(self.errors) - 1)]
            self.calls += 1
            raise error

    per_symbol = Provider([ProviderResponseError("STOOQ_EMPTY_SERIES")])
    wrapper = CircuitBreakingHistoryProvider(per_symbol, "STOOQ_BATCH_CIRCUIT_OPEN")
    with pytest.raises(ProviderResponseError):
        wrapper.history()
    with pytest.raises(ProviderResponseError):
        wrapper.history()
    assert per_symbol.calls == 2
    assert wrapper.disabled is False

    systemic = Provider([ProviderResponseError("FINVIZ_RATE_LIMITED")])
    wrapper = CircuitBreakingHistoryProvider(systemic, "FINVIZ_BATCH_CIRCUIT_OPEN")
    with pytest.raises(ProviderResponseError):
        wrapper.history()
    with pytest.raises(ProviderUnavailable, match="CIRCUIT_OPEN"):
        wrapper.history()
    assert systemic.calls == 1
    assert wrapper.disabled is True


def test_batch_isolates_failures_skips_core_and_writes_atomic_manifest(tmp_path: Path) -> None:
    root = contract_root(tmp_path, core_symbols=("AAPL",))
    marker = root / "data/assets/core.json"
    marker.parent.mkdir()
    marker.write_text("locked\n", encoding="utf-8")
    selection = UniverseSelection(
        candidates=(candidate("AAPL", 4), candidate("BAD", 3), candidate("COST", 2)),
        source="nasdaq_screener",
        requested_count=1,
    )
    yahoo = FakeYahoo(missing={"BAD"})

    path, manifest = collect_us_batch(
        root,
        selection,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        yahoo_provider=yahoo,
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )

    assert path == root / "data/dynamic-catalog.json"
    assert manifest["assetCount"] == manifest["freshCount"] == 1
    assert manifest["preservedCount"] == 0
    assert manifest["excludedCoreCount"] == 1
    assert manifest["assets"][0]["symbol"] == "COST"
    assert {item["reason"] for item in manifest["failures"]} == {"metadata_unavailable"}
    assert marker.read_text(encoding="utf-8") == "locked\n"
    assert json.loads(path.read_text(encoding="utf-8")) == manifest


def test_batch_skips_high_confidence_non_common_security_descriptions(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    selection = UniverseSelection(
        candidates=(
            UniverseCandidate(
                "PREF",
                "Example Depositary Shares representing Mandatory Convertible Preferred Stock",
                3.0,
                "nasdaq_screener",
            ),
            candidate("COST", 2.0),
        ),
        source="nasdaq_screener",
        requested_count=1,
    )
    yahoo = FakeYahoo()

    _path, manifest = collect_us_batch(
        root,
        selection,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        yahoo_provider=yahoo,
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )

    assert yahoo.lookup_calls == ["COST"]
    assert manifest["assets"][0]["symbol"] == "COST"
    assert {item["reason"] for item in manifest["failures"]} == {"excluded_non_common_security"}


def test_public_batch_requires_approval_before_any_provider_call(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = contract_root(tmp_path)
    yahoo = FakeYahoo()
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_batch(
            root,
            UniverseSelection((candidate("COST"),), "symbol_file", 1),
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            today=date(2026, 1, 31),
            cache_scope="public",
            yahoo_provider=yahoo,
            fdr_history_provider=UnusedProvider(),
            stooq_provider=MatchingStooq(),
            finviz_provider=UnusedProvider(),
        )

    assert captured.value.reason == "public_display_approval_required"
    assert yahoo.lookup_calls == []
    assert not (root / "data/dynamic-catalog.json").exists()


def test_batch_stops_after_consecutive_provider_failure_circuit(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    candidates = tuple(candidate(f"T{index}") for index in range(20))
    yahoo = FakeYahoo(unavailable={item.symbol for item in candidates})
    selection = UniverseSelection(candidates, "nasdaq_screener", 2)
    with pytest.raises(DynamicAssetError) as captured:
        collect_us_batch(
            root,
            selection,
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            today=date(2026, 1, 31),
            yahoo_provider=yahoo,
            fdr_history_provider=UnusedProvider(),
            stooq_provider=MatchingStooq(),
            finviz_provider=UnusedProvider(),
        )
    assert captured.value.reason == "batch_no_assets"
    assert len(yahoo.lookup_calls) == PROVIDER_CIRCUIT_THRESHOLD


def test_batch_attempt_cap_bounds_symbol_specific_misses(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    misses = tuple(candidate(f"T{index}") for index in range(80))
    selection = UniverseSelection((candidate("COST"), *misses), "nasdaq_screener", 2)
    yahoo = FakeYahoo(missing={item.symbol for item in misses})

    _path, manifest = collect_us_batch(
        root,
        selection,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        yahoo_provider=yahoo,
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )

    assert manifest["attemptedCount"] == 52
    assert len(yahoo.lookup_calls) == 52
    assert {item["reason"] for item in manifest["failures"]} >= {
        "attempt_limit_reached",
        "candidate_pool_exhausted",
    }


def test_zero_success_preserves_existing_manifest_bytes(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    manifest_path = root / "data/dynamic-catalog.json"
    selection = UniverseSelection((candidate("COST"),), "nasdaq_screener", 1)
    common = dict(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )
    collect_us_batch(root, selection, yahoo_provider=FakeYahoo(), **common)
    last_good = manifest_path.read_bytes()

    with pytest.raises(DynamicAssetError, match="manifest was preserved"):
        collect_us_batch(
            root,
            selection,
            yahoo_provider=FakeYahoo(unavailable={"COST"}),
            **common,
        )
    assert manifest_path.read_bytes() == last_good


def test_batch_drift_requires_explicit_backfill_and_preserves_last_good(
    tmp_path: Path,
) -> None:
    root = contract_root(tmp_path)
    selection = UniverseSelection((candidate("COST"),), "symbol_file", 1)
    start = date(2026, 1, 1)
    first_end = date(2026, 1, 31)
    common = dict(
        start=start,
        today=first_end,
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )
    manifest_path, _manifest = collect_us_batch(
        root,
        selection,
        end=first_end,
        yahoo_provider=FakeYahoo(),
        **common,
    )
    asset_path = root / "data/dynamic-assets/dynamic-us-cost.json"
    manifest_bytes = manifest_path.read_bytes()
    asset_bytes = asset_path.read_bytes()

    class DriftingYahoo(FakeYahoo):
        def history(self, symbol: str, requested_start: date, _end: date, **_kwargs: object):
            result = price_series(
                symbol,
                requested_start,
                return_basis="total_return_approximation",
                provider="Yahoo Finance",
                count=41,
            )
            prices = list(result.prices)
            prices[10] *= 1.001
            return NormalizedPriceSeries(**{**result.__dict__, "prices": tuple(prices)})

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_batch(
            root,
            selection,
            start=start,
            end=date(2026, 2, 10),
            today=date(2026, 2, 10),
            yahoo_provider=DriftingYahoo(),
            fdr_history_provider=UnusedProvider(),
            stooq_provider=MatchingStooq(),
            finviz_provider=UnusedProvider(),
        )

    assert captured.value.reason == "batch_no_assets"
    assert manifest_path.read_bytes() == manifest_bytes
    assert asset_path.read_bytes() == asset_bytes


def test_invalid_existing_manifest_blocks_replacement_and_pruning(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    manifest_path = root / "data/dynamic-catalog.json"
    marker = b'{"lastGood":"needs-review"}\n'
    manifest_path.write_bytes(marker)
    yahoo = FakeYahoo()

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_batch(
            root,
            UniverseSelection((candidate("COST"),), "symbol_file", 1),
            start=date(2026, 1, 1),
            end=date(2026, 1, 31),
            today=date(2026, 1, 31),
            yahoo_provider=yahoo,
            fdr_history_provider=UnusedProvider(),
            stooq_provider=MatchingStooq(),
            finviz_provider=UnusedProvider(),
        )

    assert captured.value.reason == "existing_manifest_invalid"
    assert manifest_path.read_bytes() == marker
    assert yahoo.lookup_calls == []

    with pytest.raises(DynamicAssetError) as preflight:
        preflight_public_manifest(root, "COST")
    assert preflight.value.reason == "existing_manifest_invalid"
    assert manifest_path.read_bytes() == marker


def test_partial_refresh_preserves_valid_last_good_entry(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    selection = UniverseSelection(
        (candidate("COST", 2), candidate("KO", 1)),
        "nasdaq_screener",
        2,
    )
    common = dict(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )
    _path, first = collect_us_batch(root, selection, yahoo_provider=FakeYahoo(), **common)
    ko_path = root / "data/dynamic-assets/dynamic-us-ko.json"
    ko_bytes = ko_path.read_bytes()

    _path, second = collect_us_batch(
        root,
        selection,
        yahoo_provider=FakeYahoo(unavailable={"KO"}),
        **common,
    )
    assert first["assetCount"] == 2
    assert second["assetCount"] == 2
    assert second["freshCount"] == 1
    assert second["preservedCount"] == 1
    assert {item["symbol"] for item in second["assets"]} == {"COST", "KO"}
    assert {item["reason"] for item in second["failures"]} >= {
        "metadata_unavailable",
        "preserved_last_good",
    }
    assert ko_path.read_bytes() == ko_bytes


def test_public_single_fetch_can_upsert_discovery_manifest(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    selection = UniverseSelection((candidate("COST"),), "symbol_file", 1)
    _manifest_path, batch = collect_us_batch(
        root,
        selection,
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        yahoo_provider=FakeYahoo(),
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )
    asset_path = root / "data/dynamic-assets/dynamic-us-cost.json"
    document = json.loads(asset_path.read_text(encoding="utf-8"))
    _path, upserted = upsert_public_asset_manifest(root, asset_path, document)
    assert batch["freshCount"] == 1
    assert upserted["assetCount"] == 1
    assert upserted["freshCount"] == 1
    assert upserted["preservedCount"] == 0
    assert upserted["universeSource"] == "mixed"


def test_successful_batch_prunes_only_owned_unreferenced_regular_files(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    selection = UniverseSelection((candidate("COST"),), "symbol_file", 1)
    common = dict(
        start=date(2026, 1, 1),
        end=date(2026, 1, 31),
        today=date(2026, 1, 31),
        yahoo_provider=FakeYahoo(),
        fdr_history_provider=UnusedProvider(),
        stooq_provider=MatchingStooq(),
        finviz_provider=UnusedProvider(),
    )
    collect_us_batch(root, selection, **common)
    directory = root / "data/dynamic-assets"
    orphan = directory / "dynamic-us-orphan.json"
    unrelated = directory / "operator-notes.json"
    target = tmp_path / "outside.json"
    symlink = directory / "dynamic-us-symlink.json"
    orphan.write_text("{}\n", encoding="utf-8")
    unrelated.write_text("{}\n", encoding="utf-8")
    target.write_text("outside\n", encoding="utf-8")
    symlink.symlink_to(target)

    _path, manifest = collect_us_batch(root, selection, **common)

    assert manifest["prunedCount"] == 1
    assert not orphan.exists()
    assert unrelated.exists()
    assert symlink.is_symlink()
    assert target.read_text(encoding="utf-8") == "outside\n"
