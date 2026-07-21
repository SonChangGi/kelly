# Operations

Local validation is `make test`, `make verify`, and `make build`. The Pages workflow repeats tests, contract verification, a bounded static build, deployment, and public hash readback. The initial public data state is intentionally unavailable.

The Pages workflow runs both on ordinary `main` pushes and after the refresh workflow completes. This explicit `workflow_run` path is required because a commit pushed with the refresh job's default `GITHUB_TOKEN` does not recursively trigger another push-based workflow. A failed refresh may still publish its already-validated status diagnostics; Pages always rebuilds and verifies the current default-branch snapshot before deployment.

The refresh workflow remains skipped until repository variable `DATA_REFRESH_ENABLED=true`. At least one complete provider gate is required. KRX requires repository secret `KRX_API_KEY` plus `KRX_PUBLIC_DISPLAY_APPROVED=true`; Twelve Data requires repository secret `TWELVE_DATA_API_KEY` plus `TWELVE_DATA_EXTERNAL_DISPLAY_APPROVED=true`. A local KRX Keychain entry proves only credential availability, not public-display rights. Store the Worker secret with `wrangler secret put TWELVE_DATA_API_KEY`; never place either secret in `wrangler.toml`, a Pages file, a command argument, logs, or a commit. Set Worker variable `TWELVE_DATA_RIGHTS_APPROVED=true` only after the same rights review.

Deploy the Worker from `worker/` after `npm ci`, `npm test`, and `npx wrangler deploy`. Set `ALLOWED_ORIGINS` to the exact approved browser origins. `/v1/health` must remain `unavailable` unless both rights and secret gates pass.
Before enabling the paid upstream path, configure an account-level rate limit
and quota alert; CORS limits browser reads but does not stop direct automated
requests from consuming the provider quota.

The post-deploy Pages readback compares every published byte with the built
artifact. It uses cache-busting and a bounded retry because an edge may briefly
serve the previous deployment; persistent hash drift still fails the workflow.

An operational refresh stages a complete candidate generation and validates its
schemas and cross-column invariants before replacing any file. Each file uses an
atomic replacement; the Git commit and Pages artifact are the atomic public
publication boundary for the complete generation. If an asset fails, preserve
its last validated artifact and expose `stale`, `degraded`, or `unavailable`
honestly. Never commit provisional or partially mixed observations.
