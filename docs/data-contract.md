# Public data contract

All published JSON uses schema version 1 and one of these states: `published`, `live_api`, `stale`, `degraded`, `unavailable`, or `ruin`. No unlisted state is accepted. `unavailable` is a valid, explicit result and must never be replaced with invented observations.

`data/catalog.json` is the locked 50-asset discovery catalog. `data/assets/<id>.json` is a normalized daily series or an explicit unavailable placeholder. `data/summary.json` uses `contract=quant-research-summary` and `projectId=kelly` for the parent dashboard. `data/automation-status.json` reports collection and publication independently.

`data/runtime.json` contains only the nullable Worker base URL. A non-null value must be credential-free HTTPS with no query or fragment and is checked before every build. `config/catalog.json` is the provider allowlist; its version, IDs, provider symbols, exchanges, return bases, and approved leveraged-product mappings must exactly match the corresponding public catalog projection.

For an asset file, `dates[i]`, `prices[i]`, and `returns[i]` describe the same observation. The first return is null. For live multi-series responses, `prices[s][d]` and `returns[s][d]` correspond to `symbols[s]` and `dates[d]`; a missing aligned observation is null, and the next available return uses that series' prior non-null price. A validator must enforce equal column lengths, ascending unique dates, positive finite prices or rates, finite returns, and a `dataAsOf` equal to the last observation date.

The catalog and each asset file are one atomic contract. `catalog.status` must equal the asset `state`; symbol, type, exchange, timezone, return basis, and base currency must agree. The catalog provider is the preferred route, while `asset.source.provider` and optional `asset.source.adapter` record the route that actually produced the observations. Any fallback must be compatible with the locked return basis. `availableFrom` and `availableTo` equal the first and last dates. An unavailable placeholder has empty columns, null availability bounds and `dataAsOf`, and `source.provider=none` while retaining the intended provider only in the catalog.

An overseas asset with observations includes a `USD/KRW` FX block. FX dates and positive rates have equal lengths, dates are ascending and unique, and `maxStalenessDays` is exactly 5. Each asset date uses only the most recent FX observation on or before that date; a future rate or a rate older than five calendar days is rejected. The verifier also generates representative history and FX responses through the Worker normalization code and validates both the `kelly-price-series` schema and cross-column semantics.

`returnBasis` is explicit: Korean equities and indices use `price_return`; US equities and ETFs use `total_return_approximation`; USD/KRW uses `fx_rate`. Actual 2x and inverse 2x instruments are mapped by asset id in `leveragedProducts`; a synthetic daily 2x path is never mislabeled as an actual ETF path.

Static validated files take precedence for reproducible research. A `live_api` response is an exploration snapshot and does not silently overwrite frozen research history.

Every observed asset may include a `quality` block with observation count, 60-return Kelly eligibility, and an independent cross-check result. A cross-check contains only provider identity, state, comparison-window dates, common-return count, and bounded return-difference statistics; Finviz source rows are never copied into the static contract. New refreshes write `windowStart` and `windowEnd` together to make the evidence window explicit because an incremental refresh normally rechecks only its recent overlap rather than the full stored history; version-1 files produced before this field pair was introduced remain readable until their next refresh. `eligibleForKelly=false` does not hide valid chart history—it prevents a short or otherwise insufficient series from being presented as a Kelly estimate.

Static source policy is explicit. Yahoo adjusted close supplies US equity/ETF `total_return_approximation`; Yahoo raw close supplies index `price_return`; FinanceDataReader is a Yahoo adapter fallback rather than independent corroboration; Stooq is price-return-only; FRED `DEXKOUS` is KRW per USD and is stored without inversion; Finviz is an ephemeral check only. Each actual source and any limitation is disclosed in the asset file.

## Dynamic US asset cache

The locked catalog remains exactly 50 assets. An on-demand US equity or ETF is
therefore not appended to `data/catalog.json` and never writes below
`data/assets/`. Its normalized `kelly-asset-history` document uses
`metadata.catalogScope=dynamic` and records the provider-observed symbol,
exchange code, instrument type, optional display name, and optional first-trade
date. Missing provider names remain null rather than being invented.

