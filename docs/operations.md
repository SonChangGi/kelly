# Operations

Local validation is `make test`, `make verify`, and `make build`. The Pages workflow repeats tests, contract verification, a bounded static build, deployment, and public hash readback.

The Pages workflow runs both on ordinary `main` pushes and after the refresh workflow completes. This explicit `workflow_run` path is required because a commit pushed with the refresh job's default `GITHUB_TOKEN` does not recursively trigger another push-based workflow. A failed refresh may still publish its already-validated status diagnostics; Pages always rebuilds and verifies the current default-branch snapshot before deployment.

The static refresh runs at `23:30 UTC` each weekday and can also be started with `workflow_dispatch`. It has no `DATA_REFRESH_ENABLED`, Twelve Data, or provider-approval gate: Yahoo, FinanceDataReader, Stooq, FRED, and the ephemeral Finviz check require no repository secret. A manual run may set `backfill=true`, optional comma-separated `asset_ids`, and optional ISO `start`/`end` dates; the scheduled path is always incremental.

Repository secret `KRX_API_KEY` is optional. Publishing those two histories also requires repository variable `KRX_PUBLIC_DISPLAY_APPROVED=true`; the collector fails closed when either value is absent. Samsung Electronics and SK Hynix then remain explicitly `unavailable` while the other 48 assets refresh and publish. A local KRX Keychain entry is not visible in Actions; after reviewing the applicable display terms, use `KRX_PUBLIC_DISPLAY_APPROVED=true with-krx-keychain uv run python -m kelly_lab.refresh --catalog config/catalog.json` locally and configure the secret and repository variable separately for Actions. Never place a key, KRX login, password, upstream URL containing credentials, or raw provider error in a command argument, public file, log, or commit.

The collector first stages a full candidate generation, validates contracts and cross-column invariants, then atomically replaces artifacts. It records the actual source/adapter and whether the independent check passed, failed, or was unavailable. The workflow commits only `data/`; code and dependency changes still go through normal CI. The collector exit code is captured so validated status diagnostics can be committed before an unexpected provider failure marks the run failed.

Use ordinary incremental refresh for new observations:

```bash
uv run python -m kelly_lab.refresh --catalog config/catalog.json
uv run python -m kelly_lab.verify
```

Use explicit backfill only after reviewing an upstream historical-return revision or a ticker-history boundary:

```bash
uv run python -m kelly_lab.refresh --catalog config/catalog.json --backfill --asset-id stock-aapl --start 2021-01-01
uv run python -m kelly_lab.verify
```

Deploy the Worker from `worker/` after `npm ci`, `npm test`, and `npx wrangler deploy`. Set `ALLOWED_ORIGINS` to the exact approved browser origins. `/v1/health` must remain `unavailable` unless both rights and secret gates pass.
Before enabling the paid upstream path, configure an account-level rate limit
and quota alert; CORS limits browser reads but does not stop direct automated
requests from consuming the provider quota.

The post-deploy Pages readback compares every published byte with the built
artifact. It uses cache-busting and a bounded retry because an edge may briefly
serve the previous deployment; persistent hash drift still fails the workflow.

Each file uses an atomic replacement; the Git commit and Pages artifact are the
atomic public publication boundary for the complete generation. If an asset
fails, preserve its last validated artifact and expose `stale`, `degraded`, or
`unavailable` honestly. Never commit provisional or partially mixed
observations. Inspect `data/automation-status.json`, `data/summary.json`, and the
affected asset's `quality` block after every backfill or provider incident.
