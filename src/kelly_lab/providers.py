from __future__ import annotations

import csv
import io
import json
import os
import time
from dataclasses import dataclass
from datetime import date, timedelta
from pathlib import Path
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
        self,
        symbol: str,
        start: date,
        end: date,
        *,
        adjust: str,
        exchange: str | None = None,
        currency: str | None = None,
        asset_type: str | None = None,
    ) -> NormalizedPriceSeries: ...


class TwelveDataProvider:
    """Licensed Twelve Data adapter that never enables itself implicitly."""

    endpoint = "https://api.twelvedata.com/time_series"
    _special_exchanges = {"INDEX", "FX", "US"}
    _us_exchanges = {
        "AMEX",
        "BATS",
        "CBOE",
        "NASDAQ",
        "NYSE",
        "NYSEARCA",
        "US",
    }

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

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def rights_approved(self) -> bool:
        return bool(self._approved)

    @staticmethod
    def _identity_token(value: object) -> str:
        return "".join(character for character in str(value or "").upper() if character.isalnum())

    @classmethod
    def _validate_identity(
        cls,
        meta: object,
        *,
        symbol: str,
        exchange: str | None,
        currency: str | None,
        asset_type: str | None,
    ) -> dict[str, object]:
        """Reject a valid-looking response that belongs to another instrument."""

        if not isinstance(meta, dict):
            raise ProviderResponseError("TWELVE_DATA_IDENTITY_METADATA_MISSING")
        if cls._identity_token(meta.get("symbol")) != cls._identity_token(symbol):
            raise ProviderResponseError("TWELVE_DATA_IDENTITY_SYMBOL_MISMATCH")

        expected_exchange = cls._identity_token(exchange)
        actual_exchange = cls._identity_token(meta.get("exchange"))
        provider_type = cls._identity_token(meta.get("type"))
        if expected_exchange and expected_exchange not in cls._special_exchanges:
            if actual_exchange != expected_exchange:
                raise ProviderResponseError("TWELVE_DATA_IDENTITY_EXCHANGE_MISMATCH")
        elif expected_exchange == "US":
            if actual_exchange not in cls._us_exchanges:
                raise ProviderResponseError("TWELVE_DATA_IDENTITY_EXCHANGE_MISMATCH")
        elif expected_exchange == "INDEX":
            if actual_exchange != "INDEX" and "INDEX" not in provider_type:
                raise ProviderResponseError("TWELVE_DATA_IDENTITY_EXCHANGE_MISMATCH")
        elif expected_exchange == "FX":
            is_fx = actual_exchange in {"FX", "FOREX"} or any(
                token in provider_type for token in ("CURRENCY", "FOREX", "FX")
            )
            if not is_fx:
                raise ProviderResponseError("TWELVE_DATA_IDENTITY_EXCHANGE_MISMATCH")

        if asset_type != "fx" and currency:
            if cls._identity_token(meta.get("currency")) != cls._identity_token(currency):
                raise ProviderResponseError("TWELVE_DATA_IDENTITY_CURRENCY_MISMATCH")
        return meta

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
        if not self.available:
            raise ProviderUnavailable("TWELVE_DATA_LICENSE_OR_KEY_UNAVAILABLE")
        if adjust not in {"all", "splits", "dividends", "none"}:
            raise ValueError("adjust must be all, splits, dividends, or none")
        if start > end:
            raise ValueError("start must be on or before end")

        params: dict[str, str | int] = {
            "symbol": symbol,
            "interval": "1day",
            "start_date": start.isoformat(),
            "end_date": end.isoformat(),
            "order": "ASC",
            "timezone": "Exchange",
            "adjust": adjust,
            "outputsize": 5000,
            "format": "JSON",
        }
        if exchange and exchange.upper() not in self._special_exchanges:
            params["exchange"] = exchange
        try:
            response = self._session.get(
                self.endpoint,
                params=params,
                headers={"Authorization": f"apikey {self._api_key}", "Accept": "application/json"},
                timeout=20,
            )
        except requests.RequestException:
            raise ProviderUnavailable("TWELVE_DATA_REQUEST_FAILED") from None
        if response.status_code in {401, 403, 404}:
            raise ProviderUnavailable("TWELVE_DATA_ACCESS_UNAVAILABLE")
        if response.status_code == 429:
            raise ProviderResponseError("TWELVE_DATA_RATE_LIMITED")
        if response.status_code != 200:
            raise ProviderResponseError("TWELVE_DATA_REQUEST_FAILED")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderResponseError("TWELVE_DATA_PAYLOAD_INVALID") from None
        if not isinstance(payload, dict):
            raise ProviderResponseError("TWELVE_DATA_PAYLOAD_INVALID")
        if payload.get("status") == "error":
            raise ProviderResponseError("TWELVE_DATA_PROVIDER_ERROR")
        values = payload.get("values")
        if not isinstance(values, list) or not values:
            raise ProviderResponseError("TWELVE_DATA_EMPTY_SERIES")
        meta = self._validate_identity(
            payload.get("meta"),
            symbol=symbol,
            exchange=exchange,
            currency=currency,
            asset_type=asset_type,
        )

        try:
            rows = sorted(values, key=lambda row: str(row["datetime"]))
            dates = tuple(str(row["datetime"])[:10] for row in rows)
            prices = tuple(float(row["close"]) for row in rows)
        except (KeyError, TypeError, ValueError):
            raise ProviderResponseError("TWELVE_DATA_PAYLOAD_INVALID") from None
        return NormalizedPriceSeries(
            symbol=symbol,
            dates=dates,
            prices=prices,
            currency=currency or str(meta.get("currency", "USD")),
            exchange=exchange or str(meta.get("exchange", "UNKNOWN")),
            timezone=str(meta.get("exchange_timezone", "Exchange")),
            return_basis="total_return_approximation" if adjust == "all" else "price_return",
            provider="Twelve Data",
            source_url="https://twelvedata.com/",
            attribution="Data provided by Twelve Data",
        )


