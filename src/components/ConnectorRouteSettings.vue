<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, reactive, ref, shallowRef, watch } from 'vue';
import { rpc } from '../bridge';

// "Route through Pebble" for a local (stdio) connector. The server gets a
// placeholder instead of the credential and a local base URL; Pebble's broker
// adds the credential only to requests for the reviewed site, methods and path.
// Saving replaces the connector's access and turns the plugin off until it is
// reviewed again; an existing environment grant is never converted silently.
// info is this plugin's connector_metadata reply, which says how exposed the
// current credential is.
type Info = Record<string, unknown>;
type Row = [string, string];
const props = defineProps<{ pluginId: string; info?: Info | null; disabled?: boolean }>();
const emit = defineEmits<{ saved: [] }>();

const METHODS = ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE', 'OPTIONS'] as const;
const EXPOSURE: Record<string, string> = {
  environment_readable_by_local_commands: "In the local server's environment. Other programs that run as you on this Mac can read it while the server runs.",
  reviewed_route_placeholder_only: 'Routed through Pebble. The server holds only a placeholder; Pebble adds the credential to the reviewed requests below.',
  host_only: 'Held by Pebble only. The server never receives it.',
};
const open = ref(false), busy = ref(false), message = ref(''), problem = ref('');
const methods = ref<string[]>(['GET']);
const form = reactive(blank());
const latest = shallowRef<Info | null>(null);
// Element refs stay shallow: a template element is never made reactive.
const region = shallowRef<HTMLElement | null>(null), card = shallowRef<HTMLElement | null>(null);
let disposed = false;

function blank() { return { name: 'API_KEY', token: '', base_url_env: '', origin: '', path_prefix: '/' }; }
function text(value: unknown, limit = 300) {
  const result = typeof value === 'string' ? value : typeof value === 'number' && Number.isFinite(value) ? String(value) : '';
  return result.length > limit ? result.slice(0, limit) + '…' : result;
}
function record(value: unknown): Info { return value && typeof value === 'object' && !Array.isArray(value) ? value as Info : {}; }
const shown = computed(() => latest.value ?? props.info ?? null);
const exposure = computed(() => {
  const info = shown.value, label = text(info?.credential_exposure, 80);
  if (label && Object.hasOwn(EXPOSURE, label)) return EXPOSURE[label];
  if (!info) return 'Not checked yet.';
  return info.state === 'disconnected' ? 'No connector access is saved.' : 'Not reported.';
});
const routeFacts = computed<Row[]>(() => {
  const info = shown.value;
  if (!info || info.kind !== 'environment_route') return [];
  const route = record(record(info.endpoints).credential_route), names = Array.isArray(info.environment_names) ? info.environment_names : [];
  const listed = Array.isArray(route.methods) ? route.methods.map(item => text(item, 10)).filter(Boolean) : [];
  return ([
    ['Site', text(route.origin)],
    ['Methods', listed.join(', ')],
    ['Path prefix', text(route.path_prefix, 1024)],
    ['Base-URL variable', text(route.base_url_env, 64)],
    ['Credential variable', text(names[0], 64)],
    ['Header', text(route.header, 64) + (route.scheme ? ' · ' + text(route.scheme, 32) : '')],
  ] as Row[]).filter(([, value]) => value !== '');
});
const ready = computed(() => !!form.name.trim() && !!form.token && !!form.base_url_env.trim() && !!form.origin.trim()
  && !!form.path_prefix.trim() && methods.value.length > 0);

