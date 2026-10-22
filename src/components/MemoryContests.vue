<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, onMounted, ref, shallowRef, watch } from 'vue';
import { rpc } from '../bridge';

// Memories that disagree wait here for a person's decision; recall withholds
// them meanwhile. Every choice is confirmed first and names exactly what will
// be forgotten. Records are untrusted data: they render as literal text.
type MemoryRecord = { memory_id: string; kind: string; label: string | null; content: string; scope: string; origin: string; created: number | null };
type Group = MemoryRecord & { counterparts: MemoryRecord[] };
type Decision = 'keep' | 'reject' | 'keep_both';
type Page = { groups?: unknown };
type Resolution = { forgotten?: unknown };
const props = defineProps<{ active?: boolean; refreshKey?: unknown }>();
const emit = defineEmits<{ changed: []; error: [message: string] }>();
const groups = ref<Group[]>([]);
const loaded = ref(false);
const busy = ref(false);
const status = ref('');
const pending = ref<{ group: Group; decision: Decision } | null>(null);
// Element refs stay shallow: a template element is never made reactive.
const region = shallowRef<HTMLElement | null>(null), confirmCard = shallowRef<HTMLElement | null>(null);
let generation = 0, disposed = false;

const KINDS: Record<string, string> = { working: 'Working context', episodic: 'Past experiences', semantic: 'Facts & knowledge',
  procedural: 'Procedures', temporal: 'Time-sensitive information', profile: 'Your preferences' };
const QUESTIONS: Record<Decision, string> = { keep: 'Keep this memory?', reject: 'Remove this memory?', keep_both: 'Keep both?' };
const DONE: Record<Decision, string> = { keep: 'Kept this memory.', reject: 'Removed this memory.', keep_both: 'Kept both.' };
const clip = (value: string, limit: number) => value.length > limit ? value.slice(0, limit) + '…' : value;
const word = (value: unknown) => typeof value === 'string' ? value : '';
const kindName = (kind: string) => Object.hasOwn(KINDS, kind) ? KINDS[kind] : 'Memory';
const scopeName = (scope: string) => scope === 'global' ? 'Personal' : scope.startsWith('chat:') ? 'Conversation' : scope ? 'Project' : '';
const topic = (record: MemoryRecord) => record.label === 'model_lesson_unverified' ? 'Unverified lesson from the model' : record.label ? 'Topic: ' + record.label : '';
const when = (created: number | null) => created && Number.isFinite(created) && created > 0 ? new Date(created * 1000).toLocaleString() : '';
const details = (record: MemoryRecord) => [kindName(record.kind), scopeName(record.scope), record.origin, when(record.created)].filter(Boolean).join(' · ');
const summary = (record: MemoryRecord) => kindName(record.kind) + ': ' + clip(record.content, 80);
const memories = (count: number) => count === 1 ? '1 memory' : count + ' memories';
const forgetting = computed<MemoryRecord[]>(() => !pending.value ? [] : pending.value.decision === 'reject' ? [pending.value.group]
  : pending.value.decision === 'keep' ? pending.value.group.counterparts : []);
const confirmLabel = computed(() => !pending.value ? '' : pending.value.decision === 'keep_both' ? 'Keep both'
  : pending.value.decision === 'reject' ? 'Forget this memory'
  : forgetting.value.length ? 'Keep this and forget ' + memories(forgetting.value.length) : 'Keep this');

function record(value: unknown): MemoryRecord | null {
  if (!value || typeof value !== 'object') return null;
  const item = value as Record<string, unknown>;
  if (typeof item.memory_id !== 'string' || !item.memory_id) return null;
  return { memory_id: item.memory_id, kind: word(item.kind), label: typeof item.label === 'string' ? item.label : null,
    content: clip(word(item.content), 2000), scope: word(item.scope), origin: word(item.origin),
    created: typeof item.created === 'number' ? item.created : null };
}
function group(value: unknown): Group | null {
  const own = record(value);
  if (!own) return null;
  const peers = (value as { counterparts?: unknown }).counterparts;
  return { ...own, counterparts: (Array.isArray(peers) ? peers : []).map(record).filter((peer): peer is MemoryRecord => !!peer) };
}
function outcome(decision: Decision, forgotten: unknown) {
  if (!Array.isArray(forgotten)) return DONE[decision];
  const count = forgotten.length;
  return DONE[decision] + (count ? ' ' + memories(count) + (count === 1 ? ' was' : ' were') + ' forgotten.' : ' Nothing was forgotten.');
}

async function load() {
  const version = ++generation;
  try {
    const page = await rpc<Page>('memory_contests');
    if (version !== generation) return;
    const list = page?.groups;
    groups.value = (Array.isArray(list) ? list : []).map(group).filter((item): item is Group => !!item);
    loaded.value = true;
    // An open confirmation always describes the current records, or closes.
    const open = pending.value, fresh = open && groups.value.find(item => item.memory_id === open.group.memory_id);
    pending.value = open && fresh ? { group: fresh, decision: open.decision } : null;
  } catch (error) { if (version === generation) emit('error', String(error)); }
}
function ask(item: Group, decision: Decision) {
  if (busy.value || props.active) return;
  pending.value = { group: item, decision };
  void nextTick(() => confirmCard.value?.focus());
}
function cancel() {
  pending.value = null;
  void nextTick(() => region.value?.focus());
}
async function decide() {
  const choice = pending.value;
  if (busy.value || props.active || !choice) return;
  busy.value = true; status.value = '';
  try {
    const result = await rpc<Resolution>('memory_resolve_contest', { memory_id: choice.group.memory_id, decision: choice.decision });
    if (disposed) return;
    status.value = outcome(choice.decision, result?.forgotten);
    cancel();
    emit('changed');
    await load();
  } catch (error) { if (!disposed) emit('error', String(error)); }
  finally { busy.value = false; }
}
watch([() => props.active, () => props.refreshKey], () => { void load(); });
onMounted(load);
onBeforeUnmount(() => { disposed = true; generation++; });
</script>

