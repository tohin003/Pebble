<script setup lang="ts">
import { computed, onMounted, ref, watch } from 'vue';
import { rpc } from '../bridge';
const props = defineProps<{ active: boolean; allowed: boolean }>();
const emit = defineEmits<{ refresh: [] }>();
const items = ref<any[]>([]), runtime = ref<any>(null), input = ref(''), review = ref<any>(null);
const busy = ref(false), error = ref(''), selected = ref('');
const blocked = computed(() => busy.value || props.active || !props.allowed);
watch(input, () => { review.value = null; selected.value = ''; });
async function load() {
  const state = await rpc('mods'); items.value = state.items; runtime.value = state.runtime;
}
async function act(fn: () => Promise<void>) {
  busy.value = true; error.value = '';
  try { await fn(); await load(); emit('refresh'); } catch (e) { error.value = String(e); }
  finally { busy.value = false; }
}
async function example() {
  await act(async () => { input.value = JSON.stringify(await rpc('mod_example'), null, 2); });
}
async function preview() {
  const submitted = input.value;
  await act(async () => {
    const result = await rpc('mod_preview', { value: submitted });
    if (input.value === submitted) review.value = result;
  });
}
async function inspect(item: any) {
  input.value = JSON.stringify(item.manifest, null, 2);
  const submitted = input.value;
  // The watch runs before the reviewed server response arrives.
  await act(async () => {
    const result = await rpc('mod_preview', { value: item.manifest });
    if (input.value === submitted) { review.value = result; selected.value = item.id; }
  });
}
async function install() {
  if (!review.value) return;
  await act(async () => { const item = await rpc('mod_install', { value: review.value.manifest, digest: review.value.digest }); selected.value = item.id; });
}
async function toggle(item: any) {
  if (!item.enabled && (!review.value || selected.value !== item.id || review.value.digest !== item.digest)) {
    error.value = 'Review this mod’s full source before enabling it.'; return;
  }
  await act(async () => { await rpc('mod_toggle', { id: item.id, enabled: !item.enabled, digest: item.digest }); });
}
async function remove(item: any) {
  await act(async () => { await rpc('mod_remove', { id: item.id }); if (selected.value === item.id) { selected.value = ''; review.value = null; } });
}
onMounted(() => { act(load); });
</script>
<template>
  <section class="advanced-section mods-settings">
    <!-- Phase 24 (V1-107, D7; V1-108): Views is the person's word for mods, shown only with Developer features. -->
    <div class="section-label">VIEWS (DEVELOPER FEATURE)</div>
    <p class="setting-note">A view is display code (a mod) that turns ordinary data into tables, charts and other widgets in a conversation. Review the full source before enabling one. Each runs in a disposable worker with no file, network, credential, input or permission access.</p>
    <p v-if="runtime" class="setting-note">{{ runtime.available ? 'JavaScriptCore available on this Mac.' : 'Mod execution is unavailable on this platform.' }} Up to {{ runtime.max_mods }} mods; {{ runtime.max_dispatches_per_run }} worker calls per run.</p>
    <div v-if="error" class="settings-error" role="alert">{{ error }}</div>
    <article v-for="item in items" :key="item.id" class="extension-card">
      <div><strong>{{ item.name }}</strong><small>{{ item.enabled ? 'Enabled' : 'Disabled' }} · ui.render · {{ item.manifest.events.join(', ') }}</small><p v-if="item.error" class="setting-note">Last worker boundary: {{ item.error }}</p></div>
      <button class="secondary-button" :disabled="busy" @click="inspect(item)">Review source</button>
      <button class="secondary-button" :disabled="item.enabled ? busy : blocked || !runtime?.available || selected !== item.id || review?.digest !== item.digest" @click="toggle(item)">{{ item.enabled ? 'Disable' : 'Enable reviewed version' }}</button>
      <button class="secondary-button danger" :disabled="busy" @click="remove(item)">Remove</button>
    </article>
    <div class="button-row"><button class="secondary-button" :disabled="busy" @click="example">Load example</button><button class="secondary-button" :disabled="busy" @click="act(load)">Refresh</button></div>
    <label for="mod-manifest">Version 1 manifest and JavaScript source</label>
    <textarea id="mod-manifest" v-model="input" rows="12" spellcheck="false" placeholder="Paste a mod manifest to review" />
    <button class="secondary-button" :disabled="busy || !input.trim()" @click="preview">Preview exact version</button>
    <div v-if="review" class="mod-review">
      <p><strong>{{ review.manifest.name }}</strong> · {{ review.manifest.events.join(', ') }} · {{ review.manifest.capabilities.join(', ') }}</p>
      <small>Reviewed SHA-256: {{ review.digest }}</small>
      <pre>{{ review.manifest.source }}</pre>
      <p class="setting-note">Installing keeps the mod disabled. Enable the reviewed version separately when the current task is stopped.</p>
      <button class="secondary-button" :disabled="blocked" @click="install">Install disabled version</button>
    </div>
  </section>
</template>
<style scoped>
.mods-settings textarea { width: 100%; box-sizing: border-box; font: 12px ui-monospace, monospace; margin: 8px 0;
  /* Phase 45 (F017): themed like every other field (it was pure white in dark mode) */
  background: var(--bg); color: var(--text); border: 1px solid var(--line-strong); border-radius: 8px; padding: 8px; }
.mods-settings small { display: block; overflow-wrap: anywhere; }
.mod-review pre { white-space: pre-wrap; overflow-wrap: anywhere; max-height: 300px; overflow: auto; }
.button-row { display: flex; gap: 8px; margin: 12px 0; }
</style>
