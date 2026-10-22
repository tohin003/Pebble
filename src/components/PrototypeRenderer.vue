<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import {
  PhArrowLeft,
  PhArrowCounterClockwise,
  PhChatCircleText,
} from "@phosphor-icons/vue";
import type {
  PrototypeArtifact,
  PrototypeComponent,
  PrototypeFeedback,
  PrototypeScreen,
} from "../types";

const props = defineProps<{
  artifact: PrototypeArtifact;
  historical?: boolean;
}>();
// Generated controls only move between screens of this demo. The one outward
// signal is the host "Send feedback" button, which carries the screen ID only.
const emit = defineEmits<{ feedback: [payload: PrototypeFeedback] }>();

const reserved = new Set(["__proto__", "prototype", "constructor", "__text"]);
// Fixed host tables: a theme token selects one of these entries or the default.
const VIEWPORTS: Record<string, string> = { phone: "frame-phone", tablet: "frame-tablet", desktop: "frame-desktop" };
const ACCENTS: Record<string, string> = {
  teal: "accent-teal", green: "accent-green", blue: "accent-blue", indigo: "accent-indigo", violet: "accent-violet",
  rose: "accent-rose", orange: "accent-orange", amber: "accent-amber", slate: "accent-slate",
};
const DENSITIES: Record<string, string> = { compact: "density-compact", cozy: "density-cozy", comfortable: "density-comfortable" };
const RADII: Record<string, string> = { 0: "radius-0", 4: "radius-4", 8: "radius-8", 12: "radius-12", 16: "radius-16" };
const ASPECTS: Record<string, string> = {
  "16:9": "aspect-16-9", "4:3": "aspect-4-3", "3:2": "aspect-3-2", "1:1": "aspect-1-1",
  "3:4": "aspect-3-4", "9:16": "aspect-9-16", "21:9": "aspect-21-9",
};
const COMPONENTS: Record<string, [string[], string[]]> = {
  heading: [["type", "text"], []],
  button: [["type", "label"], ["goto"]],
  input: [["type", "label"], ["placeholder"]],
  toggle: [["type", "label"], []],
  list: [["type", "items"], []],
  card: [["type", "title", "body"], []],
  image_placeholder: [["type", "label"], ["aspect"]],
  nav: [["type", "items"], []],
};
function token(table: Record<string, string>, key: unknown, fallback: string) {
  return (typeof key === "string" || typeof key === "number") && Object.hasOwn(table, String(key))
    ? table[String(key)]
    : table[fallback];
}

function require(condition: unknown, message = "This app demo contains invalid display data."): asserts condition {
  if (!condition) throw new Error(message);
}
function object(value: unknown, message?: string): Record<string, unknown> {
  require(value !== null && typeof value === "object" && !Array.isArray(value), message);
  return value as Record<string, unknown>;
}
const unsupported = "This app demo has fields that are not allowed. Links, scripts, styles and actions are not supported.";
function shape(value: unknown, keys: string[], optional: string[] = []) {
  const row = object(value);
  require(keys.every((key) => Object.hasOwn(row, key)), "This app demo is missing a required field.");
  require(Object.keys(row).every((key) => [...keys, ...optional].includes(key)), unsupported);
  return row;
}
function text(value: unknown, maximum: number) {
  require(typeof value === "string" && !!value.trim() && value.length <= maximum, "This app demo has an invalid label.");
}
function list(value: unknown, minimum: number, maximum: number): unknown[] {
  require(Array.isArray(value) && value.length >= minimum && value.length <= maximum);
  return value;
}

