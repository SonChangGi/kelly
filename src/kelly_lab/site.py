from __future__ import annotations

import argparse
import shutil
import tempfile
from pathlib import Path

from .security import scan_public_files

STATIC_DIRECTORIES = ("data", "docs", "schemas")
STATIC_FILES = ("favicon.svg",)


def build_site(root: Path, output: Path) -> None:
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
        (staging / ".nojekyll").write_text("", encoding="utf-8")
        if output.exists():
            shutil.rmtree(output)
        shutil.copytree(staging, output)


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the bounded Kelly Pages artifact")
    parser.add_argument("--output", type=Path, default=Path("dist"))
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[2]
    output = args.output if args.output.is_absolute() else root / args.output
    build_site(root, output)
    try:
        display_output = output.relative_to(root)
    except ValueError:
        display_output = output
    print(f"Pages artifact ready: {display_output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
