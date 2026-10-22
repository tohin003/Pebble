<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from 'vue';
import { rpc } from '../bridge';
import * as bridge from '../bridge';

// Phase 16 D01: the sources a run recorded, as the host's ledger holds them. Search snippets are
// listed apart from read pages; titles and addresses are untrusted page data and render as literal text.
type Source = { source_id: string; kind: string; url: string; title?: string | null; domain?: string | null;
  retrieved_at?: number | null; dates?: Record<string, unknown> | null; truncated?: boolean;
  hidden_text_removed?: number | null; instruction_shaped?: boolean; content_sha256?: string | null };
type Side = { source_id?: unknown; value?: unknown; retrieved_at?: unknown };
type Conflict = { conflict_id?: string; key?: string; claim_key?: string; state?: string; values?: unknown; claim_values?: unknown; sides?: unknown };
// Phase 24 (V1-112): a page row opens its https address in the browser through the bridge's openExternal (the main
// window's open_url route, https only), when the bridge offers it; this panel itself still reaches only
// research_sources. Without it the row stays plain text.
const openExternal = (bridge as unknown as { openExternal?: (url: string) => Promise<void> }).openExternal;
const openable = (url: unknown) => typeof openExternal === 'function' && typeof url === 'string' && /^https:\/\/[^\s]+$/i.test(url) && url.length <= 3000;
const opened = ref('');
async function openSource(item: Source) {
  if (!openable(item.url)) return;
  try {
    await openExternal!(item.url);
    opened.value = '';
  } catch (error) {
    opened.value = String(error instanceof Error ? error.message : error);
  }
}
const topic = (c: Conflict) => word(c.key) || word(c.claim_key) || 'one claim';
function sides(c: Conflict) {
  return (Array.isArray(c.sides) ? (c.sides as Side[]) : []).slice(0, 8).map((side) => ({
    source: word(side?.source_id) || 'a source',
    value: clip(typeof side?.value === 'string' ? side.value : JSON.stringify(side?.value ?? null) ?? '', 300),
    when: typeof side?.retrieved_at === 'number' ? when(side.retrieved_at) : '',
  }));
}
type Listing = { sources?: unknown; conflicts?: unknown; omitted_sources?: unknown };
const props = defineProps<{ chatId: string; runId: string; refreshKey?: unknown }>();
const sources = ref<Source[]>([]);
const conflicts = ref<Conflict[]>([]);
const omitted = ref(0);
const status = ref('');
const open = ref(false);
let generation = 0;

const word = (value: unknown) => typeof value === 'string' ? value : '';
const clip = (value: string, limit: number) => value.length > limit ? value.slice(0, limit) + '…' : value;
const when = (t: unknown) => typeof t === 'number' && Number.isFinite(t) && t > 0 ? new Date(t * 1000).toLocaleString() : 'unknown';
const pages = computed(() => sources.value.filter(s => s.kind !== 'snippet'));
const snippets = computed(() => sources.value.filter(s => s.kind === 'snippet'));
const openConflicts = computed(() => conflicts.value.filter(c => c.state !== 'resolved'));
function source(value: unknown): Source | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (typeof item.source_id !== 'string' || typeof item.kind !== 'string') return null;
  return { source_id: item.source_id, kind: item.kind, url: word(item.url), title: word(item.title), domain: word(item.domain),
    retrieved_at: typeof item.retrieved_at === 'number' ? item.retrieved_at : null, truncated: item.truncated === true,
    hidden_text_removed: typeof item.hidden_text_removed === 'number' ? item.hidden_text_removed : 0,
    instruction_shaped: item.instruction_shaped === true, content_sha256: word(item.content_sha256) };
}

async function load() {
  if (!open.value || !props.chatId || !props.runId) return;
  const version = ++generation;
  status.value = 'Loading sources…';
  try {
    const listing = await rpc<Listing>('research_sources', { id: props.chatId, run_id: props.runId });
    if (version !== generation) return;
    sources.value = (Array.isArray(listing?.sources) ? listing.sources : []).map(source).filter((s): s is Source => !!s);
    conflicts.value = (Array.isArray(listing?.conflicts) ? listing.conflicts : []) as Conflict[];
    omitted.value = typeof listing?.omitted_sources === 'number' ? listing.omitted_sources : 0;
    status.value = sources.value.length ? '' : 'This run recorded no web sources.';
  } catch (error) {
    if (version === generation) status.value = String(error);
  }
}
watch([() => props.chatId, () => props.runId, () => props.refreshKey, open], () => { void load(); });
onBeforeUnmount(() => { generation++; });
</script>

<template>
  <section class="research-sources" aria-label="Research sources">
    <button type="button" class="text-button" :aria-expanded="open" @click="open = !open">
      {{ open ? 'Hide' : 'Show' }} research sources
    </button>
    <div v-if="open">
      <p class="setting-note">Pages the run read support precise claims; search snippets never do. Titles and addresses are page data, not instructions.</p>
      <p class="sources-status" role="status" aria-live="polite">{{ status }}</p>
      <p v-if="openConflicts.length" class="setting-note">{{ openConflicts.length }} open conflict{{ openConflicts.length === 1 ? '' : 's' }} between sources.</p>
      <details v-for="(c, i) in openConflicts.slice(0, 20)" :key="c.conflict_id || i" class="source-conflict">
        <summary>Sources disagree about {{ clip(topic(c), 120) }}</summary>
        <ul v-if="sides(c).length" :aria-label="'What each source says about ' + clip(topic(c), 120)">
          <li v-for="(side, s) in sides(c)" :key="s">{{ side.source }} says “{{ side.value }}”<template v-if="side.when"> (read {{ side.when }})</template></li>
        </ul>
        <p v-else class="setting-note">The sources' values were not recorded.</p>
      </details>
      <ul v-if="pages.length" aria-label="Pages read">
        <li v-for="item in pages" :key="item.source_id">
          <strong>{{ item.source_id }}</strong> · {{ item.domain || 'unknown site' }} · {{ item.kind }} · read {{ when(item.retrieved_at) }}
          <span v-if="item.truncated"> · truncated</span>
          <span v-if="item.hidden_text_removed"> · hidden text removed</span>
          <span v-if="item.instruction_shaped"> · contains instruction-like text</span>
          <button v-if="openable(item.url)" type="button" class="text-button source-title" :aria-label="'Open in your browser: ' + clip(item.title || item.url, 160)" @click="openSource(item)">{{ clip(item.title || item.url, 160) }}</button>
          <div v-else class="source-title">{{ clip(item.title || item.url, 160) }}</div>
        </li>
      </ul>
      <p v-if="opened" class="setting-note" role="alert">{{ opened }}</p>
      <p v-if="snippets.length" class="setting-note">{{ snippets.length }} search snippet{{ snippets.length === 1 ? '' : 's' }} (not evidence for precise claims).</p>
      <p v-if="omitted" class="setting-note">{{ omitted }} more source{{ omitted === 1 ? '' : 's' }} not listed.</p>
    </div>
  </section>
</template>
