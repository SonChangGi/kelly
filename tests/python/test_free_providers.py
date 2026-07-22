from __future__ import annotations

import json
from datetime import UTC, date, datetime

import pytest
import requests

from kelly_lab.free_providers import (
    FinanceDataReaderYahooProvider,
    FinvizChartProvider,
    FredDexkousProvider,
    StooqCsvProvider,
    YahooChartProvider,
)
from kelly_lab.providers import ProviderResponseError, ProviderUnavailable


class FakeResponse:
    def __init__(
        self,
        payload: object | None = None,
        *,
        text: str = "",
        status_code: int = 200,
        content_type: str = "application/json",
    ) -> None:
        self._payload = payload
        self.text = text
        self.status_code = status_code
        self.headers = {"content-type": content_type}

    def json(self) -> object:
        return self._payload


class FakeSession:
    def __init__(self, responses: list[object]) -> None:
        self.responses = responses
        self.requests: list[tuple[str, dict[str, object]]] = []

    def get(self, url: str, **kwargs: object) -> object:
        self.requests.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def epoch(year: int, month: int, day: int, hour: int, minute: int = 0) -> int:
    return int(datetime(year, month, day, hour, minute, tzinfo=UTC).timestamp())


def yahoo_payload(
    *,
    symbol: str = "AAPL",
    currency: str = "USD",
    instrument_type: str = "EQUITY",
    timezone_name: str = "America/New_York",
    timestamps: list[int] | None = None,
    closes: list[float | None] | None = None,
    adjusted: list[float | None] | None = None,
) -> dict[str, object]:
    timestamps = timestamps or [epoch(2026, 1, 2, 14, 30), epoch(2026, 1, 5, 14, 30)]
    closes = closes or [100.0, 105.0]
    adjusted = adjusted or [95.0, 101.0]
    return {
        "chart": {
            "error": None,
            "result": [
                {
                    "meta": {
                        "symbol": symbol,
                        "currency": currency,
                        "instrumentType": instrument_type,
                        "exchangeTimezoneName": timezone_name,
                        "exchangeName": "NMS",
                        "fullExchangeName": "NasdaqGS",
                    },
                    "timestamp": timestamps,
                    "indicators": {
                        "quote": [{"close": closes}],
                        "adjclose": [{"adjclose": adjusted}],
                    },
                }
            ],
        }
    }


def test_yahoo_equity_uses_adjusted_close_drops_nulls_and_localizes_dates() -> None:
    # 00:30 UTC on Jan 2 is still Jan 1 in New York.  The middle null is removed.
    session = FakeSession(
        [
            FakeResponse(
                yahoo_payload(
                    timestamps=[
                        epoch(2026, 1, 2, 0, 30),
                        epoch(2026, 1, 2, 14, 30),
                        epoch(2026, 1, 3, 14, 30),
                    ],
                    closes=[100.0, 101.0, 102.0],
                    adjusted=[90.0, None, 92.0],
                )
            )
        ]
    )
    provider = YahooChartProvider(session=session, max_retries=0)

    result = provider.history(
        "AAPL",
        date(2026, 1, 1),
        date(2026, 1, 3),
        adjust="all",
        exchange="NASDAQ",
        currency="USD",
        asset_type="equity",
    )

    assert result.dates == ("2026-01-01", "2026-01-03")
    assert result.prices == (90.0, 92.0)
    assert result.return_basis == "total_return_approximation"
    assert result.timezone == "America/New_York"
    url, request = session.requests[0]
    assert url.endswith("/AAPL")
    assert request["params"]["includeAdjustedClose"] == "true"  # type: ignore[index]
    assert "key" not in repr(request).lower()


