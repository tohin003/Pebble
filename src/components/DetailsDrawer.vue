<script lang="ts">
// Width bounds and the remembered drawer state (localStorage; any storage failure keeps defaults).
export const DRAWER = Object.freeze({ min: 320, max: 520, width: 360, step: 16 });
export interface DrawerTab {
  id: string;
  label: string;
  badge?: number;
}
export interface DrawerPrefs {
  open: boolean;
  tab: string;
  width: number;
}
export function clampWidth(width: unknown, viewport = Infinity): number {
  const room = Number.isFinite(viewport) ? Math.max(DRAWER.min, Math.floor(viewport * 0.6)) : DRAWER.max;
  const value = typeof width === "number" && Number.isFinite(width) ? width : DRAWER.width;
  return Math.round(Math.min(Math.max(value, DRAWER.min), Math.min(DRAWER.max, room)));
}
export function readDrawer(storage: Pick<Storage, "getItem"> | null | undefined, key: string, tabs: readonly string[]): DrawerPrefs {
  const fallback: DrawerPrefs = { open: false, tab: tabs[0] || "", width: DRAWER.width };
  try {
    const raw = storage?.getItem(key);
    if (!raw || raw.length > 400) return fallback;
    const value = JSON.parse(raw) as Record<string, unknown>;
    if (!value || typeof value !== "object") return fallback;
    return {
      open: value.open === true,
      tab: typeof value.tab === "string" && tabs.includes(value.tab) ? value.tab : fallback.tab,
      width: clampWidth(value.width),
    };
  } catch {
    return fallback;
  }
}
export function writeDrawer(storage: Pick<Storage, "setItem"> | null | undefined, key: string, prefs: DrawerPrefs): boolean {
  try {
    storage?.setItem(key, JSON.stringify({ open: !!prefs.open, tab: String(prefs.tab).slice(0, 32), width: clampWidth(prefs.width) }));
    return !!storage;
  } catch {
    return false;
  }
}
</script>

<script setup lang="ts">
import { computed, nextTick, onUnmounted, ref } from "vue";

// The main window's Details drawer (Phase 22): the run inspector, goal, schedules, project tools,
// windows and Pebble's browser live here instead of in the chat column. This is chrome only:
// each tab's content is a slot the window fills with its existing panels, and only the selected
// tab is mounted, so hidden panels make no requests. Tabs with nothing to show are not passed in;
// the window lists them under "More" instead (choosing one emits `reveal`), and with no tab at all
// the `empty` slot is the whole panel. Tabs follow the WAI-ARIA tabs pattern; the edge is a
// keyboard-operable separator. It emits; it calls no route.
const props = withDefaults(
  defineProps<{ tabs: DrawerTab[]; tab: string; width?: number; overlay?: boolean; viewport?: number; more?: DrawerTab[] }>(),
  { width: DRAWER.width, overlay: false, viewport: Infinity, more: () => [] },
);
const emit = defineEmits<{ "update:tab": [id: string]; "update:width": [width: number]; close: []; reveal: [id: string] }>();
const menu = ref(false);
const menuItems: (HTMLElement | null)[] = [];
const keepItem = (index: number) => (el: unknown) => {
  menuItems[index] = (el as HTMLElement | null) || null;
};
const moreButton = ref<HTMLElement | null>(null);
function openMenu() {
  menu.value = !menu.value;
  if (menu.value) nextTick(() => menuItems[0]?.focus());
}
function reveal(id: string) {
  menu.value = false;
  emit("reveal", id);
  emit("update:tab", id);
  if (props.tabs.length) focus();
}
// Focus leaving the menu closes it (Tab away or a click elsewhere).
function leave(event: FocusEvent) {
  const box = event.currentTarget as HTMLElement | null;
  if (box?.contains && !box.contains(event.relatedTarget as Node | null)) menu.value = false;
}
function menuKey(event: KeyboardEvent, index: number) {
  const count = props.more.length;
  if (event.key === "Escape") {
    event.preventDefault();
    event.stopPropagation(); // close the menu, not the drawer
    menu.value = false;
    nextTick(() => moreButton.value?.focus());
    return;
  }
  const next = ({ ArrowDown: index + 1, ArrowUp: index - 1, Home: 0, End: count - 1 } as Record<string, number>)[event.key];
  if (next === undefined || !count) return;
  event.preventDefault();
  menuItems[(next + count) % count]?.focus();
}
const current = computed(() => props.tabs.find((t) => t.id === props.tab) || props.tabs[0] || null);
// Beside the chat the drawer keeps at most 60% of the window; as an overlay (narrow windows) it may
// cover the chat, leaving a 24 px edge.
const size = computed(() => (props.overlay ? Math.min(clampWidth(props.width), Math.max(0, props.viewport - 24)) : clampWidth(props.width, props.viewport)));
const tabButtons: (HTMLElement | null)[] = [];
const keep = (index: number) => (el: unknown) => {
  tabButtons[index] = (el as HTMLElement | null) || null;
};

