<script setup lang="ts">
// Phase 46 (1.9.29, D195, w25-genui-b): a chart drawn by Pebble (bar, line, area, scatter, stacked, pie, donut), for a
// model's chart view and for a reply table's chart toggle. Geometry comes from chartModel.ts; every string is a text node,
// colours are fixed classes (tokens.css --chart-1 to --chart-6, both themes), and the data stays readable as a table.
import { computed } from "vue";
import { chartModel, chartSummary, seriesLabel, valueText, type ChartData } from "../chartModel";

const props = withDefaults(defineProps<{ data: ChartData; title: string; table?: boolean }>(), { table: true });
const model = computed(() => chartModel(props.data));
const cartesian = computed(() => (model.value.kind === "cartesian" ? model.value : null));
const pie = computed(() => (model.value.kind === "pie" ? model.value : null));
const summary = computed(() => chartSummary(props.data, props.title));
const lines = computed(() => props.data.type === "line" || props.data.type === "area");
const legend = computed(() => props.data.y_keys.length > 1 || props.data.type === "scatter");
</script>

<template>
  <figure class="visual-chart">
    <figcaption v-if="data.caption">{{ data.caption }}</figcaption>
    <div class="chart-body" :class="{ round: pie }">
      <svg viewBox="0 0 640 280" role="img" :aria-label="summary" focusable="false">
        <template v-if="cartesian">
          <g class="grid" aria-hidden="true">
            <template v-for="tick in cartesian.yTicks" :key="'y' + tick.at">
              <line :x1="cartesian.plot.left" :x2="cartesian.plot.right" :y1="tick.at" :y2="tick.at" />
              <text :x="cartesian.plot.left - 8" :y="tick.at + 4" text-anchor="end">{{ tick.label }}</text>
            </template>
            <template v-for="tick in cartesian.xTicks" :key="'x' + tick.at">
              <text :x="tick.at" :y="cartesian.plot.bottom + 20" text-anchor="middle">{{ tick.label }}<title>{{ tick.full }}</title></text>
            </template>
            <line class="zero" :x1="cartesian.plot.left" :x2="cartesian.plot.right" :y1="cartesian.baseline" :y2="cartesian.baseline" />
          </g>
          <g v-for="series in cartesian.series" :key="series.key" class="series" :class="series.tone">
            <path v-if="series.area" class="area" :d="series.area" />
            <path v-if="lines" class="line" :d="series.path" />
            <rect v-for="(bar, index) in series.bars" :key="'b' + index" class="bar" :x="bar.x" :y="bar.y" :width="bar.width" :height="bar.height" rx="2">
              <title>{{ series.label }}: {{ bar.label }}</title>
            </rect>
            <template v-if="lines || data.type === 'scatter'">
              <circle v-for="(point, index) in series.points" :key="'p' + index" class="point" :cx="point.x" :cy="point.y" :r="data.type === 'scatter' ? 4.5 : 3">
                <title>{{ series.label }}: {{ point.label }}</title>
              </circle>
            </template>
          </g>
        </template>
        <template v-else-if="pie">
          <g v-for="(slice, index) in pie.slices" :key="index" class="series" :class="slice.tone">
            <path v-if="slice.path" class="slice" :d="slice.path"><title>{{ slice.label }}: {{ slice.value }} ({{ slice.share }})</title></path>
          </g>
        </template>
      </svg>
      <ul v-if="pie" class="chart-legend parts">
        <li v-for="(slice, index) in pie.slices" :key="index" :class="slice.tone">
          <i aria-hidden="true" /><span>{{ slice.label }}</span><b>{{ slice.share }}</b>
        </li>
      </ul>
    </div>
    <ul v-if="cartesian && legend" class="chart-legend">
      <li v-for="series in cartesian.series" :key="series.key" :class="series.tone"><i aria-hidden="true" />{{ series.label }}</li>
    </ul>
    <details v-if="table" class="chart-data">
      <summary>Chart data</summary>
      <div class="chart-table" tabindex="0" role="region" aria-label="Chart data">
        <table>
          <thead>
            <tr>
              <th scope="col">{{ seriesLabel(data, data.x_key) }}</th>
              <th v-for="key in data.y_keys" :key="key" scope="col">{{ seriesLabel(data, key) }}</th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in data.rows" :key="index">
              <th scope="row">{{ data.type === "scatter" ? valueText({ ...data, unit: "", format: "number" }, row[data.x_key]) : row[data.x_key] ?? "—" }}</th>
              <td v-for="key in data.y_keys" :key="key">{{ valueText(data, row[key]) }}</td>
            </tr>
          </tbody>
        </table>
      </div>
    </details>
  </figure>
</template>

<style scoped>
.visual-chart {
  margin: 0;
  min-width: 0;
  font-size: var(--text-md);
}
figcaption {
  color: var(--secondary);
  margin: 0 0 var(--space-2);
}
.chart-body.round {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: var(--space-4);
}
.chart-body.round svg {
  width: min(100%, 300px);
}
svg {
  display: block;
  width: 100%;
  height: auto;
  overflow: visible;
}
.chart-body.round svg {
  aspect-ratio: 300 / 280;
}
.grid line {
  stroke: var(--line);
  stroke-width: 1;
}
.grid .zero {
  stroke: var(--line-strong);
}
.grid text {
  fill: var(--muted);
  font: 11px system-ui, sans-serif;
}
.s0 { --series: var(--chart-1); }
.s1 { --series: var(--chart-2); }
.s2 { --series: var(--chart-3); }
.s3 { --series: var(--chart-4); }
.s4 { --series: var(--chart-5); }
.s5 { --series: var(--chart-6); }
.bar,
.point,
.slice {
  fill: var(--series);
}
.alt .slice,
.alt i {
  opacity: 0.6;
}
.slice {
  stroke: var(--panel);
  stroke-width: 2;
}
.line {
  fill: none;
  stroke: var(--series);
  stroke-width: 2.5;
  stroke-linejoin: round;
  stroke-linecap: round;
}
.area {
  fill: var(--series);
  opacity: 0.16;
}
.point {
  stroke: var(--panel);
  stroke-width: 1.5;
}
.chart-legend {
  display: flex;
  flex-wrap: wrap;
  gap: var(--space-2) var(--space-4);
  margin: var(--space-2) 0 0;
  padding: 0;
  list-style: none;
  font-size: var(--text-xs);
  color: var(--secondary);
}
.chart-legend.parts {
  flex-direction: column;
  flex: 1 1 160px;
  font-size: var(--text-sm);
}
.chart-legend li {
  display: flex;
  align-items: center;
  gap: 6px;
}
.chart-legend b {
  margin-left: auto;
  font-weight: 600;
  color: var(--text);
  font-variant-numeric: tabular-nums;
}
.chart-legend i {
  width: 9px;
  height: 9px;
  border-radius: 2px;
  flex-shrink: 0;
  background: var(--series);
}
.chart-data {
  margin-top: var(--space-3);
}
.chart-data summary {
  cursor: pointer;
  font-size: var(--text-sm);
  color: var(--secondary);
}
.chart-data summary:focus-visible,
.chart-table:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.chart-table {
  overflow: auto;
  max-width: 100%;
  margin-top: var(--space-2);
}
table {
  border-collapse: collapse;
  width: 100%;
  font-size: var(--text-sm);
}
th,
td {
  text-align: left;
  padding: 6px 10px;
  border-bottom: 0.5px solid var(--line);
}
thead th {
  font-weight: 600;
  background: var(--code);
}
tbody th {
  font-weight: 400;
}
td {
  font-variant-numeric: tabular-nums;
}
</style>
