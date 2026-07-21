import test from "node:test";
import assert from "node:assert/strict";

import {
  REASON,
  STATUS,
  annualRateToDaily,
  applyExplorationRange,
  continuousGrowthRate,
  createPeriodState,
  exactHistoricalKelly,
  estimateHistoricalMoments,
  innerJoinReturnSeries,
  leveragedReturnPath,
  normalizeAssetPayload,
  performanceMetrics,
  periodicFinancingSpread,
  portfolioKelly,
  rebalanceComparison,
  rowsToCsv,
  setExplorationRange,
  simulateRebalancing,
  singleAssetKelly,
  sliceJoinedReturnSeries,
  validateCorrelationMatrix,
} from "../../site/assets/engine.js";

const close = (actual, expected, tolerance = 1e-9) => assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);

test("GBM Kelly golden values distinguish log growth and arithmetic wealth return", () => {
  const result = singleAssetKelly({
    expectedExcessReturn: 0.06,
    volatility: 0.2,
    riskFreeRate: 0.02,
    borrowingSpread: 0,
  });
  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.theoreticalFullKelly, 1.5);
  close(result.appliedFullKelly, 1.5);
  close(result.maximumLogGrowth, 0.065);
  close(result.twiceLogGrowth, 0.06);
  close(result.maximumAnnualGrowth, Math.exp(0.065) - 1);
  close(result.twiceArithmeticWealthReturn, Math.exp(0.14) - 1);
  const half = result.presets.find((preset) => preset.fraction === 0.5);
  close((half.logGrowth - 0.02) / (result.maximumLogGrowth - 0.02), 0.75);
});

test("performance metrics return positive MDD magnitude but a negative drawdown path", () => {
  const result = performanceMetrics([0.2, -0.25], ["2024-01-01", "2024-01-02", "2024-01-03"]);
  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.maxDrawdown.value, 0.25);
  close(Math.min(...result.drawdowns), -0.25);
  close(result.cumulativeReturn.value, -0.1);
  assert.equal(result.elapsedDays, 2);
});

test("N return dates annualize over N periods while N+1 price dates use calendar elapsed time", () => {
  const returns = [0.01, 0.01];
  const returnDated = performanceMetrics(returns, ["2024-01-01", "2025-01-01"]);
  const priceDated = performanceMetrics(returns, ["2024-01-01", "2024-01-02", "2025-01-01"]);

  close(returnDated.cagr.value, 1.0201 ** (252 / 2) - 1);
  close(priceDated.cagr.value, 1.0201 ** (365.2425 / 366) - 1);
});

test("performance metrics reject malformed, mismatched, duplicate, and unsorted dates", () => {
  const returns = [0.1, -0.05];
  const invalidDates = [
    ["2024-01-01"],
    ["2024-02-30", "2024-03-01"],
    ["2024-01-01", "2024-01-01"],
    ["2024-01-03", "2024-01-02", "2024-01-10"],
  ];
  for (const dates of invalidDates) {
    const result = performanceMetrics(returns, dates);
    assert.equal(result.status, STATUS.UNAVAILABLE);
    assert.equal(result.reasonCode, REASON.INVALID_DATES);
  }
});

test("one-observation metric mode keeps valid partial results unavailable where undefined", () => {
  const result = performanceMetrics([0.01], ["2024-01-01", "2024-01-02"], { minObservations: 1 });
  assert.equal(result.status, STATUS.PUBLISHED);
  assert.equal(result.annualVolatility.value, null);
  assert.equal(result.annualVolatility.reasonCode, REASON.INSUFFICIENT_OBSERVATIONS);
  assert.equal(result.sharpe.value, null);
  assert.equal(result.sharpe.reasonCode, REASON.INSUFFICIENT_OBSERVATIONS);
});

test("zero volatility and zero downside deviation use reason codes instead of fabricated ratios", () => {
  const metrics = performanceMetrics([0.01, 0.01, 0.01], ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]);
  assert.equal(metrics.sharpe.value, null);
  assert.equal(metrics.sharpe.reasonCode, REASON.ZERO_VOLATILITY);
  assert.equal(metrics.sortino.value, null);
  assert.equal(metrics.sortino.reasonCode, REASON.ZERO_DOWNSIDE_DEVIATION);
  assert.equal(metrics.calmar.value, null);
  assert.equal(metrics.calmar.reasonCode, REASON.ZERO_MAX_DRAWDOWN);
});

