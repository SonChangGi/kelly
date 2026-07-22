from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import statistics
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .data_quality import validate_price_series
from .free_providers import (
    FinanceDataReaderYahooProvider,
    FinvizChartProvider,
    FredDexkousProvider,
    StooqCsvProvider,
    YahooChartProvider,
)
from .providers import (
    KrxOfficialApiProvider,
    NormalizedPriceSeries,
    ProviderResponseError,
    ProviderUnavailable,
    TwelveDataProvider,
)

DATA_BEARING_STATES = {"published", "live_api", "stale", "degraded"}
PUBLIC_FRESHNESS_DAYS = 10
FREE_PROVIDER_IDS = {"yahoo_finance", "fred"}


def _yahoo_public_display_approved() -> bool:
    return os.getenv("YAHOO_PUBLIC_DISPLAY_APPROVED") == "true"


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, document: Any) -> None:
    payload = json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
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


def _provider_id(entry: dict[str, Any]) -> str:
    provider = entry.get("provider")
    if isinstance(provider, dict):
        return str(provider["provider"])
    return str(provider or "yahoo_finance")


def _source_identity(series: NormalizedPriceSeries) -> tuple[str, str, str]:
    name = series.provider.lower()
    if "financedatareader" in name:
        return (
            "yahoo_finance",
            "finance_data_reader",
            "Yahoo Finance research data retrieved through FinanceDataReader; "
            "no vendor license asserted",
        )
    if "yahoo" in name:
        return (
            "yahoo_finance",
            "native",
            "Yahoo Finance research data; no vendor license asserted",
        )
    if "stooq" in name:
        return ("stooq", "native", "Stooq research data; no vendor license asserted")
    if "fred" in name:
        return ("fred", "native", "FRED source notes and underlying series terms apply")
    if "korea exchange" in name or name == "krx":
        return ("krx", "native", "KRX Open API terms apply")
    if "twelve" in name:
        return ("twelve_data", "native", "External-display approval recorded by the operator")
    raise ValueError("UNRECOGNIZED_NORMALIZED_PROVIDER")


