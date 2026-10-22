<script setup lang="ts">
import { computed } from "vue";
import type { Usage } from "../types";
// Phase 22 (design study §9): only what the provider reported is shown, in the message's quiet
// action row: "1,240 in · 312 out · 3.1 s". Unknown stays unknown rather than zero; with nothing
// reported (or zero in and zero out) there is no meter at all. tok/s appears only with at least 64
// output tokens over at least 1 s of generation. Direct routes keep their honest label.
// When the task's totals are unknown (an attempt was sent but reported nothing), the meter shows what
// the provider did report (usage.reported, t22-stream), led by a muted "Reported" that says so.
const props = defineProps<{ usage: Usage }>();
const reported = (n: unknown): n is number => typeof n === "number" && Number.isFinite(n) && n >= 0;
const route = computed(() => (props.usage.route && props.usage.route !== "model" ? props.usage.route : ""));
type Counts = { input: unknown; cached: unknown; output: unknown; rate: unknown; seconds: number | null };
function list(c: Counts) {
  const out: { key: string; text: string; title: string }[] = [];
  if (reported(c.input)) out.push({ key: "in", text: `${c.input.toLocaleString()} in`, title: "Input tokens across this task's model calls" });
  if (reported(c.cached) && c.cached > 0) out.push({ key: "cached", text: `${c.cached.toLocaleString()} cached`, title: "Input tokens reused from a cache" });
  if (reported(c.output)) out.push({ key: "out", text: `${c.output.toLocaleString()} out`, title: "Output tokens, including reasoning when the provider counts it" });
  if (c.seconds !== null && c.seconds >= 0.1) out.push({ key: "time", text: `${c.seconds.toFixed(1)} s`, title: "Generation time" });
  const rate = reported(c.rate) && c.rate > 0 ? c.rate : null;
  if (rate !== null && reported(c.output) && c.output >= 64 && c.seconds !== null && c.seconds >= 1)
    out.push({ key: "rate", text: `${rate.toLocaleString(undefined, { maximumFractionDigits: 0 })} tok/s`, title: "Output tokens per generation second" });
  // Zero input and zero output means nothing was reported, whatever the other fields say.
  const counted = (reported(c.input) && c.input > 0) || (reported(c.output) && c.output > 0);
  return counted ? out : [];
}
const meter = computed(() => {
  const u = props.usage;
  const seconds = reported(u.seconds) ? u.seconds : reported(u.generation_seconds) ? u.generation_seconds : null;
  if (reported(u.input_tokens) || reported(u.output_tokens))
    return { partial: false, fields: list({ input: u.input_tokens, cached: u.cached_tokens, output: u.output_tokens, rate: u.tokens_per_second, seconds }) };
  const r = u.reported;
  if (!r || typeof r !== "object") return { partial: false, fields: [] };
  const generation = reported(r.generation_seconds) && r.generation_seconds > 0 ? r.generation_seconds : seconds;
  return { partial: true, fields: list({ input: r.input, cached: r.cached, output: r.output, rate: r.tokens_per_second, seconds: generation }) };
});
</script>
<template>
  <span v-if="route" class="token-stats" role="group" aria-label="Generation statistics">
    <span>{{
      route === "memory"
        ? "Memory saved"
        : route === "weather"
          ? "Live weather lookup"
          : "Direct app workflow"
    }}</span
    ><span>No model tokens</span>
  </span>
  <span
    v-else-if="meter.fields.length"
    class="token-stats"
    role="group"
    :aria-label="meter.partial ? 'Generation statistics, as reported; a request that reported nothing may have used more' : 'Generation statistics'"
  >
    <span v-if="meter.partial" class="reported" title="What the provider reported. A request that reported nothing may have used more, so the task's totals are unknown.">Reported</span>
    <span v-for="field in meter.fields" :key="field.key" :title="field.title">{{ field.text }}</span>
  </span>
</template>
<style scoped>
.token-stats {
  display: inline-flex;
  flex-wrap: wrap;
  gap: 2px 8px;
  font-size: 11px;
  line-height: 1.6;
  color: var(--muted);
  font-variant-numeric: tabular-nums;
}
.token-stats span + span::before {
  content: "·";
  margin-right: 8px;
  opacity: 0.6;
}
.token-stats .reported {
  font-style: italic;
}
</style>
