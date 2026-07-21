import assert from "node:assert/strict";
import test from "node:test";

import worker, { testSupport } from "../src/index.js";

const DEFAULT_ORIGIN = "https://sonchanggi.github.io";
const origin = { Origin: DEFAULT_ORIGIN };
const configuredEnv = {
  TWELVE_DATA_API_KEY: "secret-for-test",
  TWELVE_DATA_RIGHTS_APPROVED: "true",
};
const historyUrl = "https://worker.test/v1/history?symbols=NVDA&start=2026-01-01&end=2026-01-10";

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

function providerPayload(values = [
  { datetime: "2026-01-02", open: "10", close: "100", vendorOnly: "hidden" },
  { datetime: "2026-01-05", open: "11", close: "110", vendorOnly: "hidden" },
], meta = {
  symbol: "NVDA",
  exchange: "NASDAQ",
  currency: "USD",
  type: "Common Stock",
  vendorOnly: "must not escape",
}) {
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

test("catalog is the locked 50-asset allowlist", () => {
  assert.equal(testSupport.CATALOG.length, 50);
  assert.equal(new Set(testSupport.CATALOG.map((asset) => asset.id)).size, 50);
  assert.equal(testSupport.CATALOG.filter((asset) => asset.provider === "krx").length, 2);
});

test("health fails closed without rights and server secret", async () => {
  const response = await worker.fetch(
    new Request("https://worker.test/v1/health", { headers: origin }),
    {},
    {},
  );
  assert.equal(response.status, 503);
  assert.equal((await response.json()).state, "unavailable");
});

test("search is local and never needs a provider credential", async () => {
  const response = await worker.fetch(
    new Request("https://worker.test/v1/search?q=NVDA", { headers: origin }),
    {},
    {},
  );
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.state, "published");
  assert.equal(body.assets[0].symbol, "NVDA");
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

  const denied = await worker.fetch(new Request("https://worker.test/v1/history", {
    method: "OPTIONS",
    headers: { Origin: "https://example.invalid" },
  }), {}, {});
  assert.equal(denied.status, 403);
  assert.equal(denied.headers.has("Access-Control-Allow-Origin"), false);
  assert.equal(denied.headers.has("Access-Control-Allow-Credentials"), false);
});

test("history normalizes provider observations and omits raw fields and secret", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async (_url, options) => {
      assert.equal(options.headers.Authorization, "apikey secret-for-test");
      return providerPayload();
    },
  }, async () => {
    const response = await worker.fetch(
      new Request(historyUrl, { headers: origin }),
      configuredEnv,
      {},
    );
    const text = await response.text();
    const body = JSON.parse(text);
    assert.equal(response.status, 200);
    assert.equal(response.headers.get("Access-Control-Allow-Origin"), DEFAULT_ORIGIN);
    assert.equal(response.headers.has("Access-Control-Allow-Credentials"), false);
    assert.equal(body.contract, "kelly-price-series");
    assert.equal(body.state, "live_api");
    assert.deepEqual(body.prices, [[100, 110]]);
    assert.equal(body.returns[0][0], null);
    assert.ok(Math.abs(body.returns[0][1] - 0.1) < 1e-12);
    assert.equal(body.metadata[0].returnBasis, "total_return_approximation");
    assert.equal(body.source.priceField, "close");
    assert.equal(body.source.license, "external_display_approved");
    assert.match(body.source.cachedAt, /^\d{4}-\d{2}-\d{2}T/);
    assert.equal(text.includes("vendorOnly"), false);
    assert.equal(text.includes("unexpected"), false);
    assert.equal(text.includes("secret-for-test"), false);
    assert.equal(text.includes("Authorization"), false);
    assert.equal(body.source.rawRedistribution, false);
  });
});

