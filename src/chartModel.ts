// Phase 46 (1.9.29, D195, w25-genui-b): a chart's geometry, from the data a model (or a reply's table) gives. Pure: the
// renderer (VisualChart.vue) only draws what this returns. Axes use 1-2-5 ticks from zero where the data allows, every
// number is rounded and formatted by Pebble (visuals.formatNumber), labels that do not fit are thinned and shortened, and
// series take their colour from a fixed palette by index (classes s0 to s5, defined in tokens.css for both themes).
import { formatNumber, niceTicks, type NumberFormat } from "./visuals";

export type ChartType = "bar" | "line" | "area" | "scatter" | "stacked" | "pie" | "donut";
export interface ChartData {
  type: ChartType;
  x_key: string;
  y_keys: string[];
  rows: Record<string, string | number | boolean | null>[];
  labels?: Record<string, string>;
  caption?: string;
  format?: NumberFormat;
  unit?: string;
}
export const TYPE_NAMES: Record<ChartType, string> = {
  bar: "Bar chart", line: "Line chart", area: "Area chart", scatter: "Scatter chart", stacked: "Stacked bar chart",
  pie: "Pie chart", donut: "Donut chart",
};
export const SIZE = { width: 640, height: 280 };
const PALETTE = 6;

export interface Tick { at: number; label: string }
export interface Mark { x: number; y: number; width: number; height: number; label: string }
export interface Series { key: string; label: string; tone: string; path: string; area: string; points: Mark[]; bars: Mark[] }
export interface Slice { path: string; tone: string; label: string; value: string; share: string }
export interface CartesianModel {
  kind: "cartesian";
  plot: { left: number; right: number; top: number; bottom: number };
  baseline: number;
  yTicks: Tick[];
  xTicks: (Tick & { full: string })[];
  series: Series[];
}
export interface PieModel { kind: "pie"; cx: number; cy: number; radius: number; inner: number; slices: Slice[] }
export type ChartModel = CartesianModel | PieModel;

const text = (value: unknown) => (value === null || value === undefined ? "—" : String(value));
const short = (value: string, limit = 12) => (value.length > limit ? value.slice(0, limit - 1) + "…" : value);
const n = (value: unknown) => (typeof value === "number" && Number.isFinite(value) ? value : 0);
const r = (value: number) => Math.round(value * 10) / 10;

export function seriesLabel(data: ChartData, key: string) {
  return data.labels?.[key] || key;
}

/** One series' value as Pebble writes it. */
export function valueText(data: ChartData, value: unknown) {
  return typeof value === "number" ? formatNumber(value, data.format, data.unit) : text(value);
}

function pie(data: ChartData): PieModel {
  const key = data.y_keys[0], total = data.rows.reduce((sum, row) => sum + Math.max(0, n(row[key])), 0) || 1;
  const cx = 150, cy = SIZE.height / 2, radius = 116, inner = data.type === "donut" ? 66 : 0;
  let angle = -Math.PI / 2;
  const point = (a: number, rad: number) => `${r(cx + rad * Math.cos(a))} ${r(cy + rad * Math.sin(a))}`;
  const slices = data.rows.map((row, index): Slice => {
    const value = Math.max(0, n(row[key])), sweep = (value / total) * Math.PI * 2;
    const start = angle, end = angle + Math.min(sweep, Math.PI * 2 - 1e-4);
    angle += sweep;
    const large = end - start > Math.PI ? 1 : 0;
    const path = inner
      ? `M ${point(start, radius)} A ${radius} ${radius} 0 ${large} 1 ${point(end, radius)} L ${point(end, inner)} A ${inner} ${inner} 0 ${large} 0 ${point(start, inner)} Z`
      : `M ${cx} ${cy} L ${point(start, radius)} A ${radius} ${radius} 0 ${large} 1 ${point(end, radius)} Z`;
    return { path: value ? path : "", tone: "s" + (index % PALETTE) + (index >= PALETTE ? " alt" : ""), label: text(row[data.x_key]),
      value: valueText(data, row[key]), share: formatNumber(value / total, "percent") };
  });
  return { kind: "pie", cx, cy, radius, inner, slices };
}

