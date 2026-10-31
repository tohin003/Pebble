<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref } from 'vue';
import { rpc, selectFile } from '../bridge';

// Phase 22 (s22-storage): the Data & storage page. Main window only (main.rs keeps every storage_* route out of the
// pet and generated views). It shows where Pebble keeps things, sizes by category, Show in Finder, deleting the safe
// categories after an inline confirmation, the Documents copies switch and location, settings.json changes that
// wait for the person, and Recently deleted. Every host string renders as text.
// Phase 24 (t24-data): what lies outside both folders (V1-056), forgets from Documents that wait for the person
// (V1-004), Delete all conversations and Delete all now (V1-061), Edit PEBBLE.md and Show settings.json (V1-101),
// Remove old sign-in copies (V1-017), and honest wording about what a deletion leaves in backups.
type Category = {
  id: string; label: string; place: string; path: string | null; bytes: number; files: number; partial: boolean;
  what: string; safe_to_delete: string; deletable: boolean; if_deleted: string; revealable?: boolean;
};
type Pending = { id: string; key: string; reasons: string[] };
type Trashed = { chat_id: string; title: string; deleted: number; delete_after: number };
type Question = { id: string; kind: string; file: string; count: number; whole: boolean };
type Summary = {
  home?: { path?: string; data?: string; on_old_folder?: boolean };
  migration?: Record<string, unknown>;
  documents?: { enabled?: boolean; reason?: string | null; path?: string | null; default_path?: string | null;
    custom?: boolean; icloud?: { documents_synced?: boolean; note?: string };
    pending?: { trash?: number; forget?: number; waiting?: { conversations?: number; memory?: number } };
    confirm?: Question[]; logs_copied?: boolean };
  categories?: Category[];
  settings_file?: { path?: string; ok?: boolean; error?: string | null; pending?: Pending[]; waiting?: Record<string, string>;
    unknown_keys?: string[]; write_error?: string | null };
  pebble_md?: { path?: string; status?: string | null; message?: string | null };
  recently_deleted?: Trashed[];
  events?: { at: number; kind: string; text: string }[];
  problems?: Record<string, string>;
};
const emit = defineEmits<{ refresh: [] }>();
const summary = ref<Summary | null>(null);
const busy = ref('');
const message = ref('');
const error = ref('');
const confirming = ref('');
const keepButton = ref<HTMLButtonElement | null>(null);
let alive = true;

const text = (value: unknown) => (typeof value === 'string' ? value : '');
const count = (value: unknown) => (typeof value === 'number' && Number.isFinite(value) && value >= 0 ? value : 0);
function size(bytes: unknown) {
  const n = count(bytes);
  if (n < 1024) return n + ' bytes';
  const units = ['KB', 'MB', 'GB', 'TB'];
  let value = n / 1024, unit = 0;
  while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
  return (value >= 10 ? Math.round(value) : Math.round(value * 10) / 10) + ' ' + units[unit];
}
const day = (seconds: unknown) => (count(seconds) ? new Date(count(seconds) * 1000).toLocaleDateString() : 'unknown');
const categories = computed(() => (summary.value?.categories || []).filter(c => c && typeof c.id === 'string'));
const documents = computed(() => categories.value.filter(c => c.place === 'documents'));
const internal = computed(() => categories.value.filter(c => c.place === 'internal'));
const elsewhere = computed(() => categories.value.filter(c => c.place === 'elsewhere'));
const groups = computed(() => [{ name: 'In Documents', items: documents.value }, { name: 'In Pebble’s folder', items: internal.value },
  ...(elsewhere.value.length ? [{ name: 'Elsewhere on this Mac', items: elsewhere.value }] : [])]);
const shown = (item: Category) => !!item.path && (item.place === 'documents' || item.revealable === true
  || ['backups', 'cache', 'service_logs'].includes(item.id));
