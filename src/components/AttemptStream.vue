<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from 'vue';
import { rpc, subscribe } from '../bridge';
import { emptyStream, replayStream } from '../attempt-stream.mjs';
const props = defineProps<{ chatId: string; runId?: string }>();
const state = ref(emptyStream());
const more = ref(false);
const error = ref('');
// Phase 20 H20: a draft the host withdrew after it streamed stays here, labelled, instead of vanishing at commit.
const withdrawn = ref<{ attemptId: string; text: string; partial: boolean }[]>([]);
const named = new Set<string>(); // attempt ids the host announced as withdrawn, until their settled text is read
let generation = 0, disposed = false, loading = false, requested = false;
let timer: ReturnType<typeof setTimeout> | undefined;
let release = () => {};
function adopt() {
  for (const attempt of state.value.settled) {
    if (!named.has(attempt.attemptId)) continue;
    named.delete(attempt.attemptId);
    if (!withdrawn.value.some(item => item.attemptId === attempt.attemptId)) withdrawn.value = [...withdrawn.value, attempt].slice(-4);
  }
}
async function refresh() {
  if (loading) { requested = true; return; }
  if (!props.runId || disposed) return;
  loading = true;
  requested = false;
  const version = generation, owner = props.chatId, runId = props.runId;
  try {
    const page = await rpc('stream_read', { id: owner, run_id: runId, after: state.value.cursor, limit: 50 });
    if (version !== generation || disposed) return;
    const next = replayStream(state.value, page.events);
    if (next.cursor !== page.next_after) throw new Error('Stream cursor does not match its page');
    state.value = next; more.value = !!page.more; error.value = '';
    adopt();
  } catch (failure) { if (version === generation && !disposed) error.value = String(failure); }
  finally {
    loading = false;
    if (requested && !disposed) schedule();
  }
}
function schedule() {
  if (timer || disposed) return;
  timer = setTimeout(() => { timer = undefined; void refresh(); }, 150);
}
watch(() => [props.chatId, props.runId], () => {
  generation++; state.value = emptyStream(); more.value = false; error.value = ''; withdrawn.value = []; named.clear(); schedule();
});
onMounted(async () => {
  schedule();
  const unsubscribe = await subscribe(event => {
    if (event.event === 'stream' && event.data.chat_id === props.chatId && event.data.run_id === props.runId) {
      if (event.data.type === 'withdrawn' && typeof event.data.attempt_id === 'string') { named.add(event.data.attempt_id); adopt(); }
      schedule();
    }
  });
  if (disposed) unsubscribe(); else release = unsubscribe;
});
onBeforeUnmount(() => { disposed = true; generation++; if (timer) clearTimeout(timer); release(); });
</script>
<template>
  <section v-for="draft in withdrawn" :key="draft.attemptId" aria-label="Withdrawn draft">
    <p role="status">Draft withdrawn: a check asked for a fix, so this draft is not the reply. The reply follows it.</p>
    <details>
      <summary>Show the withdrawn draft</summary>
      <p v-if="draft.partial">Showing the latest part of this draft. Earlier text remains in the saved stream.</p>
      <pre style="white-space:pre-wrap;overflow-wrap:anywhere">{{ draft.text }}</pre>
    </details>
  </section>
  <section v-if="state.state === 'open' || more || error || ['discarded', 'interrupted'].includes(state.state)" aria-label="Live attempt output">
    <p role="status">{{ state.state === 'open' ? 'Receiving model response' : state.state === 'discarded' ? 'Interrupted response discarded' : state.state === 'interrupted' ? 'Model response interrupted' : 'Response history' }}</p>
    <p v-if="state.partial">Showing the latest part of this response. Earlier text remains in the saved stream.</p>
    <pre v-if="state.state === 'open'" style="white-space:pre-wrap;overflow-wrap:anywhere">{{ state.text }}</pre>
    <button class="secondary-button" @click="refresh">{{ more ? 'Replay next page' : 'Refresh response' }}</button>
    <p v-if="error" role="alert">{{ error }}</p>
  </section>
</template>