class KrxOfficialApiProvider:
    """Official KRX daily-close adapter for the two allowlisted Korean equities.

    KRX publishes one market-wide file per business date, so this adapter fetches
    both requested tickers in a single pass and keeps only the selected close
    values in a git-ignored local cache. Credentials and raw responses are never
    persisted or included in public artifacts.
    """

    endpoint = "https://data-dbg.krx.co.kr/svc/apis/sto/stk_bydd_trd"
    public_source_url = "https://openapi.krx.co.kr/"

    def __init__(
        self,
        api_key: str | None = None,
        *,
        public_display_approved: bool | None = None,
        session: requests.Session | None = None,
        timeout: float = 20,
        cache_dir: Path | None = None,
        min_interval_seconds: float = 0.04,
    ) -> None:
        self._api_key = api_key or os.getenv("KRX_API_KEY")
        if public_display_approved is None:
            value = os.getenv("KRX_PUBLIC_DISPLAY_APPROVED", "false")
            public_display_approved = value.lower() == "true"
        self._approved = public_display_approved
        self._session = session or requests.Session()
        self._timeout = timeout
        self._cache_dir = cache_dir
        self._min_interval_seconds = max(0.0, min_interval_seconds)
        self._last_request_at = 0.0

    @property
    def available(self) -> bool:
        return bool(self._api_key and self._approved)

    @property
    def configured(self) -> bool:
        return bool(self._api_key)

    @property
    def rights_approved(self) -> bool:
        return bool(self._approved)

    @staticmethod
    def _price(value: object) -> float:
        return float(str(value).replace(",", "").strip())

    def _cache_path(self, day: date, symbols: tuple[str, ...]) -> Path | None:
        if self._cache_dir is None:
            return None
        key = "-".join(symbols)
        return self._cache_dir / f"{day.isoformat()}-{key}.json"

    def _selected_closes(self, day: date, symbols: tuple[str, ...]) -> dict[str, float]:
        cache_path = self._cache_path(day, symbols)
        if cache_path and cache_path.exists():
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
                return {str(key): float(value) for key, value in payload.items()}
            except (OSError, ValueError, TypeError):
                pass

        if not self.available:
            raise ProviderUnavailable("KRX_PUBLIC_DISPLAY_RIGHTS_OR_KEY_UNAVAILABLE")
        wait_for = self._min_interval_seconds - (time.monotonic() - self._last_request_at)
        if wait_for > 0:
            time.sleep(wait_for)
        try:
            response = self._session.get(
                self.endpoint,
                params={"basDd": day.strftime("%Y%m%d")},
                headers={"AUTH_KEY": self._api_key, "Accept": "application/json"},
                timeout=self._timeout,
            )
        except requests.RequestException:
            raise ProviderUnavailable("KRX_OFFICIAL_REQUEST_FAILED") from None
        finally:
            self._last_request_at = time.monotonic()
        if response.status_code != 200:
            raise ProviderResponseError(f"KRX_OFFICIAL_HTTP_{response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise ProviderResponseError("KRX_OFFICIAL_PAYLOAD_INVALID") from None
        rows = payload.get("OutBlock_1") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            raise ProviderResponseError("KRX_OFFICIAL_CONTRACT_CHANGED")

        wanted = set(symbols)
        selected: dict[str, float] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("ISU_SRT_CD") or row.get("ISU_CD") or "").strip()
            if symbol not in wanted:
                continue
            try:
                price = self._price(row.get("TDD_CLSPRC"))
            except (TypeError, ValueError):
                continue
            if price > 0:
                selected[symbol] = price
        if cache_path is not None:
            cache_path.parent.mkdir(parents=True, exist_ok=True)
            cache_path.write_text(
                json.dumps(selected, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
            )
        return selected

    def history_many(
        self,
        symbols: list[str] | tuple[str, ...],
        start: date,
        end: date,
    ) -> dict[str, NormalizedPriceSeries]:
        if start > end:
            raise ValueError("start must be on or before end")
        normalized_symbols = tuple(sorted({str(symbol).removesuffix(".KS") for symbol in symbols}))
        if not normalized_symbols:
            raise ValueError("at least one KRX symbol is required")
        observations: dict[str, list[tuple[str, float]]] = {
            symbol: [] for symbol in normalized_symbols
        }
        day = start
        while day <= end:
            if day.weekday() < 5:
                closes = self._selected_closes(day, normalized_symbols)
                for symbol, price in closes.items():
                    observations[symbol].append((day.isoformat(), price))
            day += timedelta(days=1)

        result: dict[str, NormalizedPriceSeries] = {}
        for symbol, rows in observations.items():
            if not rows:
                raise ProviderResponseError(f"KRX_OFFICIAL_EMPTY_SERIES_{symbol}")
            result[symbol] = NormalizedPriceSeries(
                symbol=f"{symbol}.KS",
                dates=tuple(day_value for day_value, _ in rows),
                prices=tuple(price for _, price in rows),
                currency="KRW",
                exchange="KRX",
                timezone="Asia/Seoul",
                return_basis="price_return",
                provider="Korea Exchange",
                source_url=self.public_source_url,
                attribution="한국거래소 통계정보",
            )
        return result


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
            attribution="한국거래소 통계정보",
        )
