from __future__ import annotations

import argparse
import bisect
import hashlib
import json
import math
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from .security import scan_public_files

SCHEMA_FILES = {
    "summary": "summary.schema.json",
    "catalog": "catalog.schema.json",
    "automation": "automation-status.schema.json",
    "asset": "asset.schema.json",
    "price_series": "kelly-price-series.schema.json",
    "runtime": "runtime.schema.json",
    "provider_config": "provider-catalog-config.schema.json",
}
DATA_FILES = {
    "summary": "data/summary.json",
    "catalog": "data/catalog.json",
    "automation": "data/automation-status.json",
}
RUNTIME_FILE = "data/runtime.json"
PROVIDER_CONFIG_FILE = "config/catalog.json"
ALLOWED_STATES = {"published", "live_api", "stale", "degraded", "unavailable", "ruin"}
DATA_BEARING_STATES = {"published", "live_api", "stale", "degraded"}
FX_MAX_STALENESS_DAYS = 5
LIVE_READBACK_ATTEMPTS = 4
LIVE_READBACK_DELAY_SECONDS = 1.0
EXPECTED_LEVERAGED_PRODUCTS = {
    "^GSPC": {"long2x": "etf-sso", "inverse2x": "etf-sds"},
    "SPY": {"long2x": "etf-sso", "inverse2x": "etf-sds"},
    "^NDX": {"long2x": "etf-qld", "inverse2x": "etf-qid"},
    "QQQ": {"long2x": "etf-qld", "inverse2x": "etf-qid"},
}
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


def validate_runtime_config(document: dict[str, Any], schema: dict[str, Any]) -> None:
    """Validate the only browser-consumed runtime setting before publication."""

    validate_document(document, schema, "runtime")
    value = document["workerBaseUrl"]
    if value is None:
        return
    parsed = urllib.parse.urlsplit(value)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "runtime workerBaseUrl must be credential-free HTTPS without query or fragment"
        )


def validate_provider_config(
    config: dict[str, Any],
    schema: dict[str, Any],
    public_catalog: dict[str, Any],
) -> None:
    """Keep the refresh allowlist and public discovery catalog atomic."""

    validate_document(config, schema, "provider config")
    if config["catalogVersion"] != public_catalog["catalogVersion"]:
        raise ValueError("provider/public catalog version mismatch")
    config_assets = config["assets"]
    identifiers = [asset["id"] for asset in config_assets]
    if len(set(identifiers)) != len(identifiers):
        raise ValueError("provider config asset ids must be unique")
    config_by_id = {asset["id"]: asset for asset in config_assets}
    public_by_id = {asset["id"]: asset for asset in public_catalog["assets"]}
    if set(config_by_id) != set(public_by_id):
        raise ValueError("provider/public catalog id mismatch")

    for asset_id, public in public_by_id.items():
        configured = config_by_id[asset_id]
        expected_provider = configured.get("provider", config["defaultProvider"])
        expected = {
            "symbol": configured["symbol"],
            "provider": expected_provider,
            "providerSymbol": configured["providerSymbol"],
            "providerExchange": configured["providerExchange"],
            "returnBasis": configured["returnBasis"],
            "leveragedProducts": configured.get("leveragedProducts"),
            "seriesStartFloor": configured.get("seriesStartFloor"),
        }
        actual = {
            "symbol": public["symbol"],
            "provider": public["provider"]["provider"],
            "providerSymbol": public["provider"]["symbol"],
            "providerExchange": public["provider"]["exchange"],
            "returnBasis": public["returnBasis"],
            "leveragedProducts": public.get("leveragedProducts"),
            "seriesStartFloor": public.get("seriesStartFloor"),
        }
        if actual != expected:
            raise ValueError(
                f"provider/public catalog projection mismatch for {asset_id}: "
                f"expected {expected}, found {actual}"
            )


def _expected_base_currency(asset: dict[str, Any]) -> str:
    if asset["assetType"] == "fx":
        return asset["symbol"].split("/", maxsplit=1)[0]
    return asset["currency"]


