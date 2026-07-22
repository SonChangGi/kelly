"""Optional, key-free market-data adapters.

The adapters in this module normalize observations only.  Publication rights,
refresh policy, drift detection, and fallback ordering belong to the refresh
orchestrator.  In particular, a successful HTTP response is not treated as
proof that it belongs to the requested instrument.
"""

from __future__ import annotations

import csv
import importlib
import io
import json
import math
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from datetime import time as datetime_time
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests

from .providers import NormalizedPriceSeries, ProviderResponseError, ProviderUnavailable


def _identity_token(value: object) -> str:
    return "".join(character for character in str(value or "").upper() if character.isalnum())


def _valid_price(value: object) -> float | None:
    try:
        price = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return price if math.isfinite(price) and price > 0 else None


def _response_text(response: object) -> str:
    value = getattr(response, "text", "")
    return value if isinstance(value, str) else str(value)


def _looks_like_html(response: object) -> bool:
    headers = getattr(response, "headers", {}) or {}
    content_type = str(headers.get("content-type", "")).lower()
    prefix = _response_text(response).lstrip()[:512].lower()
    return "text/html" in content_type or prefix.startswith(("<!doctype html", "<html"))


@dataclass(frozen=True)
class YahooInstrumentMetadata:
    """Identity fields returned by Yahoo's chart metadata response.

    This object intentionally contains only upstream observations.  Callers
    must validate the exchange, currency, and instrument type for their own
    universe before treating the result as an asset identity.
    """

    requested_symbol: str
    provider_symbol: str
    instrument_type: str
    currency: str
    exchange_code: str
    exchange_name: str
    timezone: str
    short_name: str | None
    long_name: str | None
    first_trade_date: str | None