def test_yahoo_index_uses_close_as_price_return() -> None:
    provider = YahooChartProvider(
        session=FakeSession(
            [
                FakeResponse(
                    yahoo_payload(
                        symbol="^GSPC",
                        instrument_type="INDEX",
                        closes=[6000.0, 6100.0],
                        adjusted=[1.0, 2.0],
                    )
                )
            ]
        ),
        max_retries=0,
    )
    result = provider.history(
        "^GSPC",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        exchange="INDEX",
        currency="USD",
        asset_type="index",
    )
    assert result.prices == (6000.0, 6100.0)
    assert result.return_basis == "price_return"


def test_yahoo_maps_usdkrw_and_uses_close_as_fx_rate() -> None:
    session = FakeSession(
        [
            FakeResponse(
                yahoo_payload(
                    symbol="KRW=X",
                    currency="KRW",
                    instrument_type="CURRENCY",
                    timezone_name="Europe/London",
                    closes=[1450.0, 1460.0],
                )
            )
        ]
    )
    result = YahooChartProvider(session=session, max_retries=0).history(
        "USD/KRW",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        exchange="FX",
        currency="KRW",
        asset_type="fx",
    )
    assert session.requests[0][0].endswith("/KRW%3DX")
    assert result.prices == (1450.0, 1460.0)
    assert result.return_basis == "fx_rate"


@pytest.mark.parametrize(
    ("overrides", "error_code"),
    [
        ({"symbol": "MSFT"}, "YAHOO_IDENTITY_SYMBOL_MISMATCH"),
        ({"currency": "EUR"}, "YAHOO_IDENTITY_CURRENCY_MISMATCH"),
        ({"instrument_type": "ETF"}, "YAHOO_IDENTITY_TYPE_MISMATCH"),
        ({"timezone_name": "Not/A_Timezone"}, "YAHOO_IDENTITY_TIMEZONE_INVALID"),
    ],
)
def test_yahoo_rejects_wrong_instrument_metadata(
    overrides: dict[str, str], error_code: str
) -> None:
    provider = YahooChartProvider(
        session=FakeSession([FakeResponse(yahoo_payload(**overrides))]),
        max_retries=0,
    )
    with pytest.raises(ProviderResponseError, match=error_code):
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="all",
            currency="USD",
            asset_type="equity",
        )


def test_yahoo_retries_429_then_succeeds_without_sleeping_in_test() -> None:
    session = FakeSession([FakeResponse({}, status_code=429), FakeResponse(yahoo_payload())])
    sleeps: list[float] = []
    provider = YahooChartProvider(
        session=session,
        max_retries=1,
        backoff_seconds=0.5,
        sleeper=sleeps.append,
    )
    result = provider.history(
        "AAPL",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="all",
        currency="USD",
        asset_type="equity",
    )
    assert result.prices == (95.0, 101.0)
    assert len(session.requests) == 2
    assert sleeps == [0.5]


def test_yahoo_exhausted_429_and_network_errors_have_stable_codes() -> None:
    limited = YahooChartProvider(
        session=FakeSession([FakeResponse({}, status_code=429)]), max_retries=0
    )
    with pytest.raises(ProviderResponseError, match="^YAHOO_RATE_LIMITED$"):
        limited.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 2),
            adjust="all",
            currency="USD",
            asset_type="equity",
        )

    offline = YahooChartProvider(
        session=FakeSession([requests.ConnectionError("secret details")]), max_retries=0
    )
    with pytest.raises(ProviderUnavailable, match="^YAHOO_REQUEST_FAILED$"):
        offline.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 2),
            adjust="all",
            currency="USD",
            asset_type="equity",
        )


class FakeSeries:
    def __init__(self, rows: list[tuple[object, object]]) -> None:
        self._rows = rows

    def items(self) -> list[tuple[object, object]]:
        return self._rows


class FakeFrame:
    def __init__(self, rows: list[tuple[object, object]], *, adjusted: bool = True) -> None:
        self.empty = not rows
        self.columns = ["Adj Close"] if adjusted else ["Close"]
        self._series = FakeSeries(rows)

    def __getitem__(self, key: str) -> FakeSeries:
        if key not in self.columns:
            raise KeyError(key)
        return self._series


