<script setup lang="ts">
import { computed, nextTick, reactive, ref, onMounted, onUnmounted, watch } from "vue";
import ModsSettings from "./ModsSettings.vue";
import MemoryContests from "./MemoryContests.vue";
import MemoryArchive from "./MemoryArchive.vue";
import ConnectorRouteSettings from "./ConnectorRouteSettings.vue";
import StorageSettings from "./StorageSettings.vue";
import {
  PhPlus,
  PhTrash,
  PhShieldCheck,
  PhCpu,
  PhBrain,
  PhPlugs,
  PhArrowClockwise,
  PhFolder,
  PhSlidersHorizontal,
} from "@phosphor-icons/vue";
import { rpc, selectFile } from "../bridge";
import { EVERY_MODE, MODES, MODE_IDS } from "../permissionModes";
import type { State, Permissions, ModelProfile } from "../types";
const props = defineProps<{
  state: State;
  chatId?: string;
  initialTab?: string;
}>();
const emit = defineEmits<{ refresh: [] }>();
const tab = ref(props.initialTab || "models"),
  error = ref(""),
  busy = ref(false),
  adding = ref(false),
  fetched = ref<{ id: string; name: string; context?: number }[]>([]);
// Phase 24 (V1-107, D7): Views (mods) are a developer feature. The switch is this window's own preference; it
// hides the tab only, and the engine, its review and its revocation controls are unchanged.
const DEVELOPER_KEY = "pebble.settings.developer";
function windowStorage(): Storage | null {
  try {
    return typeof window !== "undefined" ? window.localStorage : null;
  } catch {
    return null;
  }
}
function readDeveloper() {
  try {
    return windowStorage()?.getItem(DEVELOPER_KEY) === "on";
  } catch {
    return false;
  }
}
const developer = ref(readDeveloper());
async function toggleDeveloper() {
  developer.value = !developer.value;
  try {
    if (developer.value) windowStorage()?.setItem(DEVELOPER_KEY, "on");
    else windowStorage()?.removeItem(DEVELOPER_KEY);
  } catch {
    /* a refusing storage keeps the switch for this window only */
  }
  if (!developer.value && tab.value === "mods") await changeTab("extensions");
}
// One label per tab (V1-108: three user nouns, Skills, Connectors and Views). Tab ids are unchanged.
const TAB_LABELS: Record<string, string> = {
  models: "Models",
  permissions: "Permissions",
  memory: "Memory",
  extensions: "Skills & connectors",
  mods: "Views",
  storage: "Data & storage",
};
const tabs = computed(() => [
  "models",
  "permissions",
  "memory",
  "extensions",
  ...(developer.value || tab.value === "mods" ? ["mods"] : []),
  "storage",
]);
const form = ref({
  name: "",
  url: "",
  model: "",
  kind: "openai",
  api_key: "",
  context_mode: "full",
  context: 8192,
  budget: 8192,
  tools: true,
  profile_id: undefined as string | undefined,
});
const selectedModels = ref<string[]>([]),
  modelSearch = ref("");
const visibleModels = computed(() =>
  fetched.value.filter((m) =>
    (m.name + " " + m.id)
      .toLowerCase()
      .includes(modelSearch.value.toLowerCase()),
  ),
);
const connectionRows = computed(() =>
  (props.state.profiles || [])
    .filter(
      (p, i, all) =>
        p.kind === "gguf" ||
        all.findIndex(
          (x) => (x.connection_id || x.id) === (p.connection_id || p.id),
        ) === i,
    )
    .map(
      (p) =>
        (props.state.profiles || []).find(
          (x) =>
            x.id === props.state.model.profile_id &&
            (x.connection_id || x.id) === (p.connection_id || p.id),
        ) || p,
    ),
);
const connectionCount = (p: ModelProfile) =>
  (props.state.profiles || []).filter(
    (x) => (x.connection_id || x.id) === (p.connection_id || p.id),
  ).length;

// ---- ChatGPT sign-in (Phase 24, V1-001) ----
// The person sees "Opening ChatGPT sign-in…" at once. A refusal from chatgpt_start carries t24-auth's content-free
// SignInError code (bridge RpcError.code) and is shown by the button that started it, with the action that helps:
// Try again, Continue with ChatGPT for an ended sign-in, or nothing when trying again cannot help. The sign-in link
// can be copied when the browser does not open. Up to two failed status reads in a row are tolerated; the third
// stops the wait. A finished sign-in returns to the model list (the provider form stays closed).
type SignInPhase = "idle" | "opening" | "waiting" | "validating" | "connected" | "failed" | "cancelled";
type SignInAction = "retry" | "reconnect" | "none";
const SIGNIN_POLL_MS = 1500;
const SIGNIN_POLL_TOLERANCE = 3;
const SIGNIN_MESSAGE_ONLY = new Set(["tls", "http_403", "redirect"]);
const signin = reactive({
  phase: "idle" as SignInPhase,
  message: "",
  code: "",
  url: "",
  profile: "",
  action: "none" as SignInAction,
  copied: false,
});
const highlight = ref("");
const signingIn = computed(() => ["opening", "waiting", "validating"].includes(signin.phase));
let authTimer: ReturnType<typeof setTimeout> | undefined,
  signInVersion = 0,
  pollFailures = 0,
  alive = true;
onUnmounted(() => {
  alive = false;
  signInVersion++;
  clearTimeout(authTimer);
});
function errorCode(cause: unknown) {
  const code = (cause as { code?: unknown } | null)?.code;
  return typeof code === "string" && /^[a-z][a-z0-9_]{0,63}$/.test(code) ? code : "";
}
function errorText(cause: unknown) {
  const text = cause instanceof Error ? cause.message : String(cause);
  return (text || "Sign-in failed.").slice(0, 600);
}
function signInAction(code: string): SignInAction {
  if (code === "reconnect_required") return "reconnect";
  return SIGNIN_MESSAGE_ONLY.has(code) ? "none" : "retry";
}
function signInFailed(cause: unknown) {
  const code = errorCode(cause);
  Object.assign(signin, { phase: "failed", code, message: errorText(cause), action: signInAction(code), url: "" });
}
async function signIn(profile_id?: string) {
  if (signingIn.value) return;
  const version = ++signInVersion;
  clearTimeout(authTimer);
  pollFailures = 0;
  highlight.value = "";
  error.value = "";
  Object.assign(signin, {
    phase: "opening",
    message: "Opening ChatGPT sign-in…",
    code: "",
    url: "",
    profile: profile_id || "",
    action: "none",
    copied: false,
  });
  let auth: { url?: unknown };
  try {
    auth = await rpc("chatgpt_start", { profile_id });
  } catch (cause) {
    if (alive && version === signInVersion) signInFailed(cause);
    return;
  }
  if (!alive || version !== signInVersion) return;
  const url = typeof auth?.url === "string" && auth.url.startsWith("https://") ? auth.url : "";
  if (!url) {
    signInFailed(new Error("Pebble did not receive a sign-in link from OpenAI. Try again; your current sign-in is unchanged."));
    return;
  }
  Object.assign(signin, {
    phase: "waiting",
    url,
    message: "Finish signing in and allowing plan use in your browser, then come back here.",
  });
  try {
    await rpc("open_url", { url });
  } catch {
    if (alive && version === signInVersion)
      signin.message = "Your browser did not open. Copy the sign-in link below and open it in your browser.";
  }
  if (alive && version === signInVersion) schedulePoll(version);
}
function schedulePoll(version: number) {
  clearTimeout(authTimer);
  authTimer = setTimeout(() => void pollSignIn(version), SIGNIN_POLL_MS);
}
async function pollSignIn(version: number) {
  if (!alive || version !== signInVersion) return;
  let status: { state?: string; error?: unknown; profile_id?: unknown; email?: unknown };
  try {
    status = await rpc("chatgpt_status");
    pollFailures = 0;
  } catch {
    if (!alive || version !== signInVersion) return;
    if (++pollFailures >= SIGNIN_POLL_TOLERANCE) {
      Object.assign(signin, {
        phase: "failed",
        code: "",
        action: "retry",
        message: "Pebble lost track of this sign-in. Try again; your current sign-in is unchanged.",
      });
      return;
    }
    schedulePoll(version);
    return;
  }
  if (!alive || version !== signInVersion) return;
  if (status?.state === "waiting" || status?.state === "validating") {
    if (status.state === "validating") Object.assign(signin, { phase: "validating", message: "Checking your ChatGPT account…" });
    schedulePoll(version);
    return;
  }
  if (status?.state === "connected") {
    const email = typeof status.email === "string" ? status.email.slice(0, 120) : "";
    adding.value = false;
    fetched.value = [];
    highlight.value = typeof status.profile_id === "string" ? status.profile_id : "";
    Object.assign(signin, {
      phase: "connected",
      code: "",
      url: "",
      action: "none",
      message: "Connected to ChatGPT" + (email ? " as " + email : "") + ". This account's models are listed under Your models.",
    });
  } else if (status?.state === "failed") {
    Object.assign(signin, {
      phase: "failed",
      code: "",
      url: "",
      action: "retry",
      message: typeof status.error === "string" && status.error ? status.error.slice(0, 600) : "Sign-in did not finish. Try again.",
    });
  } else {
    Object.assign(signin, { phase: "cancelled", code: "", url: "", action: "none", message: "Sign-in cancelled." });
  }
  emit("refresh");
}
async function cancelSignIn() {
  signInVersion++;
  clearTimeout(authTimer);
  Object.assign(signin, { phase: "idle", message: "", code: "", url: "", action: "none", copied: false });
  try {
    await rpc("chatgpt_cancel");
  } catch (cause) {
    signInFailed(cause);
  }
}
async function copySignInLink() {
  try {
    await navigator.clipboard.writeText(signin.url);
    signin.copied = true;
  } catch {
    signin.copied = false;
    signin.message = "Copying did not work. Select the link below and copy it.";
  }
}
async function signOut(id: string) {
  await run(async () => {
    const result = await rpc("chatgpt_sign_out", { profile_id: id });
    Object.assign(signin, { phase: "idle", code: "", url: "", action: "none", profile: id, message: String(result?.message || "") });
  });
}
function editModel(p: NonNullable<State["profiles"]>[number]) {
  form.value = {
    name: p.provider_name || p.name,
    url: p.url || "",
    model: p.model || "",
    kind: p.kind,
    api_key: "",
    context_mode: p.context_mode || "full",
    context: p.context,
    budget: p.budget || p.context,
    tools: p.tools,
    profile_id: p.id,
  };
  fetched.value = [];
  selectedModels.value = (props.state.profiles || [])
    .filter((x) => (x.connection_id || x.id) === (p.connection_id || p.id))
    .map((x) => x.model || "");
  adding.value = true;
}
function newProvider() {
  form.value = {
    name: "",
    url: "",
    model: "",
    kind: "openai",
    api_key: "",
    context_mode: "full",
    context: 8192,
    budget: 8192,
    tools: true,
    profile_id: undefined,
  };
  fetched.value = [];
  selectedModels.value = [];
  modelSearch.value = "";
  adding.value = true;
}
const type = ref("profile"),
  memories = ref<any[]>([]),
  memoryCandidates = ref<any[]>([]),
  memoryContent = ref(""),
  memoryScope = ref("global"),
  expiry = ref(""),
  clearConfirm = ref(false);
