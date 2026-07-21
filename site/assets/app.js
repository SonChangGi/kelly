import {
  ANNUALIZATION_DAYS,
  MAX_EXPOSURE,
  STATUS,
  REASON,
  applyExplorationRange,
  continuousGrowthRate,
  createPeriodState,
  exactHistoricalKelly,
  isValidDateRange,
  leveragedReturnPath,
  normalizeAssetPayload,
  performanceMetrics,
  portfolioKelly,
  rebalanceComparison,
  rowsToCsv,
  setExplorationRange,
  singleAssetKelly,
  sliceSeries,
  wealthPath,
} from "./engine.js";
import {
  disposeCharts,
  renderCorrelationHeatmap,
  renderDrawdownChart,
  renderGrowthCurve,
  renderRebalanceChart,
  renderWealthChart,
  renderWeightsChart,
} from "./charts.js";

const $ = (selector, root = document) => root.querySelector(selector);
const $$ = (selector, root = document) => [...root.querySelectorAll(selector)];
const percentage = new Intl.NumberFormat("ko-KR", { style: "percent", minimumFractionDigits: 1, maximumFractionDigits: 2 });
const decimal = new Intl.NumberFormat("ko-KR", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
const urlParams = new URLSearchParams(location.search);

const state = {
  catalog: [],
  catalogMeta: null,
  assetEntry: null,
  rawPayload: null,
  series: null,
  period: null,
  currency: urlParams.get("currency") || "native",
  assetCache: new Map(),
  officialResult: null,
  explorationFrame: null,
  portfolioMatrix: [
    [1, 0.1, 0.15],
    [0.1, 1, 0.05],
    [0.15, 0.05, 1],
  ],
  lastPortfolioResult: null,
};

const reasonLabels = {
  [REASON.INSUFFICIENT_OBSERVATIONS]: "관측치 부족",
  [REASON.INVALID_RANGE]: "유효하지 않은 기간",
  [REASON.NON_FINITE_INPUT]: "입력값 오류",
  [REASON.ZERO_VOLATILITY]: "변동성 0",
  [REASON.ZERO_DOWNSIDE_DEVIATION]: "하방편차 0",
  [REASON.ZERO_MAX_DRAWDOWN]: "MDD 0",
  [REASON.SINGULAR_COVARIANCE]: "특이 공분산",
  [REASON.INVALID_CORRELATION]: "상관행렬 오류",
  [REASON.NON_PSD_CORRELATION]: "비양의 준정부호",
  [REASON.NO_COMMON_RETURNS]: "공통 수익률 부족",
  [REASON.FX_GAP_EXCEEDED]: "FX 5일 공백 초과",
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

function reasonText(reasonCode) {
  return reasonLabels[reasonCode] || reasonCode || "사용 불가";
}

function showNotice(message, tone = "info") {
  const notice = $("#global-notice");
  notice.hidden = !message;
  notice.textContent = message || "";
  notice.dataset.tone = tone;
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

function updateThemeLabel() {
  const dark = document.documentElement.dataset.theme === "dark";
  $("#theme-toggle").setAttribute("aria-label", dark ? "라이트 모드로 전환" : "다크 모드로 전환");
}

function configureModes() {
  const requested = urlParams.get("mode");
  const initial = ["historical", "direct", "portfolio"].includes(requested) ? requested : "historical";
  $$(".mode-tab").forEach((button) => button.addEventListener("click", () => activateMode(button.dataset.mode)));
  activateMode(initial, false);
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
  if (focus) $(`.mode-tab[data-mode="${mode}"]`)?.focus();
  requestAnimationFrame(renderActiveCharts);
}

function renderActiveCharts() {
  const mode = $(".mode-tab.is-active")?.dataset.mode;
  if (mode === "historical" && state.series && state.period) renderHistorical({ includeComparison: false });
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

async function loadCatalog() {
  try {
    const response = await fetch("./data/catalog.json", { cache: "no-cache" });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    const payload = await response.json();
    state.catalogMeta = payload;
    state.catalog = catalogItems(payload);
    if (!state.catalog.length) throw new Error("empty catalog");
    populateAssetSelect();
    const desired = urlParams.get("asset");
    const initial = state.catalog.find((asset) => asset.id === desired || asset.ticker === desired)
      ?? state.catalog.find((asset) => asset.ticker === "SPY")
      ?? state.catalog.find((asset) => [STATUS.PUBLISHED, STATUS.LIVE_API].includes(asset.status))
      ?? state.catalog[0];
    $("#asset-select").value = initial.id;
    await loadSelectedAsset();
  } catch (error) {
    $("#asset-select").innerHTML = '<option value="">정적 데이터 이용 불가</option>';
    showNotice(`과거 데이터 계약을 불러오지 못했습니다. 직접 가정 모드는 계속 사용할 수 있습니다. (${error.message})`, "error");
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

async function fetchAssetPayload(entry) {
  if (!entry) throw new Error("asset not found");
  if (state.assetCache.has(entry.id)) return state.assetCache.get(entry.id);
  const promise = fetch(assetPath(entry), { cache: "no-cache" }).then(async (response) => {
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return response.json();
  });
  state.assetCache.set(entry.id, promise);
  try { return await promise; } catch (error) { state.assetCache.delete(entry.id); throw error; }
}

function seriesFromPayload(payload, requestedCurrency) {
  const native = normalizeAssetPayload(payload);
  if (requestedCurrency === "native" || native.currency === "KRW") return native;
  const krwBlock = payload.series?.krw ?? payload.krw;
  if (krwBlock) return normalizeAssetPayload({ ...payload, ...krwBlock, columns: krwBlock.columns ?? krwBlock, currency: "KRW" });
  const columns = payload.columns ?? payload.data ?? {};
  const krwReturns = columns.returnKrw ?? columns.return_krw ?? columns.krwReturn ?? columns.krw_returns;
  const krwPrices = columns.priceKrw ?? columns.price_krw ?? columns.krwPrice;
  if (krwReturns) {
    return normalizeAssetPayload({ ...payload, currency: "KRW", columns: { date: columns.date ?? columns.dates, return: krwReturns, price: krwPrices ?? [] } });
  }
  const fx = columns.fx ?? columns.usdKrw ?? columns.usd_krw ?? payload.fx;
  if (fx?.length === native.dates.length && native.prices.length === native.dates.length) {
    const prices = native.prices.map((price, index) => price * Number(fx[index]));
    return normalizeAssetPayload({ ...payload, currency: "KRW", columns: { date: native.dates, price: prices } });
  }
  throw new Error(REASON.FX_GAP_EXCEEDED);
}

async function loadSelectedAsset() {
  const id = $("#asset-select").value;
  const entry = state.catalog.find((asset) => asset.id === id);
  state.assetEntry = entry;
  renderAssetMeta(entry);
  try {
    const payload = await fetchAssetPayload(entry);
    state.rawPayload = payload;
    const series = seriesFromPayload(payload, state.currency);
    if (series.status && ![STATUS.PUBLISHED, STATUS.LIVE_API, STATUS.STALE, STATUS.DEGRADED].includes(series.status)) throw new Error(series.status);
    if (series.returns.length < 2 || series.dates.length < 3) throw new Error(REASON.INSUFFICIENT_OBSERVATIONS);
    state.series = series;
    renderAssetMeta({ ...entry, returnBasis: series.returnBasis ?? entry.returnBasis, currency: series.currency ?? entry.currency });
    initializePeriod(series);
    showNotice(statusMessage(entry, payload), [STATUS.STALE, STATUS.DEGRADED].includes(entry.status) ? "error" : "success");
    renderHistorical();
  } catch (error) {
    state.series = null;
    state.period = null;
    showNotice(`${entry?.ticker ?? "선택 자산"}의 검증된 공개 이력이 없습니다. 직접 가정 모드는 계속 사용할 수 있습니다. (${reasonText(error.message)})`, "error");
    renderUnavailableHistorical(error.message || REASON.DATA_UNAVAILABLE);
  }
}

function statusMessage(entry, payload) {
  const asOf = payload.asOf ?? payload.dataAsOf ?? entry.asOf ?? entry.dataAsOf;
  const basis = returnBasisLabel(entry.returnBasis ?? payload.returnBasis);
  return `${entry.ticker} · ${basis}${asOf ? ` · 기준일 ${asOf}` : ""} · 상태 ${entry.status}`;
}

function renderAssetMeta(entry) {
  if (!entry) return;
  $("#asset-meta").innerHTML = [
    `<span class="badge">${escapeHtml(entry.type)}</span>`,
    `<span class="badge">${escapeHtml(entry.currency)}</span>`,
    `<span class="badge">${escapeHtml(returnBasisLabel(entry.returnBasis))}</span>`,
  ].join("");
}

function returnBasisLabel(value) {
  const basis = String(value || "").toLowerCase();
  if (basis.includes("total") || basis.includes("adjust")) return "조정 총수익 근사";
  if (basis.includes("price")) return "가격수익률";
  if (basis.includes("fx")) return "환율 변동률";
  return value || "수익률 기준 미확인";
}

function initializePeriod(series) {
  const end = series.dates.at(-1);
  const endDate = new Date(`${end}T00:00:00Z`);
  const target = new Date(endDate);
  target.setUTCFullYear(target.getUTCFullYear() - 5);
  const targetIso = target.toISOString().slice(0, 10);
  const defaultStart = series.dates.find((date) => date >= targetIso) ?? series.dates[0];
  const queryStart = urlParams.get("start");
  const queryEnd = urlParams.get("end");
  const start = isValidDateRange(queryStart, queryEnd, series.dates[0], end) ? queryStart : defaultStart;
  const finish = isValidDateRange(queryStart, queryEnd, series.dates[0], end) ? queryEnd : end;
  state.period = createPeriodState(start, finish);
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
  $("#explore-start-slider").value = String(startIndex);
  $("#explore-end-slider").value = String(endIndex);
}

function historicalInputs() {
  return {
    riskFreeRate: pctInput("#risk-free"),
    borrowingSpread: pctInput("#borrow-spread"),
    transactionCostBps: numberInput("#transaction-cost", 10),
    annualizationDays: numberInput("#annualization-days", ANNUALIZATION_DAYS),
    mar: pctInput("#sortino-mar"),
    frequency: $("#rebalance-frequency").value,
  };
}

function renderHistorical({ includeComparison = true } = {}) {
  if (!state.series || !state.period) return;
  const official = sliceSeries(state.series, state.period.official.start, state.period.official.end);
  const inputs = historicalInputs();
  const metrics = performanceMetrics(official.returns, official.dates, {
    annualizationDays: inputs.annualizationDays,
    riskFreeRate: inputs.riskFreeRate,
    mar: inputs.mar,
    minObservations: 60,
  });
  if (metrics.status !== STATUS.PUBLISHED) {
    renderUnavailableHistorical(metrics.reasonCode);
    return;
  }
  const kelly = singleAssetKelly({
    expectedExcessReturn: metrics.annualArithmeticReturn.value - inputs.riskFreeRate,
    volatility: metrics.annualVolatility.value,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
  });
  const exact = exactHistoricalKelly(official.returns, { riskFreeRate: inputs.riskFreeRate, annualizationDays: inputs.annualizationDays, minObservations: 60 });
  const rebalance = kelly.status === STATUS.PUBLISHED ? rebalanceComparison({
    returnsByAsset: [official.returns],
    dates: official.returnDates ?? official.dates.slice(1),
    targetWeights: [kelly.appliedFullKelly],
    frequency: inputs.frequency,
    transactionCostBps: inputs.transactionCostBps,
    riskFreeRate: inputs.riskFreeRate,
    borrowingSpread: inputs.borrowingSpread,
  }) : null;
  state.officialResult = { official, inputs, metrics, kelly, exact, rebalance };

  $("#result-title").textContent = `${state.assetEntry?.ticker ?? state.series.symbol ?? "자산"} 분석 결과`;
  $("#official-period-label").textContent = `${state.period.official.start} – ${state.period.official.end} · ${metrics.observations.toLocaleString("ko-KR")}개 일간수익률`;
  renderHistoricalKellyCards(kelly, exact);
  renderMetricCards(metrics);
  renderPresets($("#historical-presets"), kelly);
  renderHistoricalCharts(official, metrics, kelly, rebalance);
  updateExplorationSummary(false);
  if (includeComparison) void renderLeverageComparison(official, metrics, kelly);
}

function renderHistoricalKellyCards(kelly, exact) {
  const cards = $("#historical-kelly-cards");
  if (kelly.status !== STATUS.PUBLISHED) {
    cards.innerHTML = metricCard("Kelly 계산 불가", "—", reasonText(kelly.reasonCode), true);
    return;
  }
  cards.innerHTML = [
    metricCard("Full Kelly 적용 비중", fmtLeverage(kelly.appliedFullKelly), kelly.capApplied ? `원값 ${fmtLeverage(kelly.optimalWithBorrowing)} · 3× 상한` : "long-only · Full", true),
    metricCard("Full Kelly 장기 기하성장률", fmtPercent(kelly.maximumAnnualGrowth), `로그성장률 ${fmtPercent(kelly.maximumLogGrowth)}`),
    metricCard("절대 2배 장기 기하성장률", fmtPercent(kelly.twiceAnnualGrowth), `기대 산술 자산수익률 ${fmtPercent(kelly.twiceArithmeticWealthReturn)}`),
    metricCard("Exact Kelly", exact.status === STATUS.PUBLISHED ? fmtLeverage(exact.appliedLeverage) : "—", exact.status === STATUS.PUBLISHED ? "in-sample · 일간 재조정" : reasonText(exact.reasonCode)),
  ].join("");
}

function metricCard(label, value, note, primary = false) {
  return `<article class="metric-card${primary ? " primary" : ""}"><span>${escapeHtml(label)}</span><strong>${escapeHtml(value)}</strong><small>${escapeHtml(note)}</small></article>`;
}

function renderMetricCards(metrics) {
  const definitions = [
    ["누적수익률", metrics.cumulativeReturn, "선택기간"],
    ["연환산 산술평균", metrics.annualArithmeticReturn, "일간 평균 × 252"],
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
  renderDrawdownChart($("#drawdown-chart"), official.dates, metrics.drawdowns);
  const curve = growthCurve(kelly, state.officialResult.inputs);
  renderGrowthCurve($("#growth-chart"), curve.points, curve.markers);
  if (rebalance?.status === STATUS.PUBLISHED) {
    renderRebalanceChart($("#rebalance-chart"), official.dates, rebalance);
    renderRebalanceSummary(rebalance);
  } else {
    $("#rebalance-summary").innerHTML = `<div class="summary-cell"><span>재조정 계산</span><strong>${reasonText(rebalance?.reasonCode)}</strong></div>`;
  }
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
  state.period = setExplorationRange(state.period, start, end, { minimum: state.series.dates[0], maximum: state.series.dates.at(-1) });
  if (state.period.error) return;
  syncPeriodControls();
  updateExplorationSummary();
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
      renderWealthChart($("#wealth-chart"), {
        dates: state.series.dates, officialWealth, explorationWealth,
        explorationStart: state.period.exploration.start, explorationEnd: state.period.exploration.end,
        returnBasis: returnBasisLabel(state.assetEntry?.returnBasis ?? state.series.returnBasis),
      }, onChartExplore);
    });
  }
}

async function renderLeverageComparison(official, metrics, kelly) {
  const tbody = $("#leverage-comparison-table tbody");
  const synthetic = leveragedReturnPath(official.returns, 2, state.officialResult.inputs.riskFreeRate, state.officialResult.inputs.annualizationDays);
  let syntheticCumulative = null;
  let syntheticCagr = null;
  if (synthetic.status === STATUS.PUBLISHED) {
    const syntheticMetrics = performanceMetrics(synthetic.returns, official.dates, { minObservations: 2, riskFreeRate: state.officialResult.inputs.riskFreeRate });
    syntheticCumulative = syntheticMetrics.cumulativeReturn?.value;
    syntheticCagr = syntheticMetrics.cagr?.value;
  }
  const rows = [{
    path: "합성 고정 2배", product: `${state.assetEntry?.ticker ?? "기초자산"} × 2`, arithmetic: kelly.twiceArithmeticWealthReturn,
    cumulative: syntheticCumulative, cagr: syntheticCagr, status: synthetic.status,
  }];

  const mapping = state.assetEntry?.leveragedProducts?.long2x ?? state.assetEntry?.leveraged_products?.long2x;
  const mappedId = typeof mapping === "string" ? mapping : mapping?.id ?? mapping?.ticker;
  const actualEntry = state.catalog.find((asset) => asset.id === mappedId || asset.ticker === mappedId);
  if (!actualEntry) {
    rows.push({ path: "실제 일간목표 2배 ETF", product: mappedId || "매핑 없음", status: STATUS.UNAVAILABLE, reason: "공개 카탈로그 이력 없음" });
  } else {
    try {
      const actualPayload = await fetchAssetPayload(actualEntry);
      const actualSeries = seriesFromPayload(actualPayload, state.currency);
      const actualSlice = sliceSeries(actualSeries, state.period.official.start, state.period.official.end);
      const actualMetrics = performanceMetrics(actualSlice.returns, actualSlice.dates, { minObservations: 60, riskFreeRate: state.officialResult.inputs.riskFreeRate });
      rows.push({
        path: "실제 일간목표 2배 ETF", product: actualEntry.ticker, arithmetic: actualMetrics.annualArithmeticReturn?.value,
        cumulative: actualMetrics.cumulativeReturn?.value, cagr: actualMetrics.cagr?.value, status: actualMetrics.status,
        reason: actualMetrics.reasonCode,
      });
    } catch (error) {
      rows.push({ path: "실제 일간목표 2배 ETF", product: actualEntry.ticker, status: STATUS.UNAVAILABLE, reason: reasonText(error.message) });
    }
  }
  tbody.innerHTML = rows.map((row) => `<tr><td>${escapeHtml(row.path)}</td><td>${escapeHtml(row.product)}</td><td>${fmtPercent(row.arithmetic)}</td><td>${row.status === STATUS.RUIN ? "파산" : fmtPercent(row.cumulative)}</td><td>${row.status === STATUS.RUIN ? "파산" : fmtPercent(row.cagr)}</td><td>${row.status === STATUS.PUBLISHED ? "사용 가능" : row.status === STATUS.RUIN ? "ruin · 자산배수≤0" : `unavailable · ${escapeHtml(row.reason || "이력 없음")}`}</td></tr>`).join("");
}

function renderUnavailableHistorical(reasonCode) {
  $("#historical-kelly-cards").innerHTML = metricCard("공식 결과 사용 불가", "—", reasonText(reasonCode), true);
  $("#performance-cards").innerHTML = "";
  $("#historical-presets").innerHTML = "";
  $("#official-period-label").textContent = `기존 공식 결과는 변경하지 않았습니다 · ${reasonText(reasonCode)}`;
  $("#leverage-comparison-table tbody").innerHTML = `<tr><td colspan="6">unavailable · ${escapeHtml(reasonText(reasonCode))}</td></tr>`;
}

function applyOfficialDates(start, end) {
  if (!state.series || !state.period) return;
  const bounds = { minimum: state.series.dates[0], maximum: state.series.dates.at(-1) };
  const next = setExplorationRange(state.period, start, end, bounds);
  if (next.error) {
    showNotice("기간이 잘못되어 기존 공식 결과를 보존했습니다. 시작일은 끝일보다 앞서야 하며 가용기간 안이어야 합니다.", "error");
    syncPeriodControls();
    return;
  }
  const applied = applyExplorationRange(next, bounds);
  const candidate = sliceSeries(state.series, applied.official.start, applied.official.end);
  if (candidate.returns.length < 60) {
    showNotice("공식 분석에는 최소 60개 공통 일간수익률이 필요합니다. 기존 결과를 보존했습니다.", "error");
    syncPeriodControls();
    return;
  }
  state.period = applied;
  syncPeriodControls();
  showNotice("탐색기간을 공식 분석기간에 적용했습니다.", "success");
  renderHistorical();
}

function configureHistoricalEvents() {
  $("#asset-select").addEventListener("change", loadSelectedAsset);
  $$("[data-currency]").forEach((button) => button.addEventListener("click", async () => {
    const previous = state.currency;
    const requested = button.dataset.currency;
    if (requested === previous) return;
    try {
      state.currency = requested;
      state.series = seriesFromPayload(state.rawPayload, requested);
      $$("[data-currency]").forEach((item) => { item.classList.toggle("is-active", item === button); item.setAttribute("aria-pressed", String(item === button)); });
      initializePeriod(state.series);
      renderHistorical();
    } catch (error) {
      state.currency = previous;
      showNotice(`KRW 환산을 적용하지 않았습니다. 선행값 없는 FX 또는 5일 초과 공백을 보간하지 않습니다. (${reasonText(error.message)})`, "error");
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

async function shareCurrentUrl() {
  const params = new URLSearchParams();
  params.set("mode", $(".mode-tab.is-active").dataset.mode);
  if (state.assetEntry) params.set("asset", state.assetEntry.id);
  if (state.period) { params.set("start", state.period.official.start); params.set("end", state.period.official.end); }
  params.set("currency", state.currency);
  params.set("rf", $("#risk-free").value);
  const url = `${location.origin}${location.pathname}?${params}`;
  history.replaceState(null, "", url);
  try { await navigator.clipboard.writeText(url); showNotice("현재 공식 분석 설정 URL을 복사했습니다.", "success"); }
  catch { showNotice("주소 표시줄에 현재 설정 URL을 반영했습니다.", "success"); }
}

function exportCurrentCsv() {
  const result = state.officialResult;
  if (!result) { showNotice("내보낼 공식 분석 결과가 없습니다.", "error"); return; }
  const metricRows = [
    ["metric", "cumulative_return", result.metrics.cumulativeReturn.value],
    ["metric", "annual_arithmetic_return", result.metrics.annualArithmeticReturn.value],
    ["metric", "cagr", result.metrics.cagr.value],
    ["metric", "annual_volatility", result.metrics.annualVolatility.value],
    ["metric", "max_drawdown", result.metrics.maxDrawdown.value],
    ["metric", "sharpe", result.metrics.sharpe.value],
    ["metric", "sortino", result.metrics.sortino.value],
    ["metric", "calmar_style", result.metrics.calmar.value],
    ["kelly", "full_applied", result.kelly.appliedFullKelly],
    ["kelly", "full_geometric_growth", result.kelly.maximumAnnualGrowth],
    ["kelly", "twice_arithmetic_wealth_return", result.kelly.twiceArithmeticWealthReturn],
    ["kelly", "twice_geometric_growth", result.kelly.twiceAnnualGrowth],
  ];
  const seriesRows = result.official.returnDates.map((date, index) => ["daily_return", date, result.official.returns[index]]);
  const csv = rowsToCsv(["section", "name_or_date", "value"], [...metricRows, ...seriesRows]);
  const blob = new Blob([`\ufeff${csv}`], { type: "text/csv;charset=utf-8" });
  const link = document.createElement("a");
  link.href = URL.createObjectURL(blob);
  link.download = `kelly-${state.assetEntry?.ticker ?? "result"}-${state.period.official.end}.csv`;
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
    cards.innerHTML = metricCard("Kelly 계산 불가", "—", reasonText(kelly.reasonCode), true);
    $("#direct-presets").innerHTML = "";
    return;
  }
  cards.innerHTML = [
    metricCard("Full Kelly 적용 비중", fmtLeverage(kelly.appliedFullKelly), kelly.capApplied ? `원값 ${fmtLeverage(kelly.optimalWithBorrowing)}` : "long-only · 최대 3×", true),
    metricCard("Full Kelly 장기 기하성장률", fmtPercent(kelly.maximumAnnualGrowth), `로그성장률 ${fmtPercent(kelly.maximumLogGrowth)}`),
    metricCard("2배 기대 산술 자산수익률", fmtPercent(kelly.twiceArithmeticWealthReturn), "변동성 드래그 차감 전"),
    metricCard("2배 장기 기하성장률", fmtPercent(kelly.twiceAnnualGrowth), `로그성장률 ${fmtPercent(kelly.twiceLogGrowth)}`),
  ].join("");
  renderPresets($("#direct-presets"), kelly);
  const points = Array.from({ length: 121 }, (_, index) => {
    const leverage = index / 40;
    return [leverage, continuousGrowthRate({ leverage, ...inputs })];
  });
  renderGrowthCurve($("#direct-growth-chart"), points, [
    { name: "Full", x: kelly.appliedFullKelly, y: kelly.appliedLogGrowth },
    { name: "2×", x: 2, y: kelly.twiceLogGrowth, color: "#c58b24" },
  ]);
}

function portfolioLabels() {
  return $$("#portfolio-assets-table tbody tr").map((row, index) => $("td:first-child input", row).value.trim() || `자산 ${index + 1}`);
}

function buildCorrelationInputs() {
  const labels = portfolioLabels();
  const table = $("#correlation-input");
  table.innerHTML = `<thead><tr><th></th>${labels.map((label) => `<th>${escapeHtml(label)}</th>`).join("")}</tr></thead><tbody>${labels.map((label, i) => `<tr><th>${escapeHtml(label)}</th>${labels.map((_, j) => `<td><input type="number" min="-1" max="1" step="0.01" data-corr-row="${i}" data-corr-col="${j}" value="${state.portfolioMatrix[i][j]}" aria-label="${escapeHtml(label)}와 ${escapeHtml(labels[j])} 상관계수" ${i === j || i > j ? "readonly" : ""}></td>`).join("")}</tr>`).join("")}</tbody>`;
  $$('[data-corr-row]', table).forEach((input) => input.addEventListener("input", () => {
    const row = Number(input.dataset.corrRow);
    const col = Number(input.dataset.corrCol);
    const value = Number(input.value);
    state.portfolioMatrix[row][col] = value;
    state.portfolioMatrix[col][row] = value;
    const mirror = $(`[data-corr-row="${col}"][data-corr-col="${row}"]`, table);
    if (mirror) mirror.value = input.value;
  }));
}

function configurePortfolioEvents() {
  buildCorrelationInputs();
  $$("#portfolio-assets-table tbody td:first-child input").forEach((input) => input.addEventListener("change", buildCorrelationInputs));
  $("#calculate-portfolio").addEventListener("click", calculatePortfolio);
}

function calculatePortfolio() {
  const excess = $$('[data-p-excess]').map((input) => Number(input.value) / 100);
  const vol = $$('[data-p-vol]').map((input) => Number(input.value) / 100);
  const result = portfolioKelly({
    expectedExcessReturns: excess,
    volatilities: vol,
    correlation: state.portfolioMatrix.map((row) => [...row]),
    riskFreeRate: pctInput("#portfolio-rf"),
    cap: Math.min(MAX_EXPOSURE, numberInput("#portfolio-cap", MAX_EXPOSURE)),
  });
  if (result.status !== STATUS.PUBLISHED) {
    $("#portfolio-error").textContent = `${reasonText(result.reasonCode)}: 기존 포트폴리오 결과를 보존했습니다.`;
    return;
  }
  $("#portfolio-error").textContent = "";
  state.lastPortfolioResult = result;
  renderPortfolioResult(result);
}

function renderPortfolioResult(result) {
  if (!result || result.status !== STATUS.PUBLISHED) return;
  const cash = 1 - result.totalExposure;
  $("#portfolio-cards").innerHTML = [
    metricCard("적용 총 노출", fmtLeverage(result.totalExposure), "long-only · 상한 3×", true),
    metricCard("장기 기하성장률", fmtPercent(result.annualGrowth), `로그성장률 ${fmtPercent(result.logGrowth)}`),
    metricCard("기대 변동성", fmtPercent(result.annualVolatility), "상관행렬 반영"),
    metricCard(cash >= 0 ? "현금 비중" : "차입 비중", fmtPercent(Math.abs(cash)), cash >= 0 ? "무위험자산" : "총 노출 1× 초과"),
  ].join("");
  const labels = portfolioLabels();
  renderWeightsChart($("#weights-chart"), labels, result.theoreticalWeights, result.appliedWeights);
  renderCorrelationHeatmap($("#correlation-chart"), labels, state.portfolioMatrix);
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>'"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", "'": "&#39;", '"': "&quot;" }[character]));
}

configureTheme();
configureModes();
configureHistoricalEvents();
configureDirectEvents();
configurePortfolioEvents();
renderDirect();
calculatePortfolio();
void loadCatalog();
