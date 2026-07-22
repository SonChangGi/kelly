from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kelly_lab.site import build_site

ROOT = Path(__file__).resolve().parents[2]


def _minimal_root(tmp_path: Path) -> Path:
    for name in ("site", "data", "docs", "schemas"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def _install_local_dynamic_fixture(root: Path) -> None:
    document = json.loads((ROOT / "data/assets/stock-aapl.json").read_text(encoding="utf-8"))
    document["assetId"] = "dynamic-us-cost"
    document["metadata"].update(
        {
            "symbol": "COST",
            "exchange": "NasdaqGS",
            "catalogScope": "dynamic",
            "providerSymbol": "COST",
            "providerExchangeCode": "NMS",
            "instrumentType": "EQUITY",
            "displayName": "Costco Wholesale Corporation",
            "firstTradeDate": "1986-07-09",
        }
    )
    asset_dir = root / "var/dynamic-assets"
    asset_dir.mkdir(parents=True)
    (asset_dir / "dynamic-us-cost.json").write_text(json.dumps(document), encoding="utf-8")
    entry = {
        "id": "dynamic-us-cost",
        "symbol": "COST",
        "name": "Costco Wholesale Corporation",
        "assetType": "equity",
        "exchange": "NasdaqGS",
        "currency": "USD",
        "timezone": "America/New_York",
        "returnBasis": "total_return_approximation",
        "dataPath": "dynamic-assets/dynamic-us-cost.json",
        "state": document["state"],
        "status": document["state"],
        "dataAsOf": document["dataAsOf"],
        "observationCount": document["quality"]["observationCount"],
        "source": {
            "provider": document["source"]["provider"],
            "adapter": document["source"]["adapter"],
        },
    }
    manifest = {
        "schemaVersion": 1,
        "contract": "kelly-dynamic-asset-catalog",
        "generatedAt": document["generatedAt"],
        "universeSource": "symbol_file",
        "universeFallbackReason": None,
        "requestedCount": 1,
        "attemptedCount": 1,
        "excludedCoreCount": 0,
        "freshCount": 1,
        "preservedCount": 0,
        "prunedCount": 0,
        "assetCount": 1,
        "failures": [],
        "assets": [entry],
    }
    (root / "var/dynamic-catalog.json").write_text(json.dumps(manifest), encoding="utf-8")


def test_pages_build_does_not_copy_local_dynamic_cache(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    (root / "var/dynamic-assets").mkdir(parents=True)
    (root / "var/dynamic-catalog.json").write_text("{}", encoding="utf-8")

    output = tmp_path / "dist"
    build_site(root, output)

    assert not (output / "data/dynamic-catalog.json").exists()
    assert not (output / "data/dynamic-assets").exists()


def test_local_build_copies_and_validates_dynamic_cache(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    _install_local_dynamic_fixture(root)

    output = tmp_path / "dist-local"
    build_site(root, output, include_local_dynamic=True)

    assert (output / "data/dynamic-catalog.json").is_file()
    assert (output / "data/dynamic-assets/dynamic-us-cost.json").is_file()


def test_local_build_requires_cache(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")

    with pytest.raises(FileNotFoundError, match="fetch-us-batch"):
        build_site(root, tmp_path / "dist-local", include_local_dynamic=True)


def test_build_rejects_project_root_and_preserves_sources(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")

    with pytest.raises(ValueError, match="project root"):
        build_site(root, root)

    assert (root / "site/index.html").is_file()
    assert (root / "data/catalog.json").is_file()


def test_local_build_cannot_target_pages_dist(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    _install_local_dynamic_fixture(root)

    with pytest.raises(ValueError, match="Pages dist"):
        build_site(root, root / "dist", include_local_dynamic=True)

    with pytest.raises(ValueError, match="inside the project|Pages dist"):
        build_site(root, root / "dist/local", include_local_dynamic=True)


def test_build_rejects_symlinked_source_content(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "marker.bin").write_bytes(b"not public")
    (root / "site/assets/external").symlink_to(outside, target_is_directory=True)

    with pytest.raises(ValueError, match="symlinks"):
        build_site(root, tmp_path / "dist")

    assert not (tmp_path / "dist/assets/external/marker.bin").exists()


def test_build_rejects_symlinked_site_root(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    shutil.rmtree(root / "site")
    (root / "site").symlink_to(ROOT / "site", target_is_directory=True)

    with pytest.raises(ValueError, match="site"):
        build_site(root, tmp_path / "dist")


def test_local_build_copies_only_manifest_references(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    _install_local_dynamic_fixture(root)
    (root / "var/dynamic-assets/unowned.csv").write_text(
        "secret,not-a-manifest-entry\n", encoding="utf-8"
    )

    output = tmp_path / "dist-local"
    build_site(root, output, include_local_dynamic=True)

    assert not (output / "data/dynamic-assets/unowned.csv").exists()


def test_local_build_replaces_staged_public_dynamic_cache(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    _install_local_dynamic_fixture(root)
    shutil.copytree(root / "var/dynamic-assets", root / "data/dynamic-assets")
    shutil.copy2(root / "var/dynamic-catalog.json", root / "data/dynamic-catalog.json")

    output = tmp_path / "dist-local"
    build_site(root, output, include_local_dynamic=True)

    assert (output / "data/dynamic-assets/dynamic-us-cost.json").is_file()
    assert (
        json.loads((output / "data/dynamic-catalog.json").read_text(encoding="utf-8"))["assetCount"]
        == 1
    )


def test_build_preserves_unowned_external_output(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    output = tmp_path / "unrelated-existing"
    output.mkdir()
    marker = output / "keep.txt"
    marker.write_text("user data", encoding="utf-8")

    with pytest.raises(ValueError, match="not owned"):
        build_site(root, output)

    assert marker.read_text(encoding="utf-8") == "user data"


def test_builder_can_replace_its_own_external_output(tmp_path: Path) -> None:
    root = _minimal_root(tmp_path / "root")
    output = tmp_path / "owned-output"
    build_site(root, output)
    (output / "old-file.txt").write_text("replace me", encoding="utf-8")

    build_site(root, output)

    assert not (output / "old-file.txt").exists()
    assert (output / ".kelly-site-artifact").is_file()
