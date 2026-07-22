from __future__ import annotations

import json
import shutil
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from kelly_lab.dynamic_assets import (
    MAX_HISTORY_DAYS,
    DynamicAssetError,
    bounded_history_period,
    collect_us_asset,
    dynamic_cache_path,
    normalize_us_symbol,
    validate_us_metadata,
)
from kelly_lab.free_providers import YahooChartProvider, YahooInstrumentMetadata
from kelly_lab.providers import (
    NormalizedPriceSeries,
    ProviderResponseError,
    ProviderUnavailable,
)

ROOT = Path(__file__).resolve().parents[2]


def metadata(**overrides: object) -> YahooInstrumentMetadata:
    values = {
        "requested_symbol": "COST",
        "provider_symbol": "COST",
        "instrument_type": "EQUITY",
        "currency": "USD",
        "exchange_code": "NMS",
        "exchange_name": "NasdaqGS",
        "timezone": "America/New_York",
        "short_name": "Costco Wholesale Corporation",
        "long_name": "Costco Wholesale Corporation",
        "first_trade_date": "1986-07-09",
        **overrides,
    }
    return YahooInstrumentMetadata(**values)  # type: ignore[arg-type]


def price_series(
    symbol: str,
    *,
    start: date,
    count: int = 31,
    return_basis: str = "total_return_approximation",
    provider: str = "Yahoo Finance",
) -> NormalizedPriceSeries:
    dates = tuple((start + timedelta(days=index)).isoformat() for index in range(count))
    prices = tuple(100.0 + index for index in range(count))
    return NormalizedPriceSeries(
        symbol=symbol,
        dates=dates,
        prices=prices,
        currency="USD",
        exchange="NasdaqGS",
        timezone="America/New_York",
        return_basis=return_basis,
        provider=provider,
        source_url="https://example.test/source",
        attribution="Fixture provider",
    )


def window_price_series(
    symbol: str,
    *,
    start: date,
    end: date,
    origin: date,
    scale: float = 1.0,
    drift_day: date | None = None,
    return_basis: str = "total_return_approximation",
    provider: str = "Yahoo Finance",
) -> NormalizedPriceSeries:
    days = (end - start).days + 1
    dates = tuple((start + timedelta(days=index)).isoformat() for index in range(days))
    prices = []
    for index in range(days):
        day = start + timedelta(days=index)
        value = (100.0 + (day - origin).days) * scale
        if drift_day == day:
            value *= 1.001
        prices.append(value)
    return NormalizedPriceSeries(
        symbol=symbol,
        dates=dates,
        prices=tuple(prices),
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
        identity: YahooInstrumentMetadata | None = None,
        history_result: NormalizedPriceSeries | Exception | None = None,
    ) -> None:
        self.identity = identity or metadata()
        self.history_result = history_result
        self.lookup_calls: list[str] = []
        self.history_calls: list[dict[str, object]] = []

    def lookup(self, symbol: str) -> YahooInstrumentMetadata:
        self.lookup_calls.append(symbol)
        return self.identity

    def history(self, symbol: str, start: date, end: date, **kwargs: object):
        self.history_calls.append({"symbol": symbol, "start": start, "end": end, **kwargs})
        if isinstance(self.history_result, Exception):
            raise self.history_result
        if self.history_result is None:
            return price_series(symbol, start=start)
        return self.history_result


class FakeHistoryProvider:
    def __init__(self, result: NormalizedPriceSeries | Exception) -> None:
        self.result = result
        self.calls: list[dict[str, object]] = []

    def history(self, symbol: str, start: date, end: date, **kwargs: object):
        self.calls.append({"symbol": symbol, "start": start, "end": end, **kwargs})
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def contract_root(tmp_path: Path) -> Path:
    (tmp_path / "schemas").mkdir()
    shutil.copy2(ROOT / "schemas/asset.schema.json", tmp_path / "schemas/asset.schema.json")
    return tmp_path


@pytest.mark.parametrize(
    "value",
    ["../AAPL", "AAPL?period=1", "^GSPC", "KRW=X", "AAPL/USD", "AAPL MSFT", "AAPL;X"],
)
def test_symbol_injection_and_non_us_syntax_are_rejected(value: str) -> None:
    with pytest.raises(DynamicAssetError, match="ticker"):
        normalize_us_symbol(value)


