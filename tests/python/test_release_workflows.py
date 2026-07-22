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


def test_refresh_workflow_runs_free_sources_and_supports_targeted_backfill() -> None:
    refresh = (ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8")

    assert "DATA_REFRESH_ENABLED" not in refresh
    assert "asset_ids:" in refresh
    assert 'refresh_args+=(--asset-id "$asset_id")' in refresh
    assert "KRX_API_KEY: ${{ secrets.KRX_API_KEY }}" in refresh
    assert "KRX_PUBLIC_DISPLAY_APPROVED: ${{ vars.KRX_PUBLIC_DISPLAY_APPROVED }}" in refresh
    assert "uv run python -m kelly_lab.refresh" in refresh
