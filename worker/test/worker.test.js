import assert from "node:assert/strict";
import test from "node:test";

import worker, { testSupport } from "../src/index.js";

const DEFAULT_ORIGIN = "https://sonchanggi.github.io";
const origin = { Origin: DEFAULT_ORIGIN };
const yahooEnv = { YAHOO_PUBLIC_DISPLAY_APPROVED: "true" };
const fxEnv = {
  TWELVE_DATA_API_KEY: "secret-for-test",
  TWELVE_DATA_RIGHTS_APPROVED: "true",
};
const historyUrl = "https://worker.test/v1/history?symbols=NVDA&start=2026-01-01&end=2026-01-10";

test.beforeEach(() => testSupport.resetRateLimits());

async function withWorkerGlobals({ fetch, caches }, callback) {
  const previousFetch = globalThis.fetch;
  const previousCaches = globalThis.caches;
  try {
    if (fetch !== undefined) globalThis.fetch = fetch;
    globalThis.caches = caches;
    return await callback();
  } finally {
    globalThis.fetch = previousFetch;
    globalThis.caches = previousCaches;
  }
}

function epoch(date) {
  return Date.parse(`${date}T14:30:00Z`) / 1000;
}

function yahooChartResponse({
  symbol = "NVDA",
  instrumentType = "EQUITY",
  exchangeName = "NMS",
  currency = "USD",
  exchangeTimezoneName = "America/New_York",
  dates = ["2026-01-02", "2026-01-05"],
  closes = [100, 110],
  adjusted = [90, 108],
  shortName = "NVIDIA Corporation",
} = {}) {
  return Response.json({
    chart: {
      result: [{
        meta: {
          symbol,
          instrumentType,
          exchangeName,
          currency,
          exchangeTimezoneName,
          shortName,
          vendorOnly: "must not escape",
        },
        timestamp: dates.map(epoch),
        indicators: {
          quote: [{ close: closes, open: closes.map((value) => value - 1) }],
          ...(adjusted === undefined ? {} : { adjclose: [{ adjclose: adjusted }] }),
        },
      }],
      error: null,
    },
  });
}

function yahooSearchResponse(quotes) {
  return Response.json({ quotes, news: [{ title: "must not escape" }] });
}

function twelvePayload(values = [
  { datetime: "2026-01-02", close: "1300", vendorOnly: "hidden" },
  { datetime: "2026-01-05", close: "1310", vendorOnly: "hidden" },
], meta = { symbol: "USD/KRW", exchange: "FX", type: "Physical Currency" }) {
  return Response.json({ meta, values });
}

function memoryCache() {
  const entries = new Map();
  const matchedKeys = [];
  const writtenKeys = [];
  return {
    matchedKeys,
    writtenKeys,
    async match(request) {
      matchedKeys.push(request.url);
      return entries.get(request.url)?.clone();
    },
    async put(request, response) {
      writtenKeys.push(request.url);
      entries.set(request.url, response.clone());
    },
  };
}

test("catalog preserves the locked 50 core assets while using Yahoo for US history", () => {
  assert.equal(testSupport.CATALOG.length, 50);
  assert.equal(new Set(testSupport.CATALOG.map((asset) => asset.id)).size, 50);
  assert.equal(testSupport.CATALOG.filter((asset) => asset.provider === "krx").length, 2);
  assert.equal(testSupport.CATALOG.filter((asset) => asset.provider === "yahoo_chart").length, 47);
  assert.equal(testSupport.CATALOG.find((asset) => asset.symbol === "^GSPC").providerSymbol, "^GSPC");
});

test("health fails closed when Yahoo display rights and optional FX are unavailable", async () => {
  const response = await worker.fetch(
    new Request("https://worker.test/v1/health", { headers: origin }),
    {},
    {},
  );
  const text = await response.text();
  const body = JSON.parse(text);
  assert.equal(response.status, 503);
  assert.equal(body.state, "unavailable");
  assert.equal(body.provider, "none");
  assert.equal(body.keyRequired, false);
  assert.equal(body.rightsApproved, false);
  assert.equal(body.capabilities.search, "unavailable");
  assert.equal(body.capabilities.usHistory, "unavailable");
  assert.equal(body.capabilities.fx, "unavailable");
  assert.equal(body.capabilities.krx, "unavailable");
  assert.equal(text.includes("TWELVE_DATA_API_KEY"), false);
  assert.equal(text.includes("secret"), false);
});

