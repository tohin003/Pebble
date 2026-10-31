<script setup lang="ts">
import { computed, defineAsyncComponent, nextTick, onMounted, onUnmounted, reactive, ref, watch } from "vue";
import { getCurrentWindow } from "@tauri-apps/api/window";
import {
  PhPlus,
  PhArrowUp,
  PhStop,
  PhFolder,
  PhCaretDown,
  PhGearSix,
  PhSidebarSimple,
  PhArrowsOutSimple,
  PhArrowsInSimple,
  PhPushPin,
  PhShieldCheck,
  PhCheck,
  PhX,
  PhPaperclip,
  PhGlobe,
  PhCode,
  PhArrowRight,
  PhCommand,
  PhChatCircle,
  PhCpu,
  PhTrash,
  PhArrowCounterClockwise,
  PhPlay,
  PhWarningCircle,
  PhMagnifyingGlass,
  PhMoon,
  PhSun,
  PhMonitor,
  PhCircleNotch,
  PhPencilSimple,
} from "@phosphor-icons/vue";
import { useCompanion } from "./avatarActivity";
import { AVATAR_RANK } from "./companionPointer";
import ModelPicker from "./components/ModelPicker.vue";
import Avatar from "./components/Avatar.vue";
import StatusLine from "./components/StatusLine.vue";
// Phase 46 (1.9.29, D195): replies with the free upgrades (tables, mermaid diagrams, steps); Markdown.vue draws the rest
import ReplyBody from "./components/ReplyBody.vue";
import TokenStats from "./components/TokenStats.vue";
import RunPanel from './components/RunPanel.vue';
import AttemptStream from './components/AttemptStream.vue';
import { keysLabel, matchShortcut, SHORTCUTS, shortcutsFor, type ShortcutId } from './shortcuts';
import ActionReview from './components/ActionReview.vue';
import ConnectorEffectReview from './components/ConnectorEffectReview.vue';
import { addStoredAfter, applyRunEvent } from './runEvents';
// Phase 22: the Details drawer, the thinking disclosure, the slash menu and message actions.
import DetailsDrawer, { readDrawer, writeDrawer, type DrawerTab } from './components/DetailsDrawer.vue';
import ThinkingDisclosure from './components/ThinkingDisclosure.vue';
import WorkView from './components/WorkView.vue';
import type { GitAccess } from './components/BranchCard.vue';
import { taskLine } from './workView';
import SlashMenu from './components/SlashMenu.vue';
import MessageActions from './components/MessageActions.vue';
import { contextShare, createActivityStore, isLiveState, liveDetail, RING_AT, rows, validId, WAITING_AFTER_MS, WAITING_TEXT, waitingLong } from './activity';
import { coalesce } from './statusWords';
import { MODES, modeName } from "./permissionModes";
import { createScrollStick } from "./scrollStick";
import {
  argumentOptions, availableCommands, chooseSlash, clearRecent, completeArgument, defaultSlashIndex, filterSlash, moveSlash, parseSlash,
  readRecent, recordUse, validHost, type HostCommand, type SlashAction, type SlashData, type SlashItem,
} from './slashCommands';
// Phase 22 bundle split (design study §11): panels that are not on screen when the window opens load
// on first use, each in its own chunk: Settings, the artifact canvas with its renderers, export, the
// command palette, the maintenance screen and everything that lives only in the Details drawer.
const AdvancedSettings = defineAsyncComponent(() => import("./components/AdvancedSettings.vue"));
const NotificationSettings = defineAsyncComponent(() => import("./components/NotificationSettings.vue"));
const TelemetrySettings = defineAsyncComponent(() => import("./components/TelemetrySettings.vue"));
const ArtifactPanel = defineAsyncComponent(() => import("./components/ArtifactPanel.vue"));
const ExportPanel = defineAsyncComponent(() => import("./components/ExportPanel.vue"));
const CommandPalette = defineAsyncComponent(() => import("./components/CommandPalette.vue"));
const UpgradeMaintenance = defineAsyncComponent(() => import("./components/UpgradeMaintenance.vue"));
const GoalPanel = defineAsyncComponent(() => import("./components/GoalPanel.vue"));
const ModelHandoff = defineAsyncComponent(() => import("./components/ModelHandoff.vue"));
const ResearchSources = defineAsyncComponent(() => import("./components/ResearchSources.vue"));
const CompanionWorkflows = defineAsyncComponent(() => import("./components/CompanionWorkflows.vue"));
const CodingWorkflows = defineAsyncComponent(() => import("./components/CodingWorkflows.vue"));
const ScreenSessions = defineAsyncComponent(() => import("./components/ScreenSessions.vue"));
const PebbleBrowser = defineAsyncComponent(() => import("./components/PebbleBrowser.vue"));
import { invoke } from "@tauri-apps/api/core";
import { STEALTH_HONEST, STEALTH_OFF_TEXT, STEALTH_ON_TEXT } from "./stealthVeil";
import { listen } from "@tauri-apps/api/event";
import {
  attachBytes,
  native,
  openExternal,
  rpc,
  RpcError,
  selectFile,
  subscribe,
  setCompact,
  selectAttachments,
  subscribeDrops,
} from "./bridge";
import type { Action, Chat, CompanionSettings, PrototypeFeedback, State } from "./types";

const maintenance = ref<any>(null);
const state = ref<State>({
  chats: [],
  model: {
    state: "unloaded",
    configured: false,
    name: "Choose a local model",
    path: "",
    context: 4096,
    kind: "gguf",
  },
  guard: { available: false, error: null },
  active: null,
  settings: {
    theme: "system",
    reduced_motion: false,
    stealth: false,
    memory_enabled: true,
    fast_replies: "long_tasks",
    permissions: {
      mode: "ask",
      tools: {
        files: true,
        edits: true,
        tests: true,
        apps: true,
        web: true,
        terminal: true,
        plugins: true,
        views: true,
        screen: false,
      },
    },
    companion: { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true },
  },
  data_dir: "",
  platform: "darwin",
  version: "", // the service's own version arrives with the first state()
});
// Reviewed model handoff (main window only): offered after a model change for a settled run. Phase 24 (V1-002):
// `release` names the route classes the person is reviewing for release ("device": earlier messages on this Mac).
const handoffFor = ref<{ chatId: string; runId: string; release?: string[] } | null>(null);
// V1-002 (t24-data): a cloud request leaves out this conversation's on-device messages; the host says so once per
// run (a `progress` note carrying `handoff`), and switching from a model on this Mac to a cloud one says so at once.
// "Review…" opens the existing reviewed handoff with release ['device']; nothing is sent without that review.
const DEVICE_NOTICE = "Earlier messages were on this Mac. Review what is sent…";
const deviceNotices = reactive(new Map<string, { runId: string | null; profileId: string | null; label: string }>());
const chat = ref<Chat | null>(null),
  input = ref(""),
  busy = ref(false),
  booting = ref(true),
  compact = ref(false),
  sidebar = ref(true);
// Phase 22: the run inspector, goal, schedules, project tools, windows and the browser live in one
// right-hand Details drawer (never the chat column), which replaces the ⌘J "Activity & review"
// aside and keeps its key and button. Its tabs follow the design study (§10.3): Activity, Task,
// Context, Changes, Tools, plus (merge, i22) Browser: Pebble's browser and Pebble for Chrome, the
// panel the extension's popup sends the person to for its pairing code. Its open state, tab and width
// are remembered for this window; storage that is missing or refuses keeps the defaults.
const DRAWER_KEY = "pebble.details.main";
const DRAWER_TABS = ["activity", "task", "context", "changes", "tools", "browser"];
// Sections the person asked for in this window (through "More" or a slash command) stay listed
// even while they hold nothing yet.
const revealed = reactive(new Set<string>());
const hasGoal = ref(false);
const storage = (() => {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
})();
const remembered = readDrawer(storage, DRAWER_KEY, DRAWER_TABS);
const drawerOpen = ref(remembered.open),
  drawerTab = ref(remembered.tab || "task"),
  drawerWidth = ref(remembered.width),
  viewport = ref(window.innerWidth);
const drawer = ref<InstanceType<typeof DetailsDrawer> | null>(null),
  detailsButton = ref<HTMLButtonElement | null>(null);
watch([drawerOpen, drawerTab, drawerWidth], () =>
  writeDrawer(storage, DRAWER_KEY, { open: drawerOpen.value, tab: drawerTab.value, width: drawerWidth.value }),
);
const workflowRevision = ref(0);
// Phase 17: goal panel refreshes, the command palette and the service restart after an offline event.
const goalRevision = ref(0);
const palette = ref(false);
const serviceOffline = ref(false), restartAttempts = ref(0), restartAfter = ref(0);
async function restartService() {
  const wait = restartAfter.value - Date.now();
  if (wait > 0) { error.value = `Restart is available again in ${Math.ceil(wait / 1000)} s.`; return; }
  restartAfter.value = Date.now() + [1, 5, 30][Math.min(restartAttempts.value, 2)] * 1000;
  restartAttempts.value++;
  try {
    await invoke('service_restart');
    for (let tries = 0; tries < 60; tries++) {
      try { await rpc('state'); location.reload(); return; } catch { await new Promise(resolve => setTimeout(resolve, 500)); }
    }
    error.value = 'The service restarted but has not answered yet. Try again shortly.';
  } catch (cause) { error.value = cause instanceof Error ? cause.message : String(cause); }
}
// Phase 16 screen sessions and Pebble's browser (main window only).
const screenRevision = ref(0);
// Phase 22 (s22-storage): a calm note about Pebble's data folder (the move into ~/.pebble, a settings.json
// change that waits for the person, or one Pebble could not read), with a way to Settings -> Data & storage.
const storageNotice = ref("");
const STORAGE_NOTES: Record<string, string> = {
  settings_invalid: "settings.json has a problem, so Pebble kept your last good settings.",
  settings_pending: "A change in settings.json gives Pebble more access and waits for your confirmation.",
};
// Phase 24 (t24-data): the one-time iCloud note (V1-058) and a Documents memory change waiting for the person (V1-004).
const ICLOUD_NOTE = "Your Documents folder syncs with iCloud Drive, so Pebble's readable copies there sync too. You can turn the copies off or move them in Settings → Data & storage.";
const FORGET_NOTE = "You removed several saved memories from a file in Documents. Pebble waits for you to confirm before it forgets them.";
const storageAttention = ref(false);
function storageEvent(name: string, d: Record<string, unknown> | null) {
  if (name === "storage") {
    if (d?.changed === "recently_deleted") refresh().catch(fail); // a conversation was hidden or restored
    if (d?.changed === "recently_deleted") void leaveIfTrashed(); // V1-018: never keep writing into one
    if (d?.changed === "deleted") {
      clearQuizDrafts(d.deleted_artifacts); // V1-156: its views' saved answers leave with it
      if (typeof d.chat_id === "string" && d.chat_id === chat.value?.id) void leaveChat();
    }
    if (d?.changed === "icloud" && d.icloud_first_seen === true) storageNotice.value = ICLOUD_NOTE;
    if (d?.changed === "forget_confirm") {
      storageAttention.value = true;
      storageNotice.value = FORGET_NOTE;
    }
    const note = STORAGE_NOTES[String(d?.changed)];
    if (note) storageNotice.value = note;
    return;
  }
  const message = typeof d?.message === "string" ? d.message.slice(0, 300) : "";
  if (d?.state === "checking") storageNotice.value = "Moving Pebble's data to ~/.pebble…";
  else if (d?.state === "migrated") storageNotice.value = "Pebble's data now lives in ~/.pebble; readable copies are in Documents/Pebble.";
  else if (["refused", "failed", "conflict"].includes(String(d?.state)))
    storageNotice.value = (message || "Pebble kept using its previous data folder.") + " See Settings → Data & storage.";
}
// Phase 24 (t24-browser, V1-043/V1-124): Pebble for Chrome is waiting to pair, or a tab was shared: a calm note that
// opens the Browser section. The event carries no code, title or address.
const browserNotice = ref("");
function browserEvent(d: Record<string, any> | null) {
  if (d?.kind === "pairing" && d.state === "pending") browserNotice.value = "Pebble for Chrome is asking to pair. Its code is in Details › Browser.";
  else if (d?.kind === "pairing" && d.state === "locked") browserNotice.value = "Pairing is paused for a moment after several wrong codes.";
  else if (d?.kind === "pairing" && d.state === "paired") browserNotice.value = "";
  else if (d?.kind === "shared") {
    browserNotice.value = "You shared a Chrome tab with Pebble. You can end access in Details › Browser.";
    screenRevision.value++;
  }
}
/** V1-156: a deleted conversation's views leave no saved quiz answers in this window's storage. */
function clearQuizDrafts(ids: unknown) {
  if (!Array.isArray(ids) || !storage) return;
  try {
    const keys = Array.from({ length: storage.length }, (_, i) => storage.key(i) || "");
    for (const id of ids)
      if (typeof id === "string" && id)
        for (const key of keys) if (key === "pebble.quiz." + id || key.startsWith("pebble.quiz." + id + ".")) storage.removeItem(key);
  } catch {
    /* storage refused: nothing was kept there either */
  }
}
/** The open conversation was deleted or moved to Recently deleted elsewhere: open a new one. */
async function leaveChat() {
  chat.value = null;
  await createChat();
}
async function leaveIfTrashed() {
  const open = chat.value;
  if (!open || !native) return;
  try {
    const current = await rpc<Chat>("get_chat", { id: open.id });
    if (current.trashed && chat.value?.id === open.id) await leaveChat();
  } catch {
    /* gone or unreadable: the next action says so */
  }
}
// Pinned app identities {id, name, bundle_id, team_id}; empty until an app-identity route exists (after E-N2).
const screenApps = ref<{ id: string; name: string; bundle_id?: string; team_id?: string | null }[]>([]);
const modal = ref<"settings" | "apps" | "url" | "terminal" | "delete" | null>(
    null,
  ),
  error = ref(""),
  // Phase 24: a toast may carry one action (Undo); its polite region stays mounted (V1-118).
  toast = ref<{ text: string; action?: { label: string; run: () => void } } | null>(null),
  activity = ref<any[]>([]),
  attachmentBusy = ref(false),
  dropHover = ref(false);
const sources = ref<
    {
      id: string;
      name: string;
      characters: number;
      preview?: string;
      kind?: string;
      notice?: string;
    }[]
  >([]),
  url = ref(""),
  apps = ref<{ id: string; name: string }[]>([]),
  appSearch = ref(""),
  actionBusy = ref("");
const scroll = ref<HTMLElement | null>(null),
  composer = ref<HTMLTextAreaElement | null>(null),
  showAttach = ref(false);
// Phase 24 (V1-130): streamed text follows only a person at the end of the conversation; sending always shows it.
const stick = createScrollStick(() => scroll.value, nextTick);
const settingsTab = ref("models"),
  settingsIntent = ref(""),
  needsModel = ref(false); // Phase 45 (F019)
const canvas = ref(false),
  selectedArtifact = ref("");
const availableEfforts = computed(
  () => state.value.model.efforts || ["default"],
);
const effort = computed(() => state.value.model.effort || "default");
const effortLabel = (level: string) =>
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
  })[level] || level;
async function changeEffort(event: Event) {
  await safe(async () => {
    await rpc("model_effort", {
      effort: (event.target as HTMLSelectElement).value,
    });
    await refresh();
  });
}
const onMac = (id?: string | null) => {
  const p = state.value.profiles?.find((profile) => profile.id === id);
  return !!p && (p.kind === "gguf" || p.local === true);
};
async function changeModel(id: string) {
  await safe(async () => {
    const before = state.value.model.profile_id;
    await rpc("model_select", {
      profile_id: id,
    });
    await refresh();
    const run = chat.value?.runs?.find(r => ['paused', 'interrupted', 'failed', 'budget_exhausted'].includes(r.state));
    handoffFor.value = chat.value && run ? { chatId: chat.value.id, runId: run.id } : null;
    if (handoffFor.value) openDrawer("task"); // the reviewed handoff offer sits in the drawer's Task tab
    // V1-002: from this Mac to a cloud model in a conversation that ran here: its earlier messages stay out of the
    // cloud requests unless the person reviews and includes them.
    const latest = chat.value?.runs?.find((r) => !isLiveState(r.state));
    if (chat.value && latest && onMac(before) && !onMac(id))
      deviceNotices.set(chat.value.id, { runId: latest.id, profileId: id, label: DEVICE_NOTICE });
    else if (chat.value && onMac(id)) deviceNotices.delete(chat.value.id);
  });
}
const deviceNotice = computed(() => (chat.value ? deviceNotices.get(chat.value.id) || null : null));
/** V1-002: open the existing reviewed handoff (Details › Task), asking it to review the on-device messages too. */
function reviewDeviceHistory() {
  const notice = deviceNotice.value, c = chat.value;
  if (!notice || !c || !notice.runId) return;
  handoffFor.value = { chatId: c.id, runId: notice.runId, release: ["device"] };
  openDrawer("task", false, true);
}
async function handoffStarted(chatId: string) {
  handoffFor.value = null;
  if (chatId) await loadChat(chatId);
  else await refreshRun();
}
function openArtifact(id: string) {
  selectedArtifact.value = id;
  canvas.value = true;
  drawerOpen.value = false;
  if (window.innerWidth < 1050) sidebar.value = false;
}
// V1-129: closing the canvas returns focus to the button that opens it (or the message box when it is gone).
const canvasButton = ref<HTMLButtonElement | null>(null);
function closeCanvas() {
  canvas.value = false;
  nextTick(() => (canvasButton.value || composer.value)?.focus());
}
// V1-127: the canvas's Export… opens this view's export section in Details › Task.
function exportView(id: string) {
  selectedArtifact.value = id;
  openDrawer("task", false, true);
  nextTick(() => document.querySelector<HTMLElement>("#details-export")?.scrollIntoView?.({ block: "nearest" }));
}
// "Send feedback" from an app demo prefills the composer; the person edits and sends it. Only the validated
// screen ID and host-authored words are used, never generated text.
function demoFeedback(payload: PrototypeFeedback) {
  const view = chat.value?.artifacts?.find((a) => a.id === payload.artifact_id && a.kind === "prototype");
  if (!view || payload.chat_id !== chat.value?.id || !/^[A-Za-z0-9_-]{1,64}$/.test(payload.screen_id)) return;
  const prefix = `Feedback on the app demo (revision ${payload.revision}, screen ${payload.screen_id}): `;
  prompt((input.value.trim() ? input.value.trimEnd() + "\n" : "") + prefix);
}
function showReviews(toggle = false) {
  openDrawer("activity", toggle);
  // Phase 45 (F127, audit round R4): in a normal-size window the card's Allow and Don't allow stayed below the drawer's
  // edge; the first waiting card's buttons come into view and its title takes the keyboard (one Tab to its buttons)
  if (!drawerOpen.value) return;
  nextTick(() => nextTick(() => {
    const card = document.querySelector<HTMLElement>("#details-drawer .action-card.pending");
    card?.querySelector(".action-buttons")?.scrollIntoView?.({ block: "nearest" });
    card?.querySelector<HTMLElement>(".action-title")?.focus({ preventScroll: true });
  }));
}
// Open the drawer (at `tab` when given); `toggle` closes it instead when that tab is already showing.
// A section asked for by name is listed even before it holds anything (Activity follows its data).
function openDrawer(tab?: string, toggle = false, focus = false) {
  if (toggle && drawerOpen.value && !canvas.value && (!tab || activeTab.value === tab)) return closeDrawer();
  if (tab && tab !== "activity") revealed.add(tab);
  if (tab) drawerTab.value = tab;
  canvas.value = false;
  drawerOpen.value = true;
  if (focus) nextTick(() => drawer.value?.focus());
}
function closeDrawer() {
  const inside = !!document.activeElement?.closest?.("#details-drawer");
  drawerOpen.value = false;
  if (inside) nextTick(() => detailsButton.value?.focus());
}
function toggleDrawer(focus = false) {
  if (drawerOpen.value && !canvas.value) closeDrawer();
  else openDrawer(undefined, false, focus);
}
function toggleCanvas() {
  if (canvas.value) canvas.value = false;
  else
    openArtifact(
      selectedArtifact.value || chat.value?.artifacts?.at(-1)?.id || "",
    );
}
function openSettings(section = "models", intent = "") {
  settingsTab.value = section;
  settingsIntent.value = intent; // Phase 45 (F002): "add-provider" opens the provider form at once
  modal.value = "settings";
  if (section === "storage") storageAttention.value = false;
}
const deleteTarget = ref<Chat | null>(null);
// Phase 45 (F118, audit round R3): the dialog names the conversation it deletes ("Delete this conversation?" did not).
const deleteHeading = computed(() => {
  const title = (deleteTarget.value?.title || "").trim();
  return title ? `Delete “${title.length > 60 ? title.slice(0, 59) + "…" : title}”?` : "Delete this conversation?";
});
const terminalCommand = ref(""),
  terminalNetwork = ref(false);
