import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";
import { Window } from "happy-dom";

import {
  exactHistoricalKelly,
  leveragedReturnPath,
  performanceMetrics,
  portfolioKelly,
  rebalanceComparison,
  singleAssetKelly,
} from "../../site/assets/engine.js";

const window = new Window({ url: "https://sonchanggi.github.io/kelly/" });
globalThis.__KELLY_APP_TEST__ = true;
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: window.navigator });
Object.defineProperty(globalThis, "location", { configurable: true, value: window.location });
globalThis.HTMLElement = window.HTMLElement;
globalThis.HTMLCanvasElement = window.HTMLCanvasElement;

const { testSupport } = await import("../../site/assets/app.js");

const fixture = JSON.parse(
  await readFile(new URL("../fixtures/golden.json", import.meta.url), "utf8"),
);
const close = (actual, expected, tolerance = 1e-12) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);
const closeArray = (actual, expected, tolerance = 1e-12) => {
  assert.equal(actual.length, expected.length);
  actual.forEach((value, index) => close(value, expected[index], tolerance));
};

test("browser engine matches the shared Python/JS GBM golden fixture", () => {
  const result = singleAssetKelly(fixture.gbm.inputs);
  const expected = fixture.gbm.expected;
  close(result.theoreticalFullKelly, expected.theoreticalFraction);
  close(result.maximumLogGrowth, expected.fullLogGrowth);
  close(result.maximumAnnualGrowth, expected.fullGeometricReturn);
  close(result.twiceLogGrowth, expected.twoXLogGrowth);
  close(result.twiceAnnualGrowth, expected.twoXGeometricReturn);
  close(result.twiceArithmeticWealthReturn, expected.twoXArithmeticReturn);
  const half = result.presets.find((preset) => preset.fraction === 0.5);
  close(
    (half.logGrowth - fixture.gbm.inputs.riskFreeRate) /
      (result.maximumLogGrowth - fixture.gbm.inputs.riskFreeRate),
    expected.halfFractionOfMaximumExcessGrowth,
  );
});

test("browser engine matches the shared MDD and ruin fixture", () => {
  const { prices, dates, expected } = fixture.metrics;
  const returns = prices.slice(1).map((price, index) => price / prices[index] - 1);
  const metrics = performanceMetrics(returns, dates);
  close(metrics.cumulativeReturn.value, expected.cumulativeReturn);
  close(metrics.maxDrawdown.value, expected.maximumDrawdown);

  const ruin = leveragedReturnPath(fixture.ruin.returns, fixture.ruin.leverage);
  assert.equal(ruin.status, fixture.ruin.expectedStatus);
});

test("browser engine matches the shared financed exact historical fixture", () => {
  const { inputs, expected } = fixture.exactHistorical;
  const returns = Array.from(
    { length: inputs.returnPattern.length * inputs.repetitions },
    (_, index) => inputs.returnPattern[index % inputs.returnPattern.length],
  );
  const result = exactHistoricalKelly(returns, {
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
    annualizationDays: inputs.annualizationDays,
    cap: inputs.cap,
    searchCap: inputs.searchCap,
  });

  assert.equal(result.status, expected.status);
  close(result.theoreticalLeverage, expected.theoreticalFraction, 1e-6);
  close(result.appliedLeverage, expected.appliedFraction);
  close(result.annualLogGrowth, expected.annualLogGrowth);
  close(result.appliedAnnualLogGrowth, expected.appliedAnnualLogGrowth);
  close(result.appliedAnnualGrowth, expected.appliedAnnualGrowth);
});

test("browser engine matches the shared two-asset theory and long-only cap fixture", () => {
  const { inputs, expected } = fixture.multiAssetGbm;
  const result = portfolioKelly(inputs);

  assert.equal(result.status, expected.status);
  closeArray(result.theoreticalWeights, expected.theoreticalWeights);
  close(result.theoreticalTotalExposure, expected.theoreticalTotalExposure);
  close(result.theoreticalLogGrowth, expected.theoreticalAnnualLogGrowth);
  closeArray(result.appliedWeights, expected.appliedWeights, 1e-9);
  close(result.totalExposure, expected.appliedTotalExposure);
  close(result.logGrowth, expected.appliedAnnualLogGrowth);
  close(result.annualGrowth, expected.appliedAnnualGrowth);
});