test("health is live and secret-free after explicit Yahoo public-display approval", async () => {
  const response = await worker.fetch(
    new Request("https://worker.test/v1/health", { headers: origin }),
    yahooEnv,
    {},
  );
  const text = await response.text();
  const body = JSON.parse(text);
  assert.equal(response.status, 200);
  assert.equal(body.state, "live_api");
  assert.equal(body.provider, "yahoo_finance");
  assert.equal(body.keyRequired, false);
  assert.equal(body.rightsApproved, true);
  assert.equal(body.capabilities.search, "live_api");
  assert.equal(body.capabilities.usHistory, "live_api");
  assert.equal(body.capabilities.fx, "unavailable");
  assert.equal(body.capabilities.krx, "unavailable");
  assert.equal(text.includes("TWELVE_DATA_API_KEY"), false);
  assert.equal(text.includes("secret"), false);
});

test("health reports optional Twelve FX without enabling unapproved Yahoo capabilities", async () => {
  const response = await worker.fetch(
    new Request("https://worker.test/v1/health", { headers: origin }),
    fxEnv,
    {},
  );
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.state, "live_api");
  assert.equal(body.provider, "twelve_data");
  assert.equal(body.keyRequired, true);
  assert.equal(body.rightsApproved, false);
  assert.equal(body.capabilities.search, "unavailable");
  assert.equal(body.capabilities.usHistory, "unavailable");
  assert.equal(body.capabilities.fx, "live_api");
  assert.equal(body.capabilities.krx, "unavailable");
});

test("unconfirmed Yahoo display rights block search and history before provider access", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse();
    },
  }, async () => {
    for (const url of [
      "https://worker.test/v1/search?q=NVDA",
      historyUrl,
    ]) {
      const response = await worker.fetch(new Request(url, { headers: origin }), {}, {});
      const body = await response.json();
      assert.equal(response.status, 503);
      assert.equal(body.state, "unavailable");
      assert.equal(body.reasonCode, "provider_display_rights_unconfirmed");
    }
    assert.equal(providerCalls, 0);
  });
});

test("exact core search stays local and requires no provider call", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => { throw new Error("provider must not be called"); },
  }, async () => {
    const response = await worker.fetch(
      new Request("https://worker.test/v1/search?q=NVDA", { headers: origin }),
      yahooEnv,
      {},
    );
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.state, "published");
    assert.equal(body.assets[0].symbol, "NVDA");
  });
});

test("search discovers an arbitrary valid US stock from a fixed Yahoo host", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input, options) => {
      const url = new URL(input);
      assert.equal(url.origin, "https://query2.finance.yahoo.com");
      assert.equal(url.pathname, "/v1/finance/search");
      assert.equal(url.searchParams.get("q"), "Costco");
      assert.equal(url.searchParams.get("newsCount"), "0");
      assert.equal(url.searchParams.get("region"), "US");
      assert.equal(options.headers.Accept, "application/json");
      assert.match(options.headers["User-Agent"], /KellyAllocationLab\/1\.0/);
      return yahooSearchResponse([
        { symbol: "COST", quoteType: "EQUITY", exchange: "NMS", longname: "Costco Wholesale Corporation" },
        { symbol: "COST.L", quoteType: "EQUITY", exchange: "LSE", longname: "foreign result" },
        { symbol: "BTC-USD", quoteType: "CRYPTOCURRENCY", exchange: "CCC", longname: "crypto result" },
      ]);
    },
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/search?q=Costco&limit=5",
      { headers: origin },
    ), yahooEnv, {});
    const text = await response.text();
    const body = JSON.parse(text);
    assert.equal(response.status, 200);
    assert.equal(body.state, "live_api");
    assert.deepEqual(body.assets.map((asset) => asset.symbol), ["COST"]);
    assert.deepEqual(body.assets[0], {
      id: "stock-cost",
      symbol: "COST",
      name: "Costco Wholesale Corporation",
      assetType: "equity",
      exchange: "NASDAQ",
      currency: "USD",
      timezone: "America/New_York",
      provider: "yahoo_chart",
      providerSymbol: "COST",
    });
    assert.equal(text.includes("foreign result"), false);
    assert.equal(text.includes("crypto result"), false);
  });
});