async function proposeCommand() {
  await safe(async () => {
    const c = await ensureChat();
    await rpc("terminal_propose", {
      id: c.id,
      command: terminalCommand.value,
      network: terminalNetwork.value,
    });
    modal.value = null;
    terminalCommand.value = "";
    showReviews();
    chat.value = await rpc("get_chat", { id: c.id });
  });
}
function deleteFromSidebar(id: string) {
  deleteTarget.value = state.value.chats.find((c) => c.id === id) || null;
  modal.value = "delete";
}
let previousFocus: HTMLElement | null = null;
watch(modal, async (value) => {
  if (value) {
    previousFocus = document.activeElement as HTMLElement;
    await nextTick();
    (
      document.querySelector(".modal input, .modal button") as HTMLElement
    )?.focus();
  } else previousFocus?.focus();
});
let dropUnsubscribe = () => {};
let remotePairing = ""; // Phase 46: the six digits Settings › iPhone was last opened for
let unsubscribe = () => {},
  resizeUnsubscribe = () => {},
  chatUnsubscribe = () => {},
  toastTimer = 0;
const isWorking = computed(
  () => state.value.active === chat.value?.id && !!state.value.active,
);
const pending = computed(
  () => chat.value?.actions.filter((a) => a.status === "pending") || [],
);
const visibleActions = computed(
  () =>
    chat.value?.actions.filter(
      (a) => !["superseded", "rejected"].includes(a.status),
    ) || [],
);
// Phase 19 P9 (design study §2.1–§2.6): the companion follows host events and this window's own counts
// (companionState.ts). Progress labels, which can embed paths, stay in the activity list and never become words.
const companionSettings = computed<CompanionSettings>(
  () => state.value.settings.companion || { pointer: true, reactions: true, status_words: "rotate", ui_guidance: true },
);
const companion = useCompanion({
  approvals: () => pending.value.length,
  questions: () => chat.value?.questions?.filter((q) => q.status === "pending").length || 0,
  notices: () => state.value.job_notifications?.length || 0,
  model: () => state.value.model.state,
  reactions: () => companionSettings.value.reactions,
});
const avatarState = companion.mood;
// "Reply finished" is announced whatever companion.reactions says: it follows the host's completed run.
const companionOutcome = computed(() => {
  const s = companion.state.value;
  return s.run?.state === "completed" && s.completedUntil > 0 ? "completed" : companion.pose.value.outcome;
});
// Phase 22: what Pebble is doing and did, per run (activity.ts). The host's `run_activity` record is
// read once per live stretch (for its own start time) and when a disclosure opens; a service or
// shell without the route turns it off quietly, and the progress events the window gets fill in.
const activityStore = createActivityStore({
  fetch: (runId, after) => rpc("run_activity", { run_id: runId, after }),
});
function loadActivity(runId: string) {
  void activityStore.load(runId);
}
const liveOpen = ref(false);
const liveRecord = computed(() => {
  const record = activityStore.liveFor(chat.value?.id);
  return record && isLiveState(record.state) ? record : null;
});
watch(() => liveRecord.value?.runId, (run, before) => {
  // Phase 46 (1.9.29): with the work view on, a new task's cards are open (the person asked to see Pebble's working
  // window); the rows stay closed until the status line opens them, as before
  if (run !== before) liveOpen.value = !!run && workView.value;
});
watch(
  () => [liveRecord.value?.runId, liveRecord.value?.loaded] as const,
  ([run, loaded]) => {
    if (run && !loaded && activityStore.state.supported !== false) loadActivity(run);
  },
  { immediate: true },
);
// Secondary text after the status word (design study §3.3): the host's running step, or its latest
// reasoning headline while the model works. It never chooses the word; host states (review,
// question, …) show none; each text stays at least 1.5 s before the next replaces it.
const KEY_WORDS = new Set(["question", "review", "blocked", "uncertain", "offline", "paused", "error", "sending"]);
const statusDetail = ref<string | null>(null);
const dwell = coalesce<string | null>((text) => (statusDetail.value = text), 1500);
// Phase 24 (V1-159): 8 s into a run with nothing reported (no step, summary or reply text), the secondary text says
// so; the word and the screen reader's sentence stay the key's.
const waitTick = ref(0);
let waitTimer = 0;
watch(
  () => [liveRecord.value?.runId, liveRecord.value?.hostStart ?? liveRecord.value?.startedAt] as const,
  ([run, start]) => {
    window.clearTimeout(waitTimer);
    if (!run || typeof start !== "number") return;
    waitTimer = window.setTimeout(() => waitTick.value++, Math.max(0, start + WAITING_AFTER_MS - Date.now()));
  },
  { immediate: true },
);
onUnmounted(() => window.clearTimeout(waitTimer));
const replied = (runId: string) => [...activityStore.state.messages.values()].includes(runId);
watch(
  () => {
    const status = companion.pose.value.status;
    if (status && KEY_WORDS.has(status)) return null;
    const record = liveRecord.value;
    return liveDetail(record) ?? (waitTick.value >= 0 && record && waitingLong(record, Date.now(), replied(record.runId)) ? WAITING_TEXT : null);
  },
  (text) => {
    if (text !== null) return dwell.push(text);
    dwell.cancel();
    statusDetail.value = null;
  },
  { immediate: true },
);
onUnmounted(() => dwell.cancel());
// The elapsed time is exact once the host's clock is known (its active seconds leave waits out).
const hostStart = computed(() => liveRecord.value?.hostStart ?? null);
function recordFor(message: { id: string }) {
  const record = activityStore.forMessage(message.id);
  return record && !isLiveState(record.state) ? record : null;
}
// The latest settled run that wrote no reply of its own (it stopped first): its row closes the list.
const orphanRecord = computed(() => {
  const latest = chat.value?.runs?.[0];
  const record = latest ? activityStore.record(latest.id) : null;
  if (!record || isLiveState(record.state) || (!rows(record).length && !record.reasoning)) return null;
  for (const run of activityStore.state.messages.values()) if (run === record.runId) return null;
  return record;
});
const lastUser = computed(() => {
  const list = chat.value?.messages || [];
  for (let i = list.length - 1; i >= 0; i--) if (list[i].role === "user") return list[i];
  return null;
});
const lastReply = computed(() => {
  const list = chat.value?.messages || [];
  for (let i = list.length - 1; i >= 0; i--) if (list[i].role === "assistant") return list[i];
  return null;
});
// Retry and Edit are append-only (design study §5.3): each sends words through send() as a new
// turn. Nothing stored changes, nothing runs without the person's click, nothing is replayed (a new
// turn is a new run with new approvals). The markers below are this window's labels only.
const markers = reactive(new Map<string, string>());
function questionFor(reply: { id: string }) {
  const list = chat.value?.messages || [];
  const at = list.findIndex((m) => m.id === reply.id);
  for (let i = at - 1; i >= 0; i--) if (list[i].role === "user") return list[i];
  return null;
}
function canRetry(message: { id: string }) {
  return message.id === lastReply.value?.id && !!questionFor(message) && !isWorking.value;
}
function retryReason(message: { id: string }) {
  return questionFor(message)?.attachments?.length ? "Attachments aren't sent again; attach them to a new message to ask with them." : "";
}
async function retry(message: { id: string }) {
  const question = questionFor(message);
  if (!question || retryReason(message) || busy.value || isWorking.value) return;
  markers.set(message.id, "Earlier reply · asked again below");
  await send(question.content);
}
const editing = ref<string | null>(null),
  editText = ref("");
let editorEl: HTMLTextAreaElement | null = null; // a function ref: the editor sits inside the message list
const keepEditor = (el: unknown) => {
  editorEl = (el as HTMLTextAreaElement | null) || null;
};
function startEdit(message: { id: string; content: string }) {
  if (isWorking.value) return;
  editing.value = message.id;
  editText.value = message.content;
  nextTick(() => editorEl?.focus());
}
// V1-129: cancelling an edit returns focus to that message's Edit button.
function cancelEdit(id: string) {
  editing.value = null;
  nextTick(() => {
    const row = Array.from(document.querySelectorAll<HTMLElement>(".message[data-message-id]")).find((el) => el.dataset.messageId === id);
    (row?.querySelector<HTMLElement>('[aria-label="Edit your message"]') || composer.value)?.focus();
  });
}
async function sendEdited(message: { id: string }) {
  const text = editText.value.trim();
  if (!text || busy.value || isWorking.value) return;
  editing.value = null;
  markers.set(message.id, "Edited · new version below");
  await send(text);
}
const wroteFiles = computed(() => !!chat.value?.actions.some((a) => a.kind === "edit" && a.status === "applied"));
async function copyLastReply() {
  const reply = lastReply.value;
  if (!reply) return notify("There is no reply to copy yet.");
  try {
    await navigator.clipboard.writeText(reply.content);
    notify("Copied Pebble's last reply");
  } catch (cause) {
    fail(cause);
  }
}

// The drawer's tabs (design study §10.3). A tab with nothing to show is not listed; "More" offers it,
// and one the person asked for stays listed for this window.
const latestRun = computed(() => chat.value?.runs?.[0] || null);
const latestRecord = computed(() => activityStore.record(latestRun.value?.id));
const TAB_LABELS: Record<string, string> = { activity: "Activity", task: "Task", context: "Context", changes: "Changes", tools: "Tools", browser: "Browser" };
// Phase 24 (V1-124): one name, "Tools", for schedules and shared windows (the slash menu says "Details › Tools").
const MORE_LABELS: Record<string, string> = {
  task: "Goal, plan and agents", context: "Context", changes: "Changes and Git", tools: "Tools: schedules and shared windows",
  browser: "Browser and Pebble for Chrome",
};
// V1-124: Tools is listed whenever this conversation has a schedule or a shared window (live or waiting for its next
// task), so ending access is always one click away. Two reads per conversation, refreshed with the panels' events.
const hasTools = ref(false);
let toolsTicket = 0;
async function checkTools() {
  const c = chat.value;
  const mine = ++toolsTicket;
  if (!c || !native) return void (hasTools.value = false);
  const [jobs, sessions] = await Promise.all([
    rpc<{ jobs?: unknown[] }>("job_list", { id: c.id }).catch(() => null),
    rpc<{ state?: string; kind?: string }[]>("screen_sessions", { id: c.id }).catch(() => null),
  ]);
  if (mine !== toolsTicket || chat.value?.id !== c.id) return;
  const live = (Array.isArray(sessions) ? sessions : []).filter((s) => ["active", "handoff", "interrupted"].includes(s?.state || ""));
  const schedules = jobs?.jobs;
  hasTools.value = (Array.isArray(schedules) && schedules.length > 0) || live.length > 0;
  if (live.some((s) => s?.kind === "browser")) revealed.add("browser"); // a shared tab's controls are in Browser too
}
watch(() => [chat.value?.id, workflowRevision.value, screenRevision.value] as const, () => void checkTools(), { immediate: true });
const tabHasContent = computed<Record<string, boolean>>(() => {
  const c = chat.value, run = latestRun.value, record = latestRecord.value;
  return {
    activity: !!run || !!visibleActions.value.length || !!activity.value.length,
    task:
      hasGoal.value || !!c?.plan?.steps?.length || !!record?.delegated || !!record?.researched || !!c?.artifacts?.length ||
      (!!handoffFor.value && handoffFor.value.chatId === c?.id),
    context: !!run,
    changes: !!c?.workspace || wroteFiles.value,
    tools: !!state.value.job_notifications?.length || hasTools.value,
    browser: false, // listed once asked for (the browser slash action, or More); its panel reads its status only while shown
  };
});
const drawerTabs = computed<DrawerTab[]>(() =>
  !chat.value
    ? []
    : DRAWER_TABS.filter((id) => tabHasContent.value[id] || (id !== "activity" && id !== "context" && revealed.has(id))).map((id) => ({
        id,
        label: TAB_LABELS[id],
        badge: id === "activity" ? pending.value.length : id === "tools" ? state.value.job_notifications?.length || 0 : 0,
      })),
);
const moreTabs = computed<DrawerTab[]>(() =>
  !chat.value
    ? []
    : DRAWER_TABS.filter((id) => id !== "activity" && (id !== "context" || !!latestRun.value) && !drawerTabs.value.some((t) => t.id === id)).map((id) => ({
        id,
        label: MORE_LABELS[id],
      })),
);
const activeTab = computed(() => (drawerTabs.value.some((t) => t.id === drawerTab.value) ? drawerTab.value : drawerTabs.value[0]?.id || ""));
// Below 1,050 px (and in compact mode) the drawer overlays the chat as a sheet.
const drawerOverlay = computed(() => compact.value || viewport.value < 1050);
// V1-163: while that sheet is open, the conversation behind it is inert and a click beside it closes it.
const sheetOpen = computed(() => drawerOpen.value && !canvas.value && !!chat.value && drawerOverlay.value);
// Whether this conversation has a current goal (one goal_list read while the drawer is open).
let goalTicket = 0;
async function checkGoal() {
  const c = chat.value;
  if (!c || !native) return void (hasGoal.value = false);
  const mine = ++goalTicket;
  try {
    const list = await rpc<{ state?: string }[]>("goal_list", { chat_id: c.id, limit: 20 });
    if (mine === goalTicket && chat.value?.id === c.id)
      hasGoal.value = Array.isArray(list) && list.some((g) => ["active", "paused", "blocked"].includes(g?.state || ""));
  } catch {
    if (mine === goalTicket) hasGoal.value = false;
  }
}
watch(
  () => [drawerOpen.value, chat.value?.id, goalRevision.value] as const,
  ([open]) => {
    if (open) void checkGoal();
  },
  { immediate: true },
);
// Phase 24 (V1-120): an approval card says first, in one plain sentence, what changes and whether it can be undone;
// its status reads as words. The diff, command and technical facts follow.
const ACTION_STATES: Record<string, string> = {
  pending: "Needs your review", applied: "Done", rejected: "Not allowed", undone: "Undone", running: "Running",
  failed: "Failed", expired: "Expired", superseded: "Replaced", approved: "Approved",
  held: "Held by Pebble, not run",  // Phase 46 (1.9.28, D185): Full access's check held it and Pebble went on
};
const actionState = (status: string) => (Object.hasOwn(ACTION_STATES, status) ? ACTION_STATES[status] : String(status).replace(/_/g, " "));
// Phase 34 (W1): an ad hoc check RunTests added to the isolated copy (testing.CHECK_FILES), never the project's tests.
function isCheck(p: Record<string, any>): boolean {
  return !!p.check && typeof p.check === "object" && typeof p.check.content === "string";
}
function actionLead(action: Action): string {
  const p = action.payload || {};
  switch (action.kind) {
    case "edit": {
      const lead = p.baseline ? "Changes this file in your project. You can undo it afterwards." : "Creates this file in your project. You can undo it afterwards.";
      // Phase 34 (W1): the host's reason a test file change waits for the person.
      return typeof p.scope_review === "string" ? `${p.scope_review} ${lead}` : lead;
    }
    case "tests":
      // Phase 46 (1.9.28): with check_run the project's tests and Pebble's check run apart, each in its own copy
      if (isCheck(p) && p.check_run)
        return "Runs the project's tests, then Pebble's own check alone, each in a temporary copy. Your project files don't change.";
      return isCheck(p)
        ? "Runs Pebble's own check, added to a temporary copy for this run only. Your project files don't change."
        : "Runs the project's tests in a temporary copy. Your project files don't change.";
    case "command":
      return p.writable
        ? "Runs this command in your project, where it can change files. Pebble can't undo a command."
        : "Runs this command in your project without changing files.";
    case "plugin":
      return "Calls a tool of a connector you turned on. What it changes depends on that service; Pebble can't undo it.";
    case "git":
      return p.operation === "undo" ? "Undoes an earlier Git change." : "Runs this Git change in your project. You can propose an undo afterwards, as a new reviewed plan.";
    case "worktree":
      return p.operation === "retire_child_worktree"
        ? "Removes an agent's separate working copy of the project."
        : "Creates a separate working copy of your project for an agent. Your project stays as it is.";
    case "install":
      return "Installs the project's locked dependencies into a folder Pebble manages, outside your project.";
    case "app":
      if (p.document)  // Phase 45 (F200): this file, in this app
        return `Opens ${String(p.document)} in this app on your Mac${p.background ? ", without bringing it to the front" : ""}. Pebble won't control what happens inside it.`;
      return p.background  // Phase 43 (1.9.0): without bringing it to the front
        ? "Opens this app in the background on your Mac, without bringing it to the front. Pebble won't control what happens inside it."
        : "Opens this app on your Mac. Pebble won't control what happens inside it.";
    // Phase 40 (1.6.0) and Phase 42 (1.8.0): the Companion's asks and app windows.
    case "shell":
      return "Runs this command in Pebble's sandbox on your Mac.";
    case "file":
      return "Writes this file on your Mac. Pebble keeps a copy first, so you can undo it.";
    case "screen":  // Phase 45 (F166): the send-or-delete warning only for a press that looks like one
      return (Array.isArray(p.review?.reasons) ? p.review.reasons : []).some((r: unknown) => SENDING.has(String(r)))
        ? "Does this in the app window you shared. It may send, submit, save, delete or buy, and Pebble can't undo it."
        // Phase 45 (F210, audit round R8): typing on the keyboard read "Presses this"
        : /^(type|type_text|key)\b/.test(String(p.review?.action || "")) || typeof p.review?.text === "string"
          ? "Types this in the app window you shared. Pebble can't undo typing."
          : "Presses this in the app window you shared. Pebble can't undo a press.";
    case "screen_grant":  // Phase 45 (F165; F180: macOS's own alert, said before it comes; F187: a file panel)
      if (p.file_panel)
        return "This window is a file panel: it shows one of your folders, and Pebble would read the names of the files listed there. Allow it only if that is what you want.";
      // D136, D137 (the person): allowed for the conversation, up to 15 minutes; Full access presses without asking
      return "Lets Pebble see and use this one window in this conversation, for up to 15 minutes"
        + (p.screenshots ? ", with pictures of it when needed. " : ". ")
        + (p.full_access  // D141: Full access runs what your messages ask for; money always asks
          ? "In Full access Pebble runs the presses your messages ask for; anything that may spend money still asks."
          : "Most presses still ask you first.")
        + (p.screenshots ? " If macOS asks whether Pebble may bypass its window picker, choose Allow for the pictures." : "");
    case "url":
      return "Opens this address in your browser. Pebble can't see the page, so it can't confirm what happens there.";
    case "screen_raise":
      return "Brings this window to the front so Pebble can read it. macOS may switch to its desktop.";
    case "app_action":
      return "Asks this app to do one thing through its own scripting. Pebble never sends mail or deletes anything this way.";
    case "browser_grant":  // Phase 45 (D128): the window route opens a new window of the person's browser
      return p.route === "window"
        ? `Opens these sites in a new ${p.browser_name || "browser"} window that Pebble reads and uses in this conversation, for up to 15 minutes. Sending, buying or deleting still asks you.`
        : "Lets Pebble use these sites in its own tab in your browser in this conversation, for up to 15 minutes. Sending, buying or deleting still asks you.";
    default:
      return "";
  }
}
// The Activity tab: approvals waiting for the person first, then the steps and the task's own
// controls and output, then earlier actions.
const actionGroups = computed(() => [
  { id: "pending", label: "Needs your review", items: pending.value },
  { id: "earlier", label: "Earlier actions", items: visibleActions.value.filter((a) => a.status !== "pending") },
]);
// A new approval opens Activity and moves focus to its card, unless the person is typing.
const seenPending = new Set<string>();
function announceApprovals(actions: Action[]) {
  const fresh = actions.filter((a) => a.status === "pending" && !seenPending.has(a.id));
  actions.forEach((a) => a.status === "pending" && seenPending.add(a.id));
  if (!fresh.length || compact.value) return;
  showReviews();
  nextTick(() =>
    nextTick(() => {
      const active = document.activeElement as HTMLElement | null;
      if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return;
      document.querySelector<HTMLElement>("#details-drawer .action-card.pending .action-title")?.focus();
    }),
  );
}
// A small ring in the composer foot when the last request used at least 60 % of its window
// (design study §9.3); clicking it opens Context with the existing Compact action.
const contextUse = ref<{ used: number; window: number; percent: number } | null>(null);
let contextTicket = 0;
async function checkContext() {
  const c = chat.value, run = c?.runs?.[0];
  const mine = ++contextTicket;
  if (!c || !run || !native || isLiveState(run.state)) return void (contextUse.value = null);
  try {
    const reply = await rpc<unknown>("context_inspect", { chat_id: c.id, run_id: run.id });
    if (mine === contextTicket && chat.value?.id === c.id) contextUse.value = contextShare(reply, c.id, run.id);
  } catch {
    if (mine === contextTicket) contextUse.value = null;
  }
}
watch(() => [chat.value?.id, chat.value?.runs?.[0]?.id, chat.value?.runs?.[0]?.state] as const, () => void checkContext());
const attention = computed(() => pending.value.length + (state.value.job_notifications?.length || 0));
const detailsKeys = computed(() => keysLabel(SHORTCUTS.find((s) => s.id === "details")!, state.value.platform));
const exportTarget = computed(() => {
  const artifacts = chat.value?.artifacts || [];
  return artifacts.find((a) => a.id === selectedArtifact.value) || artifacts.at(-1) || null;
});
function onResize() {
  viewport.value = window.innerWidth;
}

