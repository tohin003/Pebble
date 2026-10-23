<script setup lang="ts">
import { computed, nextTick, onMounted, ref, watch, onUnmounted } from "vue";
import {
  PhX,
  PhPlay,
  PhArrowLeft,
  PhArrowRight,
  PhCode,
  PhCheckCircle,
} from "@phosphor-icons/vue";
import { rpc, native } from "../bridge";
import NativeArtifactRenderer from "./NativeArtifactRenderer.vue";
import QuizRenderer from "./QuizRenderer.vue";
import DashboardRenderer from "./DashboardRenderer.vue";
import PrototypeRenderer from "./PrototypeRenderer.vue";
import type { Artifact, ArtifactAnswers, ArtifactFormQuestion, DashboardArtifact, NativeArtifact, PrototypeArtifact, PrototypeFeedback, QuizV2Artifact } from "../types";
const props = defineProps<{
  artifacts: Artifact[];
  selected?: string;
  embedded?: boolean;
  // The pet passes "pet": quiz sharing then points to the workspace instead.
  surface?: "main" | "pet";
}>();
// Phase 24 (V1-127): the header is the view's title, its version, Refresh, Export…, Copy and Close; the revision,
// owner, status, Stop and integrity facts sit under Details. `export` only asks the window to open its export
// section for this view (ExportPanel there makes the requests); Copy writes the view's own data as text.
const emit = defineEmits<{ close: []; workspace: []; feedback: [payload: PrototypeFeedback]; export: [id: string] }>();
const panelSurface = computed(() => props.surface || (props.embedded ? "pet" : "main"));
// Phase 30 (D42): the pet's window may not stop a view, send a form's answers, open a static preview or read connector
// media (main.rs, the pet's allowlist). In the pet those views point to the workspace instead of failing there.
const inPet = computed(() => panelSurface.value === "pet");
const selected = ref(props.selected || props.artifacts.at(-1)?.id || "");
const listedActive = computed(() =>
  props.artifacts.find((a) => a.id === selected.value),
);
const loadedArtifact = ref<Artifact | null>(null);
const revisions = ref<Artifact[]>([]);
const revisionChoice = ref("current");
const artifactLoading = ref(false), artifactError = ref(""), artifactBusy = ref(false), formError = ref("");
let loadGeneration = 0, viewGeneration = 0;
const active = computed(() => {
  const row = listedActive.value;
  return loadedArtifact.value?.id === row?.id && loadedArtifact.value?.chat_id === row?.chat_id ? loadedArtifact.value : row;
});
const currentRevision = computed(() => Math.max(listedActive.value?.revision || 1, ...revisions.value.map(row => row.revision || 1)));
const historical = computed(() => revisionChoice.value !== "current");
const nativeArtifact = computed(() => active.value && ["form", "table", "chart", "plan", "evidence"].includes(active.value.kind) ? active.value as NativeArtifact : null);
const dashboard = computed(() => active.value?.kind === "dashboard" ? active.value as DashboardArtifact : null);
const prototype = computed(() => active.value?.kind === "prototype" ? active.value as PrototypeArtifact : null);
// v2 questions carry an ID and no answer key (the host grades); v1 questions carry the key
// and are scored locally as a labelled self-check. Anything else is refused.
function keys(value: unknown, names: string[]) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const row = value as Record<string, unknown>;
  return Object.keys(row).length === names.length && names.every((name) => Object.hasOwn(row, name));
}
const quizMode = computed(() => {
  if (active.value?.kind !== "quiz") return null;
  const rows = active.value.data?.questions;
  if (!Array.isArray(rows) || !rows.length) return "invalid";
  if (rows.every((q) => keys(q, ["id", "question", "choices"]))) return "v2";
  return rows.every((q) => keys(q, ["question", "choices", "answer", "explanation"]) && Array.isArray(q.choices) && Number.isInteger(q.answer) && q.answer >= 0 && q.answer < q.choices.length) ? "v1" : "invalid";
});
const quizV2 = computed(() => quizMode.value === "v2" ? active.value as QuizV2Artifact : null);
const artifactLive = computed(() => !!active.value && !["stopped", "expired"].includes(active.value.status || ""));
const width = ref(440),
  answers = ref<(number | null)[]>([]),
  index = ref(0),
  started = ref<number | null>(null),
  ended = ref(false),
  now = ref(Date.now());
const customUrl = ref(""),
  customError = ref(""),
  customBusy = ref(false),
  showSource = ref(false),
  customExpiresAt = ref<number | null>(null);
