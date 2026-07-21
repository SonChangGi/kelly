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
const MAX_DAYS = 366 * 20;

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

function resolveAssets(value) {
  const tokens = (value || "").split(",").map((item) => item.trim()).filter(Boolean);
  if (!tokens.length || tokens.length > MAX_SYMBOLS) return null;
  const assets = tokens.map((token) => BY_TOKEN.get(token.toUpperCase()));
  return assets.every(Boolean) && new Set(assets.map((asset) => asset.id)).size === assets.length ? assets : null;
}

function validateRange(url) {
  const start = parseDate(url.searchParams.get("start"));
  const end = parseDate(url.searchParams.get("end"));
  if (!start || !end || start > end || (end - start) / 86400000 > MAX_DAYS) return null;
  return { start: url.searchParams.get("start"), end: url.searchParams.get("end") };
}

async function providerSeries(asset, range, env) {
  if (asset.provider !== "twelve_data") {
    const failure = new Error("provider_not_available");
    failure.reasonCode = "provider_not_available";
    failure.state = "unavailable";
    failure.httpStatus = 503;
    throw failure;
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
    const failure = new Error("provider_network_failure");
    failure.reasonCode = "provider_network_failure";
    failure.state = "degraded";
    failure.httpStatus = 502;
    throw failure;
  }
  if (!response.ok) {
    const failure = new Error("provider_request_failed");
    failure.reasonCode = [401, 403, 404].includes(response.status)
      ? "provider_access_unavailable"
      : response.status === 429 ? "provider_rate_limited" : "provider_request_failed";
    failure.state = [401, 403, 404].includes(response.status) ? "unavailable" : "degraded";
    failure.httpStatus = response.status === 429 ? 429 : response.status < 500 ? 503 : 502;
    throw failure;
  }
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!payload || !Array.isArray(payload.values)) {
    const failure = new Error("provider_payload_invalid");
    failure.reasonCode = "provider_payload_invalid";
    failure.state = "degraded";
    failure.httpStatus = 502;
    throw failure;
  }
  const points = [];
  for (const value of payload.values.slice(0, MAX_POINTS)) {
    const date = String(value.datetime || "").slice(0, 10);
    const close = Number(value.close);
    if (parseDate(date) && Number.isFinite(close) && close > 0) points.push([date, close]);
  }
  if (!points.length) {
    const failure = new Error("series_unavailable");
    failure.reasonCode = "series_unavailable";
    failure.state = "unavailable";
    failure.httpStatus = 404;
    throw failure;
  }
  return points;
}

function normalizedDocument(assets, series, fxPair = null) {
  const dates = [...new Set(series.flatMap((points) => points.map(([date]) => date)))].sort();
  const prices = series.map((points) => {
    const byDate = new Map(points);
    return dates.map((date) => byDate.get(date) ?? null);
  });
  const returns = prices.map((row) => row.map((price, index) => {
    if (index === 0 || price === null || row[index - 1] === null) return null;
    return price / row[index - 1] - 1;
  }));
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
      priceField: "adjusted_close",
      license: "external_display_approved",
      cachedAt: isoNow(),
      attribution: "Normalized from Twelve Data server-side response",
    },
    limitations: ["Daily close series; dividends, fees, taxes, slippage, and financing are not guaranteed."],
  };
}

async function cachedNormalized(request, ctx, producer) {
  const cache = globalThis.caches?.default;
  if (cache) {
    const hit = await cache.match(request);
    if (hit) return hit;
  }
  const response = await producer();
  if (cache && response.ok) ctx?.waitUntil?.(cache.put(request, response.clone()));
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
  return cachedNormalized(request, ctx, async () => {
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