<template>
  <section ref="region" tabindex="-1" class="memory-contests" aria-label="Contested memories" :aria-busy="busy">
    <div class="section-label">CONTESTED MEMORIES</div>
    <p class="setting-note">When two memories disagree, Pebble does not recall either as current until you decide. Keep this forgets the memories that disagree with it. Remove this forgets this one. Keep both forgets nothing. You confirm each choice first.</p>
    <p v-if="active" class="setting-note">Decide after the current task finishes.</p>
    <p class="contest-status" role="status" aria-live="polite">{{ status }}</p>
    <p v-if="loaded && !groups.length" class="contest-empty">No memory is contested.</p>
    <article v-for="item in groups" :key="item.memory_id" class="contest-group" :aria-label="'Contested ' + summary(item)">
      <div v-for="(entry, index) in [item, ...item.counterparts]" :key="index + ':' + entry.memory_id" class="contest-record" :class="{ counterpart: index > 0 }">
        <small>{{ index ? 'Disagrees with it' : 'Contested' }} · {{ details(entry) }}</small>
        <small v-if="topic(entry)">{{ topic(entry) }}</small>
        <p>{{ entry.content }}</p>
      </div>
      <div class="setting-actions" role="group" :aria-label="'Decide about ' + summary(item)">
        <button type="button" class="secondary-button" :disabled="busy || active" @click="ask(item, 'keep')">Keep this</button>
        <button type="button" class="secondary-button" :disabled="busy || active" @click="ask(item, 'reject')">Remove this</button>
        <button type="button" class="secondary-button" :disabled="busy || active" @click="ask(item, 'keep_both')">Keep both</button>
      </div>
    </article>
    <article v-if="pending" ref="confirmCard" tabindex="-1" class="contest-confirm" role="group" aria-label="Confirm your decision">
      <strong>{{ QUESTIONS[pending.decision] }}</strong>
      <p>{{ summary(pending.group) }}</p>
      <template v-if="forgetting.length">
        <p>{{ pending.decision === 'reject' ? 'This forgets:' : 'This forgets ' + memories(forgetting.length) + (forgetting.length === 1 ? ' that disagrees' : ' that disagree') + ' with it:' }}</p>
        <ul>
          <li v-for="(entry, index) in forgetting" :key="index + ':' + entry.memory_id">{{ kindName(entry.kind) }} · {{ clip(entry.content, 280) }}</li>
        </ul>
        <p>Forgetting is permanent. A forgotten memory stays forgotten, and importing an archive cannot bring it back.</p>
      </template>
      <p v-else-if="pending.decision === 'keep_both'">Nothing is forgotten. Pebble records that you accept this disagreement, so these versions are not contested again.</p>
      <p v-else>Nothing is forgotten.</p>
      <p v-if="pending.decision === 'reject' && pending.group.counterparts.length">The memories that disagree with it are kept.</p>
      <div class="setting-actions">
        <button type="button" class="secondary-button" :class="{ danger: forgetting.length > 0 }" :disabled="busy || active" @click="decide">{{ confirmLabel }}</button>
        <button type="button" class="secondary-button" :disabled="busy" @click="cancel">Cancel</button>
      </div>
    </article>
    <button type="button" class="text-button" :disabled="busy" @click="load">Check again</button>
  </section>
</template>

<style scoped>
.memory-contests { border-top: 1px solid var(--border); margin-top: 22px; padding-top: 20px; }
.memory-contests .setting-actions { flex-wrap: wrap; }
.contest-status, .contest-empty { font-size: 11px; line-height: 1.6; margin: 8px 0; overflow-wrap: anywhere; }
.contest-empty { color: var(--muted); }
.contest-group, .contest-confirm { border: 1px solid var(--border); border-radius: 10px; padding: 10px 12px; margin: 10px 0; }
.contest-record { padding: 6px 0; }
.contest-record.counterpart { border-top: 1px dashed var(--border); }
.contest-record small { display: block; font-size: 10px; line-height: 1.6; color: var(--muted); overflow-wrap: anywhere; }
.contest-record p { font-size: 11px; line-height: 1.6; white-space: pre-wrap; overflow-wrap: anywhere; margin: 5px 0; }
.contest-confirm { border-color: var(--danger-line); font-size: 11px; line-height: 1.6; }
.contest-confirm strong { display: block; font-size: 12px; }
.contest-confirm p { margin: 6px 0; overflow-wrap: anywhere; }
.contest-confirm ul { margin: 6px 0; padding-left: 18px; }
.contest-confirm li { white-space: pre-wrap; overflow-wrap: anywhere; }
</style>