// Phase 22 slash commands (design study §4.3): "/" at the start of the message box lists the host's
// own runtime commands and a few window actions (slashCommands.ts), then completes the command's
// arguments. Actions run the handler their button uses; host commands go through send() as text.
const hostCommands = ref<HostCommand[]>([]);
const slashIndex = ref(0),
  slashDismissed = ref<string | null>(null),
  slashNote = ref(""),
  composerFocused = ref(false),
  effortSelect = ref<HTMLSelectElement | null>(null),
  recent = ref<Record<string, number>>(readRecent(storage));
let catalogueRequested = false;
const slashContext = computed(() => {
  const latest = chat.value?.runs?.[0];
  return {
    chat: !!chat.value,
    run: !!latest,
    settled_run: !!latest && !isLiveState(latest.state),
    efforts: availableEfforts.value.length > 1 && !isWorking.value && !busy.value,
    reply: !!lastReply.value,
    working: isWorking.value,
  };
});
// Argument data the window reads on demand, once per conversation.
const slashData = reactive<{ skills: SlashData["skills"]; criteria: SlashData["criteria"]; agents: SlashData["agents"] }>({ skills: null, criteria: null, agents: null });
watch(() => chat.value?.id, () => Object.assign(slashData, { criteria: null, agents: null }));
const slashInput = computed(() => parseSlash(input.value));
const slashCommands = computed(() => availableCommands(hostCommands.value, slashContext.value, recent.value));
const slashArgs = computed(() => {
  const typed = slashInput.value;
  if (!typed || typed.stage !== "argument" || !slashCommands.value.some((c) => c.kind === "host" && c.name === typed.name)) return null;
  return argumentOptions(typed, {
    runs: chat.value?.runs || [],
    views: (chat.value?.artifacts || []).map((a) => ({ id: a.id, title: a.title })),
    ...slashData,
  });
});
const slashItems = computed<SlashItem[]>(() => {
  const typed = slashInput.value;
  if (!typed) return [];
  return typed.stage === "name" ? filterSlash(slashCommands.value, typed.name) : slashArgs.value?.options || [];
});
const slashHeading = computed(() => {
  const typed = slashInput.value;
  const command = typed?.stage === "argument" ? slashCommands.value.find((c) => c.name === typed.name) : null;
  return command ? `/${command.name} ${command.hint}`.trim() : "";
});
// The menu lists commands while a name is typed, and argument options while there are some.
const slashOpen = computed(() => {
  const typed = slashInput.value;
  if (!typed || !composerFocused.value || booting.value || slashDismissed.value === input.value) return false;
  return typed.stage === "name" || slashItems.value.length > 0;
});
// Commands: the first is chosen when its name matches what was typed (defaultSlashIndex). Arguments: nothing is chosen
// until the person moves or types, so Enter after "/goal " sends "/goal" as typed.
watch(
  () => [input.value, slashItems.value.length] as const,
  () => {
    const typed = slashInput.value;
    slashIndex.value = typed?.stage === "name" ? defaultSlashIndex(slashItems.value, typed.name) : typed?.partial && slashItems.value.length ? 0 : -1;
  },
);
watch(slashInput, (typed) => {
  if (typed && !catalogueRequested) loadCatalogue();
  const source = slashArgs.value?.source;
  if (source) void loadSlashData(source);
});
async function loadSlashData(source: "skills" | "criteria" | "agents") {
  const c = chat.value;
  if (!native || !c || slashData[source] !== null) return;
  try {
    if (source === "skills") {
      const list = await rpc<unknown>("extensions");
      slashData.skills = (Array.isArray(list) ? list : [])
        .filter((e) => e && e.kind === "skill" && e.enabled && typeof e.id === "string" && typeof e.name === "string")
        .map((e) => ({ id: e.id as string, name: e.name as string }));
    } else if (source === "criteria") {
      const goals = await rpc<{ id?: string; state?: string }[]>("goal_list", { chat_id: c.id, limit: 20 });
      const current = (Array.isArray(goals) ? goals : []).find((g) => ["active", "paused", "blocked"].includes(g?.state || ""));
      const goal = current?.id ? await rpc<{ criteria?: { id?: string; text?: string }[] }>("goal_get", { chat_id: c.id, goal_id: current.id }) : null;
      if (chat.value?.id === c.id)
        slashData.criteria = (goal?.criteria || []).filter((x) => typeof x?.id === "string").map((x) => ({ id: x.id as string, text: String(x.text || "") }));
    } else if (c.runs?.[0]) {
      const tree = await rpc<{ nodes?: { agent_id?: string; name?: string }[] }>("agent_list", { chat_id: c.id, run_id: c.runs[0].id });
      if (chat.value?.id === c.id)
        slashData.agents = (tree?.nodes || []).filter((n) => typeof n?.agent_id === "string").map((n) => ({ id: n.agent_id as string, name: n.name }));
    }
  } catch {
    /* no completion for this argument; the command can still be typed */
  }
}
async function loadCatalogue() {
  catalogueRequested = true;
  if (!native) return;
  try {
    hostCommands.value = validHost(await rpc("command_catalogue"));
  } catch (cause) {
    slashNote.value = "Service commands are unavailable: " + (cause instanceof Error ? cause.message : String(cause));
  }
}
function composerKey(event: KeyboardEvent) {
  if (slashOpen.value && !event.isComposing) {
    const items = slashItems.value, count = items.length;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      if (!count) return;
      event.preventDefault();
      slashIndex.value = moveSlash(slashIndex.value, event.key === "ArrowDown" ? 1 : -1, count);
      return;
    }
    if (event.key === "Escape") {
      // Close only the menu and keep the text, not the panel or dialog behind it.
      event.preventDefault();
      event.stopPropagation();
      slashDismissed.value = input.value;
      return;
    }
    if (event.key === "Tab" && !event.shiftKey) {
      const item = items[slashIndex.value] ?? items[0];
      if (item) {
        event.preventDefault();
        chooseSlashItem(item, "tab");
      }
      return;
    }
    if (event.key === "Enter" && !event.shiftKey) {
      const item = items[slashIndex.value];
      if (item) {
        event.preventDefault();
        chooseSlashItem(item, "enter");
        return;
      }
      if (slashInput.value?.stage === "name") {
        event.preventDefault(); // "No matching command": nothing is sent
        return;
      }
    }
  }
  // keyCode 229: WebKit's Enter that confirms an input-method composition (V1-055), not a send.
  if (event.key === "Enter" && !event.shiftKey && !event.altKey && !event.metaKey && !event.ctrlKey && !event.isComposing && event.keyCode !== 229) {
    event.preventDefault();
    send();
  }
}
function chooseSlashItem(item: SlashItem, how: "enter" | "tab" | "click") {
  const typed = slashInput.value;
  if (item.kind === "argument") {
    if (typed) input.value = completeArgument(typed, item);
    composer.value?.focus();
    return;
  }
  const choice = chooseSlash(item, how);
  if (choice.kind === "none") return notify(choice.reason);
  if (choice.kind === "insert") {
    input.value = choice.text;
    composer.value?.focus();
    return;
  }
  recent.value = recordUse(storage, item.name);
  if (choice.kind === "send") {
    input.value = choice.text;
    send();
  } else {
    input.value = "";
    runSlash(choice.action);
  }
}
// Every window action here is a handler a button, menu or shortcut already uses.
function runSlash(action: SlashAction) {
  switch (action) {
    case "new_chat": createChat(); break;
    case "clear": input.value = ""; composer.value?.focus(); break;
    case "model": openSettings("models"); break;
    case "effort": effortSelect.value?.focus(); break;
    case "rewind": openDrawer("changes", false, true); break;
    case "copy": copyLastReply(); break;
    case "stop": stop(); break;
    case "details": toggleDrawer(true); break;
    case "schedule": openDrawer("tools", false, true); break;
    case "windows": openDrawer("tools", false, true); break;
    case "browser": openDrawer("browser", false, true); break;
    case "settings": openSettings(); break;
  }
}
const projectName = computed(() => chat.value?.workspace?.split(/[\\/]/).pop());
const filteredApps = computed(() =>
  apps.value.filter((a) =>
    a.name.toLowerCase().includes(appSearch.value.toLowerCase()),
  ),
);
// Phase 24 (V1-108): the model's state in words ("API selected" and "capacity · API" are gone).
const onThisMac = computed(() => state.value.model.kind === "gguf" || !!state.value.model.local);
const localLabel = computed(() => {
  const model = state.value.model;
  if (!model.configured) return "Connect a model";
  return ({ ready: "Model ready", loading: "Loading model", error: "Model needs attention", unloaded: "Model resting" } as Record<string, string>)[model.state] || "Model ready";
});
const modelFoot = computed(() => {
  const model = state.value.model;
  if (!model.configured) return "Connect a model in Settings";
  const where = onThisMac.value ? "runs on this Mac" : "answered by " + (model.provider || "your provider");
  return `${model.context.toLocaleString()}-token context · ${where}`;
});
// V1-038: the first launch, with no model yet, offers the two ways to connect one.
// Phase 45 (F022): a dialog opens without the error left over from before it (its own failures still show in it).
watch(modal, (open) => {
  if (open) error.value = "";
});
const firstRun = computed(() => !booting.value && !state.value.model.configured);
// Phase 45 (F003): the first launch shows the welcome at full width; Details opens itself when there is something to
// review (announceApprovals), or when the person opens it.
watch(firstRun, (first) => {
  if (first) drawerOpen.value = false;
});
const modeKey = computed<"ask" | "auto" | "full">(() => {
  const mode = state.value.settings.permissions.mode;
  return mode === "auto" || mode === "full" ? mode : "ask";
});
// V1-065: where replies come from, said plainly (0.6.1's slogans implied everything stayed on this Mac).
const welcomeFoot = computed(() => {
  const model = state.value.model;
  if (!model.configured) return "Nothing is sent anywhere until you connect a model.";
  if (onThisMac.value) return "Replies are made on this Mac, and your conversations are stored here.";
  return `Replies come from ${model.provider || "your provider"}, which receives your messages. Your conversations are stored on this Mac.`;
});
// Phase 45 (F032): a toast with an action (Undo) stays 12 seconds, and waits while the pointer is on it.
// Phase 45 (F135, audit round R4): a toast sat over the composer's provider and model pickers for its 12 s; it is
// placed just above the composer area, wherever the composer's height puts it.
function placeToasts() {
  const area = document.querySelector?.<HTMLElement>(".composer-area");
  const rect = area?.getBoundingClientRect?.();
  const space = rect && rect.height > 0 && typeof window.innerHeight === "number" ? Math.max(0, window.innerHeight - rect.top) : 15;
  document.documentElement?.style?.setProperty?.("--toast-bottom", Math.round(space + 12) + "px");
}
function notify(message: string, action?: { label: string; run: () => void }) {
  placeToasts();
  toast.value = { text: message, action };
  window.clearTimeout(toastTimer);
  toastTimer = window.setTimeout(() => (toast.value = null), action ? 12000 : 3500);
}
function holdToast(hold: boolean) {
  window.clearTimeout(toastTimer);
  if (!hold && toast.value) toastTimer = window.setTimeout(() => (toast.value = null), toast.value.action ? 6000 : 2500);
}
function runToast() {
  const action = toast.value?.action;
  toast.value = null;
  action?.run();
}
// Notices from the Markdown renderer (a refused link, copied code): fixed host words, shown in the toast.
// Phase 45 (F130, audit round R4): a reply's source marker ([S7]) was plain text. It opens the page the reply's run
// recorded under that number (an https address only, in the person's browser) and says which page it was; a source
// without a web address is named instead.
async function openCitation(event: Event) {
  const id = (event as CustomEvent).detail;
  const current = chat.value;
  if (typeof id !== "string" || !/^S[1-9]\d{0,2}$/.test(id) || !current) return;
  const message = (event.target as Element | null)?.closest?.("[data-message-id]")?.getAttribute("data-message-id");
  const runId = (message && activityStore.forMessage(message)?.runId) || current.runs?.[0]?.id;
  const missing = `Pebble has no record of ${id} for this reply.`;
  if (!runId) return notify(missing);
  try {
    const listing = await rpc<{ sources?: unknown }>("research_sources", { id: current.id, run_id: runId });
    const sources = Array.isArray(listing?.sources) ? (listing.sources as Record<string, unknown>[]) : [];
    const source = sources.find((s) => s && s.source_id === id);
    if (!source) return notify(missing);
    const word = (value: unknown) => (typeof value === "string" ? value.replace(/\s+/g, " ").trim() : "");
    const name = (word(source.title) || word(source.domain) || word(source.url)).slice(0, 80);
    const url = word(source.url);
    if (/^https:\/\/\S+$/i.test(url) && url.length <= 3000) {
      await openExternal(url);
      notify(`Opened ${id}: ${name}`);
    } else notify(name ? `${id} is ${name}.` : `${id} has no web address to open.`);
  } catch {
    notify(`Pebble couldn't open ${id}.`);
  }
}
// Phase 45 (F159, audit round R5): a site card's heading named every address in full and broke one mid-word ("Use
// https://www.youtube.c / om, …"); it names each site once by its host, and the card's body lists the addresses
// Phase 45 (F181, audit round R6): "press in Game 1 | Tohin - Computer (White to Move)" named neither the press nor its
// target; the card says "Press “e4” in Game 1"
function screenHeading(review: Record<string, any> | undefined) {
  const verb = String(review?.action || "act");
  const label = String(review?.element?.label || "").trim().slice(0, 40);
  const title = String(review?.window?.title || "").split(/\s[|—–-]\s/)[0].trim().slice(0, 40);
  return verb.charAt(0).toUpperCase() + verb.slice(1) + (label ? ` “${label}”` : "") + ` in ${title || "an app window"}`;
}
// Phase 45 (F166): the classification reasons that make a press look like sending, submitting, saving, deleting or buying
const SENDING = new Set(["label_commit_word", "form_submit", "transfer", "enter_submits", "newline_submits", "leaves_origin"]);
function siteNames(sites: unknown) {
  const hosts = (Array.isArray(sites) ? sites : []).map((site) => {
    try {
      return new URL(String(site)).hostname.replace(/^www\./, "");
    } catch {
      return String(site);
    }
  });
  return [...new Set(hosts)].join(", ");
}
function markdownNotice(event: Event) {
  const text = (event as CustomEvent).detail;
  if (typeof text === "string" && text) notify(text.slice(0, 200));
}
function fail(e: unknown) {
  error.value = e instanceof Error ? e.message : String(e);
}
async function safe(fn: () => Promise<void>) {
  error.value = "";
  try {
    await fn();
  } catch (e) {
    fail(e);
  }
}
async function refresh() {
  const next = await rpc<State & { maintenance?: any }>("state");
  // Startup is waiting for an upgrade step: show only the maintenance screen.
  if (next?.maintenance) { maintenance.value = next.maintenance; return; }
  maintenance.value = null;
  state.value = next;
}
async function refreshRun() {
  await refresh();
  if (!chat.value) return;
  const updated = await rpc<Chat>('get_chat', { id: chat.value.id });
  if (chat.value?.id === updated.id) {
    chat.value.questions = updated.questions; chat.value.runs = updated.runs; chat.value.plan = updated.plan;
  }
}
async function createChat(options?: { quiet?: boolean } | Event) {
  const project = chat.value?.workspace || null;
  await safe(async () => {
    handoffFor.value = null;
    await clearAttachments();
    chat.value = await rpc<Chat>("new_chat");
    sources.value = [];
    activity.value = [];
    input.value = "";
    await refresh();
    await nextTick();
    composer.value?.focus();
  });
  // Phase 45 (F095, audit round R3): ⌘N in a project conversation silently started outside the project. It still does
  // (website and app control work only without one), and now says so, with the way back in.
  const quiet = !!options && !(options instanceof Event) && options.quiet === true;
  if (!quiet && project && chat.value && !chat.value.workspace)
    notify(`New conversation, outside ${project.split(/[\\/]/).pop()}.`, {
      label: "Start it in the project",
      run: () => void openProject(project),
    });
}
async function loadChat(id: string) {
  await safe(async () => {
    handoffFor.value = null;
    await clearAttachments();
    chat.value = await rpc<Chat>("get_chat", { id });
    sources.value = [];
    activity.value = [];
    error.value = "";
    // Phase 45 (F071, audit round R2): a conversation opened here (from the list, a notice or the companion's Open)
    // kept the scroll position of the one before, often its top; it opens at its end, where its latest task is (as
    // the companion's list does)
    stick.force();
  });
}
async function ensureChat() {
  if (!chat.value) chat.value = await rpc<Chat>("new_chat");
  return chat.value;
}
async function chooseProject() {
  await safe(async () => {
    const path = await selectFile(true);
    if (!path) return;
    // Phase 45 (F175, audit round R5): the open conversation moved into the project; one with messages stays where it
    // was and the project starts a new conversation, as choosing a listed project does
    if (chat.value?.messages.length) await createChat({ quiet: true });
    const c = await ensureChat();
    chat.value = await rpc<Chat>("set_workspace", { id: c.id, path });
    await refresh();
    notify("Project connected. Source reads stay inside this folder.");
  });
}
// Phase 45 (F030): projects stay listed under YOUR SPACE (from the conversations that used them, newest first);
// choosing one starts a conversation there (or uses the empty one that is open) without the folder picker.
const recentProjects = computed(() => {
  const seen = new Set<string>(), found: string[] = [];
  for (const c of state.value.chats) if (c.workspace && !seen.has(c.workspace)) { seen.add(c.workspace); found.push(c.workspace); }
  return found.slice(0, 5);
});
const projectMenu = ref(false);
// Phase 45 (F037): the conversations over a small window; choosing one (or a new one) closes them.
const sidebarOverlay = ref(false);
watch(() => chat.value?.id, () => (sidebarOverlay.value = false));
watch(compact, (small) => { if (!small) sidebarOverlay.value = false; });
// The menu closes with Esc (on the wrapper, so the window's own Esc branch stays as it is) and when focus leaves it.
function projectFocusOut(event: FocusEvent) {
  const next = event.relatedTarget as Node | null;
  if (!next || !(event.currentTarget as HTMLElement).contains(next)) projectMenu.value = false;
}
async function openProject(path: string) {
  projectMenu.value = false;
  if (!chat.value || chat.value.messages.length) await createChat({ quiet: true });
  await safe(async () => {
    const c = await ensureChat();
    chat.value = await rpc<Chat>("set_workspace", { id: c.id, path });
    await refresh();
  });
}
async function detachProject() {
  await safe(async () => {
    if (chat.value)
      chat.value = await rpc<Chat>("set_workspace", {
        id: chat.value.id,
        path: null,
      });
    await refresh();
  });
}
// `again` is text the window sends for the person after their click (Retry, Edit): an ordinary new
// turn with no attachments, while the message box keeps whatever the person is writing.
async function send(again?: string) {
  const typed = again === undefined;
  const raw = typed ? input.value : again;
  if (!raw.trim() || busy.value || attachmentBusy.value) return;
  // Phase 45 (F019): with no model connected nothing is sent (it could only fail, three times over); the words stay in
  // the box and one card offers the ways to connect. Slash commands still run.
  if (!raw.trim().startsWith("/") && !booting.value && !state.value.model.configured) {
    needsModel.value = true;
    return;
  }
  await safe(async () => {
    const c = await ensureChat(),
      text = raw.trim();
    const command = text.startsWith('/');
    if (command) recent.value = recordUse(storage, text.slice(1).split(/\s/)[0].toLowerCase());
    const followup = state.value.active === c.id && !command;
    const optimistic = "user-" + Date.now();
    busy.value = true;
    try {
      if (typed) input.value = "";
      if (!command && !followup) activity.value = [];
      c.messages.push({ id: optimistic, role: "user", content: text });
      // F3: "Sending" until the host's `run queued`; state.active is then set by the host's run event (receive).
      if (!command && !followup) companion.sending(true);
      stick.force(); // V1-130: sending always shows the new message
      const result = await rpc("send", {
        id: c.id,
        text,
        effort: effort.value,
        source_ids: command || followup || !typed ? [] : sources.value.map((s) => s.id),
      });
      const message = c.messages.find((m) => m.id === optimistic);
      if (message) message.id = result.message_id;
      if (typed && !command && !followup) sources.value = [];
      if (result?.command) await showCommandReply(c, result.message_id);
      await refresh();
    } catch (e) {
      c.messages = c.messages.filter((m) => m.id !== optimistic);
      if (typed) input.value = text;
      await refresh();
      if (e instanceof RpcError && e.code === "trashed_conversation") {
        // V1-018: the conversation is in Recently deleted; open a new one and keep the person's words in the box.
        const kept = input.value;
        await leaveChat();
        input.value = kept;
        throw new Error("That conversation is in Recently deleted, so Pebble opened a new one. Your message is still in the box: send it again here, or restore the conversation in Settings → Data & storage.");
      }
      throw e;
    } finally {
      companion.sending(false);
      busy.value = false;
    }
  });
}
// Phase 44 (1.9.1): a runtime command's reply. The host answers a command at once (app.send): it stores the command and
// its reply, emits only `complete` and starts no task, so no token or idle event brings the reply to this window, and
// the person saw their command and nothing else. Read the conversation back and add what the host stored after the
// command (the Companion reads it back too). The command already ran, so a failed read never puts it back in the box.
async function showCommandReply(c: Chat, commandId: string) {
  try {
    const stored = await rpc<Chat>("get_chat", { id: c.id });
    stick.follow(); // measured before the reply is added: a person reading above keeps their place
    addStoredAfter(c, stored?.messages, commandId);
  } catch {
    error.value = "Pebble couldn't show the command's reply. Open the conversation again to see it.";
  }
}
async function stop() {
  await safe(async () => {
    await rpc("cancel", { id: chat.value?.id });
    notify("Stopping the current task…");
  });
}
async function attachPaths(paths: string[]) {
  if (!paths.length || state.value.active || attachmentBusy.value) return;
  await safe(async () => {
    if (sources.value.length + paths.length > 10)
      throw new Error("Attach up to ten files per message.");
    attachmentBusy.value = true;
    try {
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
    } finally {
      attachmentBusy.value = false;
    }
  });
}
async function attach() {
  showAttach.value = false;
  await safe(async () => attachPaths(await selectAttachments()));
}
// Phase 24 (V1-042): pasting a screenshot or a file attaches it through attach_bytes (bridge.attachBytes sends it in
// chunks the shell accepts); a text paste is left to the message box.
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
  if (!files.length || state.value.active || attachmentBusy.value) return;
  await safe(async () => {
    if (sources.value.length + files.length > 10)
      throw new Error("Attach up to ten files per message.");
    attachmentBusy.value = true;
    try {
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
    } finally {
      attachmentBusy.value = false;
    }
  });
}
async function clearAttachments() {
  if (chat.value)
    for (const s of sources.value)
      await rpc("remove_source", { id: chat.value.id, source_id: s.id });
  sources.value = [];
}
async function attachUrl() {
  await safe(async () => {
    attachmentBusy.value = true;
    try {
      const c = await ensureChat();
      sources.value.push(await rpc("attach_url", { id: c.id, url: url.value }));
      modal.value = null;
      url.value = "";
      nextTick(() => composer.value?.focus());  // Phase 45 (F052): the keyboard back in the message box
    } finally {
      attachmentBusy.value = false;
    }
  });
}
// Phase 45 (F052, audit round R2): a web page's chip showed only its raw address, with no sign it was read; it says
// "Read" and the site, and its tooltip the whole address and how much was read
function sourceLabel(source: { name: string }) {
  if (!/^https?:\/\//i.test(source.name)) return source.name;
  try {
    const at = new URL(source.name);
    const path = at.pathname.length > 1 ? at.pathname.slice(0, 24) + (at.pathname.length > 24 ? "…" : "") : "";
    return "Read: " + at.hostname.replace(/^www\./, "") + path;
  } catch {
    return source.name;
  }
}
function sourceTitle(source: { name: string; characters?: number }) {
  return typeof source.characters === "number" && /^https?:\/\//i.test(source.name)
    ? `${source.name} · ${source.characters.toLocaleString()} characters read`
    : source.name;
}
async function removeSource(id: string) {
  await safe(async () => {
    if (chat.value)
      await rpc("remove_source", { id: chat.value.id, source_id: id });
    sources.value = sources.value.filter((s) => s.id !== id);
  });
}
async function chooseModel() {
  await safe(async () => {
    const path = await selectFile(false, true);
    if (path) state.value.model = await rpc("set_model", { path });
  });
}
async function modelPower() {
  await safe(async () => {
    state.value.model = await rpc(
      state.value.model.state === "ready" ? "model_unload" : "model_load",
    );
  });
}
async function openApps() {
  modal.value = "apps";
  appSearch.value = "";
  await safe(async () => {
    apps.value = await rpc("apps");
  });
}
async function proposeApp(app: any) {
  await safe(async () => {
    const c = await ensureChat();
    await rpc("open_app", { id: c.id, app_id: app.id });
    modal.value = null;
    showReviews();
  });
}
async function decide(action: Action, method: "approve" | "reject" | "undo") {
  await safe(async () => {
    if (!chat.value) return;
    actionBusy.value = action.id;
    try {
      await rpc(method, { id: chat.value.id, action_id: action.id });
      chat.value = await rpc<Chat>("get_chat", { id: chat.value.id });
      notify(
        method === "approve"
          ? "Action completed"
          : method === "undo"
            ? action.kind === "command" ? "Command undone" : "Edit undone"
            : "Not allowed. Nothing ran.",
      );
    } finally {
      actionBusy.value = "";
    }
  });
}
// Phase 40 (1.6.0, D91): "Always allow" saves the hosts and folders this ask names, then approves it once.
async function grantAndApprove(action: Action) {
  await safe(async () => {
    const p = action.payload || {};
    const folders = action.kind === "file" ? [String(p.path || "").replace(/\/[^/]*$/, "")] : (p.folders || []);
    const grants = [...(p.hosts || []).map((value: string) => ["hosts", value]), ...folders.filter(Boolean).map((value: string) => ["folders", value])];
    for (const [kind, value] of grants) await rpc("shell_grant_add", { kind, value, scope: "always" });
  });
  await decide(action, "approve");
}
async function shellUndo(action: Action) {
  await safe(async () => {
    const id = action.payload?.result?.snapshot?.id || action.payload?.result?.snapshot;
    if (!chat.value || typeof id !== "string") return;
    actionBusy.value = action.id;
    try {
      const done = await rpc<{ restored: string[]; failed: unknown[] }>("shell_undo", { snapshot_id: id });
      notify(done.failed.length ? "Some changes could not be undone" : "Change undone");
    } finally {
      actionBusy.value = "";
    }
  });
}
async function proposeGitUndo(action: Action) {
  // A Git undo is a new reviewed plan; the edit-only undo RPC is never used here.
  await safe(async () => {
    if (!chat.value) return;
    actionBusy.value = action.id;
    try {
      await rpc("git_plan_propose", { id: chat.value.id, operation: "undo", receipt_id: action.payload.result.receipt_id });
      chat.value = await rpc<Chat>("get_chat", { id: chat.value.id });
      notify("Undo plan ready for review");
    } finally {
      actionBusy.value = "";
    }
  });
}
async function toggleCompact() {
  if (native && !compact.value) {
    await invoke("show_pet");
    return;
  }
  compact.value = !compact.value;
  await safe(() => setCompact(compact.value));
  if (compact.value) drawerOpen.value = false;
}
// Phase 45 (F024): Cmd+K finds and runs the app's own actions, Settings tabs and conversations, not only commands.
const SETTINGS_TABS: [string, string][] = [["models", "Models"], ["permissions", "Permissions"], ["memory", "Memory"],
  ["extensions", "Skills & connectors"], ["storage", "Data & storage"], ["privacy", "Privacy & Stealth"],
  ["iphone", "iPhone"], ["appearance", "Appearance"], ["general", "General"]];
const paletteExtras = computed(() => [
  ...(["light", "dark", "system"] as const).map((t) => ({ key: "theme-" + t, title: t[0].toUpperCase() + t.slice(1) + " theme",
    detail: "Appearance", run: "theme:" + t, words: "appearance mode colour color" })),
  { key: "stealth", title: state.value.settings.stealth ? "Turn Stealth Lockdown off" : "Turn Stealth Lockdown on",
    detail: "Hidden from screen capture and screen sharing", run: "stealth", words: "privacy hide screen share" },
  { key: "motion", title: state.value.settings.reduced_motion ? "Turn Reduce motion off" : "Turn Reduce motion on",
    detail: "Appearance", run: "motion", words: "animation still" },
  { key: "compact", title: compact.value ? "Expand the workspace" : "Switch to the floating companion",
    detail: "Window", run: "compact", words: "companion pet pebble minimize" },
  ...SETTINGS_TABS.map(([id, label]) => ({ key: "settings-" + id, title: "Settings: " + label, detail: "Open Settings",
    run: "settings:" + id, words: "preferences options" })),
  // Phase 45 (F113, audit round R3): a waiting card was 11 Tabs away from the message box; ⌘K goes to it, its title
  // focused, so the next Tab is its first button
  ...(pending.value.length ? [{ key: "review", title: pending.value.length === 1 ? "Review the waiting action"
    : `Review the ${pending.value.length} waiting actions`, detail: "Waiting for you", run: "review",
    words: "approve allow permission card pending" }] : []),
  // Phase 45 (F108, audit round R3): no way to take one conversation elsewhere; ⌘K copies it as Markdown
  ...(chat.value?.messages.length ? [{ key: "copy-chat", title: "Copy this conversation as Markdown", detail: "Conversation",
    run: "copychat", words: "export save share markdown text" }] : []),
  ...(state.value.chats || []).map((c: any, i: number) => ({ key: "chat-" + c.id, title: c.title || "Conversation",
    detail: "Conversation", run: "chat:" + c.id, words: "conversation chat history", recent: i < 5 })),
]);
async function copyConversation() {
  const c = chat.value;
  if (!c) return;
  const lines = [`# ${c.title || "Conversation"}`, ""];
  for (const m of c.messages) {
    if (m.role !== "user" && m.role !== "assistant") continue;
    lines.push(m.role === "user" ? "**You:**" : "**Pebble:**", "", String(m.content || "").trim(), "");
  }
  try {
    await navigator.clipboard.writeText(lines.join("\n"));
    notify("Copied the conversation as Markdown.");
  } catch {
    notify("Could not copy the conversation.");
  }
}
async function runLauncher(id: string) {
  palette.value = false;
  const [kind, value] = [id.split(":")[0], id.slice(id.indexOf(":") + 1)];
  if (kind === "theme") await theme(value);
  else if (kind === "stealth") await stealth();
  else if (kind === "motion") await motion();
  else if (kind === "compact") await toggleCompact();
  else if (kind === "settings") openSettings(value);
  else if (kind === "chat") await loadChat(value);
  else if (kind === "copychat") await copyConversation();
  else if (kind === "review") showReviews();
}
async function theme(value: string) {
  state.value.settings.theme = value;
  applyTheme();
  await safe(async () => {
    await rpc("setting", { key: "theme", value });
  });
}
// Phase 33 (1.1.0, D45-D47): Stealth Lockdown. The host keeps the setting and tells every window (the avatars wear the
// Veil); the shell hides Pebble from capture, the Dock, ⌘Tab and Mission Control and stops it taking focus.
async function stealth() {
  const enabled = !state.value.settings.stealth;
  state.value.settings.stealth = enabled;
  await safe(async () => {
    await rpc("setting", { key: "stealth", value: enabled });
    if (native) await invoke("stealth_set", { enabled });
  });
}
// Phase 46 (1.9.29, D206): Pebble's work as cards (the work view) or as rows of text; presentation only
const workView = computed(() => state.value.settings.work_view !== false);
// The work view's task line: the person's own message that started the run (never read from the host's records)
function taskBefore(messageId?: string) {
  const list = chat.value?.messages || [];
  const end = messageId ? list.findIndex((m) => m.id === messageId) : list.length;
  for (let i = (end < 0 ? list.length : end) - 1; i >= 0; i--) if (list[i].role === "user") return taskLine(list[i].content);
  return null;
}
// The branch card's reads (confined Git, no network) and its one proposal, which lands on a review card (the host's
// `actions` event shows it); only these three routes, all already the main window's
const gitAccess = computed<GitAccess | null>(() => {
  const id = chat.value?.workspace ? chat.value.id : null;
  if (!id) return null;
  return {
    read: (method, params) => rpc(method === "git_history" ? "git_history" : "git_status", { ...params, id }),
    propose: (params) => rpc("git_plan_propose", { id, operation: "merge", name: params.name }),
  };
});
// Phase 46 (1.9.29, D195): when the model may answer with a chart, diagram or comparison (artifact_schema.visual_offer);
// "auto" is off by default (D216/D217). Tables and diagrams already in a reply are drawn either way (ReplyBody).
const visualAnswers = computed(() => (state.value.settings.visual_answers === "auto" ? "auto" : "ask"));
async function changeVisualAnswers(value: string) {
  if ((value !== "ask" && value !== "auto") || value === visualAnswers.value) return;
  state.value.settings.visual_answers = value;
  await safe(async () => {
    await rpc("setting", { key: "visual_answers", value });
  });
}
async function toggleWorkView() {
  state.value.settings.work_view = !workView.value;
  await safe(async () => {
    await rpc("setting", { key: "work_view", value: state.value.settings.work_view });
  });
}
async function motion() {
  state.value.settings.reduced_motion = !state.value.settings.reduced_motion;
  await safe(async () => {
    await rpc("setting", {
      key: "reduced_motion",
      value: state.value.settings.reduced_motion,
    });
  });
}
// Phase 19 companion switches (presentation only; the host validates exact keys and tells every window).
async function changeCompanion(patch: Partial<CompanionSettings>) {
  const value: CompanionSettings = { ...companionSettings.value, ...patch };
  state.value.settings.companion = value;
  await safe(async () => {
    await rpc("setting", { key: "companion", value });
  });
}
function applyTheme() {
  const t = state.value.settings.theme;
  document.documentElement.dataset.theme =
    t === "system"
      ? matchMedia("(prefers-color-scheme: dark)").matches
        ? "dark"
        : "light"
      : t;
  document.documentElement.classList.toggle(
    "reduced-motion",
    state.value.settings.reduced_motion,
  );
}
watch(() => state.value.settings, applyTheme, { deep: true });
const colorQuery = matchMedia("(prefers-color-scheme: dark)");
function key(e: KeyboardEvent) {
  // Phase 45 (F096, audit round R3): typing with the focus outside every text field (after a click on a reply's steps
  // or on empty conversation space) was silently lost, twice a whole request. A printable key there goes to the
  // message box (Space and Enter still press a focused button).
  const target = e.target as HTMLElement | null;
  const editable = !!target?.closest?.("input, textarea, select, [contenteditable]:not([contenteditable='false'])");
  if (!editable && !modal.value && !palette.value && e.key.length === 1 && e.key !== " " && !e.metaKey && !e.ctrlKey
      && !e.altKey && !e.isComposing && composer.value) {
    // F131 (audit round R4): moving the focus during the key did not carry the letter into the box in WebKit (a whole
    // follow-up was lost after a click on the conversation); the first letter is put in here, the rest follow
    e.preventDefault();
    composer.value.focus();
    input.value += e.key;
    nextTick(() => composer.value?.setSelectionRange?.(input.value.length, input.value.length));
  }
  if (e.key === "Tab" && modal.value) {
    const elements = [
      ...document.querySelectorAll<HTMLElement>(
        ".modal button:not(:disabled), .modal input, .modal a[href]",
      ),
    ];
    const first = elements[0],
      last = elements.at(-1);
    if (e.shiftKey && document.activeElement === first) {
      e.preventDefault();
      last?.focus();
    } else if (!e.shiftKey && document.activeElement === last) {
      e.preventDefault();
      first?.focus();
    }
  }
  const shortcut = matchShortcut(e, "main");
  if (shortcut && shortcut !== "close") e.preventDefault();
  if (shortcut === "close") {
    if (palette.value) palette.value = false;
    else if (modal.value || showAttach.value) {
      modal.value = null;
      showAttach.value = false;
    }
    // Phase 30 (D42): "Close the open panel or dialog" also closes the canvas or the details panel, wherever focus is,
    // unless something inside already handled this Esc.
    else if (!e.defaultPrevented && canvas.value) closeCanvas();
    else if (!e.defaultPrevented && drawerOpen.value) closeDrawer();
  } else if (shortcut === "palette") {
    lastShortcut = { id: "palette", at: Date.now() };
    palette.value = !palette.value && !modal.value;
  }
  else if (shortcut) runShortcut(shortcut);
  // Phase 45 (F101, audit round R3): an Escape the page did not stop reached the window, and macOS took the workspace
  // out of full screen, even the Escape that closed Settings. Every Escape belongs to the page, after its own handlers
  // above (an input method keeps its own).
  if (e.key === "Escape" && !e.isComposing) e.preventDefault();
}
// Phase 45 (F025-F027): the menu bar's items run the same actions as their keys (a key the page already handled is not
// run again), Find focuses conversation search, Help opens the guide, the shortcuts or a problem report.
let lastShortcut = { id: "", at: 0 };
function menuAction(id: string) {
  if (lastShortcut.id === id && Date.now() - lastShortcut.at < 400) return;
  if (id === "palette") palette.value = !modal.value;
  else if (["stop", "activity", "details", "copy_reply", "new_chat", "settings"].includes(id)) runShortcut(id as ShortcutId);
  else if (id === "find") focusChatSearch();
  else if (id === "shortcuts" || id === "whats_new") {
    openSettings("general");
    revealSetting(id === "shortcuts" ? "#settings-keyboard" : "details.whats-new");
  }
  else if (id === "help") openExternal("https://github.com/tohin003/Pebble#readme").catch(() => {});
  else if (id === "report") openExternal("https://github.com/tohin003/Pebble/issues/new").catch(() => {});
  else if (id === "companion") toggleCompact();
}
// Phase 45 (F138, audit round R4): Help › Keyboard Shortcuts opened General at its top, the shortcut list about 900 pt
// below the fold; Help's items show the part they name (the shortcut list, or What's new opened), after the dialog has
// placed its own focus.
function revealSetting(selector: string) {
  setTimeout(() => {
    const target = document.querySelector<HTMLElement>(selector);
    if (!target) return;
    if (target instanceof HTMLDetailsElement) target.open = true;
    target.scrollIntoView?.({ block: "start" });
    (target instanceof HTMLDetailsElement ? target.querySelector<HTMLElement>("summary") : target)?.focus?.({ preventScroll: true });
  }, 150);
}
function focusChatSearch() {
  if (compact.value) return;
  sidebar.value = true;
  nextTick(() => document.querySelector<HTMLInputElement>(".chat-search input")?.focus());
}
// Phase 45 (F072): this window's own listener (a global listen made the companion open its palette too).
if (native) {
  try {
    void getCurrentWindow().listen<string>("pebble-menu", (event) => menuAction(String(event.payload)));
  } catch {
    // a window without event support (a test stub) has no menu bar to listen to
  }
}
function runShortcut(id: ShortcutId) {
  lastShortcut = { id, at: Date.now() };
  palette.value = false;
  if (id === "new_chat") createChat();
  else if (id === "settings") modal.value = "settings";
  else if (id === "stop" && isWorking.value) stop();
  // ⌘J keeps meaning "Show task activity": the drawer's Activity tab (or the drawer as it stands).
  else if (id === "activity") openDrawer(drawerTabs.value.some((t) => t.id === "activity") ? "activity" : undefined, true, true);
  else if (id === "details") toggleDrawer(true);
  else if (id === "copy_reply") copyLastReply();
}
function chooseCommand(text: string) {
  palette.value = false;
  prompt(text);
}
// Phase 24 (V1-135): "What's new" in About. An excerpt of the release's CHANGELOG.md entry (t24-publish keeps the
// two in step); plain text, bundled with the build, no update check (that is after 1.0).
// Phase 45 (F171, audit round R5): the list says which release it is from (the newest with highlights)
const WHATS_NEW_VERSION = "2.0.0";
const WHATS_NEW = Object.freeze([
  "Pebble works across your Mac inside a sandbox and uses the app windows and websites you allow.",
  "AgentGuard checks what you asked for before a task counts as done, and everyday Git runs under review.",
  "Pebble Remote is the Mac side of Pebble's iPhone app, and the work view draws each run's steps as cards.",
]);
// Phase 18 E06: the native Save dialog returns a single-use token; this window never sees or sends a path.
async function chooseTelemetryDestination(): Promise<string | null> {
  if (!native) return null;
  const chosen = await invoke<{ destination_token?: string } | null>('telemetry_choose_destination');
  return chosen && typeof chosen.destination_token === 'string' ? chosen.destination_token : null;
}
// Phase 24 (V1-063, D2): Delete moves a conversation to Recently deleted for 30 days (storage_trash_chat), with Undo;
// Delete now is in Settings → Data & storage. Its quiz answers leave this window's storage only when it is deleted
// for good (the `deleted` storage event, V1-156), so Undo loses nothing.
async function trashChat() {
  await safe(async () => {
    const target = deleteTarget.value;
    if (!target) return;
    const current = chat.value?.id === target.id;
    await rpc("storage_trash_chat", { chat_id: target.id });
    // The slash menu's recent-use order is local; deleting a conversation clears it (design study §4.3).
    clearRecent(storage);
    recent.value = {};
    modal.value = null;
    deleteTarget.value = null;
    if (current) {
      chat.value = null;
      await createChat();
    }
    await refresh();
    nextTick(() => composer.value?.focus()); // V1-129: focus never stays on a removed row
    notify("Moved to Recently deleted.", { label: "Undo", run: () => void restoreChat(target.id, current) });
  });
}
async function restoreChat(id: string, reopen: boolean) {
  await safe(async () => {
    await rpc("storage_restore_chat", { chat_id: id });
    await refresh();
    if (reopen) await loadChat(id);
    notify("Restored the conversation.");
  });
}
// Phase 24 (V1-125): find a conversation by its title, and rename one (chat_rename changes only the title).
// Phase 45 (F031, audit round R1): what was said is searched too (the service's chat_search, 250 ms after the last key),
// and a conversation found by its words shows where.
const chatSearch = ref("");
const contentMatches = ref<Record<string, string>>({});
let contentTimer: ReturnType<typeof setTimeout> | undefined;
watch(chatSearch, (value) => {
  clearTimeout(contentTimer);
  const words = value.trim();
  if (words.length < 2 || !native) {
    contentMatches.value = {};
    return;
  }
  contentTimer = setTimeout(async () => {
    try {
      const found = await rpc<{ matches: { id: string; excerpt: string }[] }>("chat_search", { query: words });
      if (chatSearch.value.trim() === words)
        contentMatches.value = Object.fromEntries((found.matches || []).map((m) => [m.id, m.excerpt]));
    } catch {
      contentMatches.value = {};
    }
  }, 250);
});
const listedChats = computed(() => {
  const words = chatSearch.value.trim().toLowerCase();
  return words
    ? state.value.chats.filter((c) => c.title.toLowerCase().includes(words) || c.id in contentMatches.value)
    : state.value.chats;
});
// F134 (audit round R4): Escape in the search closed the Details drawer and kept the words; it clears them first, then
// leaves the field (the window's Escape does the rest the next time)
function searchEscape(e: KeyboardEvent) {
  e.stopPropagation();
  e.preventDefault();
  if (chatSearch.value) chatSearch.value = "";
  else (e.target as HTMLElement | null)?.blur?.();
}
function excerptFor(c: Chat) {
  const words = chatSearch.value.trim().toLowerCase();
  return words && !c.title.toLowerCase().includes(words) ? contentMatches.value[c.id] || "" : "";
}
const renaming = ref<string | null>(null),
  renameText = ref("");
function startRename(c: Chat) {
  renaming.value = c.id;
  renameText.value = c.title;
  nextTick(() => document.querySelector<HTMLInputElement>(".chat-rename input")?.select());
}
function focusRow(id: string, label: string) {
  nextTick(() => {
    const row = Array.from(document.querySelectorAll<HTMLElement>(".chat-entry[data-chat-id]")).find((el) => el.dataset.chatId === id);
    row?.querySelector<HTMLElement>(`[aria-label^="${label}"]`)?.focus();
  });
}
function cancelRename(id: string) {
  renaming.value = null;
  focusRow(id, "Rename");
}
async function saveRename(c: Chat) {
  if (renaming.value !== c.id) return; // Escape already cancelled (a blur can follow it)
  const title = renameText.value.replace(/\s+/g, " ").trim().slice(0, 200);
  if (!title || title === c.title) return cancelRename(c.id);
  await safe(async () => {
    await rpc("chat_rename", { id: c.id, title });
    renaming.value = null;
    await refresh();
    if (chat.value?.id === c.id) chat.value.title = title;
    focusRow(c.id, "Rename");
  });
}
// Phase 24 (V1-034): "forget …" in the conversation shows the saved memories it matches (the host's memory_forget
// event, read through memory_forget_preview); each is forgotten only by the person's click here (memory_delete).
// Main window chrome: no generated view or model can press these buttons.
interface ForgetItem { id: string; kind: string; content: string; where: string }
const forgetCard = ref<{ chatId: string; subject: string; items: ForgetItem[]; done: Record<string, boolean>; error: string; busy: string } | null>(null);
const WHERE: Record<string, string> = { all: "In every conversation", project: "In this project", conversation: "In this conversation" };
async function openForget(d: Record<string, unknown>) {
  const ids = (Array.isArray(d.memory_ids) ? d.memory_ids : []).filter((id): id is string => typeof id === "string" && id.length <= 128).slice(0, 20);
  if (!ids.length || typeof d.chat_id !== "string") return;
  const chatId = d.chat_id;
  try {
    const reply = await rpc<{ items?: unknown[]; subject?: unknown }>("memory_forget_preview", { ids });
    const items = (Array.isArray(reply?.items) ? reply.items : [])
      .map((raw) => (raw && typeof raw === "object" ? (raw as Record<string, unknown>) : {}))
      .filter((item) => typeof item.id === "string" && typeof item.content === "string")
      .map((item) => ({ id: item.id as string, kind: String(item.kind || ""), content: (item.content as string).slice(0, 300), where: WHERE[String(item.where)] || "" }));
    if (!items.length || chat.value?.id !== chatId) return;
    const subject = typeof reply?.subject === "string" ? reply.subject : typeof d.subject === "string" ? d.subject : "";
    forgetCard.value = { chatId, subject: subject.slice(0, 120), items, done: {}, error: "", busy: "" };
  } catch (cause) {
    fail(cause);
  }
}
watch(() => chat.value?.id, (id) => {
  if (forgetCard.value && forgetCard.value.chatId !== id) forgetCard.value = null;
});
async function forgetMemory(item: ForgetItem) {
  const card = forgetCard.value;
  if (!card || card.busy || card.done[item.id] || isWorking.value) return;
  card.busy = item.id;
  card.error = "";
  try {
    await rpc("memory_delete", item.kind === "working" ? { id: item.id, kind: "working" } : { id: item.id });
    card.done[item.id] = true;
  } catch (cause) {
    card.error = cause instanceof Error ? cause.message : String(cause);
  } finally {
    card.busy = "";
  }
}
// Phase 41 (1.7.0, Pebble's Workshop, D69): a tool Pebble kept on its own is always shown (View code, Remove); a keep
// Pebble asks about waits here for the person's Keep or Discard. Main window only; no model can press these.
// Phase 45 (F162): "remove": the person asked Pebble to remove a kept tool; the card's Remove is theirs to press.
interface ToolboxCard { chatId: string; kind: "kept" | "asked" | "remove"; name: string; purpose: string; why: string[]; pendingId: string; code: string; done: string; busy: boolean; error: string; at: number }
const toolboxCards = ref<ToolboxCard[]>([]);
function toolboxEvent(d: Record<string, unknown>) {
  if (typeof d.chat_id !== "string" || typeof d.name !== "string" || (d.kind !== "kept" && d.kind !== "asked" && d.kind !== "remove")) return;
  const card: ToolboxCard = { chatId: d.chat_id, kind: d.kind, name: d.name.slice(0, 40), purpose: String(d.purpose || "").slice(0, 200),
    why: (Array.isArray(d.why) ? d.why : []).map((w) => String(w).slice(0, 200)).slice(0, 6), pendingId: String(d.pending_id || ""),
    code: "", done: "", busy: false, error: "", at: Date.now() };
  toolboxCards.value = [...toolboxCards.value.filter((c) => c.name !== card.name), card].slice(-4);
}
// Phase 45 (F100, audit round R3): the cards sit after the conversation, so a Discarded card stayed below later messages
// as if the newest reply asked to keep a tool. A card still waiting for Keep or Discard stays; a decided card, or a kept
// tool's notice (Remove stays in Settings › Toolbox), leaves once a newer message of yours moves the conversation on.
function toolboxShown(card: ToolboxCard) {
  if (card.chatId !== chat.value?.id) return false;
  if ((card.kind === "asked" || card.kind === "remove") && !card.done) return true;
  return !chat.value.messages.some((m) => m.role === "user" && typeof m.created === "number" && m.created * 1000 > card.at + 1000);
}
async function toolboxView(card: ToolboxCard) {
  if (card.code) { card.code = ""; return; }
  try { card.code = String((await rpc<{ code?: string }>("toolbox_get", { name: card.name })).code || ""); }
  catch (cause) { card.error = cause instanceof Error ? cause.message : String(cause); }
}
async function toolboxRemove(card: ToolboxCard) {
  if (card.busy) return;
  card.busy = true;
  // Phase 45 (F178): Settings › Skills & connectors can put it back for ten minutes
  try { await rpc("toolbox_remove", { name: card.name }); card.done = "Removed from the Toolbox. For ten minutes, Settings › Skills & connectors can put it back."; }
  catch (cause) { card.error = cause instanceof Error ? cause.message : String(cause); }
  finally { card.busy = false; }
}
async function toolboxDecide(card: ToolboxCard, keep: boolean) {
  if (card.busy || !card.pendingId) return;
  card.busy = true;
  try { await rpc("toolbox_decide", { pending_id: card.pendingId, keep }); card.done = keep ? "Kept in the Toolbox" : "Discarded"; }
  catch (cause) { card.error = cause instanceof Error ? cause.message : String(cause); }
  finally { card.busy = false; }
}
function prompt(text: string) {
  input.value = text;
  nextTick(() => composer.value?.focus());
}
// Restore "Thought for N s" rows when a conversation is opened or its runs change; approvals already
// waiting when it opens are not announced again as new.
watch(
  () => [chat.value, chat.value?.runs, chat.value?.messages.length] as const,
  () => {
    if (!chat.value) return;
    activityStore.seedChat(chat.value);
    chat.value.actions.forEach((a) => a.status === "pending" && seenPending.add(a.id));
  },
);
function receive(event: { event: string; data: any }) {
  const d = event.data;
  companion.accept(event);
  activityStore.accept(event); // run, activity, reasoning, progress, token and idle events, every chat
  if (event.event === 'run') {
    if (['queued', 'running', 'retry_wait'].includes(d.state)) {
      state.value.active = d.chat_id; state.value.active_run = d.run_id;
    } else if (state.value.active_run === d.run_id) {
      state.value.active = null; state.value.active_run = null;
    }
  }
  if (event.event === "settings") {
    refresh().catch(fail);
    return;
  }
  if (event.event === "model") {
    state.value.model = d;
    return;
  }
  if (event.event === "ready") {
    // The upgrade finished in place; start the app normally against the live service.
    location.reload();
    return;
  }
  if (event.event === "offline") {
    error.value = "The local service stopped. Restart it to reconnect; unfinished work is marked for review, never replayed.";
    serviceOffline.value = true;
    state.value.active = null;
    return;
  }
  if (event.event === "storage" || event.event === "storage_migration") return storageEvent(event.event, d);
  if (event.event === "browser") return browserEvent(d);
  // V1-002: kept for every conversation, so the workspace still offers the review when it opens that one.
  if (event.event === "progress" && d?.handoff && typeof d.chat_id === "string" && typeof d.label === "string")
    deviceNotices.set(d.chat_id, {
      runId: validId(d.handoff.source_run_id) ? d.handoff.source_run_id : null,
      profileId: typeof d.handoff.profile_id === "string" ? d.handoff.profile_id : null,
      label: d.label.slice(0, 300),
    });
  if (event.event === "toolbox" && d) toolboxEvent(d);
  if (event.event === "memory_forget" && d?.chat_id === chat.value?.id && d?.authorization === "none") {
    void openForget(d);
    return;
  }
  if (event.event === 'lifecycle') { workflowRevision.value++; return; } // wake summaries and zone changes refresh the panel
  if (event.event === 'goal') {
    if (d.chat_id === chat.value?.id) goalRevision.value++;
    return;
  }
  if (event.event === 'job' || event.event === 'job_notification') {
    if (d.chat_id === chat.value?.id) workflowRevision.value++;
    refresh().catch(fail);
    return;
  }
  // Phase 46 (1.9.29, D214, C7): what a phone did shows here as it happens; a new pairing that waits for the person
  // opens Settings › iPhone once, where they confirm its six digits (X-1).
  if (event.event === "remote_notice") {
    if (typeof d?.text === "string" && d.text) notify(d.text.slice(0, 160));
    return;
  }
  if (event.event === "remote_state") {
    const code = typeof d?.confirm?.code === "string" ? d.confirm.code : "";
    if (code && code !== remotePairing) openSettings("iphone");
    remotePairing = code;
    return;
  }
  if (!chat.value || d.chat_id !== chat.value.id) {
    if (event.event === "idle") refresh();
    return;
  }
  if (applyRunEvent(chat.value, event.event, d)) { stick.follow(); return; }
  if (event.event === "token") {
    stick.follow(); // V1-130: measured before the text grows; a person reading above keeps their place
    let message = chat.value.messages.find((m) => m.id === d.id);
    if (!message) {
      message = { id: d.id, role: "assistant", content: "" };
      chat.value.messages.push(message);
    }
    message.content += d.text;
  } else if (event.event === "usage") {
    const message = chat.value.messages.find((m) => m.id === d.id);
    if (message) message.usage = d.usage;
  } else if (event.event === "progress") {
    activity.value.push(d);
    if (activity.value.length > 24) activity.value.shift();
  } else if (event.event === "actions") {
    chat.value.actions = d.actions;
    announceApprovals(d.actions);
  } else if (event.event === "artifact") {
    rpc<Chat>("get_chat", { id: d.chat_id })
      .then((c) => {
        if (chat.value?.id === c.id) {
          chat.value = c;
          openArtifact(d.id);
        }
      })
      .catch(fail);
  } else if (event.event === "error") {
    error.value = d.message;
  } else if (event.event === "idle") {
    if (!d.run_id || state.value.active_run === d.run_id) { state.value.active = null; state.value.active_run = null; }
    refresh();
    rpc<Chat>("get_chat", { id: d.chat_id })
      .then((c) => {
        if (chat.value?.id === c.id) chat.value = c;
      })
      .catch(fail);
  }
}
// Phase 45 (F112, audit round R3): the message box grows with its text (CSS caps it), so a long message is not edited
// through a three-line slot.
function autosize() {
  const box = composer.value;
  if (!box?.style) return;
  box.style.height = "auto";
  box.style.height = box.scrollHeight + "px";
}
watch(input, () => nextTick(autosize));
// Phase 45 (F038): when the window comes back with nothing focused in it, the message box takes focus, so the window's
// own keys work at once (the menu bar's keys work either way). F074 (audit round R2): a click on empty conversation
// space to bring the window back left New conversation focused, with its ring (Space or Return would have started a
// conversation); a sidebar button the person did not click there goes the same way.
let lastPointer: { at: number; target: Element | null } = { at: 0, target: null };
if (typeof document !== "undefined")
  document.addEventListener("pointerdown", (e) => { lastPointer = { at: Date.now(), target: e.target as Element | null }; }, true);
function windowFocused() {
  // F146 (audit round R4): opened from the companion, the window came back with the header's Conversations sidebar
  // button focused (WebKit focuses the first control when its view takes the keyboard): typed letters went nowhere and
  // Space hid the sidebar. A title-bar or sidebar control the person did not click gives the keyboard to the message
  // box; a field being typed in keeps it, and so does a control elsewhere (the Details drawer, a card) that the person
  // reached with the keyboard. Run after WebKit has placed its own focus.
  setTimeout(() => {
    if (modal.value || compact.value) return;
    const active = document.activeElement as HTMLElement | null;
    const clicked = Date.now() - lastPointer.at < 800 ? lastPointer.target : null;
    const editable = !!active?.closest?.("input, textarea, select, [contenteditable]:not([contenteditable='false'])");
    const chrome = !!active?.closest?.("header.titlebar, #sidebar");
    const unasked = !!active && active !== document.body && chrome && !editable && !(clicked && active.contains(clicked));
    if (!active || active === document.body || unasked) composer.value?.focus();
  }, 150);
}
if (typeof window !== "undefined") window.addEventListener("focus", windowFocused);
onMounted(async () => {
  document.addEventListener("keydown", key);
  document.addEventListener("pebble-notice", markdownNotice);
  document.addEventListener("pebble-source", openCitation);
  window.addEventListener("resize", onResize);
  colorQuery.addEventListener("change", applyTheme);
  if (!native) {
    booting.value = false;
    state.value.model.configured = true;
    state.value.model.name = "FrogNano 4B · Q4";
    state.value.guard.available = true;
    applyTheme();
    return;
  }
  await safe(async () => {
    dropUnsubscribe = await subscribeDrops((type, paths) => {
      dropHover.value =
        (type === "enter" || type === "over") && !state.value.active;
      if (type === "drop") {
        dropHover.value = false;
        if (!modal.value) attachPaths(paths);
        else notify("Close this dialog to attach files.");
      }
    });
    unsubscribe = await subscribe(receive);
    chatUnsubscribe = await listen<string>("workspace-chat", (event) =>
      loadChat(event.payload),
    );
    // Phase 45 (F057): the Companion's half-typed message, for the conversation it just opened here; a message
    // already being typed here is never replaced.
    void listen<{ chatId?: string; text?: string }>("workspace-draft", async (event) => {
      const { chatId, text } = event.payload || {};
      if (!chatId || !text) return;
      for (let i = 0; i < 40 && chat.value?.id !== chatId; i++) await new Promise((r) => setTimeout(r, 50));
      if (chat.value?.id === chatId && !input.value.trim()) {
        input.value = text;
        nextTick(() => composer.value?.focus());
      }
    });
    await refresh();
    if (maintenance.value) { booting.value = false; return; }
    chat.value = state.value.chats.length
      ? await rpc<Chat>("get_chat", { id: state.value.chats[0].id })
      : await rpc<Chat>("new_chat");
    await refresh();
    companion.seed(state.value.runs, state.value.active_run); // a run already live or waiting at window load
    resizeUnsubscribe = await getCurrentWindow().onResized(({ payload }) => {
      compact.value = payload.width / window.devicePixelRatio < 720;
    });
  });
  booting.value = false;
  applyTheme();
  await nextTick();
  composer.value?.focus();
});
onUnmounted(() => {
  unsubscribe();
  dropUnsubscribe();
  resizeUnsubscribe();
  chatUnsubscribe();
  document.removeEventListener("keydown", key);
  document.removeEventListener("pebble-notice", markdownNotice);
  document.removeEventListener("pebble-source", openCitation);
  window.clearTimeout(toastTimer);
  window.removeEventListener("resize", onResize);
  colorQuery.removeEventListener("change", applyTheme);
});
</script>

<template>
  <UpgradeMaintenance v-if="maintenance" :view="maintenance" />
  <div v-else class="app-shell" :class="{ compact, 'hide-sidebar': !sidebar }">
    <!-- Phase 45 (F037): in a small window the conversations open over the window from the header's button -->
    <aside v-if="(!compact && sidebar) || (compact && sidebarOverlay)" id="sidebar" class="sidebar" :class="{ 'sidebar-overlay': compact }" :inert="!!modal" aria-label="Conversations">
      <div class="sidebar-brand" data-tauri-drag-region>
        <Avatar
          :state="avatarState"
          :size="30"
          :reduced="state.settings.reduced_motion"
          :stealth="!!state.settings.stealth"
          :rank="AVATAR_RANK.brand"
          :gaze="companionSettings.pointer"
        /><span>Pebble<span class="brand-dot">.</span></span>
      </div>
      <button class="new-chat" @click="createChat">
        <PhPlus :size="17" /> New conversation <kbd>⌘ N</kbd>
      </button>
      <div class="sidebar-heading">YOUR SPACE</div>
      <button
        class="nav-item"
        :class="{ selected: !chat?.workspace }"
        :aria-current="!chat?.workspace ? 'true' : undefined"
        @click="detachProject"
      >
        <PhChatCircle :size="18" /> Companion
      </button>
      <!-- Phase 45 (F176, audit round R5): the project you are in is marked, as Companion is; F205 (R7): its row starts
           a new conversation there, as every project row does (Connect a project stays in the + menu) -->
      <button
        class="nav-item"
        :class="{ selected: !!chat?.workspace }"
        :aria-current="chat?.workspace ? 'true' : undefined"
        :title="chat?.workspace ? 'New conversation in ' + chat.workspace : undefined"
        @click="chat?.workspace ? openProject(chat.workspace) : chooseProject()"
      >
        <PhFolder :size="18" /> {{ projectName || "Connect a project"
        }}<PhPlus v-if="!projectName" :size="13" class="end" />
      </button>
      <button
        v-for="p in recentProjects.filter((p) => p !== chat?.workspace)"
        :key="p"
        class="nav-item project-item"
        :title="'New conversation in ' + p"
        @click="openProject(p)"
      >
        <PhFolder :size="18" /> {{ p.split(/[\\/]/).pop() }}
      </button>
      <div id="recent-heading" class="sidebar-heading recent-heading">RECENT CONVERSATIONS</div>
      <div v-if="state.chats.length" class="chat-search" role="search">
        <PhMagnifyingGlass :size="13" aria-hidden="true" /><input
          v-model="chatSearch"
          type="search"
          maxlength="200"
          placeholder="Search conversations"
          aria-label="Search conversations"
          @keydown.esc="searchEscape"
        /><button v-if="chatSearch" type="button" class="icon-button chat-search-clear" aria-label="Clear the search"
          @click="chatSearch = ''"><PhX :size="12" /></button>
      </div>
      <div class="chat-list">
        <ul class="chat-list-items" aria-labelledby="recent-heading">
          <li v-for="c in listedChats" :key="c.id" class="chat-entry" :data-chat-id="c.id">
            <form
              v-if="renaming === c.id"
              class="chat-rename"
              @submit.prevent="saveRename(c)"
              @keydown.esc.stop.prevent="cancelRename(c.id)"
            >
              <input v-model="renameText" maxlength="200" aria-label="Conversation name" @blur="saveRename(c)" />
            </form>
            <template v-else>
              <button
                :class="{ current: chat?.id === c.id }"
                :aria-current="chat?.id === c.id ? 'page' : undefined"
                @click="loadChat(c.id)"
              >
                <span>{{ c.title }}</span
                ><span v-if="excerptFor(c)" class="chat-excerpt">{{ excerptFor(c) }}</span
                ><span v-if="c.workspace" class="chat-project"
                  ><PhFolder :size="11" />{{
                    c.workspace.split(/[\\/]/).pop()
                  }}</span
                ></button
              ><button
                class="chat-row-action"
                :aria-label="'Rename conversation ' + c.title"
                title="Rename"
                :disabled="!!state.active"
                @click="startRename(c)"
              >
                <PhPencilSimple :size="13" /></button
              ><button
                class="chat-delete"
                :aria-label="'Delete conversation ' + c.title"
                title="Delete"
                :disabled="!!state.active"
                @click="deleteFromSidebar(c.id)"
              >
                <PhTrash :size="13" />
              </button>
            </template>
          </li>
        </ul>
        <p v-if="!state.chats.length" class="no-history">
          Your conversations appear here.
        </p>
        <p v-else-if="!listedChats.length" class="no-history">
          No conversation matches “{{ chatSearch.trim() }}”.
        </p>
        <!-- Phase 45 (F032): deleted conversations are one click away (restorable for 30 days) -->
        <button type="button" class="text-button recently-deleted" @click="openSettings('storage')">
          <PhTrash :size="13" />Recently deleted
        </button>
      </div>
      <div class="sidebar-bottom">
        <button
          class="profile"
          :aria-label="storageAttention ? 'Settings, something waits for you in Data and storage' : 'Settings'"
          @click="openSettings(storageAttention ? 'storage' : 'models')"
        >
          <PhGearSix :size="18" /><span>Settings<small>Models, permissions and memory</small></span
          ><span v-if="storageAttention" class="attention-dot" aria-hidden="true"></span>
        </button>
      </div>
    </aside>

    <div class="main-column" :inert="!!modal">
      <header class="titlebar" data-tauri-drag-region>
        <div class="header-left">
          <button
            v-if="!compact"
            class="icon-button"
            aria-label="Conversations sidebar"
            title="Show or hide conversations"
            :aria-expanded="sidebar"
            aria-controls="sidebar"
            @click="sidebar = !sidebar"
          >
            <PhSidebarSimple :size="19" /></button
          ><button
            v-if="compact"
            class="icon-button"
            aria-label="Conversations"
            title="Conversations"
            :aria-expanded="sidebarOverlay"
            aria-controls="sidebar"
            @click="sidebarOverlay = !sidebarOverlay"
          >
            <PhSidebarSimple :size="17" /></button
          ><span v-if="compact" class="compact-wordmark">pebble.</span
          ><span v-else class="project-switch-wrap" @keydown.esc.stop="projectMenu = false" @focusout="projectFocusOut"
            ><button class="project-switch" :aria-expanded="projectMenu" aria-haspopup="menu" @click="projectMenu = !projectMenu">
              <PhFolder v-if="projectName" :size="16" />{{
                projectName || "Companion"
              }}<PhCaretDown :size="12" />
            </button>
            <!-- Phase 45 (F029): the chevron opens a menu (recent projects, a folder, no project), not a bare picker. -->
            <div v-if="projectMenu" class="project-menu" role="menu">
              <button v-for="p in recentProjects.filter((p) => p !== chat?.workspace)" :key="p" type="button" role="menuitem" :title="p" @mousedown.prevent @click="openProject(p)">
                <PhFolder :size="15" />{{ p.split(/[\\/]/).pop() }}
              </button>
              <button type="button" role="menuitem" @mousedown.prevent @click="projectMenu = false; chooseProject()">
                <PhPlus :size="15" />Choose a project folder…
              </button>
              <button v-if="projectName" type="button" role="menuitem" @mousedown.prevent @click="projectMenu = false; detachProject()">
                <PhChatCircle :size="15" />Leave this project (Companion)
              </button>
            </div></span
          >
        </div>
        <div class="header-right">
          <button
            v-if="chat?.artifacts?.length && !compact"
            ref="canvasButton"
            class="icon-button"
            title="Open interactive canvas"
            aria-label="Open interactive canvas"
            :aria-expanded="canvas"
            @click="toggleCanvas"
          >
            <PhCode :size="18" />
          </button>
          <button class="local-status" :aria-label="'Model: ' + localLabel + '. Open model settings'" @click="openSettings('models')">
            <i :class="state.model.state" aria-hidden="true" />{{ localLabel }}</button
          ><button
            v-if="!compact"
            class="icon-button"
            aria-label="Settings"
            title="Settings (⌘,)"
            @click="openSettings()"
          >
            <PhGearSix :size="18" /></button
          ><button
            class="icon-button"
            :title="compact ? 'Expand workspace' : 'Floating companion'"
            :aria-label="compact ? 'Expand workspace' : 'Floating companion'"
            @click="toggleCompact"
          >
            <PhArrowsOutSimple v-if="compact" :size="18" /><PhArrowsInSimple
              v-else
              :size="18"
            /></button
          ><button
            ref="detailsButton"
            class="icon-button"
            :title="'Details (' + detailsKeys + ')'"
            :aria-label="attention ? `Details, ${attention} waiting` : 'Details'"
            :aria-expanded="drawerOpen && !canvas"
            aria-controls="details-drawer"
            :class="{ active: drawerOpen && !canvas }"
            @click="toggleDrawer()"
          >
            <PhSidebarSimple :size="19" mirrored /><span
              v-if="attention"
              class="badge"
              aria-hidden="true"
              >{{ attention }}</span
            >
          </button>
        </div>
      </header>
      <div v-if="!native" class="preview-banner">
        Design preview · open the desktop app to chat and take actions
      </div>
      <div class="workspace-body">
        <!-- Phase 22: the chat column holds only the conversation, the status line and the composer.
             Inspectors, goal, schedules, project tools, windows and the browser are in the Details drawer. -->
        <main class="conversation" aria-label="Conversation">
          <div ref="scroll" class="conversation-scroll" :inert="sheetOpen">
            <div v-if="booting" class="booting">
              <Avatar state="loading" :size="76" :rank="AVATAR_RANK.booting" :stealth="!!state.settings.stealth" />
              <p>Getting your space ready…</p>
            </div>
            <div v-else-if="!chat?.messages.length" class="welcome">
              <div class="avatar-stage">
                <Avatar
                  :state="avatarState"
                  :size="compact ? 92 : firstRun ? 96 : 130"
                  :reduced="state.settings.reduced_motion"
                  :stealth="!!state.settings.stealth"
                  :rank="AVATAR_RANK.welcome"
                  :gaze="companionSettings.pointer"
                />
              </div>
              <h1>
                {{
                  firstRun ? "Welcome to Pebble." : projectName ? "Let’s build something." : "Good to see you."
                }}
              </h1>
              <p class="welcome-description">
                {{
                  firstRun
                    ? "Connect a model to start. Pebble answers with it, and asks before it changes your files or your Mac."
                    : projectName
                      ? `I'm here to help you explore, fix, and build in ${projectName}.`
                      : "A small companion for your next big idea.\nThink through a problem, write code, or get things moving."
                }}
              </p>
              <!-- Phase 24 (V1-038): a first launch offers the two ways to connect a model, and says in one sentence
                   each what memory and the permission mode do. -->
              <section v-if="firstRun" class="setup-card" aria-labelledby="setup-heading">
                <h2 id="setup-heading" class="setup-heading">Connect a model</h2>
                <div class="setup-choices">
                  <button type="button" class="setup-choice primary" @click="openSettings('models')">
                    <strong>Connect your ChatGPT account</strong>
                    <small>Sign in with your ChatGPT plan. Your messages are sent to OpenAI to be answered.</small>
                  </button>
                  <button type="button" class="setup-choice" @click="openSettings('models', 'add-provider')">
                    <strong>Use an API key</strong>
                    <small>Anthropic, OpenAI or a local server such as Ollama or LM Studio. Messages go to the provider you choose.</small>
                  </button>
                  <button type="button" class="setup-choice" @click="chooseModel">
                    <strong>Use a local model</strong>
                    <small>Choose a GGUF model file on this Mac. Replies are made on this Mac.</small>
                  </button>
                </div>
                <ul class="setup-facts">
                  <li>
                    <strong>Memory:</strong> Pebble keeps useful facts from your conversations on this Mac and uses them
                    in later replies; you can see and forget them in Settings → Memory.
                  </li>
                  <li>
                    <strong>Permissions: {{ modeName(state.settings.permissions.mode) }}.</strong>
                    Always asks: {{ MODES[modeKey].always_asks }}
                  </li>
                </ul>
              </section>
              <div v-else class="starter-grid">
                <!-- Phase 45 (F176): in a connected project the card works in it instead of offering to connect one -->
                <button v-if="projectName" @click="prompt('Give me a short tour of this project, then ask what I want to build.')">
                  <span class="starter-icon"><PhCode :size="21" /></span
                  ><span
                    >Build with me<small
                      >Work in {{ projectName }}</small
                    ></span
                  ><PhArrowRight :size="16" />
                </button>
                <button v-else @click="chooseProject">
                  <span class="starter-icon"><PhCode :size="21" /></span
                  ><span
                    >Build with me<small
                      >Connect a project and dive in</small
                    ></span
                  ><PhArrowRight :size="16" />
                </button>
                <button
                  @click="
                    prompt(
                      'Explain a technical concept to me. Start by asking what I am working on.',
                    )
                  "
                >
                  <span class="starter-icon"><PhChatCircle :size="21" /></span
                  ><span
                    >Think it through<small
                      >Make a complex idea click</small
                    ></span
                  ><PhArrowRight :size="16" />
                </button>
                <button @click="openApps">
                  <span class="starter-icon"><PhCommand :size="21" /></span
                  ><span
                    >A little helping hand<small
                      >Open an app on your device</small
                    ></span
                  ><PhArrowRight :size="16" />
                </button>
                <button @click="attach">
                  <span class="starter-icon"><PhPaperclip :size="21" /></span
                  ><span
                    >Bring some context<small
                      >Read a document together</small
                    ></span
                  ><PhArrowRight :size="16" />
                </button>
              </div>
              <p class="welcome-footnote">
                <PhShieldCheck :size="14" aria-hidden="true" />{{ welcomeFoot }}
              </p>
            </div>
            <div v-else class="messages">
              <div
                v-for="message in chat.messages"
                :key="message.id"
                class="message"
                :class="[message.role, { latest: message === chat.messages.at(-1) }]"
                :data-message-id="message.id"
              >
                <h3 class="sr-only">{{ message.role === "user" ? "You said:" : "Pebble said:" }}</h3>
                <template v-if="message.role === 'assistant'"
                  ><div class="message-avatar">
                    <Avatar
                      :state="
                        isWorking && message === chat?.messages.at(-1)
                          ? avatarState
                          : 'idle'
                      "
                      :size="29"
                      :reduced="state.settings.reduced_motion"
                      :stealth="!!state.settings.stealth"
                      :animate="isWorking && message === chat?.messages.at(-1)"
                      :rank="AVATAR_RANK.message"
                      :gaze="companionSettings.pointer"
                    />
                  </div>
                  <div class="message-content">
                    <ThinkingDisclosure
                      v-if="recordFor(message)"
                      :record="recordFor(message)"
                      :unavailable="activityStore.state.supported === false"
                      :reduced="state.settings.reduced_motion"
                      @expand="loadActivity"
                    ><template v-if="workView" #body
                      ><WorkView
                        :record="recordFor(message)"
                        :task="taskBefore(message.id)"
                        :unavailable="activityStore.state.supported === false"
                        :reduced="state.settings.reduced_motion"
                        :chat-id="chat?.id"
                        :git="message === chat?.messages.at(-1) ? gitAccess : null"
                        @expand="loadActivity" /></template
                    ></ThinkingDisclosure>
                    <div v-if="message.captures?.length" class="message-attachments message-captures" aria-label="Screenshots Pebble took">
                      <figure v-for="(capture, index) in message.captures" :key="index">
                        <img :src="capture.preview" alt="Screenshot Pebble took for this reply" />
                        <figcaption>Screenshot Pebble took</figcaption>
                      </figure>
                    </div>
                    <ReplyBody :text="message.content" /><p
                      v-if="markers.get(message.id)"
                      class="message-marker"
                    >{{ markers.get(message.id) }}</p
                    ><MessageActions
                      v-if="!(isWorking && message === chat.messages.at(-1))"
                      role="assistant"
                      :text="message.content"
                      :retry="canRetry(message)"
                      :retry-reason="retryReason(message)"
                      :disabled="busy"
                      @retry="retry(message)"
                      @copied="notify('Copied')"
                      @failed="error = $event"
                      ><TokenStats v-if="message.usage" :usage="message.usage"
                    /></MessageActions></div
                ></template>
                <div v-else class="user-turn">
                  <form
                    v-if="editing === message.id"
                    class="message-editor"
                    aria-label="Edit your message"
                    @submit.prevent="sendEdited(message)"
                    @keydown.esc.stop.prevent="cancelEdit(message.id)"
                  >
                    <textarea
                      :ref="keepEditor"
                      v-model="editText"
                      aria-label="Edited message"
                      rows="3"
                      @keydown.enter.exact.prevent="sendEdited(message)"
                    />
                    <p class="message-editor-note">
                      Sends a new message; this one stays as it was.
                      <button v-if="wroteFiles" type="button" class="text-button" @click="openDrawer('changes', false, true)">Undo this turn's file changes…</button>
                    </p>
                    <div class="message-editor-actions">
                      <button type="button" class="secondary-button" @click="cancelEdit(message.id)">Cancel</button>
                      <button type="submit" class="primary-button" :disabled="!editText.trim() || busy || isWorking">Send edited</button>
                    </div>
                  </form>
                  <div v-else class="user-bubble">
                    <div
                      v-if="message.attachments?.length"
                      class="message-attachments"
                    >
                      <figure v-for="a in message.attachments" :key="a.id">
                        <img v-if="a.preview" :src="a.preview" :alt="a.name" />
                        <PhPaperclip v-else :size="18" />
                        <figcaption>{{ a.name }}</figcaption>
                      </figure>
                    </div>
                    <span class="user-text">{{ message.content }}</span>
                  </div>
                  <p v-if="markers.get(message.id)" class="message-marker">{{ markers.get(message.id) }}</p>
                  <MessageActions
                    v-if="editing !== message.id"
                    role="user"
                    :text="message.content"
                    :edit="message === lastUser && !isWorking"
                    :disabled="busy || isWorking"
                    @edit="startEdit(message)"
                    @copied="notify('Copied')"
                    @failed="error = $event"
                  />
                </div>
              </div>
              <section v-for="card in toolboxCards.filter(toolboxShown)" :key="'toolbox-' + card.name" class="forget-card toolbox-card" :aria-label="card.kind === 'kept' ? 'Kept in Toolbox' : card.kind === 'remove' ? 'Remove this tool?' : 'Keep this tool?'">
                <p class="toolbox-title"><strong>{{ card.kind === "kept" ? "Kept in Pebble's Toolbox" : card.kind === "remove" ? "Remove this tool from Pebble's Toolbox?" : "Keep this tool in Pebble's Toolbox?" }}</strong> · {{ card.name }}</p>
                <p class="toolbox-purpose">{{ card.purpose }}</p>
                <ul v-if="card.kind === 'asked' && card.why.length" class="toolbox-why"><li v-for="(why, index) in card.why" :key="index">Pebble asks because {{ why }}.</li></ul>
                <p v-if="card.kind === 'kept'" class="toolbox-note">Pebble kept this tested calculation on its own. Remove it if you don't want it.</p>
                <p v-else-if="card.kind === 'remove'" class="toolbox-note">You asked to remove it. Nothing is removed until you choose Remove.</p>
                <pre v-if="card.code" class="command-preview">{{ card.code }}</pre>
                <p v-if="card.error" class="forget-error" role="alert">{{ card.error }}</p>
                <p v-if="card.done" class="forget-done" role="status">{{ card.done }}</p>
                <div v-else class="forget-actions">
                  <button type="button" class="text-button" :aria-label="(card.code ? 'Hide the code of ' : 'View the code of ') + card.name" @click="toolboxView(card)">{{ card.code ? "Hide code" : "View code" }}</button>
                  <button v-if="card.kind === 'kept'" type="button" class="secondary-button" :aria-label="'Remove ' + card.name + ' from the Toolbox'" :disabled="card.busy" @click="toolboxRemove(card)">Remove</button>
                  <template v-else-if="card.kind === 'remove'">
                    <button type="button" class="secondary-button" :aria-label="'Keep ' + card.name" :disabled="card.busy" @click="card.done = 'Kept in the Toolbox'">Keep it</button>
                    <button type="button" class="primary-button" :aria-label="'Remove ' + card.name + ' from the Toolbox'" :disabled="card.busy" @click="toolboxRemove(card)">Remove</button>
                  </template>
                  <template v-else>
                    <button type="button" class="secondary-button" :disabled="card.busy" @click="toolboxDecide(card, false)">Discard</button>
                    <button type="button" class="primary-button" :disabled="card.busy" @click="toolboxDecide(card, true)">Keep</button>
                  </template>
                </div>
              </section>
              <section v-if="forgetCard && forgetCard.chatId === chat.id" class="forget-card" aria-labelledby="forget-heading">
                <h3 id="forget-heading" class="forget-heading">
                  {{ forgetCard.subject ? `Saved memories about “${forgetCard.subject}”` : "Saved memories you may want to forget" }}
                </h3>
                <p class="forget-note">Nothing is forgotten until you choose Forget. These are records Pebble saved; forgetting one removes it for good.</p>
                <ul class="forget-list">
                  <li v-for="item in forgetCard.items" :key="item.id">
                    <div class="forget-item">
                      <p>{{ item.content }}</p>
                      <small v-if="item.where">{{ item.where }}</small>
                    </div>
                    <span v-if="forgetCard.done[item.id]" class="forget-done" role="status">Forgotten</span>
                    <button
                      v-else
                      type="button"
                      class="secondary-button"
                      :aria-label="'Forget: ' + item.content.slice(0, 60)"
                      :disabled="!!forgetCard.busy || isWorking"
                      @click="forgetMemory(item)"
                    >
                      Forget
                    </button>
                  </li>
                </ul>
                <p v-if="forgetCard.error" class="forget-error" role="alert">{{ forgetCard.error }}</p>
                <div class="forget-actions">
                  <button type="button" class="text-button" @click="openSettings('memory')">All memories in Settings</button>
                  <button type="button" class="text-button" @click="forgetCard = null">Done</button>
                </div>
              </section>
              <ThinkingDisclosure
                v-if="orphanRecord"
                class="orphan-thinking"
                :record="orphanRecord"
                :unavailable="activityStore.state.supported === false"
                :reduced="state.settings.reduced_motion"
                @expand="loadActivity"
              ><template v-if="workView" #body
                ><WorkView
                  :record="orphanRecord"
                  :task="taskBefore()"
                  :unavailable="activityStore.state.supported === false"
                  :reduced="state.settings.reduced_motion"
                  @expand="loadActivity" /></template
              ></ThinkingDisclosure>
              <!-- Phase 19: host-decided status words replace the raw progress label (labels can embed paths).
                   Always mounted, so its one polite region can announce "Reply finished".
                   Phase 22: the companion (in the window's avatar election) replaces the dot, the host's
                   step or reasoning headline follows the word, the time is the host's when known, and the
                   words open the live activity timeline. -->
              <div class="live-turn">
                <StatusLine
                  class="working-line"
                  :status="state.active && state.active !== chat?.id ? null : companion.pose.value.status"
                  :started-at="hostStart ?? companion.pose.value.startedAt"
                  :exact="hostStart !== null"
                  :attempt="companion.pose.value.attempt"
                  :outcome="companionOutcome"
                  :mode="companionSettings.status_words"
                  :reduced="state.settings.reduced_motion"
                  :stoppable="isWorking"
                  :grace="500"
                  :expandable="!!liveRecord"
                  :expanded="liveOpen"
                  controls="live-thinking"
                  @toggle="liveOpen = !liveOpen"
                  @stop="stop"
                >
                  <template #companion
                    ><Avatar
                      :state="avatarState"
                      :size="24"
                      :reduced="state.settings.reduced_motion"
                      :stealth="!!state.settings.stealth"
                      :rank="AVATAR_RANK.status"
                      :gaze="companionSettings.pointer"
                  /></template>
                  <template v-if="statusDetail" #detail>{{ statusDetail }}</template>
                </StatusLine>
                <!-- Phase 46 (1.9.29, D206): the work view's cards, or with the setting off today's rows -->
                <WorkView
                  v-if="workView && liveOpen && liveRecord && !(state.active && state.active !== chat?.id)"
                  id="live-thinking"
                  :record="liveRecord"
                  :task="taskBefore()"
                  :unavailable="activityStore.state.supported === false"
                  :reduced="state.settings.reduced_motion"
                  :chat-id="chat?.id"
                  :git="gitAccess"
                  @expand="loadActivity"
                />
                <ThinkingDisclosure
                  v-else-if="liveOpen && liveRecord && !(state.active && state.active !== chat?.id)"
                  live
                  open
                  body-id="live-thinking"
                  :record="liveRecord"
                  :unavailable="activityStore.state.supported === false"
                  :reduced="state.settings.reduced_motion"
                  @expand="loadActivity"
                />
                <!-- Phase 45 (F105): the reply as it is written, until the checked reply replaces it -->
                <AttemptStream
                  v-if="chat && isWorking && state.active === chat.id && state.active_run"
                  inline
                  :chat-id="chat.id"
                  :run-id="state.active_run"
                />
              </div>
              <RunPanel
                view="conversation"
                :chat="chat"
                :memory="state.memory"
                @refresh="refreshRun"
                @error="error = $event"
                @settled="error.startsWith('The screen act has an uncertain outcome') && (error = '')"
                @details="openDrawer('task', false, true)"
              />
              <div v-if="pending.length" class="review-callout">
                <PhShieldCheck :size="19" />
                <div>
                  <strong
                    >{{ pending.length }}
                    {{ pending.length === 1 ? "action needs" : "actions need" }}
                    your review</strong
                  ><small>Nothing changes until you approve.</small>
                </div>
                <button @click="showReviews()">
                  Review<PhArrowRight :size="14" />
                </button>
              </div>
            </div>
            <div v-if="chat?.artifacts?.length" class="artifact-links">
              <button
                v-for="a in chat.artifacts"
                :key="a.id"
                @click="openArtifact(a.id)"
              >
                <PhPlay :size="15" />{{
                  a.kind === "quiz"
                    ? "Start / resume test"
                    : a.kind === "diagram"
                      ? "Open diagram"
                      : a.kind === "dashboard"
                        ? "Open dashboard"
                        : a.kind === "prototype"
                          ? "Open app demo"
                          : "Open interactive view"
                }}
                · {{ a.title }}<PhArrowRight :size="14" />
              </button>
            </div>
          </div>
          <div class="composer-area" :inert="sheetOpen">
            <div v-if="error" class="error-banner" role="alert">
              <PhWarningCircle :size="18" /><span>{{ error }}</span
              ><button v-if="serviceOffline" type="button" class="secondary-button" @click="restartService">Restart service</button
              ><button
                class="icon-button"
                aria-label="Dismiss error"
                @click="error = ''"
              >
                <PhX :size="15" />
              </button>
            </div>
            <div v-if="needsModel && !state.model.configured" class="storage-notice needs-model" role="alert">
              <span>Connect a model first; your message stays here and is sent when you press Send again.</span
              ><button type="button" class="text-button" @click="openSettings('models'); needsModel = false">ChatGPT</button
              ><button type="button" class="text-button" @click="openSettings('models', 'add-provider'); needsModel = false">API key</button
              ><button type="button" class="text-button" @click="chooseModel(); needsModel = false">Local model</button
              ><button class="icon-button" aria-label="Dismiss note" @click="needsModel = false">
                <PhX :size="15" />
              </button>
            </div>
            <div v-if="storageNotice" class="storage-notice" role="status">
              <span>{{ storageNotice }}</span
              ><button type="button" class="text-button" @click="openSettings('storage'); storageNotice = ''">Data &amp; storage</button
              ><button class="icon-button" aria-label="Dismiss note" @click="storageNotice = ''">
                <PhX :size="15" />
              </button>
            </div>
            <div v-if="browserNotice" class="storage-notice" role="status">
              <span>{{ browserNotice }}</span
              ><button type="button" class="text-button" @click="openDrawer('browser', false, true); browserNotice = ''">Open Browser</button
              ><button class="icon-button" aria-label="Dismiss note" @click="browserNotice = ''">
                <PhX :size="15" />
              </button>
            </div>
            <div v-if="compact && pending.length" class="compact-reviews">
              <button @click="showReviews(true)">
                <PhShieldCheck :size="16" />{{ pending.length }} actions to
                review<PhCaretDown :size="13" />
              </button>
            </div>
            <div class="composer" :class="{ focused: input.length }">
              <SlashMenu
                v-if="slashOpen"
                :items="slashItems"
                :active="slashIndex"
                :heading="slashHeading"
                :note="slashNote"
                list-id="slash-menu"
                @choose="chooseSlashItem($event, 'click')"
                @hover="slashIndex = $event"
              />
              <div v-if="sources.length || attachmentBusy" class="source-chips">
                <span v-for="source in sources" :key="source.id" :title="source.notice || sourceTitle(source)"
                  ><img
                    v-if="source.preview"
                    :src="source.preview"
                    alt=""
                    class="attachment-thumb" /><PhPaperclip
                    v-else
                    :size="12" />{{ sourceLabel(source)
                  }}<button
                    :aria-label="'Remove attachment ' + source.name"
                    @click="removeSource(source.id)"
                  >
                    <PhX :size="12" /></button></span
                ><span v-if="attachmentBusy"
                  ><PhCircleNotch class="spin" :size="13" />Reading
                  source…</span
                >
              </div>
              <small
                v-for="source in sources.filter(s => s.notice)" :key="source.id" class="image-provider-note"
              >{{ source.name }}: {{ source.notice }}</small>
              <small
                v-if="sources.some((s) => s.kind === 'image')"
                class="image-provider-note"
                >Images are sent to your selected vision-capable
                provider.</small
              >
              <textarea
                ref="composer"
                v-model="input"
                :disabled="booting"
                :placeholder="
                  projectName
                    ? 'What are we building? Type / for commands'
                    : 'Ask, think, or build something… Type / for commands'
                "
                aria-label="Message Pebble"
                aria-autocomplete="list"
                :aria-controls="slashOpen && slashItems.length ? 'slash-menu' : undefined"
                :aria-activedescendant="slashOpen && slashIndex >= 0 && slashItems[slashIndex] ? 'slash-menu-' + slashIndex : undefined"
                rows="2"
                @keydown="composerKey"
                @paste="paste"
                @focus="composerFocused = true"
                @blur="composerFocused = false"
              />
              <div class="composer-toolbar">
                <div class="composer-tools">
                  <div class="attach-menu-wrap">
                    <button
                      class="icon-button"
                      :disabled="isWorking || attachmentBusy"
                      title="Add context"
                      aria-label="Add context"
                      @click="showAttach = !showAttach"
                    >
                      <PhPlus :size="20" />
                    </button>
                    <div v-if="showAttach" class="attach-menu">
                      <button
                        @click="
                          showAttach = false;
                          modal = 'terminal';
                        "
                      >
                        <PhCommand :size="16" />Terminal command
                      </button>
                      <button @click="attach">
                        <PhPaperclip :size="16" />Attach a file</button
                      ><button
                        @click="
                          showAttach = false;
                          modal = 'url';
                        "
                      >
                        <PhGlobe :size="16" />Read a web source</button
                      ><button
                        @click="
                          showAttach = false;
                          chooseProject();
                        "
                      >
                        <PhFolder :size="16" />Connect a project
                      </button>
                    </div>
                  </div>
                  <ModelPicker
                    :profiles="state.profiles || []"
                    :current="state.model.profile_id"
                    :disabled="!!state.active || busy"
                    :notice="deviceNotice && !onThisMac ? deviceNotice.label : ''"
                    @select="changeModel"
                    @review="reviewDeviceHistory"
                    @dismiss="chat && deviceNotices.delete(chat.id)"
                    @connect="openSettings('models')"
                  />
                  <label
                    class="composer-select effort-picker"
                    :class="{ locked: availableEfforts.length === 1 }"
                    :title="
                      availableEfforts.length === 1
                        ? 'This model has no verified effort control; provider default is used.'
                        : 'Controls this model’s actual reasoning parameter'
                    "
                    ><select
                      ref="effortSelect"
                      :value="effort"
                      :disabled="
                        isWorking || busy || availableEfforts.length === 1
                      "
                      @change="changeEffort"
                      aria-label="Reasoning effort"
                    >
                      <option
                        v-for="level in availableEfforts"
                        :key="level"
                        :value="level"
                      >
                        {{ effortLabel(level)
                        }}{{
                          level === state.model.default_effort &&
                          level !== "default"
                            ? " · recommended"
                            : ""
                        }}
                      </option></select
                    ><PhCaretDown v-if="availableEfforts.length > 1" :size="10"
                  /></label>
                </div>
                <button
                  v-if="isWorking && !input.trim()"
                  class="send-button stop"
                  title="Stop task"
                  aria-label="Stop task"
                  @click="stop"
                >
                  <PhStop :size="16" weight="fill" /></button
                ><button
                  v-else
                  class="send-button"
                  :disabled="!input.trim() || busy || booting || attachmentBusy"
                  title="Send message"
                  aria-label="Send message"
                  @click="send()"
                >
                  <PhArrowUp :size="20" weight="bold" />
                </button>
              </div>
            </div>
            <div class="composer-foot">
              <button
                class="permission-label"
                :aria-label="'Permissions: ' + modeName(state.settings.permissions.mode) + '. Open permission settings'"
                @click="openSettings('permissions')"
              >
                <PhShieldCheck :size="12" aria-hidden="true" />{{ modeName(state.settings.permissions.mode) }}
              </button>
              <span class="foot-model" :title="modelFoot"
                ><span class="tiny-dot" aria-hidden="true" />{{ modelFoot }}</span
              ><button
                v-if="contextUse && contextUse.percent >= RING_AT"
                type="button"
                class="context-ring"
                :aria-label="`${contextUse.percent}% of context used by the last request. Open Context`"
                :title="`${contextUse.used.toLocaleString()} of ${contextUse.window.toLocaleString()} tokens`"
                @click="openDrawer('context', false, true)"
              >
                <svg viewBox="0 0 16 16" width="12" height="12" aria-hidden="true" focusable="false">
                  <circle cx="8" cy="8" r="6" class="context-ring-track" />
                  <circle cx="8" cy="8" r="6" class="context-ring-fill" :stroke-dasharray="`${Math.min(100, contextUse.percent) * 0.377} 37.7`" />
                </svg>
                {{ contextUse.percent }}% of context used
              </button><span v-if="!compact" class="foot-note" title="Pebble can make mistakes. Check the evidence."
                >Pebble can make mistakes. Check the evidence.</span
              ><button
                v-if="compact"
                class="icon-button"
                title="Settings"
                aria-label="Settings"
                @click="openSettings()"
              >
                <PhGearSix :size="14" />
              </button>
            </div>
          </div>
        </main>

        <ArtifactPanel
          v-if="canvas && chat?.artifacts?.length && !compact"
          :artifacts="chat.artifacts"
          :selected="selectedArtifact"
          @close="closeCanvas"
          @export="exportView"
          @feedback="demoFeedback"
        />
        <!-- Phase 24 (V1-163): over a narrow window the drawer is a sheet: a click beside it closes it, and the
             conversation behind it is inert until it closes. -->
        <div v-if="sheetOpen" class="details-scrim" aria-hidden="true" @click="closeDrawer"></div>
        <DetailsDrawer
          v-if="drawerOpen && !canvas && chat"
          ref="drawer"
          :tabs="drawerTabs"
          :more="moreTabs"
          :tab="activeTab"
          :width="drawerWidth"
          :overlay="drawerOverlay"
          :viewport="viewport"
          @update:tab="drawerTab = $event"
          @update:width="drawerWidth = $event"
          @reveal="revealed.add($event)"
          @close="closeDrawer"
        >
          <template #empty>
            <p class="details-empty-title">Nothing to review yet.</p>
            <p class="details-empty-note">Steps, reviews and changes appear here once Pebble works on something.</p>
            <div class="details-empty-actions">
              <button v-for="t in moreTabs" :key="t.id" type="button" class="secondary-button" @click="openDrawer(t.id, false, true)">{{ t.label }}</button>
            </div>
          </template>
          <template #activity>
            <!-- Phase 24 (V1-144): approvals and scoped tools are the protection; AgentGuard is a reliability check,
                 and when it cannot start, project tools are off, which the person needs to know. -->
            <div class="protection-status" :class="{ off: !state.guard.available }">
              <PhShieldCheck :size="21" aria-hidden="true" />
              <div>
                <strong>{{ modeName(state.settings.permissions.mode) }}</strong
                ><small>{{
                  state.guard.available
                    ? "Approvals and scoped tools"
                    : "Project tools are off: the reliability checker could not start. See Settings."
                }}</small>
              </div>
            </div>
            <template v-for="group in actionGroups" :key="group.id">
            <h3 v-if="group.items.length" class="details-heading">{{ group.label }}</h3>
            <article
              v-for="action in group.items"
              :key="action.id"
              class="action-card"
              :class="{ pending: action.status === 'pending' }"
            >
              <div class="action-heading">
                <PhCode v-if="action.kind === 'edit'" :size="17" /><PhPlay
                  v-else-if="action.kind === 'tests'"
                  :size="17"
                /><PhCommand v-else :size="17" /><strong class="action-title" tabindex="-1">{{
                  action.kind === "edit"
                    ? action.payload.path
                    : action.kind === "tests"
                      ? (isCheck(action.payload || {}) ? "Run an ad hoc check" : "Run isolated tests")
                      : action.kind === "command"
                        ? action.payload.command
                        : action.kind === "plugin"
                          ? `${action.payload.name} / ${action.payload.tool}`
                          : action.kind === "git"
                            ? `Git ${action.payload.operation}`
                            : action.kind === "worktree"
                              ? (action.payload.operation === "retire_child_worktree" ? "Retire child worktree" : "Allocate child worktree")
                              : action.kind === "install"
                                ? `Install dependencies (${action.payload.kind})`
                                : action.kind === "shell"
                                  ? action.payload.command
                                  : action.kind === "file"
                                    ? action.payload.path
                                    : action.kind === "screen"
                                      ? screenHeading(action.payload.review)
                                      : action.kind === "screen_grant"
                                        ? `Use ${action.payload.app}`
                                        : action.kind === "url"
                                          ? `Open ${action.payload.url}`
                                          : action.kind === "screen_raise"
                                            ? `Bring ${action.payload.app} forward`
                                            : action.kind === "app_action"
                                              ? action.payload.summary || `Use ${action.payload.app}`
                                              : action.kind === "browser_grant"
                                                ? `Use ${siteNames(action.payload.sites)}`
                                                : action.payload.document  // Phase 45 (F200): a file in the app
                                                  ? `Open ${String(action.payload.document).split("/").pop()} in ${action.payload.name}`
                                                  : `Open ${action.payload.name}`
                }}</strong
                ><span :class="action.status">{{ actionState(action.status) }}</span>
              </div>
              <p v-if="actionLead(action)" class="action-lead">{{ actionLead(action) }}</p>
              <template v-if="action.kind === 'edit'"
                ><p class="action-description">
                  {{
                    action.payload.baseline
                      ? "Proposed source edit"
                      : "New file draft"
                  }}
                  · original stays intact until approval
                </p>
                <details :open="action.status === 'pending'">
                  <summary>View diff</summary>
                  <pre
                    class="diff"
                  ><span v-for="(line,i) in (action.payload.diff||'No changes').split('\n')" :key="i" :class="{added:line.startsWith('+'),removed:line.startsWith('-'),hunk:line.startsWith('@@')}">{{line}}<br/></span></pre></details
              ></template>
              <template v-else-if="action.kind === 'tests' && isCheck(action.payload || {})"
                ><p v-if="action.payload.check_run" class="action-description">
                  Your project's tests ({{ action.payload.recipe }}) in one temporary copy, then
                  {{ action.payload.check.path }} alone ({{ action.payload.check_run.recipe }}) in another.
                  Only your tests' own run counts. Network and original project writes are blocked.
                </p>
                <p v-else class="action-description">
                  {{ action.payload.recipe }} with {{ action.payload.check.path }}
                  added to a temporary project copy. It doesn't count as your
                  project's tests. Network and original project writes are blocked.
                </p>
                <details :open="action.status === 'pending'">
                  <summary>View the check</summary>
                  <pre class="command-preview">{{ action.payload.check.content }}</pre>
                </details></template
              >
              <p v-else-if="action.kind === 'tests'" class="action-description">
                {{ action.payload.recipe }} in a temporary project copy. Network
                and original project writes are blocked.
              </p>
              <template v-else-if="action.kind === 'command'"
                ><p class="action-description">
                  {{ action.payload.risk }} · Selected project only ·
                  {{
                    action.payload.network
                      ? "Network access requested"
                      : "Network denied"
                  }}.
                  {{
                    action.payload.writable
                      ? "Project writes allowed"
                      : "Read only"
                  }}.
                </p>
                <pre class="command-preview">{{ action.payload.command }}</pre>
                <small class="command-cwd">{{
                  action.payload.root
                }}</small></template
              >
              <template v-else-if="action.kind === 'plugin'"
                ><p class="action-description">
                  External tool call · {{ action.payload.url }}. Review the
                  arguments before allowing this call.
                </p>
                <pre class="command-preview">{{
                  JSON.stringify(action.payload.arguments, null, 2)
                }}</pre>
              </template>
              <ActionReview :action="action" />
              <ConnectorEffectReview v-if="action.kind === 'plugin' && action.payload.effect" :action="action" :chat-id="action.chat_id" :disabled="!!actionBusy || isWorking" />
              <pre
                v-if="
                  action.payload.result?.output ||
                  action.payload.result?.untrusted_plugin_output
                "
                class="test-output"
                >{{
                  action.payload.result.output ||
                  action.payload.result.untrusted_plugin_output
                }}</pre>
              <div v-if="action.status === 'pending'" class="action-buttons">
                <button
                  class="secondary-button"
                  :disabled="!!actionBusy || isWorking"
                  @click="decide(action, 'reject')"
                >
                  Don't allow</button
                ><button
                  v-if="(action.kind === 'shell' || action.kind === 'file') && action.payload.always_offered"
                  class="secondary-button"
                  :disabled="!!actionBusy || isWorking"
                  @click="grantAndApprove(action)"
                >
                  Always allow</button
                ><button
                  class="primary-button"
                  :disabled="!!actionBusy || isWorking"
                  @click="decide(action, 'approve')"
                >
                  <PhCircleNotch
                    v-if="actionBusy === action.id"
                    class="spin"
                    :size="13"
                  /><PhCheck v-else :size="13" />{{
                    action.kind === "edit"
                      ? "Apply edit"
                      : action.kind === "tests"
                        ? "Run tests"
                        : action.kind === "command" || action.kind === "shell"
                          ? "Run command"
                          : action.kind === "plugin"
                            ? "Call plugin"
                            : action.kind === "git"
                              ? "Run Git plan"
                              : action.kind === "worktree"
                                ? (action.payload.operation === "retire_child_worktree" ? "Retire worktree" : "Create worktree")
                                : action.kind === "install"
                                  ? "Install from lockfile"
                                  : action.kind === "file"
                                    ? "Write file"
                                    : action.kind === "screen"
                                      ? "Do it"
                                      : action.kind === "screen_grant"
                                        ? "Allow"
                                        : action.kind === "url"
                                          ? "Open link"
                                          : action.kind === "screen_raise"
                                            ? "Bring forward"
                                            : action.kind === "app_action"
                                              ? "Do it"
                                              : action.kind === "browser_grant"
                                                ? "Allow"
                                                : "Open app"
                  }}
                </button>
              </div>
              <button
                v-if="action.kind === 'edit' && action.status === 'applied'"
                class="undo-button"
                :disabled="!!actionBusy"
                @click="decide(action, 'undo')"
              >
                <PhArrowCounterClockwise :size="13" />Undo this edit
              </button>
              <!-- Phase 46 (1.9.28, D184): a command Auto ran without a card, from its copy (1.9.29, D209: kept a day
                   after its task ends) -->
              <button
                v-if="action.kind === 'command' && action.status === 'applied' && action.payload.result?.undo === 'available'"
                class="undo-button"
                :disabled="!!actionBusy"
                @click="decide(action, 'undo')"
              >
                <PhArrowCounterClockwise :size="13" />Undo this command
              </button>
              <button
                v-if="(action.kind === 'shell' || action.kind === 'file') && action.status === 'applied' && (action.payload.result?.snapshot?.undoable || action.payload.result?.undoable)"
                class="undo-button"
                :disabled="!!actionBusy"
                @click="shellUndo(action)"
              >
                <PhArrowCounterClockwise :size="13" />Undo this change
              </button>
              <button
                v-if="action.kind === 'git' && action.status === 'applied' && action.payload.operation !== 'undo' && action.payload.result?.receipt_id"
                class="undo-button"
                :disabled="!!actionBusy"
                @click="proposeGitUndo(action)"
              >
                <PhArrowCounterClockwise :size="13" />Propose undo (new reviewed plan)
              </button>
            </article>
            <template v-if="group.id === 'pending'">
              <section v-if="latestRecord && (rows(latestRecord).length || latestRecord.reasoning)" class="details-section" aria-labelledby="details-steps">
                <h3 id="details-steps" class="details-heading">Steps</h3>
                <ThinkingDisclosure
                  full
                  open
                  body-id="drawer-thinking"
                  :record="latestRecord"
                  :unavailable="activityStore.state.supported === false"
                  :reduced="state.settings.reduced_motion"
                  @expand="loadActivity"
                ><template v-if="workView" #body
                  ><WorkView
                    full
                    :record="latestRecord"
                    :task="taskBefore()"
                    :unavailable="activityStore.state.supported === false"
                    :reduced="state.settings.reduced_motion"
                    @expand="loadActivity" /></template
                ></ThinkingDisclosure>
              </section>
              <div v-else-if="activity.length" class="activity-log">
                <h3 class="details-heading">Steps</h3>
                <div
                  v-for="(step, i) in activity"
                  :key="i"
                  class="activity-step"
                  :class="step.kind"
                >
                  <PhCheck
                    v-if="step.kind === 'done'"
                    :size="13"
                  /><PhWarningCircle
                    v-else-if="['blocked', 'review'].includes(step.kind)"
                    :size="13"
                  /><span v-else class="step-dot" /><span>{{ step.label }}</span>
                </div>
              </div>
              <RunPanel view="activity" :chat="chat" :memory="state.memory" @refresh="refreshRun" @error="error = $event" />
            </template>
            </template>
            <p class="details-footnote">
              <PhShieldCheck :size="13" />External content can't approve actions.
            </p>
          </template>
          <template #task>
            <section class="details-section" aria-labelledby="details-goal">
              <h3 id="details-goal" class="details-heading">Goal</h3>
              <GoalPanel :chat-id="chat.id" :running="isWorking"
                :refresh-key="goalRevision + ':' + (chat.runs || []).map((r) => r.id + ':' + r.state).join() + ':' + (chat.questions || []).length + ':' + pending.length"
                @reviews="showReviews()" />
            </section>
            <section v-if="handoffFor && handoffFor.chatId === chat.id && state.model.profile_id && !state.active" class="details-section" aria-labelledby="details-handoff">
              <h3 id="details-handoff" class="details-heading">Continue with the new model</h3>
              <ModelHandoff :chat-id="chat.id" :run-id="handoffFor.runId" :profile-id="state.model.profile_id"
                :release="handoffFor.release"
                @started="(_run: string, chatId: string) => handoffStarted(chatId)" @error="error = $event" />
              <button type="button" class="text-button" @click="handoffFor = null">Not now</button>
            </section>
            <section v-if="chat.runs?.length" class="details-section" aria-labelledby="details-plan">
              <h3 id="details-plan" class="details-heading">Plan and agents</h3>
              <RunPanel view="task" :chat="chat" :memory="state.memory" @refresh="refreshRun" @error="error = $event" />
            </section>
            <section v-if="chat.runs?.length" class="details-section" aria-labelledby="details-sources">
              <h3 id="details-sources" class="details-heading">Sources</h3>
              <ResearchSources :chat-id="chat.id" :run-id="chat.runs[0].id" :refresh-key="chat.runs[0].state" />
            </section>
            <section v-if="exportTarget" class="details-section" aria-labelledby="details-export">
              <h3 id="details-export" class="details-heading">Export a view</h3>
              <label v-if="(chat.artifacts?.length || 0) > 1" class="details-field"
                >View<select :value="exportTarget.id" @change="selectedArtifact = ($event.target as HTMLSelectElement).value">
                  <option v-for="a in chat.artifacts" :key="a.id" :value="a.id">{{ a.title }}</option>
                </select></label
              >
              <ExportPanel :key="exportTarget.id" :chat-id="chat.id" :source="{ kind: 'artifact', id: exportTarget.id, revision: exportTarget.revision, title: exportTarget.title, artifactKind: exportTarget.kind }" @error="error = $event" />
            </section>
          </template>
          <template #context>
            <RunPanel view="context" :chat="chat" :memory="state.memory" @refresh="refreshRun" @error="error = $event" />
          </template>
          <template #changes>
            <section v-if="chat.runs?.length" class="details-section" aria-labelledby="details-rewind">
              <h3 id="details-rewind" class="details-heading">Files from the latest task</h3>
              <RunPanel view="changes" :chat="chat" :memory="state.memory" @refresh="refreshRun" @error="error = $event" />
            </section>
            <CodingWorkflows :chat-id="chat.id" :workspace="chat.workspace" :refresh-key="workflowRevision + ':' + chat.actions.map((a) => a.id + ':' + a.status).join()" />
          </template>
          <template #tools>
            <section v-if="state.job_notifications?.length" class="details-section" aria-labelledby="details-notices">
              <h3 id="details-notices" class="details-heading">Scheduled task notifications</h3>
              <div class="details-notices">
                <button v-for="notice in state.job_notifications.slice(0, 5)" :key="notice.id" class="secondary-button" @click="loadChat(notice.chat_id)">{{ notice.title }} · Open task</button>
              </div>
            </section>
            <CompanionWorkflows :chat-id="chat.id" :workspace="chat.workspace" :refresh-key="workflowRevision + ':' + chat.runs?.map(r => r.id + ':' + r.state).join()" />
            <ScreenSessions :chat-id="chat.id" :apps="screenApps" :refresh-key="screenRevision + ':' + chat.runs?.map(r => r.id + ':' + r.state).join()" />
          </template>
          <template #browser>
            <PebbleBrowser v-if="chat" :chat-id="chat.id" :refresh-key="screenRevision" @changed="screenRevision++" />
          </template>
        </DetailsDrawer>
      </div>
    </div>

    <CommandPalette v-if="palette && !modal" surface="main" :platform="state.platform" :running="isWorking"
      :extras="paletteExtras" @close="palette = false" @choose="chooseCommand" @action="runShortcut" @run="runLauncher" />
    <!-- Phase 24 (V1-118): the polite region is always mounted, so each new toast is a change it announces. -->
    <div class="toast-region" role="status" aria-live="polite">
      <Transition name="toast"
        ><div v-if="toast" class="toast" @mouseenter="holdToast(true)" @mouseleave="holdToast(false)">
          <PhCheck :size="15" aria-hidden="true" /><span>{{ toast.text }}</span
          ><button v-if="toast.action" type="button" class="toast-action" @click="runToast">{{ toast.action.label }}</button>
        </div></Transition
      >
    </div>
    <div v-if="dropHover" class="attachment-drop-target" role="status">
      <PhPaperclip :size="32" /><strong
        >Drop files into this conversation</strong
      ><span>Any file type · up to 10 files · 50 MB each</span>
    </div>
    <!-- Phase 45 (F012): a click beside Settings no longer closes it (a half-filled form was lost); X and Esc do. -->
    <div v-if="modal" class="modal-backdrop" @click.self="modal !== 'settings' && (modal = null)">
      <section
        class="modal"
        :class="{ 'settings-modal': modal === 'settings' }"
        role="dialog"
        aria-modal="true"
        aria-labelledby="modal-heading"
      >
        <div class="modal-title">
          <h2 id="modal-heading">
            {{
              modal === "delete"
                ? deleteHeading
                : modal === "settings"
                  ? "Settings"
                  : modal === "apps"
                    ? "Open an app"
                    : modal === "terminal"
                      ? "Run a terminal command"
                      : "Read a web page"
            }}
          </h2>
          <button
            class="icon-button"
            aria-label="Close dialog"
            @click="modal = null"
          >
            <PhX :size="19" />
          </button>
        </div>
        <template v-if="modal === 'delete'">
          <p class="modal-subtitle">
            “{{ deleteTarget?.title }}” moves to Recently deleted, where you can
            restore it for 30 days. After that it is deleted with its messages,
            attachments and review history. To delete it at once, use Delete
            now in Settings → Data &amp; storage.
          </p>
          <div class="setting-actions delete-actions">
            <button class="secondary-button" @click="modal = null">
              Cancel
            </button>
            <button
              class="primary-button danger"
              :disabled="!!state.active"
              :aria-label="deleteTarget?.title ? `Move “${deleteTarget.title}” to Recently deleted` : undefined"
              @click="trashChat"
            >
              Move to Recently deleted
            </button>
          </div>
        </template>
        <div v-else-if="modal === 'settings'" class="settings-layout">
          <!-- Phase 45 (F006, D129): the sections below have their own tabs (Privacy & Stealth, Appearance, General)
               instead of trailing under every tab; the tabs are a sidebar in the workspace window. -->
          <AdvancedSettings
            :state="state"
            :chat-id="chat?.id"
            :initial-tab="settingsTab"
            :intent="settingsIntent"
            @refresh="refresh"
            @tab="settingsTab = $event"
          />
          <!-- Phase 33 (1.1.0, D45-D47, D57): Stealth Lockdown, one switch for both halves, with the honest note. -->
          <div v-if="settingsTab === 'privacy'" class="settings-section">
            <div class="section-label">PRIVACY AND FOCUS</div>
            <div class="stealth-card">
              <button
                type="button"
                class="setting-row"
                role="switch"
                :aria-checked="state.settings.stealth ? 'true' : 'false'"
                aria-describedby="stealth-desc stealth-honest"
                @click="stealth"
              >
                <div>
                  <strong>Stealth Lockdown</strong
                  ><small id="stealth-desc">Keeps Pebble out of screen shares and recordings, and keeps the companion with you over the app you're in.</small>
                </div>
                <span class="toggle stealth" aria-hidden="true" :class="{ on: state.settings.stealth }" />
              </button>
              <p class="stealth-state" :class="{ on: state.settings.stealth }" role="status">
                {{ state.settings.stealth ? STEALTH_ON_TEXT : STEALTH_OFF_TEXT }}
              </p>
            </div>
            <p id="stealth-honest" class="stealth-honest">{{ STEALTH_HONEST }}</p>
            <dl class="stealth-halves">
              <div><dt>Stealth</dt><dd>Hidden from screen capture and screen sharing. No window shadow. Leaves the Dock, ⌘Tab and Mission Control. ⌃⌥⌘H hides Pebble at once; press it again, or use the menu-bar icon, to bring it back.</dd></div>
              <div><dt>Lockdown</dt><dd>The companion stays over the app you're in, full-screen apps included. Clicking or typing in the companion leaves the app you were in active.</dd></div>
            </dl>
          </div>
          <div v-if="settingsTab === 'privacy'" class="settings-section">
            <div class="section-label">PRIVACY & CONTROL</div>
            <div class="privacy-row">
              <PhShieldCheck :size="19" aria-hidden="true" />
              <!-- Phase 24 (V1-065, V1-144): what leaves this Mac, and what protects it, said plainly. -->
              <p>
                Your conversations and memories are stored on this Mac. A model
                on this Mac answers here; a cloud model such as ChatGPT receives
                each request, with the recent conversation and the memories
                Pebble recalls for it. Web tools fetch public pages. Permissions
                and your approvals decide what Pebble may do; AgentGuard is a
                reliability check, not a security boundary.
              </p>
            </div>
            <p v-if="state.guard.error" class="guard-error">
              AgentGuard: {{ state.guard.error }}
            </p>
            <p class="setting-note">
              Data: {{ state.data_dir || "Available in the desktop app" }}
            </p>
            <!-- Phase 45 (F099, audit round R3): Settings held "Delete this conversation", which acted on whatever
                 conversation was behind the dialog; each conversation's own row in the sidebar has Delete. -->
          </div>
          <div v-if="settingsTab === 'privacy'" class="settings-section">
            <div class="section-label">USAGE TELEMETRY</div>
            <TelemetrySettings :choose-destination="chooseTelemetryDestination" />
          </div>
          <div v-if="settingsTab === 'appearance'" class="settings-section">
            <div id="appearance-label" class="section-label">APPEARANCE</div>
            <!-- Phase 24 (V1-041): the theme is a set of pressed buttons; every switch is a role="switch" with
                 aria-checked, and its drawn toggle is hidden from assistive technology. -->
            <div class="theme-options" role="group" aria-labelledby="appearance-label">
              <button
                type="button"
                :class="{ selected: state.settings.theme === 'light' }"
                :aria-pressed="state.settings.theme === 'light' ? 'true' : 'false'"
                @click="theme('light')"
              >
                <PhSun :size="19" aria-hidden="true" />Light</button
              ><button
                type="button"
                :class="{ selected: state.settings.theme === 'dark' }"
                :aria-pressed="state.settings.theme === 'dark' ? 'true' : 'false'"
                @click="theme('dark')"
              >
                <PhMoon :size="19" aria-hidden="true" />Dark</button
              ><button
                type="button"
                :class="{ selected: state.settings.theme === 'system' }"
                :aria-pressed="state.settings.theme === 'system' ? 'true' : 'false'"
                @click="theme('system')"
              >
                <PhMonitor :size="19" aria-hidden="true" />System
              </button>
            </div>
            <button type="button" class="setting-row" role="switch" :aria-checked="state.settings.reduced_motion ? 'true' : 'false'" @click="motion">
              <div>
                <strong>Reduce motion</strong
                ><small>Keep the companion still</small>
              </div>
              <span
                class="toggle"
                aria-hidden="true"
                :class="{ on: state.settings.reduced_motion }"
              />
            </button>
            <button type="button" class="setting-row" role="switch" :aria-checked="workView ? 'true' : 'false'" @click="toggleWorkView">
              <div>
                <strong>Work view</strong
                ><small>Show what Pebble is doing as cards, not rows of text</small>
              </div>
              <span class="toggle" aria-hidden="true" :class="{ on: workView }" />
            </button>
            <label class="setting-row">
              <div>
                <strong>Visual answers</strong
                ><small>{{ visualAnswers === "auto" ? "Charts and diagrams when you ask, and when a request suits one (slower and costs more)" : "Charts and diagrams when you ask for one" }}. Tables and diagrams in a reply are always drawn.</small>
              </div>
              <select :value="visualAnswers" aria-label="Visual answers" @change="changeVisualAnswers(($event.target as HTMLSelectElement).value)">
                <option value="ask">When I ask</option>
                <option value="auto">Also when they help</option>
              </select>
            </label>
            <!-- Phase 19 companion switches: presentation only; reduced motion still wins over pointer and reactions. -->
            <!-- Phase 45 (F039): Reduce motion wins, so these two read off and wait while it is on. -->
            <button type="button" class="setting-row" role="switch" :disabled="state.settings.reduced_motion" :aria-checked="companionSettings.pointer && !state.settings.reduced_motion ? 'true' : 'false'" @click="changeCompanion({ pointer: !companionSettings.pointer })">
              <div>
                <strong>Follow the pointer</strong
                ><small>{{ state.settings.reduced_motion ? "Off while Reduce motion is on" : "The companion's eyes track your pointer" }}</small>
              </div>
              <span class="toggle" aria-hidden="true" :class="{ on: companionSettings.pointer && !state.settings.reduced_motion }" />
            </button>
            <button type="button" class="setting-row" role="switch" :disabled="state.settings.reduced_motion" :aria-checked="companionSettings.reactions && !state.settings.reduced_motion ? 'true' : 'false'" @click="changeCompanion({ reactions: !companionSettings.reactions })">
              <div>
                <strong>Expressive reactions</strong
                ><small>{{ state.settings.reduced_motion ? "Off while Reduce motion is on; task states always show" : "Celebrations, taps and drowsiness; task states always show" }}</small>
              </div>
              <span class="toggle" aria-hidden="true" :class="{ on: companionSettings.reactions && !state.settings.reduced_motion }" />
            </button>
            <label class="setting-row">
              <div>
                <strong>Status words</strong
                ><small>What Pebble is doing, from its own activity records</small>
              </div>
              <select
                :value="companionSettings.status_words"
                aria-label="Status words"
                @change="changeCompanion({ status_words: ($event.target as HTMLSelectElement).value as CompanionSettings['status_words'] })"
              >
                <option value="rotate">Varied</option>
                <option value="fixed">One word</option>
                <option value="off">Hidden</option>
              </select>
            </label>
            <button type="button" class="setting-row" role="switch" :aria-checked="companionSettings.ui_guidance ? 'true' : 'false'" @click="changeCompanion({ ui_guidance: !companionSettings.ui_guidance })">
              <div>
                <strong>UI design guidance</strong
                ><small>Offer Pebble's built-in view guidance to larger models when you ask for a view</small>
              </div>
              <span class="toggle" aria-hidden="true" :class="{ on: companionSettings.ui_guidance }" />
            </button>
          </div>
          <div v-if="settingsTab === 'general'" class="settings-section">
            <div class="section-label">STARTUP AND NOTIFICATIONS</div>
            <NotificationSettings />
          </div>
          <div v-if="settingsTab === 'general'" id="settings-keyboard" class="settings-section" tabindex="-1" aria-label="Keyboard shortcuts">
            <div class="section-label">KEYBOARD</div>
            <table class="shortcut-table">
              <caption class="setting-note">Keys marked Any app work from other apps; the rest work in Pebble's windows.</caption>
              <tbody>
                <tr v-for="item in shortcutsFor('main')" :key="item.id">
                  <th scope="row">{{ item.label }}</th>
                  <td><kbd>{{ keysLabel(item, state.platform) }}</kbd></td>
                  <td>{{ item.scope === 'global' ? 'Any app' : item.surfaces.includes('pet') ? 'Window and companion' : 'This window' }}</td>
                </tr>
              </tbody>
            </table>
          </div>
          <div v-if="settingsTab === 'general'" class="about-pebble">
            <Avatar :size="38" :reduced="true" :stealth="!!state.settings.stealth" />
            <div>
              <strong>Pebble <span v-if="state.version">{{ state.version }}</span></strong
              ><small>Reliability: AgentGuard (Apache 2.0).</small>
            </div>
            <!-- 2.0.0 (D222): the avatar engine's credit, in smaller type in the panel's corner. -->
            <small class="about-credit">Avatar engine: bloub by Jérémy Perret (MIT)</small>
          </div>
          <!-- Phase 24 (V1-135): release notes in the app, an excerpt of CHANGELOG.md bundled with this build. -->
          <details v-if="settingsTab === 'general'" class="whats-new">
            <summary>What's new in {{ WHATS_NEW_VERSION }}</summary>
            <ul>
              <li v-for="line in WHATS_NEW" :key="line">{{ line }}</li>
            </ul>
          </details>
        </div>
        <template v-else-if="modal === 'terminal'"
          ><p class="modal-subtitle">
            Run one command in {{ projectName || "a connected project" }}.
            Review the exact command and access before execution.
          </p>
          <form @submit.prevent="proposeCommand">
            <textarea
              v-model="terminalCommand"
              class="terminal-input"
              rows="4"
              aria-label="Terminal command"
              placeholder="For example: git status --short"
              required
            /><label class="terminal-network"
              ><input v-model="terminalNetwork" type="checkbox" />Request
              network access for this command</label
            >
            <p class="setting-note">
              Pipes, redirections, privileged commands and credential access are
              blocked. Commands run for up to 60 seconds inside a macOS sandbox,
              and the output appears in the conversation.
            </p>
            <button
              class="primary-button"
              :disabled="
                !chat?.workspace || !!state.active || !terminalCommand.trim()
              "
            >
              Review command
            </button>
          </form>
          <button
            v-if="!chat?.workspace"
            class="secondary-button connect-project"
            @click="chooseProject"
          >
            Connect a project
          </button></template
        >
        <template v-else-if="modal === 'apps'"
          ><p class="modal-subtitle">
            Select an installed app. You'll review the launch next.
          </p>
          <div class="search-input">
            <PhMagnifyingGlass :size="18" /><input
              v-model="appSearch"
              placeholder="Find an app…"
              aria-label="Search installed apps"
              autofocus
            />
          </div>
          <div class="apps-grid">
            <button
              v-for="app in filteredApps"
              :key="app.id"
              @click="proposeApp(app)"
            >
              <div class="app-monogram">{{ app.name[0] }}</div>
              <span>{{ app.name }}</span
              ><PhArrowRight :size="14" />
            </button>
            <p v-if="!filteredApps.length" class="no-history">
              {{
                native
                  ? "No matching installed apps."
                  : "App discovery is available in the desktop app."
              }}
            </p>
          </div></template
        >
        <template v-else
          ><p class="modal-subtitle">
            Read a public HTTPS page as source material. Its content gets no
            tool permissions.
          </p>
          <form @submit.prevent="attachUrl">
            <input
              class="url-input"
              v-model="url"
              type="url"
              required
              placeholder="https://docs.example.com/guide"
              aria-label="Public HTTPS source URL"
              autofocus
            />
            <p class="setting-note">
              Text and HTML only. Private networks, redirects, and scripts are
              blocked.
            </p>
            <button class="primary-button" :disabled="attachmentBusy || !url">
              <PhCircleNotch v-if="attachmentBusy" class="spin" :size="14" />{{
                attachmentBusy ? "Reading…" : "Attach source"
              }}
            </button>
          </form></template
        >
        <div v-if="error" class="modal-error" role="alert">{{ error }}</div>
      </section>
    </div>
  </div>
</template>