test("single-asset Kelly rejects negative borrowing cost and an out-of-contract cap", () => {
  assert.equal(singleAssetKelly({
    expectedExcessReturn: 0.06,
    volatility: 0.2,
    borrowingSpread: -0.01,
  }).reasonCode, REASON.INVALID_RATE);
  assert.equal(singleAssetKelly({
    expectedExcessReturn: 0.06,
    volatility: 0.2,
    cap: 3.1,
  }).reasonCode, REASON.INVALID_LEVERAGE_CAP);
  assert.equal(continuousGrowthRate({
    leverage: 2,
    expectedExcessReturn: 0.06,
    volatility: 0.2,
    borrowingSpread: -0.01,
  }), null);
});

test("single-asset Kelly distinguishes negative from zero or tiny volatility", () => {
  assert.equal(singleAssetKelly({ expectedExcessReturn: 0.06, volatility: -0.2 }).reasonCode, REASON.INVALID_RETURN);
  for (const volatility of [0, 1e-13]) {
    const result = singleAssetKelly({ expectedExcessReturn: 0.06, volatility });
    assert.equal(result.status, STATUS.UNAVAILABLE);
    assert.equal(result.reasonCode, REASON.ZERO_VOLATILITY);
  }
});

test("performance metrics fail closed for invalid rate and annualization inputs", () => {
  const returns = [0.01, -0.01];
  assert.equal(performanceMetrics(returns, [], { riskFreeRate: -1 }).status, STATUS.UNAVAILABLE);
  assert.equal(performanceMetrics(returns, [], { riskFreeRate: -1 }).reasonCode, REASON.INVALID_RATE);
  assert.equal(performanceMetrics(returns, [], { mar: -1 }).status, STATUS.UNAVAILABLE);
  assert.equal(performanceMetrics(returns, [], { mar: -1 }).reasonCode, REASON.INVALID_RATE);
  assert.equal(performanceMetrics(returns, [], { annualizationDays: 0 }).status, STATUS.UNAVAILABLE);
  assert.equal(performanceMetrics(returns, [], { annualizationDays: 0 }).reasonCode, REASON.INVALID_RATE);
});

test("historical exact Kelly recovers the 20% even-money binary optimum", () => {
  const returns = Array.from({ length: 10 }, (_, index) => index < 6 ? 1 : -1);
  const result = exactHistoricalKelly(returns);
  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.theoreticalLeverage, 0.2, 1e-6);
});

test("historical exact Kelly applies borrowing spread above one-times exposure", () => {
  const returns = Array.from({ length: 200 }, (_, index) => index % 2 === 0 ? 0.01 : -0.009);
  const withoutSpread = exactHistoricalKelly(returns, { riskFreeRate: 0.02 });
  const withSpread = exactHistoricalKelly(returns, {
    riskFreeRate: 0.02,
    borrowingSpread: 0.10,
  });

  assert.ok(withoutSpread.theoreticalLeverage > 4.6);
  close(withSpread.theoreticalLeverage, 1, 1e-6);
  assert.ok(withSpread.annualLogGrowth < withoutSpread.annualLogGrowth);
});

test("historical exact Kelly reports a search-bound result as degraded", () => {
  const result = exactHistoricalKelly(Array(60).fill(0.01));

  assert.equal(result.status, STATUS.DEGRADED);
  assert.equal(result.reasonCode, REASON.SEARCH_BOUND_REACHED);
  close(result.theoreticalLeverage, 100, 1e-8);
  assert.equal(result.appliedLeverage, 3);
});

test("historical exact Kelly rejects an applied cap above the v1 3x limit", () => {
  const result = exactHistoricalKelly(
    Array.from({ length: 100 }, (_, index) => index % 2 ? -0.009 : 0.012),
    { cap: 4 },
  );
  assert.equal(result.status, STATUS.UNAVAILABLE);
  assert.equal(result.reasonCode, REASON.INVALID_LEVERAGE_CAP);
});

