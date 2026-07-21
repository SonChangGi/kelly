# Provider and publication policy

The two Korean equities are sourced only through an official KRX surface and retain KRX attribution. The remaining allowlisted instruments use Twelve Data only after server-side credentials and external-display rights are both confirmed. The browser never receives a provider secret.

The Worker follows the official Twelve Data `/time_series` request contract, authenticates with the server-side `Authorization: apikey …` header, bounds symbols, dates, and output size, and emits only the project’s normalized columnar contract. It never republishes provider metadata, raw observations, raw errors, or response bodies. Provider 401/403/404 becomes `unavailable`; rate limits, upstream failures, malformed payloads, and network failures become `degraded`.

Cached responses contain only normalized output and are stored only after a successful response. No error response is cached. Before enabling public collection, verify the current provider plan, attribution requirements, caching terms, external-display rights, instrument coverage, and KRX access terms. Sources: [Twelve Data API documentation](https://twelvedata.com/docs/llms), [Cloudflare Workers Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/), and [Cloudflare Cache API](https://developers.cloudflare.com/workers/runtime-apis/cache/).

Historical vendor revisions require an explicit, reviewed backfill. A refresh must not mix a revised upstream history with previously frozen derived rows.
