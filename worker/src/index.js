const STATE_VALUES = new Set([
  "published",
  "live_api",
  "stale",
  "degraded",
  "unavailable",
  "ruin",
]);

const ROWS = [
  ["kr-005930", "005930.KS", "Samsung Electronics", "equity", "KRX", "KRW", "Asia/Seoul", "krx", "005930"],
  ["kr-000660", "000660.KS", "SK hynix", "equity", "KRX", "KRW", "Asia/Seoul", "krx", "000660"],
  ["index-gspc", "^GSPC", "S&P 500 Index", "index", "INDEX", "USD", "America/New_York", "twelve_data", "SPX"],
  ["index-ndx", "^NDX", "NASDAQ-100 Index", "index", "INDEX", "USD", "America/New_York", "twelve_data", "NDX"],
  ["index-sox", "^SOX", "PHLX Semiconductor Sector Index", "index", "INDEX", "USD", "America/New_York", "twelve_data", "SOX"],
  ...["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","ORCL","CRM","NFLX","SPCX","AVGO","TSM","AMD","ASML","MU","QCOM","TXN","ADI","ARM","MRVL","MCHP","AMAT","LRCX","KLAC","TER","LITE","WDC","SNDK","STX"].map((symbol) => [
    `stock-${symbol.toLowerCase()}`, symbol, symbol, "equity",
    ["ORCL", "CRM", "TSM"].includes(symbol) ? "NYSE" : symbol === "SPCX" ? "US" : "NASDAQ",
    "USD", "America/New_York", "twelve_data", symbol,
  ]),
  ...["SPY","QQQ","VTI","IWM","SMH","SOXX","GLD","TLT","SSO","SDS","QLD","QID","USD","SSG"].map((symbol) => [
    `etf-${symbol.toLowerCase()}`, symbol, symbol, "etf",
    ["QQQ", "SMH", "SOXX", "TLT", "QLD", "QID"].includes(symbol) ? "NASDAQ" : "NYSE ARCA",
    "USD", "America/New_York", "twelve_data", symbol,
  ]),
  ["fx-usd-krw", "USD/KRW", "US Dollar / Korean Won", "fx", "FX", "KRW", "UTC", "twelve_data", "USD/KRW"],
];

const CATALOG = ROWS.map(([id, symbol, name, assetType, exchange, currency, timezone, provider, providerSymbol]) => ({
  id, symbol, name, assetType, exchange, currency, timezone, provider, providerSymbol,
}));
const BY_TOKEN = new Map();
for (const asset of CATALOG) {
  for (const token of [asset.id, asset.symbol, asset.providerSymbol]) {
    if (!BY_TOKEN.has(token.toUpperCase())) BY_TOKEN.set(token.toUpperCase(), asset);
  }
}

const MAX_SYMBOLS = 5;
const MAX_POINTS = 5000;
const MAX_RANGE_YEARS = 5;
const MAX_END_LAG_DAYS = 10;
const SPECIAL_EXCHANGES = new Set(["INDEX", "FX", "US"]);
const US_EXCHANGES = new Set(["AMEX", "BATS", "CBOE", "NASDAQ", "NYSE", "NYSEARCA", "US"]);

function isoNow() {
  return new Date().toISOString();
}

function allowedOrigin(request, env) {
  const origin = request.headers.get("Origin");
  if (!origin) return null;
  const allowed = (env.ALLOWED_ORIGINS || "https://sonchanggi.github.io")
    .split(",").map((value) => value.trim()).filter(Boolean);
  return allowed.includes(origin) ? origin : false;
}

function headers(origin, cacheControl = "no-store") {
  const result = {
    "Content-Type": "application/json; charset=utf-8",
    "Cache-Control": cacheControl,
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    Vary: "Origin",
  };
  if (origin) result["Access-Control-Allow-Origin"] = origin;
  return result;
}

function json(body, status, origin, cacheControl) {
  if (body.state && !STATE_VALUES.has(body.state)) throw new Error("invalid_state");
  return new Response(JSON.stringify(body), { status, headers: headers(origin, cacheControl) });
}