def test_class_share_symbol_is_canonicalized_without_path_syntax() -> None:
    assert normalize_us_symbol(" brk.b ") == "BRK-B"
    assert normalize_us_symbol("BF-B") == "BF-B"


def test_five_year_history_bound_and_future_end_are_enforced() -> None:
    today = date(2026, 7, 22)
    start, end = bounded_history_period(None, None, today=today)
    assert end == today
    assert (end - start).days == MAX_HISTORY_DAYS

    with pytest.raises(DynamicAssetError, match="five calendar years"):
        bounded_history_period(date(2020, 1, 1), today, today=today)
    with pytest.raises(DynamicAssetError, match="future"):
        bounded_history_period(None, date(2026, 7, 23), today=today)


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"provider_symbol": "MSFT"}, "identity_symbol_mismatch"),
        ({"instrument_type": "MUTUALFUND"}, "unsupported_asset_type"),
        ({"currency": "EUR"}, "identity_currency_mismatch"),
        ({"timezone": "Europe/London"}, "identity_market_mismatch"),
        (
            {"exchange_code": "LSE", "exchange_name": "London Stock Exchange"},
            "identity_exchange_mismatch",
        ),
    ],
)
def test_metadata_identity_type_currency_and_market_guards(
    overrides: dict[str, object], reason: str
) -> None:
    with pytest.raises(DynamicAssetError) as captured:
        validate_us_metadata("COST", metadata(**overrides))
    assert captured.value.reason == reason


@pytest.mark.parametrize(
    ("symbol", "name"),
    [
        ("TQQQ", "ProShares UltraPro QQQ"),
        ("SPXL", "Direxion Daily S&P 500 Bull 3X Shares"),
    ],
)
def test_dynamic_three_x_products_remain_excluded(symbol: str, name: str) -> None:
    identity = metadata(
        requested_symbol=symbol,
        provider_symbol=symbol,
        instrument_type="ETF",
        short_name=name,
        long_name=name,
    )
    with pytest.raises(DynamicAssetError) as captured:
        validate_us_metadata(symbol, identity)
    assert captured.value.reason == "excluded_3x_product"


@pytest.mark.parametrize(
    "name",
    [
        "Example Depositary Shares representing Mandatory Convertible Preferred Stock",
        "Example Corp. Warrants",
        "Example Acquisition Corp. Units, each consisting of one common share and one right",
        "Example Holdings 5.25% Notes Due 2031",
        "Comcast Holdings ZONES",
    ],
)
def test_dynamic_non_common_securities_are_excluded(name: str) -> None:
    identity = metadata(short_name=name, long_name=name)
    with pytest.raises(DynamicAssetError) as captured:
        validate_us_metadata("COST", identity)
    assert captured.value.reason == "excluded_non_common_security"


def test_preferred_income_etf_is_not_misclassified_as_non_common_equity() -> None:
    identity = metadata(
        instrument_type="ETF",
        short_name="Example Preferred and Income Securities ETF",
        long_name="Example Preferred and Income Securities ETF",
    )
    assert validate_us_metadata("COST", identity) == ("COST", "etf")


@pytest.mark.parametrize(
    "name",
    [
        "Example Semiconductor Company American Depositary Shares",
        "Unit Corporation Common Stock",
    ],
)
def test_common_adr_and_company_names_are_not_overfiltered(name: str) -> None:
    identity = metadata(short_name=name, long_name=name)
    assert validate_us_metadata("COST", identity) == ("COST", "equity")