const questions = computed(() => (summary.value?.documents?.confirm || []).filter(q => q && typeof q.id === 'string'));
const checking = computed(() => count(summary.value?.documents?.pending?.waiting?.conversations)
  + count(summary.value?.documents?.pending?.waiting?.memory));
const total = (items: Category[]) => size(items.reduce((sum, c) => sum + count(c.bytes), 0));
const docs = computed(() => summary.value?.documents || {});
const settingsFile = computed(() => summary.value?.settings_file || {});
const pending = computed(() => (settingsFile.value.pending || []).filter(p => p && typeof p.id === 'string'));
const waiting = computed(() => Object.entries(settingsFile.value.waiting || {}));
const trashed = computed(() => (summary.value?.recently_deleted || []).filter(t => t && typeof t.chat_id === 'string'));
const events = computed(() => [...(summary.value?.events || [])].reverse());
const problems = computed(() => Object.values(summary.value?.problems || {}).filter(p => typeof p === 'string'));
const migration = computed(() => summary.value?.migration || {});
const moveNote = computed(() => {
  const m = migration.value;
  const state = text(m.state);
  if (state === 'refused' || state === 'failed' || state === 'conflict')
    return { alert: true, text: 'Pebble is still using its old folder. ' + text(m.message) + ' Nothing was deleted.' };
  if (m.warning === 'data_missing_after_move')
    return { alert: true, text: "Pebble's data folder was missing, so Pebble started fresh. Your earlier data is still in "
      + (text(m.moved_from) || '~/Library/Application Support/com.pebble.local') + '; nothing there was deleted.' };  // Phase 45 (F009)
  if (state === 'migrated' || text(m.moved_at))
    return { alert: false, text: 'Your data moved here' + (text(m.moved_at) ? ' on ' + new Date(text(m.moved_at)).toLocaleDateString() : '')
      + '. The old folder' + (text(m.moved_from) ? ' (' + text(m.moved_from) + ')' : '') + ' is untouched, with a MOVED.md; delete it yourself when you are happy.'
      + (m.legacy_changed ? ' An older Pebble has used the old folder since then; that work stays there.' : '') };
  return null;
});
const pebbleMd = computed(() => {
  const status = text(summary.value?.pebble_md?.status);
  return ({ used: 'PEBBLE.md is used as your stated preferences.', empty: 'PEBBLE.md is empty, so nothing from it is used.',
    memory_off: 'PEBBLE.md is not used while memory or "Your preferences" is off.',
    truncated: text(summary.value?.pebble_md?.message),
    too_long: text(summary.value?.pebble_md?.message) } as Record<string, string>)[status] || '';
});
const plural = (n: number, one: string, many: string) => n + ' ' + (n === 1 ? one : many);
const settingNames: Record<string, string> = { permissions: 'Permissions', memory_enabled: 'Memory', memory_layers: 'Memory layers',
  notifications: 'Notifications', storage: 'Copies in Documents', theme: 'Theme', reduced_motion: 'Reduced motion', companion: 'Companion' };

async function run(name: string, task: () => Promise<string | void>) {
  if (busy.value) return;
  busy.value = name;
  error.value = '';
  try {
    const done = await task();
    if (alive && done) message.value = done;
  } catch (failure) {
    if (alive) error.value = String(failure);
  } finally {
    if (alive) busy.value = '';
  }
}
function accept(next: unknown) {
  if (alive && next && typeof next === 'object' && 'categories' in (next as object)) summary.value = next as Summary;
}
async function load() { accept(await rpc<Summary>('storage_summary')); }
const refresh = () => run('refresh', async () => { accept(await rpc<Summary>('storage_refresh')); return 'Copies are up to date.'; });
const reveal = (category: string, label: string) =>
  run('reveal:' + category, async () => { await rpc('storage_reveal', { category }); return 'Opened ' + label + ' in Finder.'; });