const hasGeneratedScript = computed(() => active.value?.kind === "html" && !!active.value.data.script);
const connectorMedia = ref<any>(null);
async function downloadConnectorMedia() {
  if (inPet.value || !active.value || active.value.kind !== 'mcp_media') return;
  try {
    const d=active.value.data;
    const result=await rpc('media_download',{chat_id:active.value.chat_id,run_id:d.run_id,snapshot_id:d.snapshot_id});
    const bytes=Uint8Array.from(atob(result.data),(c)=>c.charCodeAt(0));
    const url=URL.createObjectURL(new Blob([bytes],{type:'application/octet-stream'}));
    const link=document.createElement('a');link.href=url;link.download=result.filename;link.click();
    setTimeout(()=>URL.revokeObjectURL(url),1000);
  } catch(e) { customError.value=String(e); }
}
const timer = setInterval(() => {
  now.value = Date.now();
  if (started.value && remaining.value <= 0) ended.value = true;
  if (customExpiresAt.value && now.value >= customExpiresAt.value) {
    stopCustom();
    customError.value = "This static preview has expired. Open it again to continue viewing.";
  }
}, 1000);
const questions = computed(() => quizMode.value === "v1" && active.value?.kind === "quiz" ? active.value.data.questions : []);
const remaining = computed(() =>
  Math.max(
    0,
    (active.value?.kind === "quiz" ? active.value.data.duration_minutes || 30 : 30) * 60 -
      (started.value ? Math.floor((now.value - started.value) / 1000) : 0),
  ),
);
const clock = computed(
  () =>
    `${Math.floor(remaining.value / 60)
      .toString()
      .padStart(2, "0")}:${(remaining.value % 60).toString().padStart(2, "0")}`,
);
const answered = computed(() => answers.value.filter((a) => a !== null).length);
const score = computed(
  () =>
    questions.value.filter((q: any, i: number) => answers.value[i] === q.answer)
      .length,
);
const stateKey = computed(() =>
  active.value ? "pebble.quiz." + active.value.id + ((active.value.revision || 1) > 1 ? "." + active.value.revision : "") : "",
);
watch(
  () => props.selected,
  (id) => {
    if (id) selected.value = id;
  },
);
watch(
  () => props.artifacts,
  () => {
    if (!listedActive.value) selected.value = props.artifacts.at(-1)?.id || "";
  },
);
async function stopCustom() {
  viewGeneration++;
  const old = customUrl.value;
  customUrl.value = "";
  customExpiresAt.value = null;
  if (old && native)
    try {
      await rpc("view_stop", { url: old });
    } catch {}
}
async function loadArtifact() {
  const row = listedActive.value, generation = ++loadGeneration;
  artifactLoading.value = !!row && native;
  await stopCustom();
  if (generation !== loadGeneration) return;
  artifactError.value = "";
  formError.value = "";
  if (!row) { loadedArtifact.value = null; revisions.value = []; artifactLoading.value = false; return; }
  if (!native) { loadedArtifact.value = row; revisions.value = [row]; artifactLoading.value = false; return; }
  artifactLoading.value = true;
  const revision = historical.value ? Number(revisionChoice.value) : undefined;
  try {
    const [snapshot, history] = await Promise.all([
      rpc<Artifact>("artifact_get", { chat_id: row.chat_id, id: row.id, ...(revision === undefined ? {} : { revision }) }),
      rpc<Artifact[]>("artifact_history", { chat_id: row.chat_id, id: row.id }),
    ]);
    if (generation !== loadGeneration) return;
    if (snapshot.id !== row.id || snapshot.chat_id !== row.chat_id || !Array.isArray(history) || history.some(item => item.id !== row.id || item.chat_id !== row.chat_id || !Number.isInteger(item.revision))) throw new Error("Invalid artifact revision response.");
    loadedArtifact.value = snapshot;
    revisions.value = [...history].sort((a, b) => (a.revision || 1) - (b.revision || 1));
  } catch (error) {
    if (generation === loadGeneration) artifactError.value = String(error);
  } finally {
    if (generation === loadGeneration) artifactLoading.value = false;
  }
}
let artifactIdentity = "";
watch(() => {
  const row = listedActive.value;
  return JSON.stringify([row?.chat_id, row?.id, row?.revision, row?.status, row?.form_question?.status]);
}, () => {
  const identity = listedActive.value ? `${listedActive.value.chat_id}:${listedActive.value.id}` : "";
  if (identity !== artifactIdentity) {
    artifactIdentity = identity;
    revisionChoice.value = "current";
    loadedArtifact.value = null;
    revisions.value = [];
  }
  loadArtifact();
}, { immediate: true });
watch(revisionChoice, () => loadArtifact());
async function stopArtifact() {
  const row = active.value;
  if (!row || artifactBusy.value || artifactLoading.value || historical.value || !artifactLive.value || !native) return;
  artifactBusy.value = true;
  artifactError.value = "";
  try {
    const snapshot = await rpc<Artifact>("artifact_stop", { chat_id: row.chat_id, id: row.id, revision: row.revision || 1 });
    if (listedActive.value?.id === row.id && listedActive.value?.chat_id === row.chat_id) {
      loadedArtifact.value = snapshot;
      await loadArtifact();
    }
  } catch (error) { if (listedActive.value?.id === row.id) artifactError.value = String(error); }
  finally { artifactBusy.value = false; }
}
// Refresh data is a read: artifact_refresh re-resolves the current revision's host bindings (behind the host's
// two-second cache) and writes nothing, so no revision is added and the owning run's revision never moves.
async function refreshDashboard() {
  const row = active.value;
  if (!row || row.kind !== "dashboard" || artifactBusy.value || artifactLoading.value || historical.value || !artifactLive.value || !native) return;
  artifactBusy.value = true;
  formError.value = "";
  try {
    await rpc("artifact_refresh", { chat_id: row.chat_id, id: row.id, revision: row.revision || 1 });
    if (listedActive.value?.id === row.id && listedActive.value?.chat_id === row.chat_id) await loadArtifact();
  } catch (error) { if (listedActive.value?.id === row.id) formError.value = String(error); }
  finally { artifactBusy.value = false; }
}
async function submitForm(answers: ArtifactAnswers) {
  const row = active.value;
  if (!row || row.kind !== "form" || artifactBusy.value || artifactLoading.value || historical.value || !artifactLive.value || row.form_question?.status !== "pending" || !native) return;
  artifactBusy.value = true;
  formError.value = "";
  try {
    const reply = await rpc<{ status: ArtifactFormQuestion["status"] }>("artifact_submit", { chat_id: row.chat_id, id: row.id, revision: row.revision || 1, answers });
    if (listedActive.value?.id === row.id && listedActive.value?.chat_id === row.chat_id) {
      loadedArtifact.value = { ...row, form_question: { ...row.form_question, status: reply.status, answer: answers } };
      await loadArtifact();
    }
  } catch (error) { if (listedActive.value?.id === row.id) formError.value = String(error); }
  finally { artifactBusy.value = false; }
}
watch(
  active,
  async () => {
    await stopCustom();
    customError.value = "";
    showSource.value = false;
    connectorMedia.value=null;
    if (active.value?.kind==='mcp_media' && !inPet.value) {
      const view=active.value;
      try {
        const d=view.data;
        const result=await rpc('media_artifact',{chat_id:view.chat_id,run_id:d.run_id,snapshot_id:d.snapshot_id});
        if (active.value?.id===view.id) connectorMedia.value=result;
      } catch(e) { if (active.value?.id===view.id) customError.value=String(e); }
    }
    index.value = 0;
    started.value = null;
    ended.value = false;
    answers.value = questions.value.map(() => null);
    if (quizMode.value === "v1")
      try {
        const s = JSON.parse(localStorage.getItem(stateKey.value) || "null");
        if (
          s &&
          Array.isArray(s.answers) &&
          s.answers.length === questions.value.length &&
          s.answers.every(
            (a: any, i: number) =>
              a === null ||
              (Number.isInteger(a) &&
                a >= 0 &&
                a < questions.value[i].choices.length),
          ) &&
          (s.started === null ||
            (typeof s.started === "number" && s.started <= Date.now()))
        ) {
          answers.value = s.answers;
          started.value = s.started;
          ended.value = !!s.ended;
        }
      } catch {}
  },
  { immediate: true },
);
watch(
  [answers, started, ended],
  () => {
    if (quizMode.value === "v1")
      try {
        localStorage.setItem(
          stateKey.value,
          JSON.stringify({
            answers: answers.value,
            started: started.value,
            ended: ended.value,
          }),
        );
      } catch {}
  },
  { deep: true },
);
function start() {
  now.value = Date.now();
  started.value = now.value;
  ended.value = false;
}
function resizeKey(delta: number) {
  width.value = Math.max(
    320,
    Math.min(
      (document.querySelector(".workspace-body")?.clientWidth ||
        window.innerWidth) - 340,
      width.value + delta,
    ),
  );
}
function resize(e: PointerEvent) {
  const origin = e.clientX,
    size = width.value;
  const target = e.currentTarget as HTMLElement;
  target.setPointerCapture(e.pointerId);
  const move = (p: PointerEvent) => {
    width.value = Math.max(
      320,
      Math.min(
        Math.max(
          320,
          (document.querySelector(".workspace-body")?.clientWidth ||
            window.innerWidth) - 340,
        ),
        size + origin - p.clientX,
      ),
    );
  };
  const end = () => {
    target.removeEventListener("pointermove", move);
    target.removeEventListener("pointerup", end);
  };
  target.addEventListener("pointermove", move);
  target.addEventListener("pointerup", end);
}
async function runCustom() {
  const view = active.value;
  if (!view || view.kind !== "html" || customBusy.value || artifactLoading.value || !artifactLive.value || !native) return;
  if (hasGeneratedScript.value) {
    customError.value = "Generated scripts cannot run here. Use native widgets or reviewed Mods for interactive behavior.";
    return;
  }
  customBusy.value = true;
  customError.value = "";
  const closing = stopCustom();
  const generation = viewGeneration;
  try {
    await closing;
    if (generation !== viewGeneration) return;
    const result = await rpc<{ url: string; expires_at?: number }>("artifact_view_open", { chat_id: view.chat_id, id: view.id, revision: view.revision || 1 });
    const url = new URL(result.url);
    if (url.protocol !== "http:" || url.hostname !== "127.0.0.1" || !/^\/view\/[A-Za-z0-9_-]+$/.test(url.pathname)) throw new Error("Invalid static preview address.");
    if (generation !== viewGeneration) { await rpc("view_stop", { url: result.url }); return; }
    customUrl.value = result.url;
    customExpiresAt.value = typeof result.expires_at === "number" && Number.isFinite(result.expires_at) ? result.expires_at * 1000 : null;
  } catch (e) {
    if (generation === viewGeneration) customError.value = String(e);
  } finally {
    customBusy.value = false;
  }
}
// V1-127: Copy writes the view as text: a table as tab-separated rows (pastes into a spreadsheet), anything else as
// its title and data. A read of what is already on screen; no request.
const copyNote = ref("");
let copyTimer: ReturnType<typeof setTimeout> | null = null;
function viewText(view: Artifact): string {
  const data = view.data as Record<string, any>;
  if (view.kind === "table" && Array.isArray(data?.columns) && Array.isArray(data?.rows)) {
    const cell = (value: unknown) => String(value ?? "").replace(/[\t\r\n]+/g, " ");
    const columns = data.columns as { id: string; label: string }[];
    return [columns.map((c) => cell(c.label)).join("\t"), ...data.rows.map((row: Record<string, unknown>) => columns.map((c) => cell(row?.[c.id])).join("\t"))].join("\n");
  }
  return view.title + "\n\n" + JSON.stringify(view.data, null, 2);
}
async function copyView() {
  const view = active.value;
  if (!view) return;
  try {
    await navigator.clipboard.writeText(viewText(view));
    copyNote.value = "Copied";
  } catch {
    copyNote.value = "Couldn't copy";
  }
  if (copyTimer !== null) clearTimeout(copyTimer);
  copyTimer = setTimeout(() => {
    copyTimer = null;
    copyNote.value = "";
  }, 2000);
}
// V1-129: opening the canvas moves focus to its title, so a keyboard user starts inside it.
const titleEl = ref<HTMLElement | null>(null);
onMounted(() => {
  if (!props.embedded) nextTick(() => titleEl.value?.focus?.());
});
const nodes = computed(() => active.value?.kind === "diagram" ? active.value.data.nodes || [] : []);
function position(id: string) {
  const i = nodes.value.findIndex((n: any) => n.id === id);
  return { x: 170, y: 55 + i * 105 };
}
onUnmounted(() => {
  loadGeneration++;
  clearInterval(timer);
  if (copyTimer !== null) clearTimeout(copyTimer);
  stopCustom();
});
</script>
<template>
  <aside
    class="artifact-panel"
    :class="{ embedded }"
    :style="{ width: embedded ? '100%' : width + 'px' }"
    aria-label="Interactive canvas"
  >
    <div
      v-if="!embedded"
      class="canvas-resizer"
      role="separator"
      aria-label="Resize canvas"
      aria-orientation="vertical"
      tabindex="0"
      @pointerdown="resize"
      @keydown.left.prevent="resizeKey(30)"
      @keydown.right.prevent="resizeKey(-30)"
    ></div>
    <header v-if="!embedded" class="canvas-header">
      <div class="canvas-heading">
        <h2 ref="titleEl" class="canvas-title" tabindex="-1">{{ active?.title || "Views" }}</h2>
        <span v-if="active" class="canvas-version">{{ historical ? "Earlier version" : "Version" }} {{ active.revision || 1 }}</span>
      </div>
      <div class="canvas-actions">
        <button type="button" class="secondary-button" :disabled="!active || artifactLoading || artifactBusy || !native" @click="loadArtifact">Refresh</button>
        <button type="button" class="secondary-button" :disabled="!active" @click="active && emit('export', active.id)">Export…</button>
        <button type="button" class="secondary-button" :disabled="!active" @click="copyView">Copy</button>
        <span class="canvas-copied" role="status">{{ copyNote }}</span>
        <button
          type="button"
          class="icon-button"
          aria-label="Close canvas"
          @click="emit('close')"
        >
          <PhX :size="18" />
        </button>
      </div>
    </header>
    <select
      v-if="artifacts.length > 1 || embedded"
      class="canvas-select"
      v-model="selected"
      aria-label="Choose interactive view"
    >
      <option v-for="a in artifacts" :key="a.id" :value="a.id">
        {{ a.title }}
      </option>
    </select>
    <details v-if="active" class="artifact-ownership" aria-label="Native artifact controls">
      <summary>Details</summary>
      <div class="artifact-revision-line">
        <span>{{ historical ? "Historical" : "Current" }} · revision {{ active.revision || 1 }}</span>
        <span class="artifact-status" role="status">{{ active.status || "ready" }}</span>
      </div>
      <p class="artifact-owner">{{ active.owner_run_id ? 'Owner run: ' + active.owner_run_id : active.kind === 'mcp_media' ? 'Owner: media from a connector' : 'Owner: saved conversation view' }}</p>
      <label class="artifact-revision-label">Revision
        <select v-model="revisionChoice" :disabled="artifactLoading || artifactBusy || !native" aria-label="Choose artifact revision">
          <option value="current">Current · revision {{ currentRevision }}</option>
          <option v-for="snapshot in revisions.filter(row => (row.revision || 1) < currentRevision)" :key="snapshot.revision" :value="String(snapshot.revision)">Revision {{ snapshot.revision }} · {{ snapshot.status || 'ready' }}</option>
        </select>
      </label>
      <div class="artifact-native-actions">
        <button v-if="embedded" class="secondary-button" :disabled="artifactLoading || artifactBusy || !native" @click="loadArtifact">Refresh</button>
        <button v-if="!inPet" class="secondary-button" :disabled="artifactLoading || artifactBusy || historical || !artifactLive || !native" @click="stopArtifact">{{ artifactBusy ? 'Saving…' : 'Stop artifact' }}</button>
      </div>
      <details v-if="active.evidence_ids?.length || active.data_hash" class="artifact-integrity">
        <summary>Evidence and integrity</summary>
        <ul v-if="active.evidence_ids?.length" aria-label="Evidence for this view"><li v-for="receipt in active.evidence_ids" :key="receipt"><span>From step: </span><code>{{ receipt }}</code></li></ul>
        <p v-else>No step evidence is attached.</p>
        <p v-if="active.data_hash">Data SHA256 <code>{{ active.data_hash }}</code></p>
      </details>
    </details>
    <p v-if="artifactLoading" class="artifact-read-message" role="status">Loading artifact revision…</p>
    <p v-if="artifactError" class="artifact-read-message artifact-read-error" role="alert">{{ artifactError }}</p>
    <div v-if="active && !artifactLoading && !artifactError" class="canvas-content">
      <NativeArtifactRenderer v-if="nativeArtifact" :artifact="nativeArtifact" :historical="historical" :disabled="!native || inPet" :disabled-note="inPet ? 'Answer this form in the workspace.' : undefined" :busy="artifactBusy" :error="formError" @submit="submitForm" />
      <button v-if="nativeArtifact && inPet" type="button" class="secondary-button" @click="emit('workspace')">Open in the workspace</button>
      <DashboardRenderer v-if="dashboard" :artifact="dashboard" :historical="historical" :disabled="!native" :busy="artifactBusy" :error="formError" @refresh="refreshDashboard" />
      <PrototypeRenderer v-if="prototype" :artifact="prototype" :historical="historical" @feedback="emit('feedback', $event)" />
      <template v-if="active.kind==='mcp_media'">
        <h2>{{active.title}}</h2>
        <template v-if="inPet">
          <p>Media from a connector opens in the workspace.</p>
          <button type="button" class="secondary-button" @click="emit('workspace')">Open in the workspace</button>
        </template>
        <p v-if="customError" role="alert">{{customError}}</p>
        <template v-if="connectorMedia">
          <img v-if="connectorMedia.rendered && connectorMedia.preview?.startsWith('data:image/png;base64,')" :src="connectorMedia.preview" alt="Untrusted connector image" style="max-width:100%;height:auto" />
          <p>{{connectorMedia.notice}}</p>
          <p>Historical connector evidence · {{connectorMedia.source_bytes}} bytes</p>
          <button class="secondary-button" @click="downloadConnectorMedia">Download attachment</button>
          <details><summary>Source and integrity</summary><pre>{{JSON.stringify(connectorMedia.origin,null,2)}}