test("browser engine keeps the shared constrained result for singular covariance", () => {
  const { inputs, expected } = fixture.singularMultiAssetGbm;
  const result = portfolioKelly(inputs);

  assert.equal(result.status, expected.status);
  assert.equal(result.reasonCode, expected.reason);
  assert.equal(result.theoreticalWeights, expected.theoreticalWeights);
  assert.equal(result.theoreticalTotalExposure, expected.theoreticalTotalExposure);
  assert.equal(result.theoreticalLogGrowth, expected.theoreticalAnnualLogGrowth);
  closeArray(result.appliedWeights, expected.appliedWeights, 1e-6);
  close(result.totalExposure, expected.appliedTotalExposure, 1e-6);
  close(result.logGrowth, expected.appliedAnnualLogGrowth);
  close(result.annualGrowth, expected.appliedAnnualGrowth);
});

test("browser path matches the shared prior-only five-day FX fixture", () => {
  const { inputs, expected } = fixture.fxPrior;
  const aligned = testSupport.alignPreviousFx(
    inputs.assetDates,
    inputs.fxDates,
    inputs.fxRates,
    inputs.maxLagDays,
  );
  assert.deepEqual(aligned, expected.alignedRates);

  const converted = testSupport.seriesFromPayload(
    {
      state: "published",
      assetId: "golden-usd",
      metadata: { symbol: "USD asset", baseCurrency: "USD", returnBasis: "price_return" },
      dates: inputs.assetDates,
      prices: inputs.assetPrices,
      returns: [],
    },
    "krw",
    {
      state: "published",
      assetId: "fx-usd-krw",
      metadata: { symbol: "USD/KRW", baseCurrency: "KRW", returnBasis: "fx_rate" },
      dates: inputs.fxDates,
      prices: inputs.fxRates,
      returns: [],
    },
  );
  closeArray(converted.prices, expected.convertedPrices);
});

test("browser engine matches the shared rebalancing paths and drag fixture", () => {
  const { inputs, expected } = fixture.rebalancing;
  const returnsByAsset = inputs.targetWeights.map((_, assetIndex) =>
    inputs.returnsMatrix.map((row) => row[assetIndex]),
  );
  const result = rebalanceComparison({
    returnsByAsset,
    dates: inputs.dates,
    targetWeights: inputs.targetWeights,
    frequency: inputs.frequency,
    transactionCostBps: inputs.oneWayCostBps,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
    annualizationDays: inputs.annualizationDays,
  });

  assert.equal(result.status, expected.status);
  closeArray(result.buyAndHold.wealth, expected.buyAndHoldWealth);
  closeArray(result.gross.wealth, expected.grossWealth);
  closeArray(result.net.wealth, expected.netWealth);
  close(result.grossRebalancingEffect, expected.grossRebalancingEffect);
  close(result.transactionCostDrag, expected.tradingCostDrag);
  close(result.netRebalancingEffect, expected.netRebalancingEffect);
  close(result.turnover, expected.turnover);
  close(result.net.totalCost, expected.tradingCostPaid);
  assert.equal(result.net.rebalanceCount, expected.rebalanceCount);
});

test("browser engine matches the shared per-asset negative-multiplier ruin boundary", () => {
  const { inputs, expectedStatus } = fixture.rebalancingRuin;
  const returnsByAsset = inputs.targetWeights.map((_, assetIndex) =>
    inputs.returnsMatrix.map((row) => row[assetIndex]),
  );
  const result = rebalanceComparison({
    returnsByAsset,
    dates: inputs.dates,
    targetWeights: inputs.targetWeights,
    frequency: inputs.frequency,
    transactionCostBps: inputs.oneWayCostBps,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
    annualizationDays: inputs.annualizationDays,
  });

  assert.equal(result.status, expectedStatus);
  assert.equal(result.reasonCode, expectedStatus);
});
