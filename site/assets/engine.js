export const ANNUALIZATION_DAYS = 252;
export const CAGR_YEAR_DAYS = 365.2425;
export const MAX_EXPOSURE = 3;

export const STATUS = Object.freeze({
  PUBLISHED: "published",
  LIVE_API: "live_api",
  STALE: "stale",
  DEGRADED: "degraded",
  UNAVAILABLE: "unavailable",
  RUIN: "ruin",
});

export const REASON = Object.freeze({
  INSUFFICIENT_OBSERVATIONS: "insufficient_observations",
  INVALID_RANGE: "invalid_range",
  NON_FINITE_INPUT: "non_finite_input",
  ZERO_VOLATILITY: "zero_volatility",
  ZERO_DOWNSIDE_DEVIATION: "zero_downside_deviation",
  ZERO_MAX_DRAWDOWN: "zero_max_drawdown",
  NON_POSITIVE_WEALTH: "non_positive_wealth",
  SINGULAR_COVARIANCE: "singular_covariance",
  INVALID_CORRELATION: "invalid_correlation",
  NON_PSD_CORRELATION: "non_psd_correlation",
  NO_COMMON_RETURNS: "no_common_returns",
  FX_GAP_EXCEEDED: "fx_gap_exceeded",
  DATA_UNAVAILABLE: "data_unavailable",
  RUIN: "ruin",
});

const EPS = 1e-12;

export function clamp(value, minimum, maximum) {
  return Math.min(maximum, Math.max(minimum, value));
}

export function mean(values) {
  return values.reduce((total, value) => total + value, 0) / values.length;
}

export function sampleVariance(values) {
  if (values.length < 2) return null;
  const average = mean(values);
  return values.reduce((total, value) => total + (value - average) ** 2, 0) / (values.length - 1);
}

export function annualRateToDaily(rate, annualizationDays = ANNUALIZATION_DAYS) {
  if (!Number.isFinite(rate) || rate <= -1 || annualizationDays <= 0) return null;
  return (1 + rate) ** (1 / annualizationDays) - 1;
}

export function wealthPath(returns, initial = 1) {
  const wealth = [initial];
  let current = initial;
  for (const value of returns) {
    if (!Number.isFinite(value)) return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT, wealth };
    current *= 1 + value;
    wealth.push(current);
    if (current <= 0) return { status: STATUS.RUIN, reasonCode: REASON.RUIN, wealth };
  }
  return { status: STATUS.PUBLISHED, reasonCode: null, wealth };
}

export function drawdownPath(wealth) {
  let peak = wealth[0];
  return wealth.map((value) => {
    peak = Math.max(peak, value);
    return peak > 0 ? value / peak - 1 : null;
  });
}

function resultValue(value, reasonCode = null) {
  return { value: Number.isFinite(value) ? value : null, reasonCode };
}