class FakeFinanceDataReader:
    def __init__(self, frame: FakeFrame) -> None:
        self.frame = frame
        self.calls: list[tuple[str, str, str]] = []

    def DataReader(self, symbol: str, start: str, end: str) -> FakeFrame:  # noqa: N802
        self.calls.append((symbol, start, end))
        return self.frame


def test_finance_data_reader_forces_yahoo_and_uses_adjusted_close() -> None:
    module = FakeFinanceDataReader(
        FakeFrame(
            [
                (datetime(2026, 1, 5), 101.0),
                (datetime(2026, 1, 2), 95.0),
                (datetime(2026, 1, 3), None),
            ]
        )
    )
    result = FinanceDataReaderYahooProvider(reader_module=module).history(
        "AAPL",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="all",
        exchange="NASDAQ",
        currency="USD",
        asset_type="equity",
    )
    assert module.calls == [("YAHOO:AAPL", "2026-01-01", "2026-01-06")]
    assert result.dates == ("2026-01-02", "2026-01-05")
    assert result.prices == (95.0, 101.0)
    assert result.return_basis == "total_return_approximation"
    assert "Yahoo upstream" in result.provider
    assert "Yahoo Finance" in result.attribution


def test_finance_data_reader_rejects_missing_adjusted_close() -> None:
    module = FakeFinanceDataReader(FakeFrame([(datetime(2026, 1, 2), 100.0)], adjusted=False))
    provider = FinanceDataReaderYahooProvider(reader_module=module)
    with pytest.raises(ProviderResponseError, match="ADJ_CLOSE_MISSING"):
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="all",
            asset_type="equity",
        )


def test_stooq_symbol_mappings_and_close_normalization() -> None:
    assert StooqCsvProvider.map_symbol("AAPL", "equity") == "aapl.us"
    assert StooqCsvProvider.map_symbol("SPY", "etf") == "spy.us"
    assert StooqCsvProvider.map_symbol("^GSPC", "index") == "^spx"
    assert StooqCsvProvider.map_symbol("USD/KRW", "fx") == "usdkrw"
    session = FakeSession(
        [
            FakeResponse(
                text="Date,Open,High,Low,Close,Volume\n"
                "2026-01-05,99,102,98,101,10\n"
                "2026-01-02,98,101,97,100,11\n",
                content_type="text/csv",
            )
        ]
    )
    result = StooqCsvProvider(session=session).history(
        "^GSPC",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        exchange="INDEX",
        currency="USD",
        asset_type="index",
    )
    assert session.requests[0][1]["params"]["s"] == "^spx"  # type: ignore[index]
    assert result.dates == ("2026-01-02", "2026-01-05")
    assert result.prices == (100.0, 101.0)
    assert result.return_basis == "price_return"


def test_stooq_rejects_total_return_request_before_http() -> None:
    session = FakeSession([])
    provider = StooqCsvProvider(session=session)
    with pytest.raises(ProviderUnavailable, match="^STOOQ_TOTAL_RETURN_UNSUPPORTED$"):
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="all",
            asset_type="equity",
        )
    assert session.requests == []


def test_stooq_detects_html_challenge_and_no_data() -> None:
    challenged = StooqCsvProvider(
        session=FakeSession(
            [
                FakeResponse(
                    text="<!doctype html><title>Just a moment</title>",
                    content_type="text/html",
                )
            ]
        )
    )
    with pytest.raises(ProviderResponseError, match="^STOOQ_HTML_CHALLENGE$"):
        challenged.history(
            "^GSPC",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="index",
        )

    empty = StooqCsvProvider(
        session=FakeSession([FakeResponse(text="No data", content_type="text/plain")])
    )
    with pytest.raises(ProviderResponseError, match="^STOOQ_EMPTY_SERIES$"):
        empty.history(
            "^GSPC",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="index",
        )