test("search excludes 3x ETFs by symbol and product name", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => yahooSearchResponse([
      { symbol: "TQQQ", quoteType: "ETF", exchange: "NMS", longname: "ProShares UltraPro QQQ" },
      { symbol: "XYZL", quoteType: "ETF", exchange: "PCX", longname: "Example Daily 3X Shares" },
      { symbol: "SCHD", quoteType: "ETF", exchange: "PCX", longname: "Schwab U.S. Dividend Equity ETF" },
    ]),
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/search?q=dividend&limit=5",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(body.assets.map((asset) => asset.symbol), ["SCHD"]);
  });
});

test("search rejects injection syntax before any provider access", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooSearchResponse([]);
    },
  }, async () => {
    for (const query of ["AAPL/../../admin", "https://evil.example", "AAPL?crumb=x", "<script>"]) {
      const response = await worker.fetch(new Request(
        `https://worker.test/v1/search?q=${encodeURIComponent(query)}`,
        { headers: origin },
      ), {}, {});
      assert.equal(response.status, 400);
      assert.equal((await response.json()).reasonCode, "invalid_search");
    }
    assert.equal(providerCalls, 0);
  });
});

test("OPTIONS returns scoped CORS headers without credential permission", async () => {
  const request = new Request("https://worker.test/v1/history", {
    method: "OPTIONS",
    headers: {
      Origin: DEFAULT_ORIGIN,
      "Access-Control-Request-Method": "GET",
      "Access-Control-Request-Headers": "Content-Type",
    },
  });
  const response = await worker.fetch(request, {}, {});
  assert.equal(response.status, 204);
  assert.equal(response.headers.get("Access-Control-Allow-Origin"), DEFAULT_ORIGIN);
  assert.equal(response.headers.get("Access-Control-Allow-Methods"), "GET, OPTIONS");
  assert.equal(response.headers.get("Access-Control-Allow-Headers"), "Content-Type");
  assert.equal(response.headers.get("Access-Control-Max-Age"), "86400");
  assert.equal(response.headers.get("Vary"), "Origin");
  assert.equal(response.headers.has("Access-Control-Allow-Credentials"), false);
  assert.equal(await response.text(), "");
});

test("history uses Yahoo adjusted close for equities and omits raw fields", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input, options) => {
      const url = new URL(input);
      assert.equal(url.origin, "https://query2.finance.yahoo.com");
      assert.equal(url.pathname, "/v8/finance/chart/NVDA");
      assert.equal(url.searchParams.get("interval"), "1d");
      assert.equal(url.searchParams.get("includeAdjustedClose"), "true");
      assert.equal(options.headers.Authorization, undefined);
      assert.match(options.headers["User-Agent"], /KellyAllocationLab\/1\.0/);
      return yahooChartResponse();
    },
  }, async () => {
    const response = await worker.fetch(new Request(historyUrl, { headers: origin }), yahooEnv, {});
    const text = await response.text();
    const body = JSON.parse(text);
    assert.equal(response.status, 200);
    assert.equal(body.contract, "kelly-price-series");
    assert.deepEqual(body.prices, [[90, 108]]);
    assert.ok(Math.abs(body.returns[0][1] - 0.2) < 1e-12);
    assert.equal(body.metadata[0].returnBasis, "total_return_approximation");
    assert.equal(body.metadata[0].priceField, "adjusted_close");
    assert.equal(body.source.provider, "yahoo_finance");
    assert.equal(body.source.priceField, "adjusted_close");
    assert.deepEqual(body.source.priceFieldBySymbol, { NVDA: "adjusted_close" });
    assert.equal(body.source.license, "provider_terms_apply");
    assert.equal(body.source.rawRedistribution, false);
    assert.equal(text.includes("vendorOnly"), false);
    assert.equal(text.includes("open"), false);
  });
});

test("history accepts a syntactically valid ticker outside the core 50 after identity validation", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input) => {
      assert.equal(new URL(input).pathname, "/v8/finance/chart/COST");
      return yahooChartResponse({ symbol: "COST", shortName: "Costco Wholesale Corporation" });
    },
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=COST&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(body.symbols, ["COST"]);
    assert.equal(body.metadata[0].id, "stock-cost");
    assert.equal(body.metadata[0].name, "Costco Wholesale Corporation");
    assert.equal(body.metadata[0].assetType, "equity");
    assert.equal(body.metadata[0].exchange, "NASDAQ");
  });
});

