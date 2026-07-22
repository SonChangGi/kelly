import {
  ANNUALIZATION_DAYS,
  MAX_EXPOSURE,
  STATUS,
  REASON,
  applyExplorationRange,
  continuousGrowthRate,
  createPeriodState,
  estimateHistoricalMoments,
  exactHistoricalKelly,
  innerJoinReturnSeries,
  isValidDateRange,
  leveragedReturnPath,
  normalizeAssetPayload,
  performanceMetrics,
  portfolioKelly,
  rebalanceComparison,
  rowsToCsv,
  setExplorationRange,
  sliceJoinedReturnSeries,
  singleAssetKelly,
  sliceSeries,
  validateCorrelationMatrix,
  wealthPath,
} from "./engine.js?v=20260722.3";
import {
  clearChart,
  disposeCharts,
  rebalanceAxisLabels,
  renderCorrelationHeatmap,
  renderDrawdownChart,
  renderGrowthCurve,
  renderRebalanceChart,
  renderWealthChart,
  renderWeightsChart,
} from "./charts.js?v=20260722.3";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const percentage = new Intl.NumberFormat("ko-KR", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 2 });
const decimal = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const FX_GAP_REASON = "FX_GAP_EXCEEDED";
const SHARE_MODES = new Set(["historical", "direct", "portfolio"]);
const REBALANCE_FREQUENCIES = new Set(["none", "daily", "weekly", "monthly", "quarterly", "yearly"]);
const CURRENCIES = new Set(["native", "krw"]);
const PORTFOLIO_SOURCES = new Set(["direct", "historical"]);
const PORTFOLIO_MIN_ASSETS = 2;
const PORTFOLIO_MAX_ASSETS = 5;
const MIN_EFFECTIVE_RATE_PERCENT = -99.999999;
const MIN_KELLY_OBSERVATIONS = 60;

function shareParams(value) {
  if (value instanceof URLSearchParams) return new URLSearchParams(value);
  const text = String(value ?? "");
  return new URLSearchParams(text.startsWith("?") ? text.slice(1) : text);
}

function safeNumber(value, { minimum = -Infinity, maximum = Infinity, integer = false } = {}) {
  if (value === null || value === undefined || String(value).trim() === "") return undefined;
  const parsed = Number(value);
  if (!Number.isFinite(parsed) || parsed < minimum || parsed > maximum || (integer && !Number.isInteger(parsed))) return undefined;
  return parsed;
}

function safeDate(value) {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value ?? "")) return undefined;
  const parsed = new Date(`${value}T00:00:00Z`);
  return !Number.isNaN(parsed.valueOf()) && parsed.toISOString().slice(0, 10) === value ? value : undefined;
}

function safeAssetId(value) {
  const text = String(value ?? "").trim();
  return /^[A-Za-z0-9.^/_-]{1,64}$/.test(text) ? text : undefined;
}

function safeDateRange(params) {
  const start = safeDate(params.get("start"));
  const end = safeDate(params.get("end"));
  return start && end && start < end ? { start, end } : {};
}

function safeJson(value, maximumLength = 5000) {
  if (typeof value !== "string" || !value || value.length > maximumLength) return undefined;
  try { return JSON.parse(value); } catch { return undefined; }
}

function safeDirectPortfolio(params) {
  const rawAssets = safeJson(params.get("assets"));
  const rawCorrelation = safeJson(params.get("corr"));
  if (!Array.isArray(rawAssets) || rawAssets.length < PORTFOLIO_MIN_ASSETS || rawAssets.length > PORTFOLIO_MAX_ASSETS) return {};
  const assets = rawAssets.map((asset, index) => {
    if (!asset || typeof asset !== "object" || Array.isArray(asset)) return null;
    const name = String(asset.name ?? "").trim();
    const expectedExcess = safeNumber(asset.expectedExcess, { minimum: -1000, maximum: 1000 });
    const volatility = safeNumber(asset.volatility, { minimum: Number.EPSILON, maximum: 1000 });
    if (!name || name.length > 64 || expectedExcess === undefined || volatility === undefined) return null;
    return { key: `direct-${index + 1}`, name, expectedExcess, volatility };
  });
  if (assets.some((asset) => asset === null) || !Array.isArray(rawCorrelation) || rawCorrelation.length !== assets.length) return {};
  if (rawCorrelation.some((row) => !Array.isArray(row) || row.length !== assets.length
    || row.some((value) => typeof value !== "number" || !Number.isFinite(value)))) return {};
  const correlation = rawCorrelation.map((row) => [...row]);
  if (!validateCorrelationMatrix(correlation).valid) return {};
  return { directAssets: assets, correlation };
}

function safeHistoricalPortfolio(params) {
  const assetIds = String(params.get("assets") ?? "").split(",").map(safeAssetId).filter(Boolean);
  if (assetIds.length < PORTFOLIO_MIN_ASSETS || assetIds.length > PORTFOLIO_MAX_ASSETS || new Set(assetIds).size !== assetIds.length) return {};
  return {
    historicalAssetIds: assetIds,
    ...safeDateRange(params),
    rebalance: REBALANCE_FREQUENCIES.has(params.get("rebalance")) ? params.get("rebalance") : undefined,
    transactionCostBps: safeNumber(params.get("cost"), { minimum: 0, maximum: 100000 }),
  };
}

function parseShareState(value) {
  const params = shareParams(value);
  const requestedMode = params.get("mode");
  const mode = SHARE_MODES.has(requestedMode) ? requestedMode : "historical";
  const result = { mode };
  if (mode === "historical") {
    const mar = params.has("mar") && params.get("mar") === ""
      ? ""
      : safeNumber(params.get("mar"), { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 });
    result.historical = {
      asset: safeAssetId(params.get("asset")),
      ...safeDateRange(params),
      currency: CURRENCIES.has(params.get("currency")) ? params.get("currency") : undefined,
      riskFreeRate: safeNumber(params.get("rf"), { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 }),
      borrowingSpread: safeNumber(params.get("borrowSpread"), { minimum: 0, maximum: 1000 }),
      transactionCostBps: safeNumber(params.get("cost"), { minimum: 0, maximum: 100000 }),
      annualizationDays: safeNumber(params.get("annualization"), { minimum: 1, maximum: 366, integer: true }),
      mar,
      rebalance: REBALANCE_FREQUENCIES.has(params.get("rebalance")) ? params.get("rebalance") : undefined,
    };
  } else if (mode === "direct") {
    result.direct = {
      expectedExcess: safeNumber(params.get("excess"), { minimum: -1000, maximum: 1000 }),
      volatility: safeNumber(params.get("vol"), { minimum: 0, maximum: 1000 }),
      riskFreeRate: safeNumber(params.get("rf"), { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 }),
      borrowingSpread: safeNumber(params.get("spread"), { minimum: 0, maximum: 1000 }),
    };
  } else {
    const requestedSource = params.get("source");
    const source = PORTFOLIO_SOURCES.has(requestedSource) ? requestedSource : "direct";
    result.portfolio = {
      source,
      riskFreeRate: safeNumber(params.get("rf"), { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 }),
      borrowingSpread: safeNumber(params.get("spread"), { minimum: 0, maximum: 1000 }),
      cap: safeNumber(params.get("cap"), { minimum: Number.EPSILON, maximum: MAX_EXPOSURE }),
      ...(source === "direct" ? safeDirectPortfolio(params) : safeHistoricalPortfolio(params)),
    };
  }
  return result;
}

function setShareNumber(params, key, value, constraints) {
  const safe = safeNumber(value, constraints);
  if (safe !== undefined) params.set(key, String(safe));
}

function serializeShareState(configuration) {
  const mode = SHARE_MODES.has(configuration?.mode) ? configuration.mode : "historical";
  const params = new URLSearchParams({ mode });
  if (mode === "historical") {
    const historical = configuration.historical ?? {};
    const asset = safeAssetId(historical.asset);
    if (asset) params.set("asset", asset);
    const start = safeDate(historical.start);
    const end = safeDate(historical.end);
    if (start && end && start < end) { params.set("start", start); params.set("end", end); }
    if (CURRENCIES.has(historical.currency)) params.set("currency", historical.currency);
    setShareNumber(params, "rf", historical.riskFreeRate, { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 });
    setShareNumber(params, "borrowSpread", historical.borrowingSpread, { minimum: 0, maximum: 1000 });
    setShareNumber(params, "cost", historical.transactionCostBps, { minimum: 0, maximum: 100000 });
    setShareNumber(params, "annualization", historical.annualizationDays, { minimum: 1, maximum: 366, integer: true });
    if (historical.mar === "" || historical.mar === null || historical.mar === undefined) params.set("mar", "");
    else setShareNumber(params, "mar", historical.mar, { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 });
    if (REBALANCE_FREQUENCIES.has(historical.rebalance)) params.set("rebalance", historical.rebalance);
  } else if (mode === "direct") {
    const direct = configuration.direct ?? {};
    setShareNumber(params, "excess", direct.expectedExcess, { minimum: -1000, maximum: 1000 });
    setShareNumber(params, "vol", direct.volatility, { minimum: 0, maximum: 1000 });
    setShareNumber(params, "rf", direct.riskFreeRate, { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 });
    setShareNumber(params, "spread", direct.borrowingSpread, { minimum: 0, maximum: 1000 });
  } else {
    const portfolio = configuration.portfolio ?? {};
    const source = PORTFOLIO_SOURCES.has(portfolio.source) ? portfolio.source : "direct";
    params.set("source", source);
    setShareNumber(params, "rf", portfolio.riskFreeRate, { minimum: MIN_EFFECTIVE_RATE_PERCENT, maximum: 100 });
    setShareNumber(params, "spread", portfolio.borrowingSpread, { minimum: 0, maximum: 1000 });
    setShareNumber(params, "cap", portfolio.cap, { minimum: Number.EPSILON, maximum: MAX_EXPOSURE });
    if (source === "direct") {
      const validationParams = new URLSearchParams();
      validationParams.set("assets", JSON.stringify(portfolio.directAssets ?? []));
      validationParams.set("corr", JSON.stringify(portfolio.correlation ?? []));
      const validated = safeDirectPortfolio(validationParams);
      if (validated.directAssets) {
        params.set("assets", JSON.stringify(validated.directAssets.map(({ name, expectedExcess, volatility }) => ({ name, expectedExcess, volatility }))));
        params.set("corr", JSON.stringify(validated.correlation));
      }
    } else {
      const ids = (portfolio.historicalAssetIds ?? []).map(safeAssetId).filter(Boolean);
      if (ids.length >= PORTFOLIO_MIN_ASSETS && ids.length <= PORTFOLIO_MAX_ASSETS && new Set(ids).size === ids.length) params.set("assets", ids.join(","));
      const start = safeDate(portfolio.start);
      const end = safeDate(portfolio.end);
      if (start && end && start < end) { params.set("start", start); params.set("end", end); }
      if (REBALANCE_FREQUENCIES.has(portfolio.rebalance)) params.set("rebalance", portfolio.rebalance);
      setShareNumber(params, "cost", portfolio.transactionCostBps, { minimum: 0, maximum: 100000 });
    }
  }
  return params.toString();
}

const initialShareState = parseShareState(location.search);

const state = {
  catalog: [],
  catalogMeta: null,
  runtime: { workerBaseUrl: null },
  workerHealth: null,
  workerHealthPromise: null,
  assetEntry: null,
  rawPayload: null,
  series: null,
  period: null,
  currency: initialShareState.historical?.currency ?? "native",
  assetCache: new Map(),
  fxCache: new Map(),
  notices: { historical: null, direct: null, portfolio: null },
  officialResult: null,
  directResult: null,
  explorationFrame: null,
  requestGeneration: { asset: 0, currency: 0, leverage: 0 },
  portfolioSource: "direct",
  portfolioDirectAssets: [
    { key: "direct-1", name: "SPY", expectedExcess: 6, volatility: 18 },
    { key: "direct-2", name: "TLT", expectedExcess: 2, volatility: 14 },
    { key: "direct-3", name: "GLD", expectedExcess: 3, volatility: 16 },
  ],
  portfolioHistoryIds: ["etf-spy", "etf-tlt", "etf-gld"],
  portfolioMatrices: {
    direct: [
      [1, 0.1, 0.15],
      [0.1, 1, 0.05],
      [0.15, 0.05, 1],
    ],
    historical: [
      [1, 0, 0],
      [0, 1, 0],
      [0, 0, 1],
    ],
  },
  portfolioHistoryData: null,
  portfolioHistoryPeriod: { start: null, end: null },
  pendingPortfolioHistoryPeriod: null,
  portfolioMatrixEdited: { direct: false, historical: false },
  portfolioResults: { direct: null, historical: null },
  lastPortfolioResult: null,
};

const reasonLabels = {
  [REASON.INSUFFICIENT_OBSERVATIONS]: "관측치 부족",
  [REASON.INVALID_RANGE]: "유효하지 않은 기간",
  [REASON.NON_FINITE_INPUT]: "입력값 오류",
  [REASON.INVALID_RATE]: "금리·비용 입력 오류",
  [REASON.INVALID_LEVERAGE_CAP]: "노출 상한 오류",
  [REASON.ZERO_VOLATILITY]: "변동성 0",
  [REASON.ZERO_DOWNSIDE_DEVIATION]: "하방편차 0",
  [REASON.ZERO_MAX_DRAWDOWN]: "MDD 0",
  [REASON.SINGULAR_COVARIANCE]: "특이 공분산",
  [REASON.INVALID_CORRELATION]: "상관행렬 오류",
  [REASON.NON_PSD_CORRELATION]: "비양의 준정부호",
  [REASON.NO_COMMON_RETURNS]: "공통 수익률 부족",
  [REASON.FX_GAP_EXCEEDED]: "FX 5일 공백 초과",
  [FX_GAP_REASON]: "FX 5일 공백 초과",
  [REASON.DATA_UNAVAILABLE]: "데이터 없음",
  [REASON.RUIN]: "파산",
};

function numberInput(id, fallback = 0) {
  const value = Number($(id)?.value);
  return Number.isFinite(value) ? value : fallback;
}

function pctInput(id, fallback = 0) {
  return numberInput(id, fallback * 100) / 100;
}

function fmtPercent(value) {
  return Number.isFinite(value) ? percentage.format(value) : "—";
}

function fmtLeverage(value) {
  return Number.isFinite(value) ? `${decimal.format(value)}×` : "—";
}

function fmtNumber(value) {
  return Number.isFinite(value) ? decimal.format(value) : "—";
}

function nextRequestGeneration(type) {
  state.requestGeneration[type] += 1;
  return state.requestGeneration[type];
}

function isCurrentRequest(type, generation) {
  return state.requestGeneration[type] === generation;
}

function isShareableHistoricalAssetId(assetId) {
  return Boolean(assetId) && assetId !== "csv-upload";
}

function fullKellyNote(kelly) {
  const adjusted = fmtLeverage(kelly.optimalWithBorrowing);
  const theory = fmtLeverage(kelly.theoreticalFullKelly);
  const leverage = Math.abs(kelly.optimalWithBorrowing - kelly.theoreticalFullKelly) > 1e-9
    ? `비용 반영 ${adjusted} · 무비용 이론 ${theory}`
    : `원값 ${adjusted}`;
  return `${leverage} · 로그 ${fmtPercent(kelly.maximumLogGrowth)}`;
}

function reasonText(reasonCode) {
  return reasonLabels[reasonCode] || reasonCode || "사용 불가";
}

function activeMode() {
  return $(".mode-tab.is-active")?.dataset.mode ?? "historical";
}

function noticeForMode(notices, mode) {
  return notices?.[mode] ?? null;
}