test("historical exact Kelly distinguishes invalid rates from non-finite inputs", () => {
  const returns = [0.01, -0.005];
  assert.equal(exactHistoricalKelly(returns, { borrowingSpread: -0.01 }).reasonCode, REASON.INVALID_RATE);
  assert.equal(exactHistoricalKelly(returns, { riskFreeRate: -1 }).reasonCode, REASON.INVALID_RATE);
  assert.equal(exactHistoricalKelly(returns, { annualizationDays: 0 }).reasonCode, REASON.INVALID_RATE);
});

test("daily -60% at 2x is ruin and never clipped", () => {
  const result = leveragedReturnPath([-0.6], 2);
  assert.equal(result.status, STATUS.RUIN);
  assert.equal(result.reasonCode, REASON.RUIN);
  assert.ok(result.wealth.at(-1) <= 0);
});

test("synthetic leveraged path can include borrowing spread without changing old calls", () => {
  const legacy = leveragedReturnPath([0], 2, 0, 252);
  const withSpread = leveragedReturnPath([0], 2, 0, 252, 0.10);

  close(legacy.returns[0], 0);
  close(withSpread.returns[0], -annualRateToDaily(0.10), 1e-12);
  assert.ok(withSpread.wealth.at(-1) < legacy.wealth.at(-1));
});

test("periodic financing spread is borrowing rate minus cash rate when risk-free is nonzero", () => {
  const spread = periodicFinancingSpread(0.05, 0.10, 252);
  const expected = annualRateToDaily(0.15) - annualRateToDaily(0.05);
  close(spread, expected, 1e-14);
  const path = leveragedReturnPath([0], 2, 0.05, 252, 0.10);
  close(path.returns[0], 2 * 0 - annualRateToDaily(0.15), 1e-14);
  const exact = exactHistoricalKelly(Array.from({ length: 60 }, (_, index) => index % 2 ? -0.005 : 0.007), {
    riskFreeRate: 0.05,
    borrowingSpread: 0.10,
  });
  assert.ok([STATUS.PUBLISHED, STATUS.DEGRADED].includes(exact.status));
});

test("official date state changes only after explicit apply and invalid exploration preserves it", () => {
  const initial = createPeriodState("2020-01-01", "2024-12-31");
  const explored = setExplorationRange(initial, "2022-01-01", "2023-12-31");
  assert.deepEqual(explored.official, initial.official);
  assert.deepEqual(explored.exploration, { start: "2022-01-01", end: "2023-12-31" });
  const applied = applyExplorationRange(explored);
  assert.deepEqual(applied.official, explored.exploration);
  const invalid = setExplorationRange(applied, "2024-01-01", "2023-01-01");
  assert.deepEqual(invalid.official, applied.official);
  assert.equal(invalid.error, REASON.INVALID_RANGE);
});

test("columnar asset contract drops leading null return and keeps N observation dates", () => {
  const series = normalizeAssetPayload({
    state: "published",
    assetId: "etf-spy",
    metadata: { symbol: "SPY", baseCurrency: "USD", returnBasis: "adjusted_total_return_approx" },
    dates: ["2024-01-01", "2024-01-02", "2024-01-03"],
    prices: [100, 101, 99],
    returns: [null, 0.01, -2 / 101],
  });
  assert.equal(series.status, STATUS.PUBLISHED);
  assert.equal(series.dates.length, 3);
  assert.deepEqual(series.returnDates, ["2024-01-02", "2024-01-03"]);
  assert.deepEqual(series.returns, [0.01, -2 / 101]);
  assert.equal(series.returnBasis, "adjusted_total_return_approx");
});

test("correlation validation rejects asymmetric and non-PSD matrices", () => {
  assert.equal(validateCorrelationMatrix([[1, 0.5], [0.4, 1]]).reasonCode, REASON.INVALID_CORRELATION);
  const nonPsd = [[1, 0.9, 0.9], [0.9, 1, -0.9], [0.9, -0.9, 1]];
  assert.equal(validateCorrelationMatrix(nonPsd).reasonCode, REASON.NON_PSD_CORRELATION);
});

