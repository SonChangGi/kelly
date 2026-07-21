from __future__ import annotations

import argparse
import copy
import json
import os
import tempfile
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .data_quality import validate_price_series
from .providers import (
    KrxOfficialApiProvider,
    NormalizedPriceSeries,
    ProviderResponseError,
    ProviderUnavailable,
    TwelveDataProvider,
)

DATA_BEARING_STATES = {"published", "live_api", "stale", "degraded"}
PUBLIC_FRESHNESS_DAYS = 10


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
    return str(provider or "twelve_data")


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

    provider_id = _provider_id(entry)
    license_label = (
        "Public-display approval recorded by the operator under KRX Open API terms"
        if provider_id == "krx"
        else "External-display approval recorded by the operator"
    )
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
            "normalized": True,
            "rawRedistribution": False,
            "sourceUrl": series.source_url,
            "license": license_label,
            "attribution": series.attribution,
            "cachedAt": generated_at,
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
    for day in overlap:
        old_price = float(old_rows[day])
        new_price = float(new_rows[day])
        tolerance = max(abs(old_price) * 1e-10, 1e-8)
        if abs(old_price - new_price) > tolerance:
            raise ValueError("HISTORICAL_DRIFT_BACKFILL_REQUIRED")

    merged = {str(day): float(price) for day, price in old_rows.items()}
    frozen_through = str(existing["dates"][-1])
    for day, price in new_rows.items():
        if day > frozen_through:
            merged[day] = float(price)
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


def refresh(
    root: Path,
    catalog_path: Path,
    *,
    backfill: bool = False,
    start: date | None = None,
    end: date | None = None,
    krx_provider: KrxOfficialApiProvider | None = None,
    twelve_provider: TwelveDataProvider | None = None,
) -> int:
    public_catalog = load(root / "data/catalog.json")
    config = load(catalog_path)
    config_by_id = {entry["id"]: entry for entry in config["assets"]}
    public_ids = {entry["id"] for entry in public_catalog["assets"]}
    if set(config_by_id) != public_ids:
        raise ValueError("CONFIG_PUBLIC_CATALOG_ID_MISMATCH")
    generated_at = datetime.now(UTC).isoformat()
    end = end or date.today()
    default_start = start or end - timedelta(days=round(365.2425 * 5))
    staged: dict[Path, dict[str, Any]] = {}
    refreshed_targets: set[Path] = set()
    failures: list[str] = []

    krx_entries = [
        entry
        for entry in public_catalog["assets"]
        if _provider_id(entry) == "krx" and entry["id"] in config_by_id
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
        reason = (
            "krx_public_display_rights_unconfirmed"
            if not krx_provider.rights_approved
            else "krx_api_key_unavailable"
        )
        failures.append(reason)
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

    twelve_provider = twelve_provider or TwelveDataProvider()
    twelve_entries = [
        entry
        for entry in public_catalog["assets"]
        if _provider_id(entry) == "twelve_data" and entry["id"] in config_by_id
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
            for document in staged.values():
                metadata = document["metadata"]
                if metadata.get("baseCurrency") == "USD" and document["assetId"] != fx_entry["id"]:
                    document["fx"] = fx_payload

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
        "label": "검증된 정적 데이터 일부 공개" if available else "시장 데이터 연결 전",
        "message": (
            f"공식 또는 공개표시 권한이 확인된 시계열 {len(available)}/50개를 제공합니다."
            if available
            else "공급자 권한과 서버 측 비밀키가 확인된 데이터만 게시합니다."
        ),
    }
    summary["coverage"]["availableAssetCount"] = len(available)
    for entity in summary.get("primaryEntities", []):
        if entity.get("id") == "kelly-allocation-lab":
            entity["state"] = state

    automation_path = root / "data/automation-status.json"
    automation = load(automation_path)
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
            "name": "mixed"
            if krx_entries and twelve_entries
            else ("krx" if krx_entries else "twelve_data"),
            "configured": bool(krx_provider.configured or twelve_provider.configured),
            "rightsApproved": bool(
                krx_provider.rights_approved and twelve_provider.rights_approved
            ),
            "providers": [
                {
                    "name": "krx",
                    "configured": krx_provider.configured,
                    "rightsApproved": krx_provider.rights_approved,
                },
                {
                    "name": "twelve_data",
                    "configured": twelve_provider.configured,
                    "rightsApproved": twelve_provider.rights_approved,
                },
            ],
        }
    )
    automation["publication"]["assetCount"] = len(available)
    if staged:
        automation["publication"]["latestPublishedAt"] = generated_at

    _preflight_generation(root, public_catalog, summary, automation, asset_documents)
    for path, document in staged.items():
        dump(path, document)
    dump(root / "data/catalog.json", public_catalog)
    dump(summary_path, summary)
    dump(automation_path, automation)
    return len(refreshed_targets)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh licensed, normalized static histories")
    parser.add_argument("--catalog", type=Path, default=Path("config/catalog.json"))
    parser.add_argument("--backfill", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat)
    parser.add_argument("--end", type=date.fromisoformat)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    catalog_path = args.catalog if args.catalog.is_absolute() else root / args.catalog
    count = refresh(
        root,
        catalog_path,
        backfill=args.backfill,
        start=args.start,
        end=args.end,
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