function errorBody(state, code) {
  return {
    schemaVersion: 1,
    contract: "kelly-worker-error",
    state,
    generatedAt: isoNow(),
    reasonCode: code,
  };
}

function parseDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value || "")) return null;
  const parsed = new Date(`${value}T00:00:00Z`);
  return Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== value ? null : parsed;
}

function configured(env) {
  return Boolean(env.TWELVE_DATA_API_KEY) && env.TWELVE_DATA_RIGHTS_APPROVED === "true";
}

function identityToken(value) {
  return String(value || "").toUpperCase().replaceAll(/[^A-Z0-9]/g, "");
}

function providerFailure(reasonCode, state = "degraded", httpStatus = 502) {
  const failure = new Error(reasonCode);
  failure.reasonCode = reasonCode;
  failure.state = state;
  failure.httpStatus = httpStatus;
  return failure;
}

function validateProviderIdentity(asset, meta) {
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    throw providerFailure("provider_identity_metadata_missing");
  }
  if (identityToken(meta.symbol) !== identityToken(asset.providerSymbol)) {
    throw providerFailure("provider_identity_symbol_mismatch");
  }

  const expectedExchange = identityToken(asset.exchange);
  const actualExchange = identityToken(meta.exchange);
  const providerType = identityToken(meta.type);
  if (!SPECIAL_EXCHANGES.has(expectedExchange) && actualExchange !== expectedExchange) {
    throw providerFailure("provider_identity_exchange_mismatch");
  }
  if (expectedExchange === "US" && !US_EXCHANGES.has(actualExchange)) {
    throw providerFailure("provider_identity_exchange_mismatch");
  }
  if (expectedExchange === "INDEX" && actualExchange !== "INDEX" && !providerType.includes("INDEX")) {
    throw providerFailure("provider_identity_exchange_mismatch");
  }
  if (expectedExchange === "FX") {
    const isFx = ["FX", "FOREX"].includes(actualExchange)
      || ["CURRENCY", "FOREX", "FX"].some((token) => providerType.includes(token));
    if (!isFx) throw providerFailure("provider_identity_exchange_mismatch");
  }
  if (asset.assetType !== "fx" && identityToken(meta.currency) !== identityToken(asset.currency)) {
    throw providerFailure("provider_identity_currency_mismatch");
  }
}

function resolveAssets(value) {
  const tokens = (value || "").split(",").map((item) => item.trim()).filter(Boolean);
  if (!tokens.length || tokens.length > MAX_SYMBOLS) return null;
  const assets = tokens.map((token) => BY_TOKEN.get(token.toUpperCase()));
  return assets.every(Boolean) && new Set(assets.map((asset) => asset.id)).size === assets.length ? assets : null;
}

function validateRange(url) {
  const start = parseDate(url.searchParams.get("start"));
  const end = parseDate(url.searchParams.get("end"));
  if (!start || !end || start > end) return null;
  const maximumEnd = new Date(start);
  maximumEnd.setUTCFullYear(maximumEnd.getUTCFullYear() + MAX_RANGE_YEARS);
  if (end > maximumEnd) return null;
  return { start: url.searchParams.get("start"), end: url.searchParams.get("end") };
}

