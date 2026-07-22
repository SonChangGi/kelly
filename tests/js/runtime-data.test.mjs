import test from "node:test";
import assert from "node:assert/strict";
import { Window } from "happy-dom";

const window = new Window({ url: "https://sonchanggi.github.io/kelly/" });
globalThis.__KELLY_APP_TEST__ = true;
globalThis.window = window;
globalThis.document = window.document;
Object.defineProperty(globalThis, "navigator", { configurable: true, value: window.navigator });
Object.defineProperty(globalThis, "location", { configurable: true, value: window.location });
globalThis.HTMLElement = window.HTMLElement;
globalThis.HTMLCanvasElement = window.HTMLCanvasElement;

const { testSupport } = await import("../../site/assets/app.js");

test("runtime Worker URL is nullable and accepts only credential-free HTTPS URLs", () => {
  assert.equal(testSupport.normalizeWorkerBaseUrl(null), null);
  assert.equal(testSupport.normalizeWorkerBaseUrl(""), null);
  assert.equal(testSupport.normalizeWorkerBaseUrl("http://worker.example.test"), null);
  assert.equal(testSupport.normalizeWorkerBaseUrl("https://user:secret@worker.example.test"), null);
  assert.equal(testSupport.normalizeWorkerBaseUrl("https://worker.example.test/"), "https://worker.example.test");
});

test("on-demand history requests use a five-calendar-year UTC range", () => {
  assert.deepEqual(
    testSupport.fiveYearRange(new Date("2026-07-21T12:00:00Z")),
    { start: "2021-07-21", end: "2026-07-21" },
  );
});

test("published static generations take precedence while unavailable can fall back", () => {
  for (const state of ["published", "stale", "degraded"]) {
    assert.equal(testSupport.isReusableStaticPayload({ state }), true);
  }
  assert.equal(testSupport.isReusableStaticPayload({ state: "unavailable" }), false);
  assert.equal(testSupport.isReusableStaticPayload(null), false);
});

test("short published histories keep performance metrics but fail Kelly closed", () => {
  const returns = Array.from({ length: 25 }, (_, index) => index % 2 === 0 ? 0.01 : -0.004);
  const dates = Array.from({ length: 26 }, (_, index) => `2026-06-${String(index + 1).padStart(2, "0")}`);
  const analysis = testSupport.computeHistoricalAnalysis(
    { returns, dates, returnDates: dates.slice(1) },
    {
      annualizationDays: 252,
      riskFreeRate: 0,
      borrowingSpread: 0,
      transactionCostBps: 10,
      mar: 0,
      frequency: "monthly",
    },
    {
      quality: {
        observationCount: 26,
        eligibleForKelly: false,
        minimumKellyObservations: 60,
      },
    },
  );

  assert.equal(analysis.metrics.status, "published");
  assert.equal(analysis.metrics.observations, 25);
  assert.equal(analysis.kelly.status, "unavailable");
  assert.equal(analysis.kelly.reasonCode, "insufficient_observations");
  assert.equal(analysis.exact.status, "unavailable");
  assert.equal(analysis.rebalance, null);
  assert.deepEqual(analysis.kellyEligibility, {
    eligible: false,
    observations: 25,
    minimumObservations: 60,
    reasonCode: "insufficient_observations",
  });
});

test("Kelly eligibility requires both source quality approval and 60 selected returns", () => {
  assert.equal(testSupport.historicalKellyEligibility(null, 60).eligible, true);
  assert.equal(testSupport.historicalKellyEligibility({ quality: { eligibleForKelly: true, minimumKellyObservations: 60 } }, 59).eligible, false);
  assert.equal(testSupport.historicalKellyEligibility({ quality: { eligibleForKelly: false, minimumKellyObservations: 60 } }, 100).eligible, false);
});

test("asset quality metadata stays compact and names the cross-check boundary", () => {
  const passed = testSupport.qualityMetaHtml({
    eligibleForKelly: true,
    minimumKellyObservations: 60,
    crossCheck: { provider: "finviz", state: "passed", commonObservations: 120 },
  }, 120);
  assert.match(passed, /Finviz 교차검증 통과/);
  assert.doesNotMatch(passed, /Kelly 관측 부족/);

  const short = testSupport.qualityMetaHtml({
    eligibleForKelly: false,
    minimumKellyObservations: 60,
    crossCheck: { provider: "stooq", state: "unavailable", commonObservations: 0 },
  }, 25);
  assert.match(short, /교차검증 미확인/);
  assert.match(short, /Kelly 관측 부족 25\/60/);
});

