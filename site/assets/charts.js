import * as echarts from "./vendor/echarts.esm.min.js";

const instances = new Map();
let observer;
const chartNumberFormatter = new Intl.NumberFormat("ko-KR", {
  minimumFractionDigits: 0,
  maximumFractionDigits: 2,
});

export function formatChartNumber(value) {
  if (value === null || value === undefined || value === "") return "—";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "—";
  return chartNumberFormatter.format(Object.is(numeric, -0) ? 0 : numeric);
}

export function formatChartPercent(value) {
  const numeric = Number(value);
  return value === null || value === undefined || value === "" || !Number.isFinite(numeric)
    ? "—"
    : `${formatChartNumber(numeric * 100)}%`;
}

export function formatChartLeverage(value) {
  const formatted = formatChartNumber(value);
  return formatted === "—" ? formatted : `${formatted}×`;
}

function palette() {
  const dark = document.documentElement.dataset.theme === "dark";
  return {
    dark,
    ink: dark ? "#eef4f8" : "#18252e",
    muted: dark ? "#9eb0bd" : "#667782",
    grid: dark ? "#273944" : "#e6ecef",
    panel: dark ? "#101c23" : "#ffffff",
    blue: dark ? "#68a8c1" : "#176b87",
    blueLight: dark ? "rgba(34, 139, 176, .18)" : "rgba(23, 107, 135, .12)",
    gold: "#c58b24",
    orange: "#bd6429",
    olive: "#72844c",
    pink: "#b85c7a",
  };
}

function chartFor(element) {
  if (!element) return null;
  let chart = instances.get(element);
  if (!chart || chart.isDisposed()) {
    chart = echarts.init(element, null, { renderer: "canvas" });
    instances.set(element, chart);
    if (!observer && typeof ResizeObserver !== "undefined") {
      observer = new ResizeObserver((entries) => entries.forEach((entry) => instances.get(entry.target)?.resize()));
    }
    observer?.observe(element);
  }
  return chart;
}

function baseOption(title, subtitle = "") {
  const colors = palette();
  return {
    animationDuration: 350,
    aria: { enabled: true, decal: { show: true } },
    backgroundColor: "transparent",
    color: [colors.blue, colors.gold, colors.olive, colors.pink, colors.orange],
    title: {
      text: title,
      subtext: subtitle,
      left: 4,
      top: 0,
      textStyle: { color: colors.ink, fontFamily: "Pretendard, Inter, sans-serif", fontSize: 15, fontWeight: 700 },
      subtextStyle: { color: colors.muted, fontFamily: "Pretendard, Inter, sans-serif", fontSize: 11, lineHeight: 18 },
    },
    tooltip: {
      trigger: "axis",
      backgroundColor: colors.panel,
      borderColor: colors.grid,
      textStyle: { color: colors.ink, fontFamily: "Pretendard, Inter, sans-serif", fontSize: 12 },
      extraCssText: "box-shadow: 0 12px 32px rgba(16,32,42,.16); border-radius: 10px;",
      confine: true,
      valueFormatter: formatChartNumber,
    },
    textStyle: { color: colors.ink, fontFamily: "Pretendard, Inter, sans-serif" },
    grid: { left: 54, right: 18, top: 66, bottom: 42, containLabel: false },
    xAxis: {
      type: "category",
      boundaryGap: false,
      axisLine: { lineStyle: { color: colors.muted } },
      axisTick: { show: false },
      axisLabel: { color: colors.muted, hideOverlap: true, fontSize: 10 },
    },
    yAxis: {
      type: "value",
      scale: true,
      axisLine: { show: true, lineStyle: { color: colors.muted } },
      axisTick: { show: false },
      axisLabel: { color: colors.muted, fontSize: 10, formatter: formatChartNumber },
      splitLine: { lineStyle: { color: colors.grid } },
    },
  };
}

export function clearChart(element, title = "계산 결과 없음", subtitle = "유효한 입력을 적용하면 차트가 표시됩니다.") {
  const chart = chartFor(element);
  if (!chart) return;
  chart.clear();
  const option = baseOption(title, subtitle);
  option.xAxis.data = [];
  option.series = [];
  chart.setOption(option, true);
  // ECharts does not refresh its generated ARIA description when a chart is
  // replaced by an empty series. Override the stale prior-series narration.
  element.setAttribute("role", "img");
  element.setAttribute("aria-label", `${title}. ${subtitle}`);
}

