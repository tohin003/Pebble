<script setup lang="ts">
import { computed, defineAsyncComponent, ref } from "vue";
import {
  PhX,
  PhChatCircle,
  PhPulse,
  PhSquaresFour,
  PhArrowLeft,
  PhArrowUpRight,
  PhPlus,
} from "@phosphor-icons/vue";
import type { Artifact, Chat, PrototypeFeedback } from "../types";
// Phase 22 (design study §11): the artifact canvas and its renderers load only when a view is opened,
// so the pet's first load carries no renderer code.
const ArtifactPanel = defineAsyncComponent(() => import("./ArtifactPanel.vue"));
const props = defineProps<{
  chats: Chat[];
  chat: Chat | null;
  active: string | null;
  activity: {
    chat_id: string;
    label: string;
    kind?: string;
    tool?: string;
    at: number;
  }[];
  tab: "chats" | "activity" | "views";
  selectedView: string;
}>();
const emit = defineEmits<{
  close: [];
  select: [id: string];
  new: [];
  workspace: [];
  tab: [value: "chats" | "activity" | "views"];
  view: [id: string];
  feedback: [payload: PrototypeFeedback];
}>();
const query = ref("");
const conversations = computed(() =>
  props.chats.filter((c) =>
    `${c.title} ${c.workspace || ""}`
      .toLowerCase()
      .includes(query.value.toLowerCase()),
  ),
);
const events = computed(() =>
  props.activity
    .filter((e) => e.chat_id === props.chat?.id)
    .slice(-40)
    .reverse(),
);
const time = (at: number) =>
  new Date(at).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
const actionName = (kind: string) =>
  ({
    edit: "File change",
    tests: "Tests",
    app: "Open app",
    command: "Terminal command",
    plugin: "Plugin",
  })[kind] || kind;
// Host labels only; a v2 quiz has question IDs and no answer key.
function viewKind(view: Artifact) {
  if (view.kind === "quiz")
    return Array.isArray(view.data?.questions) &&
      view.data.questions.every((q: any) => q && typeof q === "object" && "id" in q && !("answer" in q))
      ? "Graded quiz"
      : "Self-check quiz";
  const labels: Record<string, string> = { diagram: "Diagram", dashboard: "Dashboard", prototype: "App demo" };
  return Object.hasOwn(labels, view.kind) ? labels[view.kind] : "Interactive view";
}
</script>
<template>
  <aside class="companion-sidebar" aria-label="Companion sidebar">
    <header class="companion-sidebar-header" data-tauri-drag-region>
      <strong>Your space</strong>
      <button
        class="icon-button"
        aria-label="Close companion sidebar"
        @click="emit('close')"
      >
        <PhX :size="16" />
      </button>
    </header>
    <nav class="companion-tabs" aria-label="Companion sections">
      <button :aria-pressed="tab === 'chats'" @click="emit('tab', 'chats')">
        <PhChatCircle :size="15" />Chats
      </button>
      <button
        :aria-pressed="tab === 'activity'"
        @click="emit('tab', 'activity')"
      >
        <PhPulse :size="15" />Activity
      </button>
      <button :aria-pressed="tab === 'views'" @click="emit('tab', 'views')">
        <PhSquaresFour :size="15" />Views<span
          v-if="chat?.artifacts?.length"
          class="companion-count"
          >{{ chat.artifacts.length }}</span
        >
      </button>
    </nav>
    <div v-if="tab === 'chats'" class="companion-sidebar-content">
      <div class="companion-search">
        <input
          v-model="query"
          aria-label="Search companion chats"
          placeholder="Find a conversation…"
        /><button
          class="icon-button"
          aria-label="New sidebar conversation"
          :disabled="!!active"
          @click="emit('new')"
        >
          <PhPlus :size="17" />
        </button>
      </div>
      <button
        v-for="c in conversations"
        :key="c.id"
        class="companion-chat-row"
        :class="{ selected: c.id === chat?.id }"
        :disabled="!!active"
        :aria-current="c.id === chat?.id ? 'true' : undefined"
        @click="emit('select', c.id)"
      >
        <PhChatCircle :size="16" /><span
          ><strong>{{ c.title || "New conversation" }}</strong
          ><small>{{
            c.workspace?.split(/[\\/]/).at(-1) || "Companion"
          }}</small></span
        >
      </button>
      <p v-if="!conversations.length" class="companion-empty">
        No conversations found.
      </p>
      <small v-if="active" class="companion-footnote"
        >Finish or stop the current task to switch conversations.</small
      >
    </div>
    <!-- Only the task status is announced; progress labels are not (F9). -->
    <div
      v-else-if="tab === 'activity'"
      class="companion-sidebar-content"
    >
      <div class="companion-task-status" role="status">
        <i :class="{ running: active === chat?.id }" />{{
          active === chat?.id ? "Task in progress" : "Task history"
        }}
      </div>
      <p v-if="!events.length && !chat?.actions.length" class="companion-empty">
        Tool calls and task progress appear here as Pebble works.
      </p>
      <ol class="companion-timeline" aria-label="Task activity, newest first">
        <li
          v-for="(event, i) in events"
          :key="event.at + ':' + i"
          :class="event.kind"
        >
          <i />
          <div>
            <span>{{ event.label }}</span
            ><small>{{ event.tool || "Pebble" }} · {{ time(event.at) }}</small>
          </div>
        </li>
      </ol>
      <div v-if="chat?.actions.length" class="companion-actions">
        <small>ACTIONS</small>
        <div v-for="action in [...chat.actions].reverse()" :key="action.id">
          <span>{{ actionName(action.kind) }}</span
          ><small>{{ action.status }}</small>
        </div>
        <button
          v-if="chat.actions.some((a) => a.status === 'pending')"
          class="secondary-button"
          @click="emit('workspace')"
        >
          Review in workspace<PhArrowUpRight :size="14" />
        </button>
      </div>
    </div>
    <template v-else>
      <div
        v-if="
          selectedView && chat?.artifacts?.some((a) => a.id === selectedView)
        "
        class="companion-view"
      >
        <button class="companion-back" @click="emit('view', '')">
          <PhArrowLeft :size="14" />All views
        </button>
        <ArtifactPanel
          embedded
          surface="pet"
          :artifacts="chat.artifacts || []"
          :selected="selectedView"
          @close="emit('view', '')"
          @workspace="emit('workspace')"
          @feedback="emit('feedback', $event)"
        />
      </div>
      <div v-else class="companion-sidebar-content">
        <p v-if="!chat?.artifacts?.length" class="companion-empty">
          Quizzes, dashboards, app demos and other views from this
          conversation open here.
        </p>
        <button
          v-for="view in chat?.artifacts"
          :key="view.id"
          class="companion-chat-row"
          @click="emit('view', view.id)"
        >
          <PhSquaresFour :size="17" /><span
            ><strong>{{ view.title }}</strong
            ><small>{{ viewKind(view) }}</small></span
          ><PhArrowUpRight :size="14" />
        </button>
      </div>
    </template>
  </aside>
