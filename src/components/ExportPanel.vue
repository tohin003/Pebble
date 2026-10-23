<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { invoke } from '@tauri-apps/api/core';
import { native, rpc } from '../bridge';

// Verified local exports. Preview pins the source revision, Create file writes
// into Pebble's own folder and reads it back, and Save copies it only where the
// native Save dialog points: the shell returns a single-use token and this view
// never sends a path. There is no share, upload, email or send control here;
// publication belongs to reviewed connectors. All host text renders literally.
type Source = { kind: string; id: string; revision?: number | null; title?: string; artifactKind?: string };
type Check = { name: string; label: string; passed: boolean; detail: string };
type Plan = { planId: string; format: string; revision: number | null; pages: number | null; deadline: number | null };
type Receipt = { exportId: string; format: string; status: string; reason: string; revision: number | null; checks: Check[];
  notes: string[]; suggestedName: string; bytes: number | null; sha256: string; units: string; coverage: number | null };
type Saved = { path: string; sha256: string; verified: boolean };
const props = defineProps<{ chatId: string; source: Source }>();
const emit = defineEmits<{ error: [message: string] }>();
// Phase 30 (D42): no PDF. A release build has no native PDF view, so a PDF export could only end unavailable; an
// HTML export prints to PDF from a browser.
const FORMATS = [
  { id: 'html', label: 'Web page (HTML, no scripts)' }, { id: 'docx', label: 'Word document (DOCX)' },
  { id: 'xlsx', label: 'Excel workbook (XLSX)' }, { id: 'csv', label: 'CSV table' },
];
const STATUS: Record<string, string> = {
  verified: 'Verified: every readback check passed.',
  unverified: 'Unverified: the file was made, but some checks did not pass. You can still save it; it stays labelled unverified.',
  unavailable: 'Unavailable: nothing was written.',
  failed: 'Failed: nothing was written.',
  cancelled: 'Cancelled: the partial file was removed.',
};
const format = ref('html');
const includeAnswers = ref(false);
const busy = ref('');
const status = ref('');
const plan = ref<Plan | null>(null);
const receipt = ref<Receipt | null>(null);
const saved = ref<Saved | null>(null);
let generation = 0, disposed = false;

const text = (value: unknown) => typeof value === 'string' ? value : '';
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) ? value : null;
const object = (value: unknown): Record<string, unknown> => value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : {};
const quiz = computed(() => props.source.kind === 'artifact' && props.source.artifactKind === 'quiz');
const savable = computed(() => !!receipt.value && ['verified', 'unverified'].includes(receipt.value.status) && !!receipt.value.sha256);
const time = (seconds: number | null) => seconds === null ? '' : new Date(seconds * 1000).toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
const size = (bytes: number) => bytes >= 1 << 20 ? (bytes / (1 << 20)).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';

function options() {
  const chosen: Record<string, unknown> = {};
  if (quiz.value) chosen.include_answers = includeAnswers.value;
  return chosen;
}
function readReceipt(value: unknown): Receipt {
  const data = object(value), file = object(data.file), result = object(data.readback), source = object(data.source);
  const checks = Array.isArray(data.checks) ? data.checks.map(item => {
    const check = object(item);
    return { name: text(check.name), label: text(check.label) || text(check.name), passed: check.passed === true, detail: text(check.detail) };
  }) : [];
  const units = number(result.sheets) !== null ? number(result.sheets) + ' sheet(s)' : number(result.pages) !== null ? number(result.pages) + ' page(s)' : '';
  return { exportId: text(data.export_id), format: text(data.format), status: text(data.status) || 'failed', reason: text(data.reason),
    revision: number(source.revision), checks, notes: Array.isArray(data.notes) ? data.notes.filter((n): n is string => typeof n === 'string') : [],
    suggestedName: text(file.suggested_name), bytes: number(file.bytes), sha256: text(file.sha256), units, coverage: number(result.text_coverage) };
}