async function providerSeries(asset, range, env) {
  if (asset.provider !== "twelve_data") {
    throw providerFailure("provider_not_available", "unavailable", 503);
  }
  const upstream = new URL("https://api.twelvedata.com/time_series");
  upstream.searchParams.set("symbol", asset.providerSymbol);
  if (!["INDEX", "FX", "US"].includes(asset.exchange)) upstream.searchParams.set("exchange", asset.exchange);
  upstream.searchParams.set("interval", "1day");
  upstream.searchParams.set("start_date", range.start);
  upstream.searchParams.set("end_date", range.end);
  upstream.searchParams.set("order", "asc");
  upstream.searchParams.set("adjust", ["equity", "etf"].includes(asset.assetType) ? "all" : "none");
  upstream.searchParams.set("outputsize", String(MAX_POINTS));
  upstream.searchParams.set("dp", "8");
  let response;
  try {
    response = await fetch(upstream, {
      headers: { Authorization: `apikey ${env.TWELVE_DATA_API_KEY}` },
    });
  } catch {
    throw providerFailure("provider_network_failure");
  }
  if (!response.ok) {
    const reasonCode = [401, 403, 404].includes(response.status)
      ? "provider_access_unavailable"
      : response.status === 429 ? "provider_rate_limited" : "provider_request_failed";
    const state = [401, 403, 404].includes(response.status) ? "unavailable" : "degraded";
    const httpStatus = response.status === 429 ? 429 : response.status < 500 ? 503 : 502;
    throw providerFailure(reasonCode, state, httpStatus);
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!payload || !Array.isArray(payload.values)) {
    throw providerFailure("provider_payload_invalid");
  }
  validateProviderIdentity(asset, payload.meta);
  if (payload.values.length >= MAX_POINTS) throw providerFailure("provider_result_truncated");

  const points = [];
  const seenDates = new Set();
  for (const value of payload.values) {
    const date = String(value.datetime || "").slice(0, 10);
    const close = Number(value.close);
    if (!parseDate(date) || date < range.start || date > range.end
      || !Number.isFinite(close) || close <= 0 || seenDates.has(date)) {
      throw providerFailure("provider_payload_invalid");
    }
    seenDates.add(date);
    points.push([date, close]);
  }
  if (!points.length) {
    throw providerFailure("series_unavailable", "unavailable", 404);
  }
  points.sort(([left], [right]) => left.localeCompare(right));
  const endLagDays = (parseDate(range.end) - parseDate(points.at(-1)[0])) / 86400000;
  if (endLagDays < 0 || endLagDays > MAX_END_LAG_DAYS) {
    throw providerFailure("provider_end_coverage_insufficient");
  }
  return points;
}

function normalizedDocument(assets, series, fxPair = null) {
  const dates = [...new Set(series.flatMap((points) => points.map(([date]) => date)))].sort();
  const prices = series.map((points) => {
    const byDate = new Map(points);
    return dates.map((date) => byDate.get(date) ?? null);
  });
  const returns = prices.map((row) => {
    let previous = null;
    return row.map((price) => {
      if (price === null) return null;
      if (previous === null) {
        previous = price;
        return null;
      }
      const result = price / previous - 1;
      previous = price;
      return result;
    });
  });
  const dataAsOf = dates.at(-1) || null;
  return {
    schemaVersion: 1,
    contract: "kelly-price-series",
    state: "live_api",
    generatedAt: isoNow(),
    dataAsOf,
    symbols: assets.map((asset) => asset.symbol),
    metadata: assets.map(({ id, symbol, name, assetType, exchange, currency, timezone }) => ({
      id, symbol, name, assetType, exchange, currency, timezone,
      returnBasis: assetType === "index" ? "price_return"
        : assetType === "fx" ? "fx_rate" : "total_return_approximation",
    })),
    dates,
    prices,
    returns,
    fx: fxPair ? { base: fxPair[0], quote: fxPair[1], rates: prices[0] } : null,
    source: {
      provider: "twelve_data",
      normalized: true,
      rawRedistribution: false,
      frequency: "daily",
      priceField: "close",
      license: "external_display_approved",
      cachedAt: isoNow(),
      attribution: "Data provided by Twelve Data",
    },
    limitations: ["Daily close series; dividends, fees, taxes, slippage, and financing are not guaranteed."],
  };
}

function originCacheRequest(request, origin) {
  const cacheUrl = new URL(request.url);
  cacheUrl.searchParams.set("__kelly_cache_origin", origin || "no-origin");
  return new Request(cacheUrl.toString(), { method: "GET" });
}

