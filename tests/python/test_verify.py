from __future__ import annotations

import copy
import json
import shutil
from pathlib import Path

import pytest

from kelly_lab.security import scan_public_files
from kelly_lab.verify import (
    _validate_asset_against_catalog,
    load_worker_fixtures,
    validate_live,
    validate_local,
    validate_worker_price_series,
)

ROOT = Path(__file__).resolve().parents[2]


def _load(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def test_secret_scan_covers_worker_toml_and_github_workflows(tmp_path: Path) -> None:
    worker_config = tmp_path / "worker/wrangler.toml"
    workflow = tmp_path / ".github/workflows/release.yml"
    worker_config.parent.mkdir(parents=True)
    workflow.parent.mkdir(parents=True)
    worker_config.write_text('API_KEY = "worker-secret-value"\n', encoding="utf-8")
    workflow.write_text("token: workflow-secret-value\n", encoding="utf-8")

    assert scan_public_files(tmp_path) == [
        ".github/workflows/release.yml",
        "worker/wrangler.toml",
    ]


def _catalog_asset(symbol: str = "SPY") -> dict:
    catalog = _load(ROOT / "data/catalog.json")
    return copy.deepcopy(next(item for item in catalog["assets"] if item["symbol"] == symbol))


def _asset_document(asset: dict) -> dict:
    return copy.deepcopy(_load(ROOT / "data" / asset["dataPath"]))


def _published_spy() -> tuple[dict, dict]:
    asset = _catalog_asset()
    document = _asset_document(asset)
    dates = ["2026-01-05", "2026-01-06"]
    asset.update(status="published", availableFrom=dates[0], availableTo=dates[-1])
    document.update(
        state="published",
        dataAsOf=dates[-1],
        dates=dates,
        prices=[100.0, 110.0],
        returns=[None, 0.1],
        fx={
            "pair": "USD/KRW",
            "dates": ["2026-01-02", "2026-01-05"],
            "rates": [1450.0, 1460.0],
            "maxStalenessDays": 5,
        },
    )
    document["quality"].update(observationCount=2, eligibleForKelly=False)
    document["source"]["provider"] = "twelve_data"
    return asset, document


def _contract_root(tmp_path: Path) -> Path:
    for directory in ("config", "data", "schemas", "worker"):
        shutil.copytree(ROOT / directory, tmp_path / directory)
    return tmp_path


def test_repository_contracts_and_worker_fixtures_pass() -> None:
    hashes = validate_local(ROOT)

    assert set(hashes) == {"summary", "catalog", "automation"}


def test_validate_local_rejects_credentialed_runtime_worker_url(tmp_path: Path) -> None:
    root = _contract_root(tmp_path)
    runtime_path = root / "data/runtime.json"
    runtime = _load(runtime_path)
    runtime["workerBaseUrl"] = "https://user:secret@worker.example.test"
    runtime_path.write_text(json.dumps(runtime), encoding="utf-8")

    with pytest.raises(ValueError, match="credential-free HTTPS"):
        validate_local(root)


def test_validate_local_rejects_provider_config_catalog_drift(tmp_path: Path) -> None:
    root = _contract_root(tmp_path)
    config_path = root / "config/catalog.json"
    config = _load(config_path)
    config["assets"][0]["providerSymbol"] = "WRONG"
    config_path.write_text(json.dumps(config), encoding="utf-8")

    with pytest.raises(ValueError, match="provider/public catalog projection mismatch"):
        validate_local(root)


def test_validate_local_rejects_forged_catalog_status(tmp_path: Path) -> None:
    root = _contract_root(tmp_path)
    catalog_path = root / "data/catalog.json"
    catalog = _load(catalog_path)
    catalog["assets"][0]["status"] = "published"
    catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="catalog status/asset state mismatch"):
        validate_local(root)