test("class-share dot input is canonicalized to Yahoo's hyphen symbol", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input) => {
      assert.equal(new URL(input).pathname, "/v8/finance/chart/BRK-B");
      return yahooChartResponse({ symbol: "BRK-B", shortName: "Berkshire Hathaway Inc." });
    },
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=BRK.B&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(body.symbols, ["BRK-B"]);
    assert.equal(body.metadata[0].id, "stock-brk-b");
  });
});

test("a dynamic US ETF is classified from Yahoo metadata and also uses adjusted close", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input) => {
      assert.equal(new URL(input).pathname, "/v8/finance/chart/SCHD");
      return yahooChartResponse({
        symbol: "SCHD",
        instrumentType: "ETF",
        exchangeName: "PCX",
        shortName: "Schwab U.S. Dividend Equity ETF",
        closes: [30, 31],
        adjusted: [29, 30.5],
      });
    },
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=SCHD&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.metadata[0].id, "etf-schd");
    assert.equal(body.metadata[0].assetType, "etf");
    assert.equal(body.metadata[0].exchange, "NYSE ARCA");
    assert.equal(body.metadata[0].priceField, "adjusted_close");
    assert.deepEqual(body.prices, [[29, 30.5]]);
  });
});

test("history rejects excluded 3x ETFs after provider identity validation", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => yahooChartResponse({
      symbol: "TQQQ",
      instrumentType: "ETF",
      exchangeName: "NMS",
      shortName: "ProShares UltraPro QQQ",
    }),
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=TQQQ&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 400);
    assert.equal(body.state, "unavailable");
    assert.equal(body.reasonCode, "excluded_3x_product");
  });
});

test("indices use raw close even when Yahoo also supplies adjusted close", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => yahooChartResponse({
      symbol: "^GSPC",
      instrumentType: "INDEX",
      exchangeName: "SNP",
      closes: [6000, 6060],
      adjusted: [1, 2],
      shortName: "S&P 500",
    }),
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=%5EGSPC&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.deepEqual(body.prices, [[6000, 6060]]);
    assert.equal(body.metadata[0].returnBasis, "price_return");
    assert.equal(body.metadata[0].priceField, "close");
    assert.equal(body.source.priceField, "close");
  });
});

test("mixed stock and index history reports the per-asset price-field choice", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input) => new URL(input).pathname.endsWith("%5EGSPC")
      ? yahooChartResponse({ symbol: "^GSPC", instrumentType: "INDEX", exchangeName: "SNP", closes: [6000, 6060] })
      : yahooChartResponse(),
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=NVDA,%5EGSPC&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), yahooEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.source.priceField, "by_asset");
    assert.deepEqual(body.source.priceFieldBySymbol, { NVDA: "adjusted_close", "^GSPC": "close" });
  });
});

test("invalid or injection ticker syntax cannot alter the fixed upstream host", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse();
    },
  }, async () => {
    for (const symbols of [
      "https://evil.example/x",
      "AAPL/../../evil",
      "AAPL?period1=0",
      "AAPL%2CMSFT",
      "^RUT",
      "123456.KQ",
    ]) {
      const response = await worker.fetch(new Request(
        `https://worker.test/v1/history?symbols=${encodeURIComponent(symbols)}&start=2026-01-01&end=2026-01-10`,
        { headers: origin },
      ), {}, {});
      assert.equal(response.status, 400);
      assert.equal((await response.json()).reasonCode, "invalid_symbols");
    }
    assert.equal(providerCalls, 0);
  });
});

const identityFailures = [
  [{ symbol: "MSFT" }, "provider_identity_symbol_mismatch"],
  [{ instrumentType: "MUTUALFUND" }, "provider_identity_type_mismatch"],
  [{ exchangeName: "LSE" }, "provider_identity_exchange_mismatch"],
  [{ currency: "EUR" }, "provider_identity_currency_mismatch"],
  [{ exchangeTimezoneName: "Europe/London" }, "provider_identity_timezone_mismatch"],
];

for (const [override, reasonCode] of identityFailures) {
  test(`history fails closed on Yahoo identity boundary: ${reasonCode}`, async () => {
    await withWorkerGlobals({
      caches: undefined,
      fetch: async () => yahooChartResponse(override),
    }, async () => {
      const response = await worker.fetch(new Request(historyUrl, { headers: origin }), yahooEnv, {});
      assert.equal(response.status, 502);
      const body = await response.json();
      assert.equal(body.state, "degraded");
      assert.equal(body.reasonCode, reasonCode);
    });
  });
}

