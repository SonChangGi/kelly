import test from "node:test";
import assert from "node:assert/strict";

import {
  REASON,
  STATUS,
  applyExplorationRange,
  createPeriodState,
  exactHistoricalKelly,
  leveragedReturnPath,
  normalizeAssetPayload,
  performanceMetrics,
  portfolioKelly,
  rebalanceComparison,
  rowsToCsv,
  setExplorationRange,
  simulateRebalancing,
  singleAssetKelly,
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

test("zero volatility and zero downside deviation use reason codes instead of fabricated ratios", () => {
  const metrics = performanceMetrics([0.01, 0.01, 0.01], ["2024-01-01", "2024-01-02", "2024-01-03", "2024-01-04"]);
  assert.equal(metrics.sharpe.value, null);
  assert.equal(metrics.sharpe.reasonCode, REASON.ZERO_VOLATILITY);
  assert.equal(metrics.sortino.value, null);
  assert.equal(metrics.sortino.reasonCode, REASON.ZERO_DOWNSIDE_DEVIATION);
  assert.equal(metrics.calmar.value, null);
  assert.equal(metrics.calmar.reasonCode, REASON.ZERO_MAX_DRAWDOWN);
});

test("historical exact Kelly recovers the 20% even-money binary optimum", () => {
  const returns = Array.from({ length: 10 }, (_, index) => index < 6 ? 1 : -1);
  const result = exactHistoricalKelly(returns);
  assert.equal(result.status, STATUS.PUBLISHED);
  close(result.theoreticalLeverage, 0.2, 1e-6);
});

test("daily -60% at 2x is ruin and never clipped", () => {
  const result = leveragedReturnPath([-0.6], 2);
  assert.equal(result.status, STATUS.RUIN);
  assert.equal(result.reasonCode, REASON.RUIN);
  assert.ok(result.wealth.at(-1) <= 0);
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

test("rebalancing restores targets, records turnover, and separates cost drag", () => {
  const inputs = {
    returnsByAsset: [[0.1, -0.05, 0.04], [-0.02, 0.08, -0.01]],
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

test("CSV escaping preserves commas, quotes, and newlines", () => {
  assert.equal(rowsToCsv(["a", "b"], [["x,y", 'a"b'], ["line\nbreak", 1]]), 'a,b\n"x,y","a""b"\n"line\nbreak",1');
});
