<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import VisualCatalog from "./VisualCatalog.vue";
import VisualChart from "./VisualChart.vue";
import type {
  ArtifactAnswers,
  ArtifactPrimitive,
  CatalogArtifact,
  NativeArtifact,
} from "../types";

const props = defineProps<{
  artifact: NativeArtifact;
  historical?: boolean;
  disabled?: boolean;
  // What a disabled form says instead of the default (Phase 30: the pet points to the workspace).
  disabledNote?: string;
  busy?: boolean;
  error?: string;
  // A dashboard panel: the panel header carries the title, and a form is never shown.
  panel?: boolean;
}>();
const emit = defineEmits<{ submit: [answers: ArtifactAnswers] }>();
const values = reactive<Record<string, string>>({});
const inputError = ref("");
const reserved = new Set(["__proto__", "prototype", "constructor", "__text"]);

function require(
  condition: unknown,
  message = "This artifact contains invalid display data.",
): asserts condition {
  if (!condition) throw new Error(message);
}
function object(value: unknown): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value));
  return value as Record<string, unknown>;
}
function shape(value: unknown, keys: string[], optional: string[] = []) {
  const row = object(value);
  require(
    keys.every((key) => Object.hasOwn(row, key)) &&
      Object.keys(row).every((key) => [...keys, ...optional].includes(key)),
  );
  return row;
}
function text(value: unknown, maximum = 240): asserts value is string {
  require(
    typeof value === "string" && !!value.trim() && value.length <= maximum,
  );
}
function identifier(value: unknown): asserts value is string {
  require(
    typeof value === "string" &&
      /^[A-Za-z0-9_-]{1,40}$/.test(value) &&
      !reserved.has(value),
  );
}
function list(value: unknown, minimum: number, maximum: number): unknown[] {
  require(
    Array.isArray(value) && value.length >= minimum && value.length <= maximum,
  );
  return value;
}
function unique(rows: unknown[]) {
  const ids = rows.map((row) => object(row).id);
  ids.forEach(identifier);
  require(new Set(ids).size === ids.length);
  return ids as string[];
}
function primitive(value: unknown) {
  require(
    value === null ||
      typeof value === "boolean" ||
      (typeof value === "string" && value.length <= 2000) ||
      (typeof value === "number" &&
        Number.isFinite(value) &&
        Math.abs(value) <= 1e100),
  );
}
function evidence(value: unknown) {
  const ids = list(value, 0, 32);
  ids.forEach((id) => text(id, 350));
  require(new Set(ids).size === ids.length);
}
function optionalText(
  row: Record<string, unknown>,
  key: string,
  maximum: number,
) {
  if (Object.hasOwn(row, key)) text(row[key], maximum);
}

// Phase 46 (1.9.29, D195, w25-genui-b): the visual catalog's shapes and the chart types, as artifact_schema checks them.
const CHART_TYPES = ["bar", "line", "area", "scatter", "stacked", "pie", "donut"];
const FORMATS = ["number", "percent", "compact"];
const TONES = ["neutral", "accent", "ok", "warn", "danger"];
const CATALOG = ["compare", "metrics", "timeline", "record"];
function finite(value: unknown) {
  require(typeof value === "number" && Number.isFinite(value) && Math.abs(value) <= 1e15);
}
function catalog(kind: string, data: Record<string, unknown>) {
  if (kind === "compare") {
    shape(data, ["options", "criteria"], ["recommended", "caption"]);
    const options = list(data.options, 2, 4), ids = unique(options);
    options.forEach((value) => {
      const option = shape(value, ["id", "label"], ["summary"]);
      text(option.label, 80);
      optionalText(option, "summary", 240);
    });
    list(data.criteria, 1, 10).forEach((value) => {
      const criterion = shape(value, ["label", "values"]);
      text(criterion.label, 80);
      list(criterion.values, options.length, options.length).forEach((cell) => {
        if (typeof cell === "number") finite(cell);
        else if (typeof cell !== "boolean") text(cell, 160);
      });
    });
    if (Object.hasOwn(data, "recommended")) require(ids.includes(data.recommended as string));
    optionalText(data, "caption", 400);
  } else if (kind === "metrics") {
    shape(data, ["tiles"], ["caption"]);
    list(data.tiles, 2, 4).forEach((value) => {
      const tile = shape(value, ["label", "value"], ["unit", "delta", "format", "better", "caption"]);
      text(tile.label, 60);
      finite(tile.value);
      if (Object.hasOwn(tile, "delta")) finite(tile.delta);
      if (Object.hasOwn(tile, "format")) require(FORMATS.includes(String(tile.format)));
      if (Object.hasOwn(tile, "better")) require(tile.better === "up" || tile.better === "down");
      optionalText(tile, "unit", 12);
      optionalText(tile, "caption", 120);
    });
    optionalText(data, "caption", 400);
  } else if (kind === "timeline") {
    shape(data, ["events"], ["caption"]);
    list(data.events, 2, 20).forEach((value) => {
      const event = shape(value, ["date", "title"], ["detail", "tone"]);
      require(typeof event.date === "string" && /^\d{4}(-\d{2}(-\d{2})?)?$/.test(event.date));
      text(event.title, 120);
      optionalText(event, "detail", 400);
      if (Object.hasOwn(event, "tone")) require(TONES.includes(String(event.tone)));
    });
    optionalText(data, "caption", 400);
  } else {
    shape(data, ["fields"], ["subtitle", "note"]);
    list(data.fields, 1, 12).forEach((value) => {
      const field = shape(value, ["label", "value"]);
      text(field.label, 60);
      if (typeof field.value === "number") finite(field.value);
      else text(field.value, 160);
    });
    optionalText(data, "subtitle", 80);
    optionalText(data, "note", 400);
  }
}

