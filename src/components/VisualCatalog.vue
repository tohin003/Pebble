<script setup lang="ts">
// Phase 46 (1.9.29, D195, w25-genui-b): the visual catalog's cards, drawn from data NativeArtifactRenderer has checked:
// compare (a table of options against criteria, one marked recommended), metrics (2 to 4 tiles), timeline (dated
// events, oldest first) and record (one card of labelled values). Text nodes only, no route, no link; numbers and
// dates are formatted here, never as the model wrote them.
import { computed } from "vue";
import { formatDate, formatNumber, type NumberFormat } from "../visuals";

type Value = string | number | boolean;
interface Compare { options: { id: string; label: string; summary?: string }[]; criteria: { label: string; values: Value[] }[]; recommended?: string; caption?: string }
interface Metrics { tiles: { label: string; value: number; unit?: string; delta?: number; format?: NumberFormat; better?: "up" | "down"; caption?: string }[]; caption?: string }
interface Timeline { events: { date: string; title: string; detail?: string; tone?: string }[]; caption?: string }
interface RecordCard { fields: { label: string; value: string | number }[]; subtitle?: string; note?: string }

const props = defineProps<{ kind: "compare" | "metrics" | "timeline" | "record"; data: Record<string, any>; title: string }>();
const compare = computed(() => (props.kind === "compare" ? (props.data as Compare) : null));
const metrics = computed(() => (props.kind === "metrics" ? (props.data as Metrics) : null));
const timeline = computed(() => (props.kind === "timeline" ? (props.data as Timeline) : null));
const record = computed(() => (props.kind === "record" ? (props.data as RecordCard) : null));
// oldest first, whatever order the events came in; equal dates keep theirs
const events = computed(() => [...(timeline.value?.events || [])].map((event, index) => ({ event, index }))
  .sort((a, b) => a.event.date.localeCompare(b.event.date) || a.index - b.index).map(({ event }) => event));
const TONES: { [tone: string]: string } = { neutral: "tone-neutral", accent: "tone-accent", ok: "tone-ok", warn: "tone-warn", danger: "tone-danger" };
const toneOf = (tone?: string) => TONES[tone || "neutral"] || "tone-neutral";

function cell(value: Value) {
  if (value === true) return "Yes";
  if (value === false) return "No";
  return typeof value === "number" ? formatNumber(value) : value;
}
type Tile = Metrics["tiles"][number];
function change(tile: Tile) {
  if (tile.delta === undefined) return "";
  const size = formatNumber(Math.abs(tile.delta), tile.format === "percent" ? "percent" : tile.format, tile.format === "percent" ? "" : tile.unit);
  return tile.delta > 0 ? "Up " + size : tile.delta < 0 ? "Down " + size : "No change";
}
function changeTone(tile: Tile) {
  if (!tile.delta || !tile.better) return "";
  return (tile.delta > 0) === (tile.better === "up") ? "good" : "bad";
}
</script>

<template>
  <div class="visual-catalog">
    <div v-if="compare" class="compare-scroll" tabindex="0" role="region" :aria-label="title + ', comparison'">
      <table class="compare">
        <caption v-if="compare.caption">{{ compare.caption }}</caption>
        <thead>
          <tr>
            <td class="corner" />
            <th v-for="option in compare.options" :key="option.id" scope="col" :class="{ recommended: option.id === compare.recommended }">
              <span class="option">{{ option.label }}</span>
              <span v-if="option.id === compare.recommended" class="badge">Recommended</span>
              <small v-if="option.summary">{{ option.summary }}</small>
            </th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(criterion, row) in compare.criteria" :key="row">
            <th scope="row">{{ criterion.label }}</th>
            <td v-for="(value, column) in criterion.values" :key="column" :class="{ recommended: compare.options[column]?.id === compare.recommended, yes: value === true, no: value === false }">
              <span v-if="value === true" class="mark" aria-hidden="true">✓</span>{{ cell(value) }}
            </td>
          </tr>
        </tbody>
      </table>
    </div>
    <template v-else-if="metrics">
      <ul class="tiles" :aria-label="title">
        <li v-for="(tile, index) in metrics.tiles" :key="index" class="tile">
          <span class="tile-label">{{ tile.label }}</span>
          <strong class="tile-value">{{ formatNumber(tile.value, tile.format, tile.unit) }}</strong>
          <span v-if="tile.delta !== undefined" class="tile-change" :class="changeTone(tile)">
            <span aria-hidden="true">{{ tile.delta > 0 ? "▲ " : tile.delta < 0 ? "▼ " : "" }}</span>{{ change(tile) }}
          </span>
          <small v-if="tile.caption">{{ tile.caption }}</small>
        </li>
      </ul>
      <p v-if="metrics.caption" class="caption">{{ metrics.caption }}</p>
    </template>
    <template v-else-if="timeline">
      <p v-if="timeline.caption" class="caption">{{ timeline.caption }}</p>
      <ol class="timeline" :aria-label="title">
        <li v-for="(event, index) in events" :key="index" :class="toneOf(event.tone)">
          <i class="dot" aria-hidden="true" />
          <time :datetime="event.date">{{ formatDate(event.date) }}</time>
          <strong>{{ event.title }}</strong>
          <p v-if="event.detail">{{ event.detail }}</p>
        </li>
      </ol>
    </template>
    <article v-else-if="record" class="record" :aria-label="title">
      <p v-if="record.subtitle" class="caption">{{ record.subtitle }}</p>
      <dl>
        <template v-for="(field, index) in record.fields" :key="index">
          <dt>{{ field.label }}</dt>
          <dd>{{ typeof field.value === "number" ? formatNumber(field.value) : field.value }}</dd>
        </template>
      </dl>
      <p v-if="record.note" class="note">{{ record.note }}</p>
    </article>
  </div>
