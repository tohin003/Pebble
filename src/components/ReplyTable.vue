<script setup lang="ts">
// Phase 46 (1.9.29, D195, w25-genui-b): a reply's Markdown table, drawn as a native table at no model cost: each header
// sorts its column (numbers by value, words as people sort them; aria-sort says how), and a table with a column of
// numbers beside a column of names can show itself as a chart. The reply's text is unchanged; a cell with links, code
// or emphasis goes through the window's own Markdown pipeline (Markdown.vue), every other cell is a text node.
import { computed, ref } from "vue";
import Markdown from "./Markdown.vue";
import VisualChart from "./VisualChart.vue";
import type { ChartData } from "../chartModel";
import { numericColumns, sortedRows, tableChart, type ReplyCell, type ReplyTable } from "../visuals";

const props = defineProps<{ table: ReplyTable }>();
const numbers = computed(() => numericColumns(props.table));
const chartable = computed(() => tableChart(props.table));
const sort = ref<{ column: number; direction: "ascending" | "descending" } | null>(null);
const order = computed(() => (sort.value ? sortedRows(props.table, sort.value.column, sort.value.direction) : props.table.rows.map((_, i) => i)));
const showChart = ref(false);
const chosen = ref<number | null>(null);

function sortBy(column: number) {
  sort.value = sort.value?.column === column && sort.value.direction === "ascending"
    ? { column, direction: "descending" } : { column, direction: "ascending" };
}
const ariaSort = (column: number) => (sort.value?.column === column ? sort.value.direction : "none");
const mark = (column: number) => (sort.value?.column !== column ? "" : sort.value.direction === "ascending" ? "↑" : "↓");
const heading = (column: number) => props.table.header[column]?.text || "Column " + (column + 1);
const ALIGN: Record<string, string> = { left: "left", right: "right", center: "center" };
const alignOf = (column: number) => (numbers.value.has(column) ? "right" : ALIGN[props.table.align[column] || ""] || "");
// links, code, emphasis and source markers keep the reply's own rendering
const rich = (cell: ReplyCell) => /[*_`[\]<>~\\]|https?:/.test(cell.markdown);

/** One unit every cell of a column shares ("%", "ms", "$"), so the chart's axis says it too. */
function unitOf(column: number): string {
  const units = props.table.rows.map((row) => /^\s*([$€£¥₹]?)[-+−\d.,\s]+([a-zA-Z%]{0,4})\s*$/.exec(row[column]?.text || ""))
    .filter((m): m is RegExpExecArray => !!m).map((m) => m[1] || m[2]);
  return units.length && units.every((u) => u === units[0]) && !/^[kKMB]$/.test(units[0]) ? units[0] : "";
}
const valueColumn = computed(() => {
  const found = chartable.value;
  if (!found) return -1;
  return chosen.value !== null && found.values.includes(chosen.value) ? chosen.value : found.values[0];
});
const chart = computed<ChartData | null>(() => {
  const found = chartable.value, values = numbers.value.get(valueColumn.value);
  if (!showChart.value || !found || !values) return null;
  const labels = numbers.value.get(found.label);
  const rows = order.value.filter((i) => values[i] !== null)
    .map((i) => ({ label: props.table.rows[i][found.label]?.text || "—", value: values[i] as number }));
  // a column of years reads as a line over time; anything else as bars
  return { type: labels ? "line" : "bar", x_key: "label", y_keys: ["value"], rows, unit: unitOf(valueColumn.value),
    labels: { label: heading(found.label), value: heading(valueColumn.value) } };
});
</script>

<template>
  <div class="reply-table">
    <div v-if="chartable" class="tools" role="group" :aria-label="'Show ' + heading(chartable.values[0]) + ' as'">
      <button type="button" :aria-pressed="showChart ? 'false' : 'true'" @click="showChart = false">Table</button>
      <button type="button" :aria-pressed="showChart ? 'true' : 'false'" @click="showChart = true">Chart</button>
      <select v-if="showChart && chartable.values.length > 1" :value="valueColumn" aria-label="Column to chart" @change="chosen = Number(($event.target as HTMLSelectElement).value)">
        <option v-for="column in chartable.values" :key="column" :value="column">{{ heading(column) }}</option>
      </select>
    </div>
    <VisualChart v-if="chart" :data="chart" :title="heading(valueColumn) + ' by ' + heading(chartable?.label ?? 0)" :table="false" />
    <div v-else class="scroll" role="region" aria-label="Table" tabindex="0">
      <table>
        <thead>
          <tr>
            <th v-for="(_, column) in table.header" :key="column" scope="col" :aria-sort="ariaSort(column)" :class="alignOf(column)">
              <button type="button" class="sort" @click="sortBy(column)">{{ heading(column) }}<span class="sort-mark" aria-hidden="true">{{ mark(column) }}</span></button>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="index in order" :key="index">
            <td v-for="(cell, column) in table.rows[index]" :key="column" :class="alignOf(column)">
              <Markdown v-if="rich(cell)" :text="cell.markdown" />
              <template v-else>{{ cell.text }}</template>
            </td>
          </tr>
        </tbody>
      </table>
    </div>
  </div>
</template>

<style scoped>
.reply-table {
  margin: 1em 0;
  min-width: 0;
}
.tools {
  display: flex;
  align-items: center;
  gap: var(--space-1);
  margin-bottom: var(--space-2);
}
.tools button {
  font: inherit;
  font-size: var(--text-sm);
  padding: 3px 10px;
  border: 0.5px solid var(--line-strong);
  border-radius: var(--radius-sm);
  background: var(--panel);
  color: var(--secondary);
  cursor: pointer;
}
.tools button[aria-pressed="true"] {
  background: var(--selected);
  color: var(--text);
  font-weight: 600;
}
.tools select {
  margin-left: var(--space-2);
  font: inherit;
  font-size: var(--text-sm);
}
.tools button:focus-visible,
.tools select:focus-visible,
.sort:focus-visible,
.scroll:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.scroll {
  max-width: 100%;
  overflow-x: auto;
}
table {
  border-collapse: collapse;
  min-width: 100%;
  font-size: 13px;
}
th,
td {
  border: 1px solid var(--line);
  padding: 8px;
  text-align: left;
  vertical-align: top;
  min-width: 7ch;
}
th {
  background: var(--code);
  padding: 0;
}
.right {
  text-align: right;
  font-variant-numeric: tabular-nums;
}
.center {
  text-align: center;
}
.sort {
  all: unset;
  box-sizing: border-box;
  display: flex;
  width: 100%;
  gap: 4px;
  padding: 8px;
  font-weight: 600;
  cursor: pointer;
}
.right .sort {
  justify-content: flex-end;
}
.center .sort {
  justify-content: center;
}
.sort-mark {
  color: var(--accent);
  min-width: 0.8em;
}
td :deep(.markdown p) {
  margin: 0;
}
</style>
