from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from .security import scan_public_files
from .verify import _validate_dynamic_catalog, load_json, validate_document

STATIC_DIRECTORIES = ("data", "docs", "schemas")
STATIC_FILES = ("favicon.svg",)
ARTIFACT_SENTINEL = ".kelly-site-artifact"
ARTIFACT_SENTINEL_CONTENT = "kelly-site-artifact-v1\n"
PROTECTED_OUTPUT_DIRECTORIES = (
    ".git",
    ".github",
    "config",
    "data",
    "docs",
    "schemas",
    "scripts",
    "site",
    "src",
    "tests",
    "var",
    "worker",
)


def _validated_output(root: Path, output: Path, *, include_local_dynamic: bool) -> Path:
    root = root.resolve()
    if output.is_symlink():
        raise ValueError("artifact output cannot be a symlink")
    resolved = output.resolve()
    if root.is_relative_to(resolved):
        raise ValueError("artifact output cannot be the project root or one of its ancestors")
    canonical_outputs = {(root / "dist").resolve(), (root / "dist-local").resolve()}
    if resolved.is_relative_to(root) and resolved not in canonical_outputs:
        raise ValueError("artifact output inside the project must be dist or dist-local")
    for name in PROTECTED_OUTPUT_DIRECTORIES:
        protected = (root / name).resolve()
        if resolved == protected or resolved.is_relative_to(protected):
            raise ValueError(f"artifact output cannot overwrite project source: {name}")
    pages_dist = (root / "dist").resolve()
    if include_local_dynamic and (resolved == pages_dist or resolved.is_relative_to(pages_dist)):
        raise ValueError("local dynamic data cannot be written to the Pages dist directory")
    if resolved.exists():
        if not resolved.is_dir():
            raise ValueError("artifact output must be a directory")
        sentinel = resolved / ARTIFACT_SENTINEL
        owned = (
            resolved in canonical_outputs
            or sentinel.is_file()
            and sentinel.read_text(encoding="utf-8", errors="ignore") == ARTIFACT_SENTINEL_CONTENT
        )
        if any(resolved.iterdir()) and not owned:
            raise ValueError("refusing to replace a nonempty directory not owned by this builder")
    return resolved


def _reject_source_symlinks(path: Path) -> None:
    if path.is_symlink():
        raise ValueError(f"build source cannot be a symlink: {path.name}")
    if path.is_dir():
        for child in path.rglob("*"):
            if child.is_symlink():
                raise ValueError(f"build source cannot contain symlinks: {child.relative_to(path)}")


def _copy_local_dynamic_cache(root: Path, staging: Path) -> None:
    manifest = root / "var/dynamic-catalog.json"
    assets = root / "var/dynamic-assets"
    if not manifest.is_file() or not assets.is_dir():
        raise FileNotFoundError(
            "local dynamic cache is missing; run `kelly-lab fetch-us-batch` first"
        )
    if manifest.is_symlink() or assets.is_symlink():
        raise ValueError("local dynamic cache paths cannot be symlinks")
    if any(path.is_symlink() for path in assets.rglob("*")):
        raise ValueError("local dynamic cache cannot contain symlinks")

    manifest_document = load_json(manifest)
    validate_document(
        manifest_document,
        load_json(root / "schemas/dynamic-catalog.schema.json"),
        "local dynamic catalog",
    )

    staged_manifest = staging / "data/dynamic-catalog.json"
    staged_assets = staging / "data/dynamic-assets"
    staged_manifest.unlink(missing_ok=True)
    if staged_assets.exists():
        shutil.rmtree(staged_assets)
    staged_assets.mkdir()

    source_root = assets.resolve()
    for entry in manifest_document["assets"]:
        relative = Path(entry["dataPath"])
        if relative.parent != Path("dynamic-assets"):
            raise ValueError(f"local dynamic data path is unsafe: {relative}")
        unresolved = assets / relative.name
        source = unresolved.resolve()
        if unresolved.is_symlink() or source.parent != source_root or not source.is_file():
            raise ValueError(f"local dynamic data path is unsafe or missing: {relative}")
        shutil.copy2(source, staged_assets / relative.name)
    shutil.copy2(manifest, staged_manifest)
    _validate_dynamic_catalog(
        staging,
        catalog_schema=load_json(staging / "schemas/dynamic-catalog.schema.json"),
        asset_schema=load_json(staging / "schemas/asset.schema.json"),
    )


def build_site(root: Path, output: Path, *, include_local_dynamic: bool = False) -> None:
    root = root.resolve()
    output = _validated_output(root, output, include_local_dynamic=include_local_dynamic)
    site = root / "site"
    required = (
        site / "index.html",
        site / "assets",
        *(site / name for name in STATIC_FILES),
        *(root / name for name in STATIC_DIRECTORIES),
    )
    missing = [str(path.relative_to(root)) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"missing static paths: {', '.join(missing)}")
    _reject_source_symlinks(site)
    for path in required:
        _reject_source_symlinks(path)

    findings = scan_public_files(root)
    if findings:
        raise ValueError(f"credential material detected in public files: {', '.join(findings)}")

    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="kelly-site-", dir=output.parent) as temporary:
        staging = Path(temporary) / "dist"
        staging.mkdir()
        shutil.copy2(site / "index.html", staging / "index.html")
        shutil.copytree(site / "assets", staging / "assets")
        for name in STATIC_FILES:
            shutil.copy2(site / name, staging / name)
        for name in STATIC_DIRECTORIES:
            shutil.copytree(root / name, staging / name)
        if include_local_dynamic:
            _copy_local_dynamic_cache(root, staging)
        (staging / ".nojekyll").write_text("", encoding="utf-8")
        (staging / ARTIFACT_SENTINEL).write_text(ARTIFACT_SENTINEL_CONTENT, encoding="utf-8")
        staged_findings = scan_public_files(staging)
        if staged_findings:
            raise ValueError(
                f"credential material detected in built files: {', '.join(staged_findings)}"
            )
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(staging, output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the bounded Kelly Pages artifact")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    parser.add_argument(
        "--include-local-dynamic",
        action="store_true",
        help="copy the ignored var/dynamic-* research cache into this local artifact",
    )
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output if args.output.is_absolute() else root / args.output
    build_site(root, output, include_local_dynamic=args.include_local_dynamic)
    try:
        display_output = output.relative_to(root)
    except ValueError:
        display_output = output
    artifact_kind = "Local research artifact" if args.include_local_dynamic else "Pages artifact"
    print(f"{artifact_kind} ready: {display_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