class YahooChartProvider:
    """Yahoo Finance v8 chart adapter using no API key.

    Equities and ETFs use ``adjclose`` when ``adjust='all'``.  Indices and FX
    always use the unadjusted close because those series are price levels/rates,
    not dividend-bearing total-return series.
    """

    endpoint = "https://query2.finance.yahoo.com/v8/finance/chart"
    source_url = "https://finance.yahoo.com/"
    _asset_types = {
        "equity": {"EQUITY"},
        "etf": {"ETF"},
        "index": {"INDEX"},
        "fx": {"CURRENCY"},
    }

    def __init__(
        self,
        *,
        session: requests.Session | None = None,
        timeout: float = 20,
        max_retries: int = 4,
        backoff_seconds: float = 1.0,
        sleeper: Callable[[float], None] = time.sleep,
    ) -> None:
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        self._session = session or requests.Session()
        self._timeout = timeout
        self._max_retries = max_retries
        self._backoff_seconds = max(0.0, backoff_seconds)
        self._sleeper = sleeper

    @staticmethod
    def map_symbol(symbol: str) -> str:
        normalized = symbol.strip().upper()
        if normalized == "USD/KRW":
            return "KRW=X"
        return normalized

    def _request(self, query_symbol: str, params: dict[str, object]) -> object:
        url = f"{self.endpoint}/{quote(query_symbol, safe='')}"
        response: object | None = None
        for attempt in range(self._max_retries + 1):
            try:
                response = self._session.get(
                    url,
                    params=params,
                    headers={
                        "Accept": "application/json",
                        "User-Agent": "Mozilla/5.0 Kelly-Allocation-Lab/1.0",
                    },
                    timeout=self._timeout,
                )
            except requests.RequestException:
                if attempt < self._max_retries:
                    self._sleeper(self._backoff_seconds * (2**attempt))
                    continue
                raise ProviderUnavailable("YAHOO_REQUEST_FAILED") from None

            status = int(getattr(response, "status_code", 0))
            if status == 429:
                if attempt < self._max_retries:
                    self._sleeper(self._backoff_seconds * (2**attempt))
                    continue
                raise ProviderResponseError("YAHOO_RATE_LIMITED")
            if status >= 500:
                if attempt < self._max_retries:
                    self._sleeper(self._backoff_seconds * (2**attempt))
                    continue
                raise ProviderUnavailable("YAHOO_UPSTREAM_UNAVAILABLE")
            break

        if response is None:
            raise ProviderUnavailable("YAHOO_REQUEST_FAILED")
        status = int(getattr(response, "status_code", 0))
        if status in {401, 403}:
            raise ProviderUnavailable("YAHOO_ACCESS_UNAVAILABLE")
        if status == 404:
            raise ProviderResponseError("YAHOO_SYMBOL_NOT_FOUND")
        if status != 200:
            raise ProviderResponseError("YAHOO_HTTP_ERROR")
        return response

    def lookup(self, symbol: str) -> YahooInstrumentMetadata:
        """Return provider-observed identity metadata for one symbol.

        The history collector uses this before selecting an on-demand US
        instrument.  It is deliberately not a search endpoint: callers must
        supply one already-sanitized symbol and then validate the returned
        identity against their allowed universe.
        """

        query_symbol = self.map_symbol(symbol)
        response = self._request(
            query_symbol,
            {
                "range": "5d",
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            },
        )
        try:
            payload = response.json()  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID")
        chart = payload["chart"]
        if chart.get("error"):
            raise ProviderResponseError("YAHOO_PROVIDER_ERROR")
        results = chart.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ProviderResponseError("YAHOO_IDENTITY_METADATA_MISSING")
        meta, exchange_timezone = self._validate_meta(
            results[0].get("meta"),
            query_symbol=query_symbol,
            currency=None,
            asset_type=None,
        )

        first_trade_date: str | None = None
        raw_first_trade = meta.get("firstTradeDate")
        if raw_first_trade is not None:
            try:
                first_trade_date = (
                    datetime.fromtimestamp(float(raw_first_trade), UTC)
                    .astimezone(exchange_timezone)
                    .date()
                    .isoformat()
                )
            except (OSError, OverflowError, TypeError, ValueError):
                raise ProviderResponseError("YAHOO_IDENTITY_FIRST_TRADE_DATE_INVALID") from None

        def optional_text(value: object) -> str | None:
            text = str(value).strip() if value is not None else ""
            return text or None

        return YahooInstrumentMetadata(
            requested_symbol=symbol,
            provider_symbol=str(meta.get("symbol") or "").upper(),
            instrument_type=str(meta.get("instrumentType") or "").upper(),
            currency=str(meta.get("currency") or "").upper(),
            exchange_code=str(meta.get("exchangeName") or "").upper(),
            exchange_name=str(
                meta.get("fullExchangeName") or meta.get("exchangeName") or ""
            ).strip(),
            timezone=str(meta.get("exchangeTimezoneName") or ""),
            short_name=optional_text(meta.get("shortName")),
            long_name=optional_text(meta.get("longName")),
            first_trade_date=first_trade_date,
        )

    @classmethod
    def _validate_meta(
        cls,
        meta: object,
        *,
        query_symbol: str,
        currency: str | None,
        asset_type: str | None,
    ) -> tuple[dict[str, Any], ZoneInfo]:
        if not isinstance(meta, dict):
            raise ProviderResponseError("YAHOO_IDENTITY_METADATA_MISSING")
        if _identity_token(meta.get("symbol")) != _identity_token(query_symbol):
            raise ProviderResponseError("YAHOO_IDENTITY_SYMBOL_MISMATCH")
        if currency and _identity_token(meta.get("currency")) != _identity_token(currency):
            raise ProviderResponseError("YAHOO_IDENTITY_CURRENCY_MISMATCH")

        instrument_type = str(meta.get("instrumentType") or "").upper()
        if asset_type:
            accepted = cls._asset_types.get(asset_type.lower())
            if accepted is None:
                raise ValueError(f"unsupported asset_type: {asset_type}")
            if instrument_type not in accepted:
                raise ProviderResponseError("YAHOO_IDENTITY_TYPE_MISMATCH")
        elif instrument_type not in set().union(*cls._asset_types.values()):
            raise ProviderResponseError("YAHOO_IDENTITY_TYPE_UNSUPPORTED")

        timezone_name = meta.get("exchangeTimezoneName")
        if not isinstance(timezone_name, str) or not timezone_name:
            raise ProviderResponseError("YAHOO_IDENTITY_TIMEZONE_MISSING")
        try:
            exchange_timezone = ZoneInfo(timezone_name)
        except (ZoneInfoNotFoundError, ValueError):
            raise ProviderResponseError("YAHOO_IDENTITY_TIMEZONE_INVALID") from None
        return meta, exchange_timezone

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries:
        if start > end:
            raise ValueError("start must be on or before end")
        if adjust not in {"all", "none"}:
            raise ValueError("Yahoo supports adjust='all' or adjust='none'")

        query_symbol = self.map_symbol(symbol)
        period1 = int(datetime.combine(start, datetime_time.min, UTC).timestamp())
        period2 = int(datetime.combine(end + timedelta(days=1), datetime_time.min, UTC).timestamp())
        response = self._request(
            query_symbol,
            {
                "period1": period1,
                "period2": period2,
                "interval": "1d",
                "events": "div,splits",
                "includeAdjustedClose": "true",
            },
        )
        try:
            payload = response.json()  # type: ignore[attr-defined]
        except (AttributeError, ValueError):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("chart"), dict):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID")
        chart = payload["chart"]
        if chart.get("error"):
            error = chart.get("error")
            code = str(error.get("code", "")) if isinstance(error, dict) else ""
            if code.lower() in {"not found", "not_found"}:
                raise ProviderResponseError("YAHOO_SYMBOL_NOT_FOUND")
            raise ProviderResponseError("YAHOO_PROVIDER_ERROR")
        results = chart.get("result")
        if not isinstance(results, list) or not results or not isinstance(results[0], dict):
            raise ProviderResponseError("YAHOO_EMPTY_SERIES")
        result = results[0]
        meta, exchange_timezone = self._validate_meta(
            result.get("meta"),
            query_symbol=query_symbol,
            currency=currency,
            asset_type=asset_type,
        )

        actual_type = str(meta.get("instrumentType") or "").upper()
        is_fx = (asset_type or "").lower() == "fx" or actual_type == "CURRENCY"
        is_total_return_asset = (asset_type or "").lower() in {"equity", "etf"} or actual_type in {
            "EQUITY",
            "ETF",
        }
        indicators = result.get("indicators")
        if not isinstance(indicators, dict):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID")
        if is_total_return_asset and adjust == "all":
            blocks = indicators.get("adjclose")
            value_key = "adjclose"
            return_basis = "total_return_approximation"
        else:
            blocks = indicators.get("quote")
            value_key = "close"
            return_basis = "fx_rate" if is_fx else "price_return"
        if not isinstance(blocks, list) or not blocks or not isinstance(blocks[0], dict):
            missing = (
                "YAHOO_ADJ_CLOSE_MISSING" if value_key == "adjclose" else "YAHOO_CLOSE_MISSING"
            )
            raise ProviderResponseError(missing)
        timestamps = result.get("timestamp")
        values = blocks[0].get(value_key)
        if not isinstance(timestamps, list) or not isinstance(values, list):
            raise ProviderResponseError("YAHOO_PAYLOAD_INVALID")
        if len(timestamps) != len(values):
            raise ProviderResponseError("YAHOO_SERIES_LENGTH_MISMATCH")

        observations: dict[str, float] = {}
        for timestamp, raw_price in zip(timestamps, values, strict=True):
            price = _valid_price(raw_price)
            if price is None:
                continue
            try:
                local_day = (
                    datetime.fromtimestamp(float(timestamp), UTC)
                    .astimezone(exchange_timezone)
                    .date()
                )
            except (OSError, OverflowError, TypeError, ValueError):
                raise ProviderResponseError("YAHOO_TIMESTAMP_INVALID") from None
            if start <= local_day <= end:
                observations[local_day.isoformat()] = price
        if not observations:
            raise ProviderResponseError("YAHOO_EMPTY_SERIES")

        rows = sorted(observations.items())
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=tuple(day for day, _ in rows),
            prices=tuple(price for _, price in rows),
            currency=currency or str(meta.get("currency")),
            exchange=exchange
            or str(meta.get("fullExchangeName") or meta.get("exchangeName") or "UNKNOWN"),
            timezone=str(meta["exchangeTimezoneName"]),
            return_basis=return_basis,
            provider="Yahoo Finance",
            source_url=self.source_url,
            attribution="Data source: Yahoo Finance chart API",
        )


