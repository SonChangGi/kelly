# Methodology

Daily simple return is `P_t / P_(t-1) - 1`. Cumulative return is the compounded product minus one. CAGR uses elapsed calendar time. Volatility annualizes the sample standard deviation with the disclosed observations-per-year convention. Sharpe uses annualized arithmetic excess return divided by annualized volatility; Sortino replaces volatility with downside deviation against the chosen minimum acceptable return. Maximum drawdown is reported as a positive loss magnitude, `1 - min(wealth / prior peak)`, and Calmar is CAGR divided by that magnitude. The drawdown chart separately plots drawdowns at or below zero. Undefined denominators remain unavailable.

For a single risky asset under the GBM approximation, full Kelly leverage is expected excess return divided by variance. Expected log growth at leverage `f` is approximately `r_f + f(mu-r_f) - 0.5 f^2 sigma^2`, before any borrowing spread above one-times exposure. Full Kelly maximizes the stated model, not real-world certainty. Fractional Kelly, leverage caps, estimation error, costs, taxes, and path-dependent ruin risk must be shown beside the result.

The exact historical solver maximizes the selected period's average in-sample
`log(1 + r_f,d + f(r_t-r_f,d) - max(f-1,0)spread_d)` only where every
wealth factor is positive. Annual rates are converted to effective daily rates,
and the daily borrowing drag is the borrowing rate minus the cash rate. Crossing
zero is `ruin`, not a large negative number. A two-times comparison must show
both the actual mapped ETF path when available and a clearly labeled synthetic
daily-target path; their fees, financing, tracking, and compounding differ.

Rebalancing drag is measured by comparing wealth paths under the same underlying returns: buy-and-hold, target-weight rebalancing at a disclosed frequency, and the same policy after turnover costs. The chart reports gross-versus-net CAGR, turnover, cost drag, and volatility pumping separately. Results depend on return sequence, correlation, rebalance timing, and transaction-cost assumptions.
