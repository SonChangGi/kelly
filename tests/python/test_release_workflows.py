from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_refresh_completion_explicitly_triggers_pages_deployment() -> None:
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8")
    expanded = (ROOT / ".github/workflows/refresh-expanded-us.yml").read_text(encoding="utf-8")

    assert 'workflows: ["Refresh validated market data", "Refresh expanded US universe"]' in pages
    assert "types: [completed]" in pages
    assert "git push" in refresh
    assert "git push" in expanded
    assert "pages: write" in pages
    assert "actions/deploy-pages@v4" in pages


def test_refresh_workflow_runs_free_sources_and_supports_targeted_backfill() -> None:
    refresh = (ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8")

    assert "DATA_REFRESH_ENABLED" not in refresh
    assert "asset_ids:" in refresh
    assert 'refresh_args+=(--asset-id "$asset_id")' in refresh
    assert "KRX_API_KEY: ${{ secrets.KRX_API_KEY }}" in refresh
    assert "KRX_PUBLIC_DISPLAY_APPROVED: ${{ vars.KRX_PUBLIC_DISPLAY_APPROVED }}" in refresh
    assert (
        "YAHOO_PUBLIC_DISPLAY_APPROVED: ${{ vars.YAHOO_PUBLIC_DISPLAY_APPROVED || 'false' }}"
        in refresh
    )
    assert "uv run python -m kelly_lab.refresh" in refresh


def test_expanded_us_refresh_is_key_free_rights_gated_and_checks_fresh_coverage() -> None:
    expanded = (ROOT / ".github/workflows/refresh-expanded-us.yml").read_text(encoding="utf-8")

    assert "fetch-us-batch" in expanded
    assert "--cache-scope public" in expanded
    assert "freshCount" in expanded
    assert "preservedCount" in expanded
    assert "dataAsOf" in expanded
    assert (
        "YAHOO_PUBLIC_DISPLAY_APPROVED: ${{ vars.YAHOO_PUBLIC_DISPLAY_APPROVED || 'false' }}"
    ) in expanded
    assert '[[ "$YAHOO_PUBLIC_DISPLAY_APPROVED" != "true" ]]' in expanded
    assert "backfill:" in expanded
    assert "refresh_args+=(--backfill)" in expanded
    assert "KRX_API_KEY" not in expanded
    assert "TWELVE_DATA_API_KEY" not in expanded