export function performanceMetrics(returns, dates = [], options = {}) {
  const annualizationDays = options.annualizationDays ?? ANNUALIZATION_DAYS;
  const cagrYearDays = options.cagrYearDays ?? CAGR_YEAR_DAYS;
  const riskFreeRate = options.riskFreeRate ?? 0;
  const mar = options.mar ?? riskFreeRate;
  const minObservations = options.minObservations ?? 2;
  const values = returns.map(Number);

  if (values.length < minObservations) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.INSUFFICIENT_OBSERVATIONS, observations: values.length };
  }
  if (values.some((value) => !Number.isFinite(value))) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT, observations: values.length };
  }

  const path = wealthPath(values);
  if (path.status === STATUS.RUIN) {
    return { status: STATUS.RUIN, reasonCode: path.reasonCode, observations: values.length, wealth: path.wealth };
  }

  const wealth = path.wealth;
  const drawdowns = drawdownPath(wealth);
  const cumulativeReturn = wealth.at(-1) - 1;
  const annualArithmeticReturn = mean(values) * annualizationDays;
  const variance = sampleVariance(values);
  const annualVolatility = Math.sqrt(Math.max(0, variance)) * Math.sqrt(annualizationDays);
  const maxDrawdown = Math.min(...drawdowns);

  let elapsedDays = values.length * (cagrYearDays / annualizationDays);
  if ((dates.length === values.length || dates.length === values.length + 1) && dates.length > 1) {
    const first = Date.parse(dates[0]);
    const last = Date.parse(dates.at(-1));
    if (Number.isFinite(first) && Number.isFinite(last) && last > first) elapsedDays = (last - first) / 86400000;
  }
  const years = elapsedDays / cagrYearDays;
  const cagr = years > 0 ? wealth.at(-1) ** (1 / years) - 1 : null;

  const dailyRiskFree = annualRateToDaily(riskFreeRate, annualizationDays);
  const dailyMar = annualRateToDaily(mar, annualizationDays);
  const excess = values.map((value) => value - dailyRiskFree);
  const excessVariance = sampleVariance(excess);
  const excessStd = Math.sqrt(Math.max(0, excessVariance));
  const sharpe = excessStd > EPS ? (mean(excess) / excessStd) * Math.sqrt(annualizationDays) : null;
  const downsideDeviation = Math.sqrt(mean(values.map((value) => Math.min(0, value - dailyMar) ** 2)));
  const sortino = downsideDeviation > EPS
    ? ((mean(values) - dailyMar) / downsideDeviation) * Math.sqrt(annualizationDays)
    : null;
  const calmar = maxDrawdown < -EPS && Number.isFinite(cagr) ? cagr / Math.abs(maxDrawdown) : null;

  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    observations: values.length,
    elapsedDays,
    wealth,
    drawdowns,
    cumulativeReturn: resultValue(cumulativeReturn),
    annualArithmeticReturn: resultValue(annualArithmeticReturn),
    cagr: resultValue(cagr, cagr === null ? REASON.INVALID_RANGE : null),
    annualVolatility: resultValue(annualVolatility, annualVolatility <= EPS ? REASON.ZERO_VOLATILITY : null),
    maxDrawdown: resultValue(Math.abs(maxDrawdown)),
    sharpe: resultValue(sharpe, sharpe === null ? REASON.ZERO_VOLATILITY : null),
    sortino: resultValue(sortino, sortino === null ? REASON.ZERO_DOWNSIDE_DEVIATION : null),
    calmar: resultValue(calmar, calmar === null ? REASON.ZERO_MAX_DRAWDOWN : null),
  };
}

export function continuousGrowthRate({ leverage, expectedExcessReturn, volatility, riskFreeRate = 0, borrowingSpread = 0 }) {
  if (![leverage, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread].every(Number.isFinite)) return null;
  const borrowingDrag = Math.max(0, leverage - 1) * Math.max(0, borrowingSpread);
  return riskFreeRate + leverage * expectedExcessReturn - 0.5 * leverage ** 2 * volatility ** 2 - borrowingDrag;
}

export function expectedArithmeticWealthReturn({ leverage, expectedExcessReturn, riskFreeRate = 0, borrowingSpread = 0 }) {
  if (![leverage, expectedExcessReturn, riskFreeRate, borrowingSpread].every(Number.isFinite)) return null;
  return Math.exp(riskFreeRate + leverage * expectedExcessReturn - Math.max(0, leverage - 1) * Math.max(0, borrowingSpread)) - 1;
}