// One confirmation is open at a time; its safe choice (Keep) takes the focus.
function setKeep(element: unknown) { if (element) keepButton.value = element as HTMLButtonElement; }
async function ask(id: string) {
  confirming.value = id;
  await nextTick();
  keepButton.value?.focus();
}
function cancel() { confirming.value = ''; }
const remove = (item: Category) => run('delete:' + item.id, async () => {
  const result = await rpc<{ removed?: number; freed_bytes?: number }>('storage_delete', { category: item.id, confirm: true });
  confirming.value = '';
  await load();
  return 'Deleted ' + item.label.toLowerCase() + ' (' + size(result?.freed_bytes) + ').';
});
const setMirror = (enabled: boolean) => run('mirror', async () => {
  accept(await rpc<Summary>('storage_set', { documents_mirror: enabled }));
  return enabled ? 'Copies in Documents are on.' : 'Copies in Documents are off. Files already there stay.';
});
const chooseFolder = () => run('folder', async () => {
  const folder = await selectFile(true);
  if (!folder) return 'The location was not changed.';
  accept(await rpc<Summary>('storage_set', { documents_folder: folder }));
  return 'Copies now go to ' + folder + '. The previous folder stays as it is.';
});
const useDefault = () => run('folder', async () => {
  accept(await rpc<Summary>('storage_set', { documents_folder: null }));
  return 'Copies now go to ' + text(docs.value.default_path) + '.';
});
const resolve = (item: Pending, apply: boolean) => run('setting:' + item.id, async () => {
  accept(await rpc<Summary>('storage_settings_resolve', { id: item.id, accept: apply }));
  emit('refresh');
  return apply ? 'Applied the settings.json change.' : 'Dismissed the settings.json change; your current setting stays.';
});
const restore = (item: Trashed) => run('restore:' + item.chat_id, async () => {
  await rpc('storage_restore_chat', { chat_id: item.chat_id });
  await load();
  emit('refresh');
  return 'Restored “' + item.title + '”.';
});
const purge = (item: Trashed) => run('purge:' + item.chat_id, async () => {
  await rpc('storage_purge_chat', { chat_id: item.chat_id, confirm: true });
  confirming.value = '';
  await load();
  emit('refresh');
  return 'Deleted “' + item.title + '” for good.';
});
// V1-101: Pebble's own two text files, opened in the default text editor by the host (never run).
const edit = (category: 'pebble_md' | 'settings_file', label: string) =>
  run('edit:' + category, async () => { await rpc('storage_reveal', { category }); return 'Opened ' + label + ' in your text editor.'; });
// V1-004: a forget from Documents that waits for the person. Keep writes the entries back; Forget forgets them.
const answer = (item: Question, forget: boolean) => run('forget:' + item.id, async () => {
  accept(await rpc<Summary>('storage_forget_resolve', { id: item.id, accept: forget }));
  return forget ? 'Forgot ' + plural(item.count, 'memory', 'memories') + '.' : 'Kept them; they are back in “' + item.file + '”.';
});
// V1-061: every conversation to Recently deleted, or Recently deleted emptied for good; both after a confirmation.
const trashAll = () => run('trash-all', async () => {
  const result = await rpc<{ moved?: number; kept?: { title?: string; reason?: string }[] }>('storage_trash_all', { confirm: true });
  confirming.value = '';
  await load();
  emit('refresh');
  const kept = (result?.kept || []).length;
  return 'Moved ' + plural(count(result?.moved), 'conversation', 'conversations') + ' to Recently deleted.'
    + (kept ? ' ' + plural(kept, 'conversation stays', 'conversations stay') + ' because a task or schedule uses it.' : '');
});
const purgeAll = () => run('purge-all', async () => {
  const result = await rpc<{ deleted?: number; kept?: unknown[] }>('storage_purge_all', { confirm: true });
  confirming.value = '';
  await load();
  emit('refresh');
  return 'Deleted ' + plural(count(result?.deleted), 'conversation', 'conversations') + ' for good.';
});
// V1-017: the old per-item Keychain copies kept for going back to a version before the vault.
const cleanupLegacy = () => run('legacy', async () => {
  const result = await rpc<{ removed?: number; failed?: number }>('storage_cleanup_legacy', { confirm: true });
  confirming.value = '';
  return 'Removed ' + plural(count(result?.removed), 'old sign-in copy', 'old sign-in copies') + ' from the Keychain.'
    + (count(result?.failed) ? ' ' + count(result?.failed) + ' could not be removed; try again later.' : '');
});
function onMirror(event: Event) { void setMirror((event?.target as HTMLInputElement | null)?.checked === true); }