def test_yahoo_lookup_returns_observed_metadata_without_inventing_name() -> None:
    timestamp = int(datetime(1986, 7, 9, tzinfo=UTC).timestamp())

    class Response:
        status_code = 200

        def json(self) -> object:
            return {
                "chart": {
                    "error": None,
                    "result": [
                        {
                            "meta": {
                                "symbol": "COST",
                                "instrumentType": "EQUITY",
                                "currency": "USD",
                                "exchangeName": "NMS",
                                "fullExchangeName": "NasdaqGS",
                                "exchangeTimezoneName": "America/New_York",
                                "shortName": "Costco Wholesale Corporation",
                                "firstTradeDate": timestamp,
                            }
                        }
                    ],
                }
            }

    class Session:
        def get(self, _url: str, **_kwargs: object) -> Response:
            return Response()

    result = YahooChartProvider(session=Session(), max_retries=0).lookup("COST")  # type: ignore[arg-type]
    assert result.provider_symbol == "COST"
    assert result.instrument_type == "EQUITY"
    assert result.exchange_name == "NasdaqGS"
    assert result.short_name == "Costco Wholesale Corporation"
    assert result.long_name is None
    assert result.first_trade_date == "1986-07-08"


def test_adjusted_history_uses_fdr_only_as_same_yahoo_upstream_fallback(
    tmp_path: Path,
) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=ProviderUnavailable("YAHOO_REQUEST_FAILED"))
    fdr = FakeHistoryProvider(
        price_series(
            "COST",
            start=start,
            provider="FinanceDataReader (Yahoo upstream)",
        )
    )
    stooq = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Stooq")
    )

    path, document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
    )

    assert path == root / "var/dynamic-assets/dynamic-us-cost.json"
    assert document["metadata"]["catalogScope"] == "dynamic"
    assert document["metadata"]["returnBasis"] == "total_return_approximation"
    assert document["source"]["provider"] == "yahoo_finance"
    assert document["source"]["adapter"] == "finance_data_reader"
    assert document["quality"]["crossCheck"]["provider"] == "stooq"
    assert document["quality"]["crossCheck"]["state"] == "passed"
    assert json.loads(path.read_text(encoding="utf-8")) == document


def test_incremental_refresh_preserves_frozen_returns_and_blocks_drift(
    tmp_path: Path,
) -> None:
    root = contract_root(tmp_path)
    origin = date(2026, 1, 1)
    first_end = date(2026, 4, 10)
    first_yahoo = FakeYahoo(
        history_result=window_price_series(
            "COST",
            start=origin,
            end=first_end,
            origin=origin,
        )
    )
    first_stooq = FakeHistoryProvider(
        window_price_series(
            "COST",
            start=origin,
            end=first_end,
            origin=origin,
            return_basis="price_return",
            provider="Stooq",
        )
    )
    path, first = collect_us_asset(
        root,
        "COST",
        start=origin,
        end=first_end,
        today=first_end,
        yahoo_provider=first_yahoo,
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=first_stooq,
        finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
    )

    second_end = date(2026, 4, 20)
    overlap_start = first_end - timedelta(days=35)
    second_yahoo = FakeYahoo(
        history_result=window_price_series(
            "COST",
            start=overlap_start,
            end=second_end,
            origin=origin,
            scale=2.0,
        )
    )
    second_stooq = FakeHistoryProvider(
        window_price_series(
            "COST",
            start=overlap_start,
            end=second_end,
            origin=origin,
            return_basis="price_return",
            provider="Stooq",
        )
    )
    _path, second = collect_us_asset(
        root,
        "COST",
        end=second_end,
        today=second_end,
        yahoo_provider=second_yahoo,
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=second_stooq,
        finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
    )

    assert second_yahoo.history_calls[0]["start"] == overlap_start
    assert second["dates"][: len(first["dates"])] == first["dates"]
    assert second["prices"][: len(first["prices"])] == first["prices"]
    assert second["dataAsOf"] == second_end.isoformat()
    assert second["quality"]["observationCount"] == len(second["dates"])

    frozen_bytes = path.read_bytes()
    third_end = date(2026, 4, 30)
    third_start = second_end - timedelta(days=35)
    drift_day = third_start + timedelta(days=10)
    drifting_yahoo = FakeYahoo(
        history_result=window_price_series(
            "COST",
            start=third_start,
            end=third_end,
            origin=origin,
            scale=2.0,
            drift_day=drift_day,
        )
    )
    with pytest.raises(DynamicAssetError) as captured:
        collect_us_asset(
            root,
            "COST",
            end=third_end,
            today=third_end,
            yahoo_provider=drifting_yahoo,
            fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
            stooq_provider=FakeHistoryProvider(
                window_price_series(
                    "COST",
                    start=third_start,
                    end=third_end,
                    origin=origin,
                    return_basis="price_return",
                    provider="Stooq",
                )
            ),
            finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        )
    assert captured.value.reason == "historical_drift_backfill_required"
    assert path.read_bytes() == frozen_bytes

    full_backfill = window_price_series(
        "COST",
        start=origin,
        end=third_end,
        origin=origin,
        drift_day=drift_day,
    )
    _path, replaced = collect_us_asset(
        root,
        "COST",
        start=origin,
        end=third_end,
        today=third_end,
        backfill=True,
        yahoo_provider=FakeYahoo(history_result=full_backfill),
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=FakeHistoryProvider(
            window_price_series(
                "COST",
                start=origin,
                end=third_end,
                origin=origin,
                return_basis="price_return",
                provider="Stooq",
            )
        ),
        finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
    )
    drift_index = replaced["dates"].index(drift_day.isoformat())
    assert replaced["prices"][drift_index] == pytest.approx(full_backfill.prices[drift_index])
    assert path.read_bytes() != frozen_bytes


