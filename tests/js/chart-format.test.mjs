import test from "node:test";
import assert from "node:assert/strict";
import { Window } from "happy-dom";

const window = new Window({ url: "https://sonchanggi.github.io/kelly/" });
globalThis.window = window;
globalThis.document = window.document;
globalThis.HTMLElement = window.HTMLElement;
globalThis.HTMLCanvasElement = window.HTMLCanvasElement;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: window.navigator });

const {
  formatChartLeverage,
  formatChartNumber,
  formatChartPercent,
} = await import("../../site/assets/charts.js");

test("chart numbers use at most two decimals without unnecessary trailing zeroes", () => {
  assert.equal(formatChartNumber(95.49758150407747), "95.5");
  assert.equal(formatChartNumber(95.4), "95.4");
  assert.equal(formatChartNumber(95), "95");
  assert.equal(formatChartNumber(1234.567), "1,234.57");
  assert.equal(formatChartNumber(-0), "0");
  assert.equal(formatChartNumber(null), "—");
  assert.equal(formatChartNumber(Number.NaN), "—");
});

test("percent and leverage chart units share the two-decimal rule", () => {
  assert.equal(formatChartPercent(0.054978151), "5.5%");
  assert.equal(formatChartPercent(0.05), "5%");
  assert.equal(formatChartLeverage(1.23456), "1.23×");
  assert.equal(formatChartLeverage(2), "2×");
});