test("equities fail closed rather than falling back to raw close when adjusted close is absent", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => yahooChartResponse({ adjusted: null }),
  }, async () => {
    const response = await worker.fetch(new Request(historyUrl, { headers: origin }), yahooEnv, {});
    assert.equal(response.status, 502);
    assert.equal((await response.json()).reasonCode, "provider_adjusted_close_missing");
  });
});

test("cache hits and writes are isolated by allowed Origin", async () => {
  const cache = memoryCache();
  const pendingWrites = [];
  let providerCalls = 0;
  const env = {
    ...yahooEnv,
    ALLOWED_ORIGINS: "https://app-one.test,https://app-two.test",
  };
  const ctx = { waitUntil(promise) { pendingWrites.push(promise); } };
  await withWorkerGlobals({
    caches: { default: cache },
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse();
    },
  }, async () => {
    const first = await worker.fetch(new Request(historyUrl, { headers: { Origin: "https://app-one.test" } }), env, ctx);
    assert.equal(first.status, 200);
    await Promise.all(pendingWrites.splice(0));
    const hit = await worker.fetch(new Request(historyUrl, { headers: { Origin: "https://app-one.test" } }), env, ctx);
    assert.equal(hit.status, 200);
    assert.equal(providerCalls, 1);
    const secondOrigin = await worker.fetch(new Request(historyUrl, { headers: { Origin: "https://app-two.test" } }), env, ctx);
    assert.equal(secondOrigin.status, 200);
    await Promise.all(pendingWrites.splice(0));
    assert.equal(providerCalls, 2);
    assert.equal(cache.writtenKeys.length, 2);
    assert.notEqual(cache.writtenKeys[0], cache.writtenKeys[1]);
  });
});

test("date, symbol-count, duplicate, and future bounds reject before provider access", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse();
    },
  }, async () => {
    const urls = [
      "symbols=NVDA&start=2026-01-10&end=2026-01-01",
      "symbols=NVDA&start=2020-01-01&end=2025-01-02",
      "symbols=NVDA&start=2099-01-01&end=2099-01-02",
      "symbols=AAPL,MSFT,NVDA,AMZN,GOOGL,META&start=2026-01-01&end=2026-01-10",
      "symbols=NVDA,NVDA&start=2026-01-01&end=2026-01-10",
    ];
    for (const suffix of urls) {
      const response = await worker.fetch(new Request(
        `https://worker.test/v1/history?${suffix}`,
        { headers: origin },
      ), {}, {});
      assert.equal(response.status, 400);
    }
    assert.equal(providerCalls, 0);
  });
});

test("KRX remains fail-closed and is never sent to Yahoo", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse();
    },
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=NVDA,005930.KS&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), {}, {});
    assert.equal(response.status, 503);
    assert.equal((await response.json()).reasonCode, "provider_not_available");
    assert.equal(providerCalls, 0);
  });
});

const providerFailures = [
  ["401", async () => new Response(null, { status: 401 }), 503, "unavailable", "provider_access_unavailable"],
  ["404", async () => new Response(null, { status: 404 }), 404, "unavailable", "series_unavailable"],
  ["429", async () => new Response(null, { status: 429 }), 429, "degraded", "provider_rate_limited"],
  ["5xx", async () => new Response(null, { status: 503 }), 502, "degraded", "provider_request_failed"],
  ["network failure", async () => { throw new Error("private provider detail"); }, 502, "degraded", "provider_network_failure"],
  ["invalid JSON shape", async () => Response.json({ chart: "invalid", raw: "hidden" }), 502, "degraded", "provider_payload_invalid"],
  ["Yahoo not-found payload", async () => Response.json({ chart: { result: null, error: { code: "Not Found", description: "raw" } } }), 404, "unavailable", "series_unavailable"],
];

for (const [name, fetch, status, state, reasonCode] of providerFailures) {
  test(`Yahoo ${name} failure maps to a stable public state`, async () => {
    await withWorkerGlobals({ caches: undefined, fetch }, async () => {
      const response = await worker.fetch(new Request(historyUrl, { headers: origin }), yahooEnv, {});
      const text = await response.text();
      const body = JSON.parse(text);
      assert.equal(response.status, status);
      assert.equal(body.state, state);
      assert.equal(body.reasonCode, reasonCode);
      assert.equal(text.includes("hidden"), false);
      assert.equal(text.includes("private provider detail"), false);
      assert.equal(text.includes("raw"), false);
    });
  });
}