def _series_digest(series: NormalizedPriceSeries) -> str:
    payload = "\n".join(
        f"{day}:{float(price):.12g}" for day, price in zip(series.dates, series.prices, strict=True)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def normalized_asset(
    entry: dict[str, Any], series: NormalizedPriceSeries, generated_at: str
) -> dict[str, Any]:
    prices = list(series.prices)
    returns = [None] + [prices[index] / prices[index - 1] - 1.0 for index in range(1, len(prices))]
    metadata: dict[str, Any] = {
        "symbol": entry["symbol"],
        "assetType": entry["assetType"],
        "exchange": entry["exchange"],
        "timezone": entry["timezone"],
        "returnBasis": entry["returnBasis"],
    }
    if entry["assetType"] == "fx":
        metadata.update({"baseCurrency": "USD", "quoteCurrency": "KRW"})
    else:
        metadata["baseCurrency"] = entry["currency"]

    provider_id, adapter, license_label = _source_identity(series)
    return {
        "schemaVersion": 1,
        "contract": "kelly-asset-history",
        "state": "published",
        "assetId": entry["id"],
        "generatedAt": generated_at,
        "dataAsOf": series.dates[-1],
        "metadata": metadata,
        "dates": list(series.dates),
        "prices": prices,
        "returns": returns,
        "source": {
            "provider": provider_id,
            "adapter": adapter,
            "normalized": True,
            "rawRedistribution": False,
            "sourceUrl": series.source_url,
            "license": license_label,
            "attribution": series.attribution,
            "cachedAt": generated_at,
            "contentDigest": _series_digest(series),
        },
        "quality": {
            "observationCount": len(series.dates),
            "eligibleForKelly": len(series.dates) - 1 >= 60,
            "minimumKellyObservations": 60,
            "crossCheck": {
                "provider": "none",
                "state": "not_applicable",
                "commonObservations": 0,
                "windowStart": None,
                "windowEnd": None,
                "medianAbsReturnDifference": None,
                "p99AbsReturnDifference": None,
            },
        },
        "limitations": [
            (
                "Adjusted daily close is a total-return approximation; "
                "taxes, slippage, and financing are excluded."
            )
            if entry["returnBasis"] == "total_return_approximation"
            else (
                "Daily price-return series; dividends, taxes, slippage, and financing are excluded."
            )
        ],
    }


def merge_incremental(
    existing: dict[str, Any], series: NormalizedPriceSeries, *, backfill: bool
) -> NormalizedPriceSeries:
    """Preserve frozen rows and append only after validating the fetched overlap."""
    if backfill or existing.get("state") not in {"published", "stale", "degraded"}:
        return series

    old_rows = dict(zip(existing.get("dates", []), existing.get("prices", []), strict=True))
    new_rows = dict(zip(series.dates, series.prices, strict=True))
    overlap = sorted(set(old_rows) & set(new_rows))
    if len(overlap) < 2:
        raise ValueError("HISTORICAL_OVERLAP_INSUFFICIENT_BACKFILL_REQUIRED")
    fetched_start, fetched_end = min(new_rows), max(new_rows)
    expected_overlap_dates = {day for day in old_rows if fetched_start <= day <= fetched_end}
    if not expected_overlap_dates.issubset(new_rows):
        raise ValueError("HISTORICAL_OBSERVATION_REMOVED_BACKFILL_REQUIRED")
    if series.return_basis == "total_return_approximation":
        for previous, current in zip(overlap, overlap[1:], strict=False):
            old_return = float(old_rows[current]) / float(old_rows[previous]) - 1.0
            new_return = float(new_rows[current]) / float(new_rows[previous]) - 1.0
            if abs(old_return - new_return) > 2e-6:
                raise ValueError("HISTORICAL_DRIFT_BACKFILL_REQUIRED")
    else:
        for day in overlap:
            old_price = float(old_rows[day])
            new_price = float(new_rows[day])
            tolerance = max(abs(old_price) * 1e-10, 1e-8)
            if abs(old_price - new_price) > tolerance:
                raise ValueError("HISTORICAL_DRIFT_BACKFILL_REQUIRED")

    merged = {str(day): float(price) for day, price in old_rows.items()}
    frozen_through = str(existing["dates"][-1])
    scale = 1.0
    if series.return_basis == "total_return_approximation":
        anchor = overlap[-1]
        scale = float(old_rows[anchor]) / float(new_rows[anchor])
    for day, price in new_rows.items():
        if day > frozen_through:
            merged[day] = float(price) * scale
    ordered = sorted(merged.items())
    return replace(
        series,
        dates=tuple(day for day, _ in ordered),
        prices=tuple(price for _, price in ordered),
    )


def _fetch_start(target: Path, default_start: date, *, backfill: bool) -> date:
    if backfill or not target.exists():
        return default_start
    existing = load(target)
    dates = existing.get("dates") or []
    if existing.get("state") not in {"published", "stale", "degraded"} or not dates:
        return default_start
    return max(default_start, date.fromisoformat(dates[-1]) - timedelta(days=35))


def _reason_code(error: Exception) -> str:
    """Return a stable public code without serializing exception details or URLs."""

    value = str(error).strip().upper()
    stable_prefixes = (
        "DATA_QUALITY_REJECTED:",
        "HISTORICAL_",
        "KRX_",
        "TWELVE_DATA_",
        "YAHOO_",
        "FINANCE_DATA_READER_",
        "STOOQ_",
        "FRED_",
        "FINVIZ_",
        "INDEPENDENT_",
    )
    if value.startswith(stable_prefixes) and all(
        character.isalnum() or character in {"_", ":", "-"} for character in value
    ):
        return value.lower().replace(":", "_")[:120]
    if isinstance(error, ProviderUnavailable):
        return "provider_unavailable"
    if isinstance(error, ProviderResponseError):
        return "provider_response_invalid"
    return "refresh_failed"


def _unavailable_document(
    existing: dict[str, Any],
    *,
    generated_at: str,
    reason: str,
) -> dict[str, Any]:
    """Remove observations when public-display rights are not currently approved."""

    document = copy.deepcopy(existing)
    document.update(
        {
            "state": "unavailable",
            "generatedAt": generated_at,
            "dataAsOf": None,
            "dates": [],
            "prices": [],
            "returns": [],
            "source": {
                "provider": "none",
                "normalized": True,
                "rawRedistribution": False,
                "license": "No public-display approval recorded",
                "attribution": "No market observations published",
                "cachedAt": None,
            },
            "limitations": [reason],
        }
    )
    document.pop("fx", None)
    return document


def _preserved_failure_document(
    existing: dict[str, Any],
    *,
    generated_at: str,
    as_of: date,
    reason: str,
) -> dict[str, Any]:
    """Keep a rights-approved last-good series but disclose a failed refresh."""

    if existing.get("state") not in DATA_BEARING_STATES or not existing.get("dates"):
        return _unavailable_document(existing, generated_at=generated_at, reason=reason)
    document = copy.deepcopy(existing)
    last_date = date.fromisoformat(str(document["dates"][-1]))
    document["state"] = "stale" if (as_of - last_date).days > PUBLIC_FRESHNESS_DAYS else "degraded"
    document["generatedAt"] = generated_at
    limitations = [str(value) for value in document.get("limitations", [])]
    quality = document.get("quality")
    cross_check = quality.get("crossCheck") if isinstance(quality, dict) else None
    if isinstance(cross_check, dict) and not {
        "windowStart",
        "windowEnd",
    }.issubset(cross_check):
        cross_check.update(
            {
                "state": "unavailable",
                "commonObservations": 0,
                "windowStart": None,
                "windowEnd": None,
                "medianAbsReturnDifference": None,
                "p99AbsReturnDifference": None,
            }
        )
        if "independent_crosscheck_unavailable" not in limitations:
            limitations.append("independent_crosscheck_unavailable")
    if reason not in limitations:
        limitations.append(reason)
    document["limitations"] = limitations
    return document


def _preflight_generation(
    root: Path,
    catalog: dict[str, Any],
    summary: dict[str, Any],
    automation: dict[str, Any],
    asset_documents: dict[Path, dict[str, Any]],
) -> None:
    """Validate the complete candidate generation before replacing public files."""
    schemas_root = root / "schemas"
    if not schemas_root.is_dir():
        return
    from .verify import _validate_asset_against_catalog, validate_document

    schemas = {
        name: load(schemas_root / filename)
        for name, filename in {
            "catalog": "catalog.schema.json",
            "summary": "summary.schema.json",
            "automation": "automation-status.schema.json",
            "asset": "asset.schema.json",
        }.items()
    }
    validate_document(catalog, schemas["catalog"], "candidate catalog")
    validate_document(summary, schemas["summary"], "candidate summary")
    validate_document(automation, schemas["automation"], "candidate automation")
    for entry in catalog["assets"]:
        target = root / "data" / entry["dataPath"]
        document = asset_documents[target]
        validate_document(document, schemas["asset"], f"candidate {entry['id']}")
        _validate_asset_against_catalog(entry, document)


def _trim_to_identity_floor(
    series: NormalizedPriceSeries, floor: str | None
) -> NormalizedPriceSeries:
    if not floor:
        return series
    rows = [
        (day, price) for day, price in zip(series.dates, series.prices, strict=True) if day >= floor
    ]
    if len(rows) < 2:
        raise ProviderResponseError("SERIES_IDENTITY_FLOOR_INSUFFICIENT")
    return replace(
        series,
        dates=tuple(day for day, _ in rows),
        prices=tuple(float(price) for _, price in rows),
    )


def _free_provider_chain(
    entry: dict[str, Any],
    *,
    yahoo_provider: Any,
    fdr_provider: Any,
    stooq_provider: Any,
    fred_provider: Any,
    yahoo_allowed: bool = True,
) -> list[tuple[str, Any]]:
    if entry["assetType"] == "fx":
        chain = [
            ("fred", fred_provider),
            ("yahoo_finance", yahoo_provider),
            ("stooq", stooq_provider),
        ]
    elif entry["returnBasis"] == "total_return_approximation":
        chain = [
            ("yahoo_finance", yahoo_provider),
            ("finance_data_reader", fdr_provider),
        ]
    else:
        chain = [("yahoo_finance", yahoo_provider), ("stooq", stooq_provider)]
    if yahoo_allowed:
        return chain
    return [
        (name, provider)
        for name, provider in chain
        if name not in {"yahoo_finance", "finance_data_reader"}
    ]


def _fetch_free_series(
    entry: dict[str, Any],
    start: date,
    end: date,
    *,
    yahoo_provider: Any,
    fdr_provider: Any,
    stooq_provider: Any,
    fred_provider: Any,
    yahoo_allowed: bool = True,
) -> tuple[NormalizedPriceSeries, list[str]]:
    failures: list[str] = []
    adjust = "all" if entry["returnBasis"] == "total_return_approximation" else "none"
    for _name, provider in _free_provider_chain(
        entry,
        yahoo_provider=yahoo_provider,
        fdr_provider=fdr_provider,
        stooq_provider=stooq_provider,
        fred_provider=fred_provider,
        yahoo_allowed=yahoo_allowed,
    ):
        try:
            series = provider.history(
                entry["symbol"],
                start,
                end,
                adjust=adjust,
                exchange=entry["exchange"],
                currency=entry["currency"],
                asset_type=entry["assetType"],
            )
            series = _trim_to_identity_floor(series, entry.get("seriesStartFloor"))
            if series.return_basis != entry["returnBasis"]:
                raise ProviderResponseError("RETURN_BASIS_MISMATCH")
            return series, failures
        except (ProviderUnavailable, ProviderResponseError) as error:
            failures.append(_reason_code(error))
    raise ProviderUnavailable(failures[-1] if failures else "FREE_PROVIDER_CHAIN_EXHAUSTED")


def _return_difference(
    primary: NormalizedPriceSeries,
    secondary: NormalizedPriceSeries,
    *,
    median_tolerance: float = 0.002,
    p99_tolerance: float = 0.08,
    max_median_level_difference: float | None = None,
) -> dict[str, Any]:
    first = dict(zip(primary.dates, primary.prices, strict=True))
    second = dict(zip(secondary.dates, secondary.prices, strict=True))
    common = sorted(set(first) & set(second))
    window_start = common[0] if common else None
    window_end = common[-1] if common else None
    if len(common) < 21:
        return {
            "state": "insufficient",
            "commonObservations": max(0, len(common) - 1),
            "windowStart": window_start,
            "windowEnd": window_end,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
        }
    differences = []
    level_differences = []
    for previous, current in zip(common, common[1:], strict=False):
        primary_return = float(first[current]) / float(first[previous]) - 1.0
        secondary_return = float(second[current]) / float(second[previous]) - 1.0
        differences.append(abs(primary_return - secondary_return))
        level_differences.append(abs(float(first[current]) / float(second[current]) - 1.0))
    ordered = sorted(differences)
    p99_index = min(len(ordered) - 1, int((len(ordered) - 1) * 0.99))
    median = float(statistics.median(ordered))
    p99 = float(ordered[p99_index])
    level_consistent = (
        max_median_level_difference is None
        or statistics.median(level_differences) <= max_median_level_difference
    )
    state = (
        "passed"
        if median <= median_tolerance and p99 <= p99_tolerance and level_consistent
        else "mismatch"
    )
    return {
        "state": state,
        "commonObservations": len(differences),
        "windowStart": window_start,
        "windowEnd": window_end,
        "medianAbsReturnDifference": median,
        "p99AbsReturnDifference": p99,
    }


def _cross_check(
    entry: dict[str, Any],
    series: NormalizedPriceSeries,
    start: date,
    end: date,
    *,
    yahoo_provider: Any,
    stooq_provider: Any,
    finviz_provider: Any,
    fred_provider: Any,
    disabled_providers: set[str],
    yahoo_allowed: bool = True,
) -> dict[str, Any]:
    source_id, _, _ = _source_identity(series)
    if entry["assetType"] == "fx" and source_id == "fred":
        provider_id, provider = "yahoo_finance", yahoo_provider
    elif entry["assetType"] == "fx":
        provider_id, provider = "fred", fred_provider
    elif entry["assetType"] in {"equity", "etf"}:
        provider_id, provider = "finviz", finviz_provider
    elif source_id != "stooq":
        provider_id, provider = "stooq", stooq_provider
    else:
        return {
            "provider": "none",
            "state": "not_applicable",
            "commonObservations": 0,
            "windowStart": None,
            "windowEnd": None,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
        }
    if provider_id == "yahoo_finance" and not yahoo_allowed:
        return {
            "provider": provider_id,
            "state": "unavailable",
            "commonObservations": 0,
            "windowStart": None,
            "windowEnd": None,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
        }
    if provider_id in disabled_providers:
        return {
            "provider": provider_id,
            "state": "unavailable",
            "commonObservations": 0,
            "windowStart": None,
            "windowEnd": None,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
        }
    try:
        secondary = provider.history(
            entry["symbol"],
            start,
            end,
            adjust="none",
            exchange=entry["exchange"],
            currency=entry["currency"],
            asset_type=entry["assetType"],
        )
        secondary = _trim_to_identity_floor(secondary, entry.get("seriesStartFloor"))
        if entry["assetType"] == "fx":
            # FRED is a New York noon fixing while Yahoo is a tradable-market
            # snapshot. Their same-date returns can differ modestly even when
            # direction and units agree, so retain a bounded return tolerance
            # plus a strict level-ratio guard against inversion or 100x errors.
            result = _return_difference(
                series,
                secondary,
                median_tolerance=0.012,
                p99_tolerance=0.06,
                max_median_level_difference=0.03,
            )
        else:
            result = _return_difference(series, secondary)
        return {"provider": provider_id, **result}
    except (ProviderUnavailable, ProviderResponseError) as error:
        if _reason_code(error) in {
            "stooq_html_challenge",
            "stooq_access_unavailable",
            "stooq_rate_limited",
            "finviz_access_unavailable",
            "finviz_rate_limited",
            "fred_access_unavailable",
            "fred_rate_limited",
        }:
            disabled_providers.add(provider_id)
        return {
            "provider": provider_id,
            "state": "unavailable",
            "commonObservations": 0,
            "windowStart": None,
            "windowEnd": None,
            "medianAbsReturnDifference": None,
            "p99AbsReturnDifference": None,
        }


def refresh(
    root: Path,
    catalog_path: Path,
    *,
    backfill: bool = False,
    start: date | None = None,
    end: date | None = None,
    asset_ids: set[str] | None = None,
    krx_provider: KrxOfficialApiProvider | None = None,
    twelve_provider: TwelveDataProvider | None = None,
    yahoo_provider: YahooChartProvider | None = None,
    fdr_provider: FinanceDataReaderYahooProvider | None = None,
    stooq_provider: StooqCsvProvider | None = None,
    fred_provider: FredDexkousProvider | None = None,
    finviz_provider: FinvizChartProvider | None = None,
) -> int:
    public_catalog = load(root / "data/catalog.json")
    config = load(catalog_path)
    config_by_id = {entry["id"]: entry for entry in config["assets"]}
    public_ids = {entry["id"] for entry in public_catalog["assets"]}
    if set(config_by_id) != public_ids:
        raise ValueError("CONFIG_PUBLIC_CATALOG_ID_MISMATCH")
    selected_ids = set(asset_ids) if asset_ids else public_ids
    unknown_ids = selected_ids - public_ids
    if unknown_ids:
        raise ValueError(f"UNKNOWN_ASSET_ID:{sorted(unknown_ids)[0]}")
    yahoo_display_approved = _yahoo_public_display_approved()
    selected_yahoo_entries = [
        entry
        for entry in public_catalog["assets"]
        if entry["id"] in selected_ids and _provider_id(entry) == "yahoo_finance"
    ]
    if selected_yahoo_entries and not yahoo_display_approved:
        raise ProviderUnavailable("YAHOO_PUBLIC_DISPLAY_RIGHTS_UNCONFIRMED")
    generated_at = datetime.now(UTC).isoformat()
    end = end or date.today()
    default_start = start or end - timedelta(days=round(365.2425 * 5))
    staged: dict[Path, dict[str, Any]] = {}
    refreshed_targets: set[Path] = set()
    failures: list[str] = []
    crosscheck_disabled: set[str] = set()

    krx_entries = [
        entry
        for entry in public_catalog["assets"]
        if _provider_id(entry) == "krx" and entry["id"] in selected_ids
    ]
    krx_provider = krx_provider or KrxOfficialApiProvider(cache_dir=root / "var/krx-selected-close")
    if krx_entries and krx_provider.available:
        try:
            krx_start = min(
                _fetch_start(root / "data" / entry["dataPath"], default_start, backfill=backfill)
                for entry in krx_entries
            )
            fetched = krx_provider.history_many(
                [entry["provider"]["symbol"] for entry in krx_entries], krx_start, end
            )
            for entry in krx_entries:
                symbol = entry["provider"]["symbol"]
                target = root / "data" / entry["dataPath"]
                series = merge_incremental(load(target), fetched[symbol], backfill=backfill)
                report = validate_price_series(series, as_of=end, freshness_days=10)
                if not report.accepted:
                    raise RuntimeError(f"DATA_QUALITY_REJECTED:{entry['id']}")
                document = normalized_asset(entry, series, generated_at)
                document["state"] = report.status
                staged[target] = document
                refreshed_targets.add(target)
        except Exception as error:  # keep rights-approved last-good observations only
            reason = _reason_code(error)
            failures.append(reason)
            for entry in krx_entries:
                target = root / "data" / entry["dataPath"]
                if target not in refreshed_targets:
                    staged[target] = _preserved_failure_document(
                        load(target),
                        generated_at=generated_at,
                        as_of=end,
                        reason=reason,
                    )
    elif krx_entries:
        krx_reasons = []
        if not krx_provider.rights_approved:
            krx_reasons.append("krx_public_display_rights_unconfirmed")
        if not krx_provider.configured:
            krx_reasons.append("krx_api_key_unavailable")
        failures.extend(krx_reasons)
        reason = krx_reasons[0]
        for entry in krx_entries:
            target = root / "data" / entry["dataPath"]
            existing = load(target)
            staged[target] = (
                _unavailable_document(existing, generated_at=generated_at, reason=reason)
                if not krx_provider.rights_approved
                else _preserved_failure_document(
                    existing,
                    generated_at=generated_at,
                    as_of=end,
                    reason=reason,
                )
            )

    yahoo_provider = yahoo_provider or YahooChartProvider()
    fdr_provider = fdr_provider or FinanceDataReaderYahooProvider()
    stooq_provider = stooq_provider or StooqCsvProvider()
    fred_provider = fred_provider or FredDexkousProvider()
    finviz_provider = finviz_provider or FinvizChartProvider()
    free_entries = [
        entry
        for entry in public_catalog["assets"]
        if _provider_id(entry) in FREE_PROVIDER_IDS and entry["id"] in selected_ids
    ]
    for entry in free_entries:
        target = root / "data" / entry["dataPath"]
        try:
            fetch_start = _fetch_start(target, default_start, backfill=backfill)
            floor = entry.get("seriesStartFloor")
            if floor:
                fetch_start = max(fetch_start, date.fromisoformat(floor))
            series, adapter_failures = _fetch_free_series(
                entry,
                fetch_start,
                end,
                yahoo_provider=yahoo_provider,
                fdr_provider=fdr_provider,
                stooq_provider=stooq_provider,
                fred_provider=fred_provider,
                yahoo_allowed=yahoo_display_approved,
            )
            series = merge_incremental(load(target), series, backfill=backfill)
            report = validate_price_series(series, as_of=end, freshness_days=10)
            if not report.accepted:
                raise RuntimeError(f"DATA_QUALITY_REJECTED:{entry['id']}")
            cross_check = _cross_check(
                entry,
                series,
                fetch_start,
                end,
                yahoo_provider=yahoo_provider,
                stooq_provider=stooq_provider,
                finviz_provider=finviz_provider,
                fred_provider=fred_provider,
                disabled_providers=crosscheck_disabled,
                yahoo_allowed=yahoo_display_approved,
            )
            if cross_check["state"] == "mismatch":
                raise ProviderResponseError("INDEPENDENT_SOURCE_MISMATCH")
            document = normalized_asset(entry, series, generated_at)
            document["state"] = report.status
            document["quality"] = {
                "observationCount": len(series.dates),
                "eligibleForKelly": len(series.dates) - 1 >= 60,
                "minimumKellyObservations": 60,
                "crossCheck": cross_check,
            }
            if adapter_failures:
                document["limitations"].append("primary_adapter_failed_before_same-basis_fallback")
            if cross_check["state"] != "passed":
                document["limitations"].append(f"independent_crosscheck_{cross_check['state']}")
            staged[target] = document
            refreshed_targets.add(target)
        except Exception as error:  # preserve each last-good free-source series independently
            reason = _reason_code(error)
            failures.append(reason)
            staged[target] = _preserved_failure_document(
                load(target),
                generated_at=generated_at,
                as_of=end,
                reason=reason,
            )

    twelve_provider = twelve_provider or TwelveDataProvider()
    twelve_entries = [
        entry
        for entry in public_catalog["assets"]
        if _provider_id(entry) == "twelve_data" and entry["id"] in selected_ids
    ]
    if twelve_entries and twelve_provider.available:
        for entry in twelve_entries:
            target = root / "data" / entry["dataPath"]
            try:
                fetch_start = _fetch_start(target, default_start, backfill=backfill)
                adjust = "all" if entry["returnBasis"] == "total_return_approximation" else "none"
                series = twelve_provider.history(
                    entry["provider"]["symbol"],
                    fetch_start,
                    end,
                    adjust=adjust,
                    exchange=entry["provider"]["exchange"],
                    currency=entry["currency"],
                    asset_type=entry["assetType"],
                )
                series = merge_incremental(load(target), series, backfill=backfill)
                report = validate_price_series(series, as_of=end, freshness_days=10)
                if not report.accepted:
                    raise RuntimeError(f"DATA_QUALITY_REJECTED:{entry['id']}")
                document = normalized_asset(entry, series, generated_at)
                document["state"] = report.status
                staged[target] = document
                refreshed_targets.add(target)
            except Exception as error:  # keep other assets publishable
                reason = _reason_code(error)
                failures.append(reason)
                staged[target] = _preserved_failure_document(
                    load(target),
                    generated_at=generated_at,
                    as_of=end,
                    reason=reason,
                )
    elif twelve_entries:
        reason = "twelve_data_rights_or_key_unavailable"
        failures.append(reason)
        for entry in twelve_entries:
            target = root / "data" / entry["dataPath"]
            existing = load(target)
            staged[target] = (
                _unavailable_document(existing, generated_at=generated_at, reason=reason)
                if not twelve_provider.rights_approved
                else _preserved_failure_document(
                    existing,
                    generated_at=generated_at,
                    as_of=end,
                    reason=reason,
                )
            )

    fx_entry = next(
        (entry for entry in public_catalog["assets"] if entry["assetType"] == "fx"),
        None,
    )
    if fx_entry is not None:
        fx_target = root / "data" / fx_entry["dataPath"]
        fx_document = staged.get(fx_target)
        if fx_document is None and fx_target.exists():
            candidate = load(fx_target)
            if candidate.get("state") in {"published", "live_api", "stale", "degraded"}:
                fx_document = candidate
        if fx_document is not None:
            fx_payload = {
                "pair": "USD/KRW",
                "dates": fx_document["dates"],
                "rates": fx_document["prices"],
                "maxStalenessDays": 5,
            }
            fx_changed = fx_target in refreshed_targets
            fx_consumers = (
                public_catalog["assets"]
                if fx_changed
                else [
                    entry
                    for entry in public_catalog["assets"]
                    if root / "data" / entry["dataPath"] in staged
                ]
            )
            for entry in fx_consumers:
                target = root / "data" / entry["dataPath"]
                document = staged.get(target)
                if document is None and entry["currency"] == "USD" and target.exists():
                    document = copy.deepcopy(load(target))
                if document is None:
                    continue
                metadata = document["metadata"]
                if metadata.get("baseCurrency") == "USD" and document["assetId"] != fx_entry["id"]:
                    document["fx"] = fx_payload
                    staged[target] = document

    asset_documents: dict[Path, dict[str, Any]] = {}
    for entry in public_catalog["assets"]:
        target = root / "data" / entry["dataPath"]
        document = staged[target] if target in staged else load(target)
        asset_documents[target] = document
        entry["status"] = document.get("state", "unavailable")
        dates = document.get("dates") or []
        entry["availableFrom"] = dates[0] if dates else None
        entry["availableTo"] = dates[-1] if dates else None
    available = [
        entry
        for entry in public_catalog["assets"]
        if entry["status"] in {"published", "live_api", "stale", "degraded"}
    ]
    data_dates = [
        asset_documents[root / "data" / entry["dataPath"]]["dataAsOf"] for entry in available
    ]
    state = (
        "published"
        if len(available) == len(public_catalog["assets"])
        and all(entry["status"] == "published" for entry in available)
        else ("degraded" if available else "unavailable")
    )
    public_catalog.update({"generatedAt": generated_at, "state": state})

    summary_path = root / "data/summary.json"
    summary = load(summary_path)
    summary.update(
        {
            "generatedAt": generated_at,
            "dataAsOf": max(data_dates) if data_dates else None,
            "state": state,
        }
    )
    summary["status"] = {
        "state": state,
        "label": "검증된 연구 데이터 공개" if available else "시장 데이터 연결 전",
        "message": (
            f"출처와 수익률 기준을 검증한 일별 시계열 {len(available)}/50개를 제공합니다."
            if available
            else "무료 연구 소스와 KRX 공식 API 수집을 기다리고 있습니다."
        ),
    }
    summary["coverage"]["availableAssetCount"] = len(available)
    for entity in summary.get("primaryEntities", []):
        if entity.get("id") == "kelly-allocation-lab":
            entity["state"] = state

    automation_path = root / "data/automation-status.json"
    automation = load(automation_path)
    selected_krx = any(
        _provider_id(entry) == "krx"
        for entry in public_catalog["assets"]
        if entry["id"] in selected_ids
    )
    if not selected_krx:
        unavailable_krx = any(
            _provider_id(entry) == "krx" and entry["status"] == "unavailable"
            for entry in public_catalog["assets"]
        )
        if unavailable_krx:
            if not krx_provider.configured:
                failures.append("krx_api_key_unavailable")
            if not krx_provider.rights_approved:
                failures.append("krx_public_display_rights_unconfirmed")
    automation.update(
        {
            "state": state,
            "generatedAt": generated_at,
            "dataAsOf": max(data_dates) if data_dates else None,
            "lastAttemptAt": generated_at,
            "lastSuccessAt": (
                generated_at if refreshed_targets else automation.get("lastSuccessAt")
            ),
            "reasonCodes": sorted(set(failures)),
        }
    )
    automation["provider"].update(
        {
            "name": "mixed",
            "configured": True,
            "rightsApproved": False,
            "providers": [
                {
                    "name": "krx",
                    "configured": krx_provider.configured,
                    "rightsApproved": krx_provider.rights_approved,
                },
                {
                    "name": "yahoo_finance",
                    "configured": True,
                    "rightsApproved": yahoo_display_approved,
                },
                {
                    "name": "finance_data_reader",
                    "configured": True,
                    "rightsApproved": yahoo_display_approved,
                },
                {
                    "name": "stooq",
                    "configured": True,
                    "rightsApproved": False,
                },
                {
                    "name": "fred",
                    "configured": True,
                    "rightsApproved": False,
                },
                {
                    "name": "finviz",
                    "configured": True,
                    "rightsApproved": False,
                },
            ],
        }
    )
    automation["publication"]["assetCount"] = len(available)
    if refreshed_targets:
        automation["publication"]["latestPublishedAt"] = generated_at

    _preflight_generation(root, public_catalog, summary, automation, asset_documents)
    for path, document in staged.items():
        dump(path, document)
    dump(root / "data/catalog.json", public_catalog)
    dump(summary_path, summary)
    dump(automation_path, automation)
    return len(refreshed_targets)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh validated, normalized research histories")
    parser.add_argument("--catalog", type=Path, default=Path("config/catalog.json"))
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    parser.add_argument(
        "--asset-id",
        action="append",
        dest="asset_ids",
        help="Refresh only this catalog asset ID; repeat for multiple assets",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    catalog_path = args.catalog if args.catalog.is_absolute() else root / args.catalog
    count = refresh(
        root,
        catalog_path,
        backfill=args.backfill,
        start=args.start,
        end=args.end,
        asset_ids=set(args.asset_ids) if args.asset_ids else None,
    )
    print(f"refreshed {count} normalized series")
    automation = load(root / "data/automation-status.json")
    expected_unavailable = {
        "krx_api_key_unavailable",
        "krx_public_display_rights_unconfirmed",
        "twelve_data_rights_or_key_unavailable",
    }
    unexpected_failures = set(automation["reasonCodes"]) - expected_unavailable
    return 0 if count and not unexpected_failures else 1


if __name__ == "__main__":
    raise SystemExit(main())