def _validate_fx_block(
    asset: dict[str, Any], asset_document: dict[str, Any], dates: list[str]
) -> None:
    fx = asset_document.get("fx")
    is_overseas_asset = asset["assetType"] != "fx" and asset["currency"] != "KRW"
    if is_overseas_asset and dates and fx is None:
        raise ValueError(f"FX block is required for overseas asset {asset['id']}")
    if fx is None:
        return

    fx_dates = fx["dates"]
    fx_rates = fx["rates"]
    if len(fx_dates) != len(fx_rates):
        raise ValueError(f"FX column length mismatch for {asset['id']}")
    if fx_dates != sorted(set(fx_dates)):
        raise ValueError(f"FX dates must be sorted and unique for {asset['id']}")
    if any(
        isinstance(rate, bool)
        or not isinstance(rate, int | float)
        or not math.isfinite(rate)
        or rate <= 0
        for rate in fx_rates
    ):
        raise ValueError(f"FX rates must be positive and finite for {asset['id']}")
    if fx["maxStalenessDays"] != FX_MAX_STALENESS_DAYS:
        raise ValueError(f"FX maxStalenessDays must be {FX_MAX_STALENESS_DAYS} for {asset['id']}")

    if not is_overseas_asset or not dates:
        return
    expected_pair = f"{asset['currency']}/KRW"
    if fx["pair"] != expected_pair:
        raise ValueError(
            f"FX pair mismatch for {asset['id']}: expected {expected_pair}, found {fx['pair']}"
        )
    parsed_fx_dates = [date.fromisoformat(value) for value in fx_dates]
    for asset_date_text in dates:
        asset_date = date.fromisoformat(asset_date_text)
        index = bisect.bisect_right(parsed_fx_dates, asset_date) - 1
        if index < 0:
            raise ValueError(
                f"FX prior-only alignment unavailable for {asset['id']} at {asset_date_text}"
            )
        age = (asset_date - parsed_fx_dates[index]).days
        if age > FX_MAX_STALENESS_DAYS:
            raise ValueError(f"FX rate is stale for {asset['id']} at {asset_date_text}: {age} days")


def _validate_asset_against_catalog(asset: dict[str, Any], asset_document: dict[str, Any]) -> None:
    asset_id = asset["id"]
    if asset_document["assetId"] != asset_id:
        raise ValueError(f"asset id mismatch for {asset_id}")
    if asset_document["state"] != asset["status"]:
        raise ValueError(
            f"catalog status/asset state mismatch for {asset_id}: "
            f"{asset['status']} != {asset_document['state']}"
        )

    metadata = asset_document["metadata"]
    for field in ("symbol", "assetType", "exchange", "timezone", "returnBasis"):
        if metadata.get(field) != asset[field]:
            raise ValueError(f"metadata {field} mismatch for {asset_id}")
    expected_base_currency = _expected_base_currency(asset)
    if metadata.get("baseCurrency") != expected_base_currency:
        raise ValueError(f"metadata baseCurrency mismatch for {asset_id}")
    if asset["assetType"] == "fx":
        expected_quote_currency = asset["symbol"].split("/", maxsplit=1)[1]
        if metadata.get("quoteCurrency") != expected_quote_currency:
            raise ValueError(f"metadata quoteCurrency mismatch for {asset_id}")
        if asset["currency"] != expected_quote_currency:
            raise ValueError(f"catalog FX quote currency mismatch for {asset_id}")

    dates = asset_document["dates"]
    prices = asset_document["prices"]
    returns = asset_document["returns"]
    if not (len(dates) == len(prices) == len(returns)):
        raise ValueError(f"column length mismatch for {asset_id}")
    if dates != sorted(set(dates)):
        raise ValueError(f"dates must be sorted and unique for {asset_id}")
    series_start_floor = asset.get("seriesStartFloor")
    if dates and series_start_floor and dates[0] < series_start_floor:
        raise ValueError(f"series starts before identity floor for {asset_id}")

    expected_data_as_of = dates[-1] if dates else None
    if asset_document["dataAsOf"] != expected_data_as_of:
        raise ValueError(f"dataAsOf/last date mismatch for {asset_id}")
    expected_available_from = dates[0] if dates else None
    expected_available_to = dates[-1] if dates else None
    if asset["availableFrom"] != expected_available_from:
        raise ValueError(f"catalog availableFrom/first date mismatch for {asset_id}")
    if asset["availableTo"] != expected_available_to:
        raise ValueError(f"catalog availableTo/last date mismatch for {asset_id}")

    has_published_data = asset_document["state"] in DATA_BEARING_STATES
    if has_published_data:
        if len(dates) < 2:
            raise ValueError(f"published asset has insufficient observations: {asset_id}")
        if returns[0] is not None:
            raise ValueError(f"first return must be null for {asset_id}")
        for index in range(1, len(prices)):
            expected = prices[index] / prices[index - 1] - 1.0
            actual = returns[index]
            if actual is None or abs(actual - expected) > 1e-10:
                raise ValueError(f"price/return mismatch for {asset_id} at {dates[index]}")
        quality = asset_document.get("quality")
        if not isinstance(quality, dict):
            raise ValueError(f"published asset quality missing for {asset_id}")
        if quality.get("observationCount") != len(dates):
            raise ValueError(f"quality observation count mismatch for {asset_id}")
        expected_eligibility = len(returns) - 1 >= quality["minimumKellyObservations"]
        if quality.get("eligibleForKelly") is not expected_eligibility:
            raise ValueError(f"quality Kelly eligibility mismatch for {asset_id}")
        if quality["crossCheck"]["state"] == "mismatch":
            raise ValueError(f"cross-check mismatch cannot be published for {asset_id}")
    elif asset_document["state"] == "unavailable" and dates:
        raise ValueError(f"unavailable asset must not contain observations: {asset_id}")

    actual_provider = asset_document["source"]["provider"]
    if not has_published_data:
        allowed_providers = {"none"}
    elif asset["provider"]["provider"] == "krx":
        allowed_providers = {"krx"}
    elif asset["returnBasis"] == "total_return_approximation":
        allowed_providers = {"yahoo_finance", "twelve_data"}
    elif asset["assetType"] == "fx":
        allowed_providers = {"fred", "yahoo_finance", "stooq", "twelve_data"}
    else:
        allowed_providers = {"yahoo_finance", "stooq", "twelve_data"}
    if actual_provider not in allowed_providers:
        raise ValueError(
            f"source provider mismatch for {asset_id}: expected one of "
            f"{sorted(allowed_providers)}, found {actual_provider}"
        )
    _validate_fx_block(asset, asset_document, dates)


