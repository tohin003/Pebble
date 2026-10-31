<script setup lang="ts">
import { computed } from "vue";
import { isLiveState, type RunActivity } from "../activity";
import { parsePlan, stripOf } from "../workView";

// Phase 46 (1.9.29, D206): the companion's strip of the work view: the current card in a few words and the plan's
// progress, under the status line. The companion has no timeline route (main.rs keeps its list short), so the
// strip is fed by the live `activity` and `plan` events it already receives; before the first one it shows nothing.
// Not a live region: StatusLine announces the state once; the strip updates silently. No buttons: the workspace has
// the cards.
const props = withDefaults(defineProps<{ record: RunActivity | null; reduced?: boolean }>(), { reduced: false });
const plan = computed(() => parsePlan(props.record?.plan));
const strip = computed(() => (props.record && isLiveState(props.record.state) ? stripOf(props.record, plan.value) : null));
const label = computed(() => (strip.value && strip.value.total ? `${strip.value.done} of ${strip.value.total} plan steps done` : "In progress"));
</script>

<template>
  <div v-if="strip" class="work-strip" :class="{ reduced }" role="group" aria-label="What Pebble is doing now">
    <span class="strip-words">{{ strip.words }}</span>
    <span class="strip-meter" role="img" :aria-label="label">
      <span v-if="strip.total" class="strip-fill" :style="{ width: (strip.done / strip.total) * 100 + '%' }"></span>
      <span v-else class="strip-fill open"></span>
    </span>
  </div>
</template>

<style scoped>
.work-strip {
  display: grid;
  gap: 4px;
  padding: 6px 12px 8px;
  border-bottom: 1px solid var(--line);
  font-size: var(--text-xs);
  color: var(--secondary);
}
.strip-words {
  overflow: hidden;
  white-space: nowrap;
  text-overflow: ellipsis;
}
.strip-meter {
  position: relative;
  height: 3px;
  border-radius: 2px;
  background: var(--line);
  overflow: hidden;
}
.strip-fill {
  position: absolute;
  inset: 0 auto 0 0;
  background: var(--accent);
  transition: width 0.3s ease;
}
.strip-fill.open {
  width: 30%;
  animation: strip-slide 1.8s ease-in-out infinite alternate;
}
@keyframes strip-slide {
  from {
    left: 0;
  }
  to {
    left: 70%;
  }
}
.reduced .strip-fill,
.reduced .strip-fill.open {
  transition: none;
  animation: none;
}
@media (prefers-reduced-motion: reduce) {
  .strip-fill,
  .strip-fill.open {
    transition: none;
    animation: none;
  }
}
</style>