export function singleAssetKelly(inputs) {
  const expectedExcessReturn = Number(inputs.expectedExcessReturn);
  const volatility = Number(inputs.volatility);
  const riskFreeRate = Number(inputs.riskFreeRate ?? 0);
  const borrowingSpread = Number(inputs.borrowingSpread ?? 0);
  const cap = Number(inputs.cap ?? MAX_EXPOSURE);
  if (![expectedExcessReturn, volatility, riskFreeRate, borrowingSpread, cap].every(Number.isFinite)) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT };
  }
  if (volatility <= EPS) return { status: STATUS.UNAVAILABLE, reasonCode: REASON.ZERO_VOLATILITY };

  const variance = volatility ** 2;
  const theoreticalFullKelly = expectedExcessReturn / variance;
  const belowOne = clamp(theoreticalFullKelly, 0, 1);
  const aboveOne = Math.max(1, (expectedExcessReturn - Math.max(0, borrowingSpread)) / variance);
  const candidates = [0, belowOne, aboveOne];
  let optimalWithBorrowing = candidates[0];
  for (const candidate of candidates.slice(1)) {
    if (continuousGrowthRate({ leverage: candidate, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread }) >
        continuousGrowthRate({ leverage: optimalWithBorrowing, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread })) {
      optimalWithBorrowing = candidate;
    }
  }

  const fullKelly = clamp(optimalWithBorrowing, 0, cap);
  const presets = [0.25, 0.5, 1].map((fraction) => {
    const leverage = clamp(optimalWithBorrowing * fraction, 0, cap);
    const logGrowth = continuousGrowthRate({ leverage, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread });
    return { fraction, leverage, logGrowth, annualGrowth: Math.exp(logGrowth) - 1 };
  });
  const rawMaximumLogGrowth = continuousGrowthRate({
    leverage: optimalWithBorrowing, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread,
  });
  const appliedLogGrowth = continuousGrowthRate({ leverage: fullKelly, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread });
  const twiceLogGrowth = continuousGrowthRate({ leverage: 2, expectedExcessReturn, volatility, riskFreeRate, borrowingSpread });

  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    theoreticalFullKelly,
    optimalWithBorrowing,
    appliedFullKelly: fullKelly,
    capApplied: optimalWithBorrowing > cap,
    maximumLogGrowth: rawMaximumLogGrowth,
    maximumAnnualGrowth: Math.exp(rawMaximumLogGrowth) - 1,
    appliedLogGrowth,
    appliedAnnualGrowth: Math.exp(appliedLogGrowth) - 1,
    twiceLogGrowth,
    twiceAnnualGrowth: Math.exp(twiceLogGrowth) - 1,
    twiceArithmeticWealthReturn: expectedArithmeticWealthReturn({ leverage: 2, expectedExcessReturn, riskFreeRate, borrowingSpread }),
    presets,
  };
}

function safeLogGrowth(returns, leverage, dailyRiskFree) {
  let total = 0;
  for (const value of returns) {
    const multiplier = 1 + dailyRiskFree + leverage * (value - dailyRiskFree);
    if (multiplier <= 0) return Number.NEGATIVE_INFINITY;
    total += Math.log(multiplier);
  }
  return total / returns.length;
}

export function exactHistoricalKelly(returns, options = {}) {
  const values = returns.map(Number);
  const minObservations = options.minObservations ?? 2;
  if (values.length < minObservations) return { status: STATUS.UNAVAILABLE, reasonCode: REASON.INSUFFICIENT_OBSERVATIONS };
  if (values.some((value) => !Number.isFinite(value))) return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT };
  const annualizationDays = options.annualizationDays ?? ANNUALIZATION_DAYS;
  const dailyRiskFree = annualRateToDaily(options.riskFreeRate ?? 0, annualizationDays);
  const searchCap = options.searchCap ?? 20;
  const appliedCap = options.cap ?? MAX_EXPOSURE;
  let upper = searchCap;
  for (const value of values) {
    const differential = value - dailyRiskFree;
    if (differential < 0) upper = Math.min(upper, (1 + dailyRiskFree) / -differential - 1e-10);
  }
  upper = Math.max(0, upper);
  let left = 0;
  let right = upper;
  const phi = (Math.sqrt(5) - 1) / 2;
  for (let i = 0; i < 120 && right - left > 1e-10; i += 1) {
    const x1 = right - phi * (right - left);
    const x2 = left + phi * (right - left);
    if (safeLogGrowth(values, x1, dailyRiskFree) < safeLogGrowth(values, x2, dailyRiskFree)) left = x1;
    else right = x2;
  }
  const theoreticalLeverage = (left + right) / 2;
  const appliedLeverage = Math.min(theoreticalLeverage, appliedCap);
  const dailyLogGrowth = safeLogGrowth(values, theoreticalLeverage, dailyRiskFree);
  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    theoreticalLeverage,
    appliedLeverage,
    capApplied: theoreticalLeverage > appliedCap,
    annualLogGrowth: dailyLogGrowth * annualizationDays,
    annualGrowth: Math.exp(dailyLogGrowth * annualizationDays) - 1,
  };
}

