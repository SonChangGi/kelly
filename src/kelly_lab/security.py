from __future__ import annotations

import re
from pathlib import Path

PUBLIC_ROOTS = (
    "site",
    "data",
    "docs",
    "schemas",
    "config",
    ".github",
    "src",
    "scripts",
    "worker/src",
)
PUBLIC_FILES = (
    ".env.example",
    "README.md",
    "DESIGN.md",
    "Makefile",
    "package.json",
    "package-lock.json",
    "pyproject.toml",
    "worker/package.json",
    "worker/package-lock.json",
    "worker/wrangler.toml",
)
TEXT_SUFFIXES = {
    ".css",
    ".html",
    ".js",
    ".json",
    ".md",
    ".mjs",
    ".py",
    ".svg",
    ".toml",
    ".txt",
    ".xml",
    ".yaml",
    ".yml",
}
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{16,}"),
)
SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|secret|token|password)\b\s*[:=]\s*(?P<value>[^\s#]+)"
)


def _contains_secret(text: str) -> bool:
    if any(pattern.search(text) for pattern in SECRET_PATTERNS):
        return True
    for match in SECRET_ASSIGNMENT.finditer(text):
        value = match.group("value").strip("'\"`,;)")
        if len(value) < 8 or value.startswith(("$", "{", "<")):
            continue
        if value.lower() in {"changeme", "placeholder", "undefined"}:
            continue
        return True
    return False


def scan_public_files(root: Path) -> list[str]:
    findings: list[str] = []
    candidates: set[Path] = set()
    for public_root in PUBLIC_ROOTS:
        base = root / public_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file() and path.suffix.lower() in TEXT_SUFFIXES:
                candidates.add(path)
    for relative in PUBLIC_FILES:
        path = root / relative
        if path.is_file():
            candidates.add(path)
    for path in candidates:
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _contains_secret(text):
            findings.append(str(path.relative_to(root)))
    return sorted(set(findings))