def test_stooq_cannot_replace_adjusted_history(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    failure = ProviderUnavailable("FAILED")
    yahoo = FakeYahoo(history_result=failure)
    fdr = FakeHistoryProvider(failure)
    stooq = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Stooq")
    )

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_asset(
            root,
            "COST",
            start=start,
            end=end,
            today=end,
            yahoo_provider=yahoo,
            fdr_provider=fdr,
            stooq_provider=stooq,
        )
    assert captured.value.reason == "provider_chain_exhausted"
    assert not (root / "var/dynamic-assets").exists()


def test_explicit_price_basis_permits_stooq_primary_fallback(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=ProviderUnavailable("FAILED"))
    fdr = FakeHistoryProvider(ProviderUnavailable("SHOULD_NOT_BE_USED"))
    stooq = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Stooq")
    )

    _path, document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        basis_mode="price",
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
    )

    assert document["metadata"]["returnBasis"] == "price_return"
    assert document["source"]["provider"] == "stooq"
    assert document["quality"]["crossCheck"]["state"] == "not_applicable"
    assert fdr.calls == []


def test_history_return_basis_and_currency_cannot_be_forged(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    wrong_basis = price_series("COST", start=start, return_basis="price_return")
    yahoo = FakeYahoo(history_result=wrong_basis)
    fdr = FakeHistoryProvider(ProviderUnavailable("FAILED"))
    stooq = FakeHistoryProvider(wrong_basis)

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_asset(
            root,
            "COST",
            start=start,
            end=end,
            today=end,
            yahoo_provider=yahoo,
            fdr_provider=fdr,
            stooq_provider=stooq,
        )
    assert captured.value.reason == "return_basis_mismatch"


def test_public_dynamic_cache_is_separate_from_locked_core_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("YAHOO_PUBLIC_DISPLAY_APPROVED", "true")
    root = contract_root(tmp_path)
    (root / "data").mkdir()
    core_catalog = root / "data/catalog.json"
    core_asset = root / "data/assets/etf-spy.json"
    core_asset.parent.mkdir()
    core_catalog.write_text('{"locked":true}\n', encoding="utf-8")
    core_asset.write_text('{"locked":true}\n', encoding="utf-8")
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))
    fdr = FakeHistoryProvider(ProviderUnavailable("UNUSED"))
    stooq = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Stooq")
    )

    path, _document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        cache_scope="public",
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
    )

    assert path == root / "data/dynamic-assets/dynamic-us-cost.json"
    assert core_catalog.read_text(encoding="utf-8") == '{"locked":true}\n'
    assert core_asset.read_text(encoding="utf-8") == '{"locked":true}\n'


def test_public_collection_requires_explicit_display_approval_before_provider_use(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_asset(
            root,
            "COST",
            start=start,
            end=end,
            today=end,
            cache_scope="public",
            yahoo_provider=yahoo,
            fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
            stooq_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
            finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        )

    assert captured.value.reason == "public_display_approval_required"
    assert yahoo.lookup_calls == []
    assert not (root / "data/dynamic-assets").exists()


def test_local_collection_remains_available_without_public_display_approval(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))
    matching = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Stooq")
    )
    monkeypatch.delenv("YAHOO_PUBLIC_DISPLAY_APPROVED", raising=False)

    path, _document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        cache_scope="local",
        yahoo_provider=yahoo,
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=matching,
        finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
    )

    assert path == root / "var/dynamic-assets/dynamic-us-cost.json"
    assert yahoo.lookup_calls == ["COST"]