export function leveragedReturnPath(returns, leverage, riskFreeRate = 0, annualizationDays = ANNUALIZATION_DAYS) {
  const dailyRiskFree = annualRateToDaily(riskFreeRate, annualizationDays);
  const leveraged = returns.map((value) => dailyRiskFree + leverage * (Number(value) - dailyRiskFree));
  const path = wealthPath(leveraged);
  return { ...path, returns: leveraged };
}

export function covarianceFromVolCorrelation(volatilities, correlation) {
  return volatilities.map((left, i) => volatilities.map((right, j) => left * right * correlation[i][j]));
}

export function validateCorrelationMatrix(matrix, tolerance = 1e-8) {
  const n = matrix.length;
  if (!n || matrix.some((row) => !Array.isArray(row) || row.length !== n)) {
    return { valid: false, reasonCode: REASON.INVALID_CORRELATION };
  }
  for (let i = 0; i < n; i += 1) {
    if (!Number.isFinite(matrix[i][i]) || Math.abs(matrix[i][i] - 1) > tolerance) {
      return { valid: false, reasonCode: REASON.INVALID_CORRELATION };
    }
    for (let j = 0; j < n; j += 1) {
      if (!Number.isFinite(matrix[i][j]) || Math.abs(matrix[i][j]) > 1 + tolerance || Math.abs(matrix[i][j] - matrix[j][i]) > tolerance) {
        return { valid: false, reasonCode: REASON.INVALID_CORRELATION };
      }
    }
  }
  const values = matrix.map((row) => [...row]);
  for (let step = 0; step < 100 * n * n; step += 1) {
    let p = 0;
    let q = 1;
    let largest = 0;
    for (let i = 0; i < n; i += 1) for (let j = i + 1; j < n; j += 1) {
      if (Math.abs(values[i][j]) > largest) { largest = Math.abs(values[i][j]); p = i; q = j; }
    }
    if (largest < tolerance) break;
    const angle = 0.5 * Math.atan2(2 * values[p][q], values[q][q] - values[p][p]);
    const c = Math.cos(angle);
    const s = Math.sin(angle);
    for (let k = 0; k < n; k += 1) {
      if (k === p || k === q) continue;
      const kp = values[k][p];
      const kq = values[k][q];
      values[k][p] = values[p][k] = c * kp - s * kq;
      values[k][q] = values[q][k] = s * kp + c * kq;
    }
    const pp = values[p][p];
    const qq = values[q][q];
    const pq = values[p][q];
    values[p][p] = c * c * pp - 2 * s * c * pq + s * s * qq;
    values[q][q] = s * s * pp + 2 * s * c * pq + c * c * qq;
    values[p][q] = values[q][p] = 0;
  }
  if (values.some((row, i) => row[i] < -tolerance)) return { valid: false, reasonCode: REASON.NON_PSD_CORRELATION };
  return { valid: true, reasonCode: null };
}

function invertMatrix(matrix) {
  const n = matrix.length;
  const work = matrix.map((row, i) => [...row, ...Array.from({ length: n }, (_, j) => (i === j ? 1 : 0))]);
  for (let col = 0; col < n; col += 1) {
    let pivot = col;
    for (let row = col + 1; row < n; row += 1) if (Math.abs(work[row][col]) > Math.abs(work[pivot][col])) pivot = row;
    if (Math.abs(work[pivot][col]) < EPS) return null;
    [work[pivot], work[col]] = [work[col], work[pivot]];
    const divisor = work[col][col];
    work[col] = work[col].map((value) => value / divisor);
    for (let row = 0; row < n; row += 1) {
      if (row === col) continue;
      const factor = work[row][col];
      work[row] = work[row].map((value, index) => value - factor * work[col][index]);
    }
  }
  return work.map((row) => row.slice(n));
}