async function cachedNormalized(request, origin, ctx, producer) {
  const cache = globalThis.caches?.default;
  const cacheRequest = originCacheRequest(request, origin);
  if (cache) {
    const hit = await cache.match(cacheRequest);
    if (hit) return hit;
  }
  const response = await producer();
  if (cache && response.ok) {
    const write = cache.put(cacheRequest, response.clone());
    if (typeof ctx?.waitUntil === "function") ctx.waitUntil(write);
    else await write;
  }
  return response;
}

async function route(request, env, ctx, origin) {
  const url = new URL(request.url);
  if (url.pathname === "/v1/health") {
    const available = configured(env);
    return json({
      schemaVersion: 1,
      contract: "kelly-worker-health",
      state: available ? "live_api" : "unavailable",
      generatedAt: isoNow(),
      provider: "twelve_data",
      rightsApproved: env.TWELVE_DATA_RIGHTS_APPROVED === "true",
    }, available ? 200 : 503, origin);
  }
  if (url.pathname === "/v1/search") {
    const query = (url.searchParams.get("q") || "").trim().toLocaleLowerCase();
    const limit = Math.min(20, Math.max(1, Number(url.searchParams.get("limit") || 10)));
    if (query.length > 64 || !Number.isInteger(limit)) return json(errorBody("unavailable", "invalid_search"), 400, origin);
    const assets = CATALOG.filter((asset) => `${asset.id} ${asset.symbol} ${asset.name}`.toLocaleLowerCase().includes(query)).slice(0, limit);
    return json({ schemaVersion: 1, contract: "kelly-asset-search", state: "published", generatedAt: isoNow(), assets }, 200, origin, "public, max-age=300");
  }
  if (!["/v1/history", "/v1/fx"].includes(url.pathname)) return json(errorBody("unavailable", "route_not_found"), 404, origin);
  if (!configured(env)) return json(errorBody("unavailable", "provider_not_configured"), 503, origin);
  const range = validateRange(url);
  if (!range) return json(errorBody("unavailable", "invalid_date_range"), 400, origin);
  let assets;
  let fxPair = null;
  if (url.pathname === "/v1/fx") {
    const base = (url.searchParams.get("base") || "").toUpperCase();
    const quote = (url.searchParams.get("quote") || "").toUpperCase();
    if (base !== "USD" || quote !== "KRW") return json(errorBody("unavailable", "fx_pair_not_allowlisted"), 400, origin);
    assets = [BY_TOKEN.get("USD/KRW")];
    fxPair = [base, quote];
  } else {
    assets = resolveAssets(url.searchParams.get("symbols"));
    if (!assets) return json(errorBody("unavailable", "symbols_not_allowlisted"), 400, origin);
  }
  if (assets.some((asset) => asset.provider !== "twelve_data")) {
    return json(errorBody("unavailable", "provider_not_available"), 503, origin);
  }
  return cachedNormalized(request, origin, ctx, async () => {
    try {
      const series = await Promise.all(assets.map((asset) => providerSeries(asset, range, env)));
      return json(normalizedDocument(assets, series, fxPair), 200, origin, "public, max-age=300, s-maxage=3600");
    } catch (failure) {
      return json(errorBody(failure.state || "degraded", failure.reasonCode || "provider_request_failed"), failure.httpStatus || 502, origin);
    }
  });
}

export default {
  async fetch(request, env = {}, ctx = {}) {
    const origin = allowedOrigin(request, env);
    if (origin === false) return json(errorBody("unavailable", "origin_not_allowed"), 403, null);
    if (request.method === "OPTIONS") {
      const responseHeaders = headers(origin);
      responseHeaders["Access-Control-Allow-Methods"] = "GET, OPTIONS";
      responseHeaders["Access-Control-Allow-Headers"] = "Content-Type";
      responseHeaders["Access-Control-Max-Age"] = "86400";
      return new Response(null, { status: 204, headers: responseHeaders });
    }
    if (request.method !== "GET") return json(errorBody("unavailable", "method_not_allowed"), 405, origin);
    return route(request, env, ctx, origin);
  },
};

export const testSupport = { CATALOG, normalizedDocument, parseDate, resolveAssets };