test("Worker multi-series response is flattened into the static single-asset shape", () => {
  const result = testSupport.flattenWorkerPayload({
    schemaVersion: 1,
    contract: "kelly-price-series",
    state: "live_api",
    generatedAt: "2026-07-21T00:00:00Z",
    dataAsOf: "2026-07-21",
    symbols: ["NVDA"],
    metadata: [{
      id: "stock-nvda", symbol: "NVDA", name: "NVIDIA", assetType: "equity",
      exchange: "NASDAQ", currency: "USD", timezone: "America/New_York",
      returnBasis: "total_return_approximation",
    }],
    dates: ["2026-07-18", "2026-07-21"],
    prices: [[100, 110]],
    returns: [[null, 0.1]],
    source: { provider: "twelve_data" },
    limitations: [],
  }, "NVDA");
  assert.equal(result.assetId, "stock-nvda");
  assert.equal(result.metadata.baseCurrency, "USD");
  assert.equal(result.metadata.returnBasis, "total_return_approximation");
  assert.deepEqual(result.prices, [100, 110]);
});

test("KRW conversion uses only the latest prior FX observation within five days", () => {
  assert.deepEqual(
    testSupport.alignPreviousFx(
      ["2026-01-02", "2026-01-05"],
      ["2026-01-02", "2026-01-06"],
      [1400, 1410],
    ),
    [1400, 1400],
  );
  assert.throws(
    () => testSupport.alignPreviousFx(["2026-01-08"], ["2026-01-02"], [1400]),
    /FX_GAP_EXCEEDED/,
  );

  const asset = {
    state: "published",
    assetId: "stock-nvda",
    metadata: { symbol: "NVDA", baseCurrency: "USD", returnBasis: "total_return_approximation" },
    dates: ["2026-01-02", "2026-01-05"],
    prices: [100, 110],
    returns: [null, 0.1],
  };
  const fx = {
    state: "published",
    assetId: "fx-usd-krw",
    metadata: { symbol: "USD/KRW", baseCurrency: "KRW", returnBasis: "fx_rate" },
    dates: ["2026-01-02"],
    prices: [1400],
    returns: [null],
  };
  const converted = testSupport.seriesFromPayload(asset, "krw", fx);
  assert.equal(converted.currency, "KRW");
  assert.deepEqual(converted.prices, [140000, 154000]);
});

test("KRW conversion consumes independently dated embedded FX using prior-only five-day alignment", () => {
  const converted = testSupport.seriesFromPayload({
    state: "published",
    assetId: "stock-nvda",
    metadata: { symbol: "NVDA", baseCurrency: "USD", returnBasis: "total_return_approximation" },
    dates: ["2026-01-02", "2026-01-05"],
    prices: [100, 110],
    returns: [null, 0.1],
    fx: { dates: ["2026-01-02", "2026-01-06"], rates: [1400, 1410] },
  }, "krw");

  assert.deepEqual(converted.prices, [140000, 154000]);
  assert.throws(
    () => testSupport.seriesFromPayload({
      state: "published",
      metadata: { symbol: "X", baseCurrency: "USD" },
      dates: ["2026-01-08"], prices: [100], returns: [null],
      fx: { dates: ["2026-01-02"], rates: [1400] },
    }, "krw"),
    /FX_GAP_EXCEEDED/,
  );
});

test("an FX pair is denominated in its quote currency and is never multiplied by itself", () => {
  const fxPayload = {
    state: "published",
    assetId: "fx-usd-krw",
    metadata: {
      symbol: "USD/KRW",
      assetType: "fx",
      baseCurrency: "USD",
      quoteCurrency: "KRW",
      returnBasis: "fx_rate",
    },
    dates: ["2026-01-02", "2026-01-05"],
    prices: [1400, 1410],
    returns: [null, 1410 / 1400 - 1],
  };
  const native = testSupport.seriesFromPayload(fxPayload, "native");
  const krw = testSupport.seriesFromPayload(fxPayload, "krw", fxPayload);
  assert.equal(native.currency, "KRW");
  assert.equal(krw.currency, "KRW");
  assert.deepEqual(krw.prices, [1400, 1410]);
  assert.deepEqual(krw.returns, [1410 / 1400 - 1]);
});