function matrixVector(matrix, vector) {
  return matrix.map((row) => row.reduce((total, value, index) => total + value * vector[index], 0));
}

function dot(left, right) {
  return left.reduce((total, value, index) => total + value * right[index], 0);
}

export function projectNonnegativeL1(values, cap = MAX_EXPOSURE) {
  const positive = values.map((value) => Math.max(0, value));
  if (positive.reduce((sum, value) => sum + value, 0) <= cap) return positive;
  const sorted = [...positive].sort((a, b) => b - a);
  let cumulative = 0;
  let threshold = 0;
  for (let i = 0; i < sorted.length; i += 1) {
    cumulative += sorted[i];
    const candidate = (cumulative - cap) / (i + 1);
    if (i === sorted.length - 1 || candidate >= sorted[i + 1]) { threshold = candidate; break; }
  }
  return positive.map((value) => Math.max(0, value - threshold));
}

export function portfolioKelly({ expectedExcessReturns, volatilities, correlation, riskFreeRate = 0, cap = MAX_EXPOSURE }) {
  const e = expectedExcessReturns.map(Number);
  const vol = volatilities.map(Number);
  if (!e.length || e.length !== vol.length || e.some((value) => !Number.isFinite(value)) || vol.some((value) => !Number.isFinite(value) || value <= 0)) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT };
  }
  const validation = validateCorrelationMatrix(correlation);
  if (!validation.valid) return { status: STATUS.UNAVAILABLE, reasonCode: validation.reasonCode };
  const covariance = covarianceFromVolCorrelation(vol, correlation);
  const inverse = invertMatrix(covariance);
  if (!inverse) return { status: STATUS.UNAVAILABLE, reasonCode: REASON.SINGULAR_COVARIANCE };
  const theoreticalWeights = matrixVector(inverse, e);

  let weights = projectNonnegativeL1(theoreticalWeights, cap);
  const maxRowSum = Math.max(...covariance.map((row) => row.reduce((sum, value) => sum + Math.abs(value), 0)), EPS);
  const stepSize = 0.9 / maxRowSum;
  for (let step = 0; step < 5000; step += 1) {
    const gradient = matrixVector(covariance, weights).map((value, index) => e[index] - value);
    const next = projectNonnegativeL1(weights.map((value, index) => value + stepSize * gradient[index]), cap);
    const delta = Math.max(...next.map((value, index) => Math.abs(value - weights[index])));
    weights = next;
    if (delta < 1e-10) break;
  }
  const portfolioVariance = dot(weights, matrixVector(covariance, weights));
  const logGrowth = riskFreeRate + dot(weights, e) - 0.5 * portfolioVariance;
  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    covariance,
    theoreticalWeights,
    appliedWeights: weights,
    totalExposure: weights.reduce((sum, value) => sum + value, 0),
    logGrowth,
    annualGrowth: Math.exp(logGrowth) - 1,
    annualVolatility: Math.sqrt(Math.max(0, portfolioVariance)),
  };
}

function periodKey(date, frequency) {
  const parsed = new Date(`${date}T00:00:00Z`);
  if (frequency === "daily") return date;
  if (frequency === "weekly") {
    const copy = new Date(parsed);
    const day = copy.getUTCDay() || 7;
    copy.setUTCDate(copy.getUTCDate() + 4 - day);
    const start = new Date(Date.UTC(copy.getUTCFullYear(), 0, 1));
    return `${copy.getUTCFullYear()}-W${String(Math.ceil((((copy - start) / 86400000) + 1) / 7)).padStart(2, "0")}`;
  }
  if (frequency === "monthly") return date.slice(0, 7);
  if (frequency === "quarterly") return `${date.slice(0, 4)}-Q${Math.floor((Number(date.slice(5, 7)) - 1) / 3) + 1}`;
  if (frequency === "yearly") return date.slice(0, 4);
  return "none";
}