@pytest.mark.parametrize(
    ("relative", "mutate", "message"),
    [
        (
            "data/summary.json",
            lambda document: document["coverage"].update(availableAssetCount=1),
            "summary available asset count mismatch",
        ),
        (
            "data/automation-status.json",
            lambda document: document["publication"].update(assetCount=1),
            "automation publication asset count mismatch",
        ),
    ],
)
def test_validate_local_rejects_cross_contract_aggregate_drift(
    tmp_path: Path, relative: str, mutate, message: str
) -> None:
    root = _contract_root(tmp_path)
    path = root / relative
    document = _load(path)
    mutate(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(ValueError, match=message):
        validate_local(root)


def test_validate_local_rejects_unapproved_leveraged_proxy_mapping(tmp_path: Path) -> None:
    root = _contract_root(tmp_path)
    path = root / "data/catalog.json"
    catalog = _load(path)
    smh = next(asset for asset in catalog["assets"] if asset["symbol"] == "SMH")
    smh["leveragedProducts"] = {"long2x": "etf-usd", "inverse2x": "etf-ssg"}
    path.write_text(json.dumps(catalog), encoding="utf-8")

    with pytest.raises(ValueError, match="leveraged product mapping mismatch for SMH"):
        validate_local(root)


@pytest.mark.parametrize(
    ("field", "forged"),
    [
        ("symbol", "FORGED"),
        ("assetType", "equity"),
        ("exchange", "FORGED"),
        ("timezone", "UTC"),
        ("returnBasis", "price_return"),
        ("baseCurrency", "KRW"),
    ],
)
def test_rejects_catalog_metadata_mismatch(field: str, forged: str) -> None:
    asset, document = _published_spy()
    document["metadata"][field] = forged

    with pytest.raises(ValueError, match=f"metadata {field} mismatch"):
        _validate_asset_against_catalog(asset, document)


def test_rejects_source_provider_mismatch_for_published_and_unavailable() -> None:
    asset, document = _published_spy()
    document["source"]["provider"] = "none"
    with pytest.raises(ValueError, match="source provider mismatch"):
        _validate_asset_against_catalog(asset, document)

    asset = _catalog_asset()
    document = _asset_document(asset)
    asset.update(status="unavailable", availableFrom=None, availableTo=None)
    document.update(
        state="unavailable",
        dataAsOf=None,
        dates=[],
        prices=[],
        returns=[],
    )
    document["source"]["provider"] = "twelve_data"
    with pytest.raises(ValueError, match="source provider mismatch"):
        _validate_asset_against_catalog(asset, document)


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (
            lambda quality: quality.update(observationCount=3),
            "quality observation count mismatch",
        ),
        (
            lambda quality: quality.update(eligibleForKelly=True),
            "quality Kelly eligibility mismatch",
        ),
        (
            lambda quality: quality["crossCheck"].update(state="mismatch"),
            "cross-check mismatch cannot be published",
        ),
        (
            lambda quality: quality["crossCheck"].update(commonObservations=19),
            "passed cross-check has insufficient comparisons",
        ),
        (
            lambda quality: quality["crossCheck"].update(windowStart="2026-01-01"),
            "cross-check window incomplete",
        ),
        (
            lambda quality: quality["crossCheck"].update(windowStart=None, windowEnd=None),
            "passed cross-check window missing",
        ),
        (
            lambda quality: quality["crossCheck"].update(
                windowStart="2026-02-01", windowEnd="2026-01-01"
            ),
            "cross-check window order invalid",
        ),
    ],
)
def test_rejects_forged_published_quality(mutate, message: str) -> None:
    asset, document = _published_spy()
    mutate(document["quality"])

    with pytest.raises(ValueError, match=message):
        _validate_asset_against_catalog(asset, document)


@pytest.mark.parametrize(
    ("target", "value", "message"),
    [
        ("document.dataAsOf", "2026-01-05", "dataAsOf/last date mismatch"),
        ("asset.availableFrom", "2026-01-04", "availableFrom/first date mismatch"),
        ("asset.availableTo", "2026-01-07", "availableTo/last date mismatch"),
    ],
)
def test_rejects_date_boundary_mismatch(target: str, value: str, message: str) -> None:
    asset, document = _published_spy()
    owner, key = target.split(".")
    (asset if owner == "asset" else document)[key] = value

    with pytest.raises(ValueError, match=message):
        _validate_asset_against_catalog(asset, document)


def test_unavailable_dates_and_availability_must_remain_null() -> None:
    asset = _catalog_asset()
    document = _asset_document(asset)
    document["dataAsOf"] = "2026-01-05"
    with pytest.raises(ValueError, match="dataAsOf/last date mismatch"):
        _validate_asset_against_catalog(asset, document)

    document = _asset_document(asset)
    asset["availableFrom"] = "2026-01-05"
    with pytest.raises(ValueError, match="availableFrom/first date mismatch"):
        _validate_asset_against_catalog(asset, document)