test("mode-scoped notices do not leak historical provider state into other modes", () => {
  const notices = {
    historical: { message: "provider unavailable", tone: "error" },
    direct: null,
    portfolio: { message: "saved", tone: "success" },
  };
  assert.equal(testSupport.noticeForMode(notices, "direct"), null);
  assert.equal(testSupport.noticeForMode(notices, "historical").message, "provider unavailable");
  assert.equal(testSupport.noticeForMode(notices, "portfolio").message, "saved");
});

test("CSV import normalizes date and price rows without trusting optional returns", () => {
  const payload = testSupport.parsePriceCsv(
    "date,price,return,currency\n2026-01-05,110,9,USD\n2026-01-02,100,,USD\n2026-01-06,121,-9,USD\n",
    "sample.csv",
  );
  assert.deepEqual(payload.dates, ["2026-01-02", "2026-01-05", "2026-01-06"]);
  assert.deepEqual(payload.prices, [100, 110, 121]);
  assert.deepEqual(payload.returns, []);
  assert.equal(payload.metadata.symbol, "sample");
  assert.throws(
    () => testSupport.parsePriceCsv("date,price\n2026-01-02,100\n2026-01-02,101\n2026-01-03,102\n"),
    /csv_duplicate_date/,
  );
});

test("quick period presets choose the first available observation on or after the calendar target", () => {
  const dates = ["2021-07-20", "2021-07-22", "2026-07-21"];
  assert.equal(testSupport.quickPeriodStart("5y", dates), "2021-07-22");
  assert.equal(testSupport.quickPeriodStart("all", dates), "2021-07-20");
});

test("dynamic portfolio correlation resizing preserves existing assumptions and identity diagonals", () => {
  assert.deepEqual(
    testSupport.resizeCorrelationMatrix([[1, 0.25], [0.25, 1]], 3),
    [[1, 0.25, 0], [0.25, 1, 0], [0, 0, 1]],
  );
  assert.deepEqual(
    testSupport.resizeCorrelationMatrix([[1, 0.25], [0.25, 1]], 1),
    [[1]],
  );
});

test("removing a middle portfolio asset removes the matching correlation row and column", () => {
  assert.deepEqual(
    testSupport.removeCorrelationIndex(
      [
        [1, 0.2, 0.3],
        [0.2, 1, 0.4],
        [0.3, 0.4, 1],
      ],
      1,
    ),
    [[1, 0.3], [0.3, 1]],
  );
});

test("uploaded CSV sessions are not advertised as restorable URLs", () => {
  assert.equal(testSupport.isShareableHistoricalAssetId("etf-spy"), true);
  assert.equal(testSupport.isShareableHistoricalAssetId("csv-upload"), false);
  assert.equal(testSupport.isShareableHistoricalAssetId(""), false);
});

test("historical portfolio defaults to five years ending on the latest common observation", () => {
  const dates = ["2018-01-02", "2021-07-20", "2021-07-22", "2026-07-21"];
  assert.deepEqual(testSupport.defaultFiveYearCommonRange(dates), { start: "2021-07-22", end: "2026-07-21" });
});

test("historical and direct URL states round-trip every calculation input", () => {
  const historical = testSupport.parseShareState(testSupport.serializeShareState({
    mode: "historical",
    historical: {
      asset: "etf-spy",
      start: "2021-01-04",
      end: "2026-01-02",
      currency: "krw",
      riskFreeRate: 2.5,
      borrowingSpread: 1.25,
      transactionCostBps: 12,
      annualizationDays: 252,
      mar: "",
      rebalance: "quarterly",
    },
  }));
  assert.deepEqual(historical, {
    mode: "historical",
    historical: {
      asset: "etf-spy",
      start: "2021-01-04",
      end: "2026-01-02",
      currency: "krw",
      riskFreeRate: 2.5,
      borrowingSpread: 1.25,
      transactionCostBps: 12,
      annualizationDays: 252,
      mar: "",
      rebalance: "quarterly",
    },
  });

  const direct = testSupport.parseShareState(testSupport.serializeShareState({
    mode: "direct",
    direct: { expectedExcess: 6.5, volatility: 19, riskFreeRate: 2, borrowingSpread: 0.75 },
  }));
  assert.deepEqual(direct, {
    mode: "direct",
    direct: { expectedExcess: 6.5, volatility: 19, riskFreeRate: 2, borrowingSpread: 0.75 },
  });
});