</template>
<style>
.companion-sidebar {
  position: absolute;
  top: 8px;
  left: 8px;
  bottom: 94px;
  right: 392px;
  min-width: 0;
  display: flex;
  flex-direction: column;
  overflow: hidden;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 19px;
  box-shadow: 0 4px 18px #0002;
}
.companion-sidebar-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 14px;
  border-bottom: 1px solid var(--border);
  font-size: 13px;
}
.companion-tabs {
  display: flex;
  gap: 4px;
  padding: 8px;
  border-bottom: 1px solid var(--border);
}
.companion-tabs button {
  display: flex;
  flex: 1;
  align-items: center;
  justify-content: center;
  gap: 5px;
  font-size: 11px;
  border-radius: 7px;
  padding: 8px 4px;
  color: var(--muted);
}
.companion-tabs button[aria-pressed="true"] {
  background: var(--hover);
  color: var(--text);
}
.companion-count {
  font-size: 9px;
  color: var(--accent);
}
.companion-sidebar-content {
  padding: 12px;
  overflow: auto;
  min-height: 0;
  flex: 1;
}
.companion-search {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 10px;
}
.companion-search input {
  min-width: 0;
  width: 100%;
  padding: 8px 9px;
  background: var(--hover);
  border: 1px solid var(--border);
  border-radius: 8px;
  font-size: 11px;
  color: var(--text);
}
.companion-chat-row {
  display: flex;
  width: 100%;
  align-items: center;
  gap: 10px;
  padding: 11px 9px;
  text-align: left;
  border-radius: 9px;
  color: var(--muted);
}
.companion-chat-row:hover,
.companion-chat-row.selected {
  background: var(--hover);
  color: var(--text);
}
.companion-chat-row span {
  min-width: 0;
  flex: 1;
}
.companion-chat-row strong {
  display: block;
  font-size: 12px;
  font-weight: 500;
  white-space: nowrap;
  text-overflow: ellipsis;
  overflow: hidden;
}
.companion-chat-row small {
  display: block;
  font-size: 10px;
  color: var(--muted);
  margin-top: 4px;
}
.companion-empty {
  color: var(--muted);
  font-size: 12px;
  line-height: 1.6;
  padding: 16px 6px;
}
.companion-footnote {
  display: block;
  font-size: 10px;
  color: var(--muted);
  margin: 12px 6px;
}
.companion-task-status {
  display: flex;
  align-items: center;
  gap: 7px;
  color: var(--muted);
  font-size: 11px;
  margin: 6px 0 16px;
}
.companion-task-status i {
  width: 6px;
  height: 6px;
  background: var(--border);
  border-radius: 50%;
}
.companion-task-status i.running {
  background: var(--accent);
}
.companion-timeline {
  list-style: none;
  margin: 0;
  padding: 0;
}
.companion-timeline li {
  display: flex;
  align-items: baseline;
  gap: 10px;
  padding: 0 0 16px;
  font-size: 11px;
  line-height: 1.5;
}
.companion-timeline li > i {
  flex-shrink: 0;
  width: 5px;
  height: 5px;
  background: var(--muted);
  border-radius: 50%;
}
.companion-timeline li.done > i {
  background: var(--accent);
}
.companion-timeline li.blocked > i {
  background: var(--danger);
}
.companion-timeline small {
  display: block;
  font-size: 9px;
  color: var(--muted);
  margin-top: 4px;
}
.companion-actions {
  border-top: 1px solid var(--border);
  padding-top: 14px;
  font-size: 11px;
}
.companion-actions > small {
  color: var(--muted);
  font-size: 9px;
  letter-spacing: 1px;
}
.companion-actions > div {
  display: flex;
  justify-content: space-between;
  gap: 8px;
  padding: 10px 0;
}
.companion-actions > div small {
  color: var(--muted);
}
.companion-actions button {
  margin-top: 8px;
  font-size: 11px;
}
.companion-view {
  display: flex;
  flex: 1;
  min-height: 0;
  flex-direction: column;
}
.companion-back {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 10px;
  color: var(--muted);
  padding: 10px 14px;
}
@media (max-width: 650px) {
  .companion-sidebar {
    right: 8px;
    z-index: 15;
  }
}
</style>
