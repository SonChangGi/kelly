"""Bounded on-demand US equity and ETF history collection.

This module deliberately does not mutate the locked 50-asset catalog.  A user
symbol is discovered through Yahoo metadata, constrained to US-listed USD
equities or ETFs, and written only below a dedicated dynamic cache directory.
Yahoo adjusted close is the default research series.  FinanceDataReader is a
same-upstream transport fallback, while Stooq remains an independent
price-return cross-check and can become primary only for an explicit
price-return request.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import statistics
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .data_quality import validate_price_series
from .free_providers import (
    FinanceDataReaderYahooProvider,
    FinvizChartProvider,
    StooqCsvProvider,
    YahooChartProvider,
    YahooInstrumentMetadata,
)
from .providers import NormalizedPriceSeries, ProviderResponseError, ProviderUnavailable

MAX_HISTORY_DAYS = 1827
INCREMENTAL_OVERLAP_DAYS = 35
MIN_CROSSCHECK_PRICES = 21
MIN_KELLY_RETURNS = 60
SYMBOL_PATTERN = re.compile(r"^[A-Z][A-Z0-9]{0,9}(?:-[A-Z0-9]{1,5})?$")
US_EXCHANGE_TOKENS = {
    "ASE",
    "BATS",
    "BTS",
    "CBOE",
    "CBOEBZX",
    "IEX",
    "NCM",
    "NGM",
    "NMS",
    "NASDAQ",
    "NASDAQCM",
    "NASDAQGM",
    "NASDAQGS",
    "NYQ",
    "NYSE",
    "NYSEAMERICAN",
    "NYSEARCA",
    "PCX",
}
RETURN_BASIS_BY_MODE = {
    "adjusted": "total_return_approximation",
    "price": "price_return",
}
YAHOO_PUBLIC_DISPLAY_APPROVAL_ENV = "YAHOO_PUBLIC_DISPLAY_APPROVED"
EXCLUDED_3X_SYMBOLS = {
    "BERZ",
    "BULZ",
    "CURE",
    "DRIP",
    "DRN",
    "DRV",
    "DUST",
    "EDC",
    "EDZ",
    "ERX",
    "ERY",
    "FAS",
    "FAZ",
    "GUSH",
    "JDST",
    "JNUG",
    "LABD",
    "LABU",
    "MIDU",
    "NUGT",
    "RETL",
    "SDOW",
    "SOXL",
    "SOXS",
    "SPXL",
    "SPXS",
    "SQQQ",
    "TECL",
    "TECS",
    "TMF",
    "TMV",
    "TNA",
    "TQQQ",
    "TZA",
    "UDOW",
    "UMDD",
    "UPRO",
    "WEBL",
    "WEBS",
    "YANG",
    "YINN",
}


def is_non_common_security_name(value: str | None) -> bool:
    """Reject high-confidence preferred, unit, warrant, right, and debt descriptions."""

    name = " ".join(str(value or "").upper().split())
    if not name:
        return False
    if re.search(r"\bPREFERRED\b|\bWARRANTS?\b|\bRIGHT TO PURCHASE\b", name):
        return True
    if re.search(r"\b(?:NOTES?|DEBENTURES?|BONDS?) DUE\b|\bZONES\b", name):
        return True
    if "TANGIBLE EQUITY UNIT" in name:
        return True
    return bool(
        re.search(
            r"\bUNITS?,? (?:EACH|CONSISTING|COMPRISED|INCLUDING|REPRESENTING)\b",
            name,
        )
        or re.search(r"\bUNITS?$", name)
    )


class DynamicAssetError(ValueError):
    """A stable failure reason for the on-demand collector."""

    def __init__(self, reason: str, message: str):
        self.reason = reason
        super().__init__(message)


def require_yahoo_public_display_approval(cache_scope: str) -> None:
    """Keep Yahoo-derived public static collection behind an explicit rights gate."""

    if cache_scope not in {"local", "public"}:
        raise DynamicAssetError("invalid_cache_scope", "cache scope must be local or public")
    if (
        cache_scope == "public"
        and os.getenv(YAHOO_PUBLIC_DISPLAY_APPROVAL_ENV, "false").strip().lower() != "true"
    ):
        raise DynamicAssetError(
            "public_display_approval_required",
            "public Yahoo-derived cache collection requires "
            f"{YAHOO_PUBLIC_DISPLAY_APPROVAL_ENV}=true",
        )


def normalize_us_symbol(value: str) -> str:
    """Normalize one conservative US-listed ticker and reject URL/shell syntax."""

    if not isinstance(value, str):
        raise DynamicAssetError("invalid_symbol", "ticker must be a string")
    stripped = value.strip().upper().replace(".", "-")
    if stripped != value.strip().upper() and value.count(".") > 1:
        raise DynamicAssetError("invalid_symbol", "ticker contains too many class separators")
    if len(stripped) > 16 or not SYMBOL_PATTERN.fullmatch(stripped):
        raise DynamicAssetError(
            "invalid_symbol",
            "ticker must use only letters, digits, and one optional class-share hyphen",
        )
    return stripped


def bounded_history_period(
    start: date | None,
    end: date | None,
    *,
    today: date | None = None,
) -> tuple[date, date]:
    """Resolve an inclusive history interval bounded to five calendar years."""

    current = today or date.today()
    resolved_end = end or current
    resolved_start = start or (resolved_end - timedelta(days=MAX_HISTORY_DAYS))
    if resolved_end > current:
        raise DynamicAssetError("future_end_date", "end date cannot be in the future")
    if resolved_start > resolved_end:
        raise DynamicAssetError("invalid_dates", "start date cannot be after end date")
    if (resolved_end - resolved_start).days > MAX_HISTORY_DAYS:
        raise DynamicAssetError(
            "history_window_too_large",
            "on-demand history is limited to five calendar years",
        )
    return resolved_start, resolved_end


def _identity_token(value: str) -> str:
    return "".join(character for character in value.upper() if character.isalnum())


def validate_us_metadata(
    requested_symbol: str,
    metadata: YahooInstrumentMetadata,
) -> tuple[str, str]:
    """Validate Yahoo metadata and return canonical symbol plus asset type."""

    requested = normalize_us_symbol(requested_symbol)
    provider_symbol = normalize_us_symbol(metadata.provider_symbol)
    if _identity_token(provider_symbol) != _identity_token(requested):
        raise DynamicAssetError("identity_symbol_mismatch", "provider returned another symbol")
    if metadata.instrument_type not in {"EQUITY", "ETF"}:
        raise DynamicAssetError(
            "unsupported_asset_type",
            "only US equities and ETFs are accepted by the on-demand collector",
        )
    observed_name = " ".join(
        value for value in (metadata.long_name, metadata.short_name) if value
    ).upper()
    if metadata.instrument_type == "EQUITY" and is_non_common_security_name(observed_name):
        raise DynamicAssetError(
            "excluded_non_common_security",
            "preferred shares, units, warrants, rights, and debt-like listings are excluded",
        )
    if metadata.instrument_type == "ETF" and (
        provider_symbol in EXCLUDED_3X_SYMBOLS
        or "ULTRAPRO" in observed_name
        or re.search(r"(?<![0-9])3\s*X(?![0-9])", observed_name)
    ):
        raise DynamicAssetError(
            "excluded_3x_product",
            "3x and inverse-3x products are excluded from Kelly Allocation Lab v1",
        )
    if metadata.currency != "USD":
        raise DynamicAssetError("identity_currency_mismatch", "instrument currency must be USD")
    if metadata.timezone != "America/New_York":
        raise DynamicAssetError(
            "identity_market_mismatch",
            "instrument must use the US Eastern exchange timezone",
        )
    exchange_tokens = {
        _identity_token(metadata.exchange_code),
        _identity_token(metadata.exchange_name),
    }
    if not exchange_tokens & US_EXCHANGE_TOKENS:
        raise DynamicAssetError(
            "identity_exchange_mismatch",
            "instrument is not on an accepted US exchange",
        )
    return provider_symbol, "equity" if metadata.instrument_type == "EQUITY" else "etf"


def _trim_to_first_trade(
    series: NormalizedPriceSeries,
    first_trade_date: str | None,
) -> NormalizedPriceSeries:
    if first_trade_date is None:
        return series
    rows = [
        (day, float(price))
        for day, price in zip(series.dates, series.prices, strict=True)
        if day >= first_trade_date
    ]
    if len(rows) < 2:
        raise DynamicAssetError(
            "insufficient_observations",
            "fewer than two observations remain after the provider first-trade date",
        )
    return replace(
        series,
        dates=tuple(day for day, _price in rows),
        prices=tuple(price for _day, price in rows),
    )


def _validate_history_identity(
    series: NormalizedPriceSeries,
    *,
    canonical_symbol: str,
    expected_return_basis: str,
    start: date,
    end: date,
) -> None:
    try:
        series_symbol = normalize_us_symbol(series.symbol)
    except DynamicAssetError as error:
        raise DynamicAssetError("history_identity_mismatch", str(error)) from error
    if _identity_token(series_symbol) != _identity_token(canonical_symbol):
        raise DynamicAssetError("history_identity_mismatch", "history belongs to another symbol")
    if series.currency.upper() != "USD":
        raise DynamicAssetError("history_currency_mismatch", "history currency must be USD")
    if series.return_basis != expected_return_basis:
        raise DynamicAssetError(
            "return_basis_mismatch",
            "fallback history does not preserve the requested return basis",
        )
    if any(day < start.isoformat() or day > end.isoformat() for day in series.dates):
        raise DynamicAssetError(
            "history_date_out_of_bounds", "provider returned an out-of-range row"
        )


def _fetch_primary_history(
    *,
    canonical_symbol: str,
    asset_type: str,
    exchange: str,
    start: date,
    end: date,
    basis_mode: str,
    yahoo_provider: Any,
    fdr_provider: Any,
    stooq_provider: Any,
) -> tuple[NormalizedPriceSeries, list[str]]:
    failures: list[str] = []
    if basis_mode == "adjusted":
        chain = (yahoo_provider, fdr_provider)
        adjustment = "all"
    else:
        chain = (yahoo_provider, stooq_provider)
        adjustment = "none"

    for provider in chain:
        try:
            series = provider.history(
                canonical_symbol,
                start,
                end,
                adjust=adjustment,
                exchange=exchange,
                currency="USD",
                asset_type=asset_type,
            )
            return series, failures
        except (ProviderUnavailable, ProviderResponseError) as error:
            failures.append(str(error).lower())
    raise DynamicAssetError(
        "provider_chain_exhausted",
        "no semantically compatible history provider succeeded"
        + (f": {failures[-1]}" if failures else ""),
    )


def _crosscheck_returns(
    primary: NormalizedPriceSeries,
    secondary: NormalizedPriceSeries,
    *,
    provider_name: str,
) -> dict[str, Any]:
    primary_rows = dict(zip(primary.dates, primary.prices, strict=True))
    secondary_rows = dict(zip(secondary.dates, secondary.prices, strict=True))
    common = sorted(set(primary_rows) & set(secondary_rows))
    result: dict[str, Any] = {
        "provider": provider_name,
        "state": "insufficient",
        "commonObservations": max(0, len(common) - 1),
        "windowStart": common[0] if common else None,
        "windowEnd": common[-1] if common else None,
        "medianAbsReturnDifference": None,
        "p99AbsReturnDifference": None,
    }
    if len(common) < MIN_CROSSCHECK_PRICES:
        return result
    differences = []
    for previous, current in zip(common, common[1:], strict=False):
        primary_return = float(primary_rows[current]) / float(primary_rows[previous]) - 1.0
        secondary_return = float(secondary_rows[current]) / float(secondary_rows[previous]) - 1.0
        differences.append(abs(primary_return - secondary_return))
    ordered = sorted(differences)
    p99_index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.99))
    median = float(statistics.median(ordered))
    p99 = float(ordered[p99_index])
    result.update(
        {
            "state": "passed" if median <= 0.002 and p99 <= 0.08 else "mismatch",
            "commonObservations": len(ordered),
            "medianAbsReturnDifference": median,
            "p99AbsReturnDifference": p99,
        }
    )
    return result


def _independent_crosscheck(
    primary: NormalizedPriceSeries,
    *,
    canonical_symbol: str,
    asset_type: str,
    exchange: str,
    start: date,
    end: date,
    stooq_provider: Any,
    finviz_provider: Any,
) -> dict[str, Any]:
    primary_name = primary.provider.lower()
    if "stooq" in primary_name or "finviz" in primary_name:
        return {
            "provider": "none",
            "state": "not_applicable",
            "commonObservations": 0,
            "windowStart": None,
            "windowEnd": None,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
            "attempts": [],
        }

    # Finviz is an ephemeral recent-window check.  Its raw observations are
    # discarded after these bounded return-difference aggregates are made.
    providers: list[tuple[str, Any, date]] = [
        ("stooq", stooq_provider, start),
        ("finviz", finviz_provider, max(start, end - timedelta(days=120))),
    ]

    attempts: list[dict[str, Any]] = []
    best_result: dict[str, Any] | None = None
    for provider_name, provider, check_start in providers:
        try:
            secondary = provider.history(
                canonical_symbol,
                check_start,
                end,
                adjust="none",
                exchange=exchange,
                currency="USD",
                asset_type=asset_type,
            )
            if secondary.return_basis != "price_return":
                raise ProviderResponseError(f"{provider_name.upper()}_RETURN_BASIS_MISMATCH")
            result = _crosscheck_returns(
                primary,
                secondary,
                provider_name=provider_name,
            )
            attempts.append(
                {
                    "provider": provider_name,
                    "state": result["state"],
                    "reasonCode": None,
                }
            )
            result["attempts"] = list(attempts)
            if result["state"] in {"passed", "mismatch"}:
                return result
            best_result = result
        except (ProviderUnavailable, ProviderResponseError, DynamicAssetError) as error:
            normalized_reason = str(error).strip().lower().replace(":", "_")
            if not re.fullmatch(r"[a-z0-9_-]{1,120}", normalized_reason):
                normalized_reason = f"{provider_name}_unavailable"
            attempts.append(
                {
                    "provider": provider_name,
                    "state": "unavailable",
                    "reasonCode": normalized_reason,
                }
            )
            unavailable = {
                "provider": provider_name,
                "state": "unavailable",
                "commonObservations": 0,
                "windowStart": None,
                "windowEnd": None,
                "medianAbsReturnDifference": None,
                "p99AbsReturnDifference": None,
                "attempts": list(attempts),
            }
            # Preserve a prior insufficient comparison because it contains
            # stronger evidence than a later transport failure.  Otherwise the
            # final provider must identify the last failed attempt instead of
            # incorrectly attributing a two-provider failure to Stooq alone.
            if best_result is None or best_result["state"] == "unavailable":
                best_result = unavailable
    assert best_result is not None
    best_result["attempts"] = attempts
    return best_result


def _source_fields(series: NormalizedPriceSeries) -> tuple[str, str, str]:
    provider_name = series.provider.lower()
    if "financedatareader" in provider_name:
        return (
            "yahoo_finance",
            "finance_data_reader",
            "Yahoo Finance research data retrieved through FinanceDataReader; "
            "no vendor license asserted",
        )
    if "yahoo" in provider_name:
        return (
            "yahoo_finance",
            "native",
            "Yahoo Finance research data; no vendor license asserted",
        )
    if "stooq" in provider_name:
        return "stooq", "native", "Stooq research data; no vendor license asserted"
    raise DynamicAssetError("unrecognized_provider", "normalized provider identity is unknown")


def _content_digest(
    dates: list[str] | tuple[str, ...],
    prices: list[float] | tuple[float, ...],
) -> str:
    payload = "\n".join(
        f"{day}:{float(price):.12g}" for day, price in zip(dates, prices, strict=True)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _series_digest(series: NormalizedPriceSeries) -> str:
    return _content_digest(series.dates, series.prices)


def _asset_id(symbol: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", symbol.lower()).strip("-")
    return f"dynamic-us-{slug}"


def build_dynamic_document(
    *,
    metadata: YahooInstrumentMetadata,
    canonical_symbol: str,
    asset_type: str,
    series: NormalizedPriceSeries,
    crosscheck: dict[str, Any],
    generated_at: str,
    quality_state: str,
    fallback_failures: list[str],
) -> dict[str, Any]:
    provider, adapter, license_label = _source_fields(series)
    prices = [float(value) for value in series.prices]
    returns = [None] + [prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices))]
    state = quality_state
    if state == "published" and crosscheck["state"] in {
        "unavailable",
        "insufficient",
        "mismatch",
    }:
        state = "degraded"
    limitations = [
        "On-demand symbol discovered from provider metadata; not part of the locked core catalog.",
        (
            "Adjusted daily close is a total-return approximation; taxes, slippage, and "
            "financing are excluded."
            if series.return_basis == "total_return_approximation"
            else (
                "Daily price-return series; dividends, taxes, slippage, and financing are excluded."
            )
        ),
    ]
    if fallback_failures:
        limitations.append(
            "A higher-priority compatible provider failed before fallback succeeded."
        )
    if crosscheck["state"] != "passed":
        limitations.append(f"Independent price-return cross-check: {crosscheck['state']}.")

    return {
        "schemaVersion": 1,
        "contract": "kelly-asset-history",
        "state": state,
        "assetId": _asset_id(canonical_symbol),
        "generatedAt": generated_at,
        "dataAsOf": series.dates[-1],
        "metadata": {
            "symbol": canonical_symbol,
            "assetType": asset_type,
            "exchange": metadata.exchange_name,
            "timezone": metadata.timezone,
            "returnBasis": series.return_basis,
            "baseCurrency": "USD",
            "catalogScope": "dynamic",
            "providerSymbol": metadata.provider_symbol,
            "providerExchangeCode": metadata.exchange_code,
            "instrumentType": metadata.instrument_type,
            "displayName": metadata.long_name or metadata.short_name,
            "firstTradeDate": metadata.first_trade_date,
        },
        "dates": list(series.dates),
        "prices": prices,
        "returns": returns,
        "source": {
            "provider": provider,
            "adapter": adapter,
            "contentDigest": _series_digest(series),
            "normalized": True,
            "rawRedistribution": False,
            "sourceUrl": series.source_url,
            "license": license_label,
            "attribution": series.attribution,
            "cachedAt": generated_at,
        },
        "quality": {
            "observationCount": len(series.dates),
            "eligibleForKelly": len(series.dates) - 1 >= MIN_KELLY_RETURNS,
            "minimumKellyObservations": MIN_KELLY_RETURNS,
            "crossCheck": crosscheck,
        },
        "limitations": limitations,
    }


def dynamic_cache_path(root: Path, symbol: str, scope: str) -> Path:
    canonical = normalize_us_symbol(symbol)
    if scope not in {"local", "public"}:
        raise DynamicAssetError("invalid_cache_scope", "cache scope must be local or public")
    root_resolved = root.resolve()
    relative_base = Path("var/dynamic-assets" if scope == "local" else "data/dynamic-assets")
    unresolved_base = root / relative_base
    if unresolved_base.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic cache directory cannot be a symlink")
    base = unresolved_base.resolve()
    if not base.is_relative_to(root_resolved):
        raise DynamicAssetError("unsafe_cache_path", "dynamic cache escaped the project root")
    unresolved_candidate = base / f"{_asset_id(canonical)}.json"
    if unresolved_candidate.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic cache target cannot be a symlink")
    candidate = unresolved_candidate.resolve()
    if candidate.parent != base or not candidate.is_relative_to(root_resolved):
        raise DynamicAssetError("unsafe_cache_path", "dynamic cache target is outside its scope")
    protected = (root_resolved / "data/assets").resolve()
    if candidate.is_relative_to(protected):
        raise DynamicAssetError("unsafe_cache_path", "dynamic assets cannot enter the core cache")
    return candidate


def _validate_contract(root: Path, document: dict[str, Any]) -> None:
    schema_path = root / "schemas/asset.schema.json"
    if not schema_path.is_file():
        raise DynamicAssetError("schema_unavailable", "asset schema is missing")
    schema = json.loads(schema_path.read_text(encoding="utf-8"))
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: error.json_path,
    )
    if errors:
        details = "; ".join(f"{error.json_path}: {error.message}" for error in errors[:5])
        raise DynamicAssetError("contract_invalid", details)


def _load_incremental_baseline(
    root: Path,
    path: Path,
    *,
    canonical_symbol: str,
    expected_return_basis: str,
    requested_start: date | None,
    requested_end: date,
    backfill: bool,
) -> dict[str, Any] | None:
    """Load one trustworthy last-good file before making an incremental request."""

    if backfill or not path.exists():
        return None
    if path.is_symlink() or not path.is_file():
        raise DynamicAssetError(
            "existing_cache_invalid",
            "existing dynamic cache is unsafe; use an explicit backfill after review",
        )
    try:
        document = json.loads(path.read_text(encoding="utf-8"))
        _validate_contract(root, document)
        metadata = document["metadata"]
        dates = document["dates"]
        prices = document["prices"]
        returns = document["returns"]
        source = document["source"]
        if document["state"] not in {"published", "stale", "degraded"}:
            return None
        if (
            document["assetId"] != _asset_id(canonical_symbol)
            or metadata["symbol"] != canonical_symbol
            or metadata.get("catalogScope") != "dynamic"
            or metadata.get("baseCurrency") != "USD"
        ):
            raise DynamicAssetError(
                "existing_cache_identity_mismatch",
                "existing dynamic cache belongs to another instrument",
            )
        if metadata["returnBasis"] != expected_return_basis:
            raise DynamicAssetError(
                "historical_basis_change_backfill_required",
                "changing the cached return basis requires an explicit backfill",
            )
        if requested_start is not None:
            observed_start = date.fromisoformat(dates[0])
            starts_same_window = (
                requested_start <= observed_start and (observed_start - requested_start).days <= 7
            )
            if not starts_same_window:
                raise DynamicAssetError(
                    "historical_window_change_backfill_required",
                    "changing the start of an existing cache requires an explicit backfill",
                )
        if date.fromisoformat(dates[-1]) > requested_end:
            raise DynamicAssetError(
                "historical_window_change_backfill_required",
                "moving the end before the cached history requires an explicit backfill",
            )
        if (
            not (len(dates) == len(prices) == len(returns))
            or len(dates) < 2
            or dates != sorted(set(dates))
            or returns[0] is not None
            or document["dataAsOf"] != dates[-1]
            or source.get("contentDigest") != _content_digest(dates, prices)
        ):
            raise DynamicAssetError(
                "existing_cache_invalid",
                "existing dynamic cache failed its frozen-history integrity checks",
            )
        for index in range(1, len(prices)):
            expected = float(prices[index]) / float(prices[index - 1]) - 1.0
            actual = returns[index]
            if not isinstance(actual, int | float) or isinstance(actual, bool):
                raise DynamicAssetError(
                    "existing_cache_invalid",
                    "existing dynamic cache contains an invalid return",
                )
            if not math.isfinite(float(actual)) or abs(float(actual) - expected) > 1e-10:
                raise DynamicAssetError(
                    "existing_cache_invalid",
                    "existing dynamic cache failed its frozen-return check",
                )
    except DynamicAssetError:
        raise
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError) as error:
        raise DynamicAssetError(
            "existing_cache_invalid",
            "existing dynamic cache is invalid; use an explicit backfill after review",
        ) from error
    return document


def _merge_incremental_document(
    existing: dict[str, Any],
    candidate: dict[str, Any],
    *,
    retain_from: date,
) -> dict[str, Any]:
    """Preserve frozen returns, permit adjusted-level rebasing, and append only new rows."""

    old_rows = dict(zip(existing["dates"], existing["prices"], strict=True))
    new_rows = dict(zip(candidate["dates"], candidate["prices"], strict=True))
    overlap = sorted(set(old_rows) & set(new_rows))
    if len(overlap) < 2:
        raise DynamicAssetError(
            "historical_overlap_insufficient_backfill_required",
            "incremental history has too little overlap; preserve last-good "
            "and backfill explicitly",
        )
    fetched_start, fetched_end = min(new_rows), max(new_rows)
    expected_overlap_dates = {day for day in old_rows if fetched_start <= day <= fetched_end}
    if not expected_overlap_dates.issubset(new_rows):
        raise DynamicAssetError(
            "historical_observation_removed_backfill_required",
            "an established observation disappeared; preserve last-good and backfill explicitly",
        )

    return_basis = candidate["metadata"]["returnBasis"]
    if return_basis == "total_return_approximation":
        for previous, current in zip(overlap, overlap[1:], strict=False):
            old_return = float(old_rows[current]) / float(old_rows[previous]) - 1.0
            new_return = float(new_rows[current]) / float(new_rows[previous]) - 1.0
            if abs(old_return - new_return) > 2e-6:
                raise DynamicAssetError(
                    "historical_drift_backfill_required",
                    "an established return changed; preserve last-good and backfill explicitly",
                )
    else:
        for day in overlap:
            old_price = float(old_rows[day])
            new_price = float(new_rows[day])
            tolerance = max(abs(old_price) * 1e-10, 1e-8)
            if abs(old_price - new_price) > tolerance:
                raise DynamicAssetError(
                    "historical_drift_backfill_required",
                    "an established price changed; preserve last-good and backfill explicitly",
                )

    retain_token = retain_from.isoformat()
    merged = {str(day): float(price) for day, price in old_rows.items() if str(day) >= retain_token}
    frozen_through = str(existing["dates"][-1])
    scale = 1.0
    if return_basis == "total_return_approximation":
        anchor = overlap[-1]
        scale = float(old_rows[anchor]) / float(new_rows[anchor])
    for day, price in new_rows.items():
        if day > frozen_through:
            merged[day] = float(price) * scale
    ordered = sorted(merged.items())
    if len(ordered) < 2:
        raise DynamicAssetError(
            "insufficient_observations",
            "fewer than two observations remain in the incremental cache",
        )

    dates = [day for day, _price in ordered]
    prices = [price for _day, price in ordered]
    returns: list[float | None] = [None]
    returns.extend(prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices)))
    candidate["dates"] = dates
    candidate["prices"] = prices
    candidate["returns"] = returns
    candidate["dataAsOf"] = dates[-1]
    candidate["source"]["contentDigest"] = _content_digest(dates, prices)
    candidate["quality"]["observationCount"] = len(dates)
    candidate["quality"]["eligibleForKelly"] = len(dates) - 1 >= MIN_KELLY_RETURNS
    candidate["limitations"].append(
        "Incremental refresh preserved validated historical returns and appended "
        "only new observations."
    )
    return candidate


def _atomic_write(path: Path, document: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.parent.is_symlink():
        raise DynamicAssetError("unsafe_cache_path", "dynamic cache directory cannot be a symlink")
    payload = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except Exception:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def collect_us_asset(
    root: Path,
    symbol: str,
    *,
    start: date | None = None,
    end: date | None = None,
    basis_mode: str = "adjusted",
    cache_scope: str = "local",
    backfill: bool = False,
    today: date | None = None,
    yahoo_provider: Any | None = None,
    fdr_provider: Any | None = None,
    stooq_provider: Any | None = None,
    finviz_provider: Any | None = None,
) -> tuple[Path, dict[str, Any]]:
    """Discover, validate, collect, and atomically cache one US asset."""

    if basis_mode not in RETURN_BASIS_BY_MODE:
        raise DynamicAssetError("invalid_return_basis", "basis must be adjusted or price")
    require_yahoo_public_display_approval(cache_scope)
    requested = normalize_us_symbol(symbol)
    resolved_start, resolved_end = bounded_history_period(start, end, today=today)
    expected_basis = RETURN_BASIS_BY_MODE[basis_mode]
    path = dynamic_cache_path(root, requested, cache_scope)
    existing = _load_incremental_baseline(
        root,
        path,
        canonical_symbol=requested,
        expected_return_basis=expected_basis,
        requested_start=start,
        requested_end=resolved_end,
        backfill=backfill,
    )
    incremental_start = resolved_start
    if existing is not None:
        cached_end = date.fromisoformat(existing["dates"][-1])
        incremental_start = max(
            resolved_start,
            cached_end - timedelta(days=INCREMENTAL_OVERLAP_DAYS),
        )
    yahoo = yahoo_provider or YahooChartProvider()
    fdr = fdr_provider or FinanceDataReaderYahooProvider()
    stooq = stooq_provider or StooqCsvProvider()
    finviz = finviz_provider or FinvizChartProvider()

    try:
        metadata = yahoo.lookup(requested)
    except (ProviderUnavailable, ProviderResponseError) as error:
        raise DynamicAssetError("metadata_unavailable", str(error).lower()) from error
    canonical, asset_type = validate_us_metadata(requested, metadata)
    fetch_start = incremental_start
    if metadata.first_trade_date:
        first_trade = date.fromisoformat(metadata.first_trade_date)
        if first_trade > resolved_end:
            raise DynamicAssetError("insufficient_observations", "instrument had not yet traded")
        fetch_start = max(fetch_start, first_trade)

    series, failures = _fetch_primary_history(
        canonical_symbol=canonical,
        asset_type=asset_type,
        exchange=metadata.exchange_name,
        start=fetch_start,
        end=resolved_end,
        basis_mode=basis_mode,
        yahoo_provider=yahoo,
        fdr_provider=fdr,
        stooq_provider=stooq,
    )
    series = _trim_to_first_trade(series, metadata.first_trade_date)
    _validate_history_identity(
        series,
        canonical_symbol=canonical,
        expected_return_basis=expected_basis,
        start=fetch_start,
        end=resolved_end,
    )
    report = validate_price_series(series, as_of=resolved_end, freshness_days=10)
    if not report.accepted:
        raise DynamicAssetError("data_quality_rejected", "history failed core price checks")
    crosscheck = _independent_crosscheck(
        series,
        canonical_symbol=canonical,
        asset_type=asset_type,
        exchange=metadata.exchange_name,
        start=fetch_start,
        end=resolved_end,
        stooq_provider=stooq,
        finviz_provider=finviz,
    )
    if crosscheck["state"] == "mismatch":
        raise DynamicAssetError(
            "crosscheck_mismatch",
            "independent price-return cross-check rejected the candidate series",
        )
    generated_at = datetime.now(UTC).isoformat()
    document = build_dynamic_document(
        metadata=metadata,
        canonical_symbol=canonical,
        asset_type=asset_type,
        series=series,
        crosscheck=crosscheck,
        generated_at=generated_at,
        quality_state=report.status,
        fallback_failures=failures,
    )
    if existing is not None:
        document = _merge_incremental_document(
            existing,
            document,
            retain_from=resolved_start,
        )
    _validate_contract(root, document)
    canonical_path = dynamic_cache_path(root, canonical, cache_scope)
    if canonical_path != path:
        raise DynamicAssetError(
            "identity_path_mismatch",
            "provider canonical identity changed the derived cache path",
        )
    _atomic_write(path, document)
    return path, document