// Validate the display boundary even when a persisted or preview row is malformed.
// None of these values become markup, executable code, CSS, URLs, or RPC names.
function validate(artifact: NativeArtifact) {
  let nodes = 0;
  function json(value: unknown, depth = 0) {
    require(depth <= 8 && ++nodes <= 12000);
    if (Array.isArray(value)) {
      require(value.length <= 1000);
      value.forEach((item) => json(item, depth + 1));
    } else if (value !== null && typeof value === "object") {
      require(
        Object.getPrototypeOf(value) === Object.prototype ||
          Object.getPrototypeOf(value) === null,
      );
      const entries = Object.entries(value);
      require(entries.length <= 200);
      entries.forEach(([key, item]) => {
        require(key.length <= 120 && !reserved.has(key));
        json(item, depth + 1);
      });
    } else {
      require(
        value === null ||
          ["string", "boolean", "number"].includes(typeof value),
      );
      if (typeof value === "number")
        require(Number.isFinite(value) && Math.abs(value) <= 1e100);
    }
  }
  json(artifact.data);
  require(
    new TextEncoder().encode(JSON.stringify(artifact.data)).length <= 96000,
  );
  const data = object(artifact.data);
  if (artifact.kind === "form") {
    shape(data, ["fields"], ["description", "submit_label"]);
    const fields = list(data.fields, 1, 3);
    unique(fields);
    optionalText(data, "description", 1500);
    optionalText(data, "submit_label", 80);
    fields.forEach((value) => {
      const field = shape(
        value,
        ["id", "label", "type", "required"],
        ["options", "placeholder"],
      );
      text(field.label);
      require(field.required === true &&
        ["text", "choice"].includes(
          String(field.type),
        ), "Forms support one to three required text or choice fields.");
      optionalText(field, "placeholder", 240);
      if (field.type === "text") require(!Object.hasOwn(field, "options"));
      else {
        const options = list(field.options, 2, 6);
        unique(options);
        options.forEach((value) =>
          text(shape(value, ["id", "label"]).label, 120),
        );
      }
    });
  } else if (artifact.kind === "table") {
    shape(data, ["columns", "rows"], ["caption"]);
    const columns = list(data.columns, 1, 12),
      ids = unique(columns);
    columns.forEach((value) => text(shape(value, ["id", "label"]).label, 120));
    list(data.rows, 0, 200).forEach((row) =>
      Object.values(shape(row, ids)).forEach(primitive),
    );
    optionalText(data, "caption", 1000);
  } else if (artifact.kind === "chart") {
    shape(data, ["type", "x_key", "y_keys", "rows"], ["labels", "caption", "format", "unit"]);
    // Phase 46 (1.9.29, w25-genui-b): the catalog's chart types and number formats (artifact_schema.CHART_TYPES)
    require(CHART_TYPES.includes(String(data.type)));
    if (Object.hasOwn(data, "format")) require(FORMATS.includes(String(data.format)));
    optionalText(data, "unit", 12);
    identifier(data.x_key);
    const keys = list(data.y_keys, 1, 6);
    keys.forEach(identifier);
    require(new Set(keys).size === keys.length && !keys.includes(data.x_key));
    list(data.rows, 1, 200).forEach((value) => {
      const row = shape(value, [data.x_key as string, ...(keys as string[])]);
      primitive(row[data.x_key as string]);
      keys.forEach((key) =>
        require(typeof row[key as string] === "number" &&
          Number.isFinite(row[key as string]) &&
          Math.abs(row[key as string] as number) <=
            1e15, "Chart series must contain finite numbers."),
      );
    });
    if (Object.hasOwn(data, "labels"))
      Object.values(
        shape(data.labels, [], [data.x_key as string, ...(keys as string[])]),
      ).forEach((value) => text(value, 120));
    optionalText(data, "caption", 1000);
    const rows = data.rows as Record<string, unknown>[];
    if (data.type === "pie" || data.type === "donut")
      require(keys.length === 1 && rows.length >= 2 && rows.length <= 8 && rows.every((row) => (row[keys[0] as string] as number) >= 0));
    if (data.type === "scatter") rows.forEach((row) => require(typeof row[data.x_key as string] === "number"));
    if (data.type === "stacked") rows.forEach((row) => keys.forEach((key) => require((row[key as string] as number) >= 0)));
  } else if (CATALOG.includes(artifact.kind)) {
    catalog(artifact.kind, data);
  } else if (artifact.kind === "plan") {
    shape(data, ["steps"]);
    const steps = list(data.steps, 1, 20);
    unique(steps);
    let inProgress = 0;
    steps.forEach((value) => {
      const step = shape(value, ["id", "title", "status"], ["evidence_ids"]);
      text(step.title);
      require(
        ["pending", "in_progress", "blocked", "completed"].includes(
          String(step.status),
        ),
      );
      if (step.status === "in_progress") inProgress++;
      if (Object.hasOwn(step, "evidence_ids")) evidence(step.evidence_ids);
      if (step.status === "completed")
        require(
          Array.isArray(step.evidence_ids) && step.evidence_ids.length > 0,
        );
    });
    require(inProgress <= 1);
  } else if (artifact.kind === "evidence") {
    shape(data, ["items"], ["caption"]);
    const items = list(data.items, 1, 30);
    unique(items);
    items.forEach((value) => {
      const item = shape(value, ["id", "label", "evidence_id"], ["summary"]);
      text(item.label);
      evidence([item.evidence_id]);
      optionalText(item, "summary", 2000);
    });
    optionalText(data, "caption", 1000);
  } else throw new Error("Unsupported native artifact type.");
}
const validation = computed(() => {
  try {
    require(
      !props.panel || props.artifact.kind !== "form",
      "Dashboard panels cannot hold forms.",
    );
    validate(props.artifact);
    return "";
  } catch (error) {
    return error instanceof Error
      ? error.message
      : "This artifact cannot be displayed.";
  }
});
const form = computed(() =>
  !validation.value && props.artifact.kind === "form"
    ? props.artifact.data
    : null,
);
const table = computed(() =>
  !validation.value && props.artifact.kind === "table"
    ? props.artifact.data
    : null,
);
const chart = computed(() =>
  !validation.value && props.artifact.kind === "chart"
    ? props.artifact.data
    : null,
);
const plan = computed(() =>
  !validation.value && props.artifact.kind === "plan"
    ? props.artifact.data
    : null,
);
const receipts = computed(() =>
  !validation.value && props.artifact.kind === "evidence"
    ? props.artifact.data
    : null,
);
const visual = computed(() =>
  !validation.value && CATALOG.includes(props.artifact.kind)
    ? (props.artifact as CatalogArtifact)
    : null,
);
const questionStatus = computed(
  () => props.artifact.form_question?.status || "unavailable",
);
const formDisabled = computed(
  () =>
    props.disabled ||
    props.busy ||
    props.historical ||
    ["stopped", "expired"].includes(props.artifact.status || "") ||
    questionStatus.value !== "pending",
);
const complete = computed(
  () =>
    !!form.value &&
    form.value.fields.every((field) =>
      field.type === "text"
        ? !!values[field.id]?.trim()
        : field.options?.some((option) => option.id === values[field.id]),
    ),
);
watch(
  () =>
    JSON.stringify([
      props.artifact.id,
      props.artifact.revision,
      props.artifact.form_question?.status,
    ]),
  () => {
    Object.keys(values).forEach((key) => delete values[key]);
    inputError.value = "";
    const answers = props.artifact.form_question?.answer;
    form.value?.fields.forEach((field) => {
      const answer = answers?.[field.id];
      if (field.type === "choice") {
        const option =
          answer && typeof answer === "object" && "option_id" in answer
            ? answer.option_id
            : "";
        values[field.id] =
          typeof option === "string" &&
          field.options?.some((item) => item.id === option)
            ? option
            : "";
      } else {
        const saved =
          answer && typeof answer === "object" && "text" in answer
            ? answer.text
            : "";
        values[field.id] = typeof saved === "string" ? saved : "";
      }
    });
  },
  { immediate: true },
);
function submit() {
  if (!form.value || formDisabled.value) return;
  if (!complete.value) {
    inputError.value = "Complete every required field.";
    return;
  }
  const answers: ArtifactAnswers = Object.fromEntries(
    form.value.fields.map((field) => [
      field.id,
      field.type === "choice"
        ? { option_id: values[field.id] }
        : { text: values[field.id].trim() },
    ]),
  );
  inputError.value = "";
  emit("submit", answers);
}
function cell(value: ArtifactPrimitive | undefined) {
  return value === null || value === undefined ? "—" : String(value);
}
</script>