test("cache hits and puts are isolated by allowed Origin", async () => {
  const cache = memoryCache();
  const pendingWrites = [];
  let providerCalls = 0;
  const env = {
    ...configuredEnv,
    ALLOWED_ORIGINS: "https://app-one.test,https://app-two.test",
  };
  const ctx = { waitUntil(promise) { pendingWrites.push(promise); } };

  await withWorkerGlobals({
    caches: { default: cache },
    fetch: async () => {
      providerCalls += 1;
      return providerPayload();
    },
  }, async () => {
    const first = await worker.fetch(
      new Request(historyUrl, { headers: { Origin: "https://app-one.test" } }),
      env,
      ctx,
    );
    assert.equal(first.headers.get("Access-Control-Allow-Origin"), "https://app-one.test");
    await Promise.all(pendingWrites.splice(0));

    const hit = await worker.fetch(
      new Request(historyUrl, { headers: { Origin: "https://app-one.test" } }),
      env,
      ctx,
    );
    assert.equal(hit.headers.get("Access-Control-Allow-Origin"), "https://app-one.test");
    assert.equal(providerCalls, 1);

    const secondOrigin = await worker.fetch(
      new Request(historyUrl, { headers: { Origin: "https://app-two.test" } }),
      env,
      ctx,
    );
    assert.equal(secondOrigin.headers.get("Access-Control-Allow-Origin"), "https://app-two.test");
    await Promise.all(pendingWrites.splice(0));
    assert.equal(providerCalls, 2);
    assert.equal(cache.writtenKeys.length, 2);
    assert.notEqual(cache.writtenKeys[0], cache.writtenKeys[1]);
  });
});

test("/v1/fx returns the normalized USD/KRW contract", async () => {
  const fxMeta = { symbol: "USD/KRW", exchange: "FX", type: "Physical Currency" };
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => providerPayload(undefined, fxMeta),
  }, async () => {
    const response = await worker.fetch(new Request(
      "https://worker.test/v1/fx?base=USD&quote=KRW&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), configuredEnv, {});
    const body = await response.json();
    assert.equal(response.status, 200);
    assert.equal(body.contract, "kelly-price-series");
    assert.deepEqual(body.symbols, ["USD/KRW"]);
    assert.equal(body.metadata[0].assetType, "fx");
    assert.equal(body.metadata[0].returnBasis, "fx_rate");
    assert.deepEqual(body.fx, { base: "USD", quote: "KRW", rates: [100, 110] });
    assert.deepEqual(body.fx.rates, body.prices[0]);
  });

  const denied = await worker.fetch(new Request(
    "https://worker.test/v1/fx?base=EUR&quote=KRW&start=2026-01-01&end=2026-01-10",
    { headers: origin },
  ), configuredEnv, {});
  assert.equal(denied.status, 400);
  assert.equal((await denied.json()).reasonCode, "fx_pair_not_allowlisted");
});

test("reversed and over-five-year date ranges are rejected before provider access", async () => {
  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return providerPayload();
    },
  }, async () => {
    for (const [start, end] of [
      ["2026-01-10", "2026-01-01"],
      ["2020-01-01", "2025-01-02"],
    ]) {
      const response = await worker.fetch(new Request(
        `https://worker.test/v1/history?symbols=NVDA&start=${start}&end=${end}`,
        { headers: origin },
      ), configuredEnv, {});
      assert.equal(response.status, 400);
      assert.equal((await response.json()).reasonCode, "invalid_date_range");
    }
    assert.equal(providerCalls, 0);
  });
});

test("provider identity mismatch is rejected before normalized publication", async () => {
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => providerPayload(undefined, {
      symbol: "MSFT",
      exchange: "NASDAQ",
      currency: "USD",
      type: "Common Stock",
    }),
  }, async () => {
    const response = await worker.fetch(
      new Request(historyUrl, { headers: origin }),
      configuredEnv,
      {},
    );
    assert.equal(response.status, 502);
    const body = await response.json();
    assert.equal(body.state, "degraded");
    assert.equal(body.reasonCode, "provider_identity_symbol_mismatch");
  });
});

test("provider truncation and missing requested-end coverage fail closed", async () => {
  const truncated = Array.from({ length: 5000 }, () => ({
    datetime: "2026-01-02",
    close: "100",
  }));
  const cases = [
    {
      url: historyUrl,
      payload: () => providerPayload(truncated),
      reasonCode: "provider_result_truncated",
    },
    {
      url: "https://worker.test/v1/history?symbols=NVDA&start=2026-01-01&end=2026-02-01",
      payload: () => providerPayload(),
      reasonCode: "provider_end_coverage_insufficient",
    },
  ];
  for (const item of cases) {
    await withWorkerGlobals({ caches: undefined, fetch: async () => item.payload() }, async () => {
      const response = await worker.fetch(
        new Request(item.url, { headers: origin }),
        configuredEnv,
        {},
      );
      assert.equal(response.status, 502);
      assert.equal((await response.json()).reasonCode, item.reasonCode);
    });
  }
});

