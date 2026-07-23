from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_refresh_deploys_only_the_exact_commit_that_changed_public_data() -> None:
    pages = (ROOT / ".github/workflows/pages.yml").read_text(encoding="utf-8")
    refresh = (ROOT / ".github/workflows/refresh-data.yml").read_text(encoding="utf-8")
    expanded = (ROOT / ".github/workflows/refresh-expanded-us.yml").read_text(encoding="utf-8")

    assert "workflow_run:" not in pages
    assert "workflow_call:" in pages
    assert "ref: ${{ inputs.ref || github.sha }}" in pages
    assert "queue: max" in pages
    assert "Skip a superseded deployment" in pages
    assert "git fetch --no-tags --depth=1 origin main" in pages
    assert '[[ "$requested_sha" == "$latest_sha" ]]' in pages
    assert "if: steps.freshness.outputs.current == 'true'" in pages
    assert "pages: write" in pages
    assert "actions/deploy-pages@v4" in pages
    for workflow in (refresh, expanded):
        assert "group: kelly-data-writer-${{ github.ref }}" in workflow
        assert "queue: max" in workflow
        assert "data_changed:" in workflow
        assert "published_sha:" in workflow
        assert "git diff --cached --quiet" in workflow
        assert 'echo "data_changed=false" >> "$GITHUB_OUTPUT"' in workflow
        assert 'echo "data_changed=true" >> "$GITHUB_OUTPUT"' in workflow
        assert 'echo "published_sha=$(git rev-parse HEAD)" >> "$GITHUB_OUTPUT"' in workflow
        assert "needs.refresh.outputs.data_changed == 'true'" in workflow
        assert "uses: ./.github/workflows/pages.yml" in workflow
        assert "ref: ${{ needs.refresh.outputs.published_sha }}" in workflow
        assert 'git push origin "HEAD:${GITHUB_REF_NAME}"' in workflow
        assert 'git fetch --no-tags origin "$GITHUB_REF_NAME"' in workflow
        assert '"$(git rev-parse HEAD)" != "$(git rev-parse FETCH_HEAD)"' in workflow
        assert "git rebase" not in workflow
    assert "always()" in refresh


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
    assert "id: publish" in refresh
    assert 'git push origin "HEAD:${GITHUB_REF_NAME}"' in refresh
    assert "Fail after publishing refresh diagnostics" in refresh


def test_expanded_us_refresh_skips_cleanly_until_publication_is_approved() -> None:
    expanded = (ROOT / ".github/workflows/refresh-expanded-us.yml").read_text(encoding="utf-8")

    assert "fetch-us-batch" in expanded
    assert "--cache-scope public" in expanded
    assert "freshCount" in expanded
    assert "preservedCount" in expanded
    assert "dataAsOf" in expanded
    assert (
        "YAHOO_PUBLIC_DISPLAY_APPROVED: ${{ vars.YAHOO_PUBLIC_DISPLAY_APPROVED || 'false' }}"
    ) in expanded
    assert "Check expanded publication gate" in expanded
    assert 'echo "approved=false" >> "$GITHUB_OUTPUT"' in expanded
    assert "Expanded Yahoo-derived publication skipped" in expanded
    assert "if: steps.preflight.outputs.approved == 'true'" in expanded
    assert '[[ "$YAHOO_PUBLIC_DISPLAY_APPROVED" != "true" ]]' not in expanded
    assert "backfill:" in expanded
    assert "refresh_args+=(--backfill)" in expanded
    assert "KRX_API_KEY" not in expanded
    assert "TWELVE_DATA_API_KEY" not in expanded