class FinanceDataReaderYahooProvider:
    """Optional FinanceDataReader wrapper forced onto its Yahoo upstream."""

    def __init__(self, *, reader_module: object | None = None) -> None:
        self._reader_module = reader_module

    def _reader(self) -> object:
        if self._reader_module is not None:
            return self._reader_module
        try:
            return importlib.import_module("FinanceDataReader")
        except ImportError:
            raise ProviderUnavailable("FINANCE_DATA_READER_NOT_INSTALLED") from None

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries:
        if start > end:
            raise ValueError("start must be on or before end")
        if adjust != "all" or (asset_type and asset_type.lower() not in {"equity", "etf"}):
            raise ProviderUnavailable("FINANCE_DATA_READER_TOTAL_RETURN_ONLY")
        reader = self._reader()
        query_symbol = YahooChartProvider.map_symbol(symbol)
        try:
            frame = reader.DataReader(  # type: ignore[attr-defined]
                f"YAHOO:{query_symbol}", start.isoformat(), end.isoformat()
            )
        except Exception:
            raise ProviderUnavailable("FINANCE_DATA_READER_REQUEST_FAILED") from None
        if frame is None or getattr(frame, "empty", True):
            raise ProviderResponseError("FINANCE_DATA_READER_EMPTY_SERIES")
        if "Adj Close" not in getattr(frame, "columns", ()):
            raise ProviderResponseError("FINANCE_DATA_READER_ADJ_CLOSE_MISSING")

        observations: dict[str, float] = {}
        try:
            for index, value in frame["Adj Close"].items():
                price = _valid_price(value)
                if price is None:
                    continue
                day = (
                    index.date() if hasattr(index, "date") else date.fromisoformat(str(index)[:10])
                )
                if start <= day <= end:
                    observations[day.isoformat()] = price
        except (AttributeError, TypeError, ValueError):
            raise ProviderResponseError("FINANCE_DATA_READER_PAYLOAD_INVALID") from None
        if not observations:
            raise ProviderResponseError("FINANCE_DATA_READER_EMPTY_SERIES")
        rows = sorted(observations.items())
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=tuple(day for day, _ in rows),
            prices=tuple(price for _, price in rows),
            currency=currency or "USD",
            exchange=exchange or "US",
            timezone="America/New_York",
            return_basis="total_return_approximation",
            provider="FinanceDataReader (Yahoo upstream)",
            source_url="https://finance.yahoo.com/",
            attribution="Retrieved through FinanceDataReader; upstream price source: Yahoo Finance",
        )


