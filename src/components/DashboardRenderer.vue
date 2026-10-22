<script setup lang="ts">
import { computed } from "vue";
import { PhArrowClockwise } from "@phosphor-icons/vue";
import NativeArtifactRenderer from "./NativeArtifactRenderer.vue";
import type {
  DashboardArtifact,
  DashboardBindingResult,
  DashboardMetricData,
  NativeArtifact,
} from "../types";

const props = defineProps<{
  artifact: DashboardArtifact;
  historical?: boolean;
  disabled?: boolean;
  busy?: boolean;
  error?: string;
}>();
// Refresh is host chrome: the panel calls artifact_refresh; nothing here reaches the bridge.
const emit = defineEmits<{ refresh: [] }>();

interface PanelView {
  id: string;
  title: string;
  kind: string;
  live: boolean;
  error?: string;
  waiting?: boolean;
  metric?: { value: string; unit?: string; delta?: string; trend: string; caption?: string };
  native?: NativeArtifact;
  stamp?: { source: string; asOf: string; digest: string };
}

const reserved = new Set(["__proto__", "prototype", "constructor", "__text"]);
const PANEL_KINDS = new Set(["metric", "table", "chart", "plan", "evidence"]);
// Fixed host tables: data selects a key, never a class name, label or style.
const COLUMNS: Record<string, string> = { 1: "columns-1", 2: "columns-2", 3: "columns-3" };
const TRENDS: Record<string, string> = { up: "trend-up", down: "trend-down", flat: "trend-flat" };
const SOURCES: Record<string, string> = {
  "runs.recent": "Recent runs",
  "runs.usage": "Run usage",
  "jobs.fires": "Scheduled job fires",
  "agents.tree": "Helper agents",
  "artifacts.revisions": "View revisions",
  "memory.stats": "Memory layer counts",
  "git.summary": "Git summary",
  "goals.progress": "Goal progress",
};
const lookup = (table: Record<string, string>, key: string) =>
  Object.hasOwn(table, key) ? table[key] : undefined;

function require(
  condition: unknown,
  message = "This dashboard contains invalid display data.",
): asserts condition {
  if (!condition) throw new Error(message);
}
function object(value: unknown, message?: string): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value), message);
  return value as Record<string, unknown>;
}
function shape(value: unknown, keys: string[], optional: string[] = [], message?: string) {
  const row = object(value, message);
  require(
    keys.every((key) => Object.hasOwn(row, key)) &&
      Object.keys(row).every((key) => [...keys, ...optional].includes(key)),
    message,
  );
  return row;
}
function text(value: unknown, maximum: number, message?: string): asserts value is string {
  require(typeof value === "string" && !!value.trim() && value.length <= maximum, message);
}
function identifier(value: unknown): asserts value is string {
  require(typeof value === "string" && /^[A-Za-z0-9_-]{1,40}$/.test(value) && !reserved.has(value));
}
function finite(value: unknown, message?: string): asserts value is number {
  require(typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1e15, message);
}