SHA256 {{connectorMedia.sha256}}</pre></details>
        </template>
      </template>
      <QuizRenderer v-if="quizV2" :artifact="quizV2" :historical="historical" :surface="panelSurface" @workspace="emit('workspace')" />
      <p v-else-if="quizMode === 'invalid'" class="artifact-read-error" role="alert">This quiz contains invalid display data.</p>
      <template v-else-if="quizMode === 'v1' && active.kind === 'quiz'">
        <p class="self-check-label">Self-check (answers are stored with this view)</p>
        <div class="quiz-meta">
          <span>{{ questions.length }} questions</span
          ><span :class="{ urgent: started && remaining < 120 }">{{
            clock
          }}</span>
        </div>
        <template v-if="!started">
          <div class="quiz-intro">
            <div class="canvas-symbol">✦</div>
            <h2>{{ active.title }}</h2>
            <p>
              A focused practice session.
              {{ active.data.duration_minutes }} minutes,
              {{ questions.length }} multiple-choice questions. This view
              scores itself, and your progress stays on this device; Pebble
              does not see your answers.
            </p>
            <button class="primary-button" @click="start">
              <PhPlay :size="17" weight="fill" />Start test</button
            ><small
              >Questions and answers are generated by AI. Review the
              explanations.</small
            >
          </div>
        </template>
        <template v-else-if="!ended">
          <div class="quiz-track">
            <i
              :style="{ width: (answered / questions.length) * 100 + '%' }"
            ></i>
          </div>
          <div class="question-count">
            QUESTION {{ index + 1 }} OF {{ questions.length }}
          </div>
          <h2 class="question-text">{{ questions[index].question }}</h2>
          <button
            v-for="(choice, i) in questions[index].choices"
            :key="i"
            class="quiz-choice"
            :class="{ chosen: answers[index] === i }"
            @click="answers[index] = Number(i)"
          >
            <span>{{ String.fromCharCode(65 + Number(i)) }}</span
            >{{ choice }}
          </button>
          <div class="quiz-nav">
            <button
              class="secondary-button"
              :disabled="index === 0"
              @click="index--"
            >
              <PhArrowLeft :size="14" />Back</button
            ><span>{{ answered }} answered</span
            ><button
              v-if="index < questions.length - 1"
              class="secondary-button"
              @click="index++"
            >
              Next<PhArrowRight :size="14" /></button
            ><button v-else class="primary-button" @click="ended = true">
              Submit test
            </button>
          </div>
          <div class="question-jumps">
            <button
              v-for="(_, i) in questions"
              :key="i"
              :class="{
                done: answers[Number(i)] !== null,
                current: index === i,
              }"
              :aria-label="'Question ' + (Number(i) + 1)"
              @click="index = Number(i)"
            >
              {{ Number(i) + 1 }}
            </button>
          </div>
        </template>
        <template v-else>
          <div class="quiz-result">
            <PhCheckCircle :size="36" />
            <h2>{{ score }} / {{ questions.length }}</h2>
            <p>
              {{ remaining === 0 ? "Time is up." : "Session completed." }} Your
              answers are saved locally and were not sent to Pebble.
            </p>
          </div>
          <div v-for="(q, i) in questions" :key="i" class="answer-review">
            <small
              >QUESTION {{ Number(i) + 1 }} ·
              {{
                answers[Number(i)] === q.answer
                  ? "CORRECT"
                  : answers[Number(i)] === null
                    ? "UNANSWERED"
                    : "REVIEW"
              }}</small
            ><strong>{{ q.question }}</strong>
            <p>Answer: {{ q.choices[q.answer] }}</p>
            <p class="explanation">{{ q.explanation }}</p>
          </div>
        </template>
      </template>
      <template v-else-if="active.kind === 'diagram'">
        <h2>{{ active.title }}</h2>
        <p class="canvas-note">
          A generated diagram. Read each relationship alongside the explanation
          in chat.
        </p>
        <svg
          class="canvas-diagram"
          :viewBox="`0 0 340 ${Math.max(200, nodes.length * 105 + 30)}`"
          role="img"
          :aria-label="active.title"
        >
          <defs>
            <marker
              id="canvas-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="5"
              markerHeight="5"
              orient="auto-start-reverse"
            >
              <path d="M 0 0 L 10 5 L 0 10 z" fill="currentColor" />
            </marker>
          </defs>
          <g v-for="(edge, i) in active.data.edges" :key="i">
            <path
              :d="`M ${position(edge.from).x} ${position(edge.from).y + 28} Q ${45 + (Number(i) % 4) * 30} ${(position(edge.from).y + position(edge.to).y) / 2} ${position(edge.to).x} ${position(edge.to).y - 28}`"
              fill="none"
              stroke="currentColor"
              stroke-width="1.4"
              marker-end="url(#canvas-arrow)"
            />
            <text
              x="28"
              :y="(position(edge.from).y + position(edge.to).y) / 2"
              class="edge-label"
            >
              {{ edge.label }}
            </text>
          </g>
          <g v-for="n in nodes" :key="n.id">
            <rect
              x="70"
              :y="position(n.id).y - 28"
              width="220"
              height="56"
              rx="12"
            />
            <foreignObject
              x="82"
              :y="position(n.id).y - 23"
              width="196"
              height="46"
              ><div xmlns="http://www.w3.org/1999/xhtml" class="node-label">
                {{ n.label }}
              </div></foreignObject
            >
          </g>
        </svg>
      </template>
      <template v-else-if="active.kind === 'html'">
        <div class="custom-bar">
          <span>Generated static HTML preview</span
          ><button
            class="icon-button"
            aria-label="Inspect view source"
            @click="showSource = !showSource"
          >
            <PhCode :size="18" />
          </button>
        </div>
        <pre v-if="showSource" class="view-source">{{
          JSON.stringify(active.data, null, 2)
        }}</pre>
        <div v-if="!customUrl" class="custom-consent">
          <h2>{{ active.title }}</h2>
          <p>
            Open a static preview of this saved revision. Interactive inputs are available in native forms.
          </p>
          <p v-if="hasGeneratedScript" class="script-blocked" role="alert">
            Generated scripts cannot run here. Use native widgets or reviewed Mods for interactive behavior.
          </p>
          <p v-else-if="!artifactLive">This artifact is {{ active.status }}.</p>
          <button v-if="inPet" type="button" class="secondary-button" @click="emit('workspace')">Open in the workspace</button>
          <button
            v-else
            class="primary-button"
            :disabled="customBusy || !native || hasGeneratedScript || !artifactLive || artifactBusy"
            @click="runCustom"
          >
            <PhPlay :size="15" />{{
              customBusy ? "Preparing…" : "Open static preview"
            }}
          </button>
        </div>
        <div v-if="customError" role="alert">{{ customError }}</div>
        <template v-if="customUrl"
          ><button class="secondary-button" @click="stopCustom">
            Stop view</button
          ><iframe
            :src="customUrl"
            sandbox=""
            referrerpolicy="no-referrer"
            :title="active.title"
            class="custom-frame"
            allow="
              camera 'none';
              microphone 'none';
              geolocation 'none';
              clipboard-read 'none';
              clipboard-write 'none';
            "
          ></iframe
        ></template>
      </template>
    </div>
  </aside>
