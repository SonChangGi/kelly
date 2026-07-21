import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const html = await readFile(new URL("../../site/index.html", import.meta.url), "utf8");
const css = await readFile(new URL("../../site/assets/styles.css", import.meta.url), "utf8");
const app = await readFile(new URL("../../site/assets/app.js", import.meta.url), "utf8");
const charts = await readFile(new URL("../../site/assets/charts.js", import.meta.url), "utf8");

test("three analysis modes and official/exploration controls are independently addressable", () => {
  for (const id of ["historical-mode", "direct-mode", "portfolio-mode", "official-start", "official-end", "apply-exploration"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(html, /탐색값은 공식 카드에 미반영|공식 결과는 적용 후/);
  assert.match(app, /setExplorationRange/);
  assert.match(app, /applyExplorationRange/);
});

test("chart runtime is local and the wealth chart uses a dual-ended slider zoom", () => {
  assert.match(charts, /\.\/vendor\/echarts\.esm\.min\.js/);
  assert.doesNotMatch(html, /cdn|unpkg|jsdelivr/i);
  assert.match(charts, /type: "slider"/);
  assert.match(charts, /startValue/);
  assert.match(charts, /endValue/);
});

test("mobile layout prevents page-level horizontal overflow while tables own their scroll", () => {
  assert.match(css, /body\s*\{[^}]*overflow-x:\s*hidden/s);
  assert.match(css, /\.table-scroll\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(css, /@media \(max-width: 390px\)/);
});

test("arithmetic return and geometric growth are visibly distinct and actual leveraged ETF absence is explicit", () => {
  assert.match(html, /기대 산술 자산수익률/);
  assert.match(html, /장기 기하성장률/);
  assert.match(app, /실제 일간목표 2배 ETF/);
  assert.match(app, /unavailable/);
});