The default local path is
`var/dynamic-assets/dynamic-us-<sanitized-symbol>.json`. An explicit public
cache request uses `data/dynamic-assets/` and is consequently copied into a
Pages build, but remains outside the core discovery and automation counts.
Because keyless access does not establish public-display or redistribution
rights, a public request fails before provider discovery unless the operator
has explicitly set `YAHOO_PUBLIC_DISPLAY_APPROVED=true`. This is an operating
approval flag, not a provider credential; local collection does not require it.
`data/dynamic-catalog.json` is its only public discovery layer. A public
single-symbol fetch atomically upserts this manifest; a batch replaces it only
after at least one fresh asset passes validation. The manifest records
`requestedCount`, `attemptedCount`, `freshCount`, `preservedCount`,
`prunedCount`, per-symbol stable failure reasons, and a normalized projection
of every referenced asset. `freshCount + preservedCount` must equal
`assetCount`.

Callers cannot provide an arbitrary output path. Only conservative US ticker
syntax is accepted, the cache filename is derived from the validated canonical
symbol, and a symlink or path escape is rejected.

Dynamic requests are inclusive, cannot end in the future, and may cover at
most 1,827 days. Yahoo metadata must identify an `EQUITY` or `ETF`, USD
currency, a recognized US exchange, and the `America/New_York` exchange
timezone. Provider-observed 3x and inverse-3x ETF products are rejected in v1.
History is trimmed to an observed first-trade date when available.
The default adjusted series may fall back only to FinanceDataReader's same
Yahoo upstream. Stooq can corroborate that series as a price-return source but
cannot replace it. If Stooq returns an access challenge, a recent Finviz raw
price window is the next ephemeral cross-check; only aggregate differences are
retained. An independent mismatch rejects the asset, while an unavailable or
short check is disclosed as degraded. Stooq primary history is permitted only
when the user explicitly requests `price_return` semantics.
The cross-check may include a bounded `attempts` list with stable reason codes,
so a Stooq failure followed by a Finviz failure is not collapsed into one
provider label. No raw response text is retained.

The default batch target is 250 non-core US assets (maximum 500). Nasdaq's
public stock screener provides the primary market-cap-ranked candidate pool;
FinanceDataReader exchange listings are availability fallback and are not
misrepresented as one cross-exchange market-cap ranking. Candidate attempts
are bounded and a repeated upstream outage opens a batch circuit. A partial
generation may fill gaps only from schema- and digest-validated last-good
entries. On successful manifest replacement, only unreferenced regular files
matching `dynamic-us-*.json` inside the dedicated directory are pruned.
Normal refreshes fetch a 35-day overlap, preserve validated historical returns,
and append only newer observations. An overlap removal or return drift preserves
the last-good file and returns a `*_backfill_required` reason. Only an explicit
`--backfill` may replace complete history or change its requested start. Listings
whose observed names unambiguously identify preferred shares, units, warrants,
rights, or debt-like securities are skipped; newly listed common stocks remain
visible but are not Kelly-eligible until they reach 60 returns.

The KRX API key is optional for a generation. Without it, the two Korean equities remain `unavailable` and the other 48 entries continue. When KRX data is displayed, the UI identifies `한국거래소 통계정보`. The collector never requires or publishes a KRX login ID/password. The optional Worker uses key-free Yahoo search/history for validated US instruments; its USD/KRW Twelve Data route remains unavailable unless the existing external-display approval gate and server-side secret are both configured. No client bundle or public JSON contains provider credentials.

The generation verifier reconciles catalog state and counts with every asset,
the parent summary, and automation publication status. The Worker's locked
core projection must still match the static catalog, while a non-core US
ticker is admitted only after live identity validation. Leveraged-product
mappings remain limited to S&amp;P 500→SSO/SDS and Nasdaq-100→QLD/QID pairs.