test("direct and historical portfolio URL states round-trip their source-specific inputs", () => {
  const direct = testSupport.parseShareState(testSupport.serializeShareState({
    mode: "portfolio",
    portfolio: {
      source: "direct",
      riskFreeRate: 1,
      borrowingSpread: 0.5,
      cap: 2.5,
      directAssets: [
        { name: "주식", expectedExcess: 7, volatility: 20 },
        { name: "채권", expectedExcess: 2, volatility: 10 },
      ],
      correlation: [[1, -0.2], [-0.2, 1]],
    },
  }));
  assert.equal(direct.mode, "portfolio");
  assert.equal(direct.portfolio.source, "direct");
  assert.deepEqual(
    direct.portfolio.directAssets.map(({ name, expectedExcess, volatility }) => ({ name, expectedExcess, volatility })),
    [
      { name: "주식", expectedExcess: 7, volatility: 20 },
      { name: "채권", expectedExcess: 2, volatility: 10 },
    ],
  );
  assert.deepEqual(direct.portfolio.correlation, [[1, -0.2], [-0.2, 1]]);

  const historical = testSupport.parseShareState(testSupport.serializeShareState({
    mode: "portfolio",
    portfolio: {
      source: "historical",
      riskFreeRate: 0,
      borrowingSpread: 1,
      cap: 3,
      historicalAssetIds: ["etf-spy", "etf-tlt", "etf-gld"],
      start: "2021-01-04",
      end: "2026-01-02",
      rebalance: "monthly",
      transactionCostBps: 10,
    },
  }));
  assert.deepEqual(historical, {
    mode: "portfolio",
    portfolio: {
      source: "historical",
      riskFreeRate: 0,
      borrowingSpread: 1,
      cap: 3,
      historicalAssetIds: ["etf-spy", "etf-tlt", "etf-gld"],
      start: "2021-01-04",
      end: "2026-01-02",
      rebalance: "monthly",
      transactionCostBps: 10,
    },
  });
});

test("invalid URL values are ignored instead of replacing defaults", () => {
  const historical = testSupport.parseShareState(
    "?mode=historical&asset=%3Cscript%3E&start=2026-02-30&end=2020-01-01&currency=usd&rf=NaN&borrowSpread=-1&cost=-1&annualization=0&mar=Infinity&rebalance=sometimes",
  );
  assert.equal(historical.mode, "historical");
  for (const value of Object.values(historical.historical)) assert.equal(value, undefined);

  const portfolio = testSupport.parseShareState(new URLSearchParams({
    mode: "portfolio",
    source: "direct",
    cap: "4",
    assets: JSON.stringify([
      { name: "A", expectedExcess: 6, volatility: 20 },
      { name: "B", expectedExcess: 2, volatility: 10 },
    ]),
    corr: JSON.stringify([[1, 2], [2, 1]]),
  }));
  assert.equal(portfolio.portfolio.cap, undefined);
  assert.equal(portfolio.portfolio.directAssets, undefined);
  assert.equal(portfolio.portfolio.correlation, undefined);
});

test("source attribution links are explicit and remain dofollow", () => {
  const html = testSupport.sourceAttributionHtml([
    { provider: "twelve_data" },
    { provider: "krx" },
    { provider: "yahoo_finance", adapter: "finance_data_reader" },
    { provider: "stooq" },
    { provider: "fred" },
  ]);
  assert.match(html, /href="https:\/\/twelvedata\.com"/);
  assert.match(html, /Twelve Data/);
  assert.match(html, /한국거래소 통계정보/);
  assert.match(html, /https:\/\/openapi\.krx\.co\.kr/);
  assert.match(html, /Yahoo Finance/);
  assert.match(html, /FinanceDataReader/);
  assert.match(html, /Stooq/);
  assert.match(html, /FRED DEXKOUS/);
  assert.doesNotMatch(html, /nofollow/);
});