function select(id: string, focus = false) {
  if (id !== props.tab) emit("update:tab", id);
  if (focus) nextTick(() => tabButtons[props.tabs.findIndex((t) => t.id === id)]?.focus());
}
function tabKey(event: KeyboardEvent, index: number) {
  const count = props.tabs.length;
  if (!count) return;
  const next = ({ ArrowRight: index + 1, ArrowLeft: index - 1, Home: 0, End: count - 1 } as Record<string, number>)[event.key];
  if (next === undefined) return;
  event.preventDefault();
  select(props.tabs[(next + count) % count].id, true);
}
function focus() {
  nextTick(() => tabButtons[Math.max(0, props.tabs.findIndex((t) => t.id === current.value?.id))]?.focus());
}
defineExpose({ focus });

// Resizing: drag the left edge, or focus it and use the arrow keys (Home and End for the bounds).
let drag: { x: number; width: number; pointer: number; target: HTMLElement } | null = null;
function start(event: PointerEvent) {
  if (event.button !== 0) return;
  const target = event.currentTarget as HTMLElement;
  drag = { x: event.clientX, width: size.value, pointer: event.pointerId, target };
  target.setPointerCapture?.(event.pointerId);
  event.preventDefault();
}
function move(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointer) return;
  emit("update:width", clampWidth(drag.width + (drag.x - event.clientX), props.viewport));
}
function end(event: PointerEvent) {
  if (!drag || event.pointerId !== drag.pointer) return;
  drag.target.releasePointerCapture?.(event.pointerId);
  drag = null;
}
function resizeKey(event: KeyboardEvent) {
  const map: Record<string, number> = {
    ArrowLeft: size.value + DRAWER.step, ArrowRight: size.value - DRAWER.step, Home: DRAWER.min, End: DRAWER.max,
  };
  if (!(event.key in map)) return;
  event.preventDefault();
  emit("update:width", clampWidth(map[event.key], props.viewport));
}
onUnmounted(() => (drag = null));
</script>

<template>
  <aside
    id="details-drawer"
    class="details-drawer"
    :class="{ overlay }"
    :style="{ width: size + 'px' }"
    aria-label="Details"
    @keydown.esc.stop="emit('close')"
  >
    <div
      v-if="!overlay"
      class="details-resize"
      role="separator"
      aria-orientation="vertical"
      aria-label="Resize details"
      :aria-valuenow="size"
      :aria-valuemin="DRAWER.min"
      :aria-valuemax="DRAWER.max"
      tabindex="0"
      @pointerdown="start"
      @pointermove="move"
      @pointerup="end"
      @pointercancel="end"
      @keydown="resizeKey"
    ></div>
    <header class="details-head">
      <p v-if="!tabs.length" class="details-title">Details</p>
      <div v-else class="details-tabs" role="tablist" aria-label="Details sections">
        <button
          v-for="(t, i) in tabs"
          :id="'details-tab-' + t.id"
          :key="t.id"
          :ref="keep(i)"
          type="button"
          role="tab"
          class="details-tab"
          :aria-selected="t.id === current?.id"
          :aria-controls="'details-panel-' + t.id"
          :tabindex="t.id === current?.id ? 0 : -1"
          @click="select(t.id)"
          @keydown="tabKey($event, i)"
        >
          {{ t.label }}<span v-if="t.badge" class="details-badge" aria-hidden="true">{{ t.badge }}</span
          ><span v-if="t.badge" class="details-sr">, {{ t.badge }} waiting</span>
        </button>
      </div>
      <div v-if="more.length" class="details-more" @focusout="leave">
        <button
          ref="moreButton"
          type="button"
          class="icon-button"
          aria-label="More sections"
          title="More sections"
          aria-haspopup="menu"
          :aria-expanded="menu"
          aria-controls="details-more-menu"
          @click="openMenu"
        >
          <span class="details-dots" aria-hidden="true"></span>
        </button>
        <div v-if="menu" id="details-more-menu" class="details-menu" role="menu" aria-label="More sections">
          <button
            v-for="(t, i) in more"
            :key="t.id"
            :ref="keepItem(i)"
            type="button"
            role="menuitem"
            @click="reveal(t.id)"
            @keydown="menuKey($event, i)"
          >
            {{ t.label }}
          </button>
        </div>
      </div>
      <button type="button" class="icon-button details-close" aria-label="Close details" title="Close details" @click="emit('close')">
        <span class="details-x" aria-hidden="true"></span>
      </button>
    </header>
    <div
      v-if="current"
      :id="'details-panel-' + current.id"
      class="details-panel"
      role="tabpanel"
      :aria-labelledby="'details-tab-' + current.id"
      tabindex="0"
    >
      <slot :name="current.id" />
    </div>
    <div v-else class="details-panel details-empty">
      <slot name="empty" />
    </div>
  </aside>