onMounted(() => { void run('load', load); });
onBeforeUnmount(() => { alive = false; });
</script>

<template>
  <section class="storage-settings" aria-labelledby="storage-heading" :aria-busy="busy === 'load'">
    <div id="storage-heading" class="section-label">DATA &amp; STORAGE</div>
    <p v-if="!summary && busy === 'load'" class="setting-note" role="status">Loading…</p>
    <p v-if="error" class="storage-alert" role="alert">{{ error }}</p>
    <template v-if="summary">
      <p v-if="moveNote" :class="moveNote.alert ? 'storage-alert' : 'setting-note'" :role="moveNote.alert ? 'alert' : undefined">{{ moveNote.text }}</p>
      <p v-for="(problem, index) in problems" :key="'problem' + index" class="storage-alert" role="alert">{{ problem }}</p>

      <section class="storage-block" aria-labelledby="storage-where">
        <h4 id="storage-where">Where your data lives</h4>
        <div class="storage-row">
          <div>
            <strong>Pebble's folder</strong>
            <p class="setting-note storage-path">{{ text(summary.home?.path) }}</p>
            <p class="setting-note">Settings and the database. Important: keep it. Pebble's own tools cannot change it, and iCloud does not sync it.</p>
          </div>
          <button type="button" class="secondary-button" :disabled="!!busy" aria-label="Show Pebble's folder in Finder" @click="reveal('home', 'Pebble’s folder')">Show in Finder</button>
        </div>
        <div class="storage-row">
          <div>
            <label class="storage-switch">
              <input type="checkbox" :checked="docs.enabled === true" :disabled="!!busy" aria-describedby="storage-docs-note"
                @change="onMirror" />
              Keep readable copies in Documents
            </label>
            <p id="storage-docs-note" class="setting-note">
              <template v-if="docs.enabled">{{ text(docs.path) }}</template>
              <template v-else-if="docs.reason === 'no_folder'">Choose a folder to turn copies on.</template>
              <template v-else>Off. Files already there stay where they are.</template>
            </p>
            <p v-if="docs.icloud?.note" class="setting-note">{{ docs.icloud.note }}</p>
          </div>
          <div class="storage-actions">
            <button v-if="docs.enabled" type="button" class="secondary-button" :disabled="!!busy" aria-label="Show the Documents copies in Finder" @click="reveal('documents', 'your copies')">Show in Finder</button>
            <button type="button" class="secondary-button" :disabled="!!busy" @click="chooseFolder">Change location…</button>
            <button v-if="docs.custom && docs.default_path" type="button" class="text-button" :disabled="!!busy" @click="useDefault">Use {{ docs.default_path }}</button>
          </div>
        </div>
      </section>

      <section v-if="questions.length || checking" class="storage-block" aria-labelledby="storage-forget">
        <h4 id="storage-forget">Deletions from your files</h4>
        <p v-if="checking" class="setting-note">Pebble is checking {{ plural(checking, 'deletion', 'deletions') }} in your Documents copies. It acts once a deletion has lasted a minute, or at once when you choose Update copies now.</p>
        <article v-for="item in questions" :key="item.id" class="storage-pending" :aria-label="'Memories deleted from ' + item.file">
          <p><strong>{{ item.whole ? 'You deleted “' + item.file + '”.' : 'You deleted ' + plural(item.count, 'entry', 'entries') + ' from “' + item.file + '”.' }}</strong>
            Pebble forgets {{ plural(item.count, 'memory', 'memories') }} only if you say so, and then does not learn them again from the same source.</p>
          <div class="storage-actions">
            <button type="button" class="secondary-button" :disabled="!!busy" @click="answer(item, false)">Keep them</button>
            <button type="button" class="secondary-button danger" :disabled="!!busy" @click="answer(item, true)">Forget them</button>
          </div>
        </article>
      </section>

      <section class="storage-block" aria-labelledby="storage-file">
        <h4 id="storage-file">settings.json and PEBBLE.md</h4>
        <p v-if="settingsFile.ok === false" class="storage-alert" role="alert">{{ text(settingsFile.error) }}</p>
        <article v-for="item in pending" :key="item.id" class="storage-pending" :aria-label="'Waiting change to ' + (settingNames[item.key] || item.key)">
          <p><strong>settings.json asks for more access.</strong> {{ settingNames[item.key] || item.key }}: {{ item.reasons.join('; ') }}.</p>
          <p class="setting-note">It was changed outside Pebble, so it waits for you. Until you apply it, your current setting stays.</p>
          <div class="storage-actions">
            <button type="button" class="primary-button" :disabled="!!busy" @click="resolve(item, true)">Apply</button>
            <button type="button" class="secondary-button" :disabled="!!busy" @click="resolve(item, false)">Dismiss</button>
          </div>
        </article>
        <p v-for="[key, why] in waiting" :key="'wait' + key" class="setting-note">{{ settingNames[key] || key }} changes when Pebble can: {{ why }}</p>
        <p v-if="pebbleMd" class="setting-note">{{ pebbleMd }}</p>
        <p class="setting-note">PEBBLE.md tells Pebble about you, read as information and never as a permission. settings.json holds your settings; a change that gives Pebble more access waits for you here.</p>
        <div class="storage-actions">
          <button type="button" class="secondary-button" :disabled="!!busy" @click="edit('pebble_md', 'PEBBLE.md')">Edit PEBBLE.md</button>
          <button type="button" class="secondary-button" :disabled="!!busy" @click="edit('settings_file', 'settings.json')">Show settings.json</button>
        </div>
      </section>

      <section class="storage-block" aria-labelledby="storage-space">
        <h4 id="storage-space">Space used</h4>
        <table v-for="group in groups" :key="group.name" class="storage-table">
          <caption>{{ group.name }} · {{ total(group.items) }}</caption>
          <thead>
            <tr><th scope="col">Item</th><th scope="col">Size</th><th scope="col">Safe to delete?</th><th scope="col"><span class="visually-hidden">Actions</span></th></tr>
          </thead>
          <tbody>
            <template v-for="item in group.items" :key="item.id">
              <tr>
                <th scope="row"><span>{{ item.label }}</span><small>{{ item.what }}</small></th>
                <td>{{ size(item.bytes) }}<template v-if="item.partial">+</template></td>
                <td>{{ item.safe_to_delete }}</td>
                <td class="storage-actions">
                  <button v-if="shown(item)" type="button"
                    class="text-button" :disabled="!!busy" :aria-label="'Show ' + item.label + ' in Finder'" @click="reveal(item.id, item.label)">Show</button>
                  <button v-if="item.deletable" type="button" class="text-button danger" :disabled="!!busy || !item.bytes"
                    :aria-label="'Delete ' + item.label + '…'" @click="ask('delete:' + item.id)">Delete…</button>
                </td>
              </tr>
              <tr v-if="confirming === 'delete:' + item.id" class="storage-confirm">
                <td colspan="4">
                  <p role="alert">Delete {{ item.label.toLowerCase() }} ({{ size(item.bytes) }})? {{ item.if_deleted }}</p>
                  <div class="storage-actions">
                    <button :ref="setKeep" type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Keep</button>
                    <button type="button" class="secondary-button danger" :disabled="!!busy" @click="remove(item)">{{ busy === 'delete:' + item.id ? 'Deleting…' : 'Delete' }}</button>
                  </div>
                </td>
              </tr>
            </template>
          </tbody>
        </table>
        <p class="setting-note">Deleting a conversation file in Documents moves the conversation to Recently deleted, and deleting a memory entry there makes Pebble forget it, once Pebble is sure: after a minute, or at once when you choose Update copies now. More than 3 entries, or a whole file, wait for you on this page. Edits to those files are not read back, and Pebble’s own tools cannot open them.</p>
        <p v-if="docs.enabled && summary.documents?.logs_copied === false" class="setting-note">Troubleshooting logs are not copied to Documents while it syncs with iCloud Drive. They stay in Pebble’s folder.</p>
        <p class="setting-note">Elsewhere on this Mac, backups and the old folder keep copies of your data from when they were made, including conversations deleted since. Pebble never changes them; remove them yourself when you no longer need them.</p>
      </section>

      <section class="storage-block" aria-labelledby="storage-trash">
        <h4 id="storage-trash">Recently deleted</h4>
        <p v-if="!trashed.length" class="setting-note">Nothing here. Conversations you delete, in Pebble or by deleting their file in Documents, wait here for 30 days.</p>
        <ul v-else class="storage-list" aria-labelledby="storage-trash">
          <li v-for="item in trashed" :key="item.chat_id">
            <div>
              <span>{{ item.title || 'Conversation' }}</span>
              <small>Deleted {{ day(item.deleted) }} · deleted for good after {{ day(item.delete_after) }}</small>
            </div>
            <div class="storage-actions">
              <button type="button" class="secondary-button" :disabled="!!busy" :aria-label="'Restore ' + (item.title || 'conversation')" @click="restore(item)">Restore</button>
              <button type="button" class="text-button danger" :disabled="!!busy" :aria-label="'Delete ' + (item.title || 'conversation') + ' now…'" @click="ask('purge:' + item.chat_id)">Delete now…</button>
            </div>
            <div v-if="confirming === 'purge:' + item.chat_id" class="storage-confirm">
              <p role="alert">Delete “{{ item.title || 'Conversation' }}” for good? It cannot be restored. Copies made earlier in upgrade backups, install backups and the old folder stay until you remove them.</p>
              <div class="storage-actions">
                <button :ref="setKeep" type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Keep</button>
                <button type="button" class="secondary-button danger" :disabled="!!busy" @click="purge(item)">Delete for good</button>
              </div>
            </div>
          </li>
        </ul>
        <div class="storage-actions">
          <button v-if="trashed.length" type="button" class="text-button danger" :disabled="!!busy" @click="ask('purge-all')">Delete all now…</button>
          <button type="button" class="text-button danger" :disabled="!!busy" @click="ask('trash-all')">Delete all conversations…</button>
        </div>
        <div v-if="confirming === 'purge-all'" class="storage-confirm">
          <p role="alert">Delete all {{ plural(trashed.length, 'conversation', 'conversations') }} in Recently deleted for good? They cannot be restored. Copies made earlier in backups and the old folder stay until you remove them.</p>
          <div class="storage-actions">
            <button :ref="setKeep" type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Keep</button>
            <button type="button" class="secondary-button danger" :disabled="!!busy" @click="purgeAll">Delete all for good</button>
          </div>
        </div>
        <div v-if="confirming === 'trash-all'" class="storage-confirm">
          <p role="alert">Move every conversation to Recently deleted? You can restore them here for 30 days. A conversation with a running task or an active schedule stays.</p>
          <div class="storage-actions">
            <button :ref="setKeep" type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Keep</button>
            <button type="button" class="secondary-button danger" :disabled="!!busy" @click="trashAll">Move all to Recently deleted</button>
          </div>
        </div>
      </section>

      <section class="storage-block" aria-labelledby="storage-signins">
        <h4 id="storage-signins">Old sign-in copies</h4>
        <p class="setting-note">Pebble keeps your keys and sign-ins in one Keychain item. Older versions kept a copy of each, which Pebble kept so you could go back. Removing them is safe; going back to such a version then needs a new sign-in.</p>
        <div class="storage-actions">
          <button type="button" class="secondary-button" :disabled="!!busy" @click="ask('legacy')">Remove old sign-in copies…</button>
        </div>
        <div v-if="confirming === 'legacy'" class="storage-confirm">
          <p role="alert">Remove the old copies of your keys and sign-ins from the Keychain? Pebble keeps working; only going back to an older version needs a new sign-in.</p>
          <div class="storage-actions">
            <button :ref="setKeep" type="button" class="secondary-button" :disabled="!!busy" @click="cancel">Keep</button>
            <button type="button" class="secondary-button danger" :disabled="!!busy" @click="cleanupLegacy">Remove them</button>
          </div>
        </div>
      </section>

      <details v-if="events.length" class="storage-block">
        <summary>Recent changes from your files</summary>
        <ul class="storage-list">
          <li v-for="(event, index) in events" :key="index"><span>{{ event.text }}</span></li>
        </ul>
      </details>
      <div class="storage-actions">
        <button type="button" class="secondary-button" :disabled="!!busy" @click="refresh">{{ busy === 'refresh' ? 'Updating…' : 'Update copies now' }}</button>
      </div>
    </template>
    <p class="storage-status" role="status" aria-live="polite">{{ message }}</p>
  </section>
