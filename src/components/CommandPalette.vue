<script setup lang="ts">
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { rpc } from "../bridge";
import { keysLabel, shortcutsFor, type ShortcutId, type Surface } from "../shortcuts";

// Commands come only from the host catalogue (command_catalogue); none are
// hardcoded here. Choosing one inserts its text into the message box, and the
// person sends it; actions reuse the shared shortcut registry handlers.
interface Command {
  name: string;
  arguments: string;
  description: string;
}
interface Entry {
  key: string;
  title: string;
  detail: string;
  keys?: string;
  insert?: string;
  action?: ShortcutId;
  run?: string;
}
// Phase 45 (F024): the window may add its own launcher entries (themes, Stealth Lockdown, Settings tabs, the
// companion, conversations); they run at once, ahead of the slash commands that only fill the message box.
export interface Extra {
  key: string;
  title: string;
  detail: string;
  run: string;
  words?: string;
  recent?: boolean;
}
const props = withDefaults(
  defineProps<{ surface: Surface; platform?: string; running?: boolean; extras?: Extra[] }>(),
  { platform: "darwin", running: false, extras: () => [] },
);
const emit = defineEmits<{ close: []; choose: [text: string]; action: [id: ShortcutId]; run: [id: string] }>();
const NAME = /^[a-z][a-z0-9_-]{0,31}$/;
const MAX_COMMANDS = 200;
const uid = "command-palette"; // at most one palette per window
const field = ref<HTMLInputElement | null>(null);
const query = ref("");
const index = ref(0);
const catalogue = ref<Command[]>([]);
const omitted = ref(0);
const loading = ref(true);
const error = ref("");
let alive = true;
let previous: HTMLElement | null = null;