</template>

<style scoped>
.details-drawer {
  position: relative;
  flex-shrink: 0;
  display: flex;
  flex-direction: column;
  min-width: 0;
  max-width: 100%;
  border-left: 1px solid var(--line);
  background: var(--side);
}
.details-drawer.overlay {
  position: absolute;
  top: 0;
  right: 0;
  bottom: 0;
  z-index: 10;
  box-shadow: var(--shadow);
}
.details-resize {
  position: absolute;
  top: 0;
  bottom: 0;
  left: -4px;
  width: 8px;
  cursor: col-resize;
  z-index: 2;
  touch-action: none;
}
.details-resize::after {
  content: "";
  position: absolute;
  top: 0;
  bottom: 0;
  left: 3px;
  width: 2px;
  background: transparent;
  transition: background 0.15s;
}
.details-resize:hover::after {
  background: var(--accent-soft);
}
/* Phase 24 (V1-160): the keyboard focus on the edge is the app's focus colour, not the faint hover tint. */
.details-resize:focus-visible::after {
  left: 2px;
  width: 4px;
  background: var(--focus, var(--accent));
}
.details-resize:focus-visible {
  outline: none;
}
.details-head {
  display: flex;
  align-items: flex-start;
  gap: 6px;
  padding: 10px 10px 9px 12px;
  border-bottom: 1px solid var(--line);
}
.details-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 2px;
  flex: 1;
  min-width: 0;
}
.details-title {
  flex: 1;
  margin: 0;
  padding: 6px 2px;
  font-size: 12px;
  font-weight: 560;
  color: var(--secondary);
}
.details-tab {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 6px 9px;
  border-radius: 7px;
  font-size: 12px;
  font-weight: 520;
  color: var(--secondary);
  white-space: nowrap;
}
.details-tab:hover {
  background: var(--hover);
  color: var(--text);
}
.details-tab[aria-selected="true"] {
  background: var(--selected);
  color: var(--text);
}
.details-badge {
  min-width: 16px;
  padding: 0 5px;
  border-radius: 9px;
  background: var(--accent);
  color: var(--on-accent);
  font-size: 11px;
  line-height: 16px;
  text-align: center;
}
.details-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.details-close {
  flex: none;
}
.details-more {
  position: relative;
  flex: none;
}
.details-dots {
  width: 3px;
  height: 3px;
  border-radius: 50%;
  background: currentColor;
  box-shadow: -6px 0 0 currentColor, 6px 0 0 currentColor;
}
.details-menu {
  position: absolute;
  top: calc(100% + 4px);
  right: 0;
  z-index: 3;
  min-width: 180px;
  padding: 4px;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--line);
  border-radius: 10px;
  box-shadow: var(--shadow);
}
.details-menu button {
  padding: 8px 10px;
  border-radius: 7px;
  text-align: left;
  font-size: 12px;
  color: var(--text);
}
.details-menu button:hover,
.details-menu button:focus-visible {
  background: var(--hover);
  outline-offset: -2px;
}
.details-empty {
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 10px;
  color: var(--secondary);
}
.details-x {
  position: relative;
  width: 12px;
  height: 12px;
}
.details-x::before,
.details-x::after {
  content: "";
  position: absolute;
  top: 5px;
  left: 0;
  width: 12px;
  height: 1.5px;
  border-radius: 1px;
  background: currentColor;
  transform: rotate(45deg);
}
.details-x::after {
  transform: rotate(-45deg);
}
.details-panel {
  flex: 1;
  min-height: 0;
  overflow: auto;
  padding: 14px 16px 24px;
  font-size: 13px;
  scrollbar-width: thin;
  scrollbar-color: var(--line-strong) transparent;
}
.details-panel:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: -2px;
}
@media (prefers-reduced-motion: reduce) {
  .details-resize::after {
    transition: none;
  }
}
</style>
