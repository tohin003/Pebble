<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  PhArrowUp,
  PhArrowUpRight,
  PhX,
  PhStop,
  PhPlus,
  PhPaperclip,
  PhShieldCheck,
  PhBrain,
  PhGlobe,
  PhFolder,
  PhTerminalWindow,
  PhCheck,
  PhSidebarSimple,
  PhSquaresFour,
} from "@phosphor-icons/vue";
import { useCompanion } from "./avatarActivity";
import { gazeAllowed, useNativeGaze } from "./companionPointer";
import ModelPicker from "./components/ModelPicker.vue";
import Avatar from "./components/Avatar.vue";
import StatusLine from "./components/StatusLine.vue";
import TokenStats from "./components/TokenStats.vue";
import CompanionSidebar from "./components/CompanionSidebar.vue";
import PetRunSummary from './components/PetRunSummary.vue';
import { matchShortcut, type ShortcutId } from './shortcuts';
import { applyRunEvent } from './runEvents';
import { MODES, MODE_IDS, modeName, type PermissionMode } from "./permissionModes";
import { createScrollStick } from "./scrollStick";
import {
  attachBytes,
  native,
  rpc,
  RpcError,
  subscribe,
  subscribeDrops,
  selectAttachments,
  selectFile,
} from "./bridge";
import type { Chat, State, Attachment, CompanionSettings, Permissions, PrototypeFeedback } from "./types";
// Phase 24 (V1-165): the Markdown renderer and the command palette load on first use, not with the collapsed pet.
const Markdown = defineAsyncComponent(() => import("./components/Markdown.vue"));
const CommandPalette = defineAsyncComponent(() => import("./components/CommandPalette.vue"));
const expanded = ref(false),
  state = ref<State | null>(null),
  chat = ref<Chat | null>(null),
  input = ref(""),
  error = ref(""),
  busy = ref(false);
const composer = ref<HTMLTextAreaElement | null>(null),
  feed = ref<HTMLElement | null>(null);
// Phase 24 (V1-130): streamed text follows only a person at the end of the feed; sending always shows it.
const stick = createScrollStick(() => feed.value, nextTick);
let off = () => {},
  dropOff = () => {};
const sources = ref<Attachment[]>([]),
  dropHover = ref(false),
  attaching = ref(false);
const panel = ref<"add" | "model" | "permissions" | "url" | "terminal" | null>(
    null,
  ),
  url = ref(""),
  command = ref(""),
  commandNetwork = ref(false);
const efforts = computed(() => state.value?.model.efforts || ["default"]);
const effortName = (s: string) =>
  ({
    default: "Default",
    quick: "Quick answer",
    think: "Think deeper",
    none: "No reasoning",
    minimal: "Minimal",
    low: "Low",
    medium: "Medium",
    high: "High",
    xhigh: "Extra high",
    max: "Maximum",
  })[s] || s;
// Phase 24 (V1-103, V1-055): the modes' names and copy are the shared ones (permissionModes.ts). The pet can only
// narrow standing authority: a lower mode or a tool turned off. Anything wider is turned on in the workspace, where
// Settings explains it; the shell refuses a wider value from the pet as well (main.rs).
const RANK: Record<PermissionMode, number> = { ask: 0, auto: 1, full: 2 };
const WIDER = "Open the workspace to turn this on.";
const permissionNote = ref("");
const toolNames: Record<string, string> = {
  files: "Read files",
  edits: "Edit files",
  tests: "Run tests",
  apps: "Open apps",
  web: "Web access",
  terminal: "Terminal",
  plugins: "Connectors",
  views: "Interactive views",
  screen: "App and browser control",
};
let start: { x: number; y: number } | null = null;
let dragged = false;
const working = computed(() => !!state.value?.active);
const pending = computed(
  () => chat.value?.actions.filter((a) => a.status === "pending").length || 0,
);
// Phase 19 P10 (design study §2.1–§2.6): the companion's pose and status words come from host events and this
// window's own counts (companionState.ts); progress labels, which can embed paths, are never status words.
const companionSetting = computed<CompanionSettings>(
  () => state.value?.settings.companion || { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true },
);
const reducedMotion = computed(() => !!state.value?.settings.reduced_motion);
const companion = useCompanion({
  approvals: () => pending.value,
  questions: () => chat.value?.questions?.filter((q) => q.status === "pending").length || 0,
  notices: () => state.value?.job_notifications?.length || 0,
  model: () => state.value?.model.state,
  reactions: () => companionSetting.value.reactions,
});
const pose = companion.mood;
// "Reply finished" is announced whatever companion.reactions says: it follows the host's completed run.
const outcome = computed(() => {
  const s = companion.state.value;
  return s.run?.state === "completed" && s.completedUntil > 0 ? "completed" : companion.pose.value.outcome;
});
// F4: the shell samples the cursor only while this is allowed and sends a quantized direction (pet_gaze, P8).
const systemMotion = matchMedia("(prefers-reduced-motion: reduce)");
const nativeGaze = native
  ? useNativeGaze({ listen: (event, handler) => listen(event, handler), invoke: (command, args) => invoke(command, args) },
      () => gazeAllowed({ pointer: companionSetting.value.pointer, reduced: reducedMotion.value, systemReduced: systemMotion.matches }))
  : null;
watch(() => companion.mood.value, () => nativeGaze?.rearm());
// The collapsed pet shows a status pill while the host reports a status (and words are not hidden).
const collapsedStatus = computed(() => !expanded.value && !!companion.status.value && companionSetting.value.status_words !== "off");
watch(collapsedStatus, () => resizeCompanion().catch((e) => (error.value = String(e))));
const sidebar = ref(false);
const sidebarTab = ref<"chats" | "activity" | "views">("chats");
const selectedView = ref("");
const activity = ref<
  { chat_id: string; label: string; kind?: string; tool?: string; at: number }[]
