from __future__ import annotations

import csv
import io
import os
from dataclasses import dataclass
from datetime import date
from typing import Protocol

import requests


class ProviderUnavailable(RuntimeError):
    """Raised when a source cannot be used under the public-display policy."""


class ProviderResponseError(RuntimeError):
    """Raised when an approved source returns an unusable response."""


@dataclass(frozen=True)
class NormalizedPriceSeries:
    symbol: str
    dates: tuple[str, ...]
    prices: tuple[float, ...]
    currency: str
    exchange: str
    timezone: str
    return_basis: str
    provider: str
    source_url: str
    attribution: str

    def __post_init__(self) -> None:
        if len(self.dates) != len(self.prices):
            raise ValueError("dates and prices must have the same length")
        if not self.dates:
            raise ValueError("price series must contain observations")


class PriceProvider(Protocol):
    def history(
        self, symbol: str, start: date, end: date, *, adjust: str
    ) -> NormalizedPriceSeries: ...


class TwelveDataProvider:
    """Licensed Twelve Data adapter that never enables itself implicitly."""

    endpoint = "https://api.twelvedata.com/time_series"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        external_display_approved: bool | None = None,
        session: requests.Session | None = None,
    ) -> None:
        self._api_key = api_key or os.getenv("TWELVE_DATA_API_KEY")
        if external_display_approved is None:
            value = os.getenv("TWELVE_DATA_EXTERNAL_DISPLAY_APPROVED", "false")
            external_display_approved = value.lower() == "true"
        self._approved = external_display_approved
        self._session = session or requests.Session()

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._approved)

    def history(self, symbol: str, start: date, end: date, *, adjust: str) -> NormalizedPriceSeries:
        if not self.available:
            raise ProviderUnavailable("TWELVE_DATA_LICENSE_OR_KEY_UNAVAILABLE")
        if adjust not in {"all", "splits", "dividends", "none"}:
            raise ValueError("adjust must be all, splits, dividends, or none")
        if start > end:
            raise ValueError("start must be on or before end")

        response = self._session.get(
            self.endpoint,
            params={
                "symbol": symbol,
                "interval": "1day",
                "start_date": start.isoformat(),
                "end_date": end.isoformat(),
                "order": "ASC",
                "timezone": "Exchange",
                "adjust": adjust,
                "outputsize": 5000,
                "format": "JSON",
                "apikey": self._api_key,
            },
            timeout=20,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("status") == "error":
            code = payload.get("code", "UNKNOWN")
            raise ProviderResponseError(f"TWELVE_DATA_ERROR_{code}")
        values = payload.get("values")
        meta = payload.get("meta") or {}
        if not isinstance(values, list) or not values:
            raise ProviderResponseError("TWELVE_DATA_EMPTY_SERIES")

        rows = sorted(values, key=lambda row: row["datetime"])
        return NormalizedPriceSeries(
            symbol=str(meta.get("symbol", symbol)),
            dates=tuple(str(row["datetime"])[:10] for row in rows),
            prices=tuple(float(row["close"]) for row in rows),
            currency=str(meta.get("currency", "USD")),
            exchange=str(meta.get("exchange", "UNKNOWN")),
            timezone=str(meta.get("exchange_timezone", "Exchange")),
            return_basis="total_return_approximation" if adjust == "all" else "price_return",
            provider="Twelve Data",
            source_url="https://twelvedata.com/",
            attribution="Data provided by Twelve Data",
        )


class KrxOfficialCsvProvider:
    """Normalize a reviewed CSV export obtained from an official KRX surface."""

    allowed_hosts = {"data.krx.co.kr", "global.krx.co.kr", "openapi.krx.co.kr"}

    def parse(
        self,
        text: str,
        *,
        symbol: str,
        source_url: str,
        date_column: str = "date",
        price_column: str = "close",
    ) -> NormalizedPriceSeries:
        from urllib.parse import urlparse

        host = (urlparse(source_url).hostname or "").lower()
        if host not in self.allowed_hosts:
            raise ProviderUnavailable("KRX_OFFICIAL_SOURCE_REQUIRED")
        reader = csv.DictReader(io.StringIO(text))
        rows = list(reader)
        if not rows or date_column not in rows[0] or price_column not in rows[0]:
            raise ProviderResponseError("KRX_CSV_COLUMNS_INVALID")
        rows.sort(key=lambda row: row[date_column])
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=tuple(row[date_column].replace(".", "-")[:10] for row in rows),
            prices=tuple(float(row[price_column].replace(",", "")) for row in rows),
            currency="KRW",
            exchange="KRX",
            timezone="Asia/Seoul",
            return_basis="price_return",
            provider="Korea Exchange",
            source_url=source_url,
            attribution="Source: Korea Exchange (KRX)",
        )