// Every field is checked against an exact allowlist, so href, url, src, action,
// on*, style, class, html and script never reach the template.
function validate(data: unknown) {
  let nodes = 0;
  (function walk(value: unknown, depth: number) {
    require(depth <= 8 && ++nodes <= 12000);
    if (Array.isArray(value)) value.forEach((item) => walk(item, depth + 1));
    else if (value !== null && typeof value === "object") {
      require(Object.getPrototypeOf(value) === Object.prototype || Object.getPrototypeOf(value) === null);
      Object.entries(value).forEach(([key, item]) => {
        require(!reserved.has(key), unsupported);
        walk(item, depth + 1);
      });
    } else require(value === null || ["string", "boolean", "number"].includes(typeof value));
  })(data, 0);
  require(new TextEncoder().encode(JSON.stringify(data)).length <= 96000);
  const row = shape(data, ["start", "screens"], ["viewport", "theme"]);
  const viewport = row.viewport;
  if (Object.hasOwn(row, "viewport")) require(typeof viewport === "string" && viewport.length <= 20);
  if (Object.hasOwn(row, "theme")) {
    const theme = shape(row.theme, [], ["accent", "density", "radius"]);
    for (const key of ["accent", "density"]) {
      const value = theme[key];
      if (Object.hasOwn(theme, key)) require(typeof value === "string" && value.length <= 20);
    }
    if (Object.hasOwn(theme, "radius")) require(typeof theme.radius === "number" && Number.isFinite(theme.radius));
  }
  const screens = list(row.screens, 1, 12);
  const ids = screens.map((screen) => object(screen).id);
  require(
    ids.every((id) => typeof id === "string" && /^[A-Za-z0-9_-]{1,64}$/.test(id) && !reserved.has(id)) &&
      new Set(ids).size === ids.length,
    "App demo screens need unique, simple IDs.",
  );
  require(ids.includes(row.start), "The start screen does not exist.");
  const target = (value: unknown) =>
    require(typeof value === "string" && ids.includes(value), "A control points to a screen that does not exist.");
  screens.forEach((value) => {
    const screen = shape(value, ["id", "title", "components"]);
    text(screen.title, 120);
    list(screen.components, 0, 30).forEach((value) => {
      const type = object(value).type;
      require(typeof type === "string" && Object.hasOwn(COMPONENTS, type), "This app demo uses a component that is not supported.");
      const item = shape(value, ...COMPONENTS[type]);
      if (type === "heading") text(item.text, 240);
      else if (type === "card") {
        text(item.title, 240);
        text(item.body, 2000);
      } else if (type === "list") list(item.items, 0, 30).forEach((entry) => text(entry, 240));
      else if (type === "nav")
        list(item.items, 1, 8).forEach((entry) => {
          const link = shape(entry, ["label", "goto"]);
          text(link.label, 240);
          target(link.goto);
        });
      else {
        const aspect = item.aspect;
        text(item.label, 240);
        if (Object.hasOwn(item, "goto")) target(item.goto);
        if (Object.hasOwn(item, "placeholder")) text(item.placeholder, 240);
        if (Object.hasOwn(item, "aspect")) require(typeof aspect === "string" && aspect.length <= 8);
      }
    });
  });
}
const validation = computed(() => {
  try {
    validate(props.artifact.data);
    return "";
  } catch (error) {
    return error instanceof Error ? error.message : "This app demo cannot be displayed.";
  }
});

const screens = computed<PrototypeScreen[]>(() => (validation.value ? [] : props.artifact.data.screens));
const screenId = ref("");
const visited = ref<string[]>([]);
// Inputs and switches are ephemeral: never saved, sent or turned into task data.
const fields = reactive<Record<string, string>>({});
const toggles = reactive<Record<string, boolean>>({});
const screen = computed(() => screens.value.find((item) => item.id === screenId.value) || null);
const position = computed(() => screens.value.findIndex((item) => item.id === screenId.value) + 1);
const items = computed(() =>
  (screen.value?.components || []).map((component: PrototypeComponent, index) => ({
    component,
    key: `${screenId.value}:${index}`,
  })),
);
const body = computed(() => items.value.filter((item) => item.component.type !== "nav"));
const navs = computed(() => items.value.filter((item) => item.component.type === "nav"));
const theme = computed(() => (validation.value ? undefined : props.artifact.data.theme));
const frame = computed(() => [
  token(VIEWPORTS, validation.value ? "" : props.artifact.data.viewport, "phone"),
  token(ACCENTS, theme.value?.accent, "teal"),
  token(DENSITIES, theme.value?.density, "cozy"),
  token(RADII, typeof theme.value?.radius === "number" ? theme.value.radius : "", "8"),
]);
const aspect = (value: unknown) => token(ASPECTS, value, "16:9");

