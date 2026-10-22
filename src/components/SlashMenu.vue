<script setup lang="ts">
import { nextTick, ref, watch } from "vue";
import type { SlashItem } from "../slashCommands";

// The composer's "/" menu (Phase 22; design study §4.3). Presentation only: focus stays in the
// message box, which owns the keys (arrows, Enter, Tab, Esc) and points aria-activedescendant at
// these options. It lists commands (window actions are tagged "Action"; commands the host refuses
// mid-run are dimmed with the reason) or, once a command is typed, the options for its next
// argument. Choosing emits the entry; the window decides what it does (slashCommands.ts).
const props = withDefaults(
  defineProps<{ items: SlashItem[]; active: number; heading?: string; listId?: string; note?: string }>(),
  { heading: "", listId: "slash-menu", note: "" },
);
const emit = defineEmits<{ choose: [item: SlashItem]; hover: [index: number] }>();
const list = ref<HTMLElement | null>(null);
watch(
  () => props.active,
  async (index) => {
    await nextTick();
    const option = index >= 0 ? (list.value?.children?.[index] as HTMLElement | undefined) : undefined;
    option?.scrollIntoView?.({ block: "nearest" });
  },
);
</script>

<template>
  <div class="slash-menu">
    <p v-if="heading" class="slash-head">{{ heading }}</p>
    <ul v-if="items.length" :id="listId" ref="list" class="slash-options" role="listbox" aria-label="Slash commands">
      <li
        v-for="(item, i) in items"
        :id="listId + '-' + i"
        :key="item.id"
        role="option"
        :aria-selected="i === active"
        :aria-disabled="item.disabled ? 'true' : undefined"
        :class="{ active: i === active, dimmed: !!item.disabled }"
        @mousedown.prevent
        @click="emit('choose', item)"
        @mousemove="i !== active && emit('hover', i)"
      >
        <span class="slash-name"
          ><template v-if="item.kind !== 'argument'">/</template>{{ item.name
          }}<span v-if="item.hint" class="slash-hint">{{ item.hint }}</span></span
        >
        <span class="slash-desc">{{ item.disabled || item.description }}</span>
        <span v-if="item.kind === 'action'" class="slash-tag">Action</span>
      </li>
    </ul>
    <p v-else class="slash-note">No matching command.</p>
    <p v-if="note" class="slash-note" role="alert">{{ note }}</p>
    <p class="slash-keys" aria-hidden="true">↑↓ move · Enter run · Tab complete · Esc close</p>
  </div>
</template>

<style scoped>
.slash-menu {
  position: absolute;
  left: 0;
  right: 0;
  bottom: calc(100% + 8px);
  z-index: 20;
  display: flex;
  flex-direction: column;
  background: var(--panel);
  border: 1px solid var(--line-strong);
  border-radius: var(--radius-lg, 12px);
  box-shadow: var(--shadow);
  overflow: hidden;
}
.slash-head {
  margin: 0;
  padding: 10px 14px 2px;
  font-size: 11px;
  font-weight: 600;
  color: var(--secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.slash-options {
  list-style: none;
  margin: 0;
  padding: 6px;
  /* Eight rows show at once (design study §4.3); the rest scroll. */
  max-height: calc(8 * 33px + 12px);
  overflow: auto;
  min-height: 0;
}
.slash-options li {
  display: flex;
  align-items: baseline;
  gap: 10px;
  min-height: 33px;
  padding: 7px 10px;
  border-radius: var(--radius-md, 8px);
  cursor: pointer;
  font-size: 12px;
  line-height: 1.45;
}
.slash-options li.active {
  background: var(--hover);
}
.slash-options li.dimmed {
  cursor: default;
  opacity: 0.55;
}
.slash-name {
  flex: none;
  min-width: 92px;
  max-width: 45%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-weight: 560;
  color: var(--text);
}
.slash-hint {
  margin-left: 0.4em;
  font-weight: 400;
  color: var(--muted);
}
.slash-desc {
  flex: 1;
  min-width: 0;
  color: var(--secondary);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.slash-tag {
  flex: none;
  font-size: 10px;
  color: var(--muted);
  border: 1px solid var(--line);
  border-radius: 4px;
  padding: 0 5px;
}
.slash-note,
.slash-keys {
  margin: 0;
  padding: 6px 14px;
  font-size: 11px;
  color: var(--muted);
}
.slash-keys {
  border-top: 1px solid var(--line);
  font-size: 10px;
}
</style>