<template>
  <section class="native-artifact" :class="{ panel }" :aria-label="artifact.title">
    <h2 v-if="!panel">{{ artifact.title }}</h2>
    <p v-if="validation" class="native-error" role="alert">{{ validation }}</p>
    <template v-else>
      <form v-if="form" class="native-form" @submit.prevent="submit">
        <p v-if="form.description" class="native-description">
          {{ form.description }}
        </p>
        <p class="native-form-status" role="status">
          Form: {{ questionStatus }}
        </p>
        <p v-if="historical" class="native-note">
          Historical revision. Return to the current revision to answer.
        </p>
        <p
          v-else-if="
            artifact.status === 'stopped' || artifact.status === 'expired'
          "
          class="native-note"
        >
          This form is {{ artifact.status }}.
        </p>
        <p v-else-if="disabled" class="native-note">
          {{ disabledNote || "Open the desktop app to submit answers." }}
        </p>
        <p v-else-if="questionStatus === 'unavailable'" class="native-note">
          This form has no active input request.
        </p>
        <fieldset
          v-for="field in form.fields"
          :key="field.id"
          :disabled="formDisabled"
        >
          <legend>
            {{ field.label }} <span class="native-required">Required</span>
          </legend>
          <input
            v-if="field.type === 'text'"
            v-model="values[field.id]"
            type="text"
            required
            maxlength="2000"
            :aria-label="field.label"
            :placeholder="field.placeholder"
            autocomplete="off"
          />
          <label
            v-for="option in field.type === 'choice' ? field.options : []"
            :key="option.id"
            class="native-choice"
          >
            <input
              v-model="values[field.id]"
              type="radio"
              :name="artifact.id + ':' + artifact.revision + ':' + field.id"
              :value="option.id"
              required
            />
            <span>{{ option.label }}</span>
          </label>
        </fieldset>
        <p v-if="inputError || error" class="native-error" role="alert">
          {{ inputError || error }}
        </p>
        <button
          type="submit"
          class="primary-button"
          :disabled="formDisabled || !complete"
        >
          {{ busy ? "Saving…" : form.submit_label || "Submit answers" }}
        </button>
        <small class="native-note"
          >Answers become task input in this conversation.</small
        >
      </form>
      <div
        v-else-if="table"
        class="native-table-scroll"
        tabindex="0"
        aria-label="Scrollable table"
      >
        <table class="native-table">
          <caption v-if="table.caption">
            {{
              table.caption
            }}
          </caption>
          <thead>
            <tr>
              <th v-for="column in table.columns" :key="column.id" scope="col">
                {{ column.label }}
              </th>
            </tr>
          </thead>
          <tbody>
            <tr v-for="(row, index) in table.rows" :key="index">
              <td v-for="column in table.columns" :key="column.id">
                {{ cell(row[column.id]) }}
              </td>
            </tr>
          </tbody>
        </table>
        <p v-if="!table.rows.length" class="native-note">No rows.</p>
      </div>
      <!-- Phase 46 (1.9.29, w25-genui-b): every chart type is drawn by VisualChart (colours are fixed classes) -->
      <VisualChart v-else-if="chart" class="native-chart" :data="chart" :title="artifact.title" />
      <VisualCatalog v-else-if="visual" :kind="visual.kind" :data="visual.data" :title="artifact.title" />
      <ol v-else-if="plan" class="native-plan">
        <li v-for="step in plan.steps" :key="step.id">
          <strong>{{ step.title }}</strong
          ><span class="native-step-status" :class="step.status">{{
            step.status.replaceAll("_", " ")
          }}</span>
          <ul
            v-if="step.evidence_ids?.length"
            class="native-receipts"
            aria-label="Step evidence receipts"
          >
            <li v-for="id in step.evidence_ids" :key="id">
              <code>{{ id }}</code>
            </li>
          </ul>
        </li>
      </ol>
      <template v-else-if="receipts">
        <p v-if="receipts.caption" class="native-description">
          {{ receipts.caption }}
        </p>
        <ul class="native-evidence">
          <li v-for="item in receipts.items" :key="item.id">
            <strong>{{ item.label }}</strong>
            <p v-if="item.summary">{{ item.summary }}</p>
            <span class="native-note">Tool receipt</span
            ><code>{{ item.evidence_id }}</code>
          </li>
        </ul>
      </template>
    </template>
  </section>