// The whole dashboard is refused only for structural faults; a bad panel is refused on its own.
function validateDashboard(data: unknown) {
  let nodes = 0;
  (function walk(value: unknown, depth: number) {
    require(depth <= 10 && ++nodes <= 12000);
    if (Array.isArray(value)) value.forEach((item) => walk(item, depth + 1));
    else if (value !== null && typeof value === "object") {
      require(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
      Object.entries(value).forEach(([key, item]) => {
        require(!reserved.has(key));
        walk(item, depth + 1);
      });
    }
  })(data, 0);
  require(new TextEncoder().encode(JSON.stringify(data)).length <= 96000);
  const row = shape(data, ["panels"], ["columns"]);
  if (Object.hasOwn(row, "columns")) require([1, 2, 3].includes(row.columns as number));
  const panels = row.panels;
  require(Array.isArray(panels) && panels.length >= 1 && panels.length <= 8, "A dashboard holds one to eight panels.");
  const ids = panels.map((panel) => object(panel).id);
  ids.forEach(identifier);
  require(new Set(ids).size === ids.length, "Dashboard panel IDs must be unique.");
  const bindings = props.artifact.bindings;
  if (bindings !== undefined)
    require(Array.isArray(bindings) && bindings.length <= 8, "The host returned invalid dashboard data.");
}

function format(value: number, signed = false) {
  const formatted = new Intl.NumberFormat(undefined, {
    maximumFractionDigits: 2,
    notation: Math.abs(value) >= 1e6 ? "compact" : "standard",
  }).format(value);
  return signed && value > 0 ? "+" + formatted : formatted;
}
function metric(data: unknown): PanelView["metric"] {
  const row = shape(data, ["value"], ["unit", "delta", "caption"], "This metric is not valid.");
  finite(row.value, "Metric values must be finite numbers.");
  if (Object.hasOwn(row, "unit")) text(row.unit, 24, "This metric is not valid.");
  if (Object.hasOwn(row, "delta")) finite(row.delta, "Metric changes must be finite numbers.");
  if (Object.hasOwn(row, "caption")) text(row.caption, 240, "This metric is not valid.");
  const value = row as unknown as DashboardMetricData;
  const delta = value.delta;
  return {
    value: format(value.value),
    unit: value.unit,
    delta: delta === undefined ? undefined : format(delta, true),
    trend: lookup(TRENDS, delta === undefined || delta === 0 ? "flat" : delta > 0 ? "up" : "down") || "trend-flat",
    caption: value.caption,
  };
}
function binding(value: unknown) {
  const row = shape(value, ["source"], ["params"], "This panel's binding is not valid.");
  const source = row.source;
  require(
    typeof source === "string" && /^[a-z][a-z0-9_]{0,30}(\.[a-z0-9_]{1,30}){1,2}$/.test(source),
    "This panel's binding is not valid.",
  );
  // The same read-only, chat-scoped allowlist as the host (view_bindings.SOURCES).
  require(Object.hasOwn(SOURCES, source), "This data source is not available to dashboards.");
  if (Object.hasOwn(row, "params")) {
    const params = object(row.params, "This panel's binding is not valid.");
    const entries = Object.entries(params);
    require(entries.length <= 8, "This panel's binding is not valid.");
    entries.forEach(([key, item]) => {
      identifier(key);
      require(!/chat|path|file|dir|url/i.test(key), "Bindings read this conversation only; they cannot name a chat, file or address.");
      require(
        item === null || typeof item === "boolean" || (typeof item === "string" && item.length <= 120 && !/[\\/]|\.\./.test(item)) ||
          (typeof item === "number" && Number.isFinite(item)),
        "This panel's binding is not valid.",
      );
    });
  }
  return source;
}
function clock(seconds: number) {
  return new Date(seconds * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
}

function panelView(value: unknown): PanelView {
  const row = object(value);
  const id = row.id as string, named = row.title, kind = row.kind;
  const title = typeof named === "string" && named.trim() && named.length <= 120 ? named : "Untitled panel";
  const view: PanelView = { id, title, kind: typeof kind === "string" ? kind : "", live: Object.hasOwn(row, "binding") };
  try {
    require(
      Object.keys(row).every((key) => ["id", "title", "kind", "data", "binding"].includes(key)),
      "This panel has fields dashboards do not allow.",
    );
    text(named, 120, "Every panel needs a short title.");
    require(kind !== "form", "Dashboards cannot hold forms. A form opens as its own view.");
    require(typeof kind === "string" && PANEL_KINDS.has(kind), "This panel type is not supported.");
    require(
      Object.hasOwn(row, "data") !== Object.hasOwn(row, "binding"),
      "A panel needs either its own data or one host binding.",
    );
    let data = row.data;
    if (view.live) {
      const source = binding(row.binding);
      const matches = (props.artifact.bindings || []).filter(
        (item) => item && typeof item === "object" && (item as DashboardBindingResult).panel_id === id,
      );
      require(matches.length <= 1, "The host returned conflicting data for this panel.");
      if (!matches.length) return { ...view, waiting: true };
      const result = shape(matches[0], ["panel_id", "source"], ["as_of", "digest", "data", "error"], "The host returned invalid data for this panel.");
      require(result.source === source, "Host data does not match this panel's binding.");
      const failure = result.error, asOf = result.as_of, digest = result.digest;
      if (Object.hasOwn(result, "error")) {
        text(failure, 300, "The host returned invalid data for this panel.");
        return { ...view, error: "Live data unavailable: " + failure };
      }
      require(
        Object.hasOwn(result, "data") && typeof asOf === "number" && Number.isFinite(asOf) && asOf > 0 &&
          typeof digest === "string" && /^[A-Za-z0-9:_-]{8,140}$/.test(digest),
        "The host returned invalid data for this panel.",
      );
      data = result.data;
      view.stamp = {
        source: lookup(SOURCES, source) || "Host data",
        asOf: clock(asOf),
        digest: digest.replace(/^sha256:/, "").slice(0, 8),
      };
    }
    if (kind === "metric") return { ...view, metric: metric(data) };
    return {
      ...view,
      native: {
        id: `${props.artifact.id}.${id}`,
        chat_id: props.artifact.chat_id,
        title,
        created: props.artifact.created,
        revision: props.artifact.revision,
        status: props.artifact.status,
        kind,
        data,
      } as NativeArtifact,
    };
  } catch (error) {
    return { ...view, stamp: undefined, error: error instanceof Error ? error.message : "This panel cannot be displayed." };
  }
}

const validation = computed(() => {
  try {
    validateDashboard(props.artifact.data);
    return "";
  } catch (error) {
    return error instanceof Error ? error.message : "This dashboard cannot be displayed.";
  }
});
const panels = computed<PanelView[]>(() =>
  validation.value ? [] : props.artifact.data.panels.map(panelView),
);
const columns = computed(() => lookup(COLUMNS, String(props.artifact.data?.columns ?? 2)) || "columns-2");
const liveCount = computed(() => panels.value.filter((panel) => panel.live).length);
const updated = computed(() => {
  const times = (props.artifact.bindings || [])
    .map((item) => item?.as_of)
    .filter((value): value is number => typeof value === "number" && Number.isFinite(value) && value > 0);
  return times.length ? clock(Math.max(...times)) : "";
});
const inactive = computed(() => ["stopped", "expired"].includes(props.artifact.status || ""));
const refreshDisabled = computed(
  () => props.disabled || props.busy || props.historical || inactive.value || !liveCount.value,
);
function refresh() {
  if (!refreshDisabled.value) emit("refresh");
}
</script>

<template>
  <section class="dashboard" :aria-label="artifact.title">
    <header class="dash-header">
      <div>
        <span class="dash-eyebrow">Dashboard</span>
        <h2>{{ artifact.title }}</h2>
        <p v-if="!validation" class="dash-summary">
          {{ panels.length }} {{ panels.length === 1 ? "panel" : "panels" }}<template v-if="liveCount">
            · {{ liveCount }} live<template v-if="updated"> · updated {{ updated }}</template></template>
        </p>
      </div>
      <button
        v-if="!validation && liveCount"
        type="button"
        class="secondary-button dash-refresh"
        :disabled="refreshDisabled"
        @click="refresh"
      >
        <PhArrowClockwise :size="13" />{{ busy ? "Refreshing…" : "Refresh data" }}
      </button>
    </header>
    <p v-if="validation" class="dash-error" role="alert">{{ validation }}</p>
    <template v-else>
      <p v-if="historical" class="dash-note">Historical revision. Live data is shown as it was saved.</p>
      <p v-else-if="inactive" class="dash-note">This dashboard is {{ artifact.status }}. Its data is no longer refreshed.</p>
      <p v-else-if="disabled && liveCount" class="dash-note">Open the desktop app to refresh live data.</p>
      <p v-if="error" class="dash-error" role="alert">{{ error }}</p>
      <div class="dash-grid" :class="columns">
        <article
          v-for="panel in panels"
          :key="panel.id"
          class="dash-panel"
          :class="panel.kind === 'metric' ? 'metric' : 'wide'"
        >
          <header class="dash-panel-header">
            <h3>{{ panel.title }}</h3>
            <span v-if="panel.live" class="dash-tag live">Live</span>
            <span v-else class="dash-tag">Snapshot</span>
          </header>
          <p v-if="panel.error" class="dash-panel-error" role="alert">{{ panel.error }}</p>
          <p v-else-if="panel.waiting" class="dash-panel-note">
            Waiting for host data. Use Refresh data to load it.
          </p>
          <div v-else-if="panel.metric" class="dash-metric">
            <p class="dash-value">
              <strong>{{ panel.metric.value }}</strong><span v-if="panel.metric.unit">{{ panel.metric.unit }}</span>
            </p>
            <p v-if="panel.metric.delta" class="dash-delta" :class="panel.metric.trend">
              {{ panel.metric.delta }}
            </p>
            <p v-if="panel.metric.caption" class="dash-caption">{{ panel.metric.caption }}</p>
          </div>
          <NativeArtifactRenderer v-else-if="panel.native" :artifact="panel.native" panel historical disabled />
          <footer v-if="panel.stamp && !panel.error" class="dash-stamp">
            <i aria-hidden="true" />{{ panel.stamp.source }} · as of {{ panel.stamp.asOf }} ·
            <code>{{ panel.stamp.digest }}</code>
          </footer>
        </article>
      </div>
      <p class="dash-footnote">
        Live panels read counts from this conversation through the app. Snapshot panels show what Pebble wrote.
      </p>
    </template>
  </section>
</template>

<style scoped>
.dashboard {
  container-type: inline-size;
  min-width: 0;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.dash-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 14px;
  margin-bottom: 18px;
}
.dash-eyebrow {
  font-size: 10px;
  letter-spacing: 1.6px;
  text-transform: uppercase;
  color: var(--muted);
  font-weight: 600;
}
h2 {
  font-size: 23px;
  font-weight: 500;
  line-height: 1.3;
  letter-spacing: -0.5px;
  margin: 4px 0 6px;
}
.dash-summary {
  margin: 0;
  color: var(--secondary);
  font-size: 12px;
  font-variant-numeric: tabular-nums;
}
.dash-refresh {
  flex-shrink: 0;
  margin-top: 18px;
}
.dash-note,
.dash-footnote {
  color: var(--muted);
  font-size: 11px;
  margin: 0 0 14px;
}
.dash-footnote {
  margin: 18px 0 0;
}
.dash-error,
.dash-panel-error {
  color: var(--danger, #b64747);
  white-space: pre-wrap;
}
.dash-grid {
  display: grid;
  gap: 12px;
  grid-template-columns: minmax(0, 1fr);
}
.dash-grid.columns-2 {
  grid-template-columns: repeat(2, minmax(0, 1fr));
}
.dash-grid.columns-3 {
  grid-template-columns: repeat(3, minmax(0, 1fr));
}
.dash-grid .wide {
  grid-column: 1 / -1;
}
@container (max-width: 460px) {
  .dash-grid.columns-3 {
    grid-template-columns: repeat(2, minmax(0, 1fr));
  }
}
@container (max-width: 380px) {
  .dash-grid.columns-2,
  .dash-grid.columns-3 {
    grid-template-columns: minmax(0, 1fr);
  }
}
.dash-panel {
  min-width: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
  padding: 14px 16px;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 12px;
}
.dash-panel-header {
  display: flex;
  align-items: baseline;
  justify-content: space-between;
  gap: 10px;
}
h3 {
  margin: 0;
  font-size: 12px;
  font-weight: 550;
  color: var(--secondary);
  line-height: 1.4;
}
.dash-tag {
  flex-shrink: 0;
  font-size: 9px;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 999px;
  padding: 1px 7px;
}
.dash-tag.live {
  color: var(--accent);
  border-color: var(--accent-soft);
}
.dash-panel-note {
  margin: 0;
  color: var(--muted);
  font-size: 12px;
}
.dash-panel-error {
  margin: 0;
  font-size: 12px;
}
.dash-metric p {
  margin: 0;
}
.dash-value {
  display: flex;
  align-items: baseline;
  gap: 5px;
  font-variant-numeric: tabular-nums;
}
.dash-value strong {
  font-size: 30px;
  font-weight: 500;
  letter-spacing: -0.8px;
  line-height: 1.15;
  color: var(--text);
}
.dash-value span {
  color: var(--secondary);
  font-size: 13px;
}
.dash-delta {
  font-size: 11px;
  font-weight: 550;
  font-variant-numeric: tabular-nums;
  margin-top: 4px !important;
}
.dash-delta.trend-up {
  color: var(--accent);
}
.dash-delta.trend-down {
  color: var(--danger);
}
.dash-delta.trend-flat {
  color: var(--muted);
}
.dash-caption {
  color: var(--muted);
  font-size: 11px;
  margin-top: 6px !important;
}
.dash-stamp {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  margin-top: auto;
  padding-top: 8px;
  border-top: 1px solid var(--line);
  font-size: 10px;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.dash-stamp i {
  width: 6px;
  height: 6px;
  border-radius: 50%;
  background: var(--accent);
  margin-right: 2px;
}
.dash-stamp code {
  font-size: 10px;
}
</style>
