<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { open, save } from '@tauri-apps/plugin-dialog';
import { native, rpc } from '../bridge';

// Portable memory archive. Paths come only from the native save and open
// dialogs. Import has two steps: a preview of what would change, then an
// explicit import of that exact plan; the host refuses it if the file or local
// memory changed since. Archive text and paths render as literal text.
type Counts = Record<string, unknown>;
type Line = { label: string; count: number };
type Exported = { path?: unknown; sha256?: unknown; bytes?: unknown; counts?: unknown; refused?: unknown };
type Preview = { plan_digest?: unknown; counts?: unknown; format?: unknown; forgets_local?: unknown };
type Imported = { applied?: unknown };
const props = defineProps<{ active?: boolean }>();
const emit = defineEmits<{ changed: []; error: [message: string] }>();
const busy = ref('');
const status = ref('');
const exported = ref<{ path: string; sha256: string; bytes: number | null; counts: Counts; refused: number | null } | null>(null);
const preview = ref<{ path: string; digest: string; format: string; counts: Counts; forgetsHere: number | null } | null>(null);
const applied = ref<Counts | null>(null);
let generation = 0, disposed = false;

const FILTERS = [{ name: 'Pebble memory archive', extensions: ['json'] }];
const EXPORT_LINES: [string[], string][] = [[['records'], 'Memories'], [['relations'], 'Links between memories'],
  [['tombstones'], 'Forgetting records'], [['scopes'], 'Scopes']];
const PREVIEW_LINES: [string[], string][] = [
  [['add'], 'To be added'],
  [['skip_disabled'], 'Skipped because their layer is off'],
  [['skip_tombstoned'], 'Skipped because you forgot them'],
  [['skip_duplicate'], 'Skipped because they are already here'],
  [['revalidate'], 'Need revalidation before Pebble relies on them'],
  [['expired'], 'Skipped because they expired'],
  [['skip_other'], 'Skipped for another reason, such as a project or conversation that is not on this device'],
  [['tombstones'], 'Forgetting records (tombstones)'],
];
const APPLIED_LINES: [string[], string][] = [
  [['add', 'imported'], 'Added'],
  [['skip_disabled'], 'Skipped because their layer is off'],
  [['skip_tombstoned'], 'Skipped because you forgot them'],
  [['skip_duplicate'], 'Skipped because they were already here'],
  [['revalidate', 'needs_revalidation'], 'Need revalidation before Pebble relies on them'],
  [['expired'], 'Skipped because they expired'],
  [['skip_other'], 'Skipped for another reason'],
  [['tombstones'], 'Forgetting records applied'],
  [['forgotten'], 'Memories on this device forgotten by those records'],
];
const number = (value: unknown) => typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : null;
const object = (value: unknown): Counts => value && typeof value === 'object' && !Array.isArray(value) ? value as Counts : {};
function count(counts: Counts, keys: string[]) {
  for (const key of keys) {
    const value = Object.hasOwn(counts, key) ? counts[key] : undefined;
    // A count may arrive as a number or as {rows: n}.
    const found = number(value) ?? number(object(value).rows);
    if (found !== null) return found;
  }
  return null;
}
const lines = (counts: Counts, table: [string[], string][]) => table
  .map(([keys, label]) => ({ label, count: count(counts, keys) }))
  .filter((line): line is Line => line.count !== null);
const size = (bytes: number) => bytes >= 1 << 20 ? (bytes / (1 << 20)).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
const exportLines = computed(() => exported.value ? lines(exported.value.counts, EXPORT_LINES) : []);
const previewLines = computed(() => preview.value ? lines(preview.value.counts, PREVIEW_LINES) : []);
const appliedLines = computed(() => applied.value ? lines(applied.value, APPLIED_LINES) : []);

async function exportArchive() {
  if (busy.value) return;
  busy.value = 'export'; status.value = '';
  try {
    if (!native) throw new Error('Open the desktop app to save a memory archive.');
    const path = await save({ title: 'Export memories', defaultPath: 'pebble-memories.json', filters: FILTERS });
    if (disposed || typeof path !== 'string' || !path) return;
    const result: Exported = (await rpc<Exported | null>('memory_archive_export_to', { path })) ?? {};
    // An export is an effect: its outcome is shown while this panel is open.
    if (disposed) return;
    exported.value = { path: typeof result.path === 'string' ? result.path : path, sha256: typeof result.sha256 === 'string' ? result.sha256 : '',
      bytes: number(result.bytes), counts: object(result.counts), refused: number(result.refused) };
    status.value = 'Memory archive saved.';
  } catch (error) { if (!disposed) emit('error', String(error)); }
  finally { busy.value = ''; }
}
async function previewArchive(known?: string) {
  if (busy.value || props.active) return;
  busy.value = 'preview'; status.value = '';
  const version = ++generation;
  try {
    if (!native) throw new Error('Open the desktop app to choose a memory archive.');
    const path = known || await open({ title: 'Import memories', multiple: false, directory: false, filters: FILTERS });
    if (version !== generation || typeof path !== 'string' || !path) return;
    const plan: Preview = (await rpc<Preview | null>('memory_archive_preview_file', { path })) ?? {};
    if (version !== generation) return;
    const digest = plan.plan_digest, forgets = plan.forgets_local;
    if (typeof digest !== 'string' || !digest) throw new Error('Pebble could not check this archive. Nothing was imported.');
    preview.value = { path, digest, format: typeof plan.format === 'string' ? plan.format : '', counts: object(plan.counts),
      forgetsHere: Array.isArray(forgets) ? forgets.length : null };
    applied.value = null;
    status.value = 'Archive checked. Nothing is imported until you choose Import.';
  } catch (error) { if (version === generation) emit('error', String(error)); }
  finally { busy.value = ''; }
}
async function importArchive() {
  const reviewed = preview.value;
  if (busy.value || props.active || !reviewed) return;
  busy.value = 'import'; status.value = '';
  try {
    const result: Imported = (await rpc<Imported | null>('memory_archive_import_file', { path: reviewed.path, plan_digest: reviewed.digest })) ?? {};
    if (disposed) return;
    preview.value = null;
    applied.value = object(result.applied);
    status.value = 'Import finished.';
    emit('changed');
  } catch (error) { if (!disposed) emit('error', String(error)); }
  finally { busy.value = ''; }
}
function dismiss() {
  generation++;
  preview.value = null;
  status.value = 'Nothing was imported.';
}
// A task that starts or finishes can change local memory, so an open preview
// no longer describes this device; any preview reply still in flight is dropped.
watch(() => props.active, () => {
  generation++;
  if (preview.value) status.value = 'The preview was closed because local memory may have changed. Check the archive again.';
  preview.value = null;
});
onBeforeUnmount(() => { disposed = true; generation++; });
</script>

