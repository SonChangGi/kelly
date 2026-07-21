import assert from "node:assert/strict";
import test from "node:test";

import worker, { testSupport } from "../src/index.js";

const origin = { Origin: "https://sonchanggi.github.io" };

test("catalog is the locked 50-asset allowlist", () => {
  assert.equal(testSupport.CATALOG.length, 50);
  assert.equal(new Set(testSupport.CATALOG.map((asset) => asset.id)).size, 50);
  assert.equal(testSupport.CATALOG.filter((asset) => asset.provider === "krx").length, 2);
});

test("health fails closed without rights and server secret", async () => {
  const response = await worker.fetch(new Request("https://worker.test/v1/health", { headers: origin }), {}, {});
  assert.equal(response.status, 503);
  assert.equal((await response.json()).state, "unavailable");
});

test("search is local and never needs a provider credential", async () => {
  const response = await worker.fetch(new Request("https://worker.test/v1/search?q=NVDA", { headers: origin }), {}, {});
  const body = await response.json();
  assert.equal(response.status, 200);
  assert.equal(body.state, "published");
  assert.equal(body.assets[0].symbol, "NVDA");
});

test("history normalizes provider observations and omits raw fields and secret", async (context) => {
  const previousFetch = globalThis.fetch;
  const previousCaches = globalThis.caches;
  context.after(() => {
    globalThis.fetch = previousFetch;
    globalThis.caches = previousCaches;
  });
  globalThis.caches = undefined;
  globalThis.fetch = async (_url, options) => {
    assert.equal(options.headers.Authorization, "apikey secret-for-test");
    return Response.json({
      meta: { unexpected: "must not escape" },
      values: [
        { datetime: "2026-01-02", open: "10", close: "100", vendorOnly: "hidden" },
        { datetime: "2026-01-05", open: "11", close: "110", vendorOnly: "hidden" },
      ],
    });
  };
  const request = new Request("https://worker.test/v1/history?symbols=NVDA&start=2026-01-01&end=2026-01-10", { headers: origin });
  const response = await worker.fetch(request, { TWELVE_DATA_API_KEY: "secret-for-test", TWELVE_DATA_RIGHTS_APPROVED: "true" }, {});
  const text = await response.text();
  const body = JSON.parse(text);
  assert.equal(response.status, 200);
  assert.equal(body.contract, "kelly-price-series");
  assert.equal(body.state, "live_api");
  assert.deepEqual(body.prices, [[100, 110]]);
  assert.equal(body.returns[0][0], null);
  assert.ok(Math.abs(body.returns[0][1] - 0.1) < 1e-12);
  assert.equal(text.includes("vendorOnly"), false);
  assert.equal(text.includes("secret-for-test"), false);
  assert.equal(body.source.rawRedistribution, false);
});

test("disallowed origins and non-allowlisted symbols are rejected", async () => {
  const denied = await worker.fetch(new Request("https://worker.test/v1/search?q=AAPL", { headers: { Origin: "https://example.invalid" } }), {}, {});
  assert.equal(denied.status, 403);
  const unknown = await worker.fetch(
    new Request("https://worker.test/v1/history?symbols=UNKNOWN&start=2026-01-01&end=2026-01-10", { headers: origin }),
    { TWELVE_DATA_API_KEY: "secret", TWELVE_DATA_RIGHTS_APPROVED: "true" },
    {},
  );
  assert.equal(unknown.status, 400);
  assert.equal((await unknown.json()).reasonCode, "symbols_not_allowlisted");
});