async function previewExport() {
  if (busy.value) return;
  busy.value = 'preview'; status.value = ''; saved.value = null;
  const version = ++generation;
  try {
    const source = { kind: props.source.kind, id: props.source.id, ...(typeof props.source.revision === 'number' ? { revision: props.source.revision } : {}) };
    const result = object(await rpc('export_preview', { chat_id: props.chatId, source, format: format.value, options: options() }));
    if (version !== generation) return;
    const planId = text(result.plan_id);
    if (!planId) throw new Error('Pebble could not prepare this export. Nothing was written.');
    plan.value = { planId, format: text(result.format) || format.value, revision: number(object(result.source).revision),
      pages: number(result.pages_estimate), deadline: number(result.deadline) };
    status.value = 'Preview ready. Nothing is written until you choose Create file.';
  } catch (error) { if (version === generation && !disposed) emit('error', String(error)); }
  finally { if (version === generation) busy.value = ''; }
}
async function createFile() {
  const reviewed = plan.value;
  if (busy.value || !reviewed) return;
  busy.value = 'render'; status.value = 'Creating and checking the file…'; saved.value = null;
  const version = ++generation;
  try {
    const result = await rpc('export_render', { chat_id: props.chatId, plan_id: reviewed.planId });
    if (version !== generation) return;
    plan.value = null; // A plan is used once.
    receipt.value = readReceipt(result);
    status.value = STATUS[receipt.value.status] ?? STATUS.failed;
  } catch (error) {
    if (version !== generation || disposed) return;
    plan.value = null;
    status.value = '';
    emit('error', String(error));
  } finally { if (version === generation) busy.value = ''; }
}
async function cancelRender() {
  const reviewed = plan.value;
  if (busy.value !== 'render' || !reviewed) return;
  try { await rpc('export_cancel', { chat_id: props.chatId, plan_id: reviewed.planId }); }
  catch (error) { if (!disposed) emit('error', String(error)); }
}
async function saveFile() {
  const made = receipt.value;
  if (busy.value || !made || !savable.value) return;
  busy.value = 'save'; status.value = '';
  try {
    if (!native) throw new Error('Open the desktop app to save an export.');
    // The native shell shows the Save dialog and binds the chosen place to a single-use token.
    const chosen = object(await invoke('export_choose_destination', { chatId: props.chatId, exportId: made.exportId,
      suggestedName: made.suggestedName, extension: made.format }));
    const token = text(chosen.destination_token);
    if (disposed) return;
    if (!token) { status.value = 'Nothing was saved.'; return; }
    const result = object(await rpc('export_save', { chat_id: props.chatId, export_id: made.exportId, destination_token: token }));
    if (disposed) return;
    saved.value = { path: text(result.path), sha256: text(result.sha256), verified: result.verified === true };
    status.value = saved.value.verified ? 'Saved. The copy matches the verified export.' : 'Saved. This copy is unverified.';
  } catch (error) { if (!disposed) emit('error', String(error)); }
  finally { busy.value = ''; }
}
async function deleteExport() {
  const made = receipt.value;
  if (busy.value || !made || !made.exportId) return;
  busy.value = 'delete';
  try {
    await rpc('export_discard', { chat_id: props.chatId, export_id: made.exportId });
    if (disposed) return;
    receipt.value = null; saved.value = null;
    status.value = 'Pebble\'s copy was deleted. Files you saved elsewhere are unchanged.';
  } catch (error) { if (!disposed) emit('error', String(error)); }
  finally { busy.value = ''; }
}

// A plan pins one source revision, format and option set; any change drops it,
// and a reply still in flight for the old plan is ignored.
watch(() => [format.value, includeAnswers.value], () => { if (plan.value) { generation++; plan.value = null; busy.value = ''; status.value = 'Options changed. Preview the export again.'; } });
watch(() => [props.chatId, props.source.kind, props.source.id, props.source.revision], (next, previous) => {
  generation++; busy.value = '';
  const hadPlan = !!plan.value;
  plan.value = null;
  if (next[0] !== previous[0] || next[2] !== previous[2]) { receipt.value = null; saved.value = null; status.value = ''; return; }
  if (hadPlan) status.value = `This view changed to revision ${props.source.revision ?? '?'}. Preview the export again.`;
});
onBeforeUnmount(() => { disposed = true; generation++; });
</script>