def load_worker_fixtures(root: Path) -> list[dict[str, Any]]:
    script = """
import { testSupport } from './worker/src/index.js';
const equity = testSupport.CATALOG.find((item) => item.symbol === 'NVDA');
const fx = testSupport.CATALOG.find((item) => item.symbol === 'USD/KRW');
const fixtures = [
  testSupport.normalizedDocument(
    [equity],
    [[['2026-01-02', 100], ['2026-01-05', 110]]],
  ),
  testSupport.normalizedDocument(
    [fx],
    [[['2026-01-02', 1450], ['2026-01-05', 1460]]],
    ['USD', 'KRW'],
  ),
];
process.stdout.write(JSON.stringify(fixtures));
"""
    try:
        completed = subprocess.run(  # noqa: S603
            ["node", "--input-type=module", "--eval", script],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
    except (FileNotFoundError, subprocess.CalledProcessError) as error:
        raise ValueError(f"Worker fixture generation failed: {error}") from error
    try:
        fixtures = json.loads(completed.stdout)
    except json.JSONDecodeError as error:
        raise ValueError("Worker fixture generation returned invalid JSON") from error
    if not isinstance(fixtures, list) or not fixtures:
        raise ValueError("Worker fixture generation returned no fixtures")
    return fixtures


def validate_worker_price_series(document: dict[str, Any], schema: Any, label: str) -> None:
    validate_document(document, schema, label)
    symbols = document["symbols"]
    metadata = document["metadata"]
    dates = document["dates"]
    prices = document["prices"]
    returns = document["returns"]
    if not (len(symbols) == len(metadata) == len(prices) == len(returns)):
        raise ValueError(f"{label} series count mismatch")
    if [item["symbol"] for item in metadata] != symbols:
        raise ValueError(f"{label} metadata/symbol mismatch")
    if dates != sorted(set(dates)):
        raise ValueError(f"{label} dates must be sorted and unique")
    expected_data_as_of = dates[-1] if dates else None
    if document["dataAsOf"] != expected_data_as_of:
        raise ValueError(f"{label} dataAsOf/last date mismatch")
    for series_index, (price_row, return_row) in enumerate(zip(prices, returns, strict=True)):
        if len(price_row) != len(dates) or len(return_row) != len(dates):
            raise ValueError(f"{label} row length mismatch at series {series_index}")
        previous: float | None = None
        for observation_index in range(len(dates)):
            current = price_row[observation_index]
            actual_return = return_row[observation_index]
            if current is None:
                if actual_return is not None:
                    raise ValueError(
                        f"{label} missing-price return mismatch at series {series_index}"
                    )
                continue
            if previous is None:
                if actual_return is not None:
                    raise ValueError(
                        f"{label} first-price return mismatch at series {series_index}"
                    )
                previous = current
                continue
            expected_return = current / previous - 1.0
            if actual_return is None or abs(actual_return - expected_return) > 1e-10:
                raise ValueError(
                    f"{label} price/return mismatch at series {series_index}, "
                    f"date {dates[observation_index]}"
                )
            previous = current
    if document["fx"] is not None:
        fx_rates = document["fx"]["rates"]
        if len(fx_rates) != len(dates):
            raise ValueError(f"{label} FX row length mismatch")
        if not prices or fx_rates != prices[0]:
            raise ValueError(f"{label} FX rates must match the first normalized series")


def load_worker_catalog(root: Path) -> list[dict[str, Any]]:
    script = """
import { testSupport } from './worker/src/index.js';
process.stdout.write(JSON.stringify(testSupport.CATALOG));
"""
    try:
        completed = subprocess.run(  # noqa: S603
            ["node", "--input-type=module", "--eval", script],
            cwd=root,
            check=True,
            capture_output=True,
            text=True,
        )
        catalog = json.loads(completed.stdout)
    except (FileNotFoundError, subprocess.CalledProcessError, json.JSONDecodeError) as error:
        raise ValueError(f"Worker catalog generation failed: {error}") from error
    if not isinstance(catalog, list):
        raise ValueError("Worker catalog generation returned an invalid payload")
    return catalog


def _validate_leveraged_products(catalog_assets: list[dict[str, Any]]) -> None:
    identifiers = {asset["id"] for asset in catalog_assets}
    for asset in catalog_assets:
        expected = EXPECTED_LEVERAGED_PRODUCTS.get(asset["symbol"])
        actual = asset.get("leveragedProducts")
        if actual != expected:
            raise ValueError(
                f"leveraged product mapping mismatch for {asset['symbol']}: "
                f"expected {expected}, found {actual}"
            )
        if actual and any(target not in identifiers for target in actual.values()):
            raise ValueError(f"leveraged product target missing for {asset['symbol']}")


def _validate_worker_catalog(root: Path, catalog_assets: list[dict[str, Any]]) -> None:
    fields = (
        "id",
        "symbol",
        "assetType",
        "exchange",
        "currency",
        "timezone",
    )
    static_projection = [
        {
            "id": asset["id"],
            "symbol": asset["symbol"],
            "assetType": asset["assetType"],
            "exchange": asset["exchange"],
            "currency": asset["currency"],
            "timezone": asset["timezone"],
        }
        for asset in catalog_assets
    ]
    worker_projection = [
        {field: asset[field] for field in fields} for asset in load_worker_catalog(root)
    ]
    if worker_projection != static_projection:
        raise ValueError("Worker/static catalog mismatch")


def _validate_generation_contracts(
    documents: dict[str, Any],
    catalog_assets: list[dict[str, Any]],
    asset_documents: dict[str, dict[str, Any]],
) -> None:
    catalog = documents["catalog"]
    summary = documents["summary"]
    automation = documents["automation"]
    available = [asset for asset in catalog_assets if asset["status"] in DATA_BEARING_STATES]
    expected_state = (
        "published"
        if len(available) == len(catalog_assets)
        and all(asset["status"] == "published" for asset in available)
        else ("degraded" if available else "unavailable")
    )
    data_as_of = max(
        (asset_documents[asset["id"]]["dataAsOf"] for asset in available),
        default=None,
    )

    if catalog["state"] != expected_state:
        raise ValueError("catalog aggregate state mismatch")
    if summary["state"] != expected_state or summary["status"]["state"] != expected_state:
        raise ValueError("summary/catalog aggregate state mismatch")
    if automation["state"] != expected_state:
        raise ValueError("automation/catalog aggregate state mismatch")
    if summary["dataAsOf"] != data_as_of or automation["dataAsOf"] != data_as_of:
        raise ValueError("summary/automation dataAsOf mismatch")
    if summary["coverage"]["assetCount"] != len(catalog_assets):
        raise ValueError("summary catalog asset count mismatch")
    if summary["coverage"]["availableAssetCount"] != len(available):
        raise ValueError("summary available asset count mismatch")
    if automation["publication"]["assetCount"] != len(available):
        raise ValueError("automation publication asset count mismatch")
    if len({catalog["generatedAt"], summary["generatedAt"], automation["generatedAt"]}) != 1:
        raise ValueError("generation timestamp mismatch")

    entity = next(
        (
            item
            for item in summary.get("primaryEntities", [])
            if item.get("id") == "kelly-allocation-lab"
        ),
        None,
    )
    if entity is None or entity.get("state") != expected_state:
        raise ValueError("summary primary entity state mismatch")

    providers = automation["provider"]["providers"]
    expected_providers = {
        "krx",
        "yahoo_finance",
        "finance_data_reader",
        "stooq",
        "fred",
        "finviz",
    }
    if {provider["name"] for provider in providers} != expected_providers:
        raise ValueError("automation provider set mismatch")
    if automation["provider"]["configured"] != any(
        provider["configured"] for provider in providers
    ):
        raise ValueError("automation provider configured aggregate mismatch")
    if automation["provider"]["rightsApproved"] != all(
        provider["rightsApproved"] for provider in providers
    ):
        raise ValueError("automation provider rights aggregate mismatch")


def validate_local(root: Path) -> dict[str, str]:
    schemas = {
        name: load_json(root / "schemas" / filename) for name, filename in SCHEMA_FILES.items()
    }
    documents = {name: load_json(root / filename) for name, filename in DATA_FILES.items()}
    for name, document in documents.items():
        validate_document(document, schemas[name], name)

    runtime = load_json(root / RUNTIME_FILE)
    validate_runtime_config(runtime, schemas["runtime"])
    provider_config = load_json(root / PROVIDER_CONFIG_FILE)

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
    _validate_leveraged_products(catalog_assets)
    validate_provider_config(provider_config, schemas["provider_config"], documents["catalog"])
    _validate_worker_catalog(root, catalog_assets)

    asset_documents: dict[str, dict[str, Any]] = {}
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
        _validate_asset_against_catalog(asset, asset_document)
        asset_documents[asset["id"]] = asset_document

    _validate_generation_contracts(documents, catalog_assets, asset_documents)

    for index, fixture in enumerate(load_worker_fixtures(root)):
        validate_worker_price_series(
            fixture,
            schemas["price_series"],
            f"Worker kelly-price-series fixture {index + 1}",
        )

    findings = scan_public_files(root)
    if findings:
        raise ValueError(f"credential material detected in public files: {', '.join(findings)}")

    return {
        name: hashlib.sha256((root / filename).read_bytes()).hexdigest()
        for name, filename in DATA_FILES.items()
    }


def fetch_bytes(url: str) -> bytes:
    parts = urllib.parse.urlsplit(url)
    query = urllib.parse.parse_qsl(parts.query, keep_blank_values=True)
    query.append(("__kelly_verify", str(time.time_ns())))
    cache_busted = urllib.parse.urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urllib.parse.urlencode(query), parts.fragment)
    )
    request = urllib.request.Request(
        cache_busted, headers={"User-Agent": "kelly-contract-verifier/1"}
    )
    with urllib.request.urlopen(request, timeout=20) as response:  # noqa: S310
        if response.status != 200:
            raise ValueError(f"public readback failed: {url} returned {response.status}")
        return response.read()