export function simulateRebalancing({ returnsByAsset, dates, targetWeights, frequency = "monthly", transactionCostBps = 10, riskFreeRate = 0, borrowingSpread = 0 }) {
  const n = dates.length;
  if (!n || returnsByAsset.length !== targetWeights.length || returnsByAsset.some((series) => series.length !== n)) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NO_COMMON_RETURNS };
  }
  const weights = targetWeights.map(Number);
  if ([...weights, transactionCostBps, riskFreeRate, borrowingSpread].some((value) => !Number.isFinite(value))) {
    return { status: STATUS.UNAVAILABLE, reasonCode: REASON.NON_FINITE_INPUT };
  }
  let risky = weights.map((weight) => weight);
  let cash = 1 - weights.reduce((sum, value) => sum + value, 0);
  let totalCost = 0;
  let turnover = 0;
  const wealth = [1];
  const cashDaily = annualRateToDaily(riskFreeRate);
  const borrowDaily = annualRateToDaily(riskFreeRate + borrowingSpread);

  for (let day = 0; day < n; day += 1) {
    risky = risky.map((value, index) => value * (1 + Number(returnsByAsset[index][day])));
    cash *= 1 + (cash >= 0 ? cashDaily : borrowDaily);
    let total = risky.reduce((sum, value) => sum + value, 0) + cash;
    if (!Number.isFinite(total) || total <= 0) return { status: STATUS.RUIN, reasonCode: REASON.RUIN, wealth };
    const rebalanceNow = frequency !== "none" && (frequency === "daily" || day === n - 1 || periodKey(dates[day], frequency) !== periodKey(dates[day + 1], frequency));
    if (rebalanceNow) {
      const targets = weights.map((weight) => total * weight);
      const traded = targets.reduce((sum, value, index) => sum + Math.abs(value - risky[index]), 0);
      const cost = traded * (transactionCostBps / 10000);
      total -= cost;
      totalCost += cost;
      turnover += traded / Math.max(total + cost, EPS);
      risky = weights.map((weight) => total * weight);
      cash = total - risky.reduce((sum, value) => sum + value, 0);
    }
    wealth.push(total);
  }
  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    wealth,
    endingWealth: wealth.at(-1),
    cumulativeReturn: wealth.at(-1) - 1,
    totalCost,
    turnover,
    endingWeights: risky.map((value) => value / wealth.at(-1)),
    endingCashWeight: cash / wealth.at(-1),
  };
}

export function rebalanceComparison(inputs) {
  const buyAndHold = simulateRebalancing({ ...inputs, frequency: "none", transactionCostBps: 0 });
  const gross = simulateRebalancing({ ...inputs, transactionCostBps: 0 });
  const net = simulateRebalancing(inputs);
  if ([buyAndHold, gross, net].some((result) => result.status !== STATUS.PUBLISHED)) {
    return [buyAndHold, gross, net].find((result) => result.status !== STATUS.PUBLISHED);
  }
  return {
    status: STATUS.PUBLISHED,
    reasonCode: null,
    buyAndHold,
    gross,
    net,
    grossRebalancingEffect: gross.endingWealth - buyAndHold.endingWealth,
    transactionCostDrag: gross.endingWealth - net.endingWealth,
    netRebalancingEffect: net.endingWealth - buyAndHold.endingWealth,
    turnover: net.turnover,
  };
}

export function createPeriodState(start, end) {
  if (!isValidDateRange(start, end)) throw new Error(REASON.INVALID_RANGE);
  return { official: { start, end }, exploration: { start, end }, error: null };
}

export function isValidDateRange(start, end, minimum = null, maximum = null) {
  const left = Date.parse(start);
  const right = Date.parse(end);
  if (!Number.isFinite(left) || !Number.isFinite(right) || left >= right) return false;
  if (minimum && left < Date.parse(minimum)) return false;
  if (maximum && right > Date.parse(maximum)) return false;
  return true;
}