<template>
  <section class="export-panel" aria-label="Export" :aria-busy="!!busy">
    <div class="section-label">EXPORT</div>
    <p class="setting-note">Exports make a file on this Mac and check it by reading it back. Nothing is shared, uploaded or sent.</p>
    <fieldset class="export-formats" :disabled="!!busy">
      <legend>Format</legend>
      <label v-for="item in FORMATS" :key="item.id" class="export-choice">
        <input v-model="format" type="radio" name="export-format" :value="item.id"> {{ item.label }}
      </label>
    </fieldset>
    <label v-if="quiz" class="export-choice"><input v-model="includeAnswers" type="checkbox" :disabled="!!busy"> Include the answer key</label>
    <div class="setting-actions">
      <button type="button" class="secondary-button" :disabled="!!busy" @click="previewExport">{{ busy === 'preview' ? 'Checking…' : 'Preview export' }}</button>
    </div>
    <p class="export-status" role="status" aria-live="polite">{{ status }}</p>
    <div v-if="plan" class="export-card" role="group" aria-label="Export preview">
      <strong>{{ source.title || 'This source' }}</strong>
      <small>{{ plan.format.toUpperCase() }}<template v-if="plan.revision !== null"> · revision {{ plan.revision }}</template><template v-if="plan.pages !== null"> · about {{ plan.pages }} page(s)</template><template v-if="plan.deadline !== null"> · preview valid until {{ time(plan.deadline) }}</template></small>
      <div class="setting-actions">
        <button type="button" class="primary-button" :disabled="!!busy" @click="createFile">{{ busy === 'render' ? 'Creating…' : 'Create file' }}</button>
        <button v-if="busy === 'render'" type="button" class="secondary-button" @click="cancelRender">Cancel</button>
      </div>
    </div>
    <div v-if="receipt" class="export-card" role="group" aria-label="Export result">
      <strong :class="'export-' + receipt.status">{{ receipt.status === 'verified' ? 'Verified' : receipt.status === 'unverified' ? 'Unverified' : receipt.status === 'unavailable' ? 'Unavailable' : receipt.status === 'cancelled' ? 'Cancelled' : 'Failed' }} · {{ receipt.format.toUpperCase() }}</strong>
      <p v-if="receipt.reason">{{ receipt.reason }}</p>
      <small v-if="receipt.sha256">{{ receipt.suggestedName }}<template v-if="receipt.bytes !== null"> · {{ size(receipt.bytes) }}</template><template v-if="receipt.units"> · {{ receipt.units }}</template><template v-if="receipt.coverage !== null"> · {{ Math.floor(receipt.coverage * 1000) / 10 }}% of the source text found</template><template v-if="receipt.revision !== null"> · revision {{ receipt.revision }}</template> · SHA-256 {{ receipt.sha256.slice(0, 12) }}…</small>
      <ul v-if="receipt.checks.length" class="export-checks" aria-label="Readback checks">
        <li v-for="check in receipt.checks" :key="check.name"><span aria-hidden="true">{{ check.passed ? '✓' : '✗' }}</span> {{ check.passed ? 'Passed' : 'Did not pass' }}: {{ check.label }}<template v-if="check.detail"> — {{ check.detail }}</template></li>
      </ul>
      <ul v-if="receipt.notes.length" class="export-notes"><li v-for="note in receipt.notes" :key="note">{{ note }}</li></ul>
      <div v-if="savable" class="setting-actions">
        <button type="button" class="primary-button" :disabled="!!busy" @click="saveFile">{{ busy === 'save' ? 'Saving…' : receipt.status === 'verified' ? 'Save to…' : 'Save unverified file to…' }}</button>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="deleteExport">Delete Pebble's copy</button>
      </div>
    </div>
    <div v-if="saved" class="export-card" role="group" aria-label="Saved file">
      <strong>Saved to {{ saved.path }}</strong>
      <small>{{ saved.verified ? 'Verified copy' : 'Unverified copy' }} · SHA-256 {{ saved.sha256.slice(0, 12) }}… · not published</small>
    </div>
  </section>
</template>

<style scoped>
.export-panel { border-top: 1px solid var(--border); margin-top: 18px; padding-top: 16px; }
.export-panel .setting-actions { flex-wrap: wrap; }
.export-formats { border: 0; margin: 8px 0; padding: 0; display: grid; gap: 4px; }
.export-formats legend { font-size: 11px; font-weight: 600; margin-bottom: 4px; }
.export-choice { display: flex; align-items: center; gap: 6px; font-size: 12px; margin: 4px 0; }
.export-status { font-size: 11px; line-height: 1.6; margin: 8px 0; overflow-wrap: anywhere; }
.export-card { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 10px 0; font-size: 11px; line-height: 1.6; }
.export-card strong { display: block; font-size: 12px; overflow-wrap: anywhere; }
.export-card small { display: block; font-size: 10px; color: var(--muted); overflow-wrap: anywhere; }
.export-card ul { margin: 6px 0; padding-left: 18px; }
.export-card p { margin: 6px 0; overflow-wrap: anywhere; }
.export-checks li, .export-notes li { overflow-wrap: anywhere; }
</style>