def fetch_matching_bytes(url: str, expected: bytes) -> bytes:
    """Retry boundedly while a Pages edge still serves the previous deployment."""

    expected_digest = hashlib.sha256(expected).hexdigest()
    last_digest: str | None = None
    last_error: Exception | None = None
    for attempt in range(LIVE_READBACK_ATTEMPTS):
        try:
            remote = fetch_bytes(url)
            last_digest = hashlib.sha256(remote).hexdigest()
            if last_digest == expected_digest:
                return remote
            last_error = None
        except Exception as error:  # network/HTTP failures are retried, then normalized below
            last_error = error
        if attempt + 1 < LIVE_READBACK_ATTEMPTS:
            time.sleep(LIVE_READBACK_DELAY_SECONDS * (attempt + 1))
    if last_error is not None and last_digest is None:
        raise ValueError(f"public readback failed after retries: {url}") from last_error
    raise ValueError(f"public hash mismatch for {url}: {last_digest}")


def validate_live(root: Path, base_url: str, local_hashes: dict[str, str]) -> None:
    normalized = base_url.rstrip("/") + "/"
    artifact_root = root / "dist"
    if artifact_root.is_dir():
        public_files = sorted(
            path.relative_to(artifact_root).as_posix()
            for path in artifact_root.rglob("*")
            if path.is_file()
        )
        expected_bytes = {
            relative: (artifact_root / relative).read_bytes() for relative in public_files
        }
    else:
        catalog = load_json(root / DATA_FILES["catalog"])
        public_files = [*DATA_FILES.values(), RUNTIME_FILE] + [
            f"data/{asset['dataPath']}" for asset in catalog["assets"]
        ]
        expected_bytes = {relative: (root / relative).read_bytes() for relative in public_files}

    for relative in public_files:
        remote = fetch_matching_bytes(normalized + relative, expected_bytes[relative])
        if relative.endswith(".json"):
            json.loads(remote)

    # Keep this argument as an explicit assertion that the caller validated the
    # three parent-dashboard contracts before any network readback.
    if set(local_hashes) != set(DATA_FILES):
        raise ValueError("local parent-contract hashes are incomplete")


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
