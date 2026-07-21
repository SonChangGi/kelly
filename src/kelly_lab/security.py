from __future__ import annotations

import re
from pathlib import Path

PUBLIC_ROOTS = ("site", "data", "docs", "schemas")
TEXT_SUFFIXES = {".css", ".html", ".js", ".json", ".md", ".txt", ".xml", ".yml"}
SECRET_PATTERNS = (
    re.compile(r"(?i)(api[_-]?key|secret|token|password)\s*[:=]\s*['\"][^'\"]{8,}"),
    re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    re.compile(r"(?i)bearer\s+[a-z0-9._~+/=-]{16,}"),
)


def scan_public_files(root: Path) -> list[str]:
    findings: list[str] = []
    for public_root in PUBLIC_ROOTS:
        base = root / public_root
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in SECRET_PATTERNS:
                if pattern.search(text):
                    findings.append(str(path.relative_to(root)))
                    break
    return sorted(set(findings))