const toolLabels: Record<string, string> = {
  files: "Read project files",
  edits: "Edit project files",
  tests: "Run isolated tests",
  apps: "Open installed apps",
  web: "Search and read the web",
  terminal: "Use terminal commands",
  plugins: "Use connectors you enabled",
  views: "Create interactive views",
  screen: "Control granted app windows and browser tabs",
};
// V1-113 (D9): where a web search goes, said where the person turns it on.
const WEB_DESTINATION =
  "Searches send your search words to Microsoft Bing (its RSS results feed). Pages Pebble reads are fetched from their own sites.";
const disabled = computed(() => !!props.state.active);
async function run(fn: () => Promise<void>) {
  error.value = "";
  busy.value = true;
  try {
    await fn();
    emit("refresh");
  } catch (e) {
    error.value = String(e);
  } finally {
    busy.value = false;
  }
}
async function addLocal() {
  await run(async () => {
    const path = await selectFile(false, true);
    if (path) await rpc("set_model", { path });
  });
}
async function selectModel(id: string) {
  await run(async () => {
    await rpc("model_select", { profile_id: id });
  });
}
async function removeModel(id: string) {
  await run(async () => {
    await rpc("model_remove", { profile_id: id });
  });
}
// Phase 24 (parent): a failed model fetch is shown by its button, where the person is looking, not only at the top of
// Settings (which is scrolled out of view while the provider form is open). Editing the URL, format or key clears it.
const discoverError = ref("");
watch(() => [form.value.url, form.value.kind, form.value.api_key], () => { discoverError.value = ""; });
async function discover() {
  discoverError.value = "";
  await run(async () => {
    fetched.value = await rpc("model_discover", {
      url: form.value.url,
      kind: form.value.kind,
      api_key: form.value.api_key,
      profile_id: form.value.profile_id,
    });
    if (fetched.value.length && !form.value.model)
      form.value.model = fetched.value[0].id;
    selectedModels.value = selectedModels.value.filter((id) =>
      fetched.value.some((m) => m.id === id),
    );
    if (!fetched.value.some((m) => m.id === form.value.model))
      form.value.model = fetched.value[0]?.id || "";
    if (!selectedModels.value.length && fetched.value[0])
      selectedModels.value = [form.value.model];
    picked();
  });
  if (error.value) {
    discoverError.value = error.value;
    error.value = "";
  }
}
function autoDiscover() {
  if (
    !busy.value &&
    !fetched.value.length &&
    form.value.url &&
    (form.value.api_key ||
      /^http:\/\/(localhost|127\.0\.0\.1)(:|\/|$)/.test(form.value.url))
  )
    discover();
}
function picked() {
  const m = fetched.value.find((m) => m.id === form.value.model);
  if (
    m?.context &&
    Number.isSafeInteger(m.context) &&
    m.context >= 4096 &&
    m.context <= 10000000
  ) {
    form.value.context = m.context;
    if (form.value.context_mode === "full") form.value.budget = m.context;
  }
}
async function saveModel() {
  await run(async () => {
    await rpc("model_save_many", {
      name: form.value.name,
      url: form.value.url,
      kind: form.value.kind,
      api_key: form.value.api_key,
      profile_id: form.value.profile_id,
      models: fetched.value.length ? selectedModels.value : [form.value.model],
      context: Number(form.value.context),
      context_mode: form.value.context_mode,
      budget: Number(form.value.budget),
      tools: form.value.tools,
    });
    form.value.api_key = "";
    adding.value = false;
    fetched.value = [];
  });
}
async function permission(mode?: Permissions["mode"], tool?: string) {
  await run(async () => {
    const p = JSON.parse(JSON.stringify(props.state.settings.permissions));
    if (mode) p.mode = mode;
    if (tool) {
      const key = tool as keyof Permissions["tools"];
      p.tools[key] = !p.tools[key];
    }
    await rpc("setting", { key: "permissions", value: p });
  });
}
// V1-041: arrow keys move a radio group's or tab list's choice (roving focus); Home and End jump to the ends.
function step(event: KeyboardEvent, ids: readonly string[], current: string) {
  const at = Math.max(0, ids.indexOf(current));
  const moves: Record<string, number> = { ArrowRight: at + 1, ArrowDown: at + 1, ArrowLeft: at - 1, ArrowUp: at - 1, Home: 0, End: ids.length - 1 };
  const next = moves[event.key];
  if (next === undefined || !ids.length) return null;
  event.preventDefault();
  return ids[(next + ids.length) % ids.length];
}
function focusId(id: string) {
  void nextTick(() => {
    if (typeof document !== "undefined") document.getElementById?.(id)?.focus?.();
  });
}
async function modeKey(event: KeyboardEvent) {
  const next = step(event, MODE_IDS, props.state.settings.permissions.mode);
  if (!next || next === props.state.settings.permissions.mode || disabled.value || busy.value) return;
  focusId("permission-mode-" + next);
  await permission(next as Permissions["mode"]);
}
async function tabKey(event: KeyboardEvent) {
  const next = step(event, tabs.value, tab.value);
  if (!next) return;
  focusId("settings-tab-" + next);
  await changeTab(next);
}
async function layerKey(event: KeyboardEvent) {
  const next = step(event, (props.state.memory?.types || []).map((k) => k.id), type.value);
  if (!next) return;
  focusId("memory-layer-" + next);
  await getMemories(next);
}
function selectAll(event: Event) {
  (event.target as HTMLInputElement | null)?.select?.();
}

// ---- Memory (V1-099, V1-100, V1-146) ----
const MEMORY_PAGE = 100;
const memoryQuery = ref(""),
  memorySearched = ref(""),
  memoryMore = ref(false);
let memoryRequest = 0;
/** One page of a layer through memory_list(kind, query, offset, limit) (t24-memory). The working layer is the
 * newest task checkpoints only, filtered here. More pages exist while a page comes back full. */
async function listMemories(kind = type.value, more = false) {
  const request = ++memoryRequest,
    query = memoryQuery.value.trim().slice(0, 200),
    offset = more ? memories.value.length : 0;
  const page = await rpc(
    "memory_list",
    kind === "working" ? { kind } : { kind, ...(query ? { query } : {}), offset, limit: MEMORY_PAGE },
  );
  if (request !== memoryRequest || kind !== type.value) return;
  const rows = Array.isArray(page) ? page : [];
  const needle = query.toLowerCase();
  const shown = kind === "working" && needle ? rows.filter((m) => memoryText(m).toLowerCase().includes(needle)) : rows;
  memories.value = more ? [...memories.value, ...shown] : shown;
  memoryMore.value = kind !== "working" && rows.length === MEMORY_PAGE;
  memorySearched.value = query;
}
async function getMemories(kind = type.value) {
  if (kind !== type.value) memoryQuery.value = "";
  type.value = kind;
  await run(async () => {
    await listMemories(kind);
    memoryCandidates.value = await rpc("memory_candidates", { id: props.chatId });
  });
}
async function searchMemories() {
  await run(async () => {
    await listMemories(type.value);
  });
}
async function clearMemorySearch() {
  memoryQuery.value = "";
  await searchMemories();
}
async function loadMoreMemories() {
  await run(async () => {
    await listMemories(type.value, true);
  });
}
async function memoryPower() {
  await run(async () => {
    await rpc("setting", {
      key: "memory_enabled",
      value: !props.state.settings.memory_enabled,
    });
  });
}
async function memoryLayer() {
  await run(async () => {
    const layers = Object.fromEntries((props.state.memory?.types || []).map(k =>
      [k.id, props.state.settings.memory_layers?.[k.id] ?? true]));
    layers[type.value] = !layers[type.value];
    await rpc('setting', { key: 'memory_layers', value: layers });
  });
}
async function resolveMemory(candidate_id: string, accept: boolean) {
  await run(async () => {
    await rpc('memory_resolve', { candidate_id, accept });
    await listMemories(type.value);
    memoryCandidates.value = await rpc('memory_candidates', { id: props.chatId });
  });
}
async function maintainMemory() {
  await run(async () => {
    await rpc('memory_maintain');
    await listMemories(type.value);
    memoryCandidates.value = await rpc('memory_candidates', { id: props.chatId });
  });
}
async function saveMemory() {
  await run(async () => {
    const id = props.chatId;
    await rpc("memory_save", {
      kind: type.value,
      content: memoryContent.value,
      id,
      scope: memoryScope.value,
      expires:
        type.value === "temporal"
          ? new Date(expiry.value).getTime() / 1000
          : null,
    });
    memoryContent.value = "";
    await listMemories(type.value);
  });
}
async function forget(id: string) {
  if (props.state.active) return;
  await run(async () => {
    await rpc("memory_delete", { id, kind: type.value });
    await listMemories(type.value);
    memoryCandidates.value = await rpc('memory_candidates', { id: props.chatId });
  });
}
async function clear() {
  await run(async () => {
    await rpc("memory_clear");
    memories.value = [];
    memoryCandidates.value = [];
    memoryMore.value = false;
    clearConfirm.value = false;
  });
}
async function changeTab(value: string) {
  tab.value = value;
  error.value = "";
  if (value === "memory") await getMemories();
  if (value === "extensions") await loadExtensions();
  if (value === "permissions") await loadPlanCredentials();
}
onMounted(() => {
  if (tab.value === 'memory') getMemories();
  if (tab.value === 'extensions') loadExtensions();
  if (tab.value === 'permissions') loadPlanCredentials();
});
// Command credentials for reviewed typed plans: names here, values only in the OS keychain.
const planCredentialNames = ref<string[]>([]), planCredentialName = ref(''), planCredentialSecret = ref('');
async function loadPlanCredentials() {
  try { planCredentialNames.value = (await rpc<{ names: string[] }>('plan_credentials', {})).names; }
  catch (failure) { error.value = String(failure); }
}
async function savePlanCredential() {
  const name = planCredentialName.value.trim(), secret = planCredentialSecret.value;
  if (!name || !secret) return;
  await run(async () => {
    planCredentialNames.value = (await rpc<{ names: string[] }>('plan_credential_save', { name, secret })).names;
    planCredentialName.value = ''; planCredentialSecret.value = '';
  });
}
async function removePlanCredential(name: string) {
  await run(async () => {
    planCredentialNames.value = (await rpc<{ names: string[] }>('plan_credential_delete', { name })).names;
  });
}
const extensions = ref<any[]>([]),
  extensionPreview = ref<Record<string, any>>({}),
  extensionUrl = ref(""),
  extensionName = ref(""),
  extensionType = ref("skill"),
  extensionEra = ref("legacy"),
  extensionText = ref("");
