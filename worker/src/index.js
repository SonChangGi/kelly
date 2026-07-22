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
  ["index-gspc", "^GSPC", "S&P 500 Index", "index", "INDEX", "USD", "America/New_York", "yahoo_chart", "^GSPC"],
  ["index-ndx", "^NDX", "NASDAQ-100 Index", "index", "INDEX", "USD", "America/New_York", "yahoo_chart", "^NDX"],
  ["index-sox", "^SOX", "PHLX Semiconductor Sector Index", "index", "INDEX", "USD", "America/New_York", "yahoo_chart", "^SOX"],
  ...["AAPL","MSFT","NVDA","AMZN","GOOGL","META","TSLA","ORCL","CRM","NFLX","SPCX","AVGO","TSM","AMD","ASML","MU","QCOM","TXN","ADI","ARM","MRVL","MCHP","AMAT","LRCX","KLAC","TER","LITE","WDC","SNDK","STX"].map((symbol) => [
    `stock-${symbol.toLowerCase()}`, symbol, symbol, "equity",
    ["ORCL", "CRM", "TSM"].includes(symbol) ? "NYSE" : "NASDAQ",
    "USD", "America/New_York", "yahoo_chart", symbol,
  ]),
  ...["SPY","QQQ","VTI","IWM","SMH","SOXX","GLD","TLT","SSO","SDS","QLD","QID","USD","SSG"].map((symbol) => [
    `etf-${symbol.toLowerCase()}`, symbol, symbol, "etf",
    ["QQQ", "SMH", "SOXX", "TLT", "QLD", "QID"].includes(symbol) ? "NASDAQ" : "NYSE ARCA",
    "USD", "America/New_York", "yahoo_chart", symbol,
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
const MAX_SEARCH_QUERY = 48;
const MAX_SEARCH_RESULTS = 20;
const YAHOO_CHART_ORIGIN = "https://query2.finance.yahoo.com";
const YAHOO_SEARCH_URL = `${YAHOO_CHART_ORIGIN}/v1/finance/search`;
const YAHOO_REQUEST_HEADERS = {
  Accept: "application/json",
  "User-Agent": "Mozilla/5.0 (compatible; KellyAllocationLab/1.0; +https://github.com/SonChangGi/kelly)",
};
const SPECIAL_EXCHANGES = new Set(["INDEX", "FX", "US"]);
const TWELVE_US_EXCHANGES = new Set(["AMEX", "BATS", "CBOE", "NASDAQ", "NYSE", "NYSEARCA", "US"]);
const YAHOO_US_EXCHANGES = new Set([
  "ASE", "BATS", "BTS", "NCM", "NGM", "NMS", "NASDAQ", "NYA", "NYE", "NYQ", "NYSE", "PCX",
]);
const YAHOO_TYPES = new Map([["EQUITY", "equity"], ["ETF", "etf"], ["INDEX", "index"]]);
const EXCLUDED_3X_SYMBOLS = new Set([
  "BERZ", "BULZ", "CURE", "DRIP", "DRN", "DRV", "DUST", "EDC", "EDZ", "ERX", "ERY",
  "FAS", "FAZ", "GUSH", "JDST", "JNUG", "LABD", "LABU", "MIDU", "NUGT", "RETL", "SDOW",
  "SOXL", "SOXS", "SPXL", "SPXS", "SQQQ", "TECL", "TECS", "TMF", "TMV", "TNA", "TQQQ",
  "TZA", "UDOW", "UMDD", "UPRO", "WEBL", "WEBS", "YANG", "YINN",
]);
const SAFE_US_SYMBOL = /^[A-Z][A-Z0-9]{0,9}(?:[.-][A-Z0-9]{1,5})?$/;
const SAFE_SEARCH_QUERY = /^[A-Za-z0-9][A-Za-z0-9 .&'()-]*$/;
const RATE_WINDOWS = new Map();
const RATE_WINDOW_MS = 60_000;
const MAX_RATE_KEYS = 2048;

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

function twelveConfigured(env) {
  return Boolean(env.TWELVE_DATA_API_KEY) && env.TWELVE_DATA_RIGHTS_APPROVED === "true";
}

function yahooDisplayApproved(env) {
  return env.YAHOO_PUBLIC_DISPLAY_APPROVED === "true";
}

function identityToken(value) {
  return String(value || "").toUpperCase().replaceAll(/[^A-Z0-9]/g, "");
}

function normalizeUsSymbol(value) {
  const symbol = String(value || "").trim().toUpperCase();
  if (!SAFE_US_SYMBOL.test(symbol)) return null;
  return symbol.replace(".", "-");
}

function providerFailure(reasonCode, state = "degraded", httpStatus = 502) {
  const failure = new Error(reasonCode);
  failure.reasonCode = reasonCode;
  failure.state = state;
  failure.httpStatus = httpStatus;
  return failure;
}

function rateLimitValue(value, fallback) {
  const parsed = Number(value);
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 600 ? parsed : fallback;
}

function consumeRate(request, env, bucket, cost = 1) {
  const now = Date.now();
  const limit = rateLimitValue(
    bucket === "search" ? env.SEARCH_RATE_LIMIT_PER_MINUTE : env.HISTORY_RATE_LIMIT_PER_MINUTE,
    bucket === "search" ? 30 : 60,
  );
  const client = request.headers.get("CF-Connecting-IP") || "anonymous";
  const key = `${bucket}:${client}`;
  let window = RATE_WINDOWS.get(key);
  if (!window || now >= window.resetAt) window = { count: 0, resetAt: now + RATE_WINDOW_MS };
  if (window.count + cost > limit) return false;
  window.count += cost;
  RATE_WINDOWS.set(key, window);
  if (RATE_WINDOWS.size > MAX_RATE_KEYS) {
    for (const [candidate, value] of RATE_WINDOWS) {
      if (now >= value.resetAt || RATE_WINDOWS.size > MAX_RATE_KEYS) RATE_WINDOWS.delete(candidate);
    }
  }
  return true;
}

function validateTwelveIdentity(asset, meta) {
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
  if (expectedExchange === "US" && !TWELVE_US_EXCHANGES.has(actualExchange)) {
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

function normalizedYahooExchange(exchange) {
  const value = identityToken(exchange);
  if (["NMS", "NGM", "NCM", "NASDAQ"].includes(value)) return "NASDAQ";
  if (["NYQ", "NYA", "NYE", "NYSE"].includes(value)) return "NYSE";
  if (value === "PCX") return "NYSE ARCA";
  if (value === "ASE") return "NYSE AMERICAN";
  if (["BATS", "BTS"].includes(value)) return "BATS";
  return null;
}

function dynamicAsset(symbol, assetType = null, meta = {}) {
  const typePrefix = assetType === "etf" ? "etf" : assetType === "equity" ? "stock" : "us";
  const slug = symbol.toLowerCase().replaceAll(/[^a-z0-9]+/g, "-").replaceAll(/^-|-$/g, "");
  return {
    id: `${typePrefix}-${slug}`,
    symbol,
    name: String(meta.longName || meta.shortName || symbol).slice(0, 160),
    assetType,
    exchange: normalizedYahooExchange(meta.exchangeName || meta.exchange) || "US",
    currency: "USD",
    timezone: "America/New_York",
    provider: "yahoo_chart",
    providerSymbol: symbol,
  };
}

function isExcluded3xProduct(symbol, assetType, meta = {}) {
  if (assetType !== "etf") return false;
  if (EXCLUDED_3X_SYMBOLS.has(symbol)) return true;
  const name = `${meta.longName || meta.longname || ""} ${meta.shortName || meta.shortname || ""}`.toUpperCase();
  return name.includes("ULTRAPRO") || /(^|[^0-9])3\s*X([^0-9]|$)/.test(name);
}

function validateYahooIdentity(asset, meta) {
  if (!meta || typeof meta !== "object" || Array.isArray(meta)) {
    throw providerFailure("provider_identity_metadata_missing");
  }
  const observedSymbol = asset.assetType === "index"
    ? String(meta.symbol || "").toUpperCase()
    : normalizeUsSymbol(meta.symbol);
  if (observedSymbol !== asset.providerSymbol) {
    throw providerFailure("provider_identity_symbol_mismatch");
  }
  const providerType = YAHOO_TYPES.get(identityToken(meta.instrumentType));
  const expectedType = asset.assetType;
  if (!providerType || (expectedType && providerType !== expectedType)) {
    throw providerFailure("provider_identity_type_mismatch");
  }
  if (isExcluded3xProduct(asset.providerSymbol, providerType, meta)) {
    throw providerFailure("excluded_3x_product", "unavailable", 400);
  }
  if (providerType === "index") {
    if (expectedType !== "index") throw providerFailure("provider_identity_type_mismatch");
  } else {
    if (!YAHOO_US_EXCHANGES.has(identityToken(meta.exchangeName))) {
      throw providerFailure("provider_identity_exchange_mismatch");
    }
    if (identityToken(meta.currency) !== "USD") {
      throw providerFailure("provider_identity_currency_mismatch");
    }
    if (meta.exchangeTimezoneName !== "America/New_York") {
      throw providerFailure("provider_identity_timezone_mismatch");
    }
  }
  return dynamicAsset(asset.providerSymbol, providerType, meta);
}

function resolveAssets(value) {
  const tokens = (value || "").split(",").map((item) => item.trim()).filter(Boolean);
  if (!tokens.length || tokens.length > MAX_SYMBOLS) return null;
  const assets = [];
  for (const rawToken of tokens) {
    const candidateSymbol = rawToken.toUpperCase();
    const catalogAsset = BY_TOKEN.get(candidateSymbol);
    if (catalogAsset) {
      assets.push(catalogAsset);
    } else if (normalizeUsSymbol(candidateSymbol)) {
      assets.push(dynamicAsset(normalizeUsSymbol(candidateSymbol)));
    } else {
      return null;
    }
  }
  return new Set(assets.map((asset) => asset.symbol)).size === assets.length ? assets : null;
}

function validateRange(url) {
  const start = parseDate(url.searchParams.get("start"));
  const end = parseDate(url.searchParams.get("end"));
  if (!start || !end || start > end) return null;
  const maximumEnd = new Date(start);
  maximumEnd.setUTCFullYear(maximumEnd.getUTCFullYear() + MAX_RANGE_YEARS);
  if (end > maximumEnd) return null;
  const tomorrow = new Date();
  tomorrow.setUTCHours(0, 0, 0, 0);
  tomorrow.setUTCDate(tomorrow.getUTCDate() + 1);
  if (end > tomorrow) return null;
  return { start: url.searchParams.get("start"), end: url.searchParams.get("end") };
}

function yahooFailureStatus(response) {
  if (response.status === 404) return providerFailure("series_unavailable", "unavailable", 404);
  if ([401, 403].includes(response.status)) return providerFailure("provider_access_unavailable", "unavailable", 503);
  if (response.status === 429) return providerFailure("provider_rate_limited", "degraded", 429);
  return providerFailure("provider_request_failed", "degraded", response.status < 500 ? 503 : 502);
}

function yahooChartUrl(symbol, range) {
  const upstream = new URL(`${YAHOO_CHART_ORIGIN}/v8/finance/chart/${encodeURIComponent(symbol)}`);
  const period1 = Math.floor(parseDate(range.start).valueOf() / 1000);
  const exclusiveEnd = parseDate(range.end);
  exclusiveEnd.setUTCDate(exclusiveEnd.getUTCDate() + 1);
  upstream.searchParams.set("period1", String(period1));
  upstream.searchParams.set("period2", String(Math.floor(exclusiveEnd.valueOf() / 1000)));
  upstream.searchParams.set("interval", "1d");
  upstream.searchParams.set("events", "div,splits");
  upstream.searchParams.set("includeAdjustedClose", "true");
  return upstream;
}

async function yahooSeries(asset, range) {
  const upstream = yahooChartUrl(asset.providerSymbol, range);
  let response;
  try {
    response = await fetch(upstream, { headers: YAHOO_REQUEST_HEADERS });
  } catch {
    throw providerFailure("provider_network_failure");
  }
  if (!response.ok) throw yahooFailureStatus(response);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  const chart = payload?.chart;
  if (chart?.error || !Array.isArray(chart?.result) || chart.result.length !== 1) {
    if (chart?.error?.code === "Not Found" || (Array.isArray(chart?.result) && chart.result.length === 0)) {
      throw providerFailure("series_unavailable", "unavailable", 404);
    }
    throw providerFailure("provider_payload_invalid");
  }
  const result = chart.result[0];
  const resolvedAsset = validateYahooIdentity(asset, result.meta);
  const timestamps = result.timestamp;
  const closes = result.indicators?.quote?.[0]?.close;
  if (!Array.isArray(timestamps) || !Array.isArray(closes) || timestamps.length !== closes.length) {
    throw providerFailure("provider_payload_invalid");
  }
  if (timestamps.length >= MAX_POINTS) throw providerFailure("provider_result_truncated");

  let values = closes;
  let priceField = "close";
  if (["equity", "etf"].includes(resolvedAsset.assetType)) {
    values = result.indicators?.adjclose?.[0]?.adjclose;
    if (!Array.isArray(values) || values.length !== timestamps.length) {
      throw providerFailure("provider_adjusted_close_missing");
    }
    priceField = "adjusted_close";
  }

  const points = [];
  const seenDates = new Set();
  for (let index = 0; index < timestamps.length; index += 1) {
    if (!Number.isFinite(timestamps[index])) throw providerFailure("provider_payload_invalid");
    const instant = new Date(timestamps[index] * 1000);
    if (Number.isNaN(instant.valueOf())) throw providerFailure("provider_payload_invalid");
    const date = instant.toISOString().slice(0, 10);
    const rawValue = values[index];
    if (rawValue === null || rawValue === undefined) continue;
    const close = Number(rawValue);
    if (!parseDate(date) || date < range.start || date > range.end
      || !Number.isFinite(close) || close <= 0 || seenDates.has(date)) {
      throw providerFailure("provider_payload_invalid");
    }
    seenDates.add(date);
    points.push([date, close]);
  }
  if (!points.length) throw providerFailure("series_unavailable", "unavailable", 404);
  points.sort(([left], [right]) => left.localeCompare(right));
  const endLagDays = (parseDate(range.end) - parseDate(points.at(-1)[0])) / 86400000;
  if (endLagDays < 0 || endLagDays > MAX_END_LAG_DAYS) {
    throw providerFailure("provider_end_coverage_insufficient");
  }
  return { asset: { ...resolvedAsset, priceField }, points };
}

async function twelveSeries(asset, range, env) {
  const upstream = new URL("https://api.twelvedata.com/time_series");
  upstream.searchParams.set("symbol", asset.providerSymbol);
  upstream.searchParams.set("interval", "1day");
  upstream.searchParams.set("start_date", range.start);
  upstream.searchParams.set("end_date", range.end);
  upstream.searchParams.set("order", "asc");
  upstream.searchParams.set("adjust", "none");
  upstream.searchParams.set("outputsize", String(MAX_POINTS));
  upstream.searchParams.set("dp", "8");
  let response;
  try {
    response = await fetch(upstream, { headers: { Authorization: `apikey ${env.TWELVE_DATA_API_KEY}` } });
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
  if (!payload || !Array.isArray(payload.values)) throw providerFailure("provider_payload_invalid");
  validateTwelveIdentity(asset, payload.meta);
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
  if (!points.length) throw providerFailure("series_unavailable", "unavailable", 404);
  points.sort(([left], [right]) => left.localeCompare(right));
  const endLagDays = (parseDate(range.end) - parseDate(points.at(-1)[0])) / 86400000;
  if (endLagDays < 0 || endLagDays > MAX_END_LAG_DAYS) throw providerFailure("provider_end_coverage_insufficient");
  return { asset: { ...asset, priceField: "close" }, points };
}

function searchAsset(quote) {
  const symbol = String(quote?.symbol || "").toUpperCase();
  const assetType = YAHOO_TYPES.get(identityToken(quote?.quoteType));
  if (!SAFE_US_SYMBOL.test(symbol) || !["equity", "etf"].includes(assetType)
    || !YAHOO_US_EXCHANGES.has(identityToken(quote?.exchange))) return null;
  if (isExcluded3xProduct(symbol, assetType, quote)) return null;
  return dynamicAsset(symbol, assetType, {
    longName: quote.longname,
    shortName: quote.shortname,
    exchange: quote.exchange,
  });
}

async function yahooSearch(query, limit) {
  const upstream = new URL(YAHOO_SEARCH_URL);
  upstream.searchParams.set("q", query);
  upstream.searchParams.set("quotesCount", String(Math.min(50, Math.max(limit * 3, 10))));
  upstream.searchParams.set("newsCount", "0");
  upstream.searchParams.set("enableFuzzyQuery", "false");
  upstream.searchParams.set("region", "US");
  upstream.searchParams.set("lang", "en-US");
  let response;
  try {
    response = await fetch(upstream, { headers: YAHOO_REQUEST_HEADERS });
  } catch {
    throw providerFailure("provider_network_failure");
  }
  if (!response.ok) throw yahooFailureStatus(response);
  let payload;
  try {
    payload = await response.json();
  } catch {
    payload = null;
  }
  if (!payload || !Array.isArray(payload.quotes)) throw providerFailure("provider_payload_invalid");
  return payload.quotes.map(searchAsset).filter(Boolean);
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
  const yahoo = assets.some((asset) => asset.provider === "yahoo_chart");
  const priceFields = Object.fromEntries(assets.map((asset) => [
    asset.symbol,
    asset.priceField || (asset.assetType === "index" ? "close" : "adjusted_close"),
  ]));
  const uniquePriceFields = [...new Set(Object.values(priceFields))];
  return {
    schemaVersion: 1,
    contract: "kelly-price-series",
    state: "live_api",
    generatedAt: isoNow(),
    dataAsOf,
    symbols: assets.map((asset) => asset.symbol),
    metadata: assets.map(({ id, symbol, name, assetType, exchange, currency, timezone, priceField }) => ({
      id, symbol, name, assetType, exchange, currency, timezone,
      returnBasis: assetType === "index" ? "price_return"
        : assetType === "fx" ? "fx_rate" : "total_return_approximation",
      priceField: priceField || (assetType === "index" || assetType === "fx" ? "close" : "adjusted_close"),
    })),
    dates,
    prices,
    returns,
    fx: fxPair ? { base: fxPair[0], quote: fxPair[1], rates: prices[0] } : null,
    source: yahoo ? {
      provider: "yahoo_finance",
      normalized: true,
      rawRedistribution: false,
      frequency: "daily",
      priceField: uniquePriceFields.length === 1 ? uniquePriceFields[0] : "by_asset",
      priceFieldBySymbol: priceFields,
      license: "provider_terms_apply",
      cachedAt: isoNow(),
      attribution: "Yahoo Finance",
    } : {
      provider: "twelve_data",
      normalized: true,
      rawRedistribution: false,
      frequency: "daily",
      priceField: "close",
      priceFieldBySymbol: priceFields,
      license: "external_display_approved",
      cachedAt: isoNow(),
      attribution: "Data provided by Twelve Data",
    },
    limitations: yahoo
      ? ["Yahoo Finance terms apply. Adjusted close approximates total return for equities and ETFs; indices use raw close."]
      : ["Daily close series; fees, taxes, slippage, and financing are not guaranteed."],
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

function localSearch(query, limit) {
  const normalized = query.toLocaleLowerCase();
  return CATALOG.filter((asset) => `${asset.id} ${asset.symbol} ${asset.name}`.toLocaleLowerCase().includes(normalized)).slice(0, limit);
}

async function searchRoute(request, env, ctx, origin, url) {
  const query = (url.searchParams.get("q") || "").trim();
  const limit = Number(url.searchParams.get("limit") || 10);
  if (!Number.isInteger(limit) || limit < 1 || limit > MAX_SEARCH_RESULTS
    || query.length > MAX_SEARCH_QUERY || (query && !SAFE_SEARCH_QUERY.test(query))) {
    return json(errorBody("unavailable", "invalid_search"), 400, origin);
  }
  if (!yahooDisplayApproved(env)) {
    return json(errorBody("unavailable", "provider_display_rights_unconfirmed"), 503, origin);
  }
  const local = localSearch(query, limit);
  if (!query || BY_TOKEN.has(query.toUpperCase())) {
    return json({
      schemaVersion: 1,
      contract: "kelly-asset-search",
      state: "published",
      generatedAt: isoNow(),
      assets: local,
    }, 200, origin, "public, max-age=300");
  }
  return cachedNormalized(request, origin, ctx, async () => {
    if (!consumeRate(request, env, "search")) {
      return json(errorBody("degraded", "worker_rate_limited"), 429, origin);
    }
    try {
      const discovered = await yahooSearch(query, limit);
      const assets = [];
      const seen = new Set();
      for (const asset of [...local, ...discovered]) {
        if (!seen.has(asset.symbol)) {
          assets.push(asset);
          seen.add(asset.symbol);
        }
        if (assets.length === limit) break;
      }
      return json({
        schemaVersion: 1,
        contract: "kelly-asset-search",
        state: "live_api",
        generatedAt: isoNow(),
        assets,
      }, 200, origin, "public, max-age=300, s-maxage=1800");
    } catch (failure) {
      if (local.length) {
        return json({
          schemaVersion: 1,
          contract: "kelly-asset-search",
          state: "degraded",
          generatedAt: isoNow(),
          reasonCode: failure.reasonCode || "provider_request_failed",
          assets: local,
        }, 200, origin, "public, max-age=60");
      }
      return json(errorBody(failure.state || "degraded", failure.reasonCode || "provider_request_failed"), failure.httpStatus || 502, origin);
    }
  });
}

async function route(request, env, ctx, origin) {
  const url = new URL(request.url);
  if (url.pathname === "/v1/health") {
    const yahooAvailable = yahooDisplayApproved(env);
    const fxAvailable = twelveConfigured(env);
    const available = yahooAvailable || fxAvailable;
    return json({
      schemaVersion: 1,
      contract: "kelly-worker-health",
      state: available ? "live_api" : "unavailable",
      generatedAt: isoNow(),
      provider: yahooAvailable && fxAvailable ? "mixed"
        : yahooAvailable ? "yahoo_finance" : fxAvailable ? "twelve_data" : "none",
      keyRequired: !yahooAvailable && fxAvailable,
      rightsApproved: yahooAvailable,
      capabilities: {
        search: yahooAvailable ? "live_api" : "unavailable",
        usHistory: yahooAvailable ? "live_api" : "unavailable",
        fx: fxAvailable ? "live_api" : "unavailable",
        krx: "unavailable",
      },
    }, available ? 200 : 503, origin, "public, max-age=60");
  }
  if (url.pathname === "/v1/search") return searchRoute(request, env, ctx, origin, url);
  if (!["/v1/history", "/v1/fx"].includes(url.pathname)) {
    return json(errorBody("unavailable", "route_not_found"), 404, origin);
  }
  const range = validateRange(url);
  if (!range) return json(errorBody("unavailable", "invalid_date_range"), 400, origin);

  let assets;
  let fxPair = null;
  if (url.pathname === "/v1/fx") {
    const base = (url.searchParams.get("base") || "").toUpperCase();
    const quote = (url.searchParams.get("quote") || "").toUpperCase();
    if (base !== "USD" || quote !== "KRW") {
      return json(errorBody("unavailable", "fx_pair_not_allowlisted"), 400, origin);
    }
    if (!twelveConfigured(env)) return json(errorBody("unavailable", "provider_not_configured"), 503, origin);
    assets = [BY_TOKEN.get("USD/KRW")];
    fxPair = [base, quote];
  } else {
    assets = resolveAssets(url.searchParams.get("symbols"));
    if (!assets) return json(errorBody("unavailable", "invalid_symbols"), 400, origin);
    if (assets.some((asset) => asset.provider === "krx")) {
      return json(errorBody("unavailable", "provider_not_available"), 503, origin);
    }
    if (assets.some((asset) => asset.provider !== "yahoo_chart")) {
      return json(errorBody("unavailable", "provider_not_available"), 503, origin);
    }
    if (!yahooDisplayApproved(env)) {
      return json(errorBody("unavailable", "provider_display_rights_unconfirmed"), 503, origin);
    }
  }

  return cachedNormalized(request, origin, ctx, async () => {
    if (!consumeRate(request, env, "history", assets.length)) {
      return json(errorBody("degraded", "worker_rate_limited"), 429, origin);
    }
    try {
      const results = url.pathname === "/v1/fx"
        ? await Promise.all(assets.map((asset) => twelveSeries(asset, range, env)))
        : await Promise.all(assets.map((asset) => yahooSeries(asset, range)));
      return json(
        normalizedDocument(results.map((result) => result.asset), results.map((result) => result.points), fxPair),
        200,
        origin,
        "public, max-age=300, s-maxage=3600",
      );
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

export const testSupport = {
  CATALOG,
  normalizedDocument,
  parseDate,
  resolveAssets,
  resetRateLimits() { RATE_WINDOWS.clear(); },
};