class StooqCsvProvider:
    """Stooq daily CSV adapter for price-return and FX series only."""

    endpoint = "https://stooq.com/q/d/l/"
    _index_aliases = {"^GSPC": "^spx", "^NDX": "^ndx", "^SOX": "^sox"}

    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    @classmethod
    def map_symbol(cls, symbol: str, asset_type: str | None = None) -> str:
        normalized = symbol.strip().upper()
        if normalized in {"USD/KRW", "USDKRW", "KRW=X"}:
            return "usdkrw"
        if normalized.startswith("^"):
            return cls._index_aliases.get(normalized, normalized.lower())
        if (asset_type or "").lower() in {"equity", "etf"}:
            return normalized.lower() if normalized.endswith(".US") else f"{normalized.lower()}.us"
        return normalized.lower()

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries:
        if start > end:
            raise ValueError("start must be on or before end")
        if adjust == "all":
            raise ProviderUnavailable("STOOQ_TOTAL_RETURN_UNSUPPORTED")
        if adjust not in {"none", "splits", "dividends"}:
            raise ValueError("unsupported adjustment mode")
        query_symbol = self.map_symbol(symbol, asset_type)
        try:
            response = self._session.get(
                self.endpoint,
                params={
                    "s": query_symbol,
                    "d1": start.strftime("%Y%m%d"),
                    "d2": end.strftime("%Y%m%d"),
                    "i": "d",
                },
                headers={"Accept": "text/csv", "User-Agent": "Kelly-Allocation-Lab/1.0"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderUnavailable("STOOQ_REQUEST_FAILED") from None
        status = int(getattr(response, "status_code", 0))
        if status == 429:
            raise ProviderResponseError("STOOQ_RATE_LIMITED")
        if status in {401, 403}:
            raise ProviderUnavailable("STOOQ_ACCESS_UNAVAILABLE")
        if status != 200:
            raise ProviderResponseError("STOOQ_HTTP_ERROR")
        if _looks_like_html(response):
            raise ProviderResponseError("STOOQ_HTML_CHALLENGE")
        text = _response_text(response)
        lowered = text.lstrip().lower()
        if any(marker in lowered[:1024] for marker in ("captcha", "cloudflare", "just a moment")):
            raise ProviderResponseError("STOOQ_HTML_CHALLENGE")

        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames or not {"Date", "Close"}.issubset(reader.fieldnames):
            if "no data" in lowered:
                raise ProviderResponseError("STOOQ_EMPTY_SERIES")
            raise ProviderResponseError("STOOQ_CSV_COLUMNS_INVALID")
        observations: dict[str, float] = {}
        try:
            for row in reader:
                day = date.fromisoformat(str(row.get("Date", ""))[:10])
                price = _valid_price(row.get("Close"))
                if price is not None and start <= day <= end:
                    observations[day.isoformat()] = price
        except (TypeError, ValueError):
            raise ProviderResponseError("STOOQ_CSV_PAYLOAD_INVALID") from None
        if not observations:
            raise ProviderResponseError("STOOQ_EMPTY_SERIES")
        rows = sorted(observations.items())
        is_fx = (asset_type or "").lower() == "fx" or query_symbol == "usdkrw"
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=tuple(day for day, _ in rows),
            prices=tuple(price for _, price in rows),
            currency=currency or ("KRW" if is_fx else "USD"),
            exchange=exchange or ("FX" if is_fx else "STOOQ"),
            timezone="UTC" if is_fx else "America/New_York",
            return_basis="fx_rate" if is_fx else "price_return",
            provider="Stooq",
            source_url="https://stooq.com/",
            attribution="Price data: Stooq (unadjusted close)",
        )


class FinvizChartProvider:
    """Finviz public chart-page adapter used only for ephemeral cross-checks.

    Finviz is not a publication source for this project. The adapter reads the
    daily close array embedded in a requested quote page, normalizes it in
    memory, and lets the refresh pipeline compare returns before discarding it.
    """

    endpoint = "https://finviz.com/quote.ashx"
    _data_pattern = re.compile(r"\bvar\s+data\s*=\s*(\{.*?\});", re.DOTALL)

    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries:
        if start > end:
            raise ValueError("start must be on or before end")
        if adjust != "none":
            raise ProviderUnavailable("FINVIZ_TOTAL_RETURN_UNSUPPORTED")
        if (asset_type or "").lower() not in {"equity", "etf"}:
            raise ProviderUnavailable("FINVIZ_ASSET_TYPE_UNSUPPORTED")
        ticker = symbol.strip().upper()
        try:
            response = self._session.get(
                self.endpoint,
                params={"t": ticker, "p": "d"},
                headers={
                    "Accept": "text/html",
                    "User-Agent": "Mozilla/5.0 AppleWebKit/537.36 Kelly-Allocation-Lab/1.0",
                },
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderUnavailable("FINVIZ_REQUEST_FAILED") from None
        status = int(getattr(response, "status_code", 0))
        if status == 429:
            raise ProviderResponseError("FINVIZ_RATE_LIMITED")
        if status in {401, 403}:
            raise ProviderUnavailable("FINVIZ_ACCESS_UNAVAILABLE")
        if status == 404:
            raise ProviderResponseError("FINVIZ_SYMBOL_NOT_FOUND")
        if status != 200:
            raise ProviderResponseError("FINVIZ_HTTP_ERROR")
        text = _response_text(response)
        title_match = re.search(r"<title>\s*([A-Z0-9.\-]+)\s+-", text, re.IGNORECASE)
        if not title_match or _identity_token(title_match.group(1)) != _identity_token(ticker):
            raise ProviderResponseError("FINVIZ_IDENTITY_SYMBOL_MISMATCH")
        match = self._data_pattern.search(text)
        if not match:
            raise ProviderResponseError("FINVIZ_CHART_DATA_MISSING")
        try:
            payload = json.loads(match.group(1))
        except (TypeError, ValueError):
            raise ProviderResponseError("FINVIZ_CHART_PAYLOAD_INVALID") from None
        if not isinstance(payload, dict):
            raise ProviderResponseError("FINVIZ_IDENTITY_SYMBOL_MISMATCH")
        payload_ticker = _identity_token(payload.get("ticker"))
        if payload_ticker != _identity_token(ticker):
            raise ProviderResponseError("FINVIZ_IDENTITY_SYMBOL_MISMATCH")
        timestamps = payload.get("date")
        prices = payload.get("close")
        if not isinstance(timestamps, list) or not isinstance(prices, list):
            raise ProviderResponseError("FINVIZ_CHART_PAYLOAD_INVALID")
        if len(timestamps) != len(prices):
            raise ProviderResponseError("FINVIZ_SERIES_LENGTH_MISMATCH")
        observations: dict[str, float] = {}
        for timestamp, raw_price in zip(timestamps, prices, strict=True):
            price = _valid_price(raw_price)
            if price is None:
                continue
            try:
                day = datetime.fromtimestamp(float(timestamp), UTC).date()
            except (OSError, OverflowError, TypeError, ValueError):
                raise ProviderResponseError("FINVIZ_TIMESTAMP_INVALID") from None
            if start <= day <= end:
                observations[day.isoformat()] = price
        if not observations:
            raise ProviderResponseError("FINVIZ_EMPTY_SERIES")
        rows = sorted(observations.items())
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=tuple(day for day, _ in rows),
            prices=tuple(price for _, price in rows),
            currency=currency or "USD",
            exchange=exchange or "US",
            timezone="America/New_York",
            return_basis="price_return",
            provider="Finviz",
            source_url="https://finviz.com/",
            attribution="Ephemeral price-return cross-check: Finviz public chart",
        )


class FredDexkousProvider:
    """FRED DEXKOUS adapter.

    DEXKOUS is published as *South Korean won to one U.S. dollar*.  Therefore
    the value already is USD/KRW (KRW per USD) and must not be inverted.
    """

    endpoint = "https://fred.stlouisfed.org/graph/fredgraph.csv"
    series_id = "DEXKOUS"
    units = "South Korean Won to One U.S. Dollar"
    inverted = False

    def __init__(self, *, session: requests.Session | None = None, timeout: float = 20) -> None:
        self._session = session or requests.Session()
        self._timeout = timeout

    def history(
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries:
        if start > end:
            raise ValueError("start must be on or before end")
        if _identity_token(symbol) != "USDKRW":
            raise ProviderUnavailable("FRED_DEXKOUS_SYMBOL_UNSUPPORTED")
        if adjust != "none":
            raise ProviderUnavailable("FRED_FX_ADJUSTMENT_UNSUPPORTED")
        if asset_type and asset_type.lower() != "fx":
            raise ProviderResponseError("FRED_DEXKOUS_TYPE_MISMATCH")
        if currency and currency.upper() != "KRW":
            raise ProviderResponseError("FRED_DEXKOUS_CURRENCY_MISMATCH")
        try:
            response = self._session.get(
                self.endpoint,
                params={"id": self.series_id, "cosd": start.isoformat(), "coed": end.isoformat()},
                headers={"Accept": "text/csv", "User-Agent": "Kelly-Allocation-Lab/1.0"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderUnavailable("FRED_REQUEST_FAILED") from None
        status = int(getattr(response, "status_code", 0))
        if status == 429:
            raise ProviderResponseError("FRED_RATE_LIMITED")
        if status in {401, 403}:
            raise ProviderUnavailable("FRED_ACCESS_UNAVAILABLE")
        if status != 200:
            raise ProviderResponseError("FRED_HTTP_ERROR")
        if _looks_like_html(response):
            raise ProviderResponseError("FRED_HTML_CHALLENGE")

        reader = csv.DictReader(io.StringIO(_response_text(response)))
        date_column = "observation_date"
        if reader.fieldnames and "DATE" in reader.fieldnames:
            date_column = "DATE"
        if not reader.fieldnames or not {date_column, self.series_id}.issubset(reader.fieldnames):
            raise ProviderResponseError("FRED_CSV_COLUMNS_INVALID")
        observations: dict[str, float] = {}
        try:
            for row in reader:
                day = date.fromisoformat(str(row.get(date_column, ""))[:10])
                value = _valid_price(row.get(self.series_id))
                if value is not None and start <= day <= end:
                    # DEXKOUS is already KRW per USD.  Do not take a reciprocal.
                    observations[day.isoformat()] = value
        except (TypeError, ValueError):
            raise ProviderResponseError("FRED_CSV_PAYLOAD_INVALID") from None
        if not observations:
            raise ProviderResponseError("FRED_EMPTY_SERIES")
        rows = sorted(observations.items())
        return NormalizedPriceSeries(
            symbol="USD/KRW",
            dates=tuple(day for day, _ in rows),
            prices=tuple(value for _, value in rows),
            currency="KRW",
            exchange=exchange or "FX",
            timezone="America/New_York",
            return_basis="fx_rate",
            provider="FRED (Federal Reserve Board H.10)",
            source_url="https://fred.stlouisfed.org/series/DEXKOUS",
            attribution=(
                "Board of Governors of the Federal Reserve System (US), DEXKOUS; "
                "retrieved from FRED, Federal Reserve Bank of St. Louis"
            ),
        )


# Short aliases keep orchestrator configuration readable.
FinanceDataReaderProvider = FinanceDataReaderYahooProvider
FredFxProvider = FredDexkousProvider