</template>

<style scoped>
.native-artifact {
  font-size: 13px;
  line-height: 1.6;
  min-width: 0;
  overflow-wrap: anywhere;
}
h2 {
  font-size: 23px;
  font-weight: 500;
  line-height: 1.4;
  letter-spacing: -0.5px;
  margin: 0 0 18px;
}
.native-description,
figcaption {
  color: var(--secondary);
  margin: 0 0 16px;
  white-space: pre-wrap;
}
.native-note {
  color: var(--muted);
  font-size: 11px;
  display: block;
  margin: 10px 0;
}
.native-error {
  color: var(--danger, #b64747);
  white-space: pre-wrap;
}
.native-form-status {
  color: var(--secondary);
  font-size: 12px;
  text-transform: capitalize;
}
fieldset {
  min-width: 0;
  border: 0;
  margin: 18px 0;
  padding: 0;
}
legend {
  padding: 0;
  font-weight: 500;
  margin-bottom: 8px;
}
.native-required {
  color: var(--muted);
  font-size: 10px;
  font-weight: 400;
  margin-left: 5px;
}
input[type="text"] {
  width: 100%;
  box-sizing: border-box;
  padding: 10px 12px;
  border: 1px solid var(--line);
  border-radius: 8px;
  font: inherit;
  color: var(--text);
  background: var(--code);
}
input:disabled {
  opacity: 0.65;
}
.native-choice {
  display: flex;
  align-items: start;
  gap: 9px;
  margin: 10px 0;
}
.native-choice input {
  margin: 5px 0 0;
  accent-color: var(--accent);
}
.native-table-scroll {
  max-width: 100%;
  overflow: auto;
  margin-top: 12px;
}
.native-table {
  border-collapse: collapse;
  width: 100%;
  text-align: left;
  font-size: 12px;
}
.native-table caption {
  text-align: left;
  margin-bottom: 12px;
  color: var(--secondary);
  white-space: pre-wrap;
}
.native-table th,
.native-table td {
  border-bottom: 1px solid var(--line);
  padding: 10px 12px;
  vertical-align: top;
  white-space: pre-wrap;
  min-width: 70px;
  max-width: 320px;
}
.native-table th {
  font-weight: 600;
  background: var(--code);
}
.native-chart {
  margin: 0;
}
details {
  margin-top: 18px;
}
summary {
  cursor: pointer;
  font-size: 12px;
  color: var(--secondary);
}
.native-plan {
  padding-left: 22px;
}
.native-plan > li {
  padding: 10px 0 14px 5px;
  border-bottom: 1px solid var(--line);
}
.native-plan strong,
.native-evidence strong {
  display: block;
  font-weight: 500;
}
.native-step-status {
  font-size: 11px;
  color: var(--muted);
  text-transform: capitalize;
  display: block;
  margin-top: 4px;
}
.native-step-status.in_progress {
  color: var(--accent);
}
.native-step-status.blocked {
  color: var(--danger);
}
.native-step-status.completed {
  color: var(--accent);
}
.native-receipts {
  list-style: none;
  padding: 0;
  margin: 8px 0 0;
  font-size: 10px;
  color: var(--muted);
}
.native-evidence {
  padding: 0;
  list-style: none;
}
.native-evidence > li {
  padding: 16px 0;
  border-bottom: 1px solid var(--line);
}
.native-evidence p {
  margin: 8px 0;
  color: var(--secondary);
  white-space: pre-wrap;
}
.native-evidence code {
  display: block;
  font-size: 11px;
  color: var(--secondary);
}
code {
  overflow-wrap: anywhere;
  white-space: pre-wrap;
}
button:disabled {
  opacity: 0.55;
  cursor: default;
}
.panel {
  font-size: 12px;
}
.panel .native-table-scroll,
.panel .native-chart {
  margin-top: 4px;
}
.panel .native-plan > li,
.panel .native-evidence > li {
  padding: 8px 0 10px;
}
.panel .native-plan > li:last-child,
.panel .native-evidence > li:last-child {
  border-bottom: 0;
}
.panel details {
  margin-top: 8px;
}
</style>