def test_rejects_fx_length_order_rate_and_staleness_contracts() -> None:
    asset, document = _published_spy()
    document["fx"]["rates"].pop()
    with pytest.raises(ValueError, match="FX column length mismatch"):
        _validate_asset_against_catalog(asset, document)

    asset, document = _published_spy()
    document["fx"]["dates"].reverse()
    document["fx"]["rates"].reverse()
    with pytest.raises(ValueError, match="FX dates must be sorted and unique"):
        _validate_asset_against_catalog(asset, document)

    asset, document = _published_spy()
    document["fx"]["rates"][0] = 0
    with pytest.raises(ValueError, match="FX rates must be positive and finite"):
        _validate_asset_against_catalog(asset, document)

    asset, document = _published_spy()
    document["fx"]["maxStalenessDays"] = 4
    with pytest.raises(ValueError, match="maxStalenessDays must be 5"):
        _validate_asset_against_catalog(asset, document)


def test_rejects_missing_future_and_stale_fx_alignment() -> None:
    asset, document = _published_spy()
    document.pop("fx")
    with pytest.raises(ValueError, match="FX block is required"):
        _validate_asset_against_catalog(asset, document)

    asset, document = _published_spy()
    document["fx"]["dates"] = ["2026-01-06"]
    document["fx"]["rates"] = [1460.0]
    with pytest.raises(ValueError, match="prior-only alignment unavailable"):
        _validate_asset_against_catalog(asset, document)

    asset, document = _published_spy()
    document["fx"]["dates"] = ["2025-12-30"]
    document["fx"]["rates"] = [1450.0]
    with pytest.raises(ValueError, match="FX rate is stale"):
        _validate_asset_against_catalog(asset, document)


def test_worker_fixtures_reject_schema_and_cross_column_forgery() -> None:
    schema = _load(ROOT / "schemas/kelly-price-series.schema.json")
    fixtures = load_worker_fixtures(ROOT)
    for index, fixture in enumerate(fixtures):
        validate_worker_price_series(fixture, schema, f"fixture {index}")

    forged = copy.deepcopy(fixtures[0])
    forged["metadata"][0].pop("returnBasis")
    with pytest.raises(ValueError, match="contract invalid"):
        validate_worker_price_series(forged, schema, "forged fixture")

    forged = copy.deepcopy(fixtures[0])
    forged["returns"][0].append(0.0)
    with pytest.raises(ValueError, match="row length mismatch"):
        validate_worker_price_series(forged, schema, "forged fixture")

    forged = copy.deepcopy(fixtures[1])
    forged["fx"]["rates"][0] += 1
    with pytest.raises(ValueError, match="FX rates must match"):
        validate_worker_price_series(forged, schema, "forged fixture")


def test_live_readback_compares_every_built_artifact(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    (dist / "data").mkdir(parents=True)
    (dist / "index.html").write_text("<main>Kelly</main>", encoding="utf-8")
    (dist / "data/runtime.json").write_text('{"workerBaseUrl":null}', encoding="utf-8")
    expected = {
        "https://example.test/kelly/index.html": b"<main>Kelly</main>",
        "https://example.test/kelly/data/runtime.json": b'{"workerBaseUrl":null}',
    }
    requested: list[str] = []

    def fake_fetch(url: str) -> bytes:
        requested.append(url)
        return expected[url]

    monkeypatch.setattr("kelly_lab.verify.fetch_bytes", fake_fetch)
    validate_live(
        tmp_path,
        "https://example.test/kelly/",
        {"summary": "x", "catalog": "y", "automation": "z"},
    )
    assert requested == sorted(expected)


def test_live_readback_rejects_any_built_artifact_drift(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("expected", encoding="utf-8")
    monkeypatch.setattr("kelly_lab.verify.fetch_bytes", lambda _url: b"forged")
    monkeypatch.setattr("kelly_lab.verify.time.sleep", lambda _seconds: None)
    with pytest.raises(ValueError, match="public hash mismatch"):
        validate_live(
            tmp_path,
            "https://example.test/kelly/",
            {"summary": "x", "catalog": "y", "automation": "z"},
        )


def test_live_readback_retries_a_stale_pages_edge(tmp_path: Path, monkeypatch) -> None:
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("current", encoding="utf-8")
    responses = iter([b"previous", b"current"])
    sleeps: list[float] = []
    monkeypatch.setattr("kelly_lab.verify.fetch_bytes", lambda _url: next(responses))
    monkeypatch.setattr("kelly_lab.verify.time.sleep", sleeps.append)

    validate_live(
        tmp_path,
        "https://example.test/kelly/",
        {"summary": "x", "catalog": "y", "automation": "z"},
    )

    assert sleeps == [1.0]