</template>

<style scoped>
.visual-catalog {
  font-size: var(--text-md);
  min-width: 0;
}
.caption {
  color: var(--secondary);
  margin: 0 0 var(--space-3);
}
.compare-scroll {
  overflow: auto;
  max-width: 100%;
}
.compare-scroll:focus-visible {
  outline: 2px solid var(--focus);
  outline-offset: 2px;
}
.compare {
  border-collapse: separate;
  border-spacing: 0;
  width: 100%;
  font-size: var(--text-sm);
}
.compare caption {
  text-align: left;
  color: var(--secondary);
  margin-bottom: var(--space-3);
}
.compare th,
.compare td {
  text-align: left;
  vertical-align: top;
  padding: 10px 12px;
  border-bottom: 0.5px solid var(--line);
  min-width: 96px;
}
.compare thead th {
  font-weight: 600;
  border-top: 0.5px solid transparent;
}
.compare thead th small {
  display: block;
  font-weight: 400;
  color: var(--secondary);
  margin-top: 2px;
}
.compare tbody th {
  font-weight: 400;
  color: var(--secondary);
}
.compare .recommended {
  background: var(--soft);
  border-left: 1px solid var(--accent);
  border-right: 1px solid var(--accent);
}
.compare thead .recommended {
  border-top: 1px solid var(--accent);
  border-radius: var(--radius-md) var(--radius-md) 0 0;
}
.compare tbody tr:last-child .recommended {
  border-bottom: 1px solid var(--accent);
}
.badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 6px;
  border-radius: 999px;
  font-size: var(--text-xs);
  font-weight: 600;
  color: var(--accent);
  border: 0.5px solid var(--accent);
}
.mark {
  color: var(--ok);
  margin-right: 4px;
}
.no {
  color: var(--secondary);
}
.tiles {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(132px, 1fr));
  gap: var(--space-3);
  margin: 0 0 var(--space-3);
  padding: 0;
  list-style: none;
}
.tile {
  display: flex;
  flex-direction: column;
  gap: 2px;
  padding: var(--space-3) var(--space-4);
  border: 0.5px solid var(--line-strong);
  border-radius: var(--radius-md);
  background: var(--panel);
}
.tile-label {
  color: var(--secondary);
  font-size: var(--text-sm);
}
.tile-value {
  font-size: 22px;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  letter-spacing: -0.3px;
}
.tile-change {
  font-size: var(--text-xs);
  color: var(--secondary);
}
.tile-change.good {
  color: var(--ok);
}
.tile-change.bad {
  color: var(--danger);
}
.tile small {
  color: var(--muted);
  font-size: var(--text-xs);
}
.timeline {
  list-style: none;
  margin: 0;
  padding: 0 0 0 18px;
  border-left: 1px solid var(--line-strong);
}
.timeline li {
  position: relative;
  padding: 0 0 var(--space-4) var(--space-2);
}
.timeline li:last-child {
  padding-bottom: 0;
}
.dot {
  position: absolute;
  left: -24px;
  top: 5px;
  width: 11px;
  height: 11px;
  border-radius: 50%;
  background: var(--panel);
  border: 2px solid var(--line-strong);
}
.tone-accent .dot {
  border-color: var(--accent);
  background: var(--accent);
}
.tone-ok .dot {
  border-color: var(--ok);
}
.tone-warn .dot {
  border-color: var(--warn);
}
.tone-danger .dot {
  border-color: var(--danger);
}
.timeline time {
  display: block;
  color: var(--secondary);
  font-size: var(--text-xs);
}
.timeline strong {
  font-weight: 600;
}
.timeline p {
  margin: 2px 0 0;
  color: var(--secondary);
}
.record {
  border: 0.5px solid var(--line-strong);
  border-radius: var(--radius-lg);
  padding: var(--space-4);
  background: var(--panel);
}
.record dl {
  display: grid;
  grid-template-columns: minmax(80px, max-content) 1fr;
  gap: var(--space-2) var(--space-4);
  margin: 0;
}
.record dt {
  color: var(--secondary);
}
.record dd {
  margin: 0;
  overflow-wrap: anywhere;
}
.record .note {
  margin: var(--space-3) 0 0;
  color: var(--muted);
  font-size: var(--text-sm);
}
</style>
