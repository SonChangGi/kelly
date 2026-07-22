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
  assert.match(charts, /aria:\s*\{\s*enabled:\s*true/);
  assert.match(charts, /chart\.off\("datazoom"\)/);
});

test("static modules and styles share one release cache generation", () => {
  const appVersion = html.match(/assets\/app\.js\?v=([0-9.]+)/)?.[1];
  const styleVersion = html.match(/assets\/styles\.css\?v=([0-9.]+)/)?.[1];
  const engineVersion = app.match(/\.\/engine\.js\?v=([0-9.]+)/)?.[1];
  const chartsVersion = app.match(/\.\/charts\.js\?v=([0-9.]+)/)?.[1];

  assert.ok(appVersion);
  assert.equal(styleVersion, appVersion);
  assert.equal(engineVersion, appVersion);
  assert.equal(chartsVersion, appVersion);
});

test("historical assets use an accessible typed ticker combobox instead of a fixed select", () => {
  assert.match(html, /id="asset-input"[^>]*role="combobox"[^>]*aria-autocomplete="list"[^>]*aria-controls="asset-options"/);
  assert.match(html, /id="asset-options"[^>]*role="listbox"/);
  assert.match(html, /id="asset-submit"[^>]*>분석<\/button>/);
  assert.doesNotMatch(html, /id="asset-select"/);
  assert.match(app, /function matchingCatalogAssets/);
  assert.match(app, /\.\/data\/dynamic-catalog\.json/);
  assert.match(app, /kelly-dynamic-asset-catalog/);
  assert.match(app, /workerEndpoint\("\/v1\/search"/);
  assert.match(app, /workerHealthSupportsCapability\(payload, "usHistory"\)/);
  assert.match(app, /pathname === "\/v1\/fx" \? "fx" : "usHistory"/);
  assert.match(app, /event\.key === "ArrowDown" \|\| event\.key === "ArrowUp"/);
  assert.match(app, /event\.key === "Enter"/);
  assert.match(app, /event\.key === "Escape"/);
  assert.match(app, /role="option"/);
  assert.match(app, /data-history-index=.*role="combobox"/s);
  assert.match(css, /\.ticker-options\s*\{/);
  assert.match(css, /\.ticker-option\.is-active/);
});

test("every ECharts numeric surface uses the shared two-decimal formatter", () => {
  assert.match(charts, /maximumFractionDigits:\s*2/);
  assert.match(charts, /valueFormatter:\s*formatChartNumber/);
  assert.match(charts, /axisLabel:\s*\{[^}]*formatter:\s*formatChartNumber/s);
  assert.match(charts, /formatChartPercent/);
  assert.match(charts, /formatChartLeverage/);
  assert.doesNotMatch(charts, /\.toFixed\(/);
});

test("mobile layout prevents page-level horizontal overflow while tables own their scroll", () => {
  assert.match(css, /body\s*\{[^}]*overflow-x:\s*hidden/s);
  assert.match(css, /\.table-scroll\s*\{[^}]*overflow-x:\s*auto/s);
  assert.match(css, /@media \(max-width: 390px\)/);
});

test("arithmetic return and geometric growth are visibly distinct and actual leveraged ETF absence is explicit", () => {
  assert.match(html, /기대 산술 자산수익률/);
  assert.match(html, /<th>연환산 산술평균<\/th>/);
  assert.match(html, /장기 기하성장률/);
  assert.match(app, /실제 일간목표 2배 ETF/);
  assert.match(app, /syntheticMetrics\.annualArithmeticReturn/);
  assert.match(app, /annualizationDays:\s*inputs\.annualizationDays/);
  assert.match(app, /unavailable/);
});

test("empty state actions, quick periods, chart table and result insights are wired", () => {
  for (const id of ["historical-empty-state", "csv-upload", "csv-template-download", "wealth-period-end", "wealth-table-toggle", "wealth-data-table", "direct-result-insight", "portfolio-result-insight"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /importCsvFile/);
  assert.match(app, /downloadCsvTemplate/);
  assert.match(app, /applyQuickPeriod/);
  assert.match(app, /renderWealthDataTable/);
  assert.match(app, /direct-insight-title/);
  assert.match(app, /portfolio-insight-title/);
  assert.match(html, /data-period="3m"/);
  assert.doesNotMatch(html, /data-period="1m"/);
});

test("chart fallbacks, slider dates, and rebalance initial observations are explicit", () => {
  for (const id of ["drawdown-data-table", "growth-data-table", "rebalance-data-table", "direct-growth-data-table", "portfolio-rebalance-data-table"]) {
    assert.match(html, new RegExp(`id="${id}"`));
  }
  assert.match(app, /setAttribute\("aria-valuetext"/);
  assert.match(charts, /rebalanceAxisLabels/);
  assert.match(charts, /\["시작", \.\.\.dates\]/);
  assert.match(charts, /초기 1은 첫 수익률 전/);
});

test("async historical requests reject stale asset, currency, and 2x responses", () => {
  assert.match(app, /requestGeneration:\s*\{\s*asset:\s*0,\s*currency:\s*0,\s*leverage:\s*0\s*\}/);
  for (const type of ["asset", "currency", "leverage"]) {
    assert.match(app, new RegExp(`isCurrentRequest\\(\\"${type}\\"`));
  }
  assert.match(app, /state\.officialResult !== result/);
  assert.match(app, /initializePeriod\(converted, previousPeriod\)/);
});

test("result export and sharing disclose reproducibility boundaries", () => {
  assert.match(html, /공식 지표·Kelly 프리셋·재조정·2배 비교·일별수익률/);
  assert.match(html, /업로드 CSV 원문은 공유 URL에 포함되지 않습니다/);
  assert.match(app, /kelly_preset/);
  assert.match(app, /leverage_comparison/);
  assert.match(app, /daily_return/);
  assert.match(app, /업로드 CSV 원문은 URL에 포함되지 않아 공유 링크로 복원할 수 없습니다/);
  assert.match(app, /exportDirectCsv/);
  assert.match(app, /exportPortfolioCsv/);
});

test("invalid results and portfolio source changes clear stale charts", () => {
  assert.match(app, /clearHistoricalVisuals/);
  assert.match(app, /clearChart\(\$\("#direct-growth-chart"\)|renderGrowthCurve\(\$\("#direct-growth-chart"\), \[\], \[\]\)/);
  assert.match(app, /clearChart\(\$\("#weights-chart"\)/);
  assert.match(app, /clearChart\(\$\("#correlation-chart"\)/);
});

test("portfolio mode supports direct-first and historical 2-5 asset workflows", () => {
  for (const id of [
    "portfolio-direct-inputs",
    "portfolio-historical-inputs",
    "portfolio-history-assets",
    "portfolio-history-start",
    "portfolio-history-end",
    "portfolio-add-asset",
    "portfolio-asset-count",
    "portfolio-history-estimates",
    "portfolio-allocation-table",
    "portfolio-history-results",
    "portfolio-rebalance-summary",
    "portfolio-rebalance-chart",
  ]) assert.match(html, new RegExp(`id="${id}"`));
  assert.match(html, /data-portfolio-source="direct" class="is-active"/);
  assert.match(html, /data-portfolio-source="historical"/);
  assert.match(html, /id="portfolio-rebalance-frequency"[\s\S]*value="monthly" selected/);
  assert.match(html, /id="portfolio-transaction-cost"[^>]*value="10"/);
  assert.match(html, /id="portfolio-borrow-spread"[^>]*value="0"/);
  assert.match(app, /PORTFOLIO_MIN_ASSETS = 2/);
  assert.match(app, /PORTFOLIO_MAX_ASSETS = 5/);
  assert.match(app, /innerJoinReturnSeries\(series, 2\)/);
  assert.match(app, /sliceJoinedReturnSeries\(fullJoined, start, end, 60\)/);
  assert.match(app, /seriesForCurrency\(payload, "krw"\)/);
  assert.match(app, /estimateHistoricalMoments/);
  assert.match(app, /rebalanceComparison/);
});

test("Sortino MAR defaults to risk-free and degraded Exact Kelly remains visible", () => {
  assert.match(html, /id="sortino-mar"[^>]*placeholder="무위험률 연동"/);
  assert.doesNotMatch(html, /id="sortino-mar"[^>]*value=/);
  assert.match(app, /marValue === "" \? riskFreeRate/);
  assert.match(app, /STATUS\.PUBLISHED, STATUS\.DEGRADED/);
  assert.match(app, /탐색상한 도달/);
});

test("short histories preserve performance views while Kelly-dependent views fail closed", () => {
  assert.match(app, /function computeHistoricalAnalysis/);
  assert.match(app, /minObservations:\s*2/);
  assert.match(app, /historicalKellyEligibility\(payload, official\.returns\.length\)/);
  assert.match(app, /Kelly 계산 불가/);
  assert.match(app, /일간수익률.*최소.*필요/);
  assert.match(app, /renderMetricCards\(metrics/);
  assert.match(app, /renderWealthChart/);
  assert.match(app, /clearChart\(\$\("#growth-chart"\), "성장률–레버리지 곡선", kellyUnavailableReason\)/);
  assert.match(charts, /element\.setAttribute\("aria-label", `\$\{title\}\. \$\{subtitle\}`\)/);
});

test("mode tabs support arrow-key navigation and shared URLs restore all modes", () => {
  assert.match(app, /addEventListener\("keydown", onModeTabKeydown\)/);
  for (const key of ["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"]) {
    assert.match(app, new RegExp(key));
  }
  assert.match(app, /parseShareState\(location\.search\)/);
  assert.match(app, /const configuration = collectCurrentShareState\(\);[\s\S]*serializeShareState\(configuration\)/);
  assert.match(app, /global-share-url/);
  assert.match(app, /현재 설정 URL 공유/);
  assert.match(html, /class="header-actions">[\s\S]*id="theme-toggle"/);
  for (const parameter of ["borrowSpread", "annualization", "rebalance", "excess", "vol", "source", "assets", "corr", "cap"]) {
    assert.match(app, new RegExp(`"${parameter}"`));
  }
});

test("displayed provider data exposes dofollow Twelve Data and official KRX attribution", () => {
  assert.match(app, /href="https:\/\/twelvedata\.com"[^>]*>Data provided by Twelve Data<\/a>/);
  assert.match(app, /href="https:\/\/openapi\.krx\.co\.kr\/"[^>]*>한국거래소 통계정보<\/a>/);
  assert.match(app, /href="https:\/\/finance\.yahoo\.com\/"[^>]*>Yahoo Finance 시세<\/a>/);
  assert.match(app, /href="https:\/\/github\.com\/FinanceData\/FinanceDataReader"/);
  assert.match(app, /href="https:\/\/stooq\.com\/"/);
  assert.match(app, /href="https:\/\/fred\.stlouisfed\.org\/series\/DEXKOUS"/);
  assert.doesNotMatch(app, /source-attribution[^>]*nofollow/);
  assert.match(app, /renderAssetMeta\([\s\S]*series\.source \?\? payload\.source/);
});