watch(
  () => JSON.stringify([props.artifact.chat_id, props.artifact.id, props.artifact.revision, validation.value]),
  () => {
    screenId.value = validation.value ? "" : props.artifact.data.start;
    visited.value = [];
    Object.keys(fields).forEach((key) => delete fields[key]);
    Object.keys(toggles).forEach((key) => delete toggles[key]);
  },
  { immediate: true },
);

function go(target: unknown) {
  if (typeof target !== "string" || target === screenId.value || !screens.value.some((item) => item.id === target)) return;
  visited.value = [...visited.value.slice(-31), screenId.value];
  screenId.value = target;
}
function back() {
  const previous = visited.value.at(-1);
  if (previous === undefined) return;
  visited.value = visited.value.slice(0, -1);
  screenId.value = previous;
}
function restart() {
  if (validation.value) return;
  screenId.value = props.artifact.data.start;
  visited.value = [];
  Object.keys(fields).forEach((key) => delete fields[key]);
  Object.keys(toggles).forEach((key) => delete toggles[key]);
}
function pick(event: Event) {
  go((event.target as HTMLSelectElement | null)?.value);
}
function flip(key: string) {
  toggles[key] = !toggles[key];
}
function feedback() {
  if (validation.value || !screen.value) return;
  emit("feedback", {
    chat_id: props.artifact.chat_id,
    artifact_id: props.artifact.id,
    revision: props.artifact.revision || 1,
    screen_id: screen.value.id,
  });
}
</script>

<template>
  <section class="prototype" :aria-label="artifact.title">
    <header class="proto-header">
      <span class="proto-eyebrow">App demo</span>
      <h2>{{ artifact.title }}</h2>
    </header>
    <p v-if="validation" class="proto-error" role="alert">{{ validation }}</p>
    <template v-else-if="screen">
      <p class="proto-banner">Interactive mock-up · controls only navigate this demo</p>
      <div class="proto-toolbar" role="toolbar" aria-label="Demo controls">
        <button type="button" class="icon-button" aria-label="Previous screen" :disabled="!visited.length" @click="back">
          <PhArrowLeft :size="15" />
        </button>
        <label class="proto-pick">
          <span>Screen</span>
          <select :value="screenId" aria-label="Go to screen" @change="pick">
            <option v-for="item in screens" :key="item.id" :value="item.id">{{ item.title }}</option>
          </select>
        </label>
        <span class="proto-position">{{ position }} of {{ screens.length }}</span>
        <button type="button" class="icon-button" aria-label="Restart demo" @click="restart">
          <PhArrowCounterClockwise :size="15" />
        </button>
      </div>
      <div class="proto-stage">
        <div class="proto-device" :class="frame" role="region" :aria-label="'Demo screen: ' + screen.title">
          <div class="proto-appbar">{{ screen.title }}</div>
          <div class="proto-screen">
            <template v-for="{ component, key } in body" :key="key">
              <h3 v-if="component.type === 'heading'" class="proto-heading">{{ component.text }}</h3>
              <button
                v-else-if="component.type === 'button'"
                type="button"
                class="proto-button"
                :class="{ inert: !component.goto }"
                @click="go(component.goto)"
              >
                {{ component.label }}
              </button>
              <label v-else-if="component.type === 'input'" class="proto-field">
                <span>{{ component.label }}</span>
                <input v-model="fields[key]" type="text" maxlength="200" autocomplete="off" :placeholder="component.placeholder" />
              </label>
              <button
                v-else-if="component.type === 'toggle'"
                type="button"
                role="switch"
                class="proto-toggle"
                :aria-checked="toggles[key] ? 'true' : 'false'"
                @click="flip(key)"
              >
                <span>{{ component.label }}</span><i aria-hidden="true" />
              </button>
              <ul v-else-if="component.type === 'list'" class="proto-list">
                <li v-for="(entry, index) in component.items" :key="index">{{ entry }}</li>
              </ul>
              <article v-else-if="component.type === 'card'" class="proto-card">
                <h4>{{ component.title }}</h4>
                <p>{{ component.body }}</p>
              </article>
              <div
                v-else-if="component.type === 'image_placeholder'"
                class="proto-image"
                :class="aspect(component.aspect)"
                role="img"
                :aria-label="'Image placeholder: ' + component.label"
              >
                <span>{{ component.label }}</span>
              </div>
            </template>
          </div>
          <template v-for="{ component, key } in navs" :key="key">
            <nav v-if="component.type === 'nav'" class="proto-tabs" aria-label="Demo navigation">
              <button
                v-for="(link, index) in component.items"
                :key="index"
                type="button"
                :aria-current="link.goto === screenId ? 'page' : undefined"
                @click="go(link.goto)"
              >
                {{ link.label }}
              </button>
            </nav>
          </template>
        </div>
      </div>
      <footer class="proto-footer">
        <p>Typing and switches stay in this preview; nothing is saved or sent.</p>
        <button type="button" class="secondary-button" @click="feedback">
          <PhChatCircleText :size="14" />Send feedback
        </button>
      </footer>
    </template>
  </section>