test("portfolio solver shows unconstrained diagnostics and respects long-only 3x exposure", () => {
  const result = portfolioKelly({
    expectedExcessReturns: [0.04, 0.02],
    volatilities: [0.2, 0.1],
    correlation: [[1, 0], [0, 1]],
  });
  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.theoreticalWeights[0], 1);
  close(result.theoreticalWeights[1], 2);
  close(result.totalExposure, 3, 1e-8);
  assert.ok(result.appliedWeights.every((weight) => weight >= 0));
});

test("portfolio solver preserves stable rate and leverage-cap reason codes", () => {
  const inputs = {
    expectedExcessReturns: [0.06, 0.03],
    volatilities: [0.2, 0.1],
    correlation: [[1, 0], [0, 1]],
  };
  assert.equal(portfolioKelly({ ...inputs, borrowingSpread: -0.01 }).reasonCode, REASON.INVALID_RATE);
  assert.equal(portfolioKelly({ ...inputs, cap: 4 }).reasonCode, REASON.INVALID_LEVERAGE_CAP);
});

test("portfolio solver fails closed for zero, tiny, and negative input volatility", () => {
  const inputs = {
    expectedExcessReturns: [0.06, 0.03],
    correlation: [[1, 0], [0, 1]],
  };
  for (const volatility of [0, 1e-13]) {
    const result = portfolioKelly({ ...inputs, volatilities: [0.2, volatility] });
    assert.equal(result.status, STATUS.UNAVAILABLE);
    assert.equal(result.reasonCode, REASON.ZERO_VOLATILITY);
  }
  assert.equal(
    portfolioKelly({ ...inputs, volatilities: [0.2, -0.1] }).reasonCode,
    REASON.INVALID_RETURN,
  );
});

test("multi-asset borrowing spread penalizes exposure above one in solver and growth diagnostics", () => {
  const inputs = {
    expectedExcessReturns: [0.08, 0],
    volatilities: [0.2, 0.2],
    correlation: [[1, 0], [0, 1]],
    riskFreeRate: 0.02,
  };
  const free = portfolioKelly(inputs);
  const financed = portfolioKelly({ ...inputs, borrowingSpread: 0.06 });

  close(free.totalExposure, 2, 1e-7);
  close(financed.totalExposure, 1, 1e-7);
  close(financed.theoreticalTotalExposure, 2);
  close(financed.theoreticalLogGrowth, 0.04);
  close(financed.logGrowth, 0.08);
  assert.ok(financed.totalExposure < free.totalExposure);
});

test("historical portfolio series use a strict common-date inner join and require 60 observations", () => {
  const dates = Array.from({ length: 62 }, (_, index) => new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10));
  const left = { returnDates: dates.slice(0, 61), returns: dates.slice(0, 61).map((_, index) => index / 10000) };
  const right = { returnDates: dates.slice(1), returns: dates.slice(1).map((_, index) => -index / 12000) };
  const joined = innerJoinReturnSeries([left, right], 60);

  assert.equal(joined.status, STATUS.PUBLISHED);
  assert.equal(joined.dates.length, 60);
  assert.equal(joined.dates[0], dates[1]);
  assert.equal(joined.returnsByAsset[0][0], left.returns[1]);
  assert.equal(innerJoinReturnSeries([left, right], 61).reasonCode, REASON.NO_COMMON_RETURNS);
});

test("joined historical returns reject invalid or sub-60 requested ranges without altering the source", () => {
  const dates = Array.from({ length: 80 }, (_, index) => new Date(Date.UTC(2024, 0, index + 1)).toISOString().slice(0, 10));
  const joined = { status: STATUS.PUBLISHED, dates, returnsByAsset: [dates.map(() => 0.001), dates.map(() => 0.002)] };
  assert.equal(sliceJoinedReturnSeries(joined, dates[10], dates[69], 60).dates.length, 60);
  assert.equal(sliceJoinedReturnSeries(joined, dates[0], dates[20], 60).reasonCode, REASON.NO_COMMON_RETURNS);
  assert.equal(sliceJoinedReturnSeries(joined, dates[20], dates[0], 60).reasonCode, REASON.INVALID_RANGE);
  assert.equal(joined.dates.length, 80);
});