test("truncated and stale-end Yahoo results fail closed", async () => {
  const longDates = Array.from({ length: 5000 }, (_, index) => {
    const date = new Date("2000-01-01T14:30:00Z");
    date.setUTCDate(date.getUTCDate() + index);
    return date.toISOString().slice(0, 10);
  });
  const cases = [
    [() => yahooChartResponse({ dates: longDates, closes: longDates.map(() => 100), adjusted: longDates.map(() => 100) }), "provider_result_truncated"],
    [() => yahooChartResponse(), "provider_end_coverage_insufficient"],
  ];
  for (let index = 0; index < cases.length; index += 1) {
    const [payload, reasonCode] = cases[index];
    const url = index === 0
      ? historyUrl
      : "https://worker.test/v1/history?symbols=NVDA&start=2026-01-01&end=2026-02-01";
    await withWorkerGlobals({ caches: undefined, fetch: async () => payload() }, async () => {
      const response = await worker.fetch(new Request(url, { headers: origin }), yahooEnv, {});
      assert.equal(response.status, 502);
      assert.equal((await response.json()).reasonCode, reasonCode);
    });
  }
});

test("per-client upstream rate limits are enforced without trusting query input", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return yahooChartResponse({ symbol: providerCalls === 1 ? "COST" : "SNOW" });
    },
  }, async () => {
    const env = { ...yahooEnv, HISTORY_RATE_LIMIT_PER_MINUTE: "1" };
    const headers = { ...origin, "CF-Connecting-IP": "203.0.113.8" };
    const first = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=COST&start=2026-01-01&end=2026-01-10",
      { headers },
    ), env, {});
    assert.equal(first.status, 200);
    const second = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=SNOW&start=2026-01-01&end=2026-01-10",
      { headers },
    ), env, {});
    assert.equal(second.status, 429);
    assert.equal((await second.json()).reasonCode, "worker_rate_limited");
    assert.equal(providerCalls, 1);
  });
});

test("USD/KRW remains optional and fail-closed without Twelve Data configuration", async () => {
  const url = "https://worker.test/v1/fx?base=USD&quote=KRW&start=2026-01-01&end=2026-01-10";
  const unavailable = await worker.fetch(new Request(url, { headers: origin }), {}, {});
  assert.equal(unavailable.status, 503);
  assert.equal((await unavailable.json()).reasonCode, "provider_not_configured");

  await withWorkerGlobals({
    caches: undefined,
    fetch: async (input, options) => {
      assert.equal(new URL(input).origin, "https://api.twelvedata.com");
      assert.equal(options.headers.Authorization, "apikey secret-for-test");
      return twelvePayload();
    },
  }, async () => {
    const response = await worker.fetch(new Request(url, { headers: origin }), fxEnv, {});
    const text = await response.text();
    const body = JSON.parse(text);
    assert.equal(response.status, 200);
    assert.deepEqual(body.symbols, ["USD/KRW"]);
    assert.deepEqual(body.fx, { base: "USD", quote: "KRW", rates: [1300, 1310] });
    assert.equal(body.source.provider, "twelve_data");
    assert.equal(text.includes("secret-for-test"), false);
  });
});

test("union-calendar returns use the previous non-null observation", () => {
  const first = { ...testSupport.CATALOG.find((item) => item.symbol === "NVDA"), priceField: "adjusted_close" };
  const second = { ...testSupport.CATALOG.find((item) => item.symbol === "AAPL"), priceField: "adjusted_close" };
  const document = testSupport.normalizedDocument(
    [first, second],
    [
      [["2026-01-02", 100], ["2026-01-06", 110]],
      [["2026-01-02", 200], ["2026-01-05", 202], ["2026-01-06", 204]],
    ],
  );
  assert.deepEqual(document.prices[0], [100, null, 110]);
  assert.equal(document.returns[0][1], null);
  assert.ok(Math.abs(document.returns[0][2] - 0.1) < 1e-12);
});

test("disallowed origins are rejected before any route or provider work", async () => {
  const response = await worker.fetch(new Request("https://worker.test/v1/search?q=COST", {
    headers: { Origin: "https://example.invalid" },
  }), {}, {});
  assert.equal(response.status, 403);
  assert.equal((await response.json()).reasonCode, "origin_not_allowed");
  assert.equal(response.headers.has("Access-Control-Allow-Origin"), false);
});