def test_stooq_usdkrw_is_fx_rate() -> None:
    session = FakeSession(
        [
            FakeResponse(
                text="Date,Close\n2026-01-02,1450.5\n",
                content_type="text/csv",
            )
        ]
    )
    result = StooqCsvProvider(session=session).history(
        "USD/KRW",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        asset_type="fx",
        currency="KRW",
    )
    assert result.prices == (1450.5,)
    assert result.return_basis == "fx_rate"


def test_finviz_chart_is_an_ephemeral_price_return_crosscheck() -> None:
    payload = {
        "ticker": "AAPL",
        "date": [epoch(2026, 1, 2, 21), epoch(2026, 1, 5, 21)],
        "close": [100.0, 102.0],
    }
    session = FakeSession(
        [
            FakeResponse(
                text=(
                    "<html><head><title>AAPL - Apple Inc Stock Price and Quote</title></head>"
                    f"<script>var data = {json.dumps(payload)};</script></html>"
                ),
                content_type="text/html",
            )
        ]
    )
    result = FinvizChartProvider(session=session).history(
        "AAPL",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        exchange="NASDAQ",
        currency="USD",
        asset_type="equity",
    )
    assert result.dates == ("2026-01-02", "2026-01-05")
    assert result.prices == (100.0, 102.0)
    assert result.return_basis == "price_return"
    assert result.provider == "Finviz"


def test_finviz_rejects_total_return_and_symbol_mismatch() -> None:
    provider = FinvizChartProvider(session=FakeSession([]))
    with pytest.raises(ProviderUnavailable, match="FINVIZ_TOTAL_RETURN_UNSUPPORTED"):
        provider.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="all",
            asset_type="equity",
        )
    mismatched = FinvizChartProvider(
        session=FakeSession(
            [
                FakeResponse(
                    text="<title>MSFT - Microsoft</title><script>var data = {};</script>",
                    content_type="text/html",
                )
            ]
        )
    )
    with pytest.raises(ProviderResponseError, match="FINVIZ_IDENTITY_SYMBOL_MISMATCH"):
        mismatched.history(
            "AAPL",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="equity",
        )


def test_fred_dexkous_is_krw_per_usd_and_is_not_inverted() -> None:
    session = FakeSession(
        [
            FakeResponse(
                text=(
                    "observation_date,DEXKOUS\n"
                    "2026-01-02,1450.25\n"
                    "2026-01-03,.\n"
                    "2026-01-05,1460.50\n"
                ),
                content_type="text/csv",
            )
        ]
    )
    provider = FredDexkousProvider(session=session)
    result = provider.history(
        "USD/KRW",
        date(2026, 1, 1),
        date(2026, 1, 6),
        adjust="none",
        exchange="FX",
        currency="KRW",
        asset_type="fx",
    )
    assert provider.units == "South Korean Won to One U.S. Dollar"
    assert provider.inverted is False
    assert result.prices == (1450.25, 1460.5)
    assert result.return_basis == "fx_rate"
    assert session.requests[0][1]["params"] == {
        "id": "DEXKOUS",
        "cosd": "2026-01-01",
        "coed": "2026-01-06",
    }


def test_fred_rejects_wrong_pair_type_and_currency_without_http() -> None:
    session = FakeSession([])
    provider = FredDexkousProvider(session=session)
    with pytest.raises(ProviderUnavailable, match="SYMBOL_UNSUPPORTED"):
        provider.history(
            "EUR/USD",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="fx",
        )
    with pytest.raises(ProviderResponseError, match="TYPE_MISMATCH"):
        provider.history(
            "USD/KRW",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="index",
        )
    with pytest.raises(ProviderResponseError, match="CURRENCY_MISMATCH"):
        provider.history(
            "USD/KRW",
            date(2026, 1, 1),
            date(2026, 1, 6),
            adjust="none",
            asset_type="fx",
            currency="USD",
        )
    assert session.requests == []
