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

The KRX API key is optional for a generation. Without it, the two Korean equities remain `unavailable` and the other 48 entries continue. When KRX data is displayed, the UI identifies `한국거래소 통계정보`. The collector never requires or publishes a KRX login ID/password. The separate Twelve Data Worker remains unavailable unless its existing external-display approval gate and server-side secret are both configured. No client bundle or public JSON contains provider credentials.

The generation verifier reconciles catalog state and counts with every asset,
the parent summary, and automation publication status. It also requires the
Worker allowlist to match the static catalog and permits leveraged-product
mappings only for S&amp;P 500→SSO/SDS and Nasdaq-100→QLD/QID pairs.