def test_cache_path_rejects_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    (tmp_path / "var").mkdir()
    (tmp_path / "var/dynamic-assets").symlink_to(outside, target_is_directory=True)
    with pytest.raises(DynamicAssetError) as captured:
        dynamic_cache_path(tmp_path, "COST", "local")
    assert captured.value.reason == "unsafe_cache_path"


def test_cache_path_rejects_same_directory_target_symlink(tmp_path: Path) -> None:
    directory = tmp_path / "var/dynamic-assets"
    directory.mkdir(parents=True)
    target = directory / "dynamic-us-other.json"
    target.write_text("{}\n", encoding="utf-8")
    (directory / "dynamic-us-cost.json").symlink_to(target)

    with pytest.raises(DynamicAssetError) as captured:
        dynamic_cache_path(tmp_path, "COST", "local")
    assert captured.value.reason == "unsafe_cache_path"
    assert target.read_text(encoding="utf-8") == "{}\n"


def test_finviz_recent_window_is_used_when_stooq_is_unavailable(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))
    stooq = FakeHistoryProvider(ProviderUnavailable("STOOQ_HTML_CHALLENGE"))
    finviz = FakeHistoryProvider(
        price_series("COST", start=start, return_basis="price_return", provider="Finviz")
    )

    _path, document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        yahoo_provider=yahoo,
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=stooq,
        finviz_provider=finviz,
    )

    assert document["quality"]["crossCheck"]["provider"] == "finviz"
    assert document["quality"]["crossCheck"]["state"] == "passed"
    assert "raw" not in json.dumps(document["quality"]["crossCheck"]).lower()


def test_crosscheck_preserves_both_provider_failures(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))

    _path, document = collect_us_asset(
        root,
        "COST",
        start=start,
        end=end,
        today=end,
        yahoo_provider=yahoo,
        fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        stooq_provider=FakeHistoryProvider(ProviderUnavailable("STOOQ_HTML_CHALLENGE")),
        finviz_provider=FakeHistoryProvider(ProviderResponseError("FINVIZ_CHART_DATA_MISSING")),
    )

    crosscheck = document["quality"]["crossCheck"]
    assert crosscheck["provider"] == "finviz"
    assert crosscheck["state"] == "unavailable"
    assert crosscheck["attempts"] == [
        {
            "provider": "stooq",
            "state": "unavailable",
            "reasonCode": "stooq_html_challenge",
        },
        {
            "provider": "finviz",
            "state": "unavailable",
            "reasonCode": "finviz_chart_data_missing",
        },
    ]
    assert document["state"] == "degraded"


def test_independent_crosscheck_mismatch_rejects_before_write(tmp_path: Path) -> None:
    root = contract_root(tmp_path)
    start = date(2026, 1, 1)
    end = date(2026, 1, 31)
    yahoo = FakeYahoo(history_result=price_series("COST", start=start))
    mismatch = NormalizedPriceSeries(
        symbol="COST",
        dates=tuple((start + timedelta(days=index)).isoformat() for index in range(31)),
        prices=tuple(100.0 if index % 2 == 0 else 180.0 for index in range(31)),
        currency="USD",
        exchange="NasdaqGS",
        timezone="America/New_York",
        return_basis="price_return",
        provider="Stooq",
        source_url="https://example.test/stooq",
        attribution="Fixture Stooq",
    )

    with pytest.raises(DynamicAssetError) as captured:
        collect_us_asset(
            root,
            "COST",
            start=start,
            end=end,
            today=end,
            yahoo_provider=yahoo,
            fdr_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
            stooq_provider=FakeHistoryProvider(mismatch),
            finviz_provider=FakeHistoryProvider(ProviderUnavailable("UNUSED")),
        )

    assert captured.value.reason == "crosscheck_mismatch"
    assert not (root / "var/dynamic-assets/dynamic-us-cost.json").exists()