export function chartModel(data: ChartData): ChartModel {
  if (data.type === "pie" || data.type === "donut") return pie(data);
  const stacked = data.type === "stacked", scatter = data.type === "scatter";
  const totals = data.rows.map((row) => data.y_keys.reduce((sum, key) => sum + n(row[key]), 0));
  const values = stacked ? totals : data.rows.flatMap((row) => data.y_keys.map((key) => n(row[key])));
  // bars and areas stand on zero; lines and scatter may start nearer their data
  const fromZero = data.type !== "line" && !scatter;
  const low = Math.min(...values, ...(fromZero ? [0] : [])), high = Math.max(...values, ...(fromZero ? [0] : []));
  const yTicks = niceTicks(low, high, 5);
  const yLow = yTicks[0], yHigh = yTicks[yTicks.length - 1];
  const labels = yTicks.map((tick) => formatNumber(tick, data.format, data.unit));
  const left = 18 + Math.min(80, Math.max(...labels.map((label) => label.length)) * 6.6), right = SIZE.width - 14;
  const top = 14, bottom = SIZE.height - 34;
  const y = (value: number) => bottom - ((value - yLow) / (yHigh - yLow || 1)) * (bottom - top);
  let x: (index: number) => number, xTicks: CartesianModel["xTicks"];
  const gap = (right - left) / Math.max(1, data.rows.length);
  if (scatter) {
    const xs = data.rows.map((row) => n(row[data.x_key]));
    const ticks = niceTicks(Math.min(...xs), Math.max(...xs), 6), xLow = ticks[0], xHigh = ticks[ticks.length - 1];
    x = (index: number) => left + ((xs[index] - xLow) / (xHigh - xLow || 1)) * (right - left);
    xTicks = ticks.map((tick) => {
      const label = formatNumber(tick);
      return { at: left + ((tick - xLow) / (xHigh - xLow || 1)) * (right - left), label, full: label };
    });
  } else {
    x = (index: number) => left + gap * (index + 0.5);
    // about 8 labels; the last row keeps its label when it is not too close to the previous one shown
    const every = Math.max(1, Math.ceil(data.rows.length / 8)), last = data.rows.length - 1;
    const shown = (index: number) => index % every === 0 || (index === last && last % every >= every / 2);
    xTicks = data.rows.flatMap((row, index) => shown(index)
      ? [{ at: x(index), label: short(text(row[data.x_key])), full: text(row[data.x_key]) }] : []);
  }
  const baseline = y(Math.max(yLow, Math.min(0, yHigh)));
  const below = data.rows.map(() => 0);
  const series = data.y_keys.map((key, index): Series => {
    const label = seriesLabel(data, key);
    const points = data.rows.map((row, i): Mark => ({ x: r(x(i)), y: r(y(n(row[key]))), width: 0, height: 0,
      label: `${text(row[data.x_key])}: ${valueText(data, row[key])}` }));
    const path = points.map((p, i) => `${i ? "L" : "M"} ${p.x} ${p.y}`).join(" ");
    const area = data.type === "area" && points.length
      ? `${path} L ${points[points.length - 1].x} ${r(baseline)} L ${points[0].x} ${r(baseline)} Z` : "";
    const width = stacked ? gap * 0.6 : (gap * 0.72) / data.y_keys.length;
    const bars = data.type === "bar" || stacked ? data.rows.map((row, i): Mark => {
      const value = n(row[key]);
      if (stacked) {
        const y0 = y(below[i]), y1 = y(below[i] + value);
        below[i] += value;
        return { x: r(x(i) - width / 2), y: r(y1), width: r(width), height: r(Math.max(0, y0 - y1)), label: points[i].label };
      }
      const top = y(value);
      return { x: r(x(i) - gap * 0.36 + index * width), y: r(Math.min(baseline, top)), width: r(Math.max(1, width - 2)),
        height: r(Math.abs(baseline - top)), label: points[i].label };
    }) : [];
    return { key, label, tone: "s" + (index % PALETTE), path, area, points, bars };
  });
  return { kind: "cartesian", plot: { left, right, top, bottom }, baseline: r(baseline),
    yTicks: yTicks.map((tick, i) => ({ at: r(y(tick)), label: labels[i] })), xTicks, series };
}

/** The chart in one sentence, for its accessible name: what it shows, by what, and how many points. */
export function chartSummary(data: ChartData, title: string) {
  const what = data.y_keys.map((key) => seriesLabel(data, key)).join(", ");
  const by = seriesLabel(data, data.x_key);
  return `${title}: ${TYPE_NAMES[data.type] || "Chart"} of ${what} by ${by}, ${data.rows.length} ${data.rows.length === 1 ? "point" : "points"}.`;
}
