from __future__ import annotations

import argparse
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .data_quality import check_historical_drift, returns_digest, validate_price_series
from .providers import TwelveDataProvider


def load(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def dump(path: Path, document: Any) -> None:
    path.write_text(
        json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n",
        encoding="utf-8",
    )


def normalized_asset(entry: dict[str, Any], series: Any, generated_at: str) -> dict[str, Any]:
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
            "provider": "twelve_data",
            "normalized": True,
            "rawRedistribution": False,
            "sourceUrl": series.source_url,
            "license": "External-display approval recorded by the operator",
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


def refresh(root: Path, catalog_path: Path, *, backfill: bool = False) -> int:
    provider = TwelveDataProvider()
    if not provider.available:
        raise RuntimeError("TWELVE_DATA_LICENSE_OR_KEY_UNAVAILABLE")
    catalog = load(catalog_path)
    generated_at = datetime.now(UTC).isoformat()
    start = date.today() - timedelta(days=366 * 20)
    end = date.today()
    staged: dict[Path, dict[str, Any]] = {}

    for entry in catalog["assets"]:
        if entry["provider"]["provider"] != "twelve_data":
            continue
        adjust = "all" if entry["returnBasis"] == "total_return_approximation" else "none"
        series = provider.history(
            entry["provider"]["symbol"],
            start,
            end,
            adjust=adjust,
        )
        report = validate_price_series(series, as_of=end, freshness_days=10)
        if not report.accepted:
            raise RuntimeError(f"DATA_QUALITY_REJECTED:{entry['id']}")
        target = root / "data" / entry["dataPath"]
        existing = load(target)
        if existing.get("state") in {"published", "stale", "degraded"} and not backfill:
            frozen_through = existing["dates"][-1]
            expected = returns_digest(existing["dates"], existing["prices"], through=frozen_through)
            check_historical_drift(
                series.dates,
                series.prices,
                frozen_through=frozen_through,
                expected_digest=expected,
            )
        document = normalized_asset(entry, series, generated_at)
        document["state"] = report.status
        staged[target] = document
        entry["status"] = report.status

    for path, document in staged.items():
        dump(path, document)
    catalog["generatedAt"] = generated_at
    catalog["state"] = (
        "degraded"
        if any(entry["status"] == "unavailable" for entry in catalog["assets"])
        else "published"
    )
    dump(root / "data/catalog.json", catalog)

    available = [entry for entry in catalog["assets"] if entry["status"] != "unavailable"]
    data_dates = [document["dataAsOf"] for document in staged.values()]
    summary_path = root / "data/summary.json"
    summary = load(summary_path)
    summary["generatedAt"] = generated_at
    summary["dataAsOf"] = max(data_dates) if data_dates else None
    summary["state"] = catalog["state"]
    summary["status"] = {
        "state": catalog["state"],
        "label": (
            "검증된 정적 데이터 일부 공개"
            if catalog["state"] == "degraded"
            else "검증된 정적 데이터 공개"
        ),
        "message": "KRX 공식 시계열은 별도 승인된 수집 경로가 준비될 때까지 unavailable입니다.",
    }
    summary["coverage"]["availableAssetCount"] = len(available)
    dump(summary_path, summary)

    automation_path = root / "data/automation-status.json"
    automation = load(automation_path)
    automation.update(
        {
            "state": catalog["state"],
            "generatedAt": generated_at,
            "dataAsOf": max(data_dates) if data_dates else None,
            "lastAttemptAt": generated_at,
            "lastSuccessAt": generated_at,
            "reasonCodes": (
                ["krx_official_source_unavailable"] if catalog["state"] == "degraded" else []
            ),
        }
    )
    automation["provider"].update({"configured": True, "rightsApproved": True})
    automation["publication"]["assetCount"] = len(available)
    dump(automation_path, automation)
    return len(staged)


def main() -> int:
    parser = argparse.ArgumentParser(description="Refresh licensed, normalized static histories")
    parser.add_argument("--catalog", type=Path, default=Path("config/catalog.json"))
    parser.add_argument("--backfill", action="store_true")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    catalog_path = args.catalog if args.catalog.is_absolute() else root / args.catalog
    count = refresh(root, catalog_path, backfill=args.backfill)
    print(f"refreshed {count} normalized series")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