</template>
<style scoped>
.artifact-panel {
  position: relative;
  flex-shrink: 0;
  max-width: calc(100% - 340px);
  min-width: 320px;
  background: var(--surface);
  border-left: 1px solid var(--line);
  display: flex;
  flex-direction: column;
  min-height: 0;
}
.canvas-resizer {
  position: absolute;
  left: -4px;
  width: 8px;
  top: 0;
  bottom: 0;
  cursor: col-resize;
  z-index: 2;
  touch-action: none;
}
.canvas-resizer:hover {
  background: var(--accent-soft);
}
.canvas-header {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  flex-wrap: wrap;
  gap: 8px 12px;
  padding: 14px 22px 8px;
}
.canvas-heading {
  min-width: 0;
  flex: 1 1 160px;
}
.canvas-title {
  margin: 0;
  font-size: 16px;
  font-weight: 560;
  letter-spacing: -0.2px;
  line-height: 1.35;
  overflow-wrap: anywhere;
}
.canvas-title:focus {
  outline: none;
}
.canvas-title:focus-visible {
  outline: 2px solid var(--focus, var(--accent));
  outline-offset: 2px;
}
.canvas-version {
  display: block;
  margin-top: 2px;
  font-size: 12px;
  color: var(--muted);
}
.canvas-actions {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
}
.canvas-actions .secondary-button {
  min-height: 30px;
  padding: 5px 10px;
  font-size: 12px;
}
.canvas-copied {
  font-size: 12px;
  color: var(--secondary);
}
.canvas-copied:empty {
  display: none;
}
.canvas-select {
  margin: 8px 22px 14px;
  width: calc(100% - 44px);
  padding: 9px 12px;
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: 9px;
  color: var(--text);
  font: inherit;
  font-size: 13px;
}
.canvas-content {
  overflow: auto;
  padding: 14px 24px 28px;
  flex: 1;
}
.artifact-ownership {
  padding: 0 24px 12px;
  border-bottom: 1px solid var(--line);
  font-size: 12px;
  overflow-wrap: anywhere;
}
.artifact-ownership > summary {
  cursor: pointer;
  color: var(--secondary);
  margin-bottom: 8px;
}
.artifact-revision-line {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  color: var(--secondary);
}
.artifact-status {
  border: 1px solid var(--line);
  border-radius: 5px;
  padding: 1px 6px;
  text-transform: capitalize;
}
.artifact-owner {
  color: var(--muted);
  margin: 7px 0 10px;
  font-size: 11px;
}
.artifact-revision-label {
  display: flex;
  align-items: center;
  gap: 8px;
  color: var(--muted);
}
.artifact-revision-label select {
  min-width: 0;
  flex: 1;
  color: var(--text);
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: 6px;
  padding: 5px 8px;
  font: inherit;
}
.artifact-native-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  margin-top: 10px;
}
.artifact-native-actions button {
  padding: 6px 9px;
  font-size: 11px;
}
.artifact-ownership button:disabled,
.artifact-revision-label select:disabled,
.custom-consent button:disabled {
  opacity: .55;
  cursor: default;
}
.artifact-integrity {
  color: var(--muted);
  margin-top: 10px;
  font-size: 11px;
}
.artifact-integrity summary { cursor: pointer; }
.artifact-integrity ul { list-style: none; padding: 0; }
.artifact-integrity li { margin: 5px 0; }
.artifact-integrity code { white-space: pre-wrap; overflow-wrap: anywhere; }
.artifact-read-message { padding: 0 24px; color: var(--muted); font-size: 12px; }
.artifact-read-error, .script-blocked { color: var(--danger, #b64747); }
.self-check-label {
  display: inline-block;
  margin: 0 0 12px;
  padding: 2px 9px;
  border: 1px solid var(--line);
  border-radius: 999px;
  font-size: 11px;
  color: var(--secondary);
  background: var(--soft);
}
.quiz-meta {
  display: flex;
  justify-content: space-between;
  font-size: 12px;
  color: var(--muted);
}
.quiz-meta span:last-child {
  font-variant-numeric: tabular-nums;
  font-weight: 600;
  color: var(--text);
}
.quiz-meta .urgent {
  color: var(--danger);
}
.quiz-intro {
  padding: 44px 0;
  display: flex;
  flex-direction: column;
  align-items: flex-start;
  gap: 22px;
}
.canvas-symbol {
  font-size: 48px;
  color: var(--accent);
}
h2 {
  font-size: 23px;
  line-height: 1.4;
  letter-spacing: -0.5px;
  font-weight: 500;
  margin: 0;
}
.quiz-intro p,
.canvas-note,
.custom-consent p {
  font-size: 13px;
  line-height: 1.8;
  color: var(--secondary);
}
.quiz-intro small {
  font-size: 11px;
  color: var(--muted);
  line-height: 1.6;
}
.quiz-track {
  height: 3px;
  background: var(--line);
  margin: 23px 0 27px;
  border-radius: 10px;
}
.quiz-track i {
  display: block;
  height: 100%;
  background: var(--accent);
  transition: width 0.2s;
}
.question-count {
  font-size: 11px;
  letter-spacing: 1.5px;
  color: var(--muted);
  margin-bottom: 14px;
}
.question-text {
  font-size: 20px;
  margin-bottom: 24px;
}
.quiz-choice {
  display: flex;
  align-items: center;
  gap: 13px;
  text-align: left;
  background: var(--code);
  border: 1px solid var(--line);
  border-radius: 12px;
  width: 100%;
  padding: 15px;
  margin: 10px 0;
  line-height: 1.6;
  color: var(--text);
  font-size: 13px;
}
.quiz-choice span {
  border: 1px solid var(--line-strong);
  border-radius: 6px;
  padding: 2px 7px;
  font-size: 11px;
}
.quiz-choice.chosen {
  border-color: var(--accent);
  background: var(--accent-soft);
}
.quiz-nav {
  display: flex;
  justify-content: space-between;
  align-items: center;
  margin-top: 25px;
}
.quiz-nav span {
  font-size: 11px;
  color: var(--muted);
}
.question-jumps {
  display: flex;
  flex-wrap: wrap;
  gap: 7px;
  margin-top: 28px;
}
.question-jumps button {
  width: 28px;
  height: 28px;
  border-radius: 7px;
  background: var(--code);
  border: 1px solid var(--line);
  font-size: 11px;
  color: var(--muted);
}
.question-jumps .done {
  background: var(--accent-soft);
  color: var(--text);
}
.question-jumps .current {
  border-color: var(--accent);
}
.quiz-result {
  padding: 32px 0;
  color: var(--accent);
}
.quiz-result h2 {
  font-size: 35px;
  margin-top: 10px;
}
.quiz-result p {
  color: var(--secondary);
  font-size: 13px;
  line-height: 1.7;
}
.answer-review {
  border-top: 1px solid var(--line);
  padding: 22px 0;
  font-size: 13px;
  line-height: 1.8;
}
.answer-review small {
  font-size: 11px;
  letter-spacing: 1px;
  color: var(--muted);
}
.answer-review strong {
  display: block;
  margin-top: 9px;
  font-weight: 500;
}
.answer-review p {
  margin: 10px 0;
}
.explanation {
  color: var(--secondary);
}
.custom-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  color: var(--muted);
  font-size: 11px;
}
.custom-consent {
  padding: 35px 0;
}
.custom-frame {
  width: 100%;
  min-height: 450px;
  height: 60vh;
  border: 1px solid var(--line);
  border-radius: 10px;
  background: #f4f6ef;
  margin-top: 15px;
}
.view-source {
  font-size: 11px;
  white-space: pre-wrap;
  word-break: break-word;
  background: var(--code);
  padding: 12px;
  border-radius: 10px;
  max-height: 45vh;
  overflow: auto;
}
.canvas-diagram {
  width: 100%;
  color: var(--muted);
  margin-top: 24px;
}
.canvas-diagram rect {
  fill: var(--code);
  stroke: var(--line-strong);
}
.node-label {
  font-size: 12px;
  color: var(--text);
  height: 46px;
  display: flex;
  align-items: center;
  justify-content: center;
  text-align: center;
  line-height: 1.4;
}
.edge-label {
  font-size: 11px;
  fill: var(--muted);
}
</style>
<style>
.artifact-panel.embedded {
  flex: 1;
  max-width: 100%;
  min-width: 0;
  min-height: 0;
  border-left: 0;
}
.artifact-panel.embedded .canvas-select {
  margin: 0 12px 8px;
  width: calc(100% - 24px);
  font-size: 11px;
}
.artifact-panel.embedded .canvas-content {
  padding: 10px 14px 18px;
}
.artifact-panel.embedded .artifact-ownership { padding: 0 14px 12px; }
.artifact-panel.embedded .artifact-read-message { padding: 0 14px; }
.artifact-panel.embedded .quiz-intro,
.artifact-panel.embedded .custom-consent {
  padding: 12px 0;
}
.artifact-panel.embedded h2 {
  font-size: 17px;
}
.artifact-panel.embedded p {
  font-size: 11px;
}
.artifact-panel.embedded .quiz-choice {
  font-size: 11px;
  padding: 10px;
}
.artifact-panel.embedded .quiz-nav {
  flex-wrap: wrap;
  gap: 8px;
}
.artifact-panel.embedded .custom-frame {
  min-height: 280px;
  height: 320px;
}
.artifact-panel.embedded .canvas-symbol {
  font-size: 28px;
  margin-bottom: 12px;
}
.artifact-panel.embedded .canvas-diagram {
  margin-top: 10px;
}
</style>