// V1-102: what a local MCP server must be today, said in the form and in place of the launch refusals.
const LOCAL_SERVER_SCOPE =
  "Local servers must be one self-contained program under 100 MB, or a single Python file run by Pebble. Servers started through npx, uvx or Node are not supported yet; connect to an HTTP MCP endpoint instead.";
const LAUNCH_REFUSAL = /MCP launch grants require|Local MCP requires an absolute command|Shell\/interpreter dispatch wrappers|Credential files cannot be MCP launch grants/;
const extensionNouns: Record<string, string> = { skill: "Skill", hook: "Hook", plugin: "Connector" };
const packageOpen = ref(false), packageText = ref(""), packageTarget = ref(""),
  packageReview = ref<any>(null), packageMessage = ref(""),
  packageInfo = ref<Record<string, any>>({}), packageHistory = ref<Record<string, any[]>>({});
const packageBlocked = computed(() => busy.value || disabled.value || !props.state.settings.permissions.tools.plugins);
function newPackage(id = "") {
  packageOpen.value = true; packageTarget.value = id; packageText.value = "";
  packageReview.value = null; packageMessage.value = "";
}
async function managePackage(id: string) {
  newPackage(id);
  await run(async () => {
    packageInfo.value[id] = await rpc("plugin_package_inspect", { id });
    packageHistory.value[id] = await rpc("plugin_package_history", { id });
  });
}
async function previewPackage() {
  packageReview.value = null;
  await run(async () => {
    packageReview.value = await rpc("plugin_package_preview", {
      bundle: packageText.value, ...(packageTarget.value ? { id: packageTarget.value } : {}),
    });
  });
}
async function importPackage() {
  packageReview.value = null;
  await run(async () => {
    const path = await selectFile();
    if (path) packageReview.value = await rpc("plugin_package_import", {
      path, ...(packageTarget.value ? { id: packageTarget.value } : {}),
    });
  });
}
async function previewPackageRollback(id: string, revision: number) {
  newPackage(id);
  await run(async () => {
    packageReview.value = await rpc("plugin_package_preview_rollback", { id, revision });
  });
}
async function applyPackage() {
  const review = packageReview.value;
  if (!review) return;
  packageReview.value = null;
  await run(async () => {
    const operation = review.operation;
    if (!["install", "update", "rollback"].includes(operation)) throw new Error("Review a supported package operation again.");
    const result = await rpc(`plugin_package_${operation}`, {
      preview_id: review.preview_id, ...(operation === "install" ? {} : { id: review.plugin_id }),
    });
    packageTarget.value = result.id;
    packageText.value = "";
    packageMessage.value = `Revision ${result.revision} installed disabled. Review connector access and enablement separately.`;
    packageInfo.value[result.id] = await rpc("plugin_package_inspect", { id: result.id });
    packageHistory.value[result.id] = await rpc("plugin_package_history", { id: result.id });
    extensions.value = await rpc("extensions"); await refreshConnectorInfo();
  });
}
const connectorInfo = ref<Record<string, any>>({}),
  connectorId = ref(""), connectorMode = ref("oauth"), connectorReview = ref<any>(null),
  connectorDiscovery = ref<any>(null), connectorMessage = ref("");
const connectorForm = ref({ issuer: "", client_id: "", scopes: "read", name: "API_KEY", token: "" });
const connectorRegistrationMethod = ref("pre_registered"),
  connectorClientMetadata = ref(""), connectorRegistrationApproved = ref(false);
let connectorTimer: ReturnType<typeof setInterval> | undefined;
onUnmounted(() => clearInterval(connectorTimer));
async function refreshConnectorInfo() {
  for (const item of extensions.value.filter((x) => x.kind === "plugin")) {
    connectorInfo.value[item.id] = await rpc("connector_metadata", { id: item.id });
  }
}
async function manageConnector(item: any) {
  if (connectorReview.value) await rpc("connector_cancel");
  connectorId.value = item.id;
  connectorMode.value = item.config.transport === "stdio" ? "environment" : "oauth";
  connectorReview.value = null; connectorDiscovery.value = null; connectorMessage.value = "";
  connectorForm.value = { issuer: "", client_id: "", scopes: "read", name: "API_KEY", token: "" };
  connectorRegistrationMethod.value = "pre_registered";
  connectorClientMetadata.value = ""; connectorRegistrationApproved.value = false;
}
async function discoverConnector() {
  await run(async () => {
    if (connectorReview.value) await rpc("connector_cancel");
    connectorReview.value = null; connectorRegistrationApproved.value = false;
    connectorDiscovery.value = await rpc("connector_discover_interop", { id: connectorId.value });
    connectorForm.value.issuer = connectorDiscovery.value.authorization_servers[0];
    if (connectorDiscovery.value.suggested_scopes?.length)
      connectorForm.value.scopes = connectorDiscovery.value.suggested_scopes.join(" ");
  });
}
async function previewConnector() {
  await run(async () => {
    if (connectorReview.value) await rpc("connector_cancel");
    connectorReview.value = null; connectorRegistrationApproved.value = false;
    const client_metadata = connectorRegistrationMethod.value === "client_id_metadata" && connectorClientMetadata.value.trim()
      ? JSON.parse(connectorClientMetadata.value) : undefined;
    connectorReview.value = await rpc("connector_preview_registration", { id: connectorId.value, issuer: connectorForm.value.issuer,
      registration_method: connectorRegistrationMethod.value, client_id: connectorForm.value.client_id,
      ...(client_metadata ? { client_metadata } : {}), requested_scopes: connectorForm.value.scopes.trim().split(/\s+/) });
  });
}
async function approveConnectorRegistration() {
  await run(async () => {
    const reviewed = connectorReview.value;
    // Clear the effect approval before dispatch. An uncertain response requires
    // new native discovery; the consumed registration POST cannot be repeated.
    connectorReview.value = null; connectorRegistrationApproved.value = false;
    connectorReview.value = await rpc("connector_register_client", { id: connectorId.value,
      preview_id: reviewed.preview_id, digest: reviewed.digest });
    connectorRegistrationApproved.value = true;
    connectorMessage.value = "Client details accepted. Review the destinations and scopes before browser sign-in.";
  });
}
async function startConnector() {
  await run(async () => {
    const auth = await rpc("connector_start", { id: connectorId.value, preview_id: connectorReview.value.preview_id });
    connectorReview.value = null;
    await rpc("open_url", { url: auth.url });
    connectorMessage.value = "Finish connector sign-in in your browser. Tools stay disabled until reviewed and enabled.";
    clearInterval(connectorTimer);
    connectorTimer = setInterval(async () => {
      try {
        const status = await rpc("connector_status");
        if (!["waiting", "validating"].includes(status.state)) {
          clearInterval(connectorTimer); connectorMessage.value = status.error || status.state;
          extensions.value = await rpc("extensions"); await refreshConnectorInfo();
        }
      } catch { clearInterval(connectorTimer); connectorMessage.value = "Connector status unavailable. Reopen settings to inspect access."; }
    }, 1500);
  });
}
async function saveConnectorSecret() {
  const token = connectorForm.value.token; connectorForm.value.token = "";
  await run(async () => {
    const method = connectorMode.value === "environment" ? "connector_save_environment" : "connector_save_bearer";
    await rpc(method, { id: connectorId.value, token, ...(connectorMode.value === "environment" ? { name: connectorForm.value.name } :
      { requested_scopes: connectorForm.value.scopes.trim().split(/\s+/) }) });
    connectorMessage.value = "Access saved in secure storage. Review and enable the connector separately.";
    extensions.value = await rpc("extensions"); await refreshConnectorInfo();
  });
}
async function disconnectConnector(id: string) {
  await run(async () => {
    const result = await rpc("connector_disconnect", { id });
    connectorMessage.value = result.storage_cleanup_pending ? "Access blocked locally; secure storage cleanup needs another attempt." :
      result.remote_revocation_confirmed ? "Disconnected; remote revocation confirmed." : "Disconnected locally; remote revocation was not confirmed.";
    extensions.value = await rpc("extensions"); await refreshConnectorInfo();
  });
}
async function cancelConnector() {
  await rpc("connector_cancel"); clearInterval(connectorTimer); connectorReview.value = null;
  connectorRegistrationApproved.value = false; connectorMessage.value = "Connector review and sign-in cancelled.";
}
async function loadExtensions() {
  await run(async () => {
    extensions.value = await rpc("extensions");
    await refreshConnectorInfo();
  });
}
async function importSkill() {
  await run(async () => {
    const path = await selectFile(false, false, true);
    if (path) {
      await rpc("extension_import", { path });
      extensions.value = await rpc("extensions");
      await refreshConnectorInfo();
    }
  });
}
async function installExtension() {
  await run(async () => {
    try {
      await rpc("extension_add", {
        kind: extensionType.value,
        name: extensionName.value,
        content: extensionType.value === "plugin"
          ? JSON.stringify({ ...JSON.parse(extensionText.value), protocol_era: extensionEra.value })
          : extensionText.value,
        url: extensionUrl.value || null,
      });
    } catch (cause) {
      const text = errorText(cause);
      throw new Error(LAUNCH_REFUSAL.test(text) ? LOCAL_SERVER_SCOPE + " (Pebble said: " + text + ")" : text);
    }
    extensionName.value = "";
    extensionText.value = "";
    extensionUrl.value = "";
    extensions.value = await rpc("extensions");
    await refreshConnectorInfo();
  });
}
async function extensionToggle(id: string, enabled: boolean) {
  await run(async () => {
    await rpc("extension_toggle", { id, enabled: !enabled });
    extensions.value = await rpc("extensions");
    await refreshConnectorInfo();
  });
}
async function extensionRemove(id: string) {
  await run(async () => {
    await rpc("extension_remove", { id });
    delete packageInfo.value[id]; delete packageHistory.value[id];
    if (packageTarget.value === id) { packageTarget.value = ""; packageReview.value = null; packageOpen.value = false; }
    extensions.value = await rpc("extensions");
    await refreshConnectorInfo();
  });
}
async function inspectReference(id: string, digest: string, reference: string, offset = 0) {
  await run(async () => {
    const page = await rpc('skill_inspect', { id, digest, reference, offset });
    const previous = extensionPreview.value[id];
    extensionPreview.value[id] = { ...page, untrusted_source:
      offset && previous?.path === reference ? previous.untrusted_source + page.untrusted_source : page.untrusted_source };
  });
}