function reset() { Object.assign(form, blank()); methods.value = ['GET']; }
function begin() {
  if (busy.value || props.disabled) return;
  reset(); open.value = true; message.value = ''; problem.value = '';
  void nextTick(() => card.value?.focus());
}
function cancel() {
  open.value = false; reset(); problem.value = '';
  void nextTick(() => region.value?.focus());
}
function toggleMethod(method: string) { methods.value = methods.value.includes(method) ? methods.value.filter(item => item !== method) : [...methods.value, method]; }
async function save() {
  if (busy.value || props.disabled || !ready.value) return;
  // The credential leaves the form before the request; a refusal never keeps it.
  const token = form.token, plugin = props.pluginId;
  form.token = '';
  busy.value = true; message.value = ''; problem.value = '';
  try {
    const result = await rpc<Info>('connector_save_environment_route', { id: plugin, name: form.name.trim(), token,
      base_url_env: form.base_url_env.trim(), origin: form.origin.trim(), methods: METHODS.filter(item => methods.value.includes(item)),
      path_prefix: form.path_prefix.trim() });
    if (disposed || plugin !== props.pluginId) return;
    latest.value = result && typeof result === 'object' && !Array.isArray(result) ? result : null;
    message.value = latest.value?.storage_cleanup_pending
      ? 'Route saved. Removing the previous credential from secure storage needs another attempt; disconnect to retry.'
      : 'Route saved in secure storage. The server gets only a placeholder. Review and enable the plugin separately.';
    open.value = false; reset();
    emit('saved');
  } catch (cause) {
    if (!disposed && plugin === props.pluginId) problem.value = ((cause instanceof Error ? cause.message : String(cause)) || 'The route was not saved.').slice(0, 1000);
  } finally { busy.value = false; }
}
watch(() => props.info, () => { latest.value = null; });
watch(() => props.pluginId, () => { open.value = false; reset(); latest.value = null; message.value = ''; problem.value = ''; });
onBeforeUnmount(() => { disposed = true; form.token = ''; });
</script>

<template>
  <section ref="region" tabindex="-1" class="connector-route" aria-label="Route through Pebble">
    <p class="setting-note">Current credential exposure: {{ exposure }}</p>
    <dl v-if="routeFacts.length" class="route-facts" aria-label="Reviewed route"><template v-for="[name, value] in routeFacts" :key="name"><dt>{{ name }}</dt><dd>{{ value }}</dd></template></dl>
    <p class="route-status" role="status" aria-live="polite">{{ message }}</p>
    <p v-if="problem" class="route-problem" role="alert">{{ problem }}</p>
    <button v-if="!open" type="button" class="text-button" :disabled="busy || disabled" @click="begin">Route through Pebble instead…</button>
    <article v-else ref="card" tabindex="-1" class="route-form" role="group" aria-label="Route through Pebble settings">
      <strong>Route through Pebble</strong>
      <p class="setting-note">Instead of the credential, the server gets a placeholder, and its base-URL variable points to a local address that only Pebble answers. Pebble adds the credential only to requests that match the site, methods and path below, so other programs can't read it from the server. Use this when the server lets you set its API base URL.</p>
      <label>Credential variable name<input v-model="form.name" maxlength="64" autocomplete="off" :spellcheck="false" /></label>
      <label>Base-URL variable the server reads<input v-model="form.base_url_env" maxlength="64" autocomplete="off" :spellcheck="false" placeholder="EXAMPLE_API_URL" /></label>
      <label>Site (HTTPS origin)<input v-model="form.origin" maxlength="300" autocomplete="off" :spellcheck="false" placeholder="https://api.example.com" /></label>
      <fieldset>
        <legend>Methods</legend>
        <label v-for="method in METHODS" :key="method" class="route-method"><input type="checkbox" :checked="methods.includes(method)" @change="toggleMethod(method)" />{{ method }}</label>
      </fieldset>
      <label>Path prefix<input v-model="form.path_prefix" maxlength="1024" autocomplete="off" :spellcheck="false" placeholder="/v1/" /></label>
      <label>Connector credential<input v-model="form.token" type="password" maxlength="8192" autocomplete="off" :spellcheck="false" /></label>
      <p class="setting-note">Pebble sends it as "Authorization: Bearer". Saving replaces this connector's current access and turns the plugin off until you review and enable it again.</p>
      <div class="setting-actions">
        <button type="button" class="secondary-button" :disabled="busy || disabled || !ready" @click="save">Save route and grant access</button>
        <button type="button" class="secondary-button" :disabled="busy" @click="cancel">Cancel</button>
      </div>
    </article>
  </section>
</template>

<style scoped>
.connector-route { margin: 10px 0; font-size: 11px; line-height: 1.6; overflow-wrap: anywhere; }
.route-facts { display: grid; grid-template-columns: max-content 1fr; gap: 3px 10px; margin: 6px 0; }
.route-facts dt { color: var(--muted); }
.route-facts dd { margin: 0; overflow-wrap: anywhere; }
.route-status, .route-problem { margin: 6px 0; }
.route-problem { color: var(--danger); }
.route-form { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 8px 0; }
.route-form strong { display: block; font-size: 12px; }
.route-form fieldset { border: 0; margin: 6px 0; padding: 0; }
.route-form legend { font-weight: 600; }
.route-method { display: inline-block; margin-right: 10px; }
</style>
