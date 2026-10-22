<script setup lang="ts">
import { ref } from 'vue';
import { rpc } from '../bridge';

// Shown instead of the app while startup waits for an upgrade step the user can
// resolve (a full upgrade-backup budget). A backup is removed only after an
// explicit confirmation, by the id and manifest hash shown here.
type Backup = { id: string; state: string; from_version: number | null; to_version: number | null; created: number | null; bytes: number; manifest_sha256: string | null };
type View = { reason: string; message: string; backups: Backup[] };
const props = defineProps<{ view: View }>();
const current = ref<View>(props.view);
const confirming = ref('');
const busy = ref(false);
const error = ref('');
const status = ref('');

const size = (bytes: number) => bytes >= 1 << 20 ? (bytes / (1 << 20)).toFixed(1) + ' MB' : Math.max(1, Math.round(bytes / 1024)) + ' KB';
const when = (seconds: number | null) => seconds ? new Date(seconds * 1000).toLocaleString() : 'unknown date';
const label = (item: Backup) => 'Backup before upgrade ' + (item.from_version ?? '?') + ' → ' + (item.to_version ?? '?');

async function remove(item: Backup) {
  if (busy.value || confirming.value !== item.id || !item.manifest_sha256) return;
  busy.value = true; error.value = ''; status.value = '';
  try {
    const result = await rpc<{ maintenance: View }>('upgrade_discard_backup', { backup_id: item.id, manifest_sha256: item.manifest_sha256 });
    current.value = result.maintenance;
    status.value = label(item) + ' was removed.';
  } catch (failure) { error.value = String(failure); }
  finally { busy.value = false; confirming.value = ''; }
}
async function retry() {
  if (busy.value) return;
  busy.value = true; error.value = ''; status.value = '';
  try {
    const result = await rpc<{ ready: boolean; maintenance?: View }>('upgrade_retry', {});
    if (result.ready) status.value = 'Upgrade finished. Opening Pebble…';
    else if (result.maintenance) { current.value = result.maintenance; error.value = 'The upgrade is still waiting: ' + result.maintenance.message; }
  } catch (failure) { error.value = String(failure); }
  finally { busy.value = false; }
}
</script>

<template>
  <main class="upgrade-maintenance" aria-label="Upgrade step needed">
    <h1>Pebble needs one step before it can upgrade</h1>
    <p>{{ current.message }}</p>
    <p>Pebble keeps a verified backup of your data before every upgrade and never deletes one on its own. Remove an old backup you no longer need, then try the upgrade again. Your current data is not changed by removing a backup.</p>
    <ul>
      <li v-for="item in current.backups" :key="item.id">
        <strong>{{ label(item) }}</strong>
        <span>{{ when(item.created) }} · {{ size(item.bytes) }}<template v-if="item.state === 'pending'"> · belongs to an unfinished upgrade</template></span>
        <!-- Phase 24 (V1-108): the pinned label's numbers explained once in words. -->
        <span>A copy of your data made before Pebble updated how it stores it (from data format {{ item.from_version ?? '?' }} to {{ item.to_version ?? '?' }}).</span>
        <template v-if="item.state !== 'pending' && item.manifest_sha256">
          <button v-if="confirming !== item.id" type="button" class="secondary-button" :disabled="busy" @click="confirming = item.id">Remove this backup…</button>
          <span v-else class="upgrade-confirm">
            <button type="button" class="primary-button" :disabled="busy" @click="remove(item)">Confirm removal of {{ label(item) }}</button>
            <button type="button" class="text-button" :disabled="busy" @click="confirming = ''">Keep it</button>
          </span>
        </template>
      </li>
    </ul>
    <button type="button" class="primary-button" :disabled="busy" @click="retry">Try the upgrade again</button>
    <p v-if="status" role="status">{{ status }}</p>
    <p v-if="error" role="alert">{{ error }}</p>
  </main>
</template>

<style scoped>
.upgrade-maintenance { max-width: 640px; margin: 48px auto; padding: 0 16px; display: grid; gap: 14px; }
.upgrade-maintenance h1 { font-size: 20px; margin: 0; }
.upgrade-maintenance p { margin: 0; overflow-wrap: anywhere; }
.upgrade-maintenance ul { list-style: none; padding: 0; margin: 0; display: grid; gap: 12px; }
.upgrade-maintenance li { display: grid; gap: 6px; padding: 12px; border: 1px solid var(--border, #b9b9b9); border-radius: 10px; }
.upgrade-maintenance li span { opacity: .8; font-size: 13px; }
.upgrade-maintenance li button, .upgrade-maintenance > button { justify-self: start; }
.upgrade-confirm { display: flex; gap: 8px; flex-wrap: wrap; }
</style>
