# Operations

Local validation is `make test`, `make verify`, and `make build`. The Pages workflow repeats tests, contract verification, a bounded static build, deployment, and public hash readback.

The Pages workflow runs both on ordinary `main` pushes and after the refresh workflow completes. This explicit `workflow_run` path is required because a commit pushed with the refresh job's default `GITHUB_TOKEN` does not recursively trigger another push-based workflow. A failed refresh may still publish its already-validated status diagnostics; Pages always rebuilds and verifies the current default-branch snapshot before deployment.

The static refresh runs at `23:30 UTC` each weekday and can also be started with `workflow_dispatch`. Yahoo, FinanceDataReader, Stooq, FRED, and the ephemeral Finviz check require no provider API secret. Yahoo-family public collection nevertheless requires repository variable `YAHOO_PUBLIC_DISPLAY_APPROVED=true`; a selected Yahoo asset fails before provider access when that operating approval is absent. Targeted FRED- or KRX-only refreshes remain available without it. A manual run may set `backfill=true`, optional comma-separated `asset_ids`, and optional ISO `start`/`end` dates; the scheduled path is always incremental.

Repository secret `KRX_API_KEY` is optional. Publishing those two histories also requires repository variable `KRX_PUBLIC_DISPLAY_APPROVED=true`; the collector fails closed when either value is absent. Samsung Electronics and SK Hynix then remain explicitly `unavailable`; the overseas paths are governed independently by the Yahoo approval above. A local KRX Keychain entry is not visible in Actions; after reviewing the applicable display terms, use `KRX_PUBLIC_DISPLAY_APPROVED=true with-krx-keychain uv run python -m kelly_lab.refresh --catalog config/catalog.json --asset-id kr-005930 --asset-id kr-000660` locally and configure the secret and repository variable separately for Actions. Never place a key, KRX login, password, upstream URL containing credentials, or raw provider error in a command argument, public file, log, or commit.

The collector first stages a full candidate generation, validates contracts and cross-column invariants, then atomically replaces artifacts. It records the actual source/adapter and whether the independent check passed, failed, or was unavailable. The workflow commits only `data/`; code and dependency changes still go through normal CI. The collector exit code is captured so validated status diagnostics can be committed before an unexpected provider failure marks the run failed.

Use ordinary incremental refresh for new observations:

```bash
YAHOO_PUBLIC_DISPLAY_APPROVED=true uv run python -m kelly_lab.refresh --catalog config/catalog.json
uv run python -m kelly_lab.verify
```

Use explicit backfill only after reviewing an upstream historical-return revision or a ticker-history boundary:

```bash
YAHOO_PUBLIC_DISPLAY_APPROVED=true uv run python -m kelly_lab.refresh --catalog config/catalog.json --backfill --asset-id stock-aapl --start 2021-01-01
uv run python -m kelly_lab.verify
```

The expanded non-core US cache is a separate weekly/manual generation. Run a
small local rehearsal first, then the public batch:

```bash
uv run kelly-lab fetch-us-batch --universe file --symbols-file tickers.txt --count 3 --cache-scope local
YAHOO_PUBLIC_DISPLAY_APPROVED=true uv run kelly-lab fetch-us-batch --universe auto --count 250 --cache-scope public
uv run python -m kelly_lab.verify
```

`fetch-us-batch` uses Nasdaq screener market caps first and FinanceDataReader
US listings only as a discovery fallback. It excludes core-catalog duplicates,
bounds total attempts, and stops after repeated systemic provider failures.
Never approve a generation from `assetCount` alone: require at least 90% of the
target in `freshCount`, require recent `dataAsOf` values, and verify
`freshCount + preservedCount == assetCount`. The weekly
`refresh-expanded-us.yml` workflow enforces those gates before committing. A
zero-fresh failure leaves the prior manifest byte-for-byte intact; a partial
success may retain only digest-validated last-good entries and reports them in
`preservedCount`. Successful replacement prunes only unreferenced regular
`dynamic-us-*.json` files inside `data/dynamic-assets/`.

The weekly path is incremental: it fetches a 35-day overlap, rejects changed
frozen returns, and appends only new observations. Review a
`historical_*_backfill_required` failure before using the manual workflow's
`backfill=true` input or the CLI's explicit `--backfill`; a drift alert never
overwrites the last-good asset first. Cross-check diagnostics retain the bounded
Stooq and Finviz attempt chain with stable reason codes.

Public expanded collection has a separate legal/operating gate. After reviewing
the current Yahoo terms and confirming the intended display rights, configure
the repository variable `YAHOO_PUBLIC_DISPLAY_APPROVED=true`. The weekly
workflow passes that value to the CLI and exits before provider discovery when
it is absent or false. It is not an API secret. Do not set it merely because an
endpoint is technically reachable; keep using `--cache-scope local` while the
rights review is incomplete.

Deploy the Worker from `worker/` after `npm ci`, `npm test`, and `npx wrangler deploy`. Set `ALLOWED_ORIGINS` to the exact approved browser origins. Key-free Yahoo US search/history reports `live_api` only when `YAHOO_PUBLIC_DISPLAY_APPROVED=true`; the optional Twelve Data USD/KRW capability remains `unavailable` unless both its rights and secret gates pass.
Before enabling the optional paid FX path, configure an account-level rate
limit and quota alert. CORS limits browser reads but does not stop direct
automated requests from consuming an upstream quota. The in-isolate Yahoo
limit is best-effort; use a Cloudflare account-level rate-limiting rule for a
durable public deployment.

The post-deploy Pages readback compares every published byte with the built
artifact. It uses cache-busting and a bounded retry because an edge may briefly
serve the previous deployment; persistent hash drift still fails the workflow.

Each file uses an atomic replacement; the Git commit and Pages artifact are the
atomic public publication boundary for the complete generation. If an asset
fails, preserve its last validated artifact and expose `stale`, `degraded`, or
`unavailable` honestly. Never commit provisional or partially mixed
observations. Inspect `data/automation-status.json`, `data/summary.json`, and the
affected asset's `quality` block after every backfill or provider incident.