export function renderWealthChart(element, { dates, officialWealth, explorationWealth, explorationStart, explorationEnd, returnBasis }, onExplore) {
  const chart = chartFor(element);
  if (!chart) return;
  const colors = palette();
  const official = officialWealth.map((value) => value === null || value === undefined ? null : value * 100);
  const exploration = explorationWealth?.length === official.length
    ? explorationWealth.map((value) => value === null || value === undefined ? null : value * 100)
    : official;
  const startIndex = Math.max(0, dates.indexOf(explorationStart));
  const endIndex = Math.max(startIndex, dates.indexOf(explorationEnd));
  const option = baseOption("기준가 100 누적자산", `${returnBasis || "수익률"} · 하단 양끝 브러시는 탐색만 변경`);
  option.grid.bottom = 84;
  option.legend = {
    top: 2, right: 8, textStyle: { color: colors.muted, fontSize: 10 },
    data: ["공식 분석기간", "탐색 미리보기"],
  };
  option.xAxis.data = dates;
  option.yAxis.axisLabel.formatter = formatChartNumber;
  option.dataZoom = [
    {
      type: "slider",
      startValue: startIndex,
      endValue: endIndex >= 0 ? endIndex : dates.length - 1,
      bottom: 12,
      height: 27,
      brushSelect: false,
      realtime: true,
      showDetail: false,
      fillerColor: colors.blueLight,
      borderColor: colors.grid,
      handleStyle: { color: colors.blue, borderColor: colors.blue },
      moveHandleStyle: { color: colors.blue },
      dataBackground: { lineStyle: { color: colors.muted }, areaStyle: { color: colors.blueLight } },
      selectedDataBackground: { lineStyle: { color: colors.blue }, areaStyle: { color: colors.blueLight } },
    },
    { type: "inside", startValue: startIndex, endValue: endIndex >= 0 ? endIndex : dates.length - 1 },
  ];
  option.series = [
    {
      name: "공식 분석기간",
      type: "line",
      data: official,
      showSymbol: false,
      lineStyle: { width: 2.2, color: colors.blue },
      itemStyle: { color: colors.blue },
      emphasis: { focus: "series" },
    },
    {
      name: "탐색 미리보기",
      type: "line",
      data: exploration,
      showSymbol: false,
      lineStyle: { width: 1.3, type: "dashed", color: colors.gold },
      itemStyle: { color: colors.gold },
      emphasis: { focus: "series" },
    },
  ];
  chart.off("datazoom");
  chart.setOption(option, true);
  chart.on("datazoom", (event) => {
    const zoom = event.batch?.[0] ?? event;
    const current = chart.getOption().dataZoom?.[0] ?? {};
    let first = Number.isInteger(zoom.startValue) ? zoom.startValue : Number(current.startValue);
    let last = Number.isInteger(zoom.endValue) ? zoom.endValue : Number(current.endValue);
    if (!Number.isInteger(first)) first = Math.round(((zoom.start ?? current.start ?? 0) / 100) * Math.max(0, dates.length - 1));
    if (!Number.isInteger(last)) last = Math.round(((zoom.end ?? current.end ?? 100) / 100) * Math.max(0, dates.length - 1));
    onExplore?.(dates[clampIndex(first, dates)], dates[clampIndex(last, dates)]);
  });
}

function clampIndex(index, values) {
  return Math.max(0, Math.min(values.length - 1, index));
}

export function renderDrawdownChart(element, dates, drawdowns) {
  const chart = chartFor(element);
  if (!chart) return;
  const colors = palette();
  const option = baseOption("낙폭", "직전 고점 대비 · 0% 아래 영역");
  option.xAxis.data = dates;
  option.yAxis.max = 0;
  option.yAxis.axisLabel.formatter = formatChartPercent;
  option.tooltip.valueFormatter = formatChartPercent;
  option.series = [{
    type: "line", data: drawdowns, showSymbol: false, lineStyle: { width: 1.6, color: colors.orange },
    areaStyle: { color: "rgba(189,100,41,.16)" }, itemStyle: { color: colors.orange },
  }];
  chart.setOption(option, true);
}

export function renderGrowthCurve(element, points, markers = []) {
  const chart = chartFor(element);
  if (!chart) return;
  const colors = palette();
  const option = baseOption("성장률–레버리지 곡선", "연속복리 로그성장률 · 차입 스프레드 반영");
  option.xAxis = {
    type: "value", name: "레버리지", min: 0, max: 3,
    axisLine: { lineStyle: { color: colors.muted } }, axisTick: { show: false },
    axisLabel: { color: colors.muted, formatter: formatChartLeverage }, splitLine: { show: false },
  };
  option.yAxis.axisLabel.formatter = formatChartPercent;
  option.tooltip.formatter = (params) => {
    const point = Array.isArray(params) ? params[0] : params;
    return `${formatChartLeverage(point.data[0])}<br><strong>${formatChartPercent(point.data[1])}</strong>`;
  };
  option.series = [
    { type: "line", data: points, showSymbol: false, lineStyle: { width: 2.2, color: colors.blue }, itemStyle: { color: colors.blue } },
    ...markers.map((marker) => ({
      name: marker.name, type: "scatter", data: [[marker.x, marker.y]], symbolSize: 10,
      itemStyle: { color: marker.color ?? colors.gold, borderColor: colors.panel, borderWidth: 2 },
      label: { show: true, formatter: marker.name, position: "top", color: colors.ink, fontSize: 10 },
    })),
  ];
  chart.setOption(option, true);
}