test("union-calendar returns use the previous non-null observation", () => {
  const first = testSupport.CATALOG.find((item) => item.symbol === "NVDA");
  const second = testSupport.CATALOG.find((item) => item.symbol === "AAPL");
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

test("more than five symbols and unsupported KRX history are rejected", async () => {
  const tooMany = await worker.fetch(new Request(
    "https://worker.test/v1/history?symbols=AAPL,MSFT,NVDA,AMZN,GOOGL,META&start=2026-01-01&end=2026-01-10",
    { headers: origin },
  ), configuredEnv, {});
  assert.equal(tooMany.status, 400);
  assert.equal((await tooMany.json()).reasonCode, "symbols_not_allowlisted");

  let providerCalls = 0;
  await withWorkerGlobals({
    caches: undefined,
    fetch: async () => {
      providerCalls += 1;
      return providerPayload();
    },
  }, async () => {
    const krx = await worker.fetch(new Request(
      "https://worker.test/v1/history?symbols=NVDA,005930.KS&start=2026-01-01&end=2026-01-10",
      { headers: origin },
    ), configuredEnv, {});
    assert.equal(krx.status, 503);
    const body = await krx.json();
    assert.equal(body.state, "unavailable");
    assert.equal(body.reasonCode, "provider_not_available");
    assert.equal(providerCalls, 0);
  });
});

const providerFailures = [
  {
    name: "401",
    fetch: async () => new Response(null, { status: 401 }),
    status: 503,
    state: "unavailable",
    reasonCode: "provider_access_unavailable",
  },
  {
    name: "429",
    fetch: async () => new Response(null, { status: 429 }),
    status: 429,
    state: "degraded",
    reasonCode: "provider_rate_limited",
  },
  {
    name: "5xx",
    fetch: async () => new Response(null, { status: 503 }),
    status: 502,
    state: "degraded",
    reasonCode: "provider_request_failed",
  },
  {
    name: "network failure",
    fetch: async () => { throw new Error("network secret must not escape"); },
    status: 502,
    state: "degraded",
    reasonCode: "provider_network_failure",
  },
  {
    name: "invalid payload",
    fetch: async () => Response.json({ values: "not-an-array", raw: "hidden" }),
    status: 502,
    state: "degraded",
    reasonCode: "provider_payload_invalid",
  },
  {
    name: "empty usable rows",
    fetch: async () => Response.json({
      meta: {
        symbol: "NVDA",
        exchange: "NASDAQ",
        currency: "USD",
        type: "Common Stock",
      },
      values: [
        { datetime: "invalid", close: "100", raw: "hidden" },
        { datetime: "2026-01-02", close: "-1", raw: "hidden" },
      ],
    }),
    status: 502,
    state: "degraded",
    reasonCode: "provider_payload_invalid",
  },
];

for (const failure of providerFailures) {
  test(`provider ${failure.name} maps to a stable public state`, async () => {
    await withWorkerGlobals({ caches: undefined, fetch: failure.fetch }, async () => {
      const response = await worker.fetch(
        new Request(historyUrl, { headers: origin }),
        configuredEnv,
        {},
      );
      const text = await response.text();
      const body = JSON.parse(text);
      assert.equal(response.status, failure.status);
      assert.equal(body.state, failure.state);
      assert.equal(body.reasonCode, failure.reasonCode);
      assert.equal(text.includes("secret-for-test"), false);
      assert.equal(text.includes("hidden"), false);
      assert.equal(text.includes("network secret"), false);
    });
  });
}

test("disallowed origins and non-allowlisted symbols are rejected", async () => {
  const denied = await worker.fetch(new Request("https://worker.test/v1/search?q=AAPL", {
    headers: { Origin: "https://example.invalid" },
  }), {}, {});
  assert.equal(denied.status, 403);
  const unknown = await worker.fetch(new Request(
    "https://worker.test/v1/history?symbols=UNKNOWN&start=2026-01-01&end=2026-01-10",
    { headers: origin },
  ), configuredEnv, {});
  assert.equal(unknown.status, 400);
  assert.equal((await unknown.json()).reasonCode, "symbols_not_allowlisted");
});