<template>
  <section class="memory-archive" aria-label="Memory archive" :aria-busy="!!busy">
    <div class="section-label">MEMORY ARCHIVE</div>
    <p class="setting-note">Save your memories to a file you choose, or bring them in from one. The file is plain JSON and is not encrypted. Only layers that are on are exported; working context and unconfirmed proposals stay on this device.</p>
    <p class="setting-note">Importing shows what would change before anything is imported. Forgotten memories stay forgotten: an archive cannot bring them back, and its own forgetting records also forget matching memories here. Layers that are off here are not filled, and your memory switches and consent do not change. Imported memories are data and grant no permission. A checksum detects damage; it does not prove who made the file.</p>
    <div class="setting-actions">
      <button type="button" class="secondary-button" :disabled="!!busy" @click="exportArchive">{{ busy === 'export' ? 'Saving…' : 'Export memories…' }}</button>
      <button type="button" class="secondary-button" :disabled="!!busy || active" @click="previewArchive()">{{ busy === 'preview' ? 'Checking…' : 'Choose an archive to import…' }}</button>
    </div>
    <p v-if="active" class="setting-note">Importing waits until the current task finishes.</p>
    <p class="archive-status" role="status" aria-live="polite">{{ status }}</p>
    <div v-if="exported" class="archive-card" role="group" aria-label="Saved archive">
      <strong>Saved to {{ exported.path }}</strong>
      <ul>
        <li v-for="line in exportLines" :key="line.label">{{ line.label }}: {{ line.count }}</li>
      </ul>
      <small>{{ exported.bytes === null ? '' : size(exported.bytes) + ' · ' }}SHA-256 {{ exported.sha256.slice(0, 12) }}…</small>
      <small v-if="exported.refused">Left out because they contain credential-like text: {{ exported.refused }}</small>
    </div>
    <div v-if="preview" class="archive-card" role="group" aria-label="Import preview">
      <strong>{{ preview.path }}</strong>
      <small v-if="preview.format">Format: {{ preview.format }}</small>
      <ul>
        <li v-for="line in previewLines" :key="line.label">{{ line.label }}: {{ line.count }}</li>
      </ul>
      <p v-if="preview.forgetsHere">Memories on this device that the archive's forgetting records will forget: {{ preview.forgetsHere }}</p>
      <p>Nothing has been imported yet.</p>
      <div class="setting-actions">
        <button type="button" class="primary-button" :disabled="!!busy || active" @click="importArchive">{{ busy === 'import' ? 'Importing…' : 'Import this archive' }}</button>
        <button type="button" class="secondary-button" :disabled="!!busy || active" @click="previewArchive(preview.path)">Check this file again</button>
        <button type="button" class="secondary-button" :disabled="!!busy" @click="dismiss">Cancel</button>
      </div>
    </div>
    <div v-if="applied" class="archive-card" role="group" aria-label="Import result">
      <strong>Import finished</strong>
      <ul>
        <li v-for="line in appliedLines" :key="line.label">{{ line.label }}: {{ line.count }}</li>
      </ul>
    </div>
  </section>
</template>

<style scoped>
.memory-archive { border-top: 1px solid var(--border); margin-top: 22px; padding-top: 20px; }
.memory-archive .setting-actions { flex-wrap: wrap; }
.archive-status { font-size: 11px; line-height: 1.6; margin: 8px 0; overflow-wrap: anywhere; }
.archive-card { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 10px 0; font-size: 11px; line-height: 1.6; }
.archive-card strong { display: block; font-size: 12px; overflow-wrap: anywhere; }
.archive-card small { display: block; font-size: 10px; color: var(--muted); overflow-wrap: anywhere; }
.archive-card ul { margin: 6px 0; padding-left: 18px; }
.archive-card p { margin: 6px 0; overflow-wrap: anywhere; }
</style>