function renderNotice(mode = activeMode()) {
  const notice = $("#global-notice");
  const record = noticeForMode(state.notices, mode);
  notice.hidden = !record?.message;
  notice.textContent = record?.message || "";
  notice.dataset.tone = record?.tone || "info";
}

function showNotice(message, tone = "info", scope = activeMode()) {
  state.notices[scope] = message ? { message, tone } : null;
  if (scope === activeMode()) renderNotice(scope);
}

function setHistoricalAvailability(available) {
  const empty = $("#historical-empty-state");
  const content = $("#historical-content");
  if (empty) empty.hidden = available;
  if (content) content.hidden = !available;
}

function configureTheme() {
  const stored = localStorage.getItem("kelly-theme");
  const initial = stored || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  document.documentElement.dataset.theme = initial;
  updateThemeLabel();
  $("#theme-toggle").addEventListener("click", () => {
    document.documentElement.dataset.theme = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
    localStorage.setItem("kelly-theme", document.documentElement.dataset.theme);
    updateThemeLabel();
    disposeCharts();
    renderActiveCharts();
  });
}

function configureGlobalShareButton() {
  if ($("#global-share-url")) return;
  const themeButton = $("#theme-toggle");
  if (!themeButton?.parentElement) return;
  const exportButton = document.createElement("button");
  exportButton.id = "global-export-csv";
  exportButton.className = "icon-button header-csv-button";
  exportButton.type = "button";
  exportButton.textContent = "CSV";
  exportButton.title = "현재 모드 결과 CSV 내보내기";
  exportButton.setAttribute("aria-label", "현재 모드 결과 CSV 내보내기");
  exportButton.addEventListener("click", exportActiveModeCsv);
  const button = document.createElement("button");
  button.id = "global-share-url";
  button.className = "icon-button";
  button.type = "button";
  button.textContent = "🔗";
  button.title = "현재 설정 URL 공유";
  button.setAttribute("aria-label", "현재 설정 URL 공유");
  button.addEventListener("click", () => { void shareCurrentUrl(); });
  themeButton.before(exportButton, button);
}

function updateThemeLabel() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#theme-toggle").setAttribute("aria-label", dark ? "라이트 모드로 전환" : "다크 모드로 전환");
}

function configureModes() {
  $$(".mode-tab").forEach((button) => {
    button.addEventListener("click", () => activateMode(button.dataset.mode));
    button.addEventListener("keydown", onModeTabKeydown);
  });
  activateMode(initialShareState.mode, false);
}

function onModeTabKeydown(event) {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const tabs = $$(".mode-tab");
  const current = Math.max(0, tabs.indexOf(event.currentTarget));
  let next = current;
  if (["ArrowLeft", "ArrowUp"].includes(event.key)) next = (current - 1 + tabs.length) % tabs.length;
  if (["ArrowRight", "ArrowDown"].includes(event.key)) next = (current + 1) % tabs.length;
  if (event.key === "Home") next = 0;
  if (event.key === "End") next = tabs.length - 1;
  event.preventDefault();
  activateMode(tabs[next].dataset.mode);
}

function activateMode(mode, focus = true) {
  $$(".mode-tab").forEach((tab) => {
    const active = tab.dataset.mode === mode;
    tab.classList.toggle("is-active", active);
    tab.setAttribute("aria-selected", String(active));
    tab.tabIndex = active ? 0 : -1;
  });
  $$(".mode-view").forEach((view) => {
    const active = view.id === `${mode}-mode`;
    view.classList.toggle("is-active", active);
    view.hidden = !active;
  });
  renderNotice(mode);
  if (focus) $(`.mode-tab[data-mode="${mode}"]`)?.focus();
  requestAnimationFrame(renderActiveCharts);
}

function renderActiveCharts() {
  const mode = $(".mode-tab.is-active")?.dataset.mode;
  if (mode === "historical" && state.series && state.period) {
    if (state.officialResult) {
      const { official, metrics, kelly, rebalance } = state.officialResult;
      renderHistoricalCharts(official, metrics, kelly, rebalance);
      updateExplorationSummary(false);
    } else {
      renderHistorical({ includeComparison: false });
    }
  }
  if (mode === "direct") renderDirect();
  if (mode === "portfolio") renderPortfolioResult(state.lastPortfolioResult);
}

function catalogItems(payload) {
  const values = Array.isArray(payload) ? payload : payload.assets ?? payload.instruments ?? payload.catalog ?? payload.items ?? [];
  return values.map((item) => ({
    ...item,
    id: item.id ?? item.assetId ?? item.ticker ?? item.symbol,
    ticker: item.ticker ?? item.symbol ?? item.id,
    name: item.name ?? item.displayName ?? item.ticker ?? item.symbol ?? item.id,
    type: item.type ?? item.assetType ?? "asset",
    currency: item.currency ?? "USD",
    returnBasis: item.returnBasis ?? item.return_basis ?? "unspecified",
    status: item.status ?? STATUS.UNAVAILABLE,
  }));
}

function normalizeWorkerBaseUrl(value) {
  if (typeof value !== "string" || !value.trim()) return null;
  try {
    const parsed = new URL(value.trim());
    if (parsed.protocol !== "https:" || parsed.username || parsed.password || parsed.search || parsed.hash) return null;
    return parsed.href.replace(/\/$/, "");
  } catch {
    return null;
  }
}

function fiveYearRange(reference = new Date()) {
  const end = new Date(reference);
  if (Number.isNaN(end.valueOf())) throw new Error(REASON.INVALID_RANGE);
  const start = new Date(end);
  start.setUTCFullYear(start.getUTCFullYear() - 5);
  return { start: start.toISOString().slice(0, 10), end: end.toISOString().slice(0, 10) };
}

function payloadState(payload) {
  return payload?.state ?? payload?.status ?? STATUS.UNAVAILABLE;
}

function isReusableStaticPayload(payload) {
  return [STATUS.PUBLISHED, STATUS.STALE, STATUS.DEGRADED].includes(payloadState(payload));
}

function historicalKellyEligibility(payload, observations) {
  const configuredMinimum = Number(payload?.quality?.minimumKellyObservations);
  const minimumObservations = Number.isInteger(configuredMinimum) && configuredMinimum >= 2
    ? configuredMinimum
    : MIN_KELLY_OBSERVATIONS;
  const observed = Number.isInteger(observations) && observations >= 0 ? observations : 0;
  const sourceEligible = payload?.quality?.eligibleForKelly;
  const eligible = sourceEligible !== false && observed >= minimumObservations;
  return {
    eligible,
    observations: observed,
    minimumObservations,
    reasonCode: eligible ? null : REASON.INSUFFICIENT_OBSERVATIONS,
  };
}

function kellyEligibilityNote(eligibility) {
  if (eligibility?.reasonCode === REASON.INSUFFICIENT_OBSERVATIONS) {
    return `일간수익률 ${eligibility.observations.toLocaleString("ko-KR")}개 · 최소 ${eligibility.minimumObservations.toLocaleString("ko-KR")}개 필요`;
  }
  return reasonText(eligibility?.reasonCode);
}

function unavailableKellyResult(reasonCode) {
  return { status: STATUS.UNAVAILABLE, reasonCode, presets: [] };
}

function flattenWorkerPayload(payload, requestedSymbol = "") {
  if (payload?.contract !== "kelly-price-series" || !Array.isArray(payload.symbols) || !Array.isArray(payload.metadata)) {
    throw new Error(REASON.DATA_UNAVAILABLE);
  }
  const requested = String(requestedSymbol || "").toUpperCase();
  const index = requested ? payload.symbols.findIndex((symbol) => String(symbol).toUpperCase() === requested) : 0;
  if (index < 0 || !Array.isArray(payload.prices?.[index]) || !Array.isArray(payload.returns?.[index])) {
    throw new Error(REASON.DATA_UNAVAILABLE);
  }
  const dates = Array.isArray(payload.dates) ? payload.dates : [];
  const prices = payload.prices[index];
  const returns = payload.returns[index];
  const metadata = payload.metadata[index];
  if (!metadata || dates.length !== prices.length || dates.length !== returns.length) {
    throw new Error(REASON.DATA_UNAVAILABLE);
  }
  return {
    schemaVersion: payload.schemaVersion,
    contract: "kelly-asset-history",
    state: payload.state,
    assetId: metadata.id,
    generatedAt: payload.generatedAt,
    dataAsOf: payload.dataAsOf,
    metadata: {
      symbol: metadata.symbol,
      name: metadata.name,
      assetType: metadata.assetType,
      exchange: metadata.exchange,
      timezone: metadata.timezone,
      returnBasis: metadata.returnBasis,
      baseCurrency: metadata.currency,
      quoteCurrency: metadata.quoteCurrency,
    },
    dates,
    prices,
    returns,
    source: payload.source,
    limitations: payload.limitations ?? [],
  };
}

function fxQuoteCurrency(payload) {
  const metadata = payload?.metadata ?? {};
  const assetType = String(payload?.assetType ?? metadata.assetType ?? "").toLowerCase();
  const returnBasis = String(payload?.returnBasis ?? metadata.returnBasis ?? "").toLowerCase();
  if (assetType !== "fx" && returnBasis !== "fx_rate") return null;
  const symbol = String(payload?.symbol ?? metadata.symbol ?? "");
  const inferredQuote = symbol.includes("/") ? symbol.split("/").at(-1) : null;
  const quote = String(payload?.quoteCurrency ?? metadata.quoteCurrency ?? inferredQuote ?? "").toUpperCase();
  return /^[A-Z]{3}$/.test(quote) ? quote : null;
}

function nativeSeriesFromPayload(payload) {
  const quoteCurrency = fxQuoteCurrency(payload);
  return normalizeAssetPayload(quoteCurrency ? { ...payload, currency: quoteCurrency } : payload);
}

async function loadRuntime() {
  try {
    const response = await fetch("./data/runtime.json", { cache: "no-cache" });
    if (!response.ok) return;
    const payload = await response.json();
    state.runtime = { workerBaseUrl: normalizeWorkerBaseUrl(payload?.workerBaseUrl) };
  } catch {
    state.runtime = { workerBaseUrl: null };
  }
}

function workerEndpoint(pathname, parameters = {}) {
  const base = state.runtime.workerBaseUrl;
  if (!base) return null;
  const url = new URL(`${base}${pathname}`);
  for (const [key, value] of Object.entries(parameters)) url.searchParams.set(key, String(value));
  return url;
}

async function workerReady() {
  if (!state.runtime.workerBaseUrl) return false;
  if (state.workerHealth !== null) return state.workerHealth;
  if (!state.workerHealthPromise) {
    state.workerHealthPromise = (async () => {
      try {
        const response = await fetch(workerEndpoint("/v1/health"), { cache: "no-store" });
        if (!response.ok) return false;
        const payload = await response.json();
        return payload?.state === STATUS.LIVE_API && payload?.rightsApproved === true;
      } catch {
        return false;
      }
    })();
  }
  state.workerHealth = await state.workerHealthPromise;
  return state.workerHealth;
}

async function fetchWorkerDocument(pathname, parameters) {
  if (!(await workerReady())) return null;
  try {
    const response = await fetch(workerEndpoint(pathname, parameters), { cache: "no-store" });
    if (!response.ok) return null;
    const payload = await response.json();
    return payload?.state === STATUS.LIVE_API ? payload : null;
  } catch {
    return null;
  }
}

function applyCatalogShareInputs() {
  const portfolio = initialShareState.portfolio;
  if (portfolio?.source !== "historical" || !portfolio.historicalAssetIds) return;
  const eligibleIds = new Set(eligiblePortfolioCatalog().map((asset) => asset.id));
  if (!portfolio.historicalAssetIds.every((id) => eligibleIds.has(id))) return;
  state.portfolioHistoryIds = [...portfolio.historicalAssetIds];
  state.portfolioMatrices.historical = identityMatrix(state.portfolioHistoryIds.length);
  state.portfolioMatrixEdited.historical = false;
  if (portfolio.start && portfolio.end) {
    state.pendingPortfolioHistoryPeriod = { start: portfolio.start, end: portfolio.end };
  }
}

