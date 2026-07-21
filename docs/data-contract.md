# Public data contract

All published JSON uses schema version 1 and one of these states: `published`, `live_api`, `stale`, `degraded`, `unavailable`, or `ruin`. No unlisted state is accepted. `unavailable` is a valid, explicit result and must never be replaced with invented observations.

`data/catalog.json` is the locked 50-asset discovery catalog. `data/assets/<id>.json` is a normalized daily series or an explicit unavailable placeholder. `data/summary.json` uses `contract=quant-research-summary` and `projectId=kelly` for the parent dashboard. `data/automation-status.json` reports collection and publication independently.

`data/runtime.json` contains only the nullable Worker base URL. A non-null value must be credential-free HTTPS with no query or fragment and is checked before every build. `config/catalog.json` is the provider allowlist; its version, IDs, provider symbols, exchanges, return bases, and approved leveraged-product mappings must exactly match the corresponding public catalog projection.

For an asset file, `dates[i]`, `prices[i]`, and `returns[i]` describe the same observation. The first return is null. For live multi-series responses, `prices[s][d]` and `returns[s][d]` correspond to `symbols[s]` and `dates[d]`; a missing aligned observation is null, and the next available return uses that series' prior non-null price. A validator must enforce equal column lengths, ascending unique dates, positive finite prices or rates, finite returns, and a `dataAsOf` equal to the last observation date.

The catalog and each asset file are one atomic contract. `catalog.status` must equal the asset `state`; symbol, type, exchange, timezone, return basis, base currency, and active source provider must agree. `availableFrom` and `availableTo` equal the first and last dates. An unavailable placeholder has empty columns, null availability bounds and `dataAsOf`, and `source.provider=none` while retaining the intended provider only in the catalog.

An overseas asset with observations includes a `USD/KRW` FX block. FX dates and positive rates have equal lengths, dates are ascending and unique, and `maxStalenessDays` is exactly 5. Each asset date uses only the most recent FX observation on or before that date; a future rate or a rate older than five calendar days is rejected. The verifier also generates representative history and FX responses through the Worker normalization code and validates both the `kelly-price-series` schema and cross-column semantics.

`returnBasis` is explicit: Korean equities and indices use `price_return`; US equities and ETFs use `total_return_approximation`; USD/KRW uses `fx_rate`. Actual 2x and inverse 2x instruments are mapped by asset id in `leveragedProducts`; a synthetic daily 2x path is never mislabeled as an actual ETF path.

Static validated files take precedence for reproducible research. A `live_api` response is an exploration snapshot and does not silently overwrite frozen research history.

Data access and public display rights are separate gates. Possession of a KRX API key does not establish public-display or third-party redistribution permission. Until `KRX_PUBLIC_DISPLAY_APPROVED=true` is explicitly configured, public KRX asset history remains `unavailable`; when approved and displayed, the UI identifies the source as `한국거래소 통계정보`. Twelve Data likewise remains unavailable unless its existing explicit external-display approval gate and server-side secret are both configured. No client bundle or public JSON contains provider credentials.

The generation verifier reconciles catalog state and counts with every asset,
the parent summary, and automation publication status. It also requires the
Worker allowlist to match the static catalog and permits leveraged-product
mappings only for S&amp;P 500→SSO/SDS and Nasdaq-100→QLD/QID pairs.