</template>

<style scoped>
.storage-settings { min-width: 0; font-size: 13px; }
.storage-block { margin: 14px 0; padding-top: 10px; border-top: 1px solid var(--line); }
.storage-block h4 { margin: 0 0 8px; font-size: 13px; font-weight: 600; }
.storage-row { display: flex; gap: 12px; align-items: flex-start; justify-content: space-between; margin: 10px 0; }
.storage-row > div:first-child { min-width: 0; }
.storage-path { overflow-wrap: anywhere; font-family: ui-monospace, SFMono-Regular, Menlo, monospace; }
.storage-switch { display: flex; gap: 8px; align-items: center; font-weight: 600; }
.storage-actions { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
.storage-alert { color: var(--danger); overflow-wrap: anywhere; }
.storage-pending { margin: 8px 0; padding: 10px 12px; border: 1px solid var(--line-strong); border-radius: 10px; background: var(--soft); }
.storage-pending p { margin: 0 0 6px; }
.storage-table { width: 100%; border-collapse: collapse; margin: 8px 0 14px; }
.storage-table caption { text-align: left; font-weight: 600; padding-bottom: 6px; }
.storage-table th, .storage-table td { text-align: left; vertical-align: top; padding: 6px 6px 6px 0; border-bottom: 1px solid var(--line); }
.storage-table thead th { font-size: 11px; font-weight: 500; color: var(--secondary); }
.storage-table tbody th { font-weight: 500; }
.storage-table tbody th small { display: block; font-weight: 400; color: var(--secondary); margin-top: 2px; }
.storage-table td:nth-child(2) { white-space: nowrap; font-variant-numeric: tabular-nums; }
.storage-confirm td, div.storage-confirm { background: var(--soft); padding: 8px 10px; border-radius: 8px; }
.storage-confirm p { margin: 0 0 8px; }
.storage-list { list-style: none; padding: 0; margin: 0; }
.storage-list li { display: flex; gap: 8px; align-items: center; justify-content: space-between; flex-wrap: wrap; padding: 6px 0;
  border-bottom: 1px solid var(--line); overflow-wrap: anywhere; }
.storage-list small { display: block; color: var(--secondary); font-size: 12px; }
.storage-status { min-height: 1em; color: var(--secondary); }
.visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip: rect(0 0 0 0); white-space: nowrap; }
details summary { cursor: pointer; }
</style>