async function loadCatalog() {
  try {
    const response = await fetch("./data/catalog.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.catalogMeta = payload;
    state.catalog = catalogItems(payload);
    if (!state.catalog.length) throw new Error("empty catalog");
    populateAssetSelect();
    applyCatalogShareInputs();
    renderPortfolioRows();
    const desired = initialShareState.historical?.asset;
    const initial = state.catalog.find((asset) => asset.id === desired || asset.ticker === desired)
      ?? state.catalog.find((asset) => asset.ticker === "SPY")
      ?? state.catalog.find((asset) => [STATUS.PUBLISHED, STATUS.LIVE_API].includes(asset.status))
      ?? state.catalog[0];
    $("#asset-select").value = initial.id;
    await loadSelectedAsset();
    if (desired === "csv-upload") {
      showNotice("업로드 CSV는 URL에 포함되지 않습니다. 공유 링크에서 복원할 수 없어 기본 공개 자산으로 열었습니다.", "error", "historical");
    }
  } catch (error) {
    $("#asset-select").innerHTML = '<option value="">정적 데이터 이용 불가</option>';
    showNotice(`과거 데이터 계약을 불러오지 못했습니다. (${error.message})`, "error", "historical");
    setHistoricalAvailability(false);
    renderUnavailableHistorical(REASON.DATA_UNAVAILABLE);
  }
}

function populateAssetSelect() {
  const grouped = Map.groupBy ? Map.groupBy(state.catalog, (asset) => asset.type) : state.catalog.reduce((map, asset) => {
    const key = asset.type;
    if (!map.has(key)) map.set(key, []);
    map.get(key).push(asset);
    return map;
  }, new Map());
  const labels = { stock: "주식", equity: "주식", index: "지수", etf: "ETF", leveraged_etf: "레버리지 ETF", fx: "환율", currency: "환율" };
  const select = $("#asset-select");
  select.replaceChildren();
  for (const [type, assets] of grouped) {
    const group = document.createElement("optgroup");
    group.label = labels[type] ?? type;
    for (const asset of assets) {
      const option = document.createElement("option");
      option.value = asset.id;
      option.textContent = `${asset.ticker} · ${asset.name}`;
      if (![STATUS.PUBLISHED, STATUS.LIVE_API].includes(asset.status)) option.textContent += ` (${asset.status})`;
      group.append(option);
    }
    select.append(group);
  }
}

function assetPath(entry) {
  const explicit = entry.dataPath ?? entry.data_path ?? entry.assetFile ?? entry.asset_file;
  if (explicit) {
    if (explicit.startsWith(".") || explicit.startsWith("/")) return explicit;
    if (explicit.startsWith("assets/")) return `./data/${explicit}`;
    return `./${explicit}`;
  }
  return `./data/assets/${encodeURIComponent(entry.id)}.json`;
}

async function fetchStaticAssetPayload(entry) {
  const response = await fetch(assetPath(entry), { cache: "no-cache" });
  if (!response.ok) throw new Error(`HTTP ${response.status}`);
  return response.json();
}

async function fetchWorkerAssetPayload(entry) {
  const range = fiveYearRange();
  const payload = await fetchWorkerDocument("/v1/history", {
    symbols: entry.ticker ?? entry.symbol ?? entry.id,
    start: range.start,
    end: range.end,
  });
  if (!payload) return null;
  try {
    return flattenWorkerPayload(payload, entry.ticker ?? entry.symbol);
  } catch {
    return null;
  }
}

async function fetchAssetPayload(entry) {
  if (!entry) throw new Error("asset not found");
  if (state.assetCache.has(entry.id)) return state.assetCache.get(entry.id);
  const promise = (async () => {
    let staticPayload = null;
    let staticError = null;
    try {
      staticPayload = await fetchStaticAssetPayload(entry);
    } catch (error) {
      staticError = error;
    }
    if (isReusableStaticPayload(staticPayload)) return staticPayload;
    const workerPayload = await fetchWorkerAssetPayload(entry);
    if (workerPayload) return workerPayload;
    if (staticPayload) return staticPayload;
    throw staticError ?? new Error(REASON.DATA_UNAVAILABLE);
  })();
  state.assetCache.set(entry.id, promise);
  try { return await promise; } catch (error) { state.assetCache.delete(entry.id); throw error; }
}

function dateNumber(value) {
  const timestamp = Date.parse(`${value}T00:00:00Z`);
  return Number.isFinite(timestamp) ? timestamp / 86_400_000 : null;
}

function alignPreviousFx(assetDates, fxDates, fxRates, maxGapDays = 5) {
  if (!Array.isArray(assetDates) || !Array.isArray(fxDates) || !Array.isArray(fxRates) || fxDates.length !== fxRates.length) {
    throw new Error(FX_GAP_REASON);
  }
  let pointer = -1;
  let previousFxDay = -Infinity;
  const aligned = [];
  for (const assetDate of assetDates) {
    const assetDay = dateNumber(assetDate);
    if (assetDay === null) throw new Error(FX_GAP_REASON);
    while (pointer + 1 < fxDates.length) {
      const candidateDay = dateNumber(fxDates[pointer + 1]);
      if (candidateDay === null || candidateDay < previousFxDay) throw new Error(FX_GAP_REASON);
      if (candidateDay > assetDay) break;
      pointer += 1;
      previousFxDay = candidateDay;
    }
    const rate = Number(fxRates[pointer]);
    if (pointer < 0 || !Number.isFinite(rate) || rate <= 0 || assetDay - previousFxDay > maxGapDays) {
      throw new Error(FX_GAP_REASON);
    }
    aligned.push(rate);
  }
  return aligned;
}

async function fetchFxPayload(assetDates) {
  const start = assetDates?.[0];
  const end = assetDates?.at(-1);
  if (!start || !end) throw new Error(FX_GAP_REASON);
  const cacheKey = `${start}:${end}`;
  if (state.fxCache.has(cacheKey)) return state.fxCache.get(cacheKey);
  const promise = (async () => {
    const entry = state.catalog.find((asset) => asset.ticker === "USD/KRW" || asset.symbol === "USD/KRW");
    let staticPayload = null;
    if (entry) {
      try { staticPayload = await fetchStaticAssetPayload(entry); } catch { staticPayload = null; }
      if (isReusableStaticPayload(staticPayload)) return staticPayload;
    }
    const workerPayload = await fetchWorkerDocument("/v1/fx", { base: "USD", quote: "KRW", start, end });
    if (workerPayload) return flattenWorkerPayload(workerPayload, "USD/KRW");
    if (staticPayload) return staticPayload;
    throw new Error(FX_GAP_REASON);
  })();
  state.fxCache.set(cacheKey, promise);
  try { return await promise; } catch (error) { state.fxCache.delete(cacheKey); throw error; }
}

function krwSeries(native, payload, fxPayload) {
  if (native.currency !== "USD" || native.prices.length !== native.dates.length) {
    throw new Error(FX_GAP_REASON);
  }
  const fxSeries = normalizeAssetPayload(fxPayload);
  const rates = alignPreviousFx(native.dates, fxSeries.dates, fxSeries.prices, 5);
  const prices = native.prices.map((price, index) => Number(price) * rates[index]);
  return normalizeAssetPayload({
    id: native.id,
    symbol: native.symbol,
    name: native.name,
    currency: "KRW",
    returnBasis: native.returnBasis,
    state: native.status,
    dates: native.dates,
    prices,
    returns: [],
    source: { asset: payload.source, fx: fxPayload.source },
  });
}

function seriesFromPayload(payload, requestedCurrency, fxPayload = null) {
  const native = nativeSeriesFromPayload(payload);
  if (requestedCurrency === "native" || native.currency === "KRW") return native;
  const krwBlock = payload.series?.krw ?? payload.krw;
  if (krwBlock) return normalizeAssetPayload({ ...payload, ...krwBlock, columns: krwBlock.columns ?? krwBlock, currency: "KRW" });
  const columns = payload.columns ?? payload.data ?? {};
  const krwReturns = columns.returnKrw ?? columns.return_krw ?? columns.krwReturn ?? columns.krw_returns;
  const krwPrices = columns.priceKrw ?? columns.price_krw ?? columns.krwPrice;
  if (krwReturns) {
    return normalizeAssetPayload({ ...payload, currency: "KRW", columns: { date: columns.date ?? columns.dates, return: krwReturns, price: krwPrices ?? [] } });
  }
  const embeddedRates = columns.fx ?? columns.usdKrw ?? columns.usd_krw
    ?? (Array.isArray(payload.fx) ? payload.fx : payload.fx?.rates);
  const embeddedFxDates = payload.fx?.dates;
  if (Array.isArray(embeddedFxDates) && Array.isArray(payload.fx?.rates) && native.prices.length === native.dates.length) {
    const rates = alignPreviousFx(native.dates, embeddedFxDates, payload.fx.rates, 5);
    const prices = native.prices.map((price, index) => Number(price) * rates[index]);
    return normalizeAssetPayload({
      id: native.id,
      symbol: native.symbol,
      name: native.name,
      currency: "KRW",
      returnBasis: native.returnBasis,
      state: native.status,
      dates: native.dates,
      prices,
      returns: [],
      source: payload.source,
    });
  }
  if (embeddedRates?.length === native.dates.length && native.prices.length === native.dates.length) {
    const prices = native.prices.map((price, index) => Number(price) * Number(embeddedRates[index]));
    return normalizeAssetPayload({
      id: native.id,
      symbol: native.symbol,
      name: native.name,
      currency: "KRW",
      returnBasis: native.returnBasis,
      state: native.status,
      dates: native.dates,
      prices,
      returns: [],
      source: payload.source,
    });
  }
  if (fxPayload) return krwSeries(native, payload, fxPayload);
  throw new Error(FX_GAP_REASON);
}

async function seriesForCurrency(payload, requestedCurrency) {
  const native = nativeSeriesFromPayload(payload);
  if (requestedCurrency === "native" || native.currency === "KRW") return native;
  try {
    return seriesFromPayload(payload, requestedCurrency);
  } catch (error) {
    if (![FX_GAP_REASON, REASON.FX_GAP_EXCEEDED].includes(error.message)) throw error;
  }
  const fxPayload = await fetchFxPayload(native.dates);
  return seriesFromPayload(payload, requestedCurrency, fxPayload);
}

async function loadSelectedAsset() {
  const id = $("#asset-select").value;
  const entry = state.catalog.find((asset) => asset.id === id);
  const generation = nextRequestGeneration("asset");
  nextRequestGeneration("currency");
  nextRequestGeneration("leverage");
  const previousEntry = state.assetEntry;
  const requestedCurrency = state.currency;
  setCurrencyControlsDisabled(true);
  showNotice(`${entry?.ticker ?? "선택 자산"} 이력을 불러오는 중입니다.`, "info", "historical");
  try {
    const payload = await fetchAssetPayload(entry);
    if (!isCurrentRequest("asset", generation) || $("#asset-select").value !== id) return;
    const series = await seriesForCurrency(payload, requestedCurrency);
    if (!isCurrentRequest("asset", generation) || $("#asset-select").value !== id || state.currency !== requestedCurrency) return;
    if (series.status && ![STATUS.PUBLISHED, STATUS.LIVE_API, STATUS.STALE, STATUS.DEGRADED].includes(series.status)) throw new Error(series.status);
    if (series.returns.length < 2 || series.dates.length < 3) throw new Error(REASON.INSUFFICIENT_OBSERVATIONS);
    state.assetEntry = entry;
    state.rawPayload = payload;
    state.series = series;
    setHistoricalAvailability(true);
    renderAssetMeta(
      { ...entry, returnBasis: series.returnBasis ?? entry.returnBasis, currency: series.currency ?? entry.currency },
      series.source ?? payload.source,
      payload.quality,
      series.returns.length,
    );
    initializePeriod(series);
    const resolvedState = payloadState(payload);
    showNotice(statusMessage(entry, payload), [STATUS.STALE, STATUS.DEGRADED].includes(resolvedState) ? "error" : "success", "historical");
    renderHistorical();
  } catch (error) {
    if (!isCurrentRequest("asset", generation)) return;
    if (previousEntry && state.series && state.period) {
      $("#asset-select").value = previousEntry.id;
      renderAssetMeta(previousEntry, state.series.source, state.rawPayload?.quality, state.series.returns.length);
      setHistoricalAvailability(true);
      showNotice(`${entry?.ticker ?? "선택 자산"} 이력을 적용하지 않고 기존 공식 결과를 보존했습니다. (${reasonText(error.message)})`, "error", "historical");
    } else {
      state.assetEntry = null;
      state.rawPayload = null;
      state.series = null;
      state.period = null;
      state.officialResult = null;
      setHistoricalAvailability(false);
      showNotice(`${entry?.ticker ?? "선택 자산"}의 검증된 공개 이력이 없습니다. (${reasonText(error.message)})`, "error", "historical");
      renderUnavailableHistorical(error.message || REASON.DATA_UNAVAILABLE);
    }
  } finally {
    if (isCurrentRequest("asset", generation)) setCurrencyControlsDisabled(false);
  }
}

function statusMessage(entry, payload) {
  const asOf = payload.asOf ?? payload.dataAsOf ?? entry.asOf ?? entry.dataAsOf;
  const basis = returnBasisLabel(payload.metadata?.returnBasis ?? entry.returnBasis ?? payload.returnBasis);
  return `${entry.ticker} · ${basis}${asOf ? ` · 기준일 ${asOf}` : ""} · 상태 ${payloadState(payload)}`;
}

function sourceProviders(source, providers = new Set(), seen = new WeakSet()) {
  if (!source || typeof source !== "object") return providers;
  if (seen.has(source)) return providers;
  seen.add(source);
  if (Array.isArray(source)) {
    source.forEach((item) => sourceProviders(item, providers, seen));
    return providers;
  }
  const provider = String(source.provider ?? "").toLowerCase().replaceAll(" ", "_");
  if (["twelve_data", "twelvedata"].includes(provider)) providers.add("twelve_data");
  if (provider === "krx") providers.add("krx");
  if (["yahoo", "yahoo_finance"].includes(provider)) providers.add("yahoo_finance");
  if (provider === "stooq") providers.add("stooq");
  if (provider === "fred") providers.add("fred");
  if (String(source.adapter ?? "").toLowerCase() === "finance_data_reader") providers.add("finance_data_reader");
  for (const nested of [source.asset, source.fx]) sourceProviders(nested, providers, seen);
  return providers;
}

function sourceAttributionHtml(source) {
  const providers = sourceProviders(source);
  return [
    providers.has("twelve_data") ? '<a class="badge source-attribution" href="https://twelvedata.com" target="_blank" rel="noopener">Data provided by Twelve Data</a>' : "",
    providers.has("krx") ? '<a class="badge source-attribution" href="https://openapi.krx.co.kr/" target="_blank" rel="noopener">한국거래소 통계정보</a>' : "",
    providers.has("yahoo_finance") ? '<a class="badge source-attribution" href="https://finance.yahoo.com/" target="_blank" rel="noopener">Yahoo Finance 시세</a>' : "",
    providers.has("finance_data_reader") ? '<a class="badge source-attribution" href="https://github.com/FinanceData/FinanceDataReader" target="_blank" rel="noopener">FinanceDataReader 어댑터</a>' : "",
    providers.has("stooq") ? '<a class="badge source-attribution" href="https://stooq.com/" target="_blank" rel="noopener">Stooq 가격 시세</a>' : "",
    providers.has("fred") ? '<a class="badge source-attribution" href="https://fred.stlouisfed.org/series/DEXKOUS" target="_blank" rel="noopener">FRED DEXKOUS</a>' : "",
  ].filter(Boolean).join("");
}

function qualityMetaHtml(quality, returnObservations = 0) {
  if (!quality || typeof quality !== "object") return "";
  const eligibility = historicalKellyEligibility({ quality }, returnObservations);
  const crossCheck = quality.crossCheck;
  const providerLabels = {
    finviz: "Finviz",
    fred: "FRED",
    stooq: "Stooq",
    yahoo_finance: "Yahoo Finance",
  };
  const provider = providerLabels[crossCheck?.provider] ?? crossCheck?.provider;
  const badges = [];
  if (crossCheck?.state === "passed") {
    badges.push(`<span class="badge" title="${escapeHtml(`${crossCheck.commonObservations ?? 0}개 공통 수익률 비교`)}">${escapeHtml(provider || "보조 소스")} 교차검증 통과</span>`);
  } else if (crossCheck && !["not_applicable", "none"].includes(crossCheck.state)) {
    const detail = crossCheck.state === "insufficient"
      ? `${provider || "보조 소스"} 공통 관측 부족`
      : crossCheck.state === "mismatch"
        ? `${provider || "보조 소스"} 수익률 차이 기준 초과`
        : `${provider || "보조 소스"} 응답 확인 불가`;
    badges.push(`<span class="badge" title="${escapeHtml(detail)}">교차검증 미확인</span>`);
  }
  if (!eligibility.eligible) {
    badges.push(`<span class="badge" title="Kelly 계산에는 최소 ${eligibility.minimumObservations}개 일간수익률이 필요합니다.">Kelly 관측 부족 ${eligibility.observations}/${eligibility.minimumObservations}</span>`);
  }
  return badges.join("");
}

function renderAssetMeta(entry, source = null, quality = null, returnObservations = 0) {
  if (!entry) return;
  $("#asset-meta").innerHTML = [
    `<span class="badge">${escapeHtml(entry.type)}</span>`,
    `<span class="badge">${escapeHtml(entry.currency)}</span>`,
    `<span class="badge">${escapeHtml(returnBasisLabel(entry.returnBasis))}</span>`,
    sourceAttributionHtml(source),
    qualityMetaHtml(quality, returnObservations),
  ].join("");
}

function returnBasisLabel(value) {
  const basis = String(value || "").toLowerCase();
  if (basis.includes("total") || basis.includes("adjust")) return "조정 총수익 근사";
  if (basis.includes("price")) return "가격수익률";
  if (basis.includes("fx")) return "환율 변동률";
  return value || "수익률 기준 미확인";
}

function initializePeriod(series, preferredPeriod = null) {
  const end = series.dates.at(-1);
  const endDate = new Date(`${end}T00:00:00Z`);
  const target = new Date(endDate);
  target.setUTCFullYear(target.getUTCFullYear() - 5);
  const targetIso = target.toISOString().slice(0, 10);
  const defaultStart = series.dates.find((date) => date >= targetIso) ?? series.dates[0];
  const preferredOfficial = preferredPeriod?.official;
  const preferredExploration = preferredPeriod?.exploration;
  if (isValidDateRange(preferredOfficial?.start, preferredOfficial?.end, series.dates[0], end)) {
    const exploration = isValidDateRange(preferredExploration?.start, preferredExploration?.end, series.dates[0], end)
      ? { ...preferredExploration }
      : { ...preferredOfficial };
    state.period = { official: { ...preferredOfficial }, exploration, error: null };
  } else {
    const queryStart = initialShareState.historical?.start;
    const queryEnd = initialShareState.historical?.end;
    const start = isValidDateRange(queryStart, queryEnd, series.dates[0], end) ? queryStart : defaultStart;
    const finish = isValidDateRange(queryStart, queryEnd, series.dates[0], end) ? queryEnd : end;
    state.period = createPeriodState(start, finish);
  }
  for (const input of [$("#official-start"), $("#official-end")]) {
    input.min = series.dates[0];
    input.max = end;
  }
  syncPeriodControls();
}

function syncPeriodControls() {
  if (!state.period || !state.series) return;
  $("#official-start").value = state.period.official.start;
  $("#official-end").value = state.period.official.end;
  const max = state.series.dates.length - 1;
  const startIndex = Math.max(0, state.series.dates.indexOf(state.period.exploration.start));
  const endIndex = Math.max(startIndex + 1, state.series.dates.indexOf(state.period.exploration.end));
  for (const slider of [$("#explore-start-slider"), $("#explore-end-slider")]) slider.max = String(max);
  const startSlider = $("#explore-start-slider");
  const endSlider = $("#explore-end-slider");
  startSlider.value = String(startIndex);
  endSlider.value = String(endIndex);
  startSlider.setAttribute("aria-valuetext", state.series.dates[startIndex] ?? state.period.exploration.start);
  endSlider.setAttribute("aria-valuetext", state.series.dates[endIndex] ?? state.period.exploration.end);
}

function historicalInputs() {
  const riskFreeRate = pctInput("#risk-free");
  const marValue = $("#sortino-mar").value.trim();
  return {
    riskFreeRate,
    borrowingSpread: pctInput("#borrow-spread"),
    transactionCostBps: numberInput("#transaction-cost", 10),
    annualizationDays: numberInput("#annualization-days", ANNUALIZATION_DAYS),
    mar: marValue === "" ? riskFreeRate : Number(marValue) / 100,
    frequency: $("#rebalance-frequency").value,
  };
}

function computeHistoricalAnalysis(official, inputs, payload = null) {
  const metrics = performanceMetrics(official.returns, official.dates, {
    annualizationDays: inputs.annualizationDays,
    riskFreeRate: inputs.riskFreeRate,
    mar: inputs.mar,
    minObservations: 2,
  });
  const kellyEligibility = historicalKellyEligibility(payload, official.returns.length);
  if (metrics.status !== STATUS.PUBLISHED) {
    return {
      metrics,
      kellyEligibility,
      kelly: unavailableKellyResult(metrics.reasonCode),
      exact: unavailableKellyResult(metrics.reasonCode),
      rebalance: null,
    };
  }
  if (!kellyEligibility.eligible) {
    return {
      metrics,
      kellyEligibility,
      kelly: unavailableKellyResult(kellyEligibility.reasonCode),
      exact: unavailableKellyResult(kellyEligibility.reasonCode),
      rebalance: null,
    };
  }
  const kelly = singleAssetKelly({
    expectedExcessReturn: metrics.annualArithmeticReturn.value - inputs.riskFreeRate,
    volatility: metrics.annualVolatility.value,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
  });
  const exact = exactHistoricalKelly(official.returns, {
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
    annualizationDays: inputs.annualizationDays,
    minObservations: kellyEligibility.minimumObservations,
  });
  const rebalance = kelly.status === STATUS.PUBLISHED ? rebalanceComparison({
    returnsByAsset: [official.returns],
    dates: official.returnDates ?? official.dates.slice(1),
    targetWeights: [kelly.appliedFullKelly],
    frequency: inputs.frequency,
    transactionCostBps: inputs.transactionCostBps,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
  }) : null;
  return { metrics, kellyEligibility, kelly, exact, rebalance };
}

function renderHistorical({ includeComparison = true } = {}) {
  if (!state.series || !state.period) return;
  const previousComparison = includeComparison ? [] : (state.officialResult?.leverageComparison ?? []);
  const official = sliceSeries(state.series, state.period.official.start, state.period.official.end);
  const inputs = historicalInputs();
  const { metrics, kellyEligibility, kelly, exact, rebalance } = computeHistoricalAnalysis(official, inputs, state.rawPayload);
  if (metrics.status !== STATUS.PUBLISHED) {
    nextRequestGeneration("leverage");
    state.officialResult = null;
    renderUnavailableHistorical(metrics.reasonCode);
    clearHistoricalVisuals(reasonText(metrics.reasonCode));
    return;
  }
  const result = {
    official,
    inputs,
    metrics,
    kelly,
    exact,
    rebalance,
    kellyEligibility,
    assetEntry: state.assetEntry,
    currency: state.currency,
    period: { official: { ...state.period.official } },
    leverageComparison: previousComparison,
  };
  state.officialResult = result;

  $("#result-title").textContent = `${state.assetEntry?.ticker ?? state.series.symbol ?? "자산"} 분석 결과`;
  $("#official-period-label").textContent = `${state.period.official.start} – ${state.period.official.end} · ${metrics.observations.toLocaleString("ko-KR")}개 일간수익률`;
  renderHistoricalKellyCards(kelly, exact, kellyEligibility);
  renderMetricCards(metrics, inputs.annualizationDays);
  renderPresets($("#historical-presets"), kelly);
  renderHistoricalCharts(official, metrics, kelly, rebalance);
  updateExplorationSummary(false);
  if (includeComparison) {
    const generation = nextRequestGeneration("leverage");
    result.leverageComparisonPromise = renderLeverageComparison(result, generation);
  }
}

function clearHistoricalVisuals(reason) {
  const message = reason || "현재 입력으로 계산할 수 없습니다.";
  clearChart($("#wealth-chart"), "기준가 100 누적자산", message);
  clearChart($("#drawdown-chart"), "낙폭", message);
  clearChart($("#growth-chart"), "성장률–레버리지 곡선", message);
  clearChart($("#rebalance-chart"), "재조정 효과 비교", message);
  renderWealthDataTable([], [], []);
  renderDrawdownDataTable([], []);
  renderGrowthDataTable("#growth-data-table", [], []);
  renderRebalanceDataTable("#rebalance-data-table", [], null);
  $("#rebalance-summary").innerHTML = `<div class="summary-cell"><span>재조정 계산</span><strong>${escapeHtml(message)}</strong></div>`;
}

function renderHistoricalKellyCards(kelly, exact, eligibility = null) {
  const cards = $("#historical-kelly-cards");
  if (kelly.status !== STATUS.PUBLISHED) {
    const note = eligibility?.reasonCode === REASON.INSUFFICIENT_OBSERVATIONS
      ? kellyEligibilityNote(eligibility)
      : reasonText(kelly.reasonCode);
    cards.innerHTML = eligibility?.reasonCode === REASON.INSUFFICIENT_OBSERVATIONS
      ? [
        metricCard("Kelly 계산 불가", "—", note, true),
        metricCard("Full Kelly 성장률", "—", "관측치 충족 후 계산"),
        metricCard("절대 2배 기대성장률", "—", "동일 가정 기준 계산 보류"),
        metricCard("Exact Kelly", "—", "in-sample 계산 보류"),
      ].join("")
      : metricCard("Kelly 계산 불가", "—", note, true);
    return;
  }
  cards.innerHTML = [
    metricCard("Full Kelly 최대 기하성장률", fmtPercent(kelly.maximumAnnualGrowth), fullKellyNote(kelly), true),
    metricCard("상한 적용 경로", fmtLeverage(kelly.appliedFullKelly), `기하성장률 ${fmtPercent(kelly.appliedAnnualGrowth)}${kelly.capApplied ? " · 3× 상한" : ""}`),
    metricCard("절대 2배 장기 기하성장률", fmtPercent(kelly.twiceAnnualGrowth), `기대 산술 자산수익률 ${fmtPercent(kelly.twiceArithmeticWealthReturn)}`),
    metricCard(
      "Exact Kelly",
      [STATUS.PUBLISHED, STATUS.DEGRADED].includes(exact.status) ? fmtLeverage(exact.appliedLeverage) : "—",
      exact.status === STATUS.DEGRADED
        ? `탐색상한 도달 · 원값 ${fmtLeverage(exact.theoreticalLeverage)}`
        : exact.status === STATUS.PUBLISHED ? "in-sample · 일간 재조정" : reasonText(exact.reasonCode),
    ),
  ].join("");
}

function metricCard(label, value, note, primary = false) {
  return `<article class="metric-card${primary ? " primary" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></article>`;
}

function renderMetricCards(metrics, annualizationDays = ANNUALIZATION_DAYS) {
  const definitions = [
    ["누적수익률", metrics.cumulativeReturn, "선택기간"],
    ["연환산 산술평균", metrics.annualArithmeticReturn, `일간 평균 × ${annualizationDays}`],
    ["CAGR", metrics.cagr, "실제 경과일 복리"],
    ["연환산 변동성", metrics.annualVolatility, "표본 표준편차"],
    ["MDD", metrics.maxDrawdown, "고점 대비 최대 하락"],
    ["Sharpe", metrics.sharpe, "무위험률 초과"],
    ["Sortino", metrics.sortino, "MAR 하방편차"],
    ["Calmar-style", metrics.calmar, "선택기간 CAGR / MDD"],
  ];
  $("#performance-cards").innerHTML = definitions.map(([label, result, note]) => {
    const value = result?.value;
    const display = ["Sharpe", "Sortino", "Calmar-style"].includes(label) ? fmtNumber(value) : fmtPercent(value);
    return `<article class="mini-metric"><span>${label}</span><strong>${display}</strong><small title="${escapeHtml(reasonText(result?.reasonCode))}">${result?.reasonCode ? escapeHtml(reasonText(result.reasonCode)) : note}</small></article>`;
  }).join("");
}

function renderPresets(container, kelly) {
  if (kelly.status !== STATUS.PUBLISHED) { container.innerHTML = ""; return; }
  const names = { 0.25: "Quarter Kelly", 0.5: "Half Kelly", 1: "Full Kelly" };
  container.innerHTML = kelly.presets.map((preset) => `<div class="preset-chip${preset.fraction === 1 ? " is-full" : ""}"><span>${names[preset.fraction]}</span><strong>${fmtLeverage(preset.leverage)} · ${fmtPercent(preset.annualGrowth)}</strong></div>`).join("");
}

function rangeWealth(series, start, end) {
  const selected = sliceSeries(series, start, end);
  const path = wealthPath(selected.returns);
  const values = Array(series.dates.length).fill(null);
  if (path.status !== STATUS.PUBLISHED) return values;
  selected.dates.forEach((date, index) => {
    const position = series.dates.indexOf(date);
    if (position >= 0) values[position] = path.wealth[index] ?? null;
  });
  return values;
}

function growthCurve(kelly, inputs) {
  if (kelly.status !== STATUS.PUBLISHED) return { points: [], markers: [] };
  const expectedExcessReturn = state.officialResult?.metrics.annualArithmeticReturn.value - inputs.riskFreeRate
    ?? pctInput("#direct-excess");
  const volatility = state.officialResult?.metrics.annualVolatility.value ?? pctInput("#direct-vol");
  const points = Array.from({ length: 121 }, (_, index) => {
    const leverage = index / 40;
    return [leverage, continuousGrowthRate({ leverage, expectedExcessReturn, volatility, riskFreeRate: inputs.riskFreeRate, borrowingSpread: inputs.borrowingSpread })];
  });
  return {
    points,
    markers: [
      { name: "Full", x: kelly.appliedFullKelly, y: kelly.appliedLogGrowth },
      { name: "2×", x: 2, y: kelly.twiceLogGrowth, color: "#c58b24" },
    ],
  };
}

function renderHistoricalCharts(official, metrics, kelly, rebalance) {
  const officialWealth = rangeWealth(state.series, state.period.official.start, state.period.official.end);
  const explorationWealth = rangeWealth(state.series, state.period.exploration.start, state.period.exploration.end);
  renderWealthChart($("#wealth-chart"), {
    dates: state.series.dates,
    officialWealth,
    explorationWealth,
    explorationStart: state.period.exploration.start,
    explorationEnd: state.period.exploration.end,
    returnBasis: returnBasisLabel(state.assetEntry?.returnBasis ?? state.series.returnBasis),
  }, onChartExplore);
  renderWealthDataTable(state.series.dates, officialWealth, explorationWealth);
  renderDrawdownChart($("#drawdown-chart"), official.dates, metrics.drawdowns);
  renderDrawdownDataTable(official.dates, metrics.drawdowns);
  const curve = growthCurve(kelly, state.officialResult.inputs);
  const kellyUnavailableReason = state.officialResult.kellyEligibility?.reasonCode === REASON.INSUFFICIENT_OBSERVATIONS
    ? kellyEligibilityNote(state.officialResult.kellyEligibility)
    : reasonText(kelly.reasonCode);
  if (kelly.status === STATUS.PUBLISHED) {
    renderGrowthCurve($("#growth-chart"), curve.points, curve.markers);
    renderGrowthDataTable("#growth-data-table", curve.points, curve.markers);
  } else {
    clearChart($("#growth-chart"), "성장률–레버리지 곡선", kellyUnavailableReason);
    renderGrowthDataTable("#growth-data-table", [], []);
  }
  if (rebalance?.status === STATUS.PUBLISHED) {
    renderRebalanceChart($("#rebalance-chart"), official.dates, rebalance);
    renderRebalanceSummary(rebalance);
    renderRebalanceDataTable("#rebalance-data-table", official.dates, rebalance);
  } else {
    const reason = rebalance?.reasonCode ? reasonText(rebalance.reasonCode) : kellyUnavailableReason;
    clearChart($("#rebalance-chart"), "재조정 효과 비교", reason);
    $("#rebalance-summary").innerHTML = `<div class="summary-cell"><span>재조정 계산</span><strong>${escapeHtml(reason)}</strong></div>`;
    renderRebalanceDataTable("#rebalance-data-table", [], rebalance);
  }
}

function renderWealthDataTable(dates, officialWealth, explorationWealth) {
  const tbody = $("#wealth-data-table tbody");
  if (!tbody) return;
  const rows = dates.map((date, index) => ({
    date,
    official: officialWealth[index],
    exploration: explorationWealth[index],
  })).filter((row) => row.official !== null || row.exploration !== null);
  tbody.innerHTML = rows.length ? rows.map((row) => `<tr><td>${escapeHtml(row.date)}</td><td>${Number.isFinite(row.official) ? (row.official * 100).toFixed(2) : "—"}</td><td>${Number.isFinite(row.exploration) ? (row.exploration * 100).toFixed(2) : "—"}</td></tr>`).join("") : '<tr><td colspan="3">표시할 차트 값이 없습니다.</td></tr>';
}

function renderDrawdownDataTable(dates, drawdowns) {
  const tbody = $("#drawdown-data-table tbody");
  if (!tbody) return;
  const rows = dates.map((date, index) => [date, drawdowns[index]]);
  tbody.innerHTML = rows.length
    ? rows.map(([date, value]) => `<tr><td>${escapeHtml(date)}</td><td>${fmtPercent(value)}</td></tr>`).join("")
    : '<tr><td colspan="2">표시할 낙폭 값이 없습니다.</td></tr>';
}

function renderGrowthDataTable(selector, points, markers = []) {
  const tbody = $(`${selector} tbody`);
  if (!tbody) return;
  tbody.innerHTML = points.length ? points.map(([leverage, growth]) => {
    const labels = markers
      .filter((marker) => Math.abs(marker.x - leverage) < 1e-9 && Math.abs(marker.y - growth) < 1e-9)
      .map((marker) => marker.name)
      .join(", ");
    return `<tr><td>${fmtLeverage(leverage)}</td><td>${fmtPercent(growth)}</td><td>${escapeHtml(labels || "—")}</td></tr>`;
  }).join("") : '<tr><td colspan="3">표시할 성장률 값이 없습니다.</td></tr>';
}

function renderRebalanceDataTable(selector, dates, comparison) {
  const tbody = $(`${selector} tbody`);
  if (!tbody) return;
  if (comparison?.status !== STATUS.PUBLISHED) {
    tbody.innerHTML = `<tr><td colspan="4">${escapeHtml(reasonText(comparison?.reasonCode))}</td></tr>`;
    return;
  }
  const labels = rebalanceAxisLabels(dates, comparison.net.wealth.length);
  tbody.innerHTML = labels.map((date, index) => `<tr><td>${escapeHtml(date)}</td><td>${fmtNumber(comparison.buyAndHold.wealth[index])}</td><td>${fmtNumber(comparison.gross.wealth[index])}</td><td>${fmtNumber(comparison.net.wealth[index])}</td></tr>`).join("");
}

function renderRebalanceSummary(result) {
  $("#rebalance-summary").innerHTML = [
    ["총 재조정 효과", fmtPercent(result.grossRebalancingEffect)],
    ["거래비용 드래그", fmtPercent(result.transactionCostDrag)],
    ["순 재조정 효과", fmtPercent(result.netRebalancingEffect)],
    ["누적 회전율", fmtPercent(result.turnover)],
  ].map(([label, value]) => `<div class="summary-cell"><span>${label}</span><strong>${value}</strong></div>`).join("");
}

function onChartExplore(start, end) {
  if (!start || !end || !state.period || (start === state.period.exploration.start && end === state.period.exploration.end)) return;
  const previous = state.period;
  const next = setExplorationRange(previous, start, end, { minimum: state.series.dates[0], maximum: state.series.dates.at(-1) });
  if (next.error) {
    state.period = previous;
    syncPeriodControls();
    updateExplorationSummary();
    return false;
  }
  state.period = next;
  syncPeriodControls();
  updateExplorationSummary();
  return true;
}

function updateExplorationSummary(rerenderChart = true) {
  if (!state.series || !state.period) return;
  const selected = sliceSeries(state.series, state.period.exploration.start, state.period.exploration.end);
  const path = wealthPath(selected.returns);
  const changed = state.period.exploration.start !== state.period.official.start || state.period.exploration.end !== state.period.official.end;
  $("#exploration-period-label").textContent = `${state.period.exploration.start} – ${state.period.exploration.end} · 탐색값은 공식 카드에 미반영`;
  $("#exploration-return").textContent = path.status === STATUS.PUBLISHED ? fmtPercent(path.wealth.at(-1) - 1) : reasonText(path.reasonCode);
  $("#apply-exploration").disabled = !changed || path.status !== STATUS.PUBLISHED;
  if (rerenderChart) {
    cancelAnimationFrame(state.explorationFrame);
    state.explorationFrame = requestAnimationFrame(() => {
      const officialWealth = rangeWealth(state.series, state.period.official.start, state.period.official.end);
      const explorationWealth = rangeWealth(state.series, state.period.exploration.start, state.period.exploration.end);
      renderWealthDataTable(state.series.dates, officialWealth, explorationWealth);
      renderWealthChart($("#wealth-chart"), {
        dates: state.series.dates, officialWealth, explorationWealth,
        explorationStart: state.period.exploration.start, explorationEnd: state.period.exploration.end,
        returnBasis: returnBasisLabel(state.assetEntry?.returnBasis ?? state.series.returnBasis),
      }, onChartExplore);
    });
  }
}

async function renderLeverageComparison(result, generation) {
  const tbody = $("#leverage-comparison-table tbody");
  if (!result || !isCurrentRequest("leverage", generation)) return;
  const { official, inputs, assetEntry, currency, period } = result;
  tbody.innerHTML = '<tr><td colspan="6">2배 경로를 계산하는 중입니다.</td></tr>';
  const synthetic = leveragedReturnPath(
    official.returns,
    2,
    inputs.riskFreeRate,
    inputs.annualizationDays,
    inputs.borrowingSpread,
  );
  let syntheticArithmetic = null;
  let syntheticCumulative = null;
  let syntheticCagr = null;
  if (synthetic.status === STATUS.PUBLISHED) {
    const syntheticMetrics = performanceMetrics(synthetic.returns, official.dates, {
      minObservations: 2,
      riskFreeRate: inputs.riskFreeRate,
      annualizationDays: inputs.annualizationDays,
    });
    syntheticArithmetic = syntheticMetrics.annualArithmeticReturn?.value;
    syntheticCumulative = syntheticMetrics.cumulativeReturn?.value;
    syntheticCagr = syntheticMetrics.cagr?.value;
  }
  const rows = [{
    id: "synthetic_2x", path: "합성 고정 2배", product: `${assetEntry?.ticker ?? "기초자산"} × 2`, arithmetic: syntheticArithmetic,
    cumulative: syntheticCumulative, cagr: syntheticCagr, status: synthetic.status,
  }];
  result.leverageComparison = rows;

  const mapping = assetEntry?.leveragedProducts?.long2x ?? assetEntry?.leveraged_products?.long2x;
  const mappedId = typeof mapping === "string" ? mapping : mapping?.id ?? mapping?.ticker;
  const actualEntry = state.catalog.find((asset) => asset.id === mappedId || asset.ticker === mappedId);
  if (!actualEntry) {
    rows.push({ id: "actual_daily_target_2x", path: "실제 일간목표 2배 ETF", product: mappedId || "매핑 없음", status: STATUS.UNAVAILABLE, reason: "공개 카탈로그 이력 없음" });
  } else {
    try {
      const actualPayload = await fetchAssetPayload(actualEntry);
      if (!isCurrentRequest("leverage", generation) || state.officialResult !== result) return;
      const actualSeries = await seriesForCurrency(actualPayload, currency);
      if (!isCurrentRequest("leverage", generation) || state.officialResult !== result) return;
      const actualSlice = sliceSeries(actualSeries, period.official.start, period.official.end);
      const actualMetrics = performanceMetrics(actualSlice.returns, actualSlice.dates, {
        minObservations: 60,
        riskFreeRate: inputs.riskFreeRate,
        annualizationDays: inputs.annualizationDays,
      });
      const sourceStatus = payloadState(actualPayload);
      const status = actualMetrics.status === STATUS.PUBLISHED && [STATUS.LIVE_API, STATUS.STALE, STATUS.DEGRADED].includes(sourceStatus)
        ? sourceStatus
        : actualMetrics.status;
      rows.push({
        id: "actual_daily_target_2x", path: "실제 일간목표 2배 ETF", product: actualEntry.ticker, arithmetic: actualMetrics.annualArithmeticReturn?.value,
        cumulative: actualMetrics.cumulativeReturn?.value, cagr: actualMetrics.cagr?.value, status,
        reason: actualMetrics.reasonCode || ([STATUS.STALE, STATUS.DEGRADED].includes(status)
          ? `원본 ${status}${actualPayload.dataAsOf ? ` · 기준일 ${actualPayload.dataAsOf}` : ""}` : null),
      });
    } catch (error) {
      rows.push({ id: "actual_daily_target_2x", path: "실제 일간목표 2배 ETF", product: actualEntry.ticker, status: STATUS.UNAVAILABLE, reason: reasonText(error.message) });
    }
  }
  if (!isCurrentRequest("leverage", generation) || state.officialResult !== result) return;
  result.leverageComparison = rows;
  tbody.innerHTML = rows.map((row) => {
    const status = row.status === STATUS.PUBLISHED ? "published · 사용 가능"
      : row.status === STATUS.LIVE_API ? "live_api · 사용 가능"
        : row.status === STATUS.RUIN ? "ruin · 자산배수≤0"
          : [STATUS.STALE, STATUS.DEGRADED].includes(row.status) ? `${row.status} · ${row.reason || "원본 상태 확인 필요"}`
            : `unavailable · ${row.reason || "이력 없음"}`;
    return `<tr><td>${escapeHtml(row.path)}</td><td>${escapeHtml(row.product)}</td><td>${fmtPercent(row.arithmetic)}</td><td>${row.status === STATUS.RUIN ? "파산" : fmtPercent(row.cumulative)}</td><td>${row.status === STATUS.RUIN ? "파산" : fmtPercent(row.cagr)}</td><td>${escapeHtml(status)}</td></tr>`;
  }).join("");
  return rows;
}

function renderUnavailableHistorical(reasonCode) {
  $("#historical-kelly-cards").innerHTML = metricCard("공식 결과 사용 불가", "—", reasonText(reasonCode), true);
  $("#performance-cards").innerHTML = "";
  $("#historical-presets").innerHTML = "";
  $("#official-period-label").textContent = `현재 입력으로 계산할 수 없습니다 · ${reasonText(reasonCode)}`;
  $("#leverage-comparison-table tbody").innerHTML = `<tr><td colspan="6">unavailable · ${escapeHtml(reasonText(reasonCode))}</td></tr>`;
}

function applyOfficialDates(start, end) {
  if (!state.series || !state.period) return;
  const bounds = { minimum: state.series.dates[0], maximum: state.series.dates.at(-1) };
  const next = setExplorationRange(state.period, start, end, bounds);
  if (next.error) {
    showNotice("기간이 잘못되어 기존 공식 결과를 보존했습니다. 시작일은 끝일보다 앞서야 하며 가용기간 안이어야 합니다.", "error", "historical");
    syncPeriodControls();
    return;
  }
  const applied = applyExplorationRange(next, bounds);
  const candidate = sliceSeries(state.series, applied.official.start, applied.official.end);
  if (candidate.returns.length < 2) {
    showNotice("공식 성과지표에는 최소 2개 일간수익률이 필요합니다. 기존 결과를 보존했습니다.", "error", "historical");
    syncPeriodControls();
    return;
  }
  state.period = applied;
  syncPeriodControls();
  showNotice("탐색기간을 공식 분석기간에 적용했습니다.", "success", "historical");
  renderHistorical();
}

function parseCsvLine(line) {
  const cells = [];
  let value = "";
  let quoted = false;
  for (let index = 0; index < line.length; index += 1) {
    const character = line[index];
    if (character === '"') {
      if (quoted && line[index + 1] === '"') { value += '"'; index += 1; }
      else quoted = !quoted;
    } else if (character === "," && !quoted) {
      cells.push(value.trim());
      value = "";
    } else {
      value += character;
    }
  }
  if (quoted) throw new Error("csv_quote_error");
  cells.push(value.trim());
  return cells;
}

function parsePriceCsv(text, fileName = "CSV") {
  const lines = String(text || "").replace(/^\ufeff/, "").split(/\r?\n/).filter((line) => line.trim());
  if (lines.length < 4) throw new Error(REASON.INSUFFICIENT_OBSERVATIONS);
  const headers = parseCsvLine(lines[0]).map((header) => header.trim().toLowerCase());
  const dateIndex = headers.indexOf("date");
  const priceIndex = headers.indexOf("price");
  const currencyIndex = headers.indexOf("currency");
  if (dateIndex < 0 || priceIndex < 0) throw new Error("csv_required_columns");
  const records = lines.slice(1).map((line) => {
    const cells = parseCsvLine(line);
    const date = cells[dateIndex];
    const price = Number(cells[priceIndex]);
    const parsed = new Date(`${date}T00:00:00Z`);
    if (!/^\d{4}-\d{2}-\d{2}$/.test(date) || Number.isNaN(parsed.valueOf()) || parsed.toISOString().slice(0, 10) !== date || !Number.isFinite(price) || price <= 0) {
      throw new Error("csv_invalid_row");
    }
    return { date, price, currency: String(cells[currencyIndex] || "USD").toUpperCase() };
  }).sort((left, right) => left.date.localeCompare(right.date));
  if (new Set(records.map((record) => record.date)).size !== records.length) throw new Error("csv_duplicate_date");
  const currency = records[0].currency;
  if (!/^[A-Z]{3}$/.test(currency) || records.some((record) => record.currency !== currency)) throw new Error("csv_currency_mismatch");
  const ticker = String(fileName || "CSV").replace(/\.csv$/i, "").slice(0, 24) || "CSV";
  return {
    schemaVersion: 1,
    contract: "kelly-asset-history",
    state: STATUS.PUBLISHED,
    assetId: "csv-upload",
    generatedAt: new Date().toISOString(),
    dataAsOf: records.at(-1).date,
    metadata: {
      symbol: ticker,
      name: `${ticker} CSV`,
      assetType: "equity",
      exchange: "USER",
      timezone: "UTC",
      returnBasis: "price_return",
      baseCurrency: currency,
    },
    dates: records.map((record) => record.date),
    prices: records.map((record) => record.price),
    returns: [],
    source: { provider: "user_csv", normalized: true },
    limitations: ["사용자가 불러온 로컬 CSV이며 공개 데이터 계약에는 저장되지 않습니다."],
  };
}

async function importCsvFile(file) {
  if (!file) return;
  try {
    const payload = parsePriceCsv(await file.text(), file.name);
    const existingIndex = state.catalog.findIndex((asset) => asset.id === payload.assetId);
    const entry = {
      id: payload.assetId,
      ticker: payload.metadata.symbol,
      symbol: payload.metadata.symbol,
      name: payload.metadata.name,
      type: "equity",
      assetType: "equity",
      currency: payload.metadata.baseCurrency,
      returnBasis: payload.metadata.returnBasis,
      status: STATUS.PUBLISHED,
    };
    if (existingIndex >= 0) state.catalog.splice(existingIndex, 1, entry);
    else state.catalog.unshift(entry);
    state.assetCache.set(entry.id, Promise.resolve(payload));
    populateAssetSelect();
    $("#asset-select").value = entry.id;
    state.currency = "native";
    $$("[data-currency]").forEach((button) => {
      const active = button.dataset.currency === "native";
      button.classList.toggle("is-active", active);
      button.setAttribute("aria-pressed", String(active));
    });
    await loadSelectedAsset();
    showNotice(`${payload.metadata.symbol} CSV ${payload.dates.length.toLocaleString("ko-KR")}개 가격을 불러왔습니다.`, "success", "historical");
  } catch (error) {
    showNotice(`CSV를 적용하지 않았습니다. date·price 열, 날짜 중복, 양수 가격을 확인하세요. (${reasonText(error.message)})`, "error", "historical");
  }
}

function downloadCsvTemplate() {
  const csv = rowsToCsv(
    ["date", "price", "return", "currency"],
    [
      ["2026-01-02", "100.00", "", "USD"],
      ["2026-01-05", "101.25", "0.0125", "USD"],
      ["2026-01-06", "100.74", "-0.0050", "USD"],
    ],
  );
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = "kelly-price-template.csv";
  link.click();
  URL.revokeObjectURL(link.href);
}

function quickPeriodStart(period, dates) {
  const endValue = dates?.at(-1);
  if (!endValue) return null;
  if (period === "all") return dates[0];
  const end = new Date(`${endValue}T00:00:00Z`);
  let target = new Date(end);
  if (period === "ytd") target = new Date(Date.UTC(end.getUTCFullYear(), 0, 1));
  else {
    const offsets = { "1m": 1, "3m": 3, "6m": 6, "1y": 12, "3y": 36, "5y": 60 };
    const months = offsets[period];
    if (!months) return null;
    const day = target.getUTCDate();
    target.setUTCDate(1);
    target.setUTCMonth(target.getUTCMonth() - months);
    const lastDay = new Date(Date.UTC(target.getUTCFullYear(), target.getUTCMonth() + 1, 0)).getUTCDate();
    target.setUTCDate(Math.min(day, lastDay));
  }
  const targetIso = target.toISOString().slice(0, 10);
  return dates.find((date) => date >= targetIso) ?? dates[0];
}

function applyQuickPeriod(period) {
  if (!state.series) return;
  const start = quickPeriodStart(period, state.series.dates);
  if (!start) return;
  applyOfficialDates(start, state.series.dates.at(-1));
}

function moveExplorationToPeriodEnd() {
  if (!state.series || !state.period) return;
  const dates = state.series.dates;
  const startIndex = Math.max(0, dates.indexOf(state.period.exploration.start));
  const endIndex = Math.max(startIndex, dates.indexOf(state.period.exploration.end));
  const width = Math.max(1, endIndex - startIndex);
  const nextEnd = dates.length - 1;
  const nextStart = Math.max(0, nextEnd - width);
  state.period = setExplorationRange(state.period, dates[nextStart], dates[nextEnd], { minimum: dates[0], maximum: dates.at(-1) });
  syncPeriodControls();
  updateExplorationSummary();
}

function toggleWealthDataTable() {
  const table = $("#wealth-data-table");
  const button = $("#wealth-table-toggle");
  if (!table || !button) return;
  table.hidden = !table.hidden;
  button.setAttribute("aria-expanded", String(!table.hidden));
  button.textContent = table.hidden ? "차트 값을 표로 보기" : "차트 값 표 닫기";
}

function configureHistoricalEvents() {
  $("#asset-select").addEventListener("change", loadSelectedAsset);
  $("#csv-upload")?.addEventListener("click", () => $("#csv-file")?.click());
  $("#csv-file")?.addEventListener("change", async (event) => {
    await importCsvFile(event.target.files?.[0]);
    event.target.value = "";
  });
  $("#csv-template-download")?.addEventListener("click", downloadCsvTemplate);
  $$("[data-period]").forEach((button) => button.addEventListener("click", () => applyQuickPeriod(button.dataset.period)));
  $("#wealth-period-end")?.addEventListener("click", moveExplorationToPeriodEnd);
  $("#wealth-table-toggle")?.addEventListener("click", toggleWealthDataTable);
  $$("[data-currency]").forEach((button) => button.addEventListener("click", async () => {
    const previous = state.currency;
    const requested = button.dataset.currency;
    const payload = state.rawPayload;
    const previousPeriod = state.period;
    if (requested === previous || !payload) return;
    const generation = nextRequestGeneration("currency");
    nextRequestGeneration("leverage");
    setCurrencyControlsDisabled(true);
    try {
      const converted = await seriesForCurrency(payload, requested);
      if (!isCurrentRequest("currency", generation) || state.rawPayload !== payload) return;
      state.currency = requested;
      state.series = converted;
      syncCurrencyButtons();
      initializePeriod(converted, previousPeriod);
      renderAssetMeta(
        { ...state.assetEntry, returnBasis: converted.returnBasis ?? state.assetEntry?.returnBasis, currency: converted.currency ?? state.assetEntry?.currency },
        converted.source ?? payload.source,
        payload.quality,
        converted.returns.length,
      );
      renderHistorical();
      showNotice(`${requested === "krw" ? "KRW 환산" : "원통화"}을 적용하고 공식·탐색 기간을 유지했습니다.`, "success", "historical");
    } catch (error) {
      if (!isCurrentRequest("currency", generation)) return;
      state.currency = previous;
      syncCurrencyButtons();
      showNotice(`KRW 환산을 적용하지 않았습니다. 선행값 없는 FX 또는 5일 초과 공백을 보간하지 않습니다. (${reasonText(error.message)})`, "error", "historical");
    } finally {
      if (isCurrentRequest("currency", generation)) setCurrencyControlsDisabled(false);
    }
  }));
  $("#apply-official-inputs").addEventListener("click", () => applyOfficialDates($("#official-start").value, $("#official-end").value));
  $("#apply-exploration").addEventListener("click", () => applyOfficialDates(state.period.exploration.start, state.period.exploration.end));
  for (const id of ["#risk-free", "#borrow-spread", "#transaction-cost", "#annualization-days", "#sortino-mar", "#rebalance-frequency"]) {
    $(id).addEventListener("change", () => state.series && renderHistorical());
  }
  for (const id of ["#explore-start-slider", "#explore-end-slider"]) {
    $(id).addEventListener("input", onKeyboardExplore);
  }
  $("#share-url").addEventListener("click", shareCurrentUrl);
  $("#export-csv").addEventListener("click", exportCurrentCsv);
}

function onKeyboardExplore(event) {
  if (!state.series || !state.period) return;
  let start = Number($("#explore-start-slider").value);
  let end = Number($("#explore-end-slider").value);
  if (start >= end) {
    if (event.target.id === "explore-start-slider") start = Math.max(0, end - 1);
    else end = Math.min(state.series.dates.length - 1, start + 1);
  }
  state.period = setExplorationRange(state.period, state.series.dates[start], state.series.dates[end], { minimum: state.series.dates[0], maximum: state.series.dates.at(-1) });
  syncPeriodControls();
  updateExplorationSummary();
}

function setControlValue(selector, value) {
  const control = $(selector);
  if (control && value !== undefined) control.value = String(value);
}

function syncCurrencyButtons() {
  $$("[data-currency]").forEach((button) => {
    const active = button.dataset.currency === state.currency;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
}

function setCurrencyControlsDisabled(disabled) {
  $$("[data-currency]").forEach((button) => {
    button.disabled = disabled;
    button.setAttribute("aria-busy", String(disabled));
  });
}

function applyInitialShareInputs() {
  const historical = initialShareState.historical ?? {};
  setControlValue("#risk-free", historical.riskFreeRate);
  setControlValue("#borrow-spread", historical.borrowingSpread);
  setControlValue("#transaction-cost", historical.transactionCostBps);
  setControlValue("#annualization-days", historical.annualizationDays);
  setControlValue("#sortino-mar", historical.mar);
  setControlValue("#rebalance-frequency", historical.rebalance);
  syncCurrencyButtons();

  const direct = initialShareState.direct ?? {};
  setControlValue("#direct-excess", direct.expectedExcess);
  setControlValue("#direct-vol", direct.volatility);
  setControlValue("#direct-rf", direct.riskFreeRate);
  setControlValue("#direct-spread", direct.borrowingSpread);

  const portfolio = initialShareState.portfolio ?? {};
  setControlValue("#portfolio-rf", portfolio.riskFreeRate);
  setControlValue("#portfolio-borrow-spread", portfolio.borrowingSpread);
  setControlValue("#portfolio-cap", portfolio.cap);
  setControlValue("#portfolio-rebalance-frequency", portfolio.rebalance);
  setControlValue("#portfolio-transaction-cost", portfolio.transactionCostBps);
  if (portfolio.directAssets && portfolio.correlation) {
    state.portfolioDirectAssets = portfolio.directAssets.map((asset) => ({ ...asset }));
    state.portfolioMatrices.direct = portfolio.correlation.map((row) => [...row]);
    state.portfolioMatrixEdited.direct = true;
  }
}

function collectCurrentShareState() {
  const mode = activeMode();
  if (mode === "historical") return {
    mode,
    historical: {
      asset: state.assetEntry?.id ?? $("#asset-select")?.value,
      start: state.period?.official.start ?? $("#official-start")?.value,
      end: state.period?.official.end ?? $("#official-end")?.value,
      currency: state.currency,
      riskFreeRate: $("#risk-free")?.value,
      borrowingSpread: $("#borrow-spread")?.value,
      transactionCostBps: $("#transaction-cost")?.value,
      annualizationDays: $("#annualization-days")?.value,
      mar: $("#sortino-mar")?.value ?? "",
      rebalance: $("#rebalance-frequency")?.value,
    },
  };
  if (mode === "direct") return {
    mode,
    direct: {
      expectedExcess: $("#direct-excess")?.value,
      volatility: $("#direct-vol")?.value,
      riskFreeRate: $("#direct-rf")?.value,
      borrowingSpread: $("#direct-spread")?.value,
    },
  };
  return {
    mode,
    portfolio: {
      source: state.portfolioSource,
      riskFreeRate: $("#portfolio-rf")?.value,
      borrowingSpread: $("#portfolio-borrow-spread")?.value,
      cap: $("#portfolio-cap")?.value,
      directAssets: state.portfolioDirectAssets.map(({ name, expectedExcess, volatility }) => ({ name, expectedExcess, volatility })),
      correlation: state.portfolioMatrices.direct.map((row) => [...row]),
      historicalAssetIds: [...state.portfolioHistoryIds],
      start: $("#portfolio-history-start")?.value || state.portfolioHistoryPeriod.start,
      end: $("#portfolio-history-end")?.value || state.portfolioHistoryPeriod.end,
      rebalance: $("#portfolio-rebalance-frequency")?.value,
      transactionCostBps: $("#portfolio-transaction-cost")?.value,
    },
  };
}

async function shareCurrentUrl() {
  const configuration = collectCurrentShareState();
  if (configuration.mode === "historical" && !isShareableHistoricalAssetId(configuration.historical.asset)) {
    showNotice("업로드 CSV 원문은 URL에 포함되지 않아 공유 링크로 복원할 수 없습니다. 결과 CSV를 내보내 함께 전달하세요.", "error", "historical");
    return;
  }
  const query = serializeShareState(configuration);
  const target = new URL(location.href);
  target.search = query;
  target.hash = "";
  const url = target.toString();
  history.replaceState(null, "", url);
  const label = configuration.mode === "historical" ? "현재 공식 분석 설정" : "현재 입력 설정";
  try { await navigator.clipboard.writeText(url); showNotice(`${label} URL을 복사했습니다.`, "success"); }
  catch { showNotice(`주소 표시줄에 ${label} URL을 반영했습니다.`, "success"); }
}

async function exportCurrentCsv() {
  const result = state.officialResult;
  if (!result) { showNotice("내보낼 공식 분석 결과가 없습니다.", "error"); return; }
  if (result.leverageComparisonPromise) {
    showNotice("실제 2배 ETF 비교를 확인한 뒤 결과 CSV를 만듭니다.", "info", "historical");
    await result.leverageComparisonPromise;
    if (state.officialResult !== result) {
      showNotice("분석 대상이 바뀌어 이전 결과 CSV를 만들지 않았습니다. 현재 결과에서 다시 시도하세요.", "error", "historical");
      return;
    }
  }
  const rows = [
    ["metadata", "asset_id", result.assetEntry?.id],
    ["metadata", "ticker", result.assetEntry?.ticker],
    ["metadata", "currency", result.currency],
    ["metadata", "return_basis", result.official.returnBasis],
    ["metadata", "official_start", result.period.official.start],
    ["metadata", "official_end", result.period.official.end],
    ["metadata", "observations", result.metrics.observations],
    ["assumption", "risk_free_rate", result.inputs.riskFreeRate],
    ["assumption", "borrowing_spread", result.inputs.borrowingSpread],
    ["assumption", "annualization_days", result.inputs.annualizationDays],
    ["assumption", "sortino_mar", result.inputs.mar],
    ["assumption", "rebalance_frequency", result.inputs.frequency],
    ["assumption", "transaction_cost_bps", result.inputs.transactionCostBps],
    ["metric", "cumulative_return", result.metrics.cumulativeReturn.value],
    ["metric", "annual_arithmetic_return", result.metrics.annualArithmeticReturn.value],
    ["metric", "cagr", result.metrics.cagr.value],
    ["metric", "annual_volatility", result.metrics.annualVolatility.value],
    ["metric", "max_drawdown", result.metrics.maxDrawdown.value],
    ["metric", "sharpe", result.metrics.sharpe.value],
    ["metric", "sortino", result.metrics.sortino.value],
    ["metric", "calmar_style", result.metrics.calmar.value],
    ["kelly", "status", result.kelly.status],
    ["kelly", "reason", result.kelly.reasonCode],
    ["kelly", "minimum_observations", result.kellyEligibility?.minimumObservations],
    ["kelly", "full_theoretical_no_borrowing", result.kelly.theoreticalFullKelly],
    ["kelly", "full_cost_adjusted_raw", result.kelly.optimalWithBorrowing],
    ["kelly", "full_maximum_geometric_growth", result.kelly.maximumAnnualGrowth],
    ["kelly", "full_applied", result.kelly.appliedFullKelly],
    ["kelly", "full_applied_geometric_growth", result.kelly.appliedAnnualGrowth],
    ["kelly", "twice_arithmetic_wealth_return", result.kelly.twiceArithmeticWealthReturn],
    ["kelly", "twice_geometric_growth", result.kelly.twiceAnnualGrowth],
    ["exact_kelly", "status", result.exact.status],
    ["exact_kelly", "theoretical_leverage", result.exact.theoreticalLeverage],
    ["exact_kelly", "applied_leverage", result.exact.appliedLeverage],
    ["exact_kelly", "annual_log_growth", result.exact.annualLogGrowth],
    ["exact_kelly", "applied_annual_growth", result.exact.appliedAnnualGrowth],
  ];
  for (const preset of result.kelly.presets ?? []) {
    const name = preset.fraction === 0.25 ? "quarter" : preset.fraction === 0.5 ? "half" : "full";
    rows.push(
      ["kelly_preset", `${name}_fraction`, preset.fraction],
      ["kelly_preset", `${name}_leverage`, preset.leverage],
      ["kelly_preset", `${name}_log_growth`, preset.logGrowth],
      ["kelly_preset", `${name}_annual_growth`, preset.annualGrowth],
    );
  }
  if (result.rebalance?.status === STATUS.PUBLISHED) rows.push(
    ["rebalance", "gross_effect", result.rebalance.grossRebalancingEffect],
    ["rebalance", "transaction_cost_drag", result.rebalance.transactionCostDrag],
    ["rebalance", "net_effect", result.rebalance.netRebalancingEffect],
    ["rebalance", "turnover", result.rebalance.turnover],
  );
  for (const comparison of result.leverageComparison ?? []) {
    const prefix = comparison.id ?? "leverage_2x";
    rows.push(
      ["leverage_comparison", `${prefix}_path`, comparison.path],
      ["leverage_comparison", `${prefix}_product`, comparison.product],
      ["leverage_comparison", `${prefix}_annual_arithmetic_return`, comparison.arithmetic],
      ["leverage_comparison", `${prefix}_cumulative_return`, comparison.cumulative],
      ["leverage_comparison", `${prefix}_cagr`, comparison.cagr],
      ["leverage_comparison", `${prefix}_status`, comparison.status],
      ["leverage_comparison", `${prefix}_reason`, comparison.reason],
    );
  }
  rows.push(...result.official.returnDates.map((date, index) => ["daily_return", date, result.official.returns[index]]));
  downloadCsvRows(rows, `kelly-${result.assetEntry?.ticker ?? "result"}-${result.period.official.end}.csv`);
  showNotice("공식 지표·Kelly 프리셋·재조정·2배 비교·일별수익률 CSV를 만들었습니다.", "success", "historical");
}

function exportDirectCsv() {
  const result = state.directResult;
  if (!result) { showNotice("내보낼 직접 가정 결과가 없습니다.", "error", "direct"); return; }
  const { inputs, kelly } = result;
  downloadCsvRows([
    ["assumption", "expected_excess_return", inputs.expectedExcessReturn],
    ["assumption", "volatility", inputs.volatility],
    ["assumption", "risk_free_rate", inputs.riskFreeRate],
    ["assumption", "borrowing_spread", inputs.borrowingSpread],
    ["kelly", "full_theoretical_no_borrowing", kelly.theoreticalFullKelly],
    ["kelly", "full_cost_adjusted_raw", kelly.optimalWithBorrowing],
    ["kelly", "full_applied", kelly.appliedFullKelly],
    ["kelly", "maximum_geometric_growth", kelly.maximumAnnualGrowth],
    ["kelly", "applied_geometric_growth", kelly.appliedAnnualGrowth],
    ["kelly", "twice_arithmetic_wealth_return", kelly.twiceArithmeticWealthReturn],
    ["kelly", "twice_geometric_growth", kelly.twiceAnnualGrowth],
  ], "kelly-direct-assumptions.csv");
}

function exportPortfolioCsv() {
  const result = state.lastPortfolioResult;
  if (!result) { showNotice("내보낼 포트폴리오 결과가 없습니다.", "error", "portfolio"); return; }
  const rows = [
    ["metadata", "source", result.source],
    ["assumption", "risk_free_rate", result.calculationInputs?.riskFreeRate],
    ["assumption", "borrowing_spread", result.calculationInputs?.borrowingSpread],
    ["assumption", "exposure_cap", result.calculationInputs?.cap],
    ["portfolio", "status", result.status],
    ["portfolio", "total_exposure", result.totalExposure],
    ["portfolio", "annual_growth", result.annualGrowth],
    ["portfolio", "annual_volatility", result.annualVolatility],
  ];
  result.labels.forEach((label, index) => rows.push(
    ["theoretical_weight", label, result.theoreticalWeights?.[index]],
    ["applied_weight", label, result.appliedWeights[index]],
  ));
  if (result.source === "historical") {
    rows.push(
      ["metadata", "official_start", result.history.joined.dates[0]],
      ["metadata", "official_end", result.history.joined.dates.at(-1)],
      ["metadata", "observations", result.moments.observations],
      ["rebalance", "gross_effect", result.rebalance?.grossRebalancingEffect],
      ["rebalance", "transaction_cost_drag", result.rebalance?.transactionCostDrag],
      ["rebalance", "net_effect", result.rebalance?.netRebalancingEffect],
      ["rebalance", "turnover", result.rebalance?.turnover],
    );
  }
  downloadCsvRows(rows, `kelly-portfolio-${result.source}.csv`);
}

async function exportActiveModeCsv() {
  const mode = activeMode();
  if (mode === "direct") exportDirectCsv();
  else if (mode === "portfolio") exportPortfolioCsv();
  else await exportCurrentCsv();
}

function downloadCsvRows(rows, fileName) {
  const csv = rowsToCsv(["section", "name_or_date", "value"], rows);
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = fileName;
  link.click();
  URL.revokeObjectURL(link.href);
}

function directInputs() {
  return {
    expectedExcessReturn: pctInput("#direct-excess"),
    volatility: pctInput("#direct-vol"),
    riskFreeRate: pctInput("#direct-rf"),
    borrowingSpread: pctInput("#direct-spread"),
  };
}

function configureDirectEvents() {
  for (const id of ["#direct-excess", "#direct-vol", "#direct-rf", "#direct-spread"]) $(id).addEventListener("input", renderDirect);
}

function renderDirect() {
  const inputs = directInputs();
  const kelly = singleAssetKelly(inputs);
  const cards = $("#direct-kelly-cards");
  if (kelly.status !== STATUS.PUBLISHED) {
    state.directResult = null;
    cards.innerHTML = metricCard("Kelly 계산 불가", "—", reasonText(kelly.reasonCode), true);
    $("#direct-presets").innerHTML = "";
    renderGrowthCurve($("#direct-growth-chart"), [], []);
    renderGrowthDataTable("#direct-growth-data-table", [], []);
    if ($("#direct-insight-title")) $("#direct-insight-title").textContent = "입력값으로 Kelly 비중을 계산할 수 없습니다.";
    if ($("#direct-insight-detail")) $("#direct-insight-detail").textContent = reasonText(kelly.reasonCode);
    return;
  }
  state.directResult = { inputs: { ...inputs }, kelly };
  cards.innerHTML = [
    metricCard("Full Kelly 최대 기하성장률", fmtPercent(kelly.maximumAnnualGrowth), fullKellyNote(kelly), true),
    metricCard("상한 적용 경로", fmtLeverage(kelly.appliedFullKelly), `기하성장률 ${fmtPercent(kelly.appliedAnnualGrowth)}${kelly.capApplied ? " · 3× 상한" : ""}`),
    metricCard("2배 기대 산술 자산수익률", fmtPercent(kelly.twiceArithmeticWealthReturn), "변동성 드래그 차감 전"),
    metricCard("2배 장기 기하성장률", fmtPercent(kelly.twiceAnnualGrowth), `로그성장률 ${fmtPercent(kelly.twiceLogGrowth)}`),
  ].join("");
  renderPresets($("#direct-presets"), kelly);
  const growthGap = kelly.maximumAnnualGrowth - kelly.twiceAnnualGrowth;
  if ($("#direct-insight-title")) {
    $("#direct-insight-title").textContent = growthGap >= 0
      ? `Full Kelly의 장기 성장률이 2배보다 ${percentage.format(growthGap)} 높습니다.`
      : `2배의 장기 성장률이 Full Kelly보다 ${percentage.format(-growthGap)} 높습니다.`;
  }
  if ($("#direct-insight-detail")) {
    const theory = Math.abs(kelly.optimalWithBorrowing - kelly.theoreticalFullKelly) > 1e-9
      ? ` · 무비용 이론 ${fmtLeverage(kelly.theoreticalFullKelly)}` : "";
    $("#direct-insight-detail").textContent = `비용 반영 Full ${fmtLeverage(kelly.optimalWithBorrowing)}${theory} · 상한 적용 ${fmtLeverage(kelly.appliedFullKelly)} / ${fmtPercent(kelly.appliedAnnualGrowth)} · 2배 ${fmtPercent(kelly.twiceAnnualGrowth)}`;
  }
  const points = Array.from({ length: 121 }, (_, index) => {
    const leverage = index / 40;
    return [leverage, continuousGrowthRate({ leverage, ...inputs })];
  });
  const markers = [
    { name: "Full", x: kelly.appliedFullKelly, y: kelly.appliedLogGrowth },
    { name: "2×", x: 2, y: kelly.twiceLogGrowth, color: "#c58b24" },
  ];
  renderGrowthCurve($("#direct-growth-chart"), points, markers);
  renderGrowthDataTable("#direct-growth-data-table", points, markers);
}

function identityMatrix(size) {
  return Array.from({ length: size }, (_, row) => Array.from({ length: size }, (_, column) => row === column ? 1 : 0));
}

function resizeCorrelationMatrix(matrix, size) {
  const resized = identityMatrix(size);
  for (let row = 0; row < size; row += 1) for (let column = 0; column < size; column += 1) {
    const value = Number(matrix?.[row]?.[column]);
    if (row !== column && Number.isFinite(value)) resized[row][column] = value;
  }
  return resized;
}

function removeCorrelationIndex(matrix, index) {
  if (!Number.isInteger(index) || index < 0 || index >= matrix.length) return matrix.map((row) => [...row]);
  return matrix
    .filter((_, row) => row !== index)
    .map((row) => row.filter((_, column) => column !== index));
}

function eligiblePortfolioCatalog() {
  return state.catalog.filter((asset) => !["fx", "currency"].includes(asset.type) && asset.id !== "csv-upload");
}

function portfolioAssetCount() {
  return state.portfolioSource === "historical" ? state.portfolioHistoryIds.length : state.portfolioDirectAssets.length;
}

function portfolioLabels() {
  if (state.portfolioSource === "historical") {
    return state.portfolioHistoryIds.map((id, index) => {
      const asset = state.catalog.find((candidate) => candidate.id === id);
      return asset?.ticker || asset?.name || `자산 ${index + 1}`;
    });
  }
  return state.portfolioDirectAssets.map((asset, index) => asset.name.trim() || `자산 ${index + 1}`);
}

function activePortfolioMatrix() {
  return state.portfolioMatrices[state.portfolioSource];
}

function invalidateHistoricalPortfolio() {
  state.portfolioHistoryData = null;
  state.portfolioHistoryPeriod = { start: null, end: null };
  state.pendingPortfolioHistoryPeriod = null;
  state.portfolioMatrixEdited.historical = false;
  state.portfolioMatrices.historical = identityMatrix(state.portfolioHistoryIds.length);
  if ($("#portfolio-history-start")) $("#portfolio-history-start").value = "";
  if ($("#portfolio-history-end")) $("#portfolio-history-end").value = "";
}

function invalidateHistoricalPeriod() {
  state.pendingPortfolioHistoryPeriod = null;
  state.portfolioHistoryPeriod = {
    start: $("#portfolio-history-start").value || null,
    end: $("#portfolio-history-end").value || null,
  };
  state.portfolioHistoryData = null;
}

function defaultFiveYearCommonRange(dates) {
  const end = dates?.at(-1);
  if (!end) return null;
  const target = new Date(`${end}T00:00:00Z`);
  if (Number.isNaN(target.valueOf())) return null;
  target.setUTCFullYear(target.getUTCFullYear() - 5);
  const targetDate = target.toISOString().slice(0, 10);
  return { start: dates.find((date) => date >= targetDate) ?? dates[0], end };
}

function renderDirectPortfolioRows() {
  const tbody = $("#portfolio-assets-table tbody");
  tbody.innerHTML = state.portfolioDirectAssets.map((asset, index) => `
    <tr data-portfolio-key="${escapeHtml(asset.key)}">
      <td><input data-p-name aria-label="${index + 1}번째 자산명" value="${escapeHtml(asset.name)}"></td>
      <td><span class="input-suffix"><input data-p-excess aria-label="${index + 1}번째 기대초과수익률" type="number" value="${asset.expectedExcess}" step="0.1"><b>%</b></span></td>
      <td><span class="input-suffix"><input data-p-vol aria-label="${index + 1}번째 변동성" type="number" value="${asset.volatility}" min="0" step="0.1"><b>%</b></span></td>
      <td><button class="remove-asset" type="button" data-remove-direct="${escapeHtml(asset.key)}" aria-label="${escapeHtml(asset.name)} 삭제">×</button></td>
    </tr>`).join("");
  $$('[data-p-name]', tbody).forEach((input, index) => input.addEventListener("input", () => {
    state.portfolioDirectAssets[index].name = input.value;
    buildCorrelationInputs();
  }));
  $$('[data-p-excess]', tbody).forEach((input, index) => input.addEventListener("input", () => { state.portfolioDirectAssets[index].expectedExcess = Number(input.value); }));
  $$('[data-p-vol]', tbody).forEach((input, index) => input.addEventListener("input", () => { state.portfolioDirectAssets[index].volatility = Number(input.value); }));
  $$('[data-remove-direct]', tbody).forEach((button) => button.addEventListener("click", () => removePortfolioAsset(button.dataset.removeDirect)));
}

function historicalAssetOptions(selectedId) {
  const assets = eligiblePortfolioCatalog();
  if (!assets.length) return '<option value="">카탈로그 이용 불가</option>';
  return assets.map((asset) => {
    const stateLabel = [STATUS.PUBLISHED, STATUS.LIVE_API].includes(asset.status) ? "" : ` · ${asset.status}`;
    return `<option value="${escapeHtml(asset.id)}" ${asset.id === selectedId ? "selected" : ""}>${escapeHtml(asset.ticker)} · ${escapeHtml(asset.name)}${escapeHtml(stateLabel)}</option>`;
  }).join("");
}

function renderHistoricalPortfolioRows() {
  const list = $("#portfolio-history-assets");
  list.innerHTML = state.portfolioHistoryIds.map((id, index) => `
    <div class="history-asset-row">
      <label class="field"><span>${index + 1}번째 자산</span><select data-history-index="${index}">${historicalAssetOptions(id)}</select></label>
      <button class="remove-asset" type="button" data-remove-history="${index}" aria-label="${index + 1}번째 자산 삭제">×</button>
    </div>`).join("");
  $$('[data-history-index]', list).forEach((select) => select.addEventListener("change", () => {
    state.portfolioHistoryIds[Number(select.dataset.historyIndex)] = select.value;
    invalidateHistoricalPortfolio();
    renderPortfolioRows();
  }));
  $$('[data-remove-history]', list).forEach((button) => button.addEventListener("click", () => removePortfolioAsset(Number(button.dataset.removeHistory))));
}

function renderPortfolioRows() {
  const historical = state.portfolioSource === "historical";
  $("#portfolio-direct-inputs").hidden = historical;
  $("#portfolio-historical-inputs").hidden = !historical;
  $("#portfolio-history-settings").hidden = !historical;
  if (historical) renderHistoricalPortfolioRows();
  else renderDirectPortfolioRows();
  const count = portfolioAssetCount();
  state.portfolioMatrices[state.portfolioSource] = resizeCorrelationMatrix(activePortfolioMatrix(), count);
  $("#portfolio-asset-count").textContent = `${count} / ${PORTFOLIO_MAX_ASSETS}`;
  $("#portfolio-add-asset").disabled = count >= PORTFOLIO_MAX_ASSETS;
  $$('.remove-asset').forEach((button) => { button.disabled = count <= PORTFOLIO_MIN_ASSETS; });
  buildCorrelationInputs();
}

function addPortfolioAsset() {
  if (portfolioAssetCount() >= PORTFOLIO_MAX_ASSETS) return;
  if (state.portfolioSource === "direct") {
    const number = state.portfolioDirectAssets.length + 1;
    state.portfolioDirectAssets.push({ key: `direct-${Date.now()}-${number}`, name: `자산 ${number}`, expectedExcess: 4, volatility: 20 });
    state.portfolioMatrices.direct = resizeCorrelationMatrix(state.portfolioMatrices.direct, state.portfolioDirectAssets.length);
  } else {
    const next = eligiblePortfolioCatalog().find((asset) => !state.portfolioHistoryIds.includes(asset.id));
    if (!next) {
      $("#portfolio-error").textContent = "추가할 수 있는 카탈로그 자산이 없습니다.";
      return;
    }
    state.portfolioHistoryIds.push(next.id);
    invalidateHistoricalPortfolio();
  }
  renderPortfolioRows();
}

function removePortfolioAsset(identifier) {
  if (portfolioAssetCount() <= PORTFOLIO_MIN_ASSETS) return;
  if (state.portfolioSource === "direct") {
    const index = state.portfolioDirectAssets.findIndex((asset) => asset.key === identifier);
    if (index < 0) return;
    state.portfolioDirectAssets.splice(index, 1);
    state.portfolioMatrices.direct = removeCorrelationIndex(state.portfolioMatrices.direct, index);
  } else {
    state.portfolioHistoryIds.splice(identifier, 1);
    invalidateHistoricalPortfolio();
  }
  renderPortfolioRows();
}

function buildCorrelationInputs() {
  const labels = portfolioLabels();
  const matrix = activePortfolioMatrix();
  const table = $("#correlation-input");
  table.innerHTML = `<thead><tr><th></th>${labels.map((label) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody>${labels.map((label, i) => `<tr><th>${escapeHtml(label)}</th>${labels.map((other, j) => `<td><input type="number" min="-1" max="1" step="0.01" data-corr-row="${i}" data-corr-col="${j}" value="${matrix[i][j]}" aria-label="${escapeHtml(label)}와 ${escapeHtml(other)} 상관계수" ${i === j || i > j ? "readonly" : ""}></td>`).join("")}</tr>`).join("")}</tbody>`;
  $$('[data-corr-row]', table).forEach((input) => input.addEventListener("input", () => {
    const row = Number(input.dataset.corrRow);
    const column = Number(input.dataset.corrCol);
    const value = Number(input.value);
    matrix[row][column] = value;
    matrix[column][row] = value;
    state.portfolioMatrixEdited[state.portfolioSource] = true;
    const mirror = $(`[data-corr-row="${column}"][data-corr-col="${row}"]`, table);
    if (mirror) mirror.value = input.value;
  }));
}

function setPortfolioSource(source) {
  if (!["direct", "historical"].includes(source)) return;
  state.portfolioSource = source;
  $$('[data-portfolio-source]').forEach((button) => {
    const active = button.dataset.portfolioSource === source;
    button.classList.toggle("is-active", active);
    button.setAttribute("aria-pressed", String(active));
  });
  $("#portfolio-error").textContent = "";
  renderPortfolioRows();
  const saved = state.portfolioResults[source];
  state.lastPortfolioResult = saved;
  if (saved) renderPortfolioResult(saved);
  else renderPortfolioPlaceholder(source);
}

function renderPortfolioPlaceholder(source) {
  $("#portfolio-cards").innerHTML = metricCard(source === "historical" ? "역사 계산 대기" : "직접 가정 계산 대기", "—", "입력 후 포트폴리오 계산", true);
  $("#portfolio-insight-title").textContent = source === "historical" ? "공통 KRW 수익률을 불러온 뒤 계산합니다." : "직접 가정값으로 바로 계산할 수 있습니다.";
  $("#portfolio-insight-detail").textContent = source === "historical" ? "최소 60개 공통 거래일과 유효한 FX 결합이 필요합니다." : "기대초과수익률·변동성·상관관계를 확인하세요.";
  $("#portfolio-allocation-table tbody").innerHTML = '<tr><td colspan="3">계산 결과가 없습니다.</td></tr>';
  $("#portfolio-history-estimates").hidden = true;
  $("#portfolio-history-results").hidden = true;
  clearChart($("#weights-chart"), "포트폴리오 비중", "입력 후 포트폴리오를 계산하세요.");
  clearChart($("#correlation-chart"), "상관행렬", "입력 후 포트폴리오를 계산하세요.");
}

function configurePortfolioEvents() {
  $$('[data-portfolio-source]').forEach((button) => button.addEventListener("click", () => setPortfolioSource(button.dataset.portfolioSource)));
  $("#portfolio-add-asset").addEventListener("click", addPortfolioAsset);
  $("#portfolio-history-start").addEventListener("change", invalidateHistoricalPeriod);
  $("#portfolio-history-end").addEventListener("change", invalidateHistoricalPeriod);
  $("#calculate-portfolio").addEventListener("click", () => { void calculatePortfolio(); });
  setPortfolioSource(initialShareState.portfolio?.source ?? "direct");
}

function directPortfolioInputs() {
  return {
    labels: state.portfolioDirectAssets.map((asset, index) => asset.name.trim() || `자산 ${index + 1}`),
    expectedExcessReturns: state.portfolioDirectAssets.map((asset) => Number(asset.expectedExcess) / 100),
    volatilities: state.portfolioDirectAssets.map((asset) => Number(asset.volatility) / 100),
  };
}

async function loadHistoricalPortfolioData() {
  if (new Set(state.portfolioHistoryIds).size !== state.portfolioHistoryIds.length) throw new Error("duplicate_assets");
  const assetKey = state.portfolioHistoryIds.join("|");
  const requestedKey = `${assetKey}|${state.portfolioHistoryPeriod.start ?? ""}|${state.portfolioHistoryPeriod.end ?? ""}`;
  if (state.portfolioHistoryData?.key === requestedKey) return state.portfolioHistoryData;
  const entries = state.portfolioHistoryIds.map((id) => state.catalog.find((asset) => asset.id === id));
  if (entries.some((entry) => !entry)) throw new Error(REASON.DATA_UNAVAILABLE);
  const payloads = await Promise.all(entries.map(fetchAssetPayload));
  const series = await Promise.all(payloads.map((payload) => seriesForCurrency(payload, "krw")));
  if (series.some((item) => ![STATUS.PUBLISHED, STATUS.LIVE_API, STATUS.STALE, STATUS.DEGRADED].includes(item.status))) {
    throw new Error(REASON.DATA_UNAVAILABLE);
  }
  const fullJoined = innerJoinReturnSeries(series, 2);
  if (fullJoined.status !== STATUS.PUBLISHED) throw new Error(fullJoined.reasonCode);
  if (state.pendingPortfolioHistoryPeriod) {
    const pending = state.pendingPortfolioHistoryPeriod;
    state.pendingPortfolioHistoryPeriod = null;
    if (isValidDateRange(pending.start, pending.end, fullJoined.dates[0], fullJoined.dates.at(-1))) {
      state.portfolioHistoryPeriod = pending;
      $("#portfolio-history-start").value = pending.start;
      $("#portfolio-history-end").value = pending.end;
    }
  }
  if (!state.portfolioHistoryPeriod.start || !state.portfolioHistoryPeriod.end) {
    const initial = defaultFiveYearCommonRange(fullJoined.dates);
    if (!initial) throw new Error(REASON.NO_COMMON_RETURNS);
    state.portfolioHistoryPeriod = initial;
    $("#portfolio-history-start").value = initial.start;
    $("#portfolio-history-end").value = initial.end;
  }
  const { start, end } = state.portfolioHistoryPeriod;
  $("#portfolio-history-start").min = fullJoined.dates[0];
  $("#portfolio-history-start").max = fullJoined.dates.at(-1);
  $("#portfolio-history-end").min = fullJoined.dates[0];
  $("#portfolio-history-end").max = fullJoined.dates.at(-1);
  if (!isValidDateRange(start, end, fullJoined.dates[0], fullJoined.dates.at(-1))) throw new Error(REASON.INVALID_RANGE);
  const joined = sliceJoinedReturnSeries(fullJoined, start, end, 60);
  if (joined.status !== STATUS.PUBLISHED) throw new Error(joined.reasonCode);
  const key = `${assetKey}|${start}|${end}`;
  state.portfolioHistoryData = { key, entries, joined, fullJoined, sources: series.map((item) => item.source) };
  return state.portfolioHistoryData;
}

async function calculatePortfolio() {
  const calculationSource = state.portfolioSource;
  const riskFreeRate = pctInput("#portfolio-rf");
  const borrowingSpread = pctInput("#portfolio-borrow-spread");
  const cap = Math.min(MAX_EXPOSURE, Math.max(0, numberInput("#portfolio-cap", MAX_EXPOSURE)));
  const frequency = $("#portfolio-rebalance-frequency").value;
  const transactionCostBps = numberInput("#portfolio-transaction-cost", 10);
  const calculationInputs = { riskFreeRate, borrowingSpread, cap, frequency, transactionCostBps };
  try {
    let context;
    if (calculationSource === "historical") {
      $("#calculate-portfolio").disabled = true;
      $("#calculate-portfolio").textContent = "공통 거래일 계산 중…";
      const history = await loadHistoricalPortfolioData();
      if (state.portfolioSource !== calculationSource || !history.key.startsWith(`${state.portfolioHistoryIds.join("|")}|`)) return;
      const moments = estimateHistoricalMoments(history.joined.returnsByAsset, { riskFreeRate, annualizationDays: ANNUALIZATION_DAYS });
      if (moments.status !== STATUS.PUBLISHED) throw new Error(moments.reasonCode);
      if (!state.portfolioMatrixEdited.historical) {
        state.portfolioMatrices.historical = moments.correlation.map((row) => [...row]);
        buildCorrelationInputs();
      }
      const result = portfolioKelly({
        expectedExcessReturns: moments.expectedExcessReturns,
        volatilities: moments.volatilities,
        correlation: state.portfolioMatrices.historical.map((row) => [...row]),
        riskFreeRate,
        borrowingSpread,
        cap,
      });
      if (![STATUS.PUBLISHED, STATUS.DEGRADED].includes(result.status)) throw new Error(result.reasonCode);
      const rebalance = rebalanceComparison({
        returnsByAsset: history.joined.returnsByAsset,
        dates: history.joined.dates,
        targetWeights: result.appliedWeights,
        frequency,
        transactionCostBps,
        riskFreeRate,
        borrowingSpread,
        annualizationDays: ANNUALIZATION_DAYS,
      });
      context = { ...result, source: "historical", labels: history.entries.map((entry) => entry.ticker), moments, history, rebalance, calculationInputs, matrix: state.portfolioMatrices.historical.map((row) => [...row]) };
    } else {
      const inputs = directPortfolioInputs();
      const result = portfolioKelly({
        expectedExcessReturns: inputs.expectedExcessReturns,
        volatilities: inputs.volatilities,
        correlation: state.portfolioMatrices.direct.map((row) => [...row]),
        riskFreeRate,
        borrowingSpread,
        cap,
      });
      if (![STATUS.PUBLISHED, STATUS.DEGRADED].includes(result.status)) throw new Error(result.reasonCode);
      context = { ...result, source: "direct", labels: inputs.labels, calculationInputs, matrix: state.portfolioMatrices.direct.map((row) => [...row]) };
    }
    $("#portfolio-error").textContent = "";
    state.portfolioResults[calculationSource] = context;
    if (state.portfolioSource === calculationSource) {
      state.lastPortfolioResult = context;
      renderPortfolioResult(context);
    }
  } catch (error) {
    const message = error.message === "duplicate_assets" ? "서로 다른 자산을 선택하세요." : reasonText(error.message);
    if (state.portfolioSource === calculationSource) $("#portfolio-error").textContent = `${message}: 기존 포트폴리오 결과를 보존했습니다.`;
  } finally {
    $("#calculate-portfolio").disabled = false;
    $("#calculate-portfolio").textContent = "포트폴리오 계산";
  }
}

function renderHistoricalPortfolioDetails(result) {
  const estimates = $("#portfolio-history-estimates");
  const rebalancePanel = $("#portfolio-history-results");
  estimates.hidden = false;
  const estimateSource = $(".comparison-heading span", estimates);
  if (estimateSource) {
    const attribution = sourceAttributionHtml(result.history.sources);
    estimateSource.innerHTML = `공통 거래일 기준 연환산 산술평균 · KRW${attribution ? ` · ${attribution}` : ""}`;
  }
  $("tbody", estimates).innerHTML = result.labels.map((label, index) => `<tr><td>${escapeHtml(label)}</td><td>${result.moments.observations.toLocaleString("ko-KR")}</td><td>${fmtPercent(result.moments.expectedArithmeticReturns[index])}</td><td>${fmtPercent(result.moments.expectedExcessReturns[index])}</td><td>${fmtPercent(result.moments.volatilities[index])}</td></tr>`).join("");
  if (result.rebalance?.status !== STATUS.PUBLISHED) {
    rebalancePanel.hidden = false;
    $("#portfolio-rebalance-chart").hidden = true;
    clearChart($("#portfolio-rebalance-chart"), "재조정 효과 비교", reasonText(result.rebalance?.reasonCode));
    $("#portfolio-rebalance-summary").innerHTML = `<div class="summary-cell"><span>재조정 계산</span><strong>${reasonText(result.rebalance?.reasonCode)}</strong></div>`;
    renderRebalanceDataTable("#portfolio-rebalance-data-table", [], result.rebalance);
    return;
  }
  rebalancePanel.hidden = false;
  $("#portfolio-rebalance-chart").hidden = false;
  $("#portfolio-rebalance-summary").innerHTML = [
    ["총 재조정 효과", fmtPercent(result.rebalance.grossRebalancingEffect)],
    ["거래비용 드래그", fmtPercent(result.rebalance.transactionCostDrag)],
    ["순 재조정 효과", fmtPercent(result.rebalance.netRebalancingEffect)],
    ["누적 회전율", fmtPercent(result.rebalance.turnover)],
  ].map(([label, value]) => `<div class="summary-cell"><span>${label}</span><strong>${value}</strong></div>`).join("");
  renderRebalanceChart($("#portfolio-rebalance-chart"), result.history.joined.dates, result.rebalance);
  renderRebalanceDataTable("#portfolio-rebalance-data-table", result.history.joined.dates, result.rebalance);
}

function renderPortfolioResult(result) {
  if (!result || ![STATUS.PUBLISHED, STATUS.DEGRADED].includes(result.status)) return;
  const labels = result.labels ?? portfolioLabels();
  const matrix = result.matrix ?? state.portfolioMatrices[result.source ?? state.portfolioSource];
  const theoreticalWeights = result.theoreticalWeights ?? labels.map(() => null);
  const cash = 1 - result.totalExposure;
  $("#portfolio-cards").innerHTML = [
    metricCard("적용 총 노출", fmtLeverage(result.totalExposure), "long-only · 상한 3×", true),
    metricCard("적용 장기 기하성장률", fmtPercent(result.annualGrowth), result.status === STATUS.DEGRADED ? "특이 공분산 · 이론값 사용 불가" : `이론 ${fmtPercent(result.theoreticalAnnualGrowth)} · 무제약 ${fmtLeverage(result.theoreticalTotalExposure)}`),
    metricCard("기대 변동성", fmtPercent(result.annualVolatility), "상관행렬 반영"),
    metricCard(cash >= 0 ? "현금 비중" : "차입 비중", fmtPercent(Math.abs(cash)), cash >= 0 ? "무위험자산" : `연 차입비용 ${fmtPercent(result.borrowingCost)}`),
  ].join("");
  $("#portfolio-allocation-table tbody").innerHTML = labels.map((label, index) => `<tr><td>${escapeHtml(label)}</td><td>${fmtPercent(theoreticalWeights[index])}</td><td>${fmtPercent(result.appliedWeights[index])}</td></tr>`).join("");
  const largestIndex = result.appliedWeights.reduce((best, value, index, values) => value > values[best] ? index : best, 0);
  $("#portfolio-insight-title").textContent = `${labels[largestIndex]} ${fmtPercent(result.appliedWeights[largestIndex])} · 총 노출 ${fmtLeverage(result.totalExposure)}`;
  $("#portfolio-insight-detail").textContent = result.source === "historical"
    ? `KRW ${result.history.joined.dates[0]}–${result.history.joined.dates.at(-1)} · 순 재조정 효과 ${result.rebalance?.status === STATUS.PUBLISHED ? fmtPercent(result.rebalance.netRebalancingEffect) : "사용 불가"}`
    : `직접 가정 기준 기대 변동성 ${fmtPercent(result.annualVolatility)} · 적용 성장률 ${fmtPercent(result.annualGrowth)}`;
  renderWeightsChart($("#weights-chart"), labels, theoreticalWeights, result.appliedWeights);
  renderCorrelationHeatmap($("#correlation-chart"), labels, matrix);
  if (result.source === "historical") renderHistoricalPortfolioDetails(result);
  else {
    $("#portfolio-history-estimates").hidden = true;
    $("#portfolio-history-results").hidden = true;
  }
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

async function initializeData() {
  await loadRuntime();
  await loadCatalog();
  if (state.portfolioSource === "historical") await calculatePortfolio();
}

function bootstrap() {
  configureTheme();
  configureGlobalShareButton();
  configureModes();
  applyInitialShareInputs();
  configureHistoricalEvents();
  configureDirectEvents();
  configurePortfolioEvents();
  renderDirect();
  if (state.portfolioSource === "direct") void calculatePortfolio();
  void initializeData();
}

export const testSupport = {
  alignPreviousFx,
  computeHistoricalAnalysis,
  defaultFiveYearCommonRange,
  fiveYearRange,
  flattenWorkerPayload,
  isReusableStaticPayload,
  isShareableHistoricalAssetId,
  historicalKellyEligibility,
  normalizeWorkerBaseUrl,
  noticeForMode,
  parsePriceCsv,
  parseShareState,
  quickPeriodStart,
  qualityMetaHtml,
  removeCorrelationIndex,
  resizeCorrelationMatrix,
  serializeShareState,
  seriesFromPayload,
  sourceAttributionHtml,
};

if (!globalThis.__KELLY_APP_TEST__) bootstrap();
