# Provider and publication policy

The static research publisher is deliberately multi-source and records the
provider and adapter used for every published series. It never changes a
`returnBasis` merely because the preferred source is unavailable, never sends a
secret to the browser, and never labels one provider's observations as another
provider's data.

## Source roles

| Source | Role in the static publisher | Published semantics |
| --- | --- | --- |
| KRX Open API | Exclusive source for `005930.KS` and `000660.KS` | Official daily close, `price_return` |
| Yahoo Chart | Primary US equity/ETF/index source and USD/KRW fallback | `adjclose` for equity/ETF `total_return_approximation`; raw `close` for index `price_return` and FX |
| FinanceDataReader | Operational adapter fallback for the Yahoo Chart path | Same Yahoo adjusted/raw basis; not an independent corroborating source |
| Stooq | Independent raw-close fallback/check for price-return series | `price_return` only; never substituted for an adjusted total-return series |
| FRED `DEXKOUS` | Preferred independent USD/KRW source | Korean won per US dollar, stored directly as `USD/KRW` |
| Finviz | Ephemeral recent-history cross-check for US equities and ETFs | Raw values are not written to public artifacts; only a bounded pass/fail or aggregate difference may be retained |

The Yahoo and FinanceDataReader adapters therefore belong to one upstream
family. Switching between them is resilience, not cross-source confirmation.
If Stooq returns an access challenge instead of CSV, or FRED is unavailable,
the adapter fails closed and the artifact discloses that the independent check
was unavailable. Finviz is never promoted into a publication source.

US equities and ETFs use Yahoo's adjusted-close series as a total-return
approximation because it incorporates corporate-action adjustments. Indices
use raw closes and remain price-return series. USD/KRW uses FRED `DEXKOUS`
without inversion; Yahoo `KRW=X` is the bounded fallback. Each catalog entry
may also define a first-valid-date floor to prevent a reused ticker from
injecting an unrelated predecessor history.

This project does not claim that a source being free to access grants a general
redistribution licence. The operator is responsible for checking the current
terms before public use. Yahoo-derived files are identified as research-use
data and are not described as an official Yahoo API product. See the
[Yahoo API terms](https://legal.yahoo.com/us/en/yahoo/terms/product-atos/apiforydn/index.html),
[yfinance legal notice](https://ranaroussi.github.io/yfinance/),
[FinanceDataReader project](https://github.com/FinanceData/FinanceDataReader),
and [FRED `DEXKOUS`](https://fred.stlouisfed.org/series/DEXKOUS).

## KRX and optional credentials

The two Korean equities use only the official KRX Open API and retain
`한국거래소 통계정보` attribution. The scheduled free-source refresh does not
require KRX credentials: when repository secret `KRX_API_KEY` or repository
variable `KRX_PUBLIC_DISPLAY_APPROVED=true` is absent, those two assets remain
explicit `unavailable` placeholders while the other 48 catalog entries
continue. The collector does not require, transmit, log, or store a KRX
web-login ID or password.

For local collection, provide the key through the existing macOS Keychain
wrapper (`with-krx-keychain`) rather than a command argument or committed file.
GitHub Actions needs its own `KRX_API_KEY` repository secret and an explicit
`KRX_PUBLIC_DISPLAY_APPROVED=true` repository variable; a local Keychain entry
is not available to the runner. KRX access and public-display terms must still
be reviewed by the operator, and every published KRX series identifies the
official source.

## Validation and history changes

Every candidate series is checked for ordered unique dates, positive finite
prices, finite returns, latest-date consistency, start-date floors, return
outliers, and at least 60 returns before Kelly estimation is marked eligible.
Where an independent source is available, overlapping daily returns are
compared with bounded median and tail tolerances. A mismatch blocks that
candidate; an unavailable check is disclosed rather than presented as a pass.
For USD/KRW, FRED's New York noon fixing and Yahoo's market snapshot use
different daily fix times, so the comparison uses a wider but bounded return
tolerance together with a 3% median level-ratio guard. The level guard rejects
inversion and 100x unit errors even when scaled returns would otherwise match.

Incremental refreshes may rebase newly appended adjusted-price levels when
overlapping returns remain stable. A genuine historical return revision may not
be spliced into frozen rows: it requires an explicit, reviewed `--backfill` for
the affected asset or date range.

## Optional live Worker

The Cloudflare Worker and Twelve Data remain a separate, optional live
exploration path. They are not prerequisites for scheduled static refreshes.
The Worker stays `unavailable` unless its server-side key and external-display
approval are both configured, bounds requests to five calendar years and 5,000
observations, returns only normalized contracts, and never caches errors. See
[Twelve Data external-display plans](https://twelvedata.com/pricing-business),
[Twelve Data attribution](https://support.twelvedata.com/en/articles/12647398-attribution-guidelines-for-using-twelve-data),
[Cloudflare Workers Fetch](https://developers.cloudflare.com/workers/runtime-apis/fetch/),
and [Cloudflare Cache API](https://developers.cloudflare.com/workers/runtime-apis/cache/).