test("historical moments annualize arithmetic excess returns, covariance, volatility, and correlation", () => {
  const result = estimateHistoricalMoments(
    [[0.01, -0.01, 0.02], [0.02, 0, -0.01]],
    { annualizationDays: 12, riskFreeRate: 0.02 },
  );

  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.expectedArithmeticReturns[0], 0.08);
  close(result.expectedExcessReturns[0], 0.06);
  close(result.covariance[0][0], 0.0028);
  close(result.volatilities[0], Math.sqrt(0.0028));
  close(result.correlation[0][1], -1 / 7);
  assert.deepEqual(result.correlation.map((row, index) => row[index]), [1, 1]);
});

test("rebalancing restores targets, records turnover, and separates cost drag", () => {
  const inputs = {
    returnsByAsset: [[0.1, -0.05, 0], [-0.02, 0.08, 0]],
    dates: ["2024-01-02", "2024-01-03", "2024-01-04"],
    targetWeights: [0.6, 0.4],
    frequency: "daily",
    transactionCostBps: 10,
  };
  const simulated = simulateRebalancing(inputs);
  assert.equal(simulated.status, STATUS.PUBLISHED);
  close(simulated.endingWeights[0], 0.6, 1e-10);
  close(simulated.endingWeights[1], 0.4, 1e-10);
  assert.ok(simulated.turnover > 0);
  const comparison = rebalanceComparison(inputs);
  assert.equal(comparison.status, STATUS.PUBLISHED);
  assert.ok(comparison.transactionCostDrag >= 0);
  close(comparison.grossRebalancingEffect - comparison.transactionCostDrag, comparison.netRebalancingEffect, 1e-10);
});

test("rebalancing rejects short, over-cap, and unsupported frequency inputs", () => {
  const base = {
    returnsByAsset: [[0.01], [0.01]],
    dates: ["2024-01-02"],
    targetWeights: [0.5, 0.5],
  };
  assert.equal(simulateRebalancing({ ...base, targetWeights: [-0.1, 1.1] }).status, STATUS.UNAVAILABLE);
  assert.equal(simulateRebalancing({ ...base, targetWeights: [2, 2] }).status, STATUS.UNAVAILABLE);
  assert.equal(simulateRebalancing({ ...base, frequency: "hourly" }).status, STATUS.UNAVAILABLE);
});

test("rebalancing does not trade after the final observation", () => {
  const result = simulateRebalancing({
    returnsByAsset: [[0, 0.2], [0, 0]],
    dates: ["2024-01-02", "2024-01-03"],
    targetWeights: [0.5, 0.5],
    frequency: "daily",
    transactionCostBps: 10,
  });

  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.totalCost, 0);
  close(result.turnover, 0);
  assert.ok(result.endingWeights[0] > 0.5);
});

test("rebalancing effects are CAGR differences and N return dates use N annualized periods", () => {
  const inputs = {
    returnsByAsset: [[0.2, 0], [0, 0]],
    dates: ["2024-01-02", "2024-01-03"],
    targetWeights: [0.5, 0.5],
    frequency: "daily",
    transactionCostBps: 0,
  };
  const comparison = rebalanceComparison(inputs);
  const expectedBuyAndHoldCagr = comparison.buyAndHold.endingWealth ** (252 / 2) - 1;

  assert.ok(Math.abs(comparison.buyAndHold.cagr / expectedBuyAndHoldCagr - 1) < 1e-12);
  close(
    comparison.grossRebalancingEffect,
    comparison.gross.cagr - comparison.buyAndHold.cagr,
  );
  close(
    comparison.netRebalancingEffect,
    comparison.net.cagr - comparison.buyAndHold.cagr,
  );
});

test("N plus one rebalancing dates use the full calendar span for CAGR", () => {
  const result = simulateRebalancing({
    returnsByAsset: [[0.10]],
    dates: ["2023-01-01", "2024-01-01"],
    targetWeights: [1],
    frequency: "none",
  });
  const expected = 1.10 ** (365.2425 / 365) - 1;

  close(result.cagr, expected, 1e-12);
});

test("CSV escaping preserves commas, quotes, and newlines", () => {
  assert.equal(rowsToCsv(["a", "b"], [["x,y", 'a"b'], ["line\nbreak", 1]]), 'a,b\n"x,y","a""b"\n"line\nbreak",1');
});