</template>

<style scoped>
.prototype {
  min-width: 0;
  font-size: 13px;
  line-height: 1.55;
  overflow-wrap: anywhere;
}
.proto-eyebrow {
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
  margin: 4px 0 14px;
}
.proto-error {
  color: var(--danger, #b64747);
  white-space: pre-wrap;
}
.proto-banner {
  margin: 0 0 10px;
  padding: 8px 12px;
  border-radius: 9px;
  background: var(--soft);
  border: 1px solid var(--line);
  color: var(--secondary);
  font-size: 11px;
}
.proto-toolbar {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 12px;
  font-size: 11px;
  color: var(--muted);
}
.proto-pick {
  display: flex;
  align-items: center;
  gap: 6px;
  flex: 1;
  min-width: 0;
}
.proto-pick select {
  min-width: 0;
  flex: 1;
  max-width: 220px;
  padding: 5px 8px;
  font: inherit;
  color: var(--text);
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: 6px;
}
.proto-position {
  font-variant-numeric: tabular-nums;
}
.proto-stage {
  display: flex;
  justify-content: center;
  padding: 22px 14px;
  background: var(--soft);
  border-radius: 14px;
}
.proto-device {
  --proto-accent: #2f7f7a;
  --proto-gap: 12px;
  --proto-pad: 16px;
  --proto-radius: 8px;
  display: flex;
  flex-direction: column;
  width: 100%;
  background: var(--panel);
  color: var(--text);
  border: 1px solid var(--line-strong);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.frame-phone {
  max-width: 340px;
  min-height: 540px;
  border-radius: 28px;
}
.frame-tablet {
  max-width: 560px;
  min-height: 460px;
  border-radius: 20px;
}
.frame-desktop {
  min-height: 400px;
  border-radius: 10px;
}
.accent-teal { --proto-accent: #2f7f7a; }
.accent-green { --proto-accent: #3f7a4a; }
.accent-blue { --proto-accent: #3b6fb6; }
.accent-indigo { --proto-accent: #4f5bb3; }
.accent-violet { --proto-accent: #7056a8; }
.accent-rose { --proto-accent: #b0476a; }
.accent-orange { --proto-accent: #b8612a; }
.accent-amber { --proto-accent: #95660f; }
.accent-slate { --proto-accent: #4d5a66; }
.density-compact { --proto-gap: 8px; --proto-pad: 12px; }
.density-cozy { --proto-gap: 12px; --proto-pad: 16px; }
.density-comfortable { --proto-gap: 16px; --proto-pad: 22px; }
.radius-0 { --proto-radius: 0; }
.radius-4 { --proto-radius: 4px; }
.radius-8 { --proto-radius: 8px; }
.radius-12 { --proto-radius: 12px; }
.radius-16 { --proto-radius: 16px; }
.proto-appbar {
  padding: 14px var(--proto-pad) 12px;
  font-size: 13px;
  font-weight: 600;
  border-bottom: 1px solid var(--line);
}
.frame-phone .proto-appbar {
  padding-top: 22px;
  text-align: center;
}
.proto-screen {
  display: flex;
  flex: 1;
  flex-direction: column;
  gap: var(--proto-gap);
  padding: var(--proto-pad);
  overflow: auto;
}
.proto-heading {
  margin: 4px 0 0;
  font-size: 20px;
  font-weight: 600;
  letter-spacing: -0.4px;
  line-height: 1.3;
}
.proto-button {
  align-self: stretch;
  padding: 11px 14px;
  border-radius: var(--proto-radius);
  background: var(--proto-accent);
  color: #fff;
  font-size: 13px;
  font-weight: 550;
}
.frame-desktop .proto-button,
.frame-tablet .proto-button {
  align-self: flex-start;
}
.proto-button.inert {
  cursor: default;
}
.proto-field {
  display: flex;
  flex-direction: column;
  gap: 5px;
  font-size: 11px;
  color: var(--secondary);
}
.proto-field input {
  padding: 10px 12px;
  font: inherit;
  font-size: 13px;
  color: var(--text);
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: var(--proto-radius);
}
.proto-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  padding: 4px 0;
  text-align: left;
  font-size: 13px;
}
.proto-toggle i {
  position: relative;
  flex-shrink: 0;
  width: 36px;
  height: 21px;
  border-radius: 999px;
  background: var(--line-strong);
  transition: background 0.15s;
}
.proto-toggle i::after {
  content: "";
  position: absolute;
  top: 2px;
  left: 2px;
  width: 17px;
  height: 17px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 2px #0003;
  transition: transform 0.15s;
}
.proto-toggle[aria-checked="true"] i {
  background: var(--proto-accent);
}
.proto-toggle[aria-checked="true"] i::after {
  transform: translateX(15px);
}
.proto-list {
  list-style: none;
  margin: 0;
  padding: 0;
  border: 1px solid var(--line);
  border-radius: var(--proto-radius);
}
.proto-list li {
  padding: 10px 12px;
  border-bottom: 1px solid var(--line);
}
.proto-list li:last-child {
  border-bottom: 0;
}
.proto-card {
  padding: 12px 14px;
  border: 1px solid var(--line);
  border-radius: var(--proto-radius);
  background: var(--code);
}
.proto-card h4 {
  margin: 0 0 4px;
  font-size: 13px;
  font-weight: 600;
}
.proto-card p {
  margin: 0;
  color: var(--secondary);
  white-space: pre-wrap;
}
.proto-image {
  display: flex;
  align-items: center;
  justify-content: center;
  border-radius: var(--proto-radius);
  border: 1px dashed var(--line-strong);
  background: repeating-linear-gradient(135deg, var(--code) 0 10px, var(--soft) 10px 20px);
  color: var(--muted);
  font-size: 11px;
}
.aspect-16-9 { aspect-ratio: 16 / 9; }
.aspect-4-3 { aspect-ratio: 4 / 3; }
.aspect-3-2 { aspect-ratio: 3 / 2; }
.aspect-1-1 { aspect-ratio: 1 / 1; }
.aspect-3-4 { aspect-ratio: 3 / 4; }
.aspect-9-16 { aspect-ratio: 9 / 16; max-height: 360px; }
.aspect-21-9 { aspect-ratio: 21 / 9; }
.proto-tabs {
  display: flex;
  border-top: 1px solid var(--line);
  background: var(--panel);
}
.frame-desktop .proto-tabs {
  order: -1;
  border-top: 0;
  border-bottom: 1px solid var(--line);
  justify-content: flex-start;
}
.proto-tabs button {
  flex: 1;
  padding: 12px 6px 14px;
  font-size: 11px;
  color: var(--muted);
}
.frame-desktop .proto-tabs button {
  flex: 0 0 auto;
  padding: 10px 14px;
}
.proto-tabs button[aria-current="page"] {
  color: var(--proto-accent);
  font-weight: 600;
}
.proto-footer {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 12px;
  margin-top: 14px;
}
.proto-footer p {
  margin: 0;
  color: var(--muted);
  font-size: 11px;
}
.proto-footer button {
  flex-shrink: 0;
}
</style>
