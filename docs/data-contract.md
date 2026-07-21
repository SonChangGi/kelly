# Public data contract

All published JSON uses schema version 1 and one of these states: `published`, `live_api`, `stale`, `degraded`, `unavailable`, or `ruin`. No unlisted state is accepted. `unavailable` is a valid, explicit result and must never be replaced with invented observations.

`data/catalog.json` is the locked 50-asset discovery catalog. `data/assets/<id>.json` is a normalized daily series or an explicit unavailable placeholder. `data/summary.json` uses `contract=quant-research-summary` and `projectId=kelly` for the parent dashboard. `data/automation-status.json` reports collection and publication independently.

For an asset file, `dates[i]`, `prices[i]`, and `returns[i]` describe the same observation. The first return is null. For live multi-series responses, `prices[s][d]` and `returns[s][d]` correspond to `symbols[s]` and `dates[d]`; missing aligned observations are null. A validator must enforce equal column lengths, ascending unique dates, positive finite prices or rates, finite returns, and a `dataAsOf` equal to the last observation date.

`returnBasis` is explicit: Korean equities and indices use `price_return`; US equities and ETFs use `total_return_approximation`; USD/KRW uses `fx_rate`. Actual 2x and inverse 2x instruments are mapped by asset id in `leveragedProducts`; a synthetic daily 2x path is never mislabeled as an actual ETF path.

Static validated files take precedence for reproducible research. A `live_api` response is an exploration snapshot and does not silently overwrite frozen research history.