function valid(item: unknown): item is Command {
  if (!item || typeof item !== "object" || Array.isArray(item)) return false;
  const value = item as Record<string, unknown>;
  return (
    typeof value.name === "string" &&
    NAME.test(value.name) &&
    typeof value.arguments === "string" &&
    value.arguments.length <= 160 &&
    typeof value.description === "string" &&
    value.description.length <= 400
  );
}
async function load() {
  loading.value = true;
  error.value = "";
  try {
    const result = await rpc<unknown>("command_catalogue");
    if (!alive) return;
    if (!Array.isArray(result)) throw new Error("The command catalogue was malformed.");
    const seen = new Set<string>();
    const commands: Command[] = [];
    for (const item of result) {
      if (!valid(item) || seen.has(item.name) || commands.length >= MAX_COMMANDS) continue;
      seen.add(item.name);
      commands.push({ name: item.name, arguments: item.arguments, description: item.description });
    }
    catalogue.value = commands;
    omitted.value = result.length - commands.length;
  } catch (e) {
    if (alive) error.value = "Commands are unavailable: " + (e instanceof Error ? e.message : String(e));
  } finally {
    if (alive) loading.value = false;
  }
}
const actions = computed<Entry[]>(() =>
  shortcutsFor(props.surface)
    .filter((item) => item.scope === "window" && !["palette", "close"].includes(item.id))
    .filter((item) => item.id !== "stop" || props.running)
    .map((item) => ({
      key: "action:" + item.id,
      title: item.label,
      detail: "Action",
      keys: keysLabel(item, props.platform),
      action: item.id,
    })),
);
const entries = computed<Entry[]>(() => {
  const needle = query.value.trim().replace(/^\//, "").toLowerCase();
  const ranked: { rank: number; order: number; entry: Entry }[] = [];
  // Phase 45 (F024): launcher entries first (rank -1 before the commands' 0); with no words typed only the actions
  // and the most recent conversations show, so the list stays short.
  props.extras.forEach((extra, order) => {
    const title = extra.title.toLowerCase(), words = (extra.words || "").toLowerCase();
    const rank = !needle ? (extra.recent === false ? -9 : -1)
      : title.startsWith(needle) ? -1
      : title.includes(needle) || words.includes(needle) ? -0.5
      : extra.detail.toLowerCase().includes(needle) ? 2.5 : -9;
    if (rank > -9) ranked.push({ rank, order, entry: { key: "extra:" + extra.key, title: extra.title, detail: extra.detail, run: extra.run } });
  });
  catalogue.value.forEach((command, order) => {
    const entry = {
      key: "command:" + command.name,
      title: "/" + command.name + (command.arguments ? " " + command.arguments : ""),
      detail: command.description,
      insert: "/" + command.name + (command.arguments ? " " : ""),
    };
    const rank = !needle || command.name.startsWith(needle) ? 0
      : command.name.includes(needle) ? 1
      : command.description.toLowerCase().includes(needle) ? 2 : -1;
    if (rank >= 0) ranked.push({ rank, order, entry });
  });
  actions.value.forEach((entry, order) => {
    if (!needle || entry.title.toLowerCase().includes(needle))
      ranked.push({ rank: 3, order, entry });
  });
  return ranked.sort((a, b) => a.rank - b.rank || a.order - b.order).map((item) => item.entry);
});
const active = computed(() => entries.value[index.value]);
watch(query, () => (index.value = 0));

function choose(entry: Entry | undefined) {
  if (!entry) return;
  if (entry.run) emit("run", entry.run);
  else if (entry.action) emit("action", entry.action);
  else if (entry.insert) emit("choose", entry.insert);
}
function move(step: number) {
  const count = entries.value.length;
  if (count) index.value = (index.value + step + count) % count;
}
function key(event: KeyboardEvent) {
  if (event.key === "ArrowDown" || event.key === "ArrowUp") {
    event.preventDefault();
    move(event.key === "ArrowDown" ? 1 : -1);
  } else if (event.key === "Enter" && !event.isComposing) {
    event.preventDefault();
    choose(active.value);
  } else if (event.key === "Escape") {
    // The window handlers must not also close the surrounding panel.
    event.preventDefault();
    event.stopPropagation();
    emit("close");
  } else if (event.key === "Tab") {
    event.preventDefault();
  }
}
onMounted(async () => {
  if (typeof document !== "undefined") previous = document.activeElement as HTMLElement | null;
  load();
  await nextTick();
  field.value?.focus();
});
onUnmounted(() => {
  alive = false;
  if (previous && typeof previous.focus === "function") previous.focus();
});
</script>

<template>
  <div class="palette-backdrop" @mousedown.self="emit('close')">
    <section class="palette" role="dialog" aria-modal="true" aria-label="Command palette">
      <input
        ref="field"
        v-model="query"
        class="palette-input"
        role="combobox"
        aria-autocomplete="list"
        aria-label="Search commands and actions"
        :aria-expanded="entries.length > 0"
        :aria-controls="uid + '-options'"
        :aria-activedescendant="active ? uid + '-' + index : undefined"
        :placeholder="extras.length ? 'Search actions, settings, conversations and commands…' : 'Type a command or action…'"
        maxlength="80"
        autocomplete="off"
        spellcheck="false"
        @keydown="key"
      />
      <p v-if="loading" class="palette-note" role="status">Loading commands…</p>
      <p v-if="error" class="palette-error" role="alert">{{ error }}</p>
      <ul :id="uid + '-options'" class="palette-options" role="listbox" aria-label="Commands and actions">
        <li
          v-for="(entry, i) in entries"
          :id="uid + '-' + i"
          :key="entry.key"
          role="option"
          :aria-selected="i === index"
          :class="{ active: i === index }"
          @mousedown.prevent="choose(entry)"
          @mousemove="index = i"
        >
          <span><strong>{{ entry.title }}</strong><small>{{ entry.detail }}</small></span>
          <kbd v-if="entry.keys">{{ entry.keys }}</kbd>
        </li>
      </ul>
      <p v-if="!loading && !entries.length" class="palette-note">No matching command or action.</p>
      <p v-if="omitted" class="palette-note">{{ omitted }} catalogue {{ omitted === 1 ? "entry was" : "entries were" }} malformed or duplicated and {{ omitted === 1 ? "is" : "are" }} not shown.</p>
      <p v-if="extras.length" class="palette-note">
        Actions run at once. A slash command ({{ catalogue.length }} from this Pebble service) goes into the message box and runs when you send it.
      </p>
      <p v-else class="palette-note">
        {{ catalogue.length }} commands from this Pebble service. Enter puts a command in the message box; nothing runs until you send it.
      </p>
    </section>
  </div>
</template>

<style scoped>
.palette-backdrop { position: fixed; inset: 0; z-index: 40; display: flex; justify-content: center; align-items: flex-start; padding-top: 12vh; background: #0003; }
.palette { width: min(560px, calc(100% - 24px)); max-height: 70vh; display: flex; flex-direction: column; background: var(--surface); color: var(--text); border: 1px solid var(--border); border-radius: 14px; box-shadow: 0 12px 40px #0004; overflow: hidden; }
.palette-input { margin: 10px; padding: 10px 12px; font: inherit; font-size: 14px; color: inherit; background: var(--hover); border: 1px solid var(--border); border-radius: 9px; }
.palette-options { list-style: none; margin: 0; padding: 0 6px 6px; overflow: auto; min-height: 0; }
.palette-options li { display: flex; align-items: center; gap: 10px; padding: 8px 10px; border-radius: 8px; cursor: pointer; font-size: 12px; }
.palette-options li.active { background: var(--hover); outline: 2px solid var(--accent); outline-offset: -2px; }
.palette-options span { flex: 1; min-width: 0; }
.palette-options strong { display: block; font-weight: 600; overflow-wrap: anywhere; }
.palette-options small { display: block; color: var(--muted); margin-top: 2px; }
.palette-options kbd { font: inherit; font-size: 11px; color: var(--muted); border: 1px solid var(--border); border-radius: 5px; padding: 1px 6px; }
.palette-note, .palette-error { margin: 0; padding: 6px 14px; font-size: 11px; color: var(--muted); }
.palette-error { color: var(--danger); }
</style>
