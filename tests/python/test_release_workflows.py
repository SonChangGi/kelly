from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_refresh_completion_explicitly_triggers_pages_deployment() -> None:
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Refresh validated market data"]' in pages
    assert "types: [completed]" in pages
    assert "git push" in refresh
    assert "pages: write" in pages
    assert "actions/deploy-pages@v4" in pages