export function renderWeightsChart(element, labels, theoretical, applied) {
  const chart = chartFor(element);
  if (!chart) return;
  const colors = palette();
  const option = baseOption("포트폴리오 비중", "무제약 이론값과 long-only·총 노출 3× 적용값");
  option.grid = { left: 92, right: 20, top: 70, bottom: 30 };
  option.legend = { top: 34, right: 8, data: ["이론", "적용"], textStyle: { color: colors.muted, fontSize: 10 } };
  option.xAxis = {
    type: "value", axisLine: { lineStyle: { color: colors.muted } }, axisTick: { show: false },
    axisLabel: { color: colors.muted, formatter: formatChartLeverage }, splitLine: { lineStyle: { color: colors.grid } },
  };
  option.yAxis = { type: "category", data: labels, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: colors.ink } };
  option.series = [
    { name: "이론", type: "bar", data: theoretical, itemStyle: { color: colors.blueLight, borderColor: colors.blue, borderWidth: 1 } },
    { name: "적용", type: "bar", data: applied, itemStyle: { color: colors.gold }, label: { show: true, position: "right", formatter: ({ value }) => formatChartLeverage(value), color: colors.ink } },
  ];
  option.tooltip.valueFormatter = formatChartLeverage;
  chart.setOption(option, true);
}

export function renderCorrelationHeatmap(element, labels, correlation) {
  const chart = chartFor(element);
  if (!chart) return;
  const colors = palette();
  const data = correlation.flatMap((row, y) => row.map((value, x) => [x, y, value]));
  const option = baseOption("상관행렬", "입력값 · 대칭·범위·양의 준정부호 검증");
  option.grid = { left: 80, right: 42, top: 65, bottom: 50 };
  option.xAxis = { type: "category", data: labels, splitArea: { show: true }, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: colors.ink } };
  option.yAxis = { type: "category", data: labels, splitArea: { show: true }, axisLine: { show: false }, axisTick: { show: false }, axisLabel: { color: colors.ink } };
  option.visualMap = {
    min: -1, max: 1, calculable: false, orient: "horizontal", left: "center", bottom: 5,
    inRange: { color: [colors.orange, colors.panel, colors.blue] }, textStyle: { color: colors.muted, fontSize: 9 }, formatter: formatChartNumber,
  };
  option.tooltip.formatter = ({ data: point }) => `${labels[point[1]]} × ${labels[point[0]]}<br><strong>${formatChartNumber(point[2])}</strong>`;
  option.series = [{ type: "heatmap", data, label: { show: true, formatter: ({ data: point }) => formatChartNumber(point[2]), color: colors.ink }, emphasis: { itemStyle: { shadowBlur: 8, shadowColor: "rgba(0,0,0,.2)" } } }];
  chart.setOption(option, true);
}

export function renderRebalanceChart(element, dates, comparison) {
  const chart = chartFor(element);
  if (!chart || comparison.status !== "published") return;
  const colors = palette();
  const expectedLength = comparison.net.wealth.length;
  const xDates = rebalanceAxisLabels(dates, expectedLength);
  const option = baseOption("재조정 효과 비교", "초기 1은 첫 수익률 전 · 동일 목표비중 · 비용 전/후와 미재조정 경로");
  option.legend = { top: 32, right: 8, data: ["미재조정", "비용 전", "비용 후"], textStyle: { color: colors.muted, fontSize: 10 } };
  option.xAxis.data = xDates;
  option.yAxis.axisLabel.formatter = formatChartNumber;
  option.series = [
    { name: "미재조정", type: "line", data: comparison.buyAndHold.wealth, showSymbol: false, lineStyle: { color: colors.muted, type: "dashed", width: 1.4 } },
    { name: "비용 전", type: "line", data: comparison.gross.wealth, showSymbol: false, lineStyle: { color: colors.gold, width: 1.7 } },
    { name: "비용 후", type: "line", data: comparison.net.wealth, showSymbol: false, lineStyle: { color: colors.blue, width: 2.2 } },
  ];
  chart.setOption(option, true);
}

export function rebalanceAxisLabels(dates, expectedLength) {
  if (!Number.isInteger(expectedLength) || expectedLength <= 0) return [];
  if (dates.length === expectedLength) return dates.slice(0, expectedLength);
  if (dates.length === expectedLength - 1) return ["시작", ...dates];
  return Array.from({ length: expectedLength }, (_, index) => dates[index] ?? (index === 0 ? "시작" : `관측 ${index}`));
}

export function disposeCharts() {
  for (const [element, chart] of instances) {
    observer?.unobserve(element);
    chart.dispose();
  }
  instances.clear();
}