>([]);
const palette = ref(false);
const goalRevision = ref(0);
async function resizeCompanion() {
  if (native)
    await invoke("pet_expand", {
      expanded: expanded.value,
      sidebar: expanded.value && sidebar.value,
      status: collapsedStatus.value,
    });
}
// "Send feedback" from an app demo prefills the composer; the person edits and sends it. Only the validated
// screen ID and host-authored words are used, never generated text.
function demoFeedback(payload: PrototypeFeedback) {
  const view = chat.value?.artifacts?.find((a) => a.id === payload.artifact_id && a.kind === "prototype");
  if (!view || payload.chat_id !== chat.value?.id || !/^[A-Za-z0-9_-]{1,64}$/.test(payload.screen_id)) return;
  const prefix = `Feedback on the app demo (revision ${payload.revision}, screen ${payload.screen_id}): `;
  input.value = (input.value.trim() ? input.value.trimEnd() + "\n" : "") + prefix;
  nextTick(() => composer.value?.focus());
}
async function toggleSidebar(value = !sidebar.value) {
  panel.value = null;
  sidebar.value = value;
  await resizeCompanion();
  if (value) await refresh();
}
async function openView(id: string) {
  selectedView.value = id;
  sidebarTab.value = "views";
  await toggleSidebar(true);
}
async function selectChat(id: string) {
  if (working.value || id === chat.value?.id) return;
  try {
    for (const source of [...sources.value]) await removeAttachment(source.id);
    chat.value = await rpc("get_chat", { id });
    localStorage.setItem("pebble-pet-chat", id);
    input.value = "";
    error.value = "";
    selectedView.value = "";
    stick.force();
  } catch (e) {
    error.value = String(e);
  }
}
// V1-018: a conversation moved to Recently deleted takes no new messages; the pet lets go of it (the saved id too),
// and the next message starts a new conversation.
const SAVED = "pebble-pet-chat";
function dropChat() {
  chat.value = null;
  sources.value = [];
  selectedView.value = "";
  try {
    localStorage.removeItem(SAVED);
  } catch {
    /* storage refused: the id is checked again when it is read */
  }
}
const TRASHED = "That conversation is in Recently deleted, so Pebble let go of it. Send your message again to start a new conversation, or restore it in the workspace (Settings → Data & storage).";
const trashed = (e: unknown) => e instanceof RpcError && e.code === "trashed_conversation";
function theme() {
  const s = state.value?.settings;
  const t = s?.theme || "system";
  document.documentElement.dataset.theme =
    t === "system"
      ? matchMedia("(prefers-color-scheme:dark)").matches
        ? "dark"
        : "light"
      : t;
}
async function refresh() {
  if (native) {
    state.value = await rpc("state");
    theme();
  }
}
async function ensureChat() {
  if (!chat.value) {
    const saved = localStorage.getItem(SAVED);
    if (saved) {
      try {
        const found = await rpc<Chat>("get_chat", { id: saved });
        if (found.trashed) dropChat(); // V1-018: never reopen a conversation in Recently deleted
        else chat.value = found;
      } catch {
        localStorage.removeItem(SAVED);
      }
    }
    if (!chat.value) {
      chat.value = await rpc("new_chat");
      localStorage.setItem(SAVED, chat.value!.id);
    }
  }
  return chat.value!;
}
async function toggle(value: boolean) {
  expanded.value = value;
  if (!value) {
    panel.value = null;
    sidebar.value = false;
  }
  await resizeCompanion();
  if (value) {
    await refresh();
    if (native) await ensureChat();
    await nextTick();
    composer.value?.focus();
    stick.force();
  }
}
async function workspace() {
  if (native)
    await invoke("open_workspace", { chatId: chat.value?.id || null });
  await toggle(false);
}
async function send() {
  if (!input.value.trim() || busy.value || attaching.value) return;
  error.value = "";
  busy.value = true;
  panel.value = null;
  try {
    const c = await ensureChat();
    const text = input.value.trim();
    const command = text.startsWith('/');
    const followup = state.value?.active === c.id && !command;
    // F3: "Sending" until the host's `run queued` (or an error); never "working" before admission.
    if (!command && !followup) companion.sending(true);
    await rpc("send", {
      id: c.id,
      text,
      effort: state.value?.model.effort || "default",
      source_ids: command || followup ? [] : sources.value.map((s) => s.id),
    });
    input.value = "";
    if (!command && !followup) sources.value = [];
    chat.value = await rpc("get_chat", { id: c.id });
    await refresh();
    stick.force();
  } catch (e) {
    if (trashed(e)) {
      dropChat(); // the message stays in the box; sending it again starts a new conversation
      error.value = TRASHED;
    } else error.value = String(e);
  } finally {
    companion.sending(false);
    busy.value = false;
  }
}
// V1-055: Enter that confirms an input-method composition (Japanese, Chinese, Korean…) is not a send.
function enterKey(event: KeyboardEvent) {
  if (event.isComposing || event.keyCode === 229) return;
  event.preventDefault();
  send();
}
// V1-042: pasting a screenshot or a file attaches it (through attach_bytes, as a file chosen in Attach would be);
// pasting text is left to the text box.
function clipboardFiles(data: DataTransfer | null | undefined): File[] {
  if (!data) return [];
  const items = Array.from(data.items || []).filter((item) => item.kind === "file").map((item) => item.getAsFile()).filter((file): file is File => !!file);
  return items.length ? items : Array.from(data.files || []);
}
async function paste(event: ClipboardEvent) {
  const files = clipboardFiles(event.clipboardData);
  if (!files.length) return;
  event.preventDefault();
  await attachFiles(files);
}
async function attachFiles(files: File[]) {
  if (!files.length || working.value || attaching.value) return;
  attaching.value = true;
  try {
    if (files.length + sources.value.length > 10) throw new Error("Use up to ten attachments.");
    const c = await ensureChat();
    const failures: string[] = [];
    for (const file of files) {
      try {
        sources.value.push(await attachBytes(c.id, file.name || "", file));
      } catch (e) {
        failures.push(`${file.name || "Pasted image"}: ${String(e)}`);
      }
    }
    if (failures.length) throw new Error(failures.join("\n"));
  } catch (e) {
    error.value = String(e);
  } finally {
    attaching.value = false;
  }
}
async function newChat() {
  if (working.value) return;
  for (const s of sources.value) await removeAttachment(s.id);
  panel.value = null;
  chat.value = await rpc("new_chat");
  localStorage.setItem(SAVED, chat.value!.id);
  input.value = "";
  error.value = "";
  selectedView.value = "";
  await refresh();
  composer.value?.focus();
}
async function stop() {
  await rpc("cancel", { id: chat.value?.id });
}
async function attachPaths(paths: string[]) {
  if (!paths.length || working.value || attaching.value) return;
  attaching.value = true;
  try {
    if (paths.length + sources.value.length > 10)
      throw new Error("Use up to ten attachments.");
    if (!expanded.value) await toggle(true);
    const c = await ensureChat();
    const failures: string[] = [];
    for (const path of paths) {
      try {
        sources.value.push(await rpc("attach", { id: c.id, path }));
      } catch (e) {
        failures.push(`${path.split(/[\\/]/).pop()}: ${String(e)}`);
      }
    }
    if (failures.length) throw new Error(failures.join("\n"));
  } catch (e) {
    error.value = String(e);
  } finally {
    attaching.value = false;
  }
}
// V1-002: moving this conversation from a model on this Mac to a cloud model leaves its earlier messages out of the
// cloud requests unless the person reviews them; the pet says so and points to the workspace, where the review is.
const DEVICE_NOTICE = "Earlier messages were on this Mac. Review what is sent…";
const deviceNotice = ref("");
const onMac = (id?: string | null) => {
  const p = state.value?.profiles?.find((profile) => profile.id === id);
  return !!p && (p.kind === "gguf" || p.local === true);
};
async function changeModel(id: string) {
  try {
    const before = state.value?.model.profile_id;
    await rpc("model_select", { profile_id: id });
    await refresh();
    deviceNotice.value = onMac(before) && !onMac(id) && !!chat.value?.messages.length ? DEVICE_NOTICE : "";
  } catch (e) {
    error.value = String(e);
  }
}
// Storage events: a conversation moved to Recently deleted or deleted (V1-018), and its views' quiz drafts (V1-156).
async function storageEvent(d: Record<string, any> | null) {
  if (d?.changed === "deleted") {
    clearQuizDrafts(d.deleted_artifacts);
    if (d.chat_id && d.chat_id === chat.value?.id) dropChat();
    return;
  }
  if (d?.changed !== "recently_deleted" || !chat.value || !native) return;
  try {
    const current = await rpc<Chat>("get_chat", { id: chat.value.id });
    if (current.trashed && current.id === chat.value?.id) dropChat();
  } catch {
    dropChat();
  }
}
function clearQuizDrafts(ids: unknown) {
  if (!Array.isArray(ids)) return;
  try {
    const keys = Array.from({ length: localStorage.length }, (_, i) => localStorage.key(i) || "");
    for (const id of ids)
      if (typeof id === "string" && id)
        for (const key of keys) if (key === "pebble.quiz." + id || key.startsWith("pebble.quiz." + id + ".")) localStorage.removeItem(key);
  } catch {
    /* storage refused: nothing was kept there either */
  }
}
// Notices from the Markdown renderer (a refused link, a copied code block): fixed host words.
const note = ref("");
let noteTimer = 0;
function showNote(event: Event) {
  const text = (event as CustomEvent).detail;
  if (typeof text !== "string") return;
  note.value = text.slice(0, 200);
  window.clearTimeout(noteTimer);
  noteTimer = window.setTimeout(() => (note.value = ""), 3500);
}
async function changeEffort(event: Event) {
  try {
    await rpc("model_effort", {
      effort: (event.target as HTMLSelectElement).value,
    });
    await refresh();
  } catch (e) {
    error.value = String(e);
  }
}
/** Whether a choice would widen standing authority: a higher mode, or a tool that is off now. */
function widens(mode?: Permissions["mode"], tool?: string) {
  const current = state.value?.settings.permissions;
  if (!current) return true;
  if (mode) return RANK[mode] > (RANK[current.mode] ?? 0);
  return !!tool && !current.tools[tool as keyof Permissions["tools"]];
}
async function permission(mode?: Permissions["mode"], tool?: string) {
  if (!state.value || working.value) return;
  permissionNote.value = "";
  if (widens(mode, tool)) {
    permissionNote.value = WIDER; // nothing is sent: the workspace is where authority grows
    return;
  }
  try {
    const value = JSON.parse(JSON.stringify(state.value.settings.permissions));
    if (mode) value.mode = mode;
    if (tool) value.tools[tool] = !value.tools[tool];
    await rpc("setting", { key: "permissions", value });
    await refresh();
  } catch (e) {
    error.value = String(e);
  }
}
async function addFiles() {
  panel.value = null;
  try {
    await attachPaths(await selectAttachments());
  } catch (e) {
    error.value = String(e);
  }
}
async function connectProject() {
  try {
    const path = await selectFile(true);
    if (path) {
      const c = await ensureChat();
      chat.value = await rpc("set_workspace", { id: c.id, path });
      panel.value = null;
    }
  } catch (e) {
    error.value = String(e);
  }
}
async function readUrl() {
  attaching.value = true;
  try {
    const c = await ensureChat();
    sources.value.push(await rpc("attach_url", { id: c.id, url: url.value }));
    url.value = "";
    panel.value = null;
  } catch (e) {
    error.value = String(e);
  } finally {
    attaching.value = false;
  }
}
async function proposeCommand() {
  try {
    const c = await ensureChat();
    await rpc("terminal_propose", {
      id: c.id,
      command: command.value,
      network: commandNetwork.value,
    });
    command.value = "";
    panel.value = null;
    await workspace();
  } catch (e) {
    error.value = String(e);
  }
}
function togglePanel(value: typeof panel.value) {
  panel.value = panel.value === value ? null : value;
}
async function removeAttachment(id: string) {
  if (chat.value)
    await rpc("remove_source", { id: chat.value.id, source_id: id });
  sources.value = sources.value.filter((s) => s.id !== id);
}
function down(e: PointerEvent) {
  start = { x: e.screenX, y: e.screenY };
  dragged = false;
}
async function move(e: PointerEvent) {
  if (
    start &&
    !dragged &&
    Math.hypot(e.screenX - start.x, e.screenY - start.y) > 7
  ) {
    dragged = true;
    start = null;
    companion.interact("drag"); // presentation only: never an RPC or task state
    if (native) await getCurrentWindow().startDragging();
  }
}
function tap() {
  start = null;
  if (!dragged) {
    companion.interact("tap");
    toggle(!expanded.value).catch((e) => (error.value = String(e)));
  }
  dragged = false;
}
function keyboard(e: KeyboardEvent) {
  const shortcut = expanded.value ? matchShortcut(e, "pet") : null;
  if (!shortcut) return;
  if (shortcut !== "close") e.preventDefault();
  if (shortcut === "close") {
    if (palette.value) palette.value = false;
    else if (panel.value) panel.value = null;
    else if (sidebar.value) toggleSidebar(false);
    else toggle(false);
  } else if (shortcut === "palette") {
    panel.value = null;
    palette.value = !palette.value;
  } else runShortcut(shortcut);
}
function runShortcut(id: ShortcutId) {
  palette.value = false;
  if (id === "stop" && working.value) stop().catch((e) => (error.value = String(e)));
  else if (id === "activity") {
    sidebarTab.value = "activity";
    toggleSidebar(true);
  }
}
function chooseCommand(text: string) {
  palette.value = false;
  input.value = text;
  nextTick(() => composer.value?.focus());
}
function outside(event: PointerEvent) {
  if (!(event.target as Element).closest(".pet-composer")) panel.value = null;
}
onMounted(async () => {
  document.addEventListener("keydown", keyboard);
  document.addEventListener("pointerdown", outside);
  document.addEventListener("pebble-notice", showNote);
  if (!native) {
    theme();
    return;
  }
  await refresh();
  companion.seed(state.value?.runs, state.value?.active_run); // a run already live or waiting when the pet opens
  dropOff = await subscribeDrops((type, paths) => {
    dropHover.value = (type === "enter" || type === "over") && !working.value;
    if (type === "drop") {
      dropHover.value = false;
      attachPaths(paths);
    }
  });
  off = await subscribe(async ({ event, data: d }) => {
    companion.accept({ event, data: d });
    if (event === "settings") await refresh();
    if (event === "offline") {
      error.value = "The local service stopped. Restart Pebble to reconnect.";
      if (state.value) state.value.active = null;
      return;
    }
    if (event === "model" && state.value) state.value.model = d;
    if (event === "progress") {
      if (state.value) state.value.active = d.chat_id;
      activity.value.push({ ...d, at: Date.now() });
      if (activity.value.length > 100) activity.value.shift();
    }
    if (event === "idle") {
      await refresh();
      if (chat.value?.id === d.chat_id) {
        const following = stick.stuck();
        chat.value = await rpc("get_chat", { id: d.chat_id });
        if (following) stick.force();
      }
    }
    if (event === "storage") return storageEvent(d);
    if (event === "goal" && d.chat_id === chat.value?.id) goalRevision.value++;
    if (d.chat_id !== chat.value?.id) return;
    // V1-002: earlier on-device messages were not sent to the cloud model; the review happens in the workspace.
    if (event === "progress" && d?.handoff && typeof d.label === "string") deviceNotice.value = d.label.slice(0, 300);
    if (chat.value && applyRunEvent(chat.value, event, d)) {
      if (event === 'run' && state.value) {
        if (['queued', 'running', 'retry_wait'].includes(d.state)) {
          state.value.active = d.chat_id; state.value.active_run = d.run_id;
        } else if (state.value.active_run === d.run_id) {
          state.value.active = null; state.value.active_run = null;
        }
      }
      stick.follow(); return;
    }
    if (event === "token") {
      stick.follow();
      let m = chat.value!.messages.find((m) => m.id === d.id);
      if (!m) {
        m = { id: d.id, role: "assistant", content: "" };
        chat.value!.messages.push(m);
      }
      m.content += d.text;
    }
    if (event === "actions") chat.value!.actions = d.actions;
    if (event === "artifact") {
      const updated = await rpc<Chat>("get_chat", { id: d.chat_id });
      if (chat.value?.id === updated.id) {
        chat.value = updated;
        selectedView.value = d.id;
        if (sidebar.value) sidebarTab.value = "views";
      }
    }
    if (event === "usage") {
      const m = chat.value!.messages.find((m) => m.id === d.id);
      if (m) m.usage = d.usage;
    }
    if (event === "error") error.value = d.message;
  });
});
onUnmounted(() => {
  off();
  dropOff();
  document.removeEventListener("keydown", keyboard);
  document.removeEventListener("pointerdown", outside);
  document.removeEventListener("pebble-notice", showNote);
  window.clearTimeout(noteTimer);
});
</script>
<template>
  <div class="pet-root" :class="{ expanded, 'with-sidebar': sidebar }">
    <div v-if="dropHover" class="pet-drop-hint">Drop to attach</div>
    <CompanionSidebar
      v-if="expanded && sidebar"
      :chats="state?.chats || []"
      :chat="chat"
      :active="state?.active || null"
      :activity="activity"
      :tab="sidebarTab"
      :selected-view="selectedView"
      @close="toggleSidebar(false)"
      @select="selectChat"
      @new="newChat"
      @workspace="workspace"
      @tab="sidebarTab = $event"
      @view="selectedView = $event"
      @feedback="demoFeedback"
    />
    <main v-if="expanded" class="quick-chat" aria-label="Pebble quick chat">
      <header data-tauri-drag-region>
        <button
          title="Toggle companion sidebar"
          aria-label="Toggle companion sidebar"
          :aria-expanded="sidebar"
          @click="toggleSidebar()"
        >
          <PhSidebarSimple :size="18" />
        </button>
        <div>
          <strong>pebble.</strong>
          <!-- Phase 19: the pet's one status region; Stop stays in the composer toolbar. -->
          <StatusLine
            class="pet-status"
            :status="companion.pose.value.status"
            :started-at="companion.pose.value.startedAt"
            :attempt="companion.pose.value.attempt"
            :outcome="outcome"
            :mode="companionSetting.status_words"
            :reduced="reducedMotion"
          /><small v-if="!companion.pose.value.status || companionSetting.status_words === 'off'">A little help, right here.</small>
        </div>
        <button
          title="New conversation"
          aria-label="New conversation"
          :disabled="working"
          @click="newChat"
        >
          <PhPlus :size="16" /></button
        ><button
          title="Open full workspace"
          aria-label="Open full workspace"
          @click="workspace"
        >
          <PhArrowUpRight :size="18" /></button
        ><button
          title="Minimize to pebble"
          aria-label="Minimize to pebble"
          @click="toggle(false)"
        >
          <PhX :size="16" />
        </button>
      </header>
      <!-- F9: the feed is not a live region; StatusLine announces the status and "Reply finished" once. -->
      <div ref="feed" class="quick-feed">
        <div v-if="!chat?.messages.length" class="pet-greeting">
          <p>Small companion.<br /><strong>Room for big ideas.</strong></p>
          <span>Ask a question, check the weather, or open an app.</span>
        </div>
        <article
          v-for="m in chat?.messages.slice(-8)"
          :key="m.id"
          :class="m.role"
        >
          <div v-if="m.attachments?.length" class="pet-history-images">
            <span v-for="a in m.attachments" :key="a.id">
              <img v-if="a.preview" :src="a.preview" :alt="a.name" />
              <span v-else><PhPaperclip :size="12" /> {{ a.name }}</span>
            </span>
          </div>
          <h3 class="pet-sr">{{ m.role === "user" ? "You said:" : "Pebble said:" }}</h3>
          <Markdown v-if="m.role === 'assistant'" :text="m.content" /><p v-else class="pet-user-text">{{ m.content }}</p><TokenStats
            v-if="m.usage"
            :usage="m.usage"
          />
        </article>
        <PetRunSummary v-if="chat" :chat="chat" @workspace="workspace"
          :refresh-key="goalRevision + ':' + (chat.runs || []).map((r) => r.id + ':' + r.state).join() + ':' + (chat.questions || []).length" />
      </div>
      <div v-if="chat?.artifacts?.length" class="pet-view-links">
        <button
          v-for="view in chat.artifacts"
          :key="view.id"
          @click="openView(view.id)"
        >
          <PhSquaresFour :size="13" />{{ view.title }}
        </button>
      </div>
      <button v-if="pending" class="pet-review" @click="workspace">
        <PhShieldCheck :size="15" />{{ pending }}
        {{ pending === 1 ? "action needs" : "actions need" }}
        review<PhArrowUpRight :size="14" />
      </button>
      <p v-if="deviceNotice && panel !== 'model'" class="pet-device-note" role="status">
        <span>{{ deviceNotice }}</span
        ><button type="button" class="pet-link" @click="workspace">Review in the workspace</button
        ><button type="button" class="pet-link" aria-label="Dismiss this note" @click="deviceNotice = ''">×</button>
      </p>
      <p v-if="note" class="pet-note" role="status">{{ note }}</p>
      <p v-if="error" class="pet-error" role="alert">{{ error }}</p>
      <form class="pet-composer" @submit.prevent="send">
        <div
          v-if="panel"
          class="pet-popover"
          role="group"
          :aria-label="
            panel === 'model'
              ? 'Model and reasoning'
              : panel === 'permissions'
                ? 'Tool permissions'
                : 'Add context'
          "
        >
          <template v-if="panel === 'model'">
            <div class="pet-popover-heading">
              <strong>Model & reasoning</strong
              ><button
                type="button"
                aria-label="Close menu"
                @click="panel = null"
              >
                <PhX :size="14" />
              </button>
            </div>
            <ModelPicker
              compact
              :profiles="state?.profiles || []"
              :current="state?.model.profile_id"
              :disabled="working || busy"
              :notice="deviceNotice"
              review-label="Review in the workspace"
              @select="changeModel"
              @review="workspace"
              @dismiss="deviceNotice = ''"
            />
            <label class="pet-effort"
              ><span>Reasoning effort</span
              ><select
                :value="state?.model.effort || 'default'"
                aria-label="Reasoning effort"
                :disabled="working || busy || efforts.length === 1"
                @change="changeEffort"
              >
                <option v-for="level in efforts" :key="level" :value="level">
                  {{ effortName(level)
                  }}{{
                    level === state?.model.default_effort
                      ? " · recommended"
                      : ""
                  }}
                </option>
              </select></label
            >
            <small
              >{{
                efforts.length === 1
                  ? "This model uses its default effort."
                  : "Uses the selected model’s supported reasoning levels."
              }}
              <template v-if="state?.model.configured">
                · {{ state.model.context.toLocaleString() }} context</template
              ></small
            >
          </template>
          <template v-else-if="panel === 'permissions'">
            <div class="pet-popover-heading">
              <strong>Tool permissions</strong
              ><button
                type="button"
                aria-label="Close menu"
                @click="panel = null"
              >
                <PhX :size="14" />
              </button>
            </div>
            <p v-if="state?.settings.permissions.mode === 'full'" class="pet-full-warning" role="note">
              Full access is on. {{ MODES.full.runs_without_asking }}
            </p>
            <div role="radiogroup" aria-label="Permission mode" class="pet-modes">
              <button
                v-for="id in MODE_IDS"
                :key="id"
                type="button"
                role="radio"
                class="pet-mode"
                :aria-checked="state?.settings.permissions.mode === id ? 'true' : 'false'"
                :aria-disabled="widens(id) ? 'true' : undefined"
                :disabled="working"
                @click="permission(id)"
              >
                <span
                  >{{ MODES[id].name }}<small>{{ MODES[id].runs_without_asking }}</small></span
                ><PhCheck
                  v-if="state?.settings.permissions.mode === id"
                  :size="14"
                />
              </button>
            </div>
            <div class="pet-tool-grid" role="group" aria-label="Tools">
              <button
                v-for="(name, key) in toolNames"
                :key="key"
                type="button"
                role="switch"
                :aria-checked="
                  state?.settings.permissions.tools[
                    key as keyof Permissions['tools']
                  ] ? 'true' : 'false'
                "
                :aria-disabled="widens(undefined, key) ? 'true' : undefined"
                :disabled="working"
                @click="permission(undefined, key)"
              >
                <i
                  aria-hidden="true"
                  :class="{
                    on: state?.settings.permissions.tools[
                      key as keyof Permissions['tools']
                    ],
                  }"
                />{{ name }}
              </button>
            </div>
            <p v-if="permissionNote" class="pet-permission-note" role="status">
              {{ permissionNote }}
              <button type="button" class="pet-link" @click="workspace">Open the workspace</button>
            </p>
            <small>Here you can only turn things down. Turning a mode or tool up happens in the workspace's Settings.</small>
          </template>
          <template v-else-if="panel === 'add'">
            <button type="button" :disabled="attaching" @click="addFiles">
              <PhPaperclip :size="16" />Attach files or images
            </button>
            <button type="button" @click="panel = 'url'">
              <PhGlobe :size="16" />Read a web source
            </button>
            <button type="button" @click="connectProject">
              <PhFolder :size="16" />Connect a project
            </button>
            <button type="button" @click="panel = 'terminal'">
              <PhTerminalWindow :size="16" />Terminal command
            </button>
          </template>
          <template v-else-if="panel === 'url'">
            <div class="pet-popover-heading">
              <strong>Read a web source</strong
              ><button
                type="button"
                aria-label="Back to add context"
                @click="panel = 'add'"
              >
                <PhX :size="14" />
              </button>
            </div>
            <input
              v-model="url"
              aria-label="Web source URL"
              placeholder="https://…"
              @keydown.enter.prevent="readUrl"
            />
            <button
              type="button"
              :disabled="attaching || sources.length >= 10 || !url.trim()"
              @click="readUrl"
            >
              Read and attach
            </button>
          </template>
          <template v-else>
            <div class="pet-popover-heading">
              <strong>Terminal command</strong
              ><button
                type="button"
                aria-label="Back to add context"
                @click="panel = 'add'"
              >
                <PhX :size="14" />
              </button>
            </div>
            <textarea
              v-model="command"
              aria-label="Terminal command"
              placeholder="git status --short"
              rows="2"
            />
            <label
              ><input v-model="commandNetwork" type="checkbox" /> Request
              network access</label
            >
            <small
              >Commands are reviewed in the workspace before execution.</small
            >
            <button
              v-if="!chat?.workspace"
              type="button"
              @click="connectProject"
            >
              Connect a project
            </button>
            <button
              v-else
              type="button"
              :disabled="!command.trim() || working"
              @click="proposeCommand"
            >
              Review command
            </button>
          </template>
        </div>
        <div v-if="sources.length" class="pet-attachments">
          <span v-for="s in sources" :key="s.id" :title="s.notice || s.name"
            ><img v-if="s.preview" :src="s.preview" alt="" />{{ s.name
            }}<button
              type="button"
              :aria-label="'Remove attachment ' + s.name"
              @click="removeAttachment(s.id)"
            >
              ×
            </button></span
          >
        </div>
        <textarea
          ref="composer"
          v-model="input"
          aria-label="Message Pebble"
          placeholder="What's on your mind?"
          rows="2"
          @keydown.enter.exact="enterKey"
          @paste="paste"
        />
        <small
          v-if="sources.some((s) => s.kind === 'image')"
          class="pet-image-note"
          >Images go to your selected vision-capable provider.</small
        >
        <div class="pet-toolbar">
          <button
            type="button"
            aria-label="Add context"
            title="Add files, images, web sources or a project"
            :aria-expanded="panel === 'add'"
            :disabled="working || attaching"
            @click="togglePanel('add')"
          >
            <PhPlus :size="19" />
          </button>
          <button
            type="button"
            aria-label="Tool permissions"
            :title="'Permissions: ' + modeName(state?.settings.permissions.mode)"
            :aria-expanded="panel === 'permissions'"
            :class="{ full: state?.settings.permissions.mode === 'full' }"
            :disabled="working"
            @click="togglePanel('permissions')"
          >
            <PhShieldCheck :size="18" />
          </button>
          <span class="pet-model-label">{{
            state?.model.name || "Choose a model"
          }}</span>
          <button
            type="button"
            aria-label="Model and reasoning"
            :title="
              (state?.model.name || 'Model') +
              ' · ' +
              effortName(state?.model.effort || 'default')
            "
            :aria-expanded="panel === 'model'"
            :disabled="working || busy"
            @click="togglePanel('model')"
          >
            <PhBrain :size="19" />
          </button>
          <button
            v-if="working && !input.trim()"
            type="button"
            aria-label="Stop task"
            @click="stop"
          >
            <PhStop :size="17" weight="fill" /></button
          ><button
            v-else
            aria-label="Send message"
            :disabled="!input.trim() || busy || attaching"
            class="pet-send"
          >
            <PhArrowUp :size="20" weight="bold" />
          </button>
        </div>
      </form>
      <CommandPalette v-if="palette" surface="pet" :platform="state?.platform" :running="working"
        @close="palette = false" @choose="chooseCommand" @action="runShortcut" />
    </main>
    <!-- Collapsed: one status region stays mounted (it announces "Reply finished"); it is a pill while a status shows. -->
    <StatusLine
      v-if="!expanded"
      class="pet-pill"
      :pill="collapsedStatus"
      :status="collapsedStatus ? companion.pose.value.status : null"
      :started-at="companion.pose.value.startedAt"
      :attempt="companion.pose.value.attempt"
      :outcome="outcome"
      :mode="companionSetting.status_words"
      :reduced="reducedMotion"
    />
    <button
      class="pet-avatar"
      :title="expanded ? 'Minimize to pebble' : 'Tap to chat · drag to move'"
      :aria-label="expanded ? 'Minimize to pebble' : 'Chat with Pebble'"
      @pointerdown="down"
      @pointermove="move"
      @pointerup="tap"
      @pointercancel="start = null"
      @pointerenter="companion.interact('hover')"
      @pointerleave="companion.interact('leave')"
      @keydown.enter.prevent="toggle(!expanded)"
      @keydown.space.prevent="toggle(!expanded)"
    >
      <Avatar
        :size="148"
        :state="pose"
        :reduced="reducedMotion"
        :look="nativeGaze?.look.value ?? null"
        :gaze="companionSetting.pointer"
      />
    </button>
  </div>
