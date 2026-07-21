import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  leveragedReturnPath,
  performanceMetrics,
  singleAssetKelly,
} from "../../site/assets/engine.js";

const fixture = JSON.parse(
  await readFile(new URL("../fixtures/golden.json", import.meta.url), "utf8"),
);
const close = (actual, expected, tolerance = 1e-12) =>
  assert.ok(Math.abs(actual - expected) <= tolerance, `${actual} != ${expected}`);

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
