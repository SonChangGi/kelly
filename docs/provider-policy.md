# Provider and publication policy

The two Korean equities may be sourced only through the official KRX Open API and retain `한국거래소 통계정보` attribution. A key alone is not permission to place the received price series in a public JSON file: the static publisher remains fail-closed until the operator has separately documented public-display/third-party provision rights and sets `KRX_PUBLIC_DISPLAY_APPROVED=true`. The remaining allowlisted instruments use Twelve Data only after server-side credentials and external-display rights are both confirmed. The browser never receives a provider secret.

Both server-side collectors follow the Twelve Data `/time_series` request
contract and authenticate with the `Authorization: apikey …` header; the key is
never placed in a query string. The requested allowlisted exchange is sent for
exchange-traded instruments. Before prices are labeled with catalog metadata,
the response symbol, exchange/type, and currency must match the requested
instrument. A mismatch fails closed with a stable reason code that never embeds
an upstream URL or raw error.

The Worker bounds requests to five calendar years and 5,000 observations,
rejects a response that reaches the point cap, and requires the returned end to
be within ten calendar days of the requested end. It emits only the project's
normalized columnar contract and never returns provider credentials, metadata,
raw errors, or unbounded response bodies. Provider 401/403/404 becomes
`unavailable`; rate limits, upstream failures, malformed payloads, identity
mismatches, and network failures become `degraded`.

Cached responses contain only normalized output and are stored only after a
successful response. No error response is cached. Before enabling public
collection, verify the current provider plan, attribution requirements, caching
terms, external-display rights, instrument coverage, and KRX access terms.
Sources: [KRX 이용약관](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO002.jsp),
[KRX 이용안내](https://openapi.krx.co.kr/contents/OPP/INFO/OPPINFO003.jsp),
[Twelve Data external-display plans](https://twelvedata.com/pricing-business),
[Twelve Data attribution](https://support.twelvedata.com/en/articles/12647398-attribution-guidelines-for-using-twelve-data),
[Cloudflare Workers Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/),
and [Cloudflare Cache API](https://developers.cloudflare.com/workers/runtime-apis/cache/).

Historical vendor revisions require an explicit, reviewed backfill. A refresh must not mix a revised upstream history with previously frozen derived rows.
