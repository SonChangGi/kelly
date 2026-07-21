# Operations

Local validation is `make test`, `make verify`, and `make build`. The Pages workflow repeats tests, contract verification, a bounded static build, deployment, and public hash readback. The initial public data state is intentionally unavailable.

The refresh workflow remains skipped until repository variable `DATA_REFRESH_ENABLED=true`. Enabling it also requires `TWELVE_DATA_EXTERNAL_DISPLAY_APPROVED=true` and repository secret `TWELVE_DATA_API_KEY`. Store the Worker secret with `wrangler secret put TWELVE_DATA_API_KEY`; never place it in `wrangler.toml`, a Pages file, a command argument, logs, or a commit. Set Worker variable `TWELVE_DATA_RIGHTS_APPROVED=true` only after the same rights review.

Deploy the Worker from `worker/` after `npm install`, `npm test`, and `npx wrangler deploy`. Set `ALLOWED_ORIGINS` to the exact approved browser origins. `/v1/health` must remain `unavailable` unless both rights and secret gates pass.

An operational refresh must collect into a temporary generation, validate schemas and cross-column invariants, update automation status, then atomically publish all related files. If any asset fails, preserve the last validated generation and expose `stale`, `degraded`, or `unavailable` honestly. Never commit provisional or partially mixed observations.