// ---- How a memory record reads (V1-100) ----
// Every record shows its words and where it came from, never an origin code, a field name or "undefined".
const words = (value: unknown) => (typeof value === "string" ? value.trim() : "");
const ACTION_NOUNS: Record<string, string> = {
  edit: "File edit", tests: "Test run", command: "Command", app: "App launch", git: "Git change", plugin: "Connector call",
  worktree: "Helper worktree", install: "Install", screen: "Browser or app action",
};
const ACTION_STATES: Record<string, string> = {
  applied: "done", completed: "done", rejected: "declined", failed: "failed", pending: "waiting for you", running: "running",
  awaiting_user_approval: "waiting for you", superseded: "replaced", cancelled: "stopped", uncertain: "outcome unknown",
};
const OUTCOMES: Record<string, string> = {
  completed: "finished", failed: "failed", stopped: "stopped", cancelled: "stopped", interrupted: "interrupted",
  budget_exhausted: "ran out of time or steps", verification_required: "held for a check", paused: "paused",
};
function where(scope: unknown) {
  return typeof scope === "string" && scope.startsWith("chat:") ? "this conversation" : "this project";
}
function memoryText(m: any): string {
  const raw = typeof m?.content === "string" ? m.content : "";
  let v: any;
  try {
    v = JSON.parse(raw);
  } catch {
    return raw;
  }
  if (!v || typeof v !== "object" || Array.isArray(v)) return raw;
  if (m.kind === "working") {
    const goal = words(v.current_goal), phase = words(v.phase);
    const actions = Array.isArray(v.actions) ? v.actions.filter((a: any) => a && typeof a === "object") : [];
    const lines = [goal ? "Current task: " + goal : "A task in progress"];
    if (phase) lines.push("State: " + phase.replaceAll("_", " "));
    if (actions.length)
      lines.push("Actions: " + actions.map((a: any) => [words(a.path) || ACTION_NOUNS[a.kind] || "Action", ACTION_STATES[a.status] || words(a.status)]
        .filter(Boolean).join(" · ") + (a.result?.exit_code != null ? " · exit " + a.result.exit_code : "")).join("; "));
    return lines.join("\n");
  }
  if (m.kind === "episodic") {
    if (v.claim_class === "observed_outcome" || v.claim_class === "host_lesson")
      return words(v.observation) ? "Seen in " + where(m.scope) + ": " + words(v.observation) : "A past result in " + where(m.scope) + ".";
    if (v.claim_class === "model_lesson_unverified")
      return words(v.lesson) ? "Lesson suggested by the model (not verified): " + words(v.lesson) : "A lesson suggested by the model (not verified).";
    if ("user_request" in v || "task_outcome" in v) {
      const actions = Array.isArray(v.broker_evidence) ? v.broker_evidence.filter((a: any) => a && typeof a === "object") : [];
      return [
        words(v.user_request) ? "Asked: " + words(v.user_request) : "An earlier request",
        "Result: " + (OUTCOMES[v.task_outcome] || words(v.task_outcome).replaceAll("_", " ") || "not recorded"),
        actions.length
          ? actions.map((a: any) => (ACTION_NOUNS[a.kind] || "Action") + " · " + (ACTION_STATES[a.status] || words(a.status) || "unknown")).join("; ")
          : "No file, command or app actions.",
      ].join("\n");
    }
    if (words(v.observation)) return words(v.observation);
    if (words(v.lesson)) return words(v.lesson);
  }
  if (m.kind === "temporal" && v.place)
    return [
      words(v.place) + (words(v.date) ? " · " + words(v.date) : ""),
      (v.temperature_c ?? "?") + " °C · forecast " + (v.forecast_low_c ?? "?") + "–" + (v.forecast_high_c ?? "?") + " °C",
      v.retrieved ? "Retrieved " + new Date(v.retrieved).toLocaleString() : "",
    ].filter(Boolean).join("\n");
  if (typeof v.quote === "string") {
    const source = words(v.path) || words(v.source);
    return "“" + v.quote + "”" + (source ? "\nFrom " + source : "");
  }
  if (m.kind === "procedural" && typeof v.recipe === "string")
    return "Known test recipe: " + v.recipe + (words(v.language) ? " (" + words(v.language) + ")" : "") + ". It never runs from memory; each run still follows your permissions.";
  return raw;
}
function provenanceClass(m: any) {
  if (typeof m?.provenance !== "string") return "";
  try {
    const proof = JSON.parse(m.provenance);
    return typeof proof?.evidence_class === "string" ? proof.evidence_class : "";
  } catch {
    return "";
  }
}
const LEARNED_FROM: Record<string, string> = {
  human_preference: "Learned from what you said",
  confirmed_human_quote: "Learned from what you said",
  attributed_file_quote: "Learned from a file Pebble read",
  attributed_web_quote: "Learned from a web page Pebble read",
  weather_service: "From a weather lookup",
  host_lesson: "Learned from repeated task results",
  model_lesson_unverified: "Suggested by the model (not verified)",
};
function memoryOrigin(m: any): string {
  if (m?.id === "pebble-md") return "From PEBBLE.md";
  switch (m?.origin) {
    case "direct-user":
      return "You asked Pebble to remember this";
    case "user":
      return "Added in Settings";
    case "curated":
      return "Built into Pebble";
    case "imported":
      return "Imported from a memory archive";
    case "broker":
      return "Task state";
    case "conversation":
      return m.kind === "episodic" ? "Learned from a task" : "From a conversation";
    case "auto-learned":
      return LEARNED_FROM[provenanceClass(m)] || (m.kind === "procedural" ? "Learned from a test run" : "Learned from a task");
    default:
      return "Saved by Pebble";
  }
}
const LEARNING_STATES: Record<string, string> = {
  contested: "disagrees with another saved memory; not used until you choose",
  superseded: "replaced by a newer version",
  promoted: "",
};
function memoryMeta(m: any) {
  const chat = m.chat_id ? props.state.chats?.find((c) => c.id === m.chat_id) : null;
  const when = typeof m.updated === "number" && Number.isFinite(m.updated) ? new Date(m.updated * 1000).toLocaleDateString() : "";
  const status = m.learning_status ? LEARNING_STATES[m.learning_status] ?? "" : "";
  return [
    memoryOrigin(m),
    m.scope === "global" ? "Personal" : String(m.scope || "").startsWith("chat:") ? "Conversation" : "Project",
    chat?.title ? "From: " + chat.title : "",
    when,
    status,
    m.expires ? "expires " + new Date(m.expires * 1000).toLocaleString() : "",
  ].filter(Boolean).join(" · ");
}
// V1-161: the Forget button names the record by its words, not by an internal ID.
function forgetLabel(m: any) {
  const text = memoryText(m).replace(/\s+/g, " ").trim();
  return "Forget this memory: " + (text.length > 40 ? text.slice(0, 40) + "…" : text || "untitled");
}
// V1-146: candidates say why they wait, in words.
const CANDIDATE_STATES: Record<string, string> = { pending: "Waiting for evidence", conflict: "Disagrees with a saved memory", unsupported: "Not saved" };
const CANDIDATE_REASONS: Record<string, string> = {
  run_consent_disabled: "memory was off when that task started",
  file_permission_unavailable: "reading project files is off in Permissions",
  durable_human_preference_required: "it was not something you said you prefer",
  exact_source_quote_required: "the text is not an exact quote of its source",
  quote_not_observed: "Pebble did not see this text in its source",
  current_disk_witness_required: "the file changed since Pebble read it",
  source_changed: "the source changed since Pebble read it",
  fresh_temporal_receipt_required: "the time-sensitive source is no longer fresh",
  dated_source_required: "the source has no date",
  wait_for_settled_test_evidence: "waiting for the test run to finish",
  wait_for_settled_lesson_evidence: "waiting for the task to finish",
  completed_test_recipe_required: "a finished, passing test run is needed",
  successful_broker_test_required: "a passing test run is needed",
  new_test_evidence_required_after_failure: "a new passing run is needed after a failure",
  lesson_evidence_required: "no task result supports it",
  lesson_evidence_not_current: "the task results behind it changed",
  unsafe_lesson_text: "the text looked unsafe to keep",
  sensitive_or_oversized_evidence: "the evidence was sensitive or too large",
  contested_record: "it disagrees with another saved memory",
};
function candidateText(candidate: any) {
  const state = CANDIDATE_STATES[candidate.status] || "Waiting";
  const reason = typeof candidate.reason === "string" ? CANDIDATE_REASONS[candidate.reason] || "Pebble could not confirm it from its evidence" : "";
  return reason ? state + ": " + reason : state;
}
// V1-146: the real recall size, 6% of the model's window between 600 and 4,000 characters (memory_retrieval).
const recallChars = computed(() => {
  const window = Number(props.state.model?.budget || props.state.model?.context || 4096);
  return Math.max(600, Math.min(4000, Math.round((Number.isFinite(window) ? window : 4096) * 0.06)));
});
async function power() {
  await run(async () => {
    await rpc(
      props.state.model.state === "ready" ? "model_unload" : "model_load",
    );
  });
}
</script>
<template>
  <nav class="settings-tabs" role="tablist" aria-label="Settings sections">
    <!-- The tab list StorageSettings.test.mjs pins; Views (mods) is listed only with Developer features (tabs). -->
    <template v-for="t in ['models', 'permissions', 'memory', 'extensions', 'mods', 'storage']" :key="t">
    <button
      v-if="tabs.includes(t)"
      :id="'settings-tab-' + t"
      type="button"
      role="tab"
      :aria-selected="tab === t"
      aria-controls="settings-panel"
      :tabindex="tab === t ? 0 : -1"
      :class="{ selected: tab === t }"
      @click="changeTab(t)"
      @keydown="tabKey"
    >
      <PhCpu v-if="t === 'models'" :size="15" /><PhShieldCheck
        v-else-if="t === 'permissions'"
        :size="15"
      /><PhBrain v-else-if="t === 'memory'" :size="15" /><PhFolder
        v-else-if="t === 'storage'"
        :size="15"
      /><PhPlugs
        v-else
        :size="15"
      />{{ TAB_LABELS[t] || t }}
    </button>
    </template>
  </nav>
  <div v-if="error" class="settings-error" role="alert">{{ error }}</div>
  <div id="settings-panel" role="tabpanel" :aria-labelledby="'settings-tab-' + tab">
  <section v-if="tab === 'models'" class="advanced-section">
    <div class="section-label">YOUR MODELS</div>
    <template v-for="p in connectionRows" :key="p.id">
    <article
      class="profile-row"
      :class="{ selected: state.model.profile_id === p.id, 'just-connected': highlight === p.id }"
    >
      <button :disabled="disabled || busy" :aria-pressed="state.model.profile_id === p.id" @click="selectModel(p.id)">
        <PhCpu :size="20" /><span
          ><strong>{{ p.name }}</strong
          ><small
            >{{
              p.kind === "gguf"
                ? "On this Mac (GGUF file)"
                : p.local
                  ? "Local API server"
                  : p.kind === "anthropic"
                    ? "Anthropic API"
                    : p.kind === "chatgpt"
                      ? "ChatGPT plan"
                      : "API provider"
            }}
            · {{ p.model || p.context.toLocaleString() + "-token window"
            }}{{ p.has_key ? " · Key stored securely" : "" }} ·
            {{ p.context.toLocaleString() }}-token window · {{ connectionCount(p) }}
            {{ connectionCount(p) === 1 ? "model" : "models" }}</small
          ></span
        ><i
          v-if="state.model.profile_id === p.id"
          class="selected-dot"
        /></button
      ><button
        v-if="p.kind !== 'gguf'"
        class="icon-button"
        :disabled="disabled || busy"
        :aria-label="'Configure ' + p.name"
        title="Model and context settings"
        @click="editModel(p)"
      >
        <PhSlidersHorizontal :size="16" /></button
      ><button
        v-if="p.kind === 'chatgpt'"
        class="secondary-button"
        :disabled="disabled || busy || signingIn"
        @click="signIn(p.id)"
      >
        Reconnect</button
      ><button
        v-if="p.kind === 'chatgpt'"
        class="secondary-button"
        :disabled="disabled || busy"
        title="Sign out of this account"
        @click="signOut(p.id)"
      >
        Sign out</button
      ><button
        class="icon-button"
        :disabled="disabled || busy"
        :aria-label="'Remove selected model ' + (p.model || p.name)"
        title="Remove this model; keep other models in the connection"
        @click="removeModel(p.id)"
      >
        <PhTrash :size="15" />
      </button>
    </article>
    <div v-if="signin.profile === p.id && (signin.phase !== 'idle' || signin.message)" class="signin-status" :class="{ failed: signin.phase === 'failed' }" :role="signin.phase === 'failed' ? 'alert' : 'status'" aria-live="polite">
      <p>{{ signin.message }}</p>
      <div v-if="signin.url && (signin.phase === 'waiting' || signin.phase === 'validating')" class="signin-link">
        <input readonly :value="signin.url" aria-label="ChatGPT sign-in link" @focus="selectAll" />
        <button type="button" class="secondary-button" @click="copySignInLink">{{ signin.copied ? "Copied" : "Copy link" }}</button>
      </div>
      <div class="setting-actions">
        <button v-if="signin.phase === 'failed' && signin.action === 'retry'" type="button" class="secondary-button" :disabled="disabled || busy" @click="signIn(p.id)">Try again</button>
        <button v-if="signin.phase === 'failed' && signin.action === 'reconnect'" type="button" class="secondary-button" :disabled="disabled || busy" @click="signIn(p.id)">Continue with ChatGPT</button>
        <button v-if="signingIn && signin.phase !== 'opening'" type="button" class="text-button" @click="cancelSignIn">Cancel sign-in</button>
      </div>
    </div>
    </template>
    <div class="setting-actions">
      <button
        class="secondary-button"
        :disabled="disabled || busy"
        title="Choose a GGUF model file on this Mac"
        @click="addLocal"
      >
        <PhFolder :size="14" />Add a model file</button
      ><button
        class="secondary-button"
        :disabled="disabled || busy"
        @click="newProvider"
      >
        <PhPlus :size="14" />Connect provider</button
      ><button
        v-if="state.model.kind === 'gguf' && state.model.configured"
        class="secondary-button"
        :disabled="disabled || busy || state.model.state === 'loading'"
        @click="power"
      >
        {{ state.model.state === "ready" ? "Rest model" : "Load now" }}
      </button>
    </div>
    <button
      class="chatgpt-sign-in"
      :disabled="disabled || busy || signingIn"
      @click="signIn()"
    >
      {{ signin.phase === 'opening' && !signin.profile ? "Opening ChatGPT sign-in…" : "Continue with ChatGPT" }}
    </button>
    <div v-if="!signin.profile && (signin.phase !== 'idle' || signin.message)" class="signin-status" :class="{ failed: signin.phase === 'failed' }" :role="signin.phase === 'failed' ? 'alert' : 'status'" aria-live="polite">
      <p>{{ signin.message }}</p>
      <div v-if="signin.url && (signin.phase === 'waiting' || signin.phase === 'validating')" class="signin-link">
        <input readonly :value="signin.url" aria-label="ChatGPT sign-in link" @focus="selectAll" />
        <button type="button" class="secondary-button" @click="copySignInLink">{{ signin.copied ? "Copied" : "Copy link" }}</button>
      </div>
      <div class="setting-actions">
        <button v-if="signin.phase === 'failed' && signin.action === 'retry'" type="button" class="secondary-button" :disabled="disabled || busy" @click="signIn()">Try again</button>
        <button v-if="signin.phase === 'failed' && signin.action === 'reconnect'" type="button" class="secondary-button" :disabled="disabled || busy" @click="signIn()">Continue with ChatGPT</button>
        <button v-if="signingIn && signin.phase !== 'opening'" type="button" class="text-button" @click="cancelSignIn">Cancel sign-in</button>
      </div>
    </div>
    <p class="setting-note">
      Use an eligible Plus or Pro plan through OpenAI's preview for open-source
      or approved private clients. Plan limits and model availability apply. API
      billing stays separate.
    </p>
    <form v-if="adding" class="provider-form" @submit.prevent="saveModel">
      <label
        >Connection name<input
          v-model="form.name"
          placeholder="e.g. LM Studio or my API provider"
          required
          maxlength="80" /></label
      ><label
        >API format<select
          v-model="form.kind"
          :disabled="form.kind === 'chatgpt'"
        >
          <option value="openai">
            OpenAI compatible · OpenAI, Ollama, LM Studio, other servers
          </option>
          <option value="anthropic">Anthropic · Claude</option>
          <option value="responses">
            Responses API · compatible endpoints
          </option>
          <option v-if="form.kind === 'chatgpt'" value="chatgpt">
            ChatGPT plan · Responses
          </option>
        </select></label
      ><label
        >API base URL<input
          v-model="form.url"
          :disabled="form.kind === 'chatgpt'"
          @blur="autoDiscover"
          type="url"
          placeholder="https://api.openai.com/v1 or http://localhost:1234/v1"
          required /></label
      ><label v-if="form.kind !== 'chatgpt'"
        >API key <span class="optional">optional for local servers</span
        ><input
          v-model="form.api_key"
          @blur="autoDiscover"
          type="password"
          autocomplete="new-password"
          placeholder="Stored in your OS credential vault" /></label
      ><button
        class="secondary-button"
        type="button"
        :disabled="busy || !form.url"
        @click="discover"
      >
        <PhArrowClockwise :size="14" />{{
          busy ? "Connecting…" : "Fetch available models"
        }}</button
      ><p v-if="discoverError" class="settings-error discover-error" role="alert">{{ discoverError }}</p
      ><label
        >Inspect model<select
          v-if="fetched.length"
          v-model="form.model"
          @change="picked"
        >
          <option v-for="m in fetched" :key="m.id" :value="m.id">
            {{ m.name }}
          </option></select
        ><input
          v-else
          v-model="form.model"
          required
          placeholder="Model ID · or fetch the list above"
      /></label>
      <div v-if="fetched.length" class="model-catalogue">
        <label
          >Find models<input
            v-model="modelSearch"
            placeholder="Search model names"
        /></label>
        <div class="catalogue-summary">
          <span>{{ selectedModels.length }} selected</span
          ><button
            type="button"
            class="text-button"
            @click="
              selectedModels = [
                ...new Set([
                  ...selectedModels,
                  ...visibleModels.slice(0, 50).map((m) => m.id),
                ]),
              ].slice(0, 50)
            "
          >
            Select visible</button
          ><button
            type="button"
            class="text-button"
            @click="selectedModels = []"
          >
            Clear
          </button>
        </div>
        <div class="catalogue-options">
          <label v-for="m in visibleModels" :key="m.id" class="catalogue-option"
            ><input
              type="checkbox"
              v-model="selectedModels"
              :value="m.id"
              :disabled="
                !selectedModels.includes(m.id) && selectedModels.length >= 50
              "
            /><span
              >{{ m.name
              }}<small>{{
                m.context
                  ? m.context.toLocaleString() + " tokens"
                  : "Window size not reported"
              }}</small></span
            ></label
          >
        </div>
      </div>
      <div class="form-inline">
        <label
          >Context window (tokens)<input
            v-model.number="form.context"
            :disabled="
              form.kind === 'chatgpt' ||
              !!fetched.find((m) => m.id === form.model)?.context
            "
            type="number"
            min="4096"
            max="10000000"
            step="1" /></label
        ><label class="checkbox-label"
          ><input v-model="form.tools" type="checkbox" />Model supports tool
          calls</label
        >
      </div>
      <label
        >Context usage<select v-model="form.context_mode">
          <option value="full">
            Full available window · automatic compaction
          </option>
          <option value="custom">Custom input budget</option>
        </select></label
      >
      <label v-if="form.context_mode === 'custom'"
        >Working input budget<input
          v-model.number="form.budget"
          type="number"
          min="4096"
          :max="form.context"
          required
      /></label>
      <p class="setting-note">
        Full mode allows the available model window, reserving space for output.
        Older complete tool exchanges compact first; your current request and
        task checkpoint stay protected. ChatGPT account limits can differ from
        API-key limits. Only available account context is used; unavailable
        experimental limits are not enabled. Estimates are used when the
        provider offers no authorized token-counting endpoint.
      </p>
      <p class="setting-note">
        Online providers receive the conversation, retrieved memories and source
        excerpts included in a request. Models in a provider's catalogue may not
        all support chat or tools.
      </p>
      <button
        class="primary-button"
        :disabled="
          busy || disabled || (fetched.length > 0 && !selectedModels.length)
        "
      >
        Save selected models
      </button>
    </form>
    <p class="setting-note">
      One model runs at a time. Local GGUF models use a 4K context and unload
      after five idle minutes. API models use their own context window and
      adjustable working budget.
    </p>
  </section>
  <section v-else-if="tab === 'permissions'" class="advanced-section">
    <div id="permission-mode-label" class="section-label">HOW PEBBLE ACTS</div>
    <div role="radiogroup" aria-labelledby="permission-mode-label" class="permission-modes">
      <button
        v-for="id in MODE_IDS"
        :id="'permission-mode-' + id"
        :key="id"
        type="button"
        role="radio"
        :aria-checked="state.settings.permissions.mode === id"
        :tabindex="state.settings.permissions.mode === id ? 0 : -1"
        class="permission-choice"
        :class="{ selected: state.settings.permissions.mode === id }"
        :disabled="disabled || busy"
        @click="permission(id)"
        @keydown="modeKey"
      >
        <i /><span
          ><strong>{{ MODES[id].name }}</strong
          ><small>Runs without asking: {{ MODES[id].runs_without_asking }}</small
          ><small>Always asks: {{ MODES[id].always_asks }}</small></span
        >
      </button>
    </div>
    <p class="setting-note">{{ EVERY_MODE }}</p>
    <div v-if="state.settings.permissions.mode === 'full'" class="access-note">
      Full access increases the impact of mistakes and prompt injection.
      AgentGuard is a reliability check; it cannot guarantee that every command
      or answer is safe.
    </div>
    <div class="section-label tool-label">AVAILABLE TOOLS</div>
    <button
      v-for="(enabled, tool) in state.settings.permissions.tools"
      :key="tool"
      type="button"
      role="switch"
      :aria-checked="!!enabled"
      class="setting-row"
      :disabled="disabled || busy"
      @click="permission(undefined, tool)"
    >
      <div>
        <strong>{{ toolLabels[tool] || tool }}</strong>
        <small v-if="tool === 'web'">{{ WEB_DESTINATION }}</small>
      </div>
      <span class="toggle" :class="{ on: enabled }" aria-hidden="true" />
    </button>
    <p class="setting-note">
      Only this settings screen can grant access. Documents, web pages,
      memories, models and extensions cannot change permissions. Changing access
      takes effect on the next task.
    </p>
    <div class="section-label">COMMAND CREDENTIALS</div>
    <p class="setting-note">
      A reviewed command plan can name one of these to reach an approved destination. Pebble adds the
      value to matching requests only. The command never sees it, and it is stored in the macOS keychain, never in Pebble's database.
    </p>
    <article v-for="name in planCredentialNames" :key="name" class="profile-row">
      <strong>{{ name }}</strong>
      <button class="text-button" :disabled="busy" :aria-label="'Remove command credential ' + name" @click="removePlanCredential(name)">Remove</button>
    </article>
    <form class="plan-credential-form" @submit.prevent="savePlanCredential">
      <label>Credential name<input v-model="planCredentialName" maxlength="128" autocomplete="off" :spellcheck="false" /></label>
      <label>Value<input v-model="planCredentialSecret" type="password" maxlength="2400" autocomplete="off" :spellcheck="false" /></label>
      <button class="secondary-button" type="submit" :disabled="busy || !planCredentialName.trim() || !planCredentialSecret">Save command credential</button>
    </form>
  </section>
  <section v-else-if="tab === 'memory'" class="advanced-section">
    <button class="setting-row" type="button" role="switch" :aria-checked="!!state.settings.memory_enabled" :disabled="busy" @click="memoryPower">
      <div>
        <strong>Remember useful context</strong
        ><small
          >Selective recall across conversations, stored on this device</small
        >
      </div>
      <span class="toggle" :class="{ on: state.settings.memory_enabled }" aria-hidden="true" />
    </button>
    <div class="memory-types" role="tablist" aria-label="Memory layers">
      <button
        v-for="k in state.memory?.types"
        :key="k.id"
        type="button"
        role="tab"
        :aria-selected="type === k.id"
        aria-controls="memory-records"
        :tabindex="type === k.id ? 0 : -1"
        :id="'memory-layer-' + k.id"
        :class="{ selected: type === k.id }"
        @click="getMemories(k.id)"
        @keydown="layerKey"
      >
        <span>{{ k.name }}</span
        ><small>{{ k.count }}</small>
      </button>
    </div>
    <p class="setting-note">
      Working context records the task state. Experiences record past requests
      and what their actions did. The agent can learn attributed source quotes,
      direct preferences and repeated test recipes. Time-sensitive data expires.
      Retrieved text never becomes permission.
    </p>
    <button class="setting-row" type="button" role="switch" :aria-checked="state.settings.memory_layers?.[type] !== false" :disabled="busy || !state.settings.memory_enabled" @click="memoryLayer">
      <div><strong>Learning and recall in this layer</strong><small>{{ state.memory?.types.find(k => k.id === type)?.name }}</small></div>
      <span class="toggle" :class="{ on: state.settings.memory_layers?.[type] !== false }" aria-hidden="true" />
    </button>
    <form class="memory-search" role="search" @submit.prevent="searchMemories">
      <input v-model="memoryQuery" type="search" maxlength="200" aria-label="Search this layer's memories" placeholder="Search this layer" />
      <button class="secondary-button" type="submit" :disabled="busy">Search</button>
      <button v-if="memorySearched" class="text-button" type="button" :disabled="busy" @click="clearMemorySearch">Clear search</button>
    </form>
    <div id="memory-records" class="memory-items" role="tabpanel" aria-label="Saved memories">
      <article v-for="m in memories" :key="m.id">
        <div>
          <small>{{ memoryMeta(m) }}</small>
          <p>{{ memoryText(m) }}</p>
        </div>
        <button
          class="icon-button"
          type="button"
          :disabled="busy || !!state.active"
          :title="state.active ? 'Available when the task is idle' : 'Forget this memory'"
          :aria-label="forgetLabel(m)"
          @click="forget(m.id)"
        >
          <PhTrash :size="14" />
        </button>
      </article>
      <p v-if="!memories.length" class="empty-memory">
        {{ memorySearched ? "No memories in this layer match “" + memorySearched + "”." : "Nothing saved here yet." }}
      </p>
      <button v-if="memoryMore" class="secondary-button" type="button" :disabled="busy" @click="loadMoreMemories">Load more</button>
    </div>
    <div class="memory-items" aria-label="Learning candidates">
      <p class="setting-note">Supported proposals are saved automatically. Conflicting or unsupported proposals stay out of recall. {{ state.memory?.learning?.last_error_type ? 'Background memory upkeep hit a problem; it tries again on its own.' : '' }}</p>
      <article v-for="candidate in memoryCandidates.filter(c => c.kind === type && ['pending','conflict','unsupported'].includes(c.status))" :key="candidate.id">
        <div><small>{{ candidateText(candidate) }}</small><p>{{ candidate.content || 'A test recipe Pebble saw pass' }}</p><small>{{ candidate.scope === 'global' ? 'Personal' : candidate.scope.startsWith('chat:') ? 'Conversation' : 'Project' }}</small></div>
        <div v-if="candidate.status === 'conflict'" class="setting-actions">
          <button class="secondary-button" :disabled="busy || !!state.active || !state.settings.memory_enabled || state.settings.memory_layers?.[type] === false" @click="resolveMemory(candidate.id, true)">Use this value</button>
          <button class="secondary-button" :disabled="busy || !!state.active" @click="resolveMemory(candidate.id, false)">Keep current</button>
        </div>
      </article>
      <button class="secondary-button" :disabled="busy || !!state.active || !state.settings.memory_enabled" @click="maintainMemory">Check waiting memories now</button>
    </div>
    <MemoryContests :active="!!state.active" :refresh-key="memories" @changed="getMemories()" @error="error = $event" />
    <form
      v-if="!['working', 'episodic'].includes(type)"
      class="provider-form"
      @submit.prevent="saveMemory"
    >
      <label
        >Add
        {{ state.memory?.types.find((k) => k.id === type)?.name.toLowerCase()
        }}<textarea
          v-model="memoryContent"
          rows="3"
          maxlength="4000"
          required
          placeholder="Something useful to recall later…"
        /></label
      ><label
        >Recall scope<select v-model="memoryScope">
          <option value="global">Personal · all conversations</option>
          <option value="project">
            Current conversation's connected project
          </option>
        </select></label
      ><label v-if="type === 'temporal'"
        >Expires at<input
          v-model="expiry"
          type="datetime-local"
          required /></label
      ><button
        class="secondary-button"
        :disabled="busy || !memoryContent.trim()"
      >
        <PhPlus :size="14" />Save memory
      </button>
    </form>
    <p class="setting-note">
      Pebble includes up to about {{ recallChars.toLocaleString() }} characters of memory with each message to the
      current model, ranked by relevance and scope (the amount depends on the model). Recovery journals remain
      available while personalized memory is off.
    </p>
    <MemoryArchive :active="!!state.active" @changed="getMemories()" @error="error = $event" />
    <button class="text-button danger" :disabled="!!state.active" :title="state.active ? 'Available when the task is idle' : undefined" @click="clearConfirm = true">
      Forget all memories
    </button>
    <div v-if="clearConfirm" class="delete-confirmation">
      <p>
        Delete all saved memories and task checkpoints? Conversations stay
        available.
      </p>
      <div class="setting-actions">
        <button class="secondary-button" @click="clearConfirm = false">
          Keep them</button
        ><button class="secondary-button danger" @click="clear">
          Forget permanently
        </button>
      </div>
    </div>
  </section>
  <ModsSettings v-else-if="tab === 'mods'" :active="!!state.active" :allowed="state.settings.permissions.tools.views" @refresh="emit('refresh')" />
  <!-- Phase 22 (s22-storage): Data & storage, main window only. -->
  <section v-else-if="tab === 'storage'" class="advanced-section"><StorageSettings @refresh="emit('refresh')" /></section>
  <section v-else class="advanced-section">
    <div class="section-label">SKILLS AND CONNECTORS</div>
    <p class="setting-note">
      Skills are reusable task instructions. Connectors are MCP servers that give
      Pebble tools. Both start disabled: review their contents before enabling
      them. Imported skills keep a pinned snapshot. Hooks (advanced) are rules
      that run at task steps in an isolated Python worker with no project or
      network access; they need terminal permission. Skills, connectors and
      hooks cannot expand permissions.
    </p>
    <article v-for="x in extensions" :key="x.id" class="extension-card">
      <div>
        <strong>{{ x.name }}</strong
        ><small
          >{{ extensionNouns[x.kind] || x.kind }} · {{ x.enabled ? "Enabled" : "Disabled"
          }}{{
            x.capabilities?.length ? " · " + x.capabilities.join(", ") : ""
          }}</small
        >
      </div>
      <details>
        <summary>Review {{ (extensionNouns[x.kind] || 'extension').toLowerCase() }}</summary>
        <pre>{{ x.content }}</pre>
        <template v-if="x.kind === 'plugin'">
          <p class="setting-note">{{ x.config.transport || 'http' }} · {{ x.config.protocol_era || 'legacy' }} protocol · {{ x.config.url || x.config.command }}. Calls require native review.</p>
          <p v-if="x.config.catalogue_digest" class="setting-note">Reviewed version {{ x.config.catalogue_digest }} · protocol {{ x.config.protocol }}. The server is checked again before every call.</p>
          <pre v-if="x.config.launch_files">{{ JSON.stringify(x.config.launch_files, null, 2) }}</pre>
          <pre v-if="x.config.schemas">{{ JSON.stringify(x.config.schemas, null, 2) }}</pre>
          <p class="setting-note">Connector access: {{ connectorInfo[x.id]?.state || 'disconnected' }}. Credential values are withheld.</p>
          <p v-if="connectorInfo[x.id]?.storage_cleanup_pending" class="setting-note">Secure storage cleanup is pending. Disconnect to retry removal.</p>
          <template v-if="x.config.package_digest">
            <p class="setting-note">Package checksum {{ x.config.package_digest }} · publisher unverified.</p>
            <button class="text-button" :disabled="packageBlocked" @click="managePackage(x.id)">Package history and update</button>
            <template v-if="packageInfo[x.id]">
              <p class="setting-note">{{ packageInfo[x.id].package_id }} · installed revision {{ packageInfo[x.id].revision }} · release {{ packageInfo[x.id].manifest.release }}</p>
              <div v-for="revision in packageHistory[x.id]" :key="revision.revision" class="package-history-row">
                <small>Revision {{ revision.revision }} · {{ revision.release }} · {{ revision.operation }}<br />{{ revision.digest }}</small>
                <button v-if="revision.revision < packageInfo[x.id].revision" class="text-button" :disabled="packageBlocked"
                  @click="previewPackageRollback(x.id, revision.revision)">Review rollback</button>
              </div>
            </template>
          </template>
          <button class="text-button" :disabled="busy || disabled" @click="manageConnector(x)">Manage connector access</button>
          <button v-if="connectorInfo[x.id]?.grant_id || connectorInfo[x.id]?.storage_cleanup_pending" class="text-button danger" :disabled="busy || disabled" @click="disconnectConnector(x.id)">Disconnect connector</button>
          <section v-if="connectorId === x.id">
            <p class="setting-note">Access is granted only to {{ x.config.url || x.config.command }}. Connector calls still require native review.</p>
            <label>Authentication<select v-model="connectorMode" @change="connectorReview = null; connectorForm.token = ''">
              <template v-if="x.config.transport !== 'stdio'"><option value="oauth">OAuth · public client</option><option value="bearer">Manual bearer credential</option></template>
              <option v-else value="environment">One local credential environment variable</option>
            </select></label>
            <template v-if="connectorMode === 'oauth'">
              <button class="secondary-button" :disabled="busy || disabled" @click="discoverConnector">Discover sign-in metadata</button>
              <template v-if="connectorDiscovery">
                <p class="setting-note">Server metadata is untrusted. Review the issuer and requested scopes before sign-in.</p>
                <label>Authorization server<select v-model="connectorForm.issuer"><option v-for="issuer in connectorDiscovery.authorization_servers" :key="issuer" :value="issuer">{{ issuer }}</option></select></label>
                <label>Client registration<select v-model="connectorRegistrationMethod">
                  <option value="pre_registered">Use a registered public client</option>
                  <option value="client_id_metadata">Use an existing HTTPS client metadata document</option>
                  <option value="dynamic_registration">Review a new public-client registration</option>
                </select></label>
                <label v-if="connectorRegistrationMethod === 'pre_registered'">Registered public client ID<input v-model="connectorForm.client_id" maxlength="200" autocomplete="off" /></label>
                <template v-if="connectorRegistrationMethod === 'client_id_metadata'">
                  <label>Existing HTTPS client metadata URL<input v-model="connectorForm.client_id" maxlength="2000" autocomplete="off" /></label>
                  <label>Optional supplied metadata JSON<textarea v-model="connectorClientMetadata" maxlength="64000" rows="4" :spellcheck="false" /></label>
                  <p class="setting-note">Pebble validates the supplied document or fetches your existing HTTPS reference. Public hosting must already exist. Its exact loopback callback must be available.</p>
                </template>
                <p v-if="connectorRegistrationMethod === 'dynamic_registration'" class="setting-note">A new client is created only after you approve the exact registration endpoint, callback and request below. The issuer must support public clients.</p>
                <p v-if="connectorDiscovery.unresolved_registrations?.length" class="setting-note">A prior registration has an unknown outcome. Check the issuer and use an existing registered client ID; Pebble will not repeat that registration.</p>
                <label>Requested scopes (space separated)<input v-model="connectorForm.scopes" maxlength="2000" /></label>
                <button class="secondary-button" :disabled="busy || disabled" @click="previewConnector">Review sign-in destinations</button>
              </template>
              <template v-if="connectorReview">
                <pre>{{ JSON.stringify(connectorReview, null, 2) }}</pre>
                <button v-if="!connectorRegistrationApproved" class="secondary-button" :disabled="busy || disabled" @click="approveConnectorRegistration">{{ connectorReview.registration_method === 'dynamic_registration' ? 'Approve this registration request' : 'Approve these client details' }}</button>
                <button v-else class="secondary-button" :disabled="busy || disabled" @click="startConnector">Approve reviewed access and sign in</button>
              </template>
              <button class="text-button" @click="cancelConnector">Cancel connector sign-in</button>
            </template>
            <template v-else>
              <label v-if="connectorMode === 'environment'">Credential variable name<input v-model="connectorForm.name" maxlength="64" autocomplete="off" /></label>
              <p v-if="connectorMode === 'environment'" class="setting-note">This credential is placed in the local server's environment while it runs. Other local commands that run as your user can read a running program's environment, and macOS has no sandbox rule that prevents it. Use a scoped, revocable credential.</p>
              <label v-else>Reviewed scopes (space separated)<input v-model="connectorForm.scopes" maxlength="2000" /></label>
              <label>Connector credential<input v-model="connectorForm.token" type="password" maxlength="8192" autocomplete="off" :spellcheck="false" /></label>
              <button class="secondary-button" :disabled="busy || disabled || !connectorForm.token" @click="saveConnectorSecret">Save and grant access to this destination</button>
              <ConnectorRouteSettings v-if="connectorMode === 'environment'" :plugin-id="x.id" :info="connectorInfo[x.id]" :disabled="busy || disabled" @saved="loadExtensions()" />
            </template>
            <p v-if="connectorMessage" role="status">{{ connectorMessage }}</p>
          </section>
        </template>
        <template v-if="x.kind === 'skill' && x.config?.bundle_digest">
          <p class="setting-note">Snapshot {{ x.config.bundle_digest }}. Use /skill "{{ x.name }}" followed by your task.</p>
          <div v-for="file in x.config.files" :key="file.path">
            <small>{{ file.path }} · {{ file.bytes }} bytes · {{ file.sha256 }}</small>
            <button v-if="file.path !== x.config.main && /\.(md|txt|json|yaml|yml|py|js|ts|svg)$/i.test(file.path)"
              class="text-button" :disabled="busy" @click="inspectReference(x.id, x.config.bundle_digest, file.path)">View text</button>
          </div>
          <template v-if="extensionPreview[x.id]">
            <p class="setting-note">{{ extensionPreview[x.id].path }} · imported snapshot; scripts are viewed as text.</p>
            <pre>{{ extensionPreview[x.id].untrusted_source }}</pre>
            <button v-if="extensionPreview[x.id].next_offset !== null" class="text-button" :disabled="busy"
              @click="inspectReference(x.id, x.config.bundle_digest, extensionPreview[x.id].path, extensionPreview[x.id].next_offset)">Load next page</button>
          </template>
        </template>
      </details>
      <div class="setting-actions">
        <button
          class="secondary-button"
          :disabled="busy || disabled"
          @click="extensionToggle(x.id, x.enabled)"
        >
          {{ x.enabled ? "Disable" : "Enable" }}</button
        ><button
          class="text-button danger"
          :disabled="busy || disabled"
          @click="extensionRemove(x.id)"
        >
          Remove
        </button>
      </div>
    </article>
    <div class="setting-actions">
      <button class="secondary-button" @click="importSkill">
        <PhFolder :size="14" />Import SKILL.md
      </button>
    </div>
    <section class="package-panel">
      <div class="section-label">CONNECTOR PACKAGES</div>
      <p class="setting-note">Review a versioned JSON package before installation. Prerequisite files are checked by their exact hashes. Provision missing prerequisites separately; package review runs no installer.</p>
      <button class="secondary-button" :disabled="packageBlocked" @click="newPackage()">Review a new connector package</button>
      <p v-if="!state.settings.permissions.tools.plugins" class="setting-note">Turn on “Use connectors you enabled” in Permissions to review or apply packages.</p>
      <template v-if="packageOpen">
        <form class="provider-form" @submit.prevent="previewPackage">
          <p class="setting-note">{{ packageTarget ? 'Review an update for ' + (packageInfo[packageTarget]?.manifest.name || packageTarget) : 'Review a new disabled installation' }}.</p>
          <label>Complete package JSON<textarea v-model="packageText" rows="7" maxlength="96000" :spellcheck="false"
            placeholder="Paste the complete version 1 bundle with manifest, explicit dependency file hashes and canonical digest."
            @input="packageReview = null" /></label>
          <div class="setting-actions">
            <button class="secondary-button" :disabled="packageBlocked || !packageText.trim()">Review pasted package</button>
            <button class="secondary-button" type="button" :disabled="packageBlocked" @click="importPackage"><PhFolder :size="14" />Review local JSON file</button>
            <button class="text-button" type="button" @click="packageReview = null; packageOpen = false">Close package review</button>
          </div>
        </form>
        <section v-if="packageReview" class="package-review" aria-label="Exact connector package review">
          <strong>{{ packageReview.operation === 'rollback' ? 'Reviewed rollback' : packageReview.operation === 'update' ? 'Reviewed update' : 'Reviewed installation' }} · {{ packageReview.manifest.name }} · {{ packageReview.manifest.release }}</strong>
          <p class="setting-note">Trust: {{ packageReview.trust }}. The checksum identifies these bytes; publisher identity is unverified. Capabilities and recipe descriptions grant no permission. Review expires in five minutes.</p>
          <pre>{{ JSON.stringify(packageReview, null, 2) }}</pre>
          <p v-if="packageReview.operation !== 'install'" class="setting-note">Applying this revision disconnects current connector access. Rollback never restores an earlier credential grant.</p>
          <button class="secondary-button" :disabled="packageBlocked" @click="applyPackage">{{ packageReview.operation === 'rollback' ? 'Apply reviewed rollback disabled' : packageReview.operation === 'update' ? 'Apply reviewed update disabled' : 'Install reviewed package disabled' }}</button>
        </section>
        <p v-if="packageMessage" class="setting-note" role="status">{{ packageMessage }}</p>
      </template>
    </section>
    <form class="provider-form" @submit.prevent="installExtension">
      <label
        >Add<select v-model="extensionType">
          <option value="skill">Skill · reusable task instructions</option>
          <option value="plugin">
            Connector · an MCP server, over HTTP or as a local program
          </option>
          <option value="hook">Hook (advanced) · rules that run at task steps</option>
        </select></label
      ><label
        >Name<input v-model="extensionName" required maxlength="80" /></label
      ><label v-if="extensionType === 'plugin'"
        >MCP protocol era<select v-model="extensionEra"><option value="legacy">Legacy · session-based MCP</option><option value="modern">Modern · stateless MCP</option></select></label
      ><label v-if="extensionType === 'plugin'"
        >MCP endpoint (HTTP servers)<input
          v-model="extensionUrl"
          type="url"
          placeholder="http://localhost:3000/mcp or https://…/mcp" /></label
      ><p v-if="extensionType === 'plugin'" class="setting-note">{{ LOCAL_SERVER_SCOPE }} For a local program, leave the endpoint empty and set transport: "stdio", an absolute command, args and pinned read_files in the manifest. Local programs need file and terminal permissions; network and child processes are blocked.</p
      ><label
        >{{
          extensionType === "skill" ? "Skill instructions" : "Manifest (JSON)"
        }}<textarea
          v-model="extensionText"
          rows="4"
          required
          :placeholder="
            extensionType === 'hook'
              ? 'Example: {&quot;event&quot;:&quot;before_command&quot;,&quot;deny_patterns&quot;:[&quot;docker system prune&quot;]}'
              : extensionType === 'plugin'
                ? 'Example: {&quot;tools&quot;:[&quot;search_docs&quot;],&quot;read_only&quot;:true}'
                : 'When to use this skill and steps to follow…'
          "
        /></label
      ><button class="secondary-button" :disabled="busy || disabled">
        Install disabled for review
      </button>
    </form>
    <div class="section-label tool-label">DEVELOPER FEATURES</div>
    <button class="setting-row" type="button" role="switch" :aria-checked="developer" @click="toggleDeveloper">
      <div>
        <strong>Developer features</strong
        ><small>Shows Views, where you review display code (mods) that draws panels in conversations.</small>
      </div>
      <span class="toggle" :class="{ on: developer }" aria-hidden="true" />
    </button>
  </section>
  </div>