export function setExplorationRange(state, start, end, bounds = {}) {
  if (!isValidDateRange(start, end, bounds.minimum, bounds.maximum)) return { ...state, error: REASON.INVALID_RANGE };
  return { ...state, exploration: { start, end }, error: null };
}

export function applyExplorationRange(state, bounds = {}) {
  const { start, end } = state.exploration;
  if (!isValidDateRange(start, end, bounds.minimum, bounds.maximum)) return { ...state, error: REASON.INVALID_RANGE };
  return { official: { start, end }, exploration: { start, end }, error: null };
}

export function normalizeAssetPayload(payload) {
  const metadata = payload?.metadata ?? {};
  const columns = payload?.columns ?? payload?.data ?? {};
  const dates = columns.date ?? columns.dates ?? payload?.dates ?? [];
  const prices = columns.adjustedClose ?? columns.adjusted_close ?? columns.close ?? columns.price ?? payload?.prices ?? [];
  let returns = columns.return ?? columns.returns ?? payload?.returns ?? [];
  if (!returns.length && prices.length === dates.length) {
    returns = prices.slice(1).map((price, index) => Number(price) / Number(prices[index]) - 1);
    return {
      id: payload.id ?? payload.assetId,
      symbol: payload.symbol ?? payload.ticker ?? metadata.symbol,
      name: payload.name ?? metadata.name,
      currency: payload.currency ?? metadata.baseCurrency ?? metadata.currency,
      returnBasis: payload.returnBasis ?? payload.return_basis ?? metadata.returnBasis,
      status: payload.status ?? payload.state ?? STATUS.PUBLISHED,
      dates,
      returnDates: dates.slice(1),
      prices: prices.map(Number),
      returns,
      source: payload.source,
    };
  }
  const hasLeadingNull = returns.length === dates.length && (returns[0] === null || returns[0] === undefined || returns[0] === "");
  const numericReturns = (hasLeadingNull ? returns.slice(1) : returns).map(Number);
  const observationDates = hasLeadingNull || dates.length === numericReturns.length + 1
    ? dates.slice(0, numericReturns.length + 1)
    : dates.slice(0, numericReturns.length);
  const alignedReturnDates = observationDates.length === numericReturns.length + 1 ? observationDates.slice(1) : observationDates;
  return {
    id: payload.id ?? payload.assetId,
    symbol: payload.symbol ?? payload.ticker ?? metadata.symbol,
    name: payload.name ?? metadata.name,
    currency: payload.currency ?? metadata.baseCurrency ?? metadata.currency,
    returnBasis: payload.returnBasis ?? payload.return_basis ?? metadata.returnBasis,
    status: payload.status ?? payload.state ?? STATUS.PUBLISHED,
    dates: observationDates,
    returnDates: alignedReturnDates,
    prices: prices.slice(0, observationDates.length).map(Number),
    returns: numericReturns,
    source: payload.source,
  };
}

export function sliceSeries(series, start, end) {
  const observationIndexes = series.dates.map((date, index) => ({ date, index })).filter(({ date }) => date >= start && date <= end);
  const selectedDates = observationIndexes.map(({ date }) => date);
  const returnDates = series.returnDates ?? (series.dates.length === series.returns.length + 1 ? series.dates.slice(1) : series.dates);
  const returnIndexes = returnDates.map((date, index) => ({ date, index })).filter(({ date }) => date > selectedDates[0] && date <= selectedDates.at(-1));
  return {
    ...series,
    dates: selectedDates,
    returnDates: returnIndexes.map(({ date }) => date),
    returns: returnIndexes.map(({ index }) => series.returns[index]),
    prices: observationIndexes.map(({ index }) => series.prices[index]).filter(Number.isFinite),
  };
}

export function rowsToCsv(headers, rows) {
  const escape = (value) => {
    const text = value === null || value === undefined ? "" : String(value);
    return /[",\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
  };
  return [headers, ...rows].map((row) => row.map(escape).join(",")).join("\n");
}