</template>
<style>
html.pet-surface,
html.pet-surface body,
html.pet-surface #app {
  background: transparent !important;
  overflow: hidden;
  margin: 0;
  width: 100%;
  height: 100%;
}
.pet-root {
  height: 100dvh;
  width: 100%;
  position: relative;
  overflow: clip;
  color: var(--text);
}
.pet-avatar {
  position: absolute;
  right: 0;
  bottom: 0;
  width: 112px;
  height: 112px;
  display: grid;
  place-items: center;
  padding: 0;
  touch-action: none;
  user-select: none;
  -webkit-user-select: none;
  cursor: grab;
  color: var(--avatar-body);
  filter: drop-shadow(0 4px 4px #0002);
}
.pet-avatar:active {
  cursor: grabbing;
}
/* Phase 19: the collapsed pet's status pill sits above the avatar in the 176x136 window (pet_expand status). */
.pet-pill {
  position: absolute;
  top: 4px;
  left: 4px;
  right: 4px;
  justify-content: center;
  pointer-events: none;
  font-size: 11px;
}
.quick-chat header .pet-status {
  min-height: 0;
  margin-top: 3px;
  max-width: 200px;
  font-size: 11px;
}
.pet-avatar .avatar {
  position: absolute;
  pointer-events: none;
}
.quick-chat {
  position: absolute;
  left: 8px;
  top: 8px;
  right: 8px;
  bottom: 94px;
  border: 1px solid var(--border);
  border-radius: 19px;
  background: var(--surface);
  box-shadow: 0 4px 18px #0002;
  display: flex;
  flex-direction: column;
  overflow: hidden;
}
.pet-root.with-sidebar .quick-chat {
  left: auto;
  width: 368px;
}
.pet-view-links {
  display: flex;
  gap: 6px;
  overflow-x: auto;
  padding: 4px 12px 8px;
}
.pet-view-links button {
  display: flex;
  align-items: center;
  gap: 5px;
  flex-shrink: 0;
  max-width: 240px;
  padding: 5px 8px;
  border-radius: 7px;
  background: var(--hover);
  color: var(--accent);
  font-size: 11px;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}
.quick-chat header button[aria-expanded="true"] {
  background: var(--hover);
  color: var(--accent);
}
.quick-chat header {
  padding: 13px 14px;
  display: flex;
  gap: 7px;
  align-items: center;
  border-bottom: 1px solid var(--border);
}
.quick-chat header div {
  flex: 1;
  pointer-events: none;
}
.quick-chat header strong {
  font-size: 17px;
  letter-spacing: -0.5px;
}
.quick-chat header small {
  display: block;
  font-size: 11px;
  color: var(--muted);
  margin-top: 3px;
  max-width: 160px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
/* Phase 24 (V1-160): 28 px targets. */
.quick-chat header button {
  display: grid;
  place-items: center;
  width: 28px;
  height: 28px;
  border-radius: 6px;
  color: var(--muted);
}
.quick-chat header button:hover {
  background: var(--hover);
  color: var(--text);
}
.quick-feed {
  flex: 1;
  overflow: auto;
  min-height: 0;
  padding: 12px 16px;
  font-size: 13px;
}
.quick-feed article {
  margin-bottom: 15px;
  line-height: 1.55;
}
.quick-feed article.user {
  background: var(--hover);
  padding: 8px 11px;
  border-radius: 10px;
  margin-left: 28px;
}
.quick-feed article p {
  margin: 0 0 7px;
}
/* V1-126: each message names its speaker for screen readers. */
.pet-sr {
  position: absolute;
  width: 1px;
  height: 1px;
  margin: -1px;
  overflow: hidden;
  clip: rect(0 0 0 0);
  white-space: nowrap;
}
.quick-feed .pet-user-text {
  margin: 0;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
}
.pet-greeting {
  padding: 12px 5px;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
}
.pet-greeting p {
  font-size: 19px;
  letter-spacing: -0.5px;
  margin-bottom: 10px !important;
}
.pet-greeting strong {
  color: var(--text);
  font-weight: 500;
}
.quick-chat form {
  display: flex;
  flex-direction: column;
  gap: 7px;
  padding: 10px 12px;
  border-top: 1px solid var(--border);
  align-items: stretch;
}
.quick-chat textarea {
  width: 100%;
  min-width: 0;
  resize: none;
  background: transparent;
  border: 0;
  color: var(--text);
  font-size: 12px;
  line-height: 1.5;
  padding: 4px;
}
.quick-chat form button {
  height: 32px;
  width: 32px;
  display: grid;
  place-items: center;
  background: var(--accent);
  color: var(--on-accent);
  border-radius: 10px;
}
.pet-review {
  margin: 0 12px 6px;
  display: flex;
  align-items: center;
  gap: 7px;
  font-size: 11px;
  color: var(--accent);
  padding: 6px;
  border-radius: 7px;
  background: var(--hover);
}
.pet-review svg:last-child {
  margin-left: auto;
}
.pet-error {
  margin: 0;
  padding: 5px 14px;
  color: var(--danger);
  font-size: 11px;
  max-height: 64px;
  overflow: auto;
}
.pet-note,
.pet-device-note {
  margin: 0 12px 6px;
  font-size: 11px;
  color: var(--secondary);
}
.pet-device-note {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 8px;
  padding: 6px 8px;
  border: 1px solid var(--accent-soft);
  border-radius: 8px;
  color: var(--text);
}
.pet-device-note span {
  flex: 1 1 160px;
}
.quick-chat .pet-link {
  width: auto;
  height: auto;
  min-height: 24px;
  padding: 2px 4px;
  background: transparent;
  color: var(--accent);
  font-size: 11px;
  text-decoration: underline;
}
</style>

<style>
.pet-attachments {
  display: flex;
  gap: 5px;
  flex-wrap: wrap;
  font-size: 11px;
  margin-bottom: 6px;
}
.pet-attachments span {
  display: flex;
  align-items: center;
  gap: 4px;
  max-width: 180px;
  overflow: hidden;
}
.pet-attachments img {
  width: 24px;
  height: 24px;
  object-fit: cover;
  border-radius: 4px;
}
.pet-drop-hint {
  position: absolute;
  inset: 8px;
  background: var(--surface);
  border: 2px dashed var(--accent);
  border-radius: 18px;
  display: grid;
  place-items: center;
  z-index: 20;
  font-size: 12px;
}
.pet-toolbar {
  display: flex;
  align-items: center;
  gap: 5px;
}
.pet-toolbar .pet-model-label {
  flex: 1;
  min-width: 0;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  font-size: 11px;
  color: var(--muted);
  text-align: right;
  padding: 0 3px;
}
.quick-chat form .pet-toolbar button {
  background: transparent;
  color: var(--muted);
  flex-shrink: 0;
}
.quick-chat form .pet-toolbar button:hover {
  background: var(--hover);
  color: var(--text);
}
.quick-chat form .pet-toolbar button.full {
  color: var(--danger);
}
.quick-chat form .pet-toolbar button.pet-send {
  background: var(--accent);
  color: var(--on-accent);
}
.pet-popover {
  position: absolute;
  bottom: 108px;
  left: 10px;
  right: 10px;
  max-height: 220px;
  overflow: auto;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 13px;
  box-shadow: 0 4px 24px #0003;
  padding: 10px;
  z-index: 12;
  font-size: 11px;
  display: flex;
  flex-direction: column;
  gap: 7px;
}
.quick-chat form .pet-popover button {
  width: auto;
  height: auto;
  min-height: 30px;
  border-radius: 7px;
  background: transparent;
  color: var(--text);
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 7px;
  text-align: left;
}
/* The chosen mode is the checked radio (the 0.6.1 rule used an unscoped .chosen class). */
.quick-chat form .pet-popover button:hover,
.quick-chat form .pet-popover button[role="radio"][aria-checked="true"] {
  background: var(--hover);
}
.quick-chat form .pet-popover button[aria-disabled="true"] {
  color: var(--secondary);
}
.pet-modes {
  display: flex;
  flex-direction: column;
  gap: 2px;
}
.pet-full-warning,
.pet-permission-note {
  margin: 0;
  padding: 6px 8px;
  border-radius: 7px;
  font-size: 11px;
  line-height: 1.45;
}
.pet-full-warning {
  border: 1px solid var(--danger-line);
  color: var(--danger);
}
.pet-permission-note {
  background: var(--hover);
  color: var(--text);
}
.pet-popover-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
}
.pet-popover-heading strong {
  font-size: 12px;
}
.pet-popover small {
  color: var(--muted);
  font-size: 11px;
  line-height: 1.4;
}
.pet-popover .model-selectors {
  flex-direction: column;
  align-items: stretch;
}
.pet-popover .composer-select {
  display: flex;
  flex-wrap: nowrap;
  min-width: 0;
}
.pet-popover .model-selectors select {
  flex: 1;
  width: 0;
  max-width: none;
  font-size: 11px;
}
.pet-popover select,
.pet-popover input:not([type="checkbox"]) {
  background: var(--hover);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 6px;
  color: var(--text);
  min-width: 0;
}
.pet-effort {
  display: flex;
  flex-direction: column;
  gap: 5px;
}
.pet-mode span {
  flex: 1;
}
.pet-mode small {
  display: block;
  margin-top: 3px;
}
.pet-tool-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  border-top: 1px solid var(--border);
  padding-top: 5px;
}
.pet-tool-grid i {
  width: 7px;
  height: 7px;
  border-radius: 50%;
  background: var(--border);
}
.pet-tool-grid i.on {
  background: var(--accent);
}
.pet-image-note {
  font-size: 11px;
  color: var(--muted);
}
.pet-history-images {
  display: flex;
  gap: 5px;
}
.pet-history-images img {
  max-width: 120px;
  max-height: 90px;
  object-fit: contain;
  border-radius: 5px;
}
.quick-chat form .pet-attachments button {
  background: transparent;
  color: var(--muted);
  width: 24px;
  height: 24px;
  font-size: 14px;
}
</style>
