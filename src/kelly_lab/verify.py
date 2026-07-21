from __future__ import annotations

import argparse
import hashlib
import json
import urllib.request
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .security import scan_public_files

SCHEMA_FILES = {
    "summary": "summary.schema.json",
    "catalog": "catalog.schema.json",
    "automation": "automation-status.schema.json",
    "asset": "asset.schema.json",
}
DATA_FILES = {
    "summary": "data/summary.json",
    "catalog": "data/catalog.json",
    "automation": "data/automation-status.json",
}
ALLOWED_STATES = {"published", "live_api", "stale", "degraded", "unavailable", "ruin"}
EXPECTED_SYMBOLS = {
    "005930.KS",
    "000660.KS",
    "^GSPC",
    "^NDX",
    "^SOX",
    "AAPL",
    "MSFT",
    "NVDA",
    "AMZN",
    "GOOGL",
    "META",
    "TSLA",
    "ORCL",
    "CRM",
    "NFLX",
    "SPCX",
    "AVGO",
    "TSM",
    "AMD",
    "ASML",
    "MU",
    "QCOM",
    "TXN",
    "ADI",
    "ARM",
    "MRVL",
    "MCHP",
    "AMAT",
    "LRCX",
    "KLAC",
    "TER",
    "LITE",
    "WDC",
    "SNDK",
    "STX",
    "SPY",
    "QQQ",
    "VTI",
    "IWM",
    "SMH",
    "SOXX",
    "GLD",
    "TLT",
    "SSO",
    "SDS",
    "QLD",
    "QID",
    "USD",
    "SSG",
    "USD/KRW",
}


def load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def validate_document(document: Any, schema: Any, label: str) -> None:
    errors = sorted(
        Draft202012Validator(schema, format_checker=FormatChecker()).iter_errors(document),
        key=lambda error: error.json_path,
    )
    if errors:
        joined = "; ".join(f"{error.json_path}: {error.message}" for error in errors[:10])
        raise ValueError(f"{label} contract invalid: {joined}")


def validate_local(root: Path) -> dict[str, str]:
    schemas = {
        name: load_json(root / "schemas" / filename) for name, filename in SCHEMA_FILES.items()
    }
    documents = {name: load_json(root / filename) for name, filename in DATA_FILES.items()}
    for name, document in documents.items():
        validate_document(document, schemas[name], name)

    catalog_assets = documents["catalog"]["assets"]
    if len(catalog_assets) != 50:
        raise ValueError(
            f"catalog must contain exactly 50 instruments, found {len(catalog_assets)}"
        )
    identifiers = [asset["id"] for asset in catalog_assets]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("catalog asset ids must be unique")
    symbols = {asset["symbol"] for asset in catalog_assets}
    if symbols != EXPECTED_SYMBOLS:
        missing = sorted(EXPECTED_SYMBOLS - symbols)
        extra = sorted(symbols - EXPECTED_SYMBOLS)
        raise ValueError(f"catalog universe mismatch: missing={missing}, extra={extra}")
    if any(symbol in symbols for symbol in {"UPRO", "SPXL", "TQQQ", "SOXL"}):
        raise ValueError("3x ETFs are excluded from the v1 catalog")

    for asset in catalog_assets:
        if asset["status"] not in ALLOWED_STATES:
            raise ValueError(f"unsupported state for {asset['id']}: {asset['status']}")
        asset_path = root / "data" / asset["dataPath"]
        if not asset_path.exists():
            raise ValueError(f"catalog data path is missing: {asset_path.relative_to(root)}")
        asset_document = load_json(asset_path)
        validate_document(
            asset_document,
            schemas["asset"],
            str(asset_path.relative_to(root)),
        )
        if asset_document["assetId"] != asset["id"]:
            raise ValueError(f"asset id mismatch for {asset['id']}")
        dates = asset_document["dates"]
        prices = asset_document["prices"]
        returns = asset_document["returns"]
        if not (len(dates) == len(prices) == len(returns)):
            raise ValueError(f"column length mismatch for {asset['id']}")
        if dates != sorted(set(dates)):
            raise ValueError(f"dates must be sorted and unique for {asset['id']}")
        if asset_document["state"] in {"published", "live_api", "stale", "degraded"}:
            if len(dates) < 2:
                raise ValueError(f"published asset has insufficient observations: {asset['id']}")
            if returns[0] is not None:
                raise ValueError(f"first return must be null for {asset['id']}")
            for index in range(1, len(prices)):
                expected = prices[index] / prices[index - 1] - 1.0
                actual = returns[index]
                if actual is None or abs(actual - expected) > 1e-10:
                    raise ValueError(f"price/return mismatch for {asset['id']} at {dates[index]}")

    findings = scan_public_files(root)
    if findings:
        raise ValueError(f"credential material detected in public files: {', '.join(findings)}")

    return {
        name: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for name, filename in DATA_FILES.items()
    }


def fetch_bytes(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "kelly-contract-verifier/1"})
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        if response.status != 200:
            raise ValueError(f"public readback failed: {url} returned {response.status}")
        return response.read()


def validate_live(root: Path, base_url: str, local_hashes: dict[str, str]) -> None:
    normalized = base_url.rstrip("/") + "/"
    for name, relative in DATA_FILES.items():
        remote = fetch_bytes(normalized + relative)
        digest = hashlib.sha256(remote).hexdigest()
        if digest != local_hashes[name]:
            raise ValueError(f"public hash mismatch for {relative}: {digest}")
        json.loads(remote)


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate static Kelly data contracts")
    parser.add_argument("--base-url")
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    hashes = validate_local(root)
    if args.base_url:
        validate_live(root, args.base_url, hashes)
    print(json.dumps({"status": "passed", "hashes": hashes}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