</template>
<style scoped>
.settings-tabs {
  display: flex;
  flex-wrap: wrap;
  gap: 3px;
  padding: 4px;
  background: var(--hover);
  border-radius: 10px;
  margin: 16px 0;
}
.settings-tabs button {
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 6px;
  flex: 1 0 auto;
  border-radius: 7px;
  padding: 8px 10px;
  font-size: 11px;
  white-space: nowrap;
  color: var(--muted);
}
.settings-tabs button.selected {
  background: var(--bg);
  box-shadow: 0 1px 3px #0001;
  color: var(--text);
}
.advanced-section {
  margin: 18px 0 24px;
}
.profile-row {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 4px;
  border: 1px solid var(--border);
  border-radius: 10px;
  margin: 8px 0;
  padding: 5px;
}
.profile-row.selected {
  border-color: var(--accent);
}
.profile-row.just-connected {
  box-shadow: 0 0 0 2px var(--accent);
}
.profile-row > button:first-child {
  flex: 1;
  display: flex;
  align-items: center;
  gap: 10px;
  text-align: left;
  padding: 8px;
  min-width: 0;
}
.profile-row span {
  min-width: 0;
  flex: 1;
}
.profile-row strong,
.extension-card strong {
  display: block;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.profile-row small,
.extension-card small {
  display: block;
  color: var(--muted);
  font-size: 10px;
  line-height: 1.6;
  margin-top: 3px;
}
.selected-dot {
  width: 6px;
  height: 6px;
  flex: none;
  border-radius: 50%;
  background: var(--accent);
}
.signin-status {
  font-size: 12px;
  line-height: 1.5;
  margin: 8px 0;
  padding: 10px;
  border: 1px solid var(--border);
  border-radius: 8px;
}
.signin-status.failed {
  background: var(--danger-bg);
  border-color: var(--danger-line);
  color: var(--danger);
}
.signin-status p {
  margin: 0 0 6px;
}
.signin-link {
  display: flex;
  gap: 6px;
  align-items: center;
  margin: 6px 0;
}
.signin-link input {
  flex: 1;
  min-width: 0;
  font-size: 11px;
  padding: 6px 8px;
  border: 1px solid var(--border);
  border-radius: 6px;
  color: var(--text);
  background: var(--bg);
}
.provider-form {
  display: flex;
  flex-direction: column;
  gap: 12px;
  padding: 16px 0;
}
.package-panel {
  border-top: 1px solid var(--border);
  margin-top: 22px;
  padding-top: 20px;
}
.package-review {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
}
.package-review strong {
  display: block;
  font-size: 12px;
  overflow-wrap: anywhere;
}
.package-review pre {
  max-height: 380px;
  overflow: auto;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  font-size: 10px;
  line-height: 1.6;
  margin: 12px 0;
}
.package-history-row {
  border-bottom: 1px solid var(--border);
  padding: 7px 0;
  overflow-wrap: anywhere;
}
.provider-form label {
  display: flex;
  flex-direction: column;
  gap: 6px;
  font-size: 11px;
}
.provider-form input:not([type="checkbox"]),
.provider-form select,
.provider-form textarea {
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 9px 10px;
  font-size: 12px;
  width: 100%;
  min-width: 0;
}
.provider-form textarea {
  resize: vertical;
  line-height: 1.5;
}
.form-inline {
  display: flex;
  gap: 12px;
  align-items: center;
}
.form-inline > label {
  flex: 1;
}
.form-inline .checkbox-label {
  flex-direction: row;
  align-items: center;
}
.optional {
  color: var(--muted);
  font-size: 10px;
}
.settings-error {
  background: var(--danger-bg);
  border: 1px solid var(--danger-line);
  color: var(--danger);
  padding: 10px;
  border-radius: 8px;
  font-size: 12px;
  line-height: 1.5;
}
.permission-choice {
  display: flex;
  gap: 12px;
  align-items: center;
  text-align: left;
  padding: 12px;
  border: 1px solid var(--border);
  border-radius: 10px;
  width: 100%;
  margin-top: 8px;
}
.permission-choice i {
  width: 15px;
  height: 15px;
  border: 1px solid var(--border);
  border-radius: 50%;
  flex: none;
}
.permission-choice.selected {
  border-color: var(--accent);
  background: var(--hover);
}
.permission-choice.selected i {
  border: 4px solid var(--accent);
  background: var(--bg);
}
.permission-choice strong {
  font-size: 12px;
}
.permission-choice small {
  display: block;
  color: var(--muted);
  font-size: 11px;
  line-height: 1.6;
  margin-top: 4px;
}
.setting-row small {
  display: block;
}
.tool-label {
  margin-top: 24px;
}
.access-note {
  font-size: 11px;
  line-height: 1.6;
  color: var(--danger);
  padding: 10px 3px;
}
.memory-types {
  display: grid;
  grid-template-columns: repeat(2, 1fr);
  gap: 6px;
  margin: 16px 0;
}
.memory-types button {
  display: flex;
  justify-content: space-between;
  text-align: left;
  gap: 8px;
  border: 1px solid var(--border);
  border-radius: 8px;
  padding: 9px 10px;
  font-size: 11px;
}
.memory-types button.selected {
  border-color: var(--accent);
  background: var(--hover);
}
.memory-types small {
  color: var(--muted);
}
.memory-search {
  display: flex;
  gap: 6px;
  align-items: center;
  margin: 10px 0;
}
.memory-search input {
  flex: 1;
  min-width: 0;
  color: var(--text);
  background: var(--bg);
  border: 1px solid var(--border);
  border-radius: 7px;
  padding: 7px 9px;
  font-size: 12px;
}
.memory-items {
  max-height: 250px;
  overflow: auto;
}
.memory-items article {
  display: flex;
  gap: 7px;
  align-items: flex-start;
  border-bottom: 1px solid var(--border);
  padding: 10px 0;
}
.memory-items article div {
  flex: 1;
  min-width: 0;
}
.memory-items small {
  font-size: 10px;
  color: var(--muted);
}
.memory-items p {
  font-size: 11px;
  line-height: 1.6;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  margin: 5px 0;
}
.empty-memory {
  font-size: 12px;
  color: var(--muted);
  padding: 12px 0;
}
.extension-card {
  border: 1px solid var(--border);
  border-radius: 10px;
  padding: 12px;
  margin: 10px 0;
}
.extension-card details {
  margin: 10px 0;
  font-size: 11px;
}
.extension-card pre {
  font-size: 10px;
  white-space: pre-wrap;
  overflow-wrap: anywhere;
  max-height: 180px;
  overflow: auto;
  padding: 9px;
  background: var(--bg);
  border-radius: 5px;
}
</style>
<style scoped>
.chatgpt-sign-in {
  margin-top: 18px;
  min-height: 44px;
  border-radius: 10px;
  padding: 0 18px;
  background: var(--text);
  color: var(--surface);
  font-weight: 600;
  border: 1px solid var(--text);
}
.text-button {
  text-decoration: underline;
  color: var(--text);
  background: none;
}
</style>

<style scoped>
.model-catalogue {
  display: grid;
  gap: 9px;
  padding: 13px;
  border: 1px solid var(--border);
  border-radius: 12px;
}
.catalogue-summary {
  display: flex;
  align-items: center;
  gap: 12px;
  font-size: 12px;
}
.catalogue-summary span {
  margin-right: auto;
  color: var(--muted);
}
.catalogue-options {
  max-height: 190px;
  overflow: auto;
  display: grid;
  gap: 4px;
}
.catalogue-option {
  display: flex !important;
  flex-direction: row !important;
  align-items: center;
  gap: 10px;
  padding: 8px;
  border-radius: 7px;
}
.catalogue-option input {
  width: 16px !important;
}
.catalogue-option small {
  display: block;
  color: var(--muted);
  font-size: 11px;
  margin-top: 3px;
}
.catalogue-option:hover {
  background: var(--surface);
}
</style>
